"""Jinja2 + HTMX routes for Spoolman filament linking."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import FilamentProfileResponse, SpoolmanFilament

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
