"""REST API routes for status, profiles, and activation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.services.mqtt_printer import _synthetic_slot_uuid
from app.models import (
    VALID_TRAY_TYPES,
    ActivationRecord,
    ActivateRequest,
    ActivateResponse,
    AmsStateResponse,
    AmsTrayResponse,
    FilamentProfileResponse,
    PrintTrackingSettingsRequest,
    StatusResponse,
)
from app.tray_labels import EXTERNAL_HELPER_TRAY, tray_label

logger = logging.getLogger(__name__)

router = APIRouter(tags=["API"])


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse:
    settings = request.app.state.settings
    profile_count = len(
        await request.app.state.orcaslicer.get_profiles(settings.default_machine_profile_id)
    )

    return StatusResponse(
        port=settings.port,
        profiles_loaded=profile_count,
        print_tracking=(
            request.app.state.print_tracking.status
            if hasattr(request.app.state, "print_tracking")
            else {
                "available": False,
                "enabled": False,
                "connected": False,
                "active": False,
            }
        ),
    )


@router.put("/api/print-tracking/settings")
async def update_print_tracking_settings(
    request: Request, payload: PrintTrackingSettingsRequest,
) -> dict[str, Any]:
    service = request.app.state.print_tracking
    await service.set_enabled(payload.enabled)
    return service.status


@router.get("/valid-tray-types")
async def get_valid_tray_types() -> list[str]:
    return sorted(VALID_TRAY_TYPES)


def _tray_color_css(raw: str) -> str:
    cleaned = (raw or "").strip().lstrip("#").upper()
    return f"#{cleaned[:6]}" if len(cleaned) >= 6 else ""


@router.get("/ams", response_model=AmsStateResponse)
async def get_ams_state(request: Request) -> AmsStateResponse:
    """Current AMS + external spool contents, with Spoolman spool bindings.

    In MQTT mode the refresh request is asynchronous, so a cold cache may
    need a second call before trays are populated.
    """
    mqtt = request.app.state.mqtt
    await asyncio.to_thread(mqtt.request_full_status)
    tray_data = mqtt.get_tray_data()
    connection = mqtt.get_connection_status()

    try:
        uuids = await mqtt.get_tray_uuids()
    except Exception as exc:
        logger.warning("Tray uuid snapshot failed for /ams: %s", exc)
        uuids = {}

    bindings: dict[int, int] = {}
    if uuids:
        try:
            spools = await request.app.state.spoolman.get_spools()
        except Exception as exc:
            logger.warning("Spoolman unavailable for /ams bindings: %s", exc)
            spools = []
        by_uuid: dict[str, list[int]] = {}
        for spool in spools:
            spool_uuid = spool.bambu_tray_uuid
            if spool_uuid:
                by_uuid.setdefault(spool_uuid, []).append(spool.id)
        for tray_index, tray_uuid in uuids.items():
            matches = by_uuid.get(tray_uuid, [])
            if len(matches) == 1:
                bindings[tray_index] = matches[0]

    trays: list[AmsTrayResponse] = []
    for index in range(EXTERNAL_HELPER_TRAY + 1):
        td = tray_data.get(index)
        if td is None:
            trays.append(AmsTrayResponse(tray=index, label=tray_label(index), empty=True))
            continue
        trays.append(AmsTrayResponse(
            tray=index,
            label=tray_label(index),
            empty=not td.tray_type and not td.tray_info_idx,
            tray_type=td.tray_type,
            tray_color=_tray_color_css(td.tray_color),
            filament_id=td.tray_info_idx,
            tray_sub_brands=td.tray_sub_brands,
            remain=td.remain,
            nozzle_temp_min=td.nozzle_temp_min,
            nozzle_temp_max=td.nozzle_temp_max,
            bed_temp=td.bed_temp,
            spool_id=bindings.get(index),
        ))

    return AmsStateResponse(
        connected=bool(connection.get("connected")),
        printer_state=connection.get("printer_state"),
        active_tray=mqtt.get_active_tray(),
        trays=trays,
    )


@router.get("/profiles", response_model=list[FilamentProfileResponse])
async def get_profiles(
    request: Request,
    machine: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> list[FilamentProfileResponse]:
    profiles = await request.app.state.orcaslicer.get_profiles(machine)
    term = (search or "").strip().lower()
    if not term:
        return profiles

    return [
        profile
        for profile in profiles
        if term in profile.name.lower() or term in profile.filament_type.lower()
    ]


@router.post("/activate", response_model=ActivateResponse)
async def activate_profile(request: Request, payload: ActivateRequest) -> ActivateResponse:
    mqtt = request.app.state.mqtt

    ams_filament_id = payload.filament_id.strip()
    if not ams_filament_id:
        return ActivateResponse(success=False, profile_name="", message="filament_id is required")

    filament_type = payload.filament_type.strip()
    if not filament_type:
        return ActivateResponse(success=False, profile_name="", message="filament_type is required")

    profile_name = f"{filament_type} ({ams_filament_id})"

    setting_id = (payload.setting_id or "").strip()
    if not setting_id:
        # Required by gateway routing; resolved here so legacy /activate callers
        # that only send filament_id keep working when a gateway is wired up.
        resolved = await request.app.state.orcaslicer.find_profile(filament_id=ams_filament_id)
        if resolved is not None:
            setting_id = (resolved.setting_id or "").strip()

    if payload.spool_id is not None:
        mqtt.request_full_status()
        tray_uuid = mqtt.get_tray_uuid(payload.tray)
        if tray_uuid is None:
            # AMS hasn't reported the slot, or reports it as empty (no RFID and
            # no filament configured on the printer yet). The caller is
            # explicitly claiming this slot for a non-RFID spool — fall back to
            # the synthetic per-slot id so the bind proceeds. The
            # `activate_filament` call below then teaches the printer what's
            # loaded. Mirrors the web UI's tray-bind flow.
            printer_serial = request.app.state.settings.printer_serial
            if not printer_serial:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{tray_label(payload.tray)} has no UUID and PRINTER_SERIAL "
                        "is not configured, so the spool can't be bound to the slot."
                    ),
                )
            tray_uuid = _synthetic_slot_uuid(printer_serial, payload.tray)
        try:
            await request.app.state.spoolman.bind_spool_to_tray_uuid(
                spool_id=payload.spool_id, tray_uuid=tray_uuid,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to bind spool {payload.spool_id} to tray_uuid: {exc}",
            )

    success, mqtt_message = mqtt.activate_filament(
        tray=payload.tray,
        tray_info_idx=ams_filament_id,
        color_hex=payload.color_hex,
        nozzle_temp_min=payload.nozzle_temp_min,
        nozzle_temp_max=payload.nozzle_temp_max,
        filament_type=filament_type,
        setting_id=setting_id or None,
        bed_temp=payload.bed_temp or None,
        cali_idx=-1,
        remain=-1,
    )

    target_label = tray_label(payload.tray)
    message = (
        f"Updated {target_label}: {profile_name}"
        if success
        else f"Failed to update {target_label}: {mqtt_message}"
    )
    if success and mqtt_message != "Command sent to printer":
        message = f"{message} ({mqtt_message})"

    if success:
        recent_activations: list[ActivationRecord] = request.app.state.recent_activations
        recent_activations.insert(
            0,
            ActivationRecord(
                created_at=datetime.now(timezone.utc),
                profile_name=profile_name,
                tray=payload.tray,
                color_hex=payload.color_hex,
                success=True,
            ),
        )
        del recent_activations[10:]

    return ActivateResponse(success=success, profile_name=profile_name, message=message)


@router.post("/reload")
async def reload_profiles(
    request: Request,
    machine: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        reload_summary, profiles = await request.app.state.orcaslicer.reload_profiles(machine)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reload profiles: {exc}") from exc

    return {
        "success": True,
        "orcaslicer": reload_summary,
        "profiles_loaded": len(profiles),
    }


@router.post("/import")
async def import_profile(
    request: Request,
    payload: dict[str, Any],
    machine: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return await request.app.state.orcaslicer.import_profile(payload, machine)
    except httpx.HTTPStatusError as exc:
        error_detail = exc.response.text.strip()
        if not error_detail:
            error_detail = str(exc)
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Failed to import profile: {error_detail}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Import request failed: {exc}") from exc
