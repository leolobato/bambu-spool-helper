"""Jinja2 + HTMX routes for Spoolman filament linking."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import FilamentProfileResponse, MachineProfileResponse, SpoolmanFilament, SpoolmanSpool, TrayStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web", tags=["Web"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

FILAMENT_TYPE_NAME_HINTS = (
    ("PCTG", "PCTG"),
    ("PETG", "PETG"),
    ("BVOH", "BVOH"),
    ("HIPS", "HIPS"),
    ("PVA", "PVA"),
    ("PPS", "PPS"),
    ("PPA", "PPA"),
    ("PHA", "PHA"),
    ("EVA", "EVA"),
    ("ABS", "ABS"),
    ("ASA", "ASA"),
    ("TPU", "TPU"),
    ("PLA", "PLA"),
    ("PC", "PC"),
    ("PP", "PP"),
    ("PE", "PE"),
)


def _filter_profiles(profiles: list[FilamentProfileResponse], search: str) -> list[FilamentProfileResponse]:
    term = search.strip().casefold()
    if not term:
        return profiles

    return [
        profile
        for profile in profiles
        if any(
            term in candidate
            for candidate in (
                profile.name.casefold(),
                profile.filament_type.casefold(),
                profile.filament_id.casefold(),
            )
        )
    ]


def _normalize_profile_id(value: str) -> str:
    return str(value or "").strip().upper()


def _profile_ids_match(left: str, right: str) -> bool:
    left_norm = _normalize_profile_id(left)
    right_norm = _normalize_profile_id(right)
    return bool(left_norm and right_norm and left_norm == right_norm)


def _find_profile_by_linked_id(
    profiles: list[FilamentProfileResponse],
    linked_id: str,
) -> FilamentProfileResponse | None:
    linked_id = str(linked_id or "").strip()
    if not linked_id:
        return None

    return next(
        (profile for profile in profiles if _profile_ids_match(profile.filament_id, linked_id)),
        None,
    )


def _find_profile_by_setting_id(
    profiles: list[FilamentProfileResponse],
    setting_id: str,
) -> FilamentProfileResponse | None:
    normalized_setting_id = str(setting_id or "").strip()
    if not normalized_setting_id:
        return None

    return next(
        (profile for profile in profiles if profile.setting_id.strip() == normalized_setting_id),
        None,
    )


def _find_profiles_by_linked_id(
    profiles: list[FilamentProfileResponse],
    linked_id: str,
) -> list[FilamentProfileResponse]:
    return [
        profile
        for profile in profiles
        if _profile_ids_match(profile.filament_id, linked_id)
    ]


def _score_linked_profile_match(
    profile: FilamentProfileResponse,
    filament: SpoolmanFilament,
) -> tuple[int, int, int]:
    return (
        1 if _values_match(profile.filament_type, filament.material or "") else 0,
        1 if _values_match(profile.filament_type, filament.ams_filament_type or "") else 0,
        1 if profile.source == "user" else 0,
    )


def _infer_filament_type_from_name(name: str) -> str:
    normalized_name = str(name or "").upper()
    if not normalized_name:
        return ""

    for marker, filament_type in FILAMENT_TYPE_NAME_HINTS:
        if marker in normalized_name:
            return filament_type

    if re.search(r"\bPA(?:[0-9A-Z+-]*)\b", normalized_name):
        return "PA"

    return ""


def _resolve_link_filament_type(
    profile: FilamentProfileResponse,
    filament: SpoolmanFilament | None,
) -> str:
    candidates = [
        profile.filament_type,
        filament.material if filament else "",
        filament.ams_filament_type if filament else "",
        _infer_filament_type_from_name(profile.name),
    ]
    for candidate in candidates:
        normalized_candidate = str(candidate or "").strip()
        if normalized_candidate:
            return normalized_candidate
    return ""


def _values_match(left: str, right: str) -> bool:
    left_normalized = str(left or "").strip().casefold()
    right_normalized = str(right or "").strip().casefold()
    return bool(left_normalized and right_normalized and left_normalized == right_normalized)


def _float_matches(left: float | None, right: float | None, tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _score_tray_profile_match(
    tray: TrayStatus,
    profile: FilamentProfileResponse,
) -> tuple[int, int, int, int, int]:
    return (
        1 if _values_match(profile.filament_type, tray.tray_type) else 0,
        1 if tray.nozzle_temp_min == profile.nozzle_temp_min and tray.nozzle_temp_max == profile.nozzle_temp_max else 0,
        1 if tray.bed_temp and tray.bed_temp == profile.bed_temp_min else 0,
        1 if _float_matches(profile.k, tray.k) and _float_matches(profile.n, tray.n) else 0,
        1 if profile.source == "user" else 0,
    )


def _find_profile_for_tray(
    profiles: list[FilamentProfileResponse],
    tray: TrayStatus,
) -> FilamentProfileResponse | None:
    candidates = _find_profiles_by_linked_id(profiles, tray.tray_info_idx)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    ranked_candidates = sorted(
        candidates,
        key=lambda profile: (
            _score_tray_profile_match(tray, profile),
            profile.name.casefold(),
        ),
        reverse=True,
    )
    return ranked_candidates[0]


def _find_linked_profile(
    profiles: list[FilamentProfileResponse],
    filament: SpoolmanFilament,
) -> FilamentProfileResponse | None:
    candidates = _find_profiles_by_linked_id(profiles, filament.ams_filament_id or "")
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    ranked_candidates = sorted(
        candidates,
        key=lambda profile: (
            _score_linked_profile_match(profile, filament),
            profile.name.casefold(),
        ),
        reverse=True,
    )
    return ranked_candidates[0]


async def _machine_context(
    request: Request,
    requested_machine_id: str = "",
) -> tuple[list[MachineProfileResponse], str]:
    orcaslicer = request.app.state.orcaslicer
    machines = orcaslicer.get_machines()
    if not machines:
        machines = await orcaslicer.load_machines()

    selected_machine_id = str(requested_machine_id or "").strip()
    if selected_machine_id and orcaslicer.has_machine(selected_machine_id):
        return machines, selected_machine_id

    if orcaslicer.default_machine_id and orcaslicer.has_machine(orcaslicer.default_machine_id):
        return machines, orcaslicer.default_machine_id

    if machines:
        return machines, machines[0].setting_id

    return [], orcaslicer.default_machine_id


def _build_tray_profile_matches(
    trays: list[TrayStatus],
    profiles: list[FilamentProfileResponse],
) -> dict[int, FilamentProfileResponse]:
    matches: dict[int, FilamentProfileResponse] = {}
    for tray in trays:
        tray_filament_id = (tray.tray_info_idx or "").strip()
        if not tray_filament_id:
            continue
        profile = _find_profile_for_tray(profiles, tray)
        if profile:
            matches[tray.tray_index] = profile
    return matches


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


def _find_filament_by_id(filaments: list[SpoolmanFilament], filament_id: int) -> SpoolmanFilament | None:
    return next((item for item in filaments if item.id == filament_id), None)


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _decode_extra_range(extra: dict[str, str], key: str) -> tuple[int | None, int | None]:
    raw = str(extra.get(key, "") or "").strip()
    if not raw:
        return None, None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(decoded, list) or len(decoded) < 2:
        return None, None
    return _safe_int(decoded[0]), _safe_int(decoded[1])


def _suggest_profile_name(filament: SpoolmanFilament) -> str:
    base = filament.display_name.strip() or f"Filament {filament.id}"
    return f"{base} Imported"


def _recommended_base_profile(
    profiles: list[FilamentProfileResponse],
    filament_type: str,
) -> FilamentProfileResponse | None:
    typed = [
        profile for profile in profiles
        if profile.setting_id.strip() and profile.filament_type.strip().casefold() == filament_type.strip().casefold()
    ]
    if typed:
        typed.sort(key=lambda profile: ("generic" not in profile.name.casefold(), profile.name.casefold()))
        return typed[0]

    with_setting = [profile for profile in profiles if profile.setting_id.strip()]
    if not with_setting:
        return None
    with_setting.sort(key=lambda profile: profile.name.casefold())
    return with_setting[0]


def _base_profile_options(profiles: list[FilamentProfileResponse]) -> list[dict[str, str]]:
    options = [
        {
            "setting_id": profile.setting_id,
            "name": profile.name,
            "filament_type": profile.filament_type,
        }
        for profile in profiles
        if profile.setting_id.strip()
    ]
    options.sort(key=lambda option: option["name"].casefold())
    return options


def _render_create_profile_modal(
    request: Request,
    *,
    filament: SpoolmanFilament,
    machine_id: str,
    base_options: list[dict[str, str]],
    selected_base_setting_id: str,
    suggested_name: str,
    filament_type: str,
    nozzle_temp_min: int | None,
    nozzle_temp_max: int | None,
    bed_temp: int | None,
    print_speed_min: int | None,
    print_speed_max: int | None,
    error_message: str = "",
    success_message: str = "",
    import_result: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/create_profile_from_filament.html",
        {
            "request": request,
            "filament": filament,
            "machine_id": machine_id,
            "base_options": base_options,
            "selected_base_setting_id": selected_base_setting_id,
            "suggested_name": suggested_name,
            "filament_type": filament_type,
            "nozzle_temp_min": nozzle_temp_min,
            "nozzle_temp_max": nozzle_temp_max,
            "bed_temp": bed_temp,
            "print_speed_min": print_speed_min,
            "print_speed_max": print_speed_max,
            "error_message": error_message,
            "success_message": success_message,
            "import_result": import_result or {},
        },
        headers=headers,
    )

async def _render_filament_detail(
    request: Request,
    filament_id: int,
    machine_id: str,
    profile_search: str = "",
) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    filament = next((item for item in filaments if item.id == filament_id), None)
    if filament is None:
        raise HTTPException(status_code=404, detail="Filament not found")

    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    linked_profile = _find_linked_profile(profiles, filament)
    filtered_profiles = _filter_profiles(profiles, profile_search)

    return templates.TemplateResponse(
        "partials/filament_detail.html",
        {
            "request": request,
            "filament": filament,
            "machine_id": machine_id,
            "profiles": filtered_profiles,
            "profile_search": profile_search,
            "selected_setting_id": linked_profile.setting_id if linked_profile else "",
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
    machine_id: str = "",
    error_message: str = "",
    success_message: str = "",
    import_result: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/import_profile.html",
        {
            "request": request,
            "machine_id": machine_id,
            "error_message": error_message,
            "success_message": success_message,
            "import_result": import_result or {},
        },
        headers=headers,
    )


@router.get("/")
async def index(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    machine_options, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    filter_mode = "all"
    search = ""
    filtered_filaments = _filter_filaments(filaments, filter_mode, search)

    selected_filament = filtered_filaments[0] if filtered_filaments else None
    linked_profile = _find_linked_profile(profiles, selected_filament) if selected_filament else None

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "machine_options": machine_options,
            "machine_id": machine_id,
            "filaments": filtered_filaments,
            "error": error,
            "filter_mode": filter_mode,
            "search": search,
            "selected_filament_id": selected_filament.id if selected_filament else None,
            "filament": selected_filament,
            "profiles": profiles,
            "profile_search": "",
            "selected_setting_id": linked_profile.setting_id if linked_profile else "",
            "selected_linked_filament_id": (
                linked_profile.filament_id if linked_profile else (selected_filament.ams_filament_id if selected_filament else "")
            ),
            "linked_profile": linked_profile,
            "active_page": "filaments",
        },
    )


@router.get("/import-profile")
async def import_profile_modal(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    return _render_import_profile_modal(request, machine_id=machine_id)


@router.post("/import-profile")
async def import_profile_upload(
    request: Request,
    profile_file: UploadFile = File(...),
    machine: str = Form(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    filename = profile_file.filename or ""
    if not filename:
        return _render_import_profile_modal(request, machine_id=machine_id, error_message="Please choose a JSON file.")
    if not filename.lower().endswith(".json"):
        return _render_import_profile_modal(request, machine_id=machine_id, error_message="Only .json profile files are supported.")

    try:
        raw = await profile_file.read()
    finally:
        await profile_file.close()

    if not raw:
        return _render_import_profile_modal(request, machine_id=machine_id, error_message="Uploaded file is empty.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _render_import_profile_modal(request, machine_id=machine_id, error_message="Profile file must be UTF-8 encoded JSON.")
    except json.JSONDecodeError:
        return _render_import_profile_modal(request, machine_id=machine_id, error_message="Invalid JSON file.")

    if not isinstance(payload, dict):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            error_message="Profile JSON must be an object.",
        )

    try:
        result = await request.app.state.orcaslicer.import_profile(payload, machine_id)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip()
        if not error_detail:
            error_detail = str(exc)
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            error_message=f"Import request failed: {exc}",
        )

    profile_name = str(result.get("name", "")).strip()
    profile_id = str(result.get("filament_id") or "").strip()
    success_message = f"Imported profile {profile_name or profile_id or 'successfully'}."
    headers = {"HX-Trigger": json.dumps({"profiles-imported": True})}
    return _render_import_profile_modal(
        request,
        machine_id=machine_id,
        success_message=success_message,
        import_result=result,
        headers=headers,
    )


@router.get("/create-profile/{filament_id}")
async def create_profile_modal(
    request: Request,
    filament_id: int,
    machine: str = Query(default=""),
) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    if error:
        raise HTTPException(status_code=502, detail=error)
    filament = _find_filament_by_id(filaments, filament_id)
    if filament is None:
        raise HTTPException(status_code=404, detail="Filament not found")

    _, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    base_options = _base_profile_options(profiles)
    inferred_filament_type = (filament.material or filament.ams_filament_type or "").strip()
    preferred_base = _recommended_base_profile(profiles, inferred_filament_type)
    selected_base_setting_id = preferred_base.setting_id if preferred_base else ""
    if not inferred_filament_type and preferred_base:
        inferred_filament_type = preferred_base.filament_type

    extra = filament.extra or {}
    nozzle_min, nozzle_max = _decode_extra_range(extra, "nozzle_temp")
    bed_min, bed_max = _decode_extra_range(extra, "bed_temp")
    print_speed_min, print_speed_max = _decode_extra_range(extra, "printing_speed")
    bed_temp = bed_min if bed_min is not None else bed_max

    return _render_create_profile_modal(
        request,
        filament=filament,
        machine_id=machine_id,
        base_options=base_options,
        selected_base_setting_id=selected_base_setting_id,
        suggested_name=_suggest_profile_name(filament),
        filament_type=inferred_filament_type,
        nozzle_temp_min=nozzle_min,
        nozzle_temp_max=nozzle_max,
        bed_temp=bed_temp,
        print_speed_min=print_speed_min,
        print_speed_max=print_speed_max,
    )


@router.post("/create-profile/{filament_id}")
async def create_profile_submit(
    request: Request,
    filament_id: int,
    machine: str = Form(default=""),
    profile_name: str = Form(...),
    base_setting_id: str = Form(...),
    filament_type: str = Form(...),
    nozzle_temp_min: str = Form(default=""),
    nozzle_temp_max: str = Form(default=""),
    bed_temp: str = Form(default=""),
    print_speed_min: str = Form(default=""),
    print_speed_max: str = Form(default=""),
) -> HTMLResponse:
    filaments, error = await _load_filaments(request)
    if error:
        raise HTTPException(status_code=502, detail=error)
    filament = _find_filament_by_id(filaments, filament_id)
    if filament is None:
        raise HTTPException(status_code=404, detail="Filament not found")

    _, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    base_options = _base_profile_options(profiles)
    base_option_ids = {option["setting_id"] for option in base_options}

    clean_name = profile_name.strip()
    clean_base = base_setting_id.strip()
    clean_type = filament_type.strip()

    nozzle_min = _safe_int(nozzle_temp_min)
    nozzle_max = _safe_int(nozzle_temp_max)
    bed = _safe_int(bed_temp)
    speed_min = _safe_int(print_speed_min)
    speed_max = _safe_int(print_speed_max)

    error_message = ""
    if not clean_name:
        error_message = "Profile name is required."
    elif not clean_base:
        error_message = "Base filament profile is required."
    elif clean_base not in base_option_ids:
        error_message = "Selected base filament profile is invalid."
    elif not clean_type:
        error_message = "Filament type is required."
    elif (nozzle_min is None) != (nozzle_max is None):
        error_message = "Provide both nozzle min and max temperatures, or leave both empty."
    elif (speed_min is None) != (speed_max is None):
        error_message = "Provide both print speed min and max, or leave both empty."

    if error_message:
        return _render_create_profile_modal(
            request,
            filament=filament,
            machine_id=machine_id,
            base_options=base_options,
            selected_base_setting_id=clean_base,
            suggested_name=clean_name,
            filament_type=clean_type,
            nozzle_temp_min=nozzle_min,
            nozzle_temp_max=nozzle_max,
            bed_temp=bed,
            print_speed_min=speed_min,
            print_speed_max=speed_max,
            error_message=error_message,
        )

    payload: dict[str, Any] = {
        "name": clean_name,
        "inherits": clean_base,
        "filament_type": [clean_type],
    }

    if nozzle_min is not None and nozzle_max is not None:
        low, high = sorted((nozzle_min, nozzle_max))
        payload["nozzle_temperature_range_low"] = [low]
        payload["nozzle_temperature_range_high"] = [high]

    if bed is not None:
        payload["hot_plate_temp"] = [bed]

    if speed_min is not None and speed_max is not None:
        low, high = sorted((speed_min, speed_max))
        payload["slow_down_min_speed"] = [low]
        payload["filament_max_volumetric_speed"] = [high]

    try:
        result = await request.app.state.orcaslicer.import_profile(payload, machine_id)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip() or str(exc)
        return _render_create_profile_modal(
            request,
            filament=filament,
            machine_id=machine_id,
            base_options=base_options,
            selected_base_setting_id=clean_base,
            suggested_name=clean_name,
            filament_type=clean_type,
            nozzle_temp_min=nozzle_min,
            nozzle_temp_max=nozzle_max,
            bed_temp=bed,
            print_speed_min=speed_min,
            print_speed_max=speed_max,
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_create_profile_modal(
            request,
            filament=filament,
            machine_id=machine_id,
            base_options=base_options,
            selected_base_setting_id=clean_base,
            suggested_name=clean_name,
            filament_type=clean_type,
            nozzle_temp_min=nozzle_min,
            nozzle_temp_max=nozzle_max,
            bed_temp=bed,
            print_speed_min=speed_min,
            print_speed_max=speed_max,
            error_message=f"Import request failed: {exc}",
        )

    success_headers = {"HX-Trigger": json.dumps({"profiles-imported": True})}
    imported_name = str(result.get("name", "")).strip()
    imported_filament_id = str(result.get("filament_id", "")).strip()
    success_message = f"Imported profile {imported_name or imported_filament_id or 'successfully'}."

    return _render_create_profile_modal(
        request,
        filament=filament,
        machine_id=machine_id,
        base_options=base_options,
        selected_base_setting_id=clean_base,
        suggested_name=clean_name,
        filament_type=clean_type,
        nozzle_temp_min=nozzle_min,
        nozzle_temp_max=nozzle_max,
        bed_temp=bed,
        print_speed_min=speed_min,
        print_speed_max=speed_max,
        success_message=success_message,
        import_result=result,
        headers=success_headers,
    )


@router.get("/filaments")
async def filament_list(
    request: Request,
    machine: str = Query(default=""),
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
            "machine_id": (await _machine_context(request, machine))[1],
            "filaments": filtered_filaments,
            "error": error,
            "selected_filament_id": selected_id,
        },
    )


@router.get("/filament/{filament_id}")
async def filament_detail(
    request: Request,
    filament_id: int,
    machine: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    return await _render_filament_detail(request, filament_id, machine_id)


@router.post("/link/{filament_id}")
async def link_filament(
    request: Request,
    filament_id: int,
    machine: str = Form(default=""),
    selected_setting_id: str = Form(default=""),
    linked_filament_id: str = Form(...),
    profile_search: str = Form(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    profile = _find_profile_by_setting_id(profiles, selected_setting_id)
    if profile is None:
        profile = _find_profile_by_linked_id(profiles, linked_filament_id)
    if profile is None:
        raise HTTPException(status_code=400, detail="Invalid filament_id")
    if not profile.filament_id:
        raise HTTPException(status_code=400, detail="Selected profile has no filament_id")

    filament: SpoolmanFilament | None = None
    try:
        filament = await request.app.state.spoolman.get_filament(filament_id)
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch Spoolman filament %s before linking: %s", filament_id, exc)

    from app.models import VALID_TRAY_TYPES

    link_filament_type = _resolve_link_filament_type(profile, filament)
    if not link_filament_type:
        raise HTTPException(
            status_code=400,
            detail="Selected profile has no filament_type and no fallback material could be inferred",
        )
    if link_filament_type.upper() not in VALID_TRAY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Filament type '{link_filament_type}' is not a valid AMS tray type. "
            f"Valid types: {', '.join(sorted(VALID_TRAY_TYPES))}",
        )

    try:
        await request.app.state.spoolman.link_filament(
            filament_id=filament_id,
            ams_filament_id=profile.filament_id,
            ams_filament_type=link_filament_type,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to link filament: {exc}") from exc

    return await _render_filament_detail(request, filament_id, machine_id, profile_search=profile_search)


@router.post("/unlink/{filament_id}")
async def unlink_filament(
    request: Request,
    filament_id: int,
    machine: str = Form(default=""),
) -> HTMLResponse:
    try:
        await request.app.state.spoolman.unlink_filament(filament_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to unlink filament: {exc}") from exc

    _, machine_id = await _machine_context(request, machine)
    return await _render_filament_detail(request, filament_id, machine_id)


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


async def _build_trays_context(request: Request, requested_machine_id: str = "") -> dict:
    machine_options, machine_id = await _machine_context(request, requested_machine_id)
    request.app.state.mqtt.request_full_status()
    tray_statuses = _build_tray_statuses(request)
    spools, error = await _load_spools(request)
    linked_spools = [s for s in spools if s.filament.is_linked]
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    tray_profile_matches = _build_tray_profile_matches(tray_statuses, profiles)

    return {
        "request": request,
        "machine_options": machine_options,
        "machine_id": machine_id,
        "trays": tray_statuses,
        "tray_profile_matches": tray_profile_matches,
        "spools": linked_spools,
        "error": error,
        "mqtt_status": _build_mqtt_status(request),
    }


@router.get("/trays")
async def trays_page(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    context = await _build_trays_context(request, machine)
    context["active_page"] = "trays"
    return templates.TemplateResponse("trays.html", context)


@router.get("/trays/content")
async def trays_content(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    context = await _build_trays_context(request, machine)
    return templates.TemplateResponse("partials/trays_content.html", context)


@router.get("/tray/{tray_index}")
async def tray_detail(
    request: Request,
    tray_index: int,
    machine: str = Query(default=""),
    search: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
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
            "machine_id": machine_id,
            "spools": linked_spools,
            "search": search,
            "error": error,
        },
    )


@router.post("/tray/{tray_index}/assign")
async def assign_spool_to_tray(
    request: Request,
    tray_index: int,
    machine: str = Form(default=""),
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

    _, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    profile = _find_linked_profile(profiles, filament)
    if not profile:
        raise HTTPException(
            status_code=400,
            detail=f"OrcaSlicer profile not found for filament_id={linked_filament_id}",
        )
    ams_payload_filament_id = (profile.filament_id or "").strip()
    if not ams_payload_filament_id:
        raise HTTPException(status_code=400, detail="Linked profile missing filament_id")

    mqtt = request.app.state.mqtt
    activation_filament_type = _resolve_link_filament_type(profile, filament)
    if not activation_filament_type:
        raise HTTPException(status_code=400, detail="Linked profile missing filament_type")
    success, message = mqtt.activate_filament(
        tray=tray_index,
        tray_info_idx=ams_payload_filament_id,
        color_hex=filament.color_hex or "FFFFFF",
        nozzle_temp_min=profile.nozzle_temp_min,
        nozzle_temp_max=profile.nozzle_temp_max,
        filament_type=activation_filament_type,
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
            "machine_id": machine_id,
            "tray": tray,
            "matched_profile": profile,
            "spools": linked_spools,
            "assign_success": f"Assigned {spool.display_name} to {tray.label}",
        },
    )


@router.get("/profiles")
async def profile_picker(
    request: Request,
    filament_id: int = Query(...),
    machine: str = Query(default=""),
    selected_setting_id: str = Query(default=""),
    selected_linked_filament_id: str = Query(default=""),
    search: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    filtered_profiles = [
        profile for profile in _filter_profiles(profiles, search)
        if profile.filament_id.strip()
    ]
    selected_profile = _find_profile_by_setting_id(profiles, selected_setting_id)
    if selected_profile is None:
        selected_profile = _find_profile_by_linked_id(profiles, selected_linked_filament_id)
    selected_linked_filament_id_canonical = (
        selected_profile.filament_id if selected_profile else selected_linked_filament_id
    )
    selected_setting_id_canonical = selected_profile.setting_id if selected_profile else selected_setting_id

    return templates.TemplateResponse(
        "partials/profile_picker.html",
        {
            "request": request,
            "filament_id": filament_id,
            "machine_id": machine_id,
            "profiles": filtered_profiles,
            "profile_search": search,
            "selected_setting_id": selected_setting_id_canonical,
            "selected_linked_filament_id": selected_linked_filament_id_canonical,
        },
    )
