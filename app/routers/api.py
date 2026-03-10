"""REST API routes for status, profiles, and activation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.models import ActivationRecord, ActivateRequest, ActivateResponse, FilamentProfileResponse, StatusResponse
from app.routers.web import _infer_filament_type_from_name

router = APIRouter(tags=["API"])


TRAY_LABELS = ["Tray 1", "Tray 2", "Tray 3", "Tray 4", "Ext"]


def _resolve_activation_filament_type(profile_name: str, filament_type: str) -> str:
    normalized_filament_type = str(filament_type or "").strip()
    if normalized_filament_type:
        return normalized_filament_type
    return _infer_filament_type_from_name(profile_name)


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse:
    settings = request.app.state.settings
    profile_count = len(request.app.state.orcaslicer.get_profiles())
    return StatusResponse(port=settings.port, profiles_loaded=profile_count)


@router.get("/profiles", response_model=list[FilamentProfileResponse])
async def get_profiles(
    request: Request,
    search: str | None = Query(default=None),
) -> list[FilamentProfileResponse]:
    profiles = request.app.state.orcaslicer.get_profiles()
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
    orcaslicer = request.app.state.orcaslicer
    mqtt = request.app.state.mqtt

    profile = orcaslicer.find_profile(payload.filament_id)
    if not profile:
        return ActivateResponse(
            success=False,
            profile_name="",
            message=f"No profile found for filament_id={payload.filament_id}",
        )
    ams_payload_filament_id = (profile.filament_id or "").strip()
    if not ams_payload_filament_id:
        return ActivateResponse(
            success=False,
            profile_name=profile.name,
            message=f"Profile {profile.name} has no filament_id",
        )
    activation_filament_type = _resolve_activation_filament_type(profile.name, profile.filament_type)
    if not activation_filament_type:
        return ActivateResponse(
            success=False,
            profile_name=profile.name,
            message=f"Profile {profile.name} has no filament_type",
        )

    success, mqtt_message = mqtt.activate_filament(
        tray=payload.tray,
        tray_info_idx=ams_payload_filament_id,
        color_hex=payload.color_hex,
        nozzle_temp_min=profile.nozzle_temp_min,
        nozzle_temp_max=profile.nozzle_temp_max,
        filament_type=activation_filament_type,
        bed_temp=profile.bed_temp_min,
        k=profile.k,
        n=profile.n,
        cali_idx=-1,
        remain=-1,
    )

    tray_label = TRAY_LABELS[payload.tray]
    message = (
        f"Updated {tray_label}: {profile.name}"
        if success
        else f"Failed to update {tray_label}: {mqtt_message}"
    )
    if success and mqtt_message != "Command sent to printer":
        message = f"{message} ({mqtt_message})"

    if success:
        recent_activations: list[ActivationRecord] = request.app.state.recent_activations
        recent_activations.insert(
            0,
            ActivationRecord(
                created_at=datetime.now(timezone.utc),
                profile_name=profile.name,
                tray=payload.tray,
                color_hex=payload.color_hex,
                success=True,
            ),
        )
        del recent_activations[10:]

    return ActivateResponse(success=success, profile_name=profile.name, message=message)


@router.post("/reload")
async def reload_profiles(request: Request) -> dict[str, int | bool]:
    try:
        profiles = await request.app.state.orcaslicer.load_profiles()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reload profiles: {exc}") from exc

    return {
        "success": True,
        "profiles_loaded": len(profiles),
    }


@router.post("/import")
async def import_profile(
    request: Request,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return await request.app.state.orcaslicer.import_profile(payload)
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
