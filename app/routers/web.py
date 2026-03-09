"""Jinja2 + HTMX routes for Spoolman filament linking."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import FilamentProfileResponse, SpoolmanFilament, SpoolmanSpool, TrayStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["Web"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _filter_profiles(profiles: list[FilamentProfileResponse], search: str) -> list[FilamentProfileResponse]:
    term = search.strip().casefold()
    if not term:
        return profiles

    def _profile_id_terms(value: str) -> list[str]:
        normalized = str(value or "").strip().upper()
        if not normalized:
            return []
        variants = [normalized]
        if normalized.startswith("O"):
            variants.append(normalized[1:])
        else:
            variants.append(f"O{normalized}")
        return variants

    return [
        profile
        for profile in profiles
        if any(
            term in candidate
            for candidate in (
                profile.name.casefold(),
                profile.filament_type.casefold(),
                profile.tray_info_idx.casefold(),
                profile.filament_id.casefold(),
                *[value.casefold() for value in _profile_id_terms(profile.tray_info_idx)],
                *[value.casefold() for value in _profile_id_terms(profile.filament_id)],
            )
        )
    ]


def _normalize_profile_id(value: str) -> str:
    return str(value or "").strip().upper()


def _profile_ids_match(left: str, right: str) -> bool:
    left_norm = _normalize_profile_id(left)
    right_norm = _normalize_profile_id(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if left_norm.startswith("O") and left_norm[1:] == right_norm:
        return True
    if right_norm.startswith("O") and right_norm[1:] == left_norm:
        return True
    return False


def _find_profile_by_linked_id(
    profiles: list[FilamentProfileResponse],
    linked_id: str,
) -> FilamentProfileResponse | None:
    linked_id = str(linked_id or "").strip()
    if not linked_id:
        return None

    # Primary: linked id is Orca filament_id.
    profile = next(
        (profile for profile in profiles if _profile_ids_match(profile.filament_id, linked_id)),
        None,
    )
    if profile:
        return profile

    # Backward compatibility: older links stored tray_info_idx.
    return next(
        (profile for profile in profiles if _profile_ids_match(profile.tray_info_idx, linked_id)),
        None,
    )


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
    return _find_profile_by_linked_id(profiles, filament.ams_filament_id or "")


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
            "selected_linked_filament_id": (
                linked_profile.filament_id if linked_profile else (filament.ams_filament_id or "")
            ),
            "linked_profile": linked_profile,
            "error": error,
        },
    )


def _render_import_profile_modal(
    request: Request,
    *,
    error_message: str = "",
    success_message: str = "",
    import_result: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/import_profile.html",
        {
            "request": request,
            "error_message": error_message,
            "success_message": success_message,
            "import_result": import_result or {},
        },
        headers=headers,
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
            "selected_linked_filament_id": (
                linked_profile.filament_id if linked_profile else (selected_filament.ams_filament_id if selected_filament else "")
            ),
            "linked_profile": linked_profile,
            "active_page": "filaments",
        },
    )


@router.get("/import-profile")
async def import_profile_modal(request: Request) -> HTMLResponse:
    return _render_import_profile_modal(request)


@router.post("/import-profile")
async def import_profile_upload(
    request: Request,
    profile_file: UploadFile = File(...),
) -> HTMLResponse:
    filename = profile_file.filename or ""
    if not filename:
        return _render_import_profile_modal(request, error_message="Please choose a JSON file.")
    if not filename.lower().endswith(".json"):
        return _render_import_profile_modal(request, error_message="Only .json profile files are supported.")

    try:
        raw = await profile_file.read()
    finally:
        await profile_file.close()

    if not raw:
        return _render_import_profile_modal(request, error_message="Uploaded file is empty.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _render_import_profile_modal(request, error_message="Profile file must be UTF-8 encoded JSON.")
    except json.JSONDecodeError:
        return _render_import_profile_modal(request, error_message="Invalid JSON file.")

    if not isinstance(payload, dict):
        return _render_import_profile_modal(
            request,
            error_message="Profile JSON must be an object.",
        )

    try:
        result = await request.app.state.orcaslicer.import_profile(payload)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip()
        if not error_detail:
            error_detail = str(exc)
        return _render_import_profile_modal(
            request,
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            error_message=f"Import request failed: {exc}",
        )

    tray_info_idx = str(result.get("tray_info_idx", "")).strip()
    success_message = f"Imported profile {result.get('name', tray_info_idx or 'successfully')}."
    headers = {"HX-Trigger": json.dumps({"profiles-imported": {"tray_info_idx": tray_info_idx}})}
    return _render_import_profile_modal(
        request,
        success_message=success_message,
        import_result=result,
        headers=headers,
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
    linked_filament_id: str = Form(...),
    profile_search: str = Form(default=""),
) -> HTMLResponse:
    profiles = request.app.state.orcaslicer.get_profiles()
    profile = _find_profile_by_linked_id(profiles, linked_filament_id)
    if profile is None:
        raise HTTPException(status_code=400, detail="Invalid filament_id")
    if not profile.filament_id:
        raise HTTPException(status_code=400, detail="Selected profile has no filament_id")

    try:
        await request.app.state.spoolman.link_filament(
            filament_id=filament_id,
            ams_filament_id=profile.filament_id,
            ams_filament_type=profile.filament_type,
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
                tray_sub_brands=td.tray_sub_brands,
                tag_uid=td.tag_uid,
                nozzle_temp_min=td.nozzle_temp_min,
                nozzle_temp_max=td.nozzle_temp_max,
                bed_temp=td.bed_temp,
                remain=td.remain,
                tray_weight=td.tray_weight,
                k=td.k,
                n=td.n,
                tray_uuid=td.tray_uuid,
                cali_idx=td.cali_idx,
            ))
        else:
            statuses.append(TrayStatus(tray_index=i))
    return statuses


def _sort_spools(spools: list[SpoolmanSpool]) -> list[SpoolmanSpool]:
    return sorted(
        spools,
        key=lambda spool: (
            spool.display_name.casefold(),
            (spool.filament.material or "").casefold(),
            spool.id,
        ),
    )


async def _load_spools(request: Request) -> tuple[list[SpoolmanSpool], str | None]:
    spoolman = request.app.state.spoolman
    try:
        spools = _sort_spools(await spoolman.get_spools())
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch spools from Spoolman: %s", exc)
        return [], f"Spoolman request failed: {exc}"
    return spools, None


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
    linked_spools = [s for s in spools if s.filament.is_linked]

    return {
        "request": request,
        "trays": tray_statuses,
        "spools": linked_spools,
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
            "search": search,
            "error": error,
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
    linked_filament_id = (filament.ams_filament_id or "").strip()
    if not linked_filament_id:
        raise HTTPException(status_code=400, detail="Spool filament missing ams_filament_id")

    tray_statuses = _build_tray_statuses(request)
    tray = next((t for t in tray_statuses if t.tray_index == tray_index), None)
    if tray is None:
        raise HTTPException(status_code=404, detail="Tray not found")

    profiles = request.app.state.orcaslicer.get_profiles()
    profile = _find_profile_by_linked_id(profiles, linked_filament_id)
    if not profile:
        raise HTTPException(
            status_code=400,
            detail=f"OrcaSlicer profile not found for filament_id={linked_filament_id}",
        )
    tray_info_idx = (profile.filament_id or "").strip()
    if not tray_info_idx:
        raise HTTPException(status_code=400, detail="Linked profile missing filament_id")

    mqtt = request.app.state.mqtt
    success, message = mqtt.activate_filament(
        tray=tray_index,
        tray_info_idx=tray_info_idx,
        color_hex=filament.color_hex or "FFFFFF",
        nozzle_temp_min=profile.nozzle_temp_min,
        nozzle_temp_max=profile.nozzle_temp_max,
        filament_type=profile.filament_type,
        tag_uid=tray.tag_uid or None,
        bed_temp=profile.bed_temp_min,
        tray_weight=tray.tray_weight if tray.tray_weight > 0 else None,
        remain=-1,
        k=profile.k,
        n=profile.n,
        tray_uuid=tray.tray_uuid or None,
        cali_idx=tray.cali_idx if tray.cali_idx >= 0 else -1,
    )

    if not success:
        raise HTTPException(status_code=502, detail=f"Failed to activate: {message}")

    tray_statuses = _build_tray_statuses(request)
    spools_fresh, error = await _load_spools(request)
    linked_spools = [s for s in spools_fresh if s.filament.is_linked]
    tray = next((t for t in tray_statuses if t.tray_index == tray_index), None)
    if tray is None:
        raise HTTPException(status_code=404, detail="Tray not found")

    return templates.TemplateResponse(
        "partials/tray_card.html",
        {
            "request": request,
            "tray": tray,
            "spools": linked_spools,
            "assign_success": f"Assigned {spool.display_name} to {tray.label}",
        },
    )


@router.get("/profiles")
async def profile_picker(
    request: Request,
    filament_id: int = Query(...),
    selected_linked_filament_id: str = Query(default=""),
    search: str = Query(default=""),
) -> HTMLResponse:
    profiles = request.app.state.orcaslicer.get_profiles()
    filtered_profiles = _filter_profiles(profiles, search)
    selected_profile = _find_profile_by_linked_id(profiles, selected_linked_filament_id)
    selected_linked_filament_id_canonical = (
        selected_profile.filament_id if selected_profile else selected_linked_filament_id
    )

    return templates.TemplateResponse(
        "partials/profile_picker.html",
        {
            "request": request,
            "filament_id": filament_id,
            "profiles": filtered_profiles,
            "profile_search": search,
            "selected_linked_filament_id": selected_linked_filament_id_canonical,
        },
    )
