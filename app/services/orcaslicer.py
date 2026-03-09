"""HTTP client for OrcaSlicer filament profiles."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.models import FilamentProfileResponse

logger = logging.getLogger(__name__)


class OrcaSlicerClient:
    def __init__(self, base_url: str, machine_id: str, detail_fetch_concurrency: int = 10) -> None:
        self._machine_id = machine_id
        self._detail_fetch_concurrency = max(1, detail_fetch_concurrency)
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20.0)
        self._profiles: list[FilamentProfileResponse] = []

    async def close(self) -> None:
        await self._client.aclose()

    async def import_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/profiles/filaments", json=data)
        response.raise_for_status()
        payload = self._normalize_profile_payload(response.json())
        await self.load_profiles()
        return payload

    async def load_profiles(self) -> list[FilamentProfileResponse]:
        response = await self._client.get(
            "/profiles/filaments",
            params={"machine": self._machine_id},
        )
        response.raise_for_status()
        summary_profiles = response.json()

        semaphore = asyncio.Semaphore(self._detail_fetch_concurrency)

        async def fetch_detail(summary: dict[str, Any]) -> FilamentProfileResponse | None:
            tray_info_idx = self._extract_profile_id(summary)
            if not tray_info_idx:
                return None
            async with semaphore:
                detail_response = await self._client.get(f"/profiles/filaments/{tray_info_idx}")
            detail_response.raise_for_status()
            detail = detail_response.json()
            return self._build_profile(summary, detail)

        tasks = [fetch_detail(summary) for summary in summary_profiles]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        loaded_profiles: list[FilamentProfileResponse] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Skipping filament profile after error: %s", result)
                continue
            if result is None:
                continue
            loaded_profiles.append(result)

        loaded_profiles.sort(key=lambda profile: profile.name.lower())
        self._profiles = loaded_profiles
        return self.get_profiles()

    def get_profiles(self) -> list[FilamentProfileResponse]:
        return [profile.model_copy() for profile in self._profiles]

    def find_profile(self, tray_info_idx: str, filament_id: str) -> FilamentProfileResponse | None:
        tray_info_idx = self._normalize_id(tray_info_idx)
        filament_id = self._normalize_id(filament_id)

        exact = next(
            (
                profile
                for profile in self._profiles
                if self._ids_match(profile.tray_info_idx, tray_info_idx)
                and self._ids_match(profile.filament_id, filament_id)
            ),
            None,
        )
        if exact:
            return exact.model_copy()

        tray_info_fallback = next(
            (profile for profile in self._profiles if self._ids_match(profile.tray_info_idx, tray_info_idx)),
            None,
        )
        if tray_info_fallback:
            return tray_info_fallback.model_copy()

        fallback = next(
            (profile for profile in self._profiles if self._ids_match(profile.filament_id, filament_id)),
            None,
        )
        if fallback:
            return fallback.model_copy()
        return None

    @staticmethod
    def _normalize_id(value: str) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def _ids_match(left: str, right: str) -> bool:
        left_norm = OrcaSlicerClient._normalize_id(left)
        right_norm = OrcaSlicerClient._normalize_id(right)
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm:
            return True
        # Orca profiles may expose IDs prefixed with "O" (e.g. OGFA00) while
        # Spoolman links may keep non-prefixed IDs (e.g. GFA00).
        if left_norm.startswith("O") and left_norm[1:] == right_norm:
            return True
        if right_norm.startswith("O") and right_norm[1:] == left_norm:
            return True
        return False

    @staticmethod
    def _extract_profile_id(payload: dict[str, Any]) -> str:
        return str(payload.get("tray_info_idx") or payload.get("setting_id") or "").strip()

    @staticmethod
    def _normalize_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        tray_info_idx = OrcaSlicerClient._extract_profile_id(normalized)
        normalized.pop("setting_id", None)
        if tray_info_idx:
            normalized["tray_info_idx"] = tray_info_idx
        return normalized

    @staticmethod
    def _build_profile(summary: dict[str, Any], detail: dict[str, Any]) -> FilamentProfileResponse:
        nozzle_temp_min = OrcaSlicerClient._extract_first_int(detail, "nozzle_temperature_range_low")
        nozzle_temp_max = OrcaSlicerClient._extract_first_int(detail, "nozzle_temperature_range_high")

        bed_temp = OrcaSlicerClient._extract_first_int(detail, "hot_plate_temp")

        drying_values = OrcaSlicerClient._extract_int_list(
            detail,
            "filament_dev_ams_drying_temperature",
        )
        if len(drying_values) >= 2:
            drying_temp_min, drying_temp_max = sorted(drying_values[:2])
        elif len(drying_values) == 1:
            drying_temp_min = drying_values[0]
            drying_temp_max = drying_values[0]
        else:
            drying_temp_min = 0
            drying_temp_max = 0

        filament_id = OrcaSlicerClient._extract_first_str(detail, "filament_id") or str(
            OrcaSlicerClient._extract_profile_id(summary)
        )
        filament_type = (
            OrcaSlicerClient._extract_first_str(detail, "filament_type")
            or str(summary.get("filament_type", ""))
        )

        return FilamentProfileResponse(
            name=str(summary.get("name") or detail.get("name") or filament_id),
            filament_id=filament_id,
            tray_info_idx=(
                OrcaSlicerClient._extract_profile_id(summary)
                or OrcaSlicerClient._extract_profile_id(detail)
            ),
            filament_type=filament_type,
            nozzle_temp_min=nozzle_temp_min,
            nozzle_temp_max=nozzle_temp_max,
            bed_temp_min=bed_temp,
            bed_temp_max=bed_temp,
            drying_temp_min=drying_temp_min,
            drying_temp_max=drying_temp_max,
            drying_time=OrcaSlicerClient._extract_first_int(detail, "filament_dev_ams_drying_time"),
            print_speed_min=OrcaSlicerClient._extract_first_int(detail, "slow_down_min_speed"),
            print_speed_max=OrcaSlicerClient._extract_first_int(detail, "filament_max_volumetric_speed"),
            k=OrcaSlicerClient._extract_first_float(detail, "k"),
            n=OrcaSlicerClient._extract_first_float(detail, "n"),
            source="system",
        )

    @staticmethod
    def _extract_first_int(payload: dict[str, Any], key: str) -> int:
        values = OrcaSlicerClient._extract_int_list(payload, key)
        return values[0] if values else 0

    @staticmethod
    def _extract_int_list(payload: dict[str, Any], key: str) -> list[int]:
        raw = payload.get(key)
        candidates: list[Any]
        if isinstance(raw, list):
            candidates = raw
        elif raw is None:
            candidates = []
        else:
            candidates = [raw]

        values: list[int] = []
        for candidate in candidates:
            number = OrcaSlicerClient._to_int(candidate)
            if number is not None:
                values.append(number)
        return values

    @staticmethod
    def _extract_first_str(payload: dict[str, Any], key: str) -> str:
        raw = payload.get(key)
        if isinstance(raw, list):
            if not raw:
                return ""
            first = raw[0]
            return str(first).strip() if first is not None else ""
        if raw is None:
            return ""
        return str(raw).strip()

    @staticmethod
    def _extract_first_float(payload: dict[str, Any], key: str) -> float | None:
        raw = payload.get(key)
        candidate = raw[0] if isinstance(raw, list) and raw else raw
        if candidate is None:
            return None
        try:
            return float(str(candidate).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            parsed = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
        if parsed < 0:
            return None
        return parsed
