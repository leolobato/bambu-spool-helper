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

from app.models import VALID_TRAY_TYPES, FilamentProfileResponse, MachineProfileResponse, SpoolmanFilament, SpoolmanSpool, TrayStatus

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
        normalized_candidate = _normalize_valid_filament_type(candidate)
        if normalized_candidate:
            return normalized_candidate
    return ""


def _normalize_valid_filament_type(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in VALID_TRAY_TYPES:
        return normalized
    return ""


def _extract_payload_filament_type(payload: dict[str, Any]) -> str:
    raw = payload.get("filament_type")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return str(raw or "").strip()


def _set_payload_filament_type(payload: dict[str, Any], filament_type: str) -> None:
    payload["filament_type"] = [_normalize_valid_filament_type(filament_type)]


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


def _build_linked_profile_validation(
    filaments: list[SpoolmanFilament],
    profiles: list[FilamentProfileResponse],
) -> dict[str, Any]:
    linked_filaments = [filament for filament in filaments if filament.is_linked]
    unlinked_filaments = [filament for filament in filaments if not filament.is_linked]
    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for filament in linked_filaments:
        profile = _find_linked_profile(profiles, filament)
        item = {
            "filament": filament,
            "linked_filament_id": (filament.ams_filament_id or "").strip(),
            "linked_filament_type": (filament.ams_filament_type or "").strip(),
        }
        if profile is None:
            missing.append(item)
            continue
        matched.append({
            **item,
            "profile": profile,
        })

    unlinked = [{"filament": filament} for filament in unlinked_filaments]

    return {
        "linked_count": len(linked_filaments),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "unlinked_count": len(unlinked_filaments),
        "matched": matched,
        "missing": missing,
        "unlinked": unlinked,
    }


def _range_changed(
    current: tuple[int | None, int | None],
    target: tuple[int, int],
) -> bool:
    normalized_current = _normalize_optional_range(current)
    normalized_target = _normalize_required_range(target)
    return normalized_current != normalized_target


def _normalize_optional_range(
    values: tuple[int | None, int | None],
) -> tuple[int | None, int | None]:
    low, high = values
    if low is None or high is None:
        return low, high
    if low <= high:
        return low, high
    return high, low


def _normalize_required_range(values: tuple[int, int]) -> tuple[int, int]:
    low, high = values
    if low <= high:
        return low, high
    return high, low


def _format_range_label(low: int | None, high: int | None, unit: str = "") -> str:
    low, high = _normalize_optional_range((low, high))
    if low is None and high is None:
        return "-"

    values = [value for value in (low, high) if value is not None]
    if not values:
        return "-"

    suffix = f" {unit}" if unit else ""
    if len(values) == 1 or values[0] == values[-1]:
        return f"{values[0]}{suffix}"
    return f"{values[0]}-{values[-1]}{suffix}"


def _build_profile_field_sync(
    filament: SpoolmanFilament,
    profile: FilamentProfileResponse | None,
) -> dict[str, Any] | None:
    if profile is None:
        return None

    current_nozzle = _decode_extra_range(filament.extra or {}, "nozzle_temp")
    current_bed = _decode_extra_range(filament.extra or {}, "bed_temp")

    current_nozzle = _normalize_optional_range(current_nozzle)
    current_bed = _normalize_optional_range(current_bed)

    target_nozzle = _normalize_required_range((profile.nozzle_temp_min, profile.nozzle_temp_max))
    target_bed = _normalize_required_range((profile.bed_temp_min, profile.bed_temp_max))
    target_extruder_temp = profile.extruder_temp
    target_extruder_temp_initial_layer = profile.extruder_temp_initial_layer
    current_extruder_temp = filament.extruder_temp
    current_basic_bed_temp = filament.bed_temp
    target_basic_bed_temp = profile.bed_temp_min

    custom_fields = [
        {
            "label": "Nozzle Temp",
            "key": "nozzle_temp",
            "current": current_nozzle,
            "target": target_nozzle,
            "current_label": _format_range_label(*current_nozzle, unit="°C"),
            "target_label": _format_range_label(*target_nozzle, unit="°C"),
            "changed": _range_changed(current_nozzle, target_nozzle),
            "source_fields": ["nozzle_temperature_range_low", "nozzle_temperature_range_high"],
            "source_label": "nozzle_temperature_range_low + nozzle_temperature_range_high",
        },
        {
            "label": "Bed Temp",
            "key": "bed_temp",
            "current": current_bed,
            "target": target_bed,
            "current_label": _format_range_label(*current_bed, unit="°C"),
            "target_label": _format_range_label(*target_bed, unit="°C"),
            "changed": _range_changed(current_bed, target_bed),
            "source_fields": ["hot_plate_temp"],
            "source_label": "hot_plate_temp",
        },
    ]
    basic_fields = []
    if target_extruder_temp is not None:
        target_extruder_label = f"{target_extruder_temp} °C"
        note = ""
        if (
            target_extruder_temp_initial_layer is not None
            and target_extruder_temp_initial_layer != target_extruder_temp
        ):
            note = f" (initial layer {target_extruder_temp_initial_layer} °C)"
            target_extruder_label += note
        basic_fields.append(
            {
                "label": "Settings Extruder Temp",
                "key": "extruder_temp",
                "current": current_extruder_temp,
                "current_label": "-" if current_extruder_temp is None else f"{current_extruder_temp} °C",
                "target": target_extruder_temp,
                "target_label": target_extruder_label,
                "changed": current_extruder_temp != target_extruder_temp,
                "note": note,
                "source_fields": ["nozzle_temperature", "nozzle_temperature_initial_layer"],
                "source_label": "nozzle_temperature"
                if not note
                else "nozzle_temperature + nozzle_temperature_initial_layer",
            }
        )
    basic_fields.append(
        {
            "label": "Settings Bed Temp",
            "key": "bed_temp_basic",
            "current": current_basic_bed_temp,
            "current_label": "-" if current_basic_bed_temp is None else f"{current_basic_bed_temp} °C",
            "target": target_basic_bed_temp,
            "target_label": f"{target_basic_bed_temp} °C",
            "changed": current_basic_bed_temp != target_basic_bed_temp,
            "source_fields": ["hot_plate_temp"],
            "source_label": "hot_plate_temp",
        }
    )

    has_changes = any(field["changed"] for field in custom_fields + basic_fields)

    return {
        "custom_fields": custom_fields,
        "basic_fields": basic_fields,
        "has_changes": has_changes,
        "is_fully_synced": not has_changes,
        "target_nozzle": target_nozzle,
        "target_bed": target_bed,
        "target_basic_bed_temp": target_basic_bed_temp,
    }


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
        if (
            term in filament.display_name.lower()
            or term in (filament.material or "").lower()
            or term in (filament.ams_filament_id or "").lower()
        )
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


def _midpoint_or_single(low: int | None, high: int | None) -> int | None:
    if low is not None and high is not None:
        return (low + high) // 2
    return low if low is not None else high


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


def _profile_base_values(profile: FilamentProfileResponse | None) -> dict[str, int | None]:
    if profile is None:
        return {
            "nozzle_temp_min": None,
            "nozzle_temp_max": None,
            "nozzle_temperature": None,
            "nozzle_temperature_initial_layer": None,
            "hot_plate_temp": None,
            "hot_plate_temp_initial_layer": None,
        }
    return {
        "nozzle_temp_min": profile.nozzle_temp_min or None,
        "nozzle_temp_max": profile.nozzle_temp_max or None,
        "nozzle_temperature": profile.extruder_temp,
        "nozzle_temperature_initial_layer": profile.extruder_temp_initial_layer,
        "hot_plate_temp": profile.bed_temp_min or None,
        "hot_plate_temp_initial_layer": profile.bed_temp_initial_layer,
    }


def _all_base_values_map(profiles: list[FilamentProfileResponse]) -> dict[str, dict[str, int | None]]:
    return {
        profile.setting_id: _profile_base_values(profile)
        for profile in profiles
        if profile.setting_id.strip()
    }


def _render_create_profile_modal(
    request: Request,
    *,
    filament: SpoolmanFilament,
    machine_id: str,
    base_options: list[dict[str, str]],
    profiles: list[FilamentProfileResponse],
    selected_base_setting_id: str,
    suggested_name: str,
    filament_type: str,
    nozzle_temp_min: int | None,
    nozzle_temp_max: int | None,
    nozzle_temperature: int | None,
    nozzle_temperature_initial_layer: int | None,
    hot_plate_temp: int | None,
    hot_plate_temp_initial_layer: int | None,
    error_message: str = "",
    success_message: str = "",
    import_result: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    selected_profile = _find_profile_by_setting_id(profiles, selected_base_setting_id)
    base_values = _profile_base_values(selected_profile)
    base_values_map = _all_base_values_map(profiles)
    return templates.TemplateResponse(
        request,
        "partials/create_profile_from_filament.html",
        {
            "request": request,
            "filament": filament,
            "machine_id": machine_id,
            "base_options": base_options,
            "selected_base_setting_id": selected_base_setting_id,
            "suggested_name": suggested_name,
            "filament_type": filament_type,
            "valid_filament_types": sorted(VALID_TRAY_TYPES),
            "nozzle_temp_min": nozzle_temp_min,
            "nozzle_temp_max": nozzle_temp_max,
            "nozzle_temperature": nozzle_temperature,
            "nozzle_temperature_initial_layer": nozzle_temperature_initial_layer,
            "hot_plate_temp": hot_plate_temp,
            "hot_plate_temp_initial_layer": hot_plate_temp_initial_layer,
            "base_values": base_values,
            "base_values_map_json": json.dumps(base_values_map),
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
    success_message: str = "",
    action_error: str = "",
) -> HTMLResponse:
    error = ""
    try:
        filament = await request.app.state.spoolman.get_filament(filament_id)
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch filament %s from Spoolman detail endpoint: %s", filament_id, exc)
        filaments, list_error = await _load_filaments(request)
        filament = next((item for item in filaments if item.id == filament_id), None)
        error = list_error or f"Spoolman request failed: {exc}"
        if filament is None:
            raise HTTPException(status_code=404, detail="Filament not found")

    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    linked_profile = _find_linked_profile(profiles, filament)
    filtered_profiles = _filter_profiles(profiles, profile_search)
    selected_filament_type = _normalize_valid_filament_type(filament.ams_filament_type or "")
    link_filament_type_by_setting_id = {
        profile.setting_id: _resolve_link_filament_type(profile, filament)
        for profile in filtered_profiles
    }
    profile_field_sync = _build_profile_field_sync(filament, linked_profile)
    headers = {"HX-Trigger": json.dumps({"filament-selected": {"filamentId": filament.id}})}

    return templates.TemplateResponse(
        request,
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
            "selected_filament_type": selected_filament_type,
            "valid_filament_types": sorted(VALID_TRAY_TYPES),
            "link_filament_type_by_setting_id": link_filament_type_by_setting_id,
            "linked_profile": linked_profile,
            "error": error,
            "success_message": success_message,
            "action_error": action_error,
            "profile_field_sync": profile_field_sync,
        },
        headers=headers,
    )


def _render_import_profile_modal(
    request: Request,
    *,
    machine_id: str = "",
    kind: str = "filament",
    error_message: str = "",
    success_message: str = "",
    import_result: dict[str, Any] | None = None,
    pending_import_payload: str = "",
    pending_profile_name: str = "",
    pending_filament_id: str = "",
    pending_filament_type: str = "",
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/import_profile.html",
        {
            "request": request,
            "machine_id": machine_id,
            "kind": kind if kind in {"filament", "process"} else "filament",
            "error_message": error_message,
            "success_message": success_message,
            "import_result": import_result or {},
            "pending_import_payload": pending_import_payload,
            "pending_profile_name": pending_profile_name,
            "pending_filament_id": pending_filament_id,
            "pending_filament_type": pending_filament_type,
            "valid_filament_types": sorted(VALID_TRAY_TYPES),
        },
        headers=headers,
    )


def _render_settings_action_result(
    request: Request,
    *,
    success_message: str = "",
    error_message: str = "",
    reload_summary: dict[str, Any] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/settings_action_result.html",
        {
            "request": request,
            "success_message": success_message,
            "error_message": error_message,
            "reload_summary": reload_summary or {},
        },
    )


def _render_settings_validation_result(
    request: Request,
    *,
    validation: dict[str, Any] | None = None,
    error_message: str = "",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/settings_validation_result.html",
        {
            "request": request,
            "validation": validation,
            "error_message": error_message,
        },
    )


def _render_settings_spoolman_result(
    request: Request,
    *,
    validation: dict[str, Any] | None = None,
    created_keys: list[str] | None = None,
    errors: list[str] | None = None,
    error_message: str = "",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/settings_spoolman_result.html",
        {
            "request": request,
            "expected_fields": request.app.state.spoolman.REQUIRED_SETTINGS_FILAMENT_FIELDS,
            "validation": validation,
            "created_keys": created_keys or [],
            "errors": errors or [],
            "error_message": error_message,
        },
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
    detail_error = ""
    if selected_filament is not None:
        try:
            selected_filament = await request.app.state.spoolman.get_filament(selected_filament.id)
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch initial filament %s from Spoolman detail endpoint: %s", selected_filament.id, exc)
            selected_filament = None
            detail_error = f"Spoolman request failed: {exc}"

    linked_profile = _find_linked_profile(profiles, selected_filament) if selected_filament else None
    selected_filament_type = _normalize_valid_filament_type(selected_filament.ams_filament_type or "") if selected_filament else ""
    link_filament_type_by_setting_id = {
        profile.setting_id: _resolve_link_filament_type(profile, selected_filament)
        for profile in profiles
    } if selected_filament else {}
    profile_field_sync = _build_profile_field_sync(selected_filament, linked_profile) if selected_filament else None

    return templates.TemplateResponse(
        request,
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
            "selected_filament_type": selected_filament_type,
            "valid_filament_types": sorted(VALID_TRAY_TYPES),
            "link_filament_type_by_setting_id": link_filament_type_by_setting_id,
            "linked_profile": linked_profile,
            "profile_field_sync": profile_field_sync,
            "success_message": "",
            "action_error": "",
            "detail_error": detail_error,
            "active_page": "filaments",
        },
    )


@router.get("/import-profile")
async def import_profile_modal(
    request: Request,
    machine: str = Query(default=""),
    kind: str = Query(default="filament"),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    return _render_import_profile_modal(request, machine_id=machine_id, kind=kind)


@router.get("/settings")
async def settings_page(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    machine_options, machine_id = await _machine_context(request, machine)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "machine_options": machine_options,
            "machine_id": machine_id,
            "active_page": "settings",
            "expected_fields": request.app.state.spoolman.REQUIRED_SETTINGS_FILAMENT_FIELDS,
        },
    )


@router.post("/settings/reload-profiles")
async def settings_reload_profiles(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    try:
        reload_summary, profiles = await request.app.state.orcaslicer.reload_profiles(machine_id)
    except httpx.HTTPError as exc:
        return _render_settings_action_result(
            request,
            error_message=f"Reload failed: {exc}",
        )

    success_message = f"Reloaded {len(profiles)} profiles for {machine_id}."
    return _render_settings_action_result(
        request,
        success_message=success_message,
        reload_summary=reload_summary,
    )


@router.post("/settings/validate-profiles")
async def settings_validate_profiles(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    filaments, error = await _load_filaments(request)
    if error:
        return _render_settings_validation_result(request, error_message=error)

    try:
        profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    except httpx.HTTPError as exc:
        return _render_settings_validation_result(
            request,
            error_message=f"Failed to load OrcaSlicer profiles: {exc}",
        )

    validation = _build_linked_profile_validation(filaments, profiles)
    validation["machine_id"] = machine_id
    return _render_settings_validation_result(request, validation=validation)


@router.post("/settings/spoolman/validate")
async def settings_validate_spoolman_fields(
    request: Request,
) -> HTMLResponse:
    try:
        validation = await request.app.state.spoolman.validate_required_filament_fields()
    except httpx.HTTPError as exc:
        return _render_settings_spoolman_result(
            request,
            error_message=f"Failed to validate Spoolman fields: {exc}",
        )
    return _render_settings_spoolman_result(request, validation=validation)


@router.post("/settings/spoolman/ensure")
async def settings_ensure_spoolman_fields(
    request: Request,
) -> HTMLResponse:
    try:
        result = await request.app.state.spoolman.ensure_required_filament_fields()
    except httpx.HTTPError as exc:
        return _render_settings_spoolman_result(
            request,
            error_message=f"Failed to create missing Spoolman fields: {exc}",
        )
    return _render_settings_spoolman_result(
        request,
        validation=result["validation"],
        created_keys=result["created_keys"],
        errors=result["errors"],
    )


async def _read_uploaded_profile_json(
    request: Request,
    profile_file: UploadFile | None,
    *,
    machine_id: str,
    kind: str,
) -> dict[str, Any] | HTMLResponse:
    """Parse an uploaded `.json` profile into a dict.

    On any validation error, renders the import modal with the appropriate
    error message and returns the HTMLResponse directly. Callers must check
    the return type with `isinstance(..., HTMLResponse)`.
    """
    filename = profile_file.filename if profile_file else ""
    if not filename:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind=kind,
            error_message="Please choose a JSON file.",
        )
    if not filename.lower().endswith(".json"):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind=kind,
            error_message="Only .json profile files are supported.",
        )

    try:
        raw = await profile_file.read()
    finally:
        await profile_file.close()

    if not raw:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind=kind,
            error_message="Uploaded file is empty.",
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind=kind,
            error_message="Profile file must be UTF-8 encoded JSON.",
        )
    except json.JSONDecodeError:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind=kind,
            error_message="Invalid JSON file.",
        )

    if not isinstance(payload, dict):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind=kind,
            error_message="Profile JSON must be an object.",
        )

    return payload


