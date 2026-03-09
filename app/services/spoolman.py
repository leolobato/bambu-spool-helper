"""HTTP client for Spoolman filament linking."""

from __future__ import annotations

import json

import httpx

from app.models import SpoolmanFilament, SpoolmanSpool


class SpoolmanClient:
    REQUIRED_EXTRA_FIELDS = [
        ("ams_filament_id", "AMS Filament ID"),
        ("ams_filament_type", "AMS Filament Type"),
    ]

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_spools(self) -> list[SpoolmanSpool]:
        response = await self._client.get("/api/v1/spool")
        response.raise_for_status()
        payload = response.json()
        return [SpoolmanSpool.model_validate(item) for item in payload if not item.get("archived")]

    async def get_filaments(self) -> list[SpoolmanFilament]:
        response = await self._client.get("/api/v1/filament")
        response.raise_for_status()
        payload = response.json()
        return [SpoolmanFilament.model_validate(item) for item in payload]

    async def ensure_extra_fields(self) -> None:
        response = await self._client.get("/api/v1/field/filament")
        response.raise_for_status()
        existing = {str(item.get("key", "")) for item in response.json()}

        for key, name in self.REQUIRED_EXTRA_FIELDS:
            if key in existing:
                continue
            create_response = await self._client.post(
                f"/api/v1/field/filament/{key}",
                json={
                    "name": name,
                    "field_type": "text",
                    "default_value": self._json_encode(""),
                },
            )
            if create_response.status_code not in (200, 201, 409):
                create_response.raise_for_status()

    async def link_filament(
        self,
        filament_id: int,
        ams_filament_id: str,
        ams_filament_type: str,
    ) -> None:
        await self.ensure_extra_fields()
        await self._patch_filament(
            filament_id,
            {
                "ams_filament_id": self._json_encode(ams_filament_id),
                "ams_filament_type": self._json_encode(ams_filament_type),
            },
        )

    async def unlink_filament(self, filament_id: int) -> None:
        await self._patch_filament(
            filament_id,
            {
                "ams_filament_id": self._json_encode(""),
                "ams_filament_type": self._json_encode(""),
            },
        )

    async def _patch_filament(self, filament_id: int, extra_fields: dict[str, str]) -> None:
        response = await self._client.patch(
            f"/api/v1/filament/{filament_id}",
            json={"extra": extra_fields},
        )
        if response.status_code not in (200, 204):
            response.raise_for_status()

    @staticmethod
    def _json_encode(value: str) -> str:
        return json.dumps(value)
