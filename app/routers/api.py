"""REST API routes for status, profiles, and activation."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.models import ActivationRecord, ActivateRequest, ActivateResponse, FilamentProfileResponse, StatusResponse

router = APIRouter(tags=["API"])


TRAY_LABELS = ["Tray 1", "Tray 2", "Tray 3", "Tray 4", "Ext"]


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

    profile = orcaslicer.find_profile(payload.setting_id, payload.filament_id)
    if not profile:
        return ActivateResponse(
            success=False,
            profile_name="",
            message=(
                f"No profile found for setting_id={payload.setting_id}, "
                f"filament_id={payload.filament_id}"
            ),
        )

    success, mqtt_message = mqtt.activate_filament(
        tray=payload.tray,
        tray_info_idx=profile.setting_id,
        color_hex=payload.color_hex,
        nozzle_temp_min=profile.nozzle_temp_min,
        nozzle_temp_max=profile.nozzle_temp_max,
        filament_type=profile.filament_type,
        setting_id=profile.setting_id,
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
