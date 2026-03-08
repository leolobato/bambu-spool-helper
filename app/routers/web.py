"""Jinja2 + HTMX routes for Spoolman filament linking."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import FilamentProfileResponse, SpoolmanFilament, SpoolmanSpool, TrayStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["Web"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _filter_profiles(profiles: list[FilamentProfileResponse], search: str) -> list[FilamentProfileResponse]:
    term = search.strip().lower()
    if not term:
        return profiles
    return [
        profile
        for profile in profiles
        if term in profile.name.lower() or term in profile.filament_type.lower()
    ]


def _filter_filaments(filaments: list[SpoolmanFilament], filter_mode: str, search: str) -> list[SpoolmanFilament]:
    filtered = filaments
    if filter_mode == "linked":
        filtered = [filament for filament in filtered if filament.is_linked]
    elif filter_mode == "unlinked":
        filtered = [filament for filament in filtered if not filament.is_linked]

    term = search.strip().lower()
    if not term:
        return filtered

    return [
        filament
        for filament in filtered
        if term in filament.display_name.lower() or term in (filament.material or "").lower()
    ]


async def _load_filaments(request: Request) -> tuple[list[SpoolmanFilament], str | None]:
    spoolman = request.app.state.spoolman
    try:
        filaments = await spoolman.get_filaments()
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch filaments from Spoolman: %s", exc)
        return [], f"Spoolman request failed: {exc}"
    return filaments, None


def _find_linked_profile(
    profiles: list[FilamentProfileResponse],
    filament: SpoolmanFilament,
) -> FilamentProfileResponse | None:
    if filament.bambu_setting_id and filament.bambu_filament_id:
        profile = next(
            (
                profile
                for profile in profiles
                if profile.setting_id == filament.bambu_setting_id
                and profile.filament_id == filament.bambu_filament_id
            ),
            None,
        )
        if profile:
            return profile

    if filament.bambu_filament_id:
        return next(
            (profile for profile in profiles if profile.filament_id == filament.bambu_filament_id),
            None,
        )

    return None


async def _render_filament_detail(
    request: Request,
    filament_id: int,
    profile_search: str = "",
) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    filament = next((item for item in filaments if item.id == filament_id), None)
    if filament is None:
        raise HTTPException(status_code=404, detail="Filament not found")

    profiles = request.app.state.orcaslicer.get_profiles()
    linked_profile = _find_linked_profile(profiles, filament)
    filtered_profiles = _filter_profiles(profiles, profile_search)

    return templates.TemplateResponse(
        "partials/filament_detail.html",
        {
            "request": request,
            "filament": filament,
            "profiles": filtered_profiles,
            "profile_search": profile_search,
            "selected_setting_id": filament.bambu_setting_id or "",
            "linked_profile": linked_profile,
            "error": error,
        },
    )


@router.get("/")
async def index(request: Request) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    profiles = request.app.state.orcaslicer.get_profiles()
    filter_mode = "all"
    search = ""
    filtered_filaments = _filter_filaments(filaments, filter_mode, search)

    selected_filament = filtered_filaments[0] if filtered_filaments else None
    linked_profile = _find_linked_profile(profiles, selected_filament) if selected_filament else None

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "filaments": filtered_filaments,
            "error": error,
            "filter_mode": filter_mode,
            "search": search,
            "selected_filament_id": selected_filament.id if selected_filament else None,
            "filament": selected_filament,
            "profiles": profiles,
            "profile_search": "",
            "selected_setting_id": selected_filament.bambu_setting_id if selected_filament else "",
            "linked_profile": linked_profile,
            "active_page": "filaments",
        },
    )


@router.get("/filaments")
async def filament_list(
    request: Request,
    filter: str = Query(default="all", pattern="^(all|linked|unlinked)$"),
    search: str = Query(default=""),
    selected: int | None = Query(default=None),
) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    filtered_filaments = _filter_filaments(filaments, filter, search)

    selected_id = selected
    if selected_id is None and filtered_filaments:
        selected_id = filtered_filaments[0].id

    return templates.TemplateResponse(
        "partials/filament_list.html",
        {
            "request": request,
            "filaments": filtered_filaments,
            "error": error,
            "selected_filament_id": selected_id,
        },
    )


@router.get("/filament/{filament_id}")
async def filament_detail(
    request: Request,
    filament_id: int,
) -> HTMLResponse:
    return await _render_filament_detail(request, filament_id)


@router.post("/link/{filament_id}")
async def link_filament(
    request: Request,
    filament_id: int,
    setting_id: str = Form(...),
    profile_search: str = Form(default=""),
) -> HTMLResponse:
    profiles = request.app.state.orcaslicer.get_profiles()
    profile = next((profile for profile in profiles if profile.setting_id == setting_id), None)
    if profile is None:
        raise HTTPException(status_code=400, detail="Invalid setting_id")

    try:
        await request.app.state.spoolman.link_filament(
            filament_id=filament_id,
            bambu_filament_id=profile.filament_id,
            bambu_setting_id=profile.setting_id,
            bambu_filament_type=profile.filament_type,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to link filament: {exc}") from exc

    return await _render_filament_detail(request, filament_id, profile_search=profile_search)


@router.post("/unlink/{filament_id}")
async def unlink_filament(request: Request, filament_id: int) -> HTMLResponse:
    try:
        await request.app.state.spoolman.unlink_filament(filament_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to unlink filament: {exc}") from exc

    return await _render_filament_detail(request, filament_id)


def _build_tray_statuses(request: Request) -> list[TrayStatus]:
    mqtt = request.app.state.mqtt
    tray_data = mqtt.get_tray_data()
    statuses = []
    for i in range(5):  # 0-3 AMS, 4 external
        td = tray_data.get(i)
        if td:
            statuses.append(TrayStatus(
                tray_index=i,
                tray_type=td.tray_type,
                tray_color=td.tray_color,
                tray_info_idx=td.tray_info_idx,
                nozzle_temp_min=td.nozzle_temp_min,
                nozzle_temp_max=td.nozzle_temp_max,
            ))
        else:
            statuses.append(TrayStatus(tray_index=i))
    return statuses


async def _load_spools(request: Request) -> tuple[list[SpoolmanSpool], str | None]:
    spoolman = request.app.state.spoolman
    try:
        spools = await spoolman.get_spools()
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch spools from Spoolman: %s", exc)
        return [], f"Spoolman request failed: {exc}"
    return spools, None


def _normalize_match_key(value: str | None) -> str:
    return (value or "").strip().upper()


def _split_setting_id(value: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z]+)(\d+(?:_\d+)?)$", value)
    if not match:
        return value, ""
    return match.group(1).upper(), match.group(2).split("_")[0]


def _is_fuzzy_setting_match(tray_setting: str, spool_setting: str) -> bool:
    if not tray_setting or not spool_setting:
        return False
    tray_prefix, tray_num = _split_setting_id(tray_setting)
    spool_prefix, spool_num = _split_setting_id(spool_setting)
    if tray_num != spool_num:
        return False
    return (
        tray_prefix == spool_prefix
        or tray_prefix in spool_prefix
        or spool_prefix in tray_prefix
        or tray_prefix.replace("S", "") == spool_prefix.replace("S", "")
    )


def _build_tray_picker_state(tray: TrayStatus, linked_spools: list[SpoolmanSpool]) -> dict[str, int | str | None]:
    tray_key = _normalize_match_key(tray.tray_info_idx)
    if not tray_key:
        return {"selected_spool_id": None, "no_match_message": None}

    exact_setting_matches: list[SpoolmanSpool] = []
    exact_filament_matches: list[SpoolmanSpool] = []
    fuzzy_setting_matches: list[SpoolmanSpool] = []

    for spool in linked_spools:
        setting_key = _normalize_match_key(spool.filament.bambu_setting_id)
        filament_key = _normalize_match_key(spool.filament.bambu_filament_id)
        if tray_key == setting_key:
            exact_setting_matches.append(spool)
        elif tray_key == filament_key:
            exact_filament_matches.append(spool)
        elif _is_fuzzy_setting_match(tray_key, setting_key):
            fuzzy_setting_matches.append(spool)

    def _pick_first(matches: list[SpoolmanSpool]) -> int | None:
        if not matches:
            return None
        best = sorted(matches, key=lambda item: (item.display_name.lower(), item.id))[0]
        return best.id

    selected_spool_id = _pick_first(exact_setting_matches)
    if selected_spool_id is None:
        selected_spool_id = _pick_first(exact_filament_matches)
    if selected_spool_id is None:
        selected_spool_id = _pick_first(fuzzy_setting_matches)
    if selected_spool_id is not None:
        return {"selected_spool_id": selected_spool_id, "no_match_message": None}

    return {
        "selected_spool_id": None,
        "no_match_message": f"No linked spool matches AMS setting {tray.tray_info_idx}.",
    }


def _build_tray_picker_states(
    trays: list[TrayStatus],
    linked_spools: list[SpoolmanSpool],
) -> dict[int, dict[str, int | str | None]]:
    return {
        tray.tray_index: _build_tray_picker_state(tray, linked_spools)
        for tray in trays
    }


def _build_mqtt_status(request: Request) -> dict[str, str | int | bool | None]:
    status = request.app.state.mqtt.get_connection_status()
    configured = bool(status["configured"])
    connected = bool(status["connected"])
    tray_count = int(status["tray_count"])
    last_error = status.get("last_error")
    last_message_at = status.get("last_message_at")

    if not configured:
        return {
            "state": "not_configured",
            "configured": configured,
            "connected": connected,
            "tray_count": tray_count,
            "message": "Printer MQTT is not configured. Set PRINTER_IP, PRINTER_ACCESS_CODE, and PRINTER_SERIAL.",
            "last_error": last_error,
            "last_message_at": last_message_at,
        }

    if connected:
        if tray_count == 0:
            message = "Connected to printer. Waiting for AMS tray report..."
        else:
            message = f"Connected to printer. Loaded {tray_count} tray report(s)."
        return {
            "state": "connected",
            "configured": configured,
            "connected": connected,
            "tray_count": tray_count,
            "message": message,
            "last_error": last_error,
            "last_message_at": last_message_at,
        }

    message = "Printer MQTT is disconnected."
    if last_error:
        message = f"{message} {last_error}"
    return {
        "state": "disconnected",
        "configured": configured,
        "connected": connected,
        "tray_count": tray_count,
        "message": message,
        "last_error": last_error,
        "last_message_at": last_message_at,
    }


async def _build_trays_context(request: Request) -> dict:
    request.app.state.mqtt.request_full_status()
    tray_statuses = _build_tray_statuses(request)
    spools, error = await _load_spools(request)
    profiles = request.app.state.orcaslicer.get_profiles()
    linked_spools = [s for s in spools if s.filament.is_linked]
    tray_picker_states = _build_tray_picker_states(tray_statuses, linked_spools)

    return {
        "request": request,
        "trays": tray_statuses,
        "spools": linked_spools,
        "profiles": profiles,
        "tray_picker_states": tray_picker_states,
        "error": error,
        "mqtt_status": _build_mqtt_status(request),
    }


@router.get("/trays")
async def trays_page(request: Request) -> HTMLResponse:
    context = await _build_trays_context(request)
    context["active_page"] = "trays"
    return templates.TemplateResponse("trays.html", context)


@router.get("/trays/content")
async def trays_content(request: Request) -> HTMLResponse:
    context = await _build_trays_context(request)
    return templates.TemplateResponse("partials/trays_content.html", context)


@router.get("/tray/{tray_index}")
async def tray_detail(
    request: Request,
    tray_index: int,
    search: str = Query(default=""),
) -> HTMLResponse:
    tray_statuses = _build_tray_statuses(request)
    tray = next((t for t in tray_statuses if t.tray_index == tray_index), None)
    if tray is None:
        raise HTTPException(status_code=404, detail="Tray not found")

    spools, error = await _load_spools(request)
    linked_spools = [s for s in spools if s.filament.is_linked]
    profiles = request.app.state.orcaslicer.get_profiles()
    picker_state = _build_tray_picker_state(tray, linked_spools)

    term = search.strip().lower()
    if term:
        linked_spools = [
            s for s in linked_spools
            if term in s.display_name.lower() or term in (s.filament.material or "").lower()
        ]

    return templates.TemplateResponse(
        "partials/tray_detail.html",
        {
            "request": request,
            "tray": tray,
            "spools": linked_spools,
            "profiles": profiles,
            "search": search,
            "error": error,
            "selected_spool_id": picker_state["selected_spool_id"],
            "no_match_message": picker_state["no_match_message"],
        },
    )


@router.post("/tray/{tray_index}/assign")
async def assign_spool_to_tray(
    request: Request,
    tray_index: int,
    spool_id: int = Form(...),
) -> HTMLResponse:
    spools, _ = await _load_spools(request)
    spool = next((s for s in spools if s.id == spool_id), None)
    if spool is None:
        raise HTTPException(status_code=400, detail="Spool not found")

    filament = spool.filament
    if not filament.is_linked:
        raise HTTPException(status_code=400, detail="Spool filament is not linked to a profile")

    orcaslicer = request.app.state.orcaslicer
    profile = orcaslicer.find_profile(
        filament.bambu_setting_id or "",
        filament.bambu_filament_id or "",
    )
    if not profile:
        raise HTTPException(status_code=400, detail="Linked profile not found")

    mqtt = request.app.state.mqtt
    success, message = mqtt.activate_filament(
        tray=tray_index,
        filament_id=profile.filament_id,
        color_hex=filament.color_hex or "FFFFFF",
        nozzle_temp_min=profile.nozzle_temp_min,
        nozzle_temp_max=profile.nozzle_temp_max,
        filament_type=profile.filament_type,
    )

    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to activate: {message}")

    tray_statuses = _build_tray_statuses(request)
    spools_fresh, error = await _load_spools(request)
    linked_spools = [s for s in spools_fresh if s.filament.is_linked]
    profiles = request.app.state.orcaslicer.get_profiles()
    tray = tray_statuses[tray_index]
    picker_state = _build_tray_picker_state(tray, linked_spools)

    return templates.TemplateResponse(
        "partials/tray_card.html",
        {
            "request": request,
            "tray": tray,
            "spools": linked_spools,
            "profiles": profiles,
            "selected_spool_id": picker_state["selected_spool_id"],
            "no_match_message": picker_state["no_match_message"],
            "assign_success": f"Assigned {spool.display_name} to {tray.label}",
        },
    )


@router.get("/profiles")
async def profile_picker(
    request: Request,
    filament_id: int = Query(...),
    selected_setting_id: str = Query(default=""),
    search: str = Query(default=""),
) -> HTMLResponse:
    profiles = request.app.state.orcaslicer.get_profiles()
    filtered_profiles = _filter_profiles(profiles, search)

    return templates.TemplateResponse(
        "partials/profile_picker.html",
        {
            "request": request,
            "filament_id": filament_id,
            "profiles": filtered_profiles,
            "profile_search": search,
            "selected_setting_id": selected_setting_id,
        },
    )
