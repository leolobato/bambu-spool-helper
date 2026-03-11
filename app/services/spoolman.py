"""HTTP client for Spoolman filament linking."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.models import SpoolmanFilament, SpoolmanSpool


class SpoolmanClient:
    REQUIRED_SETTINGS_FILAMENT_FIELDS = [
        {
            "key": "nozzle_temp",
            "name": "Nozzle Temperature",
            "field_type": "integer_range",
            "unit": "°C",
        },
        {
            "key": "bed_temp",
            "name": "Bed Temperature",
            "field_type": "integer_range",
            "unit": "°C",
        },
        {
            "key": "printing_speed",
            "name": "Printing Speed",
            "field_type": "integer_range",
            "unit": "mm/s",
        },
        {
            "key": "ams_filament_type",
            "name": "ams_filament_type",
            "field_type": "text",
            "unit": "",
        },
        {
            "key": "ams_filament_id",
            "name": "ams_filament_id",
            "field_type": "text",
            "unit": "",
        },
    ]
    REQUIRED_EXTRA_FIELDS = [
        {
            "key": "ams_filament_id",
            "name": "AMS Filament ID",
            "field_type": "text",
            "unit": "",
        },
        {
            "key": "ams_filament_type",
            "name": "AMS Filament Type",
            "field_type": "text",
            "unit": "",
        },
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

    async def get_filament(self, filament_id: int) -> SpoolmanFilament:
        response = await self._client.get(f"/api/v1/filament/{filament_id}")
        response.raise_for_status()
        return SpoolmanFilament.model_validate(response.json())

    async def get_filament_fields(self) -> list[dict[str, Any]]:
        response = await self._client.get("/api/v1/field/filament")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    async def validate_required_filament_fields(self) -> dict[str, Any]:
        fields = await self.get_filament_fields()
        return self._validate_field_specs(fields, self.REQUIRED_SETTINGS_FILAMENT_FIELDS)

    async def ensure_required_filament_fields(self) -> dict[str, Any]:
        initial_fields = await self.get_filament_fields()
        initial_validation = self._validate_field_specs(initial_fields, self.REQUIRED_SETTINGS_FILAMENT_FIELDS)

        created_keys: list[str] = []
        errors: list[str] = []
        for spec in initial_validation["missing"]:
            try:
                created = await self._create_filament_field(spec)
            except httpx.HTTPError as exc:
                errors.append(f"{spec['key']}: {exc}")
                continue
            if created:
                created_keys.append(spec["key"])

        final_fields = await self.get_filament_fields()
        final_validation = self._validate_field_specs(final_fields, self.REQUIRED_SETTINGS_FILAMENT_FIELDS)
        return {
            "created_keys": created_keys,
            "errors": errors,
            "validation": final_validation,
        }

    async def ensure_extra_fields(self) -> None:
        fields = await self.get_filament_fields()
        validation = self._validate_field_specs(fields, self.REQUIRED_EXTRA_FIELDS)
        for spec in validation["missing"]:
            await self._create_filament_field(spec)

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

    async def _create_filament_field(self, spec: dict[str, Any]) -> bool:
        payload: dict[str, Any] = {
            "name": spec["name"],
            "field_type": spec["field_type"],
        }
        unit = str(spec.get("unit", "") or "").strip()
        if unit:
            payload["unit"] = unit
        if spec["field_type"] == "text":
            payload["default_value"] = self._json_encode("")

        response = await self._client.post(
            f"/api/v1/field/filament/{spec['key']}",
            json=payload,
        )
        if response.status_code == 409:
            return False
        if response.status_code not in (200, 201):
            response.raise_for_status()
        return True

    @staticmethod
    def _validate_field_specs(fields: list[dict[str, Any]], expected_specs: list[dict[str, Any]]) -> dict[str, Any]:
        fields_by_key = {
            str(field.get("key", "")).strip(): field
            for field in fields
            if str(field.get("key", "")).strip()
        }
        valid: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []

        for spec in expected_specs:
            key = str(spec["key"]).strip()
            actual = fields_by_key.get(key)
            if actual is None:
                missing.append(dict(spec))
                continue

            mismatches: list[str] = []
            if str(actual.get("name", "") or "") != str(spec.get("name", "") or ""):
                mismatches.append(f"name should be {spec['name']}")
            if str(actual.get("field_type", "") or "") != str(spec.get("field_type", "") or ""):
                mismatches.append(f"field_type should be {spec['field_type']}")

            expected_unit = str(spec.get("unit", "") or "")
            actual_unit = str(actual.get("unit", "") or "")
            if actual_unit != expected_unit:
                mismatches.append(f"unit should be {expected_unit or '(empty)'}")

            if mismatches:
                invalid.append({
                    "expected": dict(spec),
                    "actual": dict(actual),
                    "mismatches": mismatches,
                })
                continue

            valid.append({
                "expected": dict(spec),
                "actual": dict(actual),
            })

        return {
            "is_valid": not missing and not invalid,
            "valid": valid,
            "missing": missing,
            "invalid": invalid,
            "valid_count": len(valid),
            "missing_count": len(missing),
            "invalid_count": len(invalid),
            "required_count": len(expected_specs),
        }

    @staticmethod
    def _json_encode(value: str) -> str:
        return json.dumps(value)