async def _import_process_profile_flow(
    request: Request,
    *,
    profile_file: UploadFile | None,
    machine_id: str,
) -> HTMLResponse:
    result = await _read_uploaded_profile_json(
        request, profile_file, machine_id=machine_id, kind="process",
    )
    if isinstance(result, HTMLResponse):
        return result
    payload = result

    try:
        resolved_preview = await request.app.state.orcaslicer.resolve_import_process_profile(payload)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip() or str(exc)
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Profile resolution failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Profile resolution request failed: {exc}",
        )

    resolved_payload = resolved_preview.get("resolved_payload")
    if not isinstance(resolved_payload, dict):
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message="Resolved process payload is invalid.",
        )

    try:
        result = await request.app.state.orcaslicer.import_process_profile(dict(resolved_payload))
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip() or str(exc)
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="process",
            error_message=f"Import request failed: {exc}",
        )

    profile_name = str(result.get("name", "")).strip()
    setting_id = str(result.get("setting_id") or "").strip()
    success_message = f"Imported process profile {profile_name or setting_id or 'successfully'}."
    return _render_import_profile_modal(
        request,
        machine_id=machine_id,
        kind="process",
        success_message=success_message,
        import_result=result,
    )


@router.post("/import-profile")
async def import_profile_upload(
    request: Request,
    profile_file: UploadFile | None = File(default=None),
    machine: str = Form(default=""),
    payload_json: str = Form(default=""),
    filament_type: str = Form(default=""),
    kind: str = Form(default="filament"),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    normalized_kind = kind if kind in {"filament", "process"} else "filament"

    if normalized_kind == "process":
        return await _import_process_profile_flow(
            request,
            profile_file=profile_file,
            machine_id=machine_id,
        )

    # kind == "filament" — existing flow, unchanged
    if payload_json.strip():
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Pending import payload is invalid. Upload the JSON file again.",
            )

        if not isinstance(payload, dict):
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Pending import payload is invalid. Upload the JSON file again.",
            )

        normalized_filament_type = _normalize_valid_filament_type(filament_type)
        if not normalized_filament_type:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Choose a valid filament type before importing.",
                pending_import_payload=payload_json,
                pending_profile_name=str(payload.get("name", "")).strip(),
                pending_filament_id=str(payload.get("filament_id", "")).strip(),
                pending_filament_type=str(filament_type or "").strip(),
            )
        _set_payload_filament_type(payload, normalized_filament_type)
    else:
        result = await _read_uploaded_profile_json(
            request, profile_file, machine_id=machine_id, kind="filament",
        )
        if isinstance(result, HTMLResponse):
            return result
        payload = result

        try:
            resolved_preview = await request.app.state.orcaslicer.resolve_import_profile(payload)
        except httpx.HTTPStatusError as exc:
            error_detail = exc.response.text.strip() or str(exc)
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message=f"Profile resolution failed ({exc.response.status_code}): {error_detail}",
            )
        except httpx.HTTPError as exc:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message=f"Profile resolution request failed: {exc}",
            )

        resolved_payload = resolved_preview.get("resolved_payload")
        if not isinstance(resolved_payload, dict):
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Resolved profile payload is invalid.",
            )

        payload = dict(resolved_payload)
        normalized_filament_type = _normalize_valid_filament_type(_extract_payload_filament_type(payload))
        if not normalized_filament_type:
            return _render_import_profile_modal(
                request,
                machine_id=machine_id,
                kind="filament",
                error_message="Resolved profile is missing a valid filament type. Choose one before importing.",
                pending_import_payload=json.dumps(payload),
                pending_profile_name=str(resolved_preview.get("name", "")).strip(),
                pending_filament_id=str(resolved_preview.get("filament_id", "")).strip(),
                pending_filament_type=_extract_payload_filament_type(payload),
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
            kind="filament",
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_import_profile_modal(
            request,
            machine_id=machine_id,
            kind="filament",
            error_message=f"Import request failed: {exc}",
        )

    profile_name = str(result.get("name", "")).strip()
    profile_id = str(result.get("filament_id") or "").strip()
    success_message = f"Imported profile {profile_name or profile_id or 'successfully'}."
    headers = {"HX-Trigger": json.dumps({"profiles-imported": True})}
    return _render_import_profile_modal(
        request,
        machine_id=machine_id,
        kind="filament",
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
    inferred_filament_type = _normalize_valid_filament_type(filament.material or filament.ams_filament_type or "")
    preferred_base = _recommended_base_profile(profiles, inferred_filament_type)
    selected_base_setting_id = preferred_base.setting_id if preferred_base else ""
    if not inferred_filament_type and preferred_base:
        inferred_filament_type = _normalize_valid_filament_type(preferred_base.filament_type)

    extra = filament.extra or {}
    nozzle_min, nozzle_max = _normalize_optional_range(_decode_extra_range(extra, "nozzle_temp"))
    bed_min, bed_max = _decode_extra_range(extra, "bed_temp")
    nozzle_fallback = _midpoint_or_single(nozzle_min, nozzle_max)
    bed_fallback = _midpoint_or_single(bed_min, bed_max)
    nozzle_temperature = filament.extruder_temp or nozzle_fallback
    nozzle_temperature_initial_layer = nozzle_temperature
    hot_plate_temp = filament.bed_temp or bed_fallback
    hot_plate_temp_initial_layer = hot_plate_temp

    return _render_create_profile_modal(
        request,
        filament=filament,
        machine_id=machine_id,
        base_options=base_options,
        profiles=profiles,
        selected_base_setting_id=selected_base_setting_id,
        suggested_name=_suggest_profile_name(filament),
        filament_type=inferred_filament_type,
        nozzle_temp_min=nozzle_min,
        nozzle_temp_max=nozzle_max,
        nozzle_temperature=nozzle_temperature,
        nozzle_temperature_initial_layer=nozzle_temperature_initial_layer,
        hot_plate_temp=hot_plate_temp,
        hot_plate_temp_initial_layer=hot_plate_temp_initial_layer,
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
    nozzle_temperature: str = Form(default=""),
    nozzle_temperature_initial_layer: str = Form(default=""),
    hot_plate_temp: str = Form(default=""),
    hot_plate_temp_initial_layer: str = Form(default=""),
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
    clean_type = _normalize_valid_filament_type(filament_type)

    nozzle_min = _safe_int(nozzle_temp_min)
    nozzle_max = _safe_int(nozzle_temp_max)
    nozzle_temp = _safe_int(nozzle_temperature)
    nozzle_temp_initial = _safe_int(nozzle_temperature_initial_layer)
    hot_plate = _safe_int(hot_plate_temp)
    hot_plate_initial = _safe_int(hot_plate_temp_initial_layer)

    error_message = ""
    if not clean_name:
        error_message = "Profile name is required."
    elif not clean_base:
        error_message = "Base filament profile is required."
    elif clean_base not in base_option_ids:
        error_message = "Selected base filament profile is invalid."
    elif not clean_type:
        error_message = "Choose a valid filament type."
    elif (nozzle_min is None) != (nozzle_max is None):
        error_message = "Provide both nozzle min and max temperatures, or leave both empty."

    if error_message:
        return _render_create_profile_modal(
            request,
            filament=filament,
            machine_id=machine_id,
            base_options=base_options,
            profiles=profiles,
            selected_base_setting_id=clean_base,
            suggested_name=clean_name,
            filament_type=clean_type,
            nozzle_temp_min=nozzle_min,
            nozzle_temp_max=nozzle_max,
            nozzle_temperature=nozzle_temp,
            nozzle_temperature_initial_layer=nozzle_temp_initial,
            hot_plate_temp=hot_plate,
            hot_plate_temp_initial_layer=hot_plate_initial,
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

    if nozzle_temp is not None:
        payload["nozzle_temperature"] = [nozzle_temp]

    if nozzle_temp_initial is not None:
        payload["nozzle_temperature_initial_layer"] = [nozzle_temp_initial]

    if hot_plate is not None:
        payload["hot_plate_temp"] = [hot_plate]

    if hot_plate_initial is not None:
        payload["hot_plate_temp_initial_layer"] = [hot_plate_initial]

    try:
        result = await request.app.state.orcaslicer.import_profile(payload, machine_id)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip() or str(exc)
        return _render_create_profile_modal(
            request,
            filament=filament,
            machine_id=machine_id,
            base_options=base_options,
            profiles=profiles,
            selected_base_setting_id=clean_base,
            suggested_name=clean_name,
            filament_type=clean_type,
            nozzle_temp_min=nozzle_min,
            nozzle_temp_max=nozzle_max,
            nozzle_temperature=nozzle_temp,
            nozzle_temperature_initial_layer=nozzle_temp_initial,
            hot_plate_temp=hot_plate,
            hot_plate_temp_initial_layer=hot_plate_initial,
            error_message=f"Import failed ({exc.response.status_code}): {error_detail}",
        )
    except httpx.HTTPError as exc:
        return _render_create_profile_modal(
            request,
            filament=filament,
            machine_id=machine_id,
            base_options=base_options,
            profiles=profiles,
            selected_base_setting_id=clean_base,
            suggested_name=clean_name,
            filament_type=clean_type,
            nozzle_temp_min=nozzle_min,
            nozzle_temp_max=nozzle_max,
            nozzle_temperature=nozzle_temp,
            nozzle_temperature_initial_layer=nozzle_temp_initial,
            hot_plate_temp=hot_plate,
            hot_plate_temp_initial_layer=hot_plate_initial,
            error_message=f"Import request failed: {exc}",
        )

    imported_name = str(result.get("name", "")).strip()
    imported_filament_id = str(result.get("filament_id", "")).strip()
    success_message = f"Imported profile {imported_name or imported_filament_id or 'successfully'}."

    link_trigger_events: dict[str, Any] = {"profiles-imported": True}
    if filament.is_linked:
        success_message += f" Filament already linked to {filament.ams_filament_id}; kept existing link."
    elif imported_filament_id and clean_type:
        try:
            await request.app.state.spoolman.link_filament(
                filament_id=filament_id,
                ams_filament_id=imported_filament_id,
                ams_filament_type=clean_type,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to auto-link filament %s to new profile: %s", filament_id, exc)
            success_message += f" (Auto-link failed: {exc})"
        else:
            success_message += f" Linked filament to {imported_filament_id}."
            link_trigger_events["filament-selected"] = {"filamentId": filament.id}

    success_headers = {"HX-Trigger": json.dumps(link_trigger_events)}

    return _render_create_profile_modal(
        request,
        filament=filament,
        machine_id=machine_id,
        base_options=base_options,
        profiles=profiles,
        selected_base_setting_id=clean_base,
        suggested_name=clean_name,
        filament_type=clean_type,
        nozzle_temp_min=nozzle_min,
        nozzle_temp_max=nozzle_max,
        nozzle_temperature=nozzle_temp,
        nozzle_temperature_initial_layer=nozzle_temp_initial,
        hot_plate_temp=hot_plate,
        hot_plate_temp_initial_layer=hot_plate_initial,
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
        request,
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
    override_filament_type: str = Form(default=""),
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

    link_filament_type = _normalize_valid_filament_type(override_filament_type) or _resolve_link_filament_type(profile, filament)
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


@router.post("/sync-profile-fields/{filament_id}")
async def sync_profile_fields(
    request: Request,
    filament_id: int,
    machine: str = Form(default=""),
    profile_search: str = Form(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    try:
        filament = await request.app.state.spoolman.get_filament(filament_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to load filament: {exc}") from exc

    if not filament.is_linked:
        return await _render_filament_detail(
            request,
            filament_id,
            machine_id,
            profile_search=profile_search,
            action_error="Filament is not linked to an Orca profile.",
        )

    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    linked_profile = _find_linked_profile(profiles, filament)
    if linked_profile is None:
        return await _render_filament_detail(
            request,
            filament_id,
            machine_id,
            profile_search=profile_search,
            action_error="No Orca profile currently matches this linked filament.",
        )

    try:
        result = await request.app.state.spoolman.update_filament_profile_fields(
            filament_id,
            extruder_temp=linked_profile.extruder_temp,
            nozzle_temp=(linked_profile.nozzle_temp_min, linked_profile.nozzle_temp_max),
            bed_temp=(linked_profile.bed_temp_min, linked_profile.bed_temp_max),
            basic_bed_temp=linked_profile.bed_temp_min,
        )
    except ValueError as exc:
        return await _render_filament_detail(
            request,
            filament_id,
            machine_id,
            profile_search=profile_search,
            action_error=str(exc),
        )
    except httpx.HTTPError as exc:
        return await _render_filament_detail(
            request,
            filament_id,
            machine_id,
            profile_search=profile_search,
            action_error=f"Failed to update Spoolman profile fields: {exc}",
        )

    created_keys = result.get("created_keys", [])
    created_suffix = f" Created missing fields: {', '.join(created_keys)}." if created_keys else ""
    updated_basic_fields = ["settings_bed_temp"]
    if linked_profile.extruder_temp is not None:
        updated_basic_fields.insert(0, "settings_extruder_temp")
    updated_basic_fields_label = ", ".join(updated_basic_fields)
    return await _render_filament_detail(
        request,
        filament_id,
        machine_id,
        profile_search=profile_search,
        success_message=(
            f"Updated Spoolman {updated_basic_fields_label}, bed_temp, and nozzle_temp "
            f"from the linked Orca profile.{created_suffix}"
        ),
    )


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


def _apply_assignment_to_tray_view(
    tray: TrayStatus,
    spool: SpoolmanSpool,
    profile: FilamentProfileResponse,
    filament_type: str,
) -> TrayStatus:
    color_hex = (spool.filament.color_hex or "").lstrip("#").upper()
    return tray.model_copy(
        update={
            "tray_info_idx": (profile.filament_id or "").strip(),
            "tray_type": filament_type,
            "tray_color": color_hex[:6] if len(color_hex) >= 6 else tray.tray_color,
            "nozzle_temp_min": profile.nozzle_temp_min,
            "nozzle_temp_max": profile.nozzle_temp_max,
            "bed_temp": profile.bed_temp_min,
        }
    )


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
    return templates.TemplateResponse(request, "trays.html", context)


@router.get("/trays/content")
async def trays_content(
    request: Request,
    machine: str = Query(default=""),
) -> HTMLResponse:
    context = await _build_trays_context(request, machine)
    return templates.TemplateResponse(request, "partials/trays_content.html", context)


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
        request,
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
    tray = _apply_assignment_to_tray_view(tray, spool, profile, activation_filament_type)

    return templates.TemplateResponse(
        request,
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
    selected_filament_type: str = Query(default=""),
    search: str = Query(default=""),
) -> HTMLResponse:
    _, machine_id = await _machine_context(request, machine)
    profiles = await request.app.state.orcaslicer.get_profiles(machine_id)
    filament: SpoolmanFilament | None = None
    try:
        filament = await request.app.state.spoolman.get_filament(filament_id)
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch Spoolman filament %s for profile picker: %s", filament_id, exc)
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
    selected_filament_type_canonical = _normalize_valid_filament_type(selected_filament_type)
    if not selected_filament_type_canonical and filament is not None:
        selected_filament_type_canonical = _normalize_valid_filament_type(filament.ams_filament_type or "")
    link_filament_type_by_setting_id = {
        profile.setting_id: _resolve_link_filament_type(profile, filament)
        for profile in filtered_profiles
    }

    return templates.TemplateResponse(
        request,
        "partials/profile_picker.html",
        {
            "request": request,
            "filament_id": filament_id,
            "machine_id": machine_id,
            "profiles": filtered_profiles,
            "profile_search": search,
            "selected_setting_id": selected_setting_id_canonical,
            "selected_linked_filament_id": selected_linked_filament_id_canonical,
            "selected_filament_type": selected_filament_type_canonical,
            "link_filament_type_by_setting_id": link_filament_type_by_setting_id,
        },
    )
