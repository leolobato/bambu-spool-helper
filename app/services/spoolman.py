"""HTTP client for Spoolman filament linking."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

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
    REQUIRED_SPOOL_EXTRA_FIELDS = [
        {
            "key": "bambu_tray_uuid",
            "name": "Bambu Tray UUID",
            "field_type": "text",
            # Intentionally no `unit`: Spoolman's schema rejects empty
            # strings (min_length: 1), and there is no unit for a UUID.
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

    async def _get_spool_fields(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/v1/field/spool")
        resp.raise_for_status()
        return resp.json() or []

    async def _create_spool_field(self, spec: dict[str, Any]) -> bool:
        key = spec["key"]
        payload = {k: v for k, v in spec.items() if k != "key"}
        resp = await self._client.post(f"/api/v1/field/spool/{key}", json=payload)
        if resp.status_code in (200, 201):
            return True
        logger.warning("Failed to create spool extra field %s: %s", key, resp.text)
        return False

    async def ensure_spool_extra_fields(self) -> None:
        existing_keys = {f.get("key") for f in await self._get_spool_fields()}
        for spec in self.REQUIRED_SPOOL_EXTRA_FIELDS:
            if spec["key"] not in existing_keys:
                await self._create_spool_field(spec)

    async def validate_required_spool_fields(self) -> dict[str, Any]:
        fields = await self._get_spool_fields()
        return self._validate_field_specs(fields, self.REQUIRED_SPOOL_EXTRA_FIELDS)

    async def ensure_required_spool_fields(self) -> dict[str, Any]:
        initial_fields = await self._get_spool_fields()
        initial_validation = self._validate_field_specs(initial_fields, self.REQUIRED_SPOOL_EXTRA_FIELDS)

        created_keys: list[str] = []
        errors: list[str] = []
        for spec in initial_validation["missing"]:
            try:
                created = await self._create_spool_field(spec)
            except httpx.HTTPError as exc:
                errors.append(f"{spec['key']}: {exc}")
                continue
            if created:
                created_keys.append(spec["key"])

        final_fields = await self._get_spool_fields()
        final_validation = self._validate_field_specs(final_fields, self.REQUIRED_SPOOL_EXTRA_FIELDS)
        return {
            "created_keys": created_keys,
            "errors": errors,
            "validation": final_validation,
        }

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

    async def update_filament_profile_fields(
        self,
        filament_id: int,
        *,
        extruder_temp: int | None = None,
        nozzle_temp: tuple[int, int],
        bed_temp: tuple[int, int],
        basic_bed_temp: int,
    ) -> dict[str, Any]:
        validation_result = await self.ensure_required_filament_fields()
        validation = validation_result["validation"]
        if validation["invalid_count"] > 0:
            invalid_keys = ", ".join(item["expected"]["key"] for item in validation["invalid"])
            raise ValueError(
                f"Spoolman filament fields have invalid definitions: {invalid_keys}. "
                "Fix them in Settings before updating profile fields."
            )

        basic_fields: dict[str, Any] = {
            "settings_bed_temp": int(basic_bed_temp),
        }
        if extruder_temp is not None:
            basic_fields["settings_extruder_temp"] = int(extruder_temp)

        await self._patch_filament(
            filament_id,
            extra_fields={
                "nozzle_temp": self._json_encode_range(nozzle_temp),
                "bed_temp": self._json_encode_range(bed_temp),
            },
            basic_fields=basic_fields,
        )
        return validation_result

    async def _get_spool(self, spool_id: int) -> dict:
        resp = await self._client.get(f"/api/v1/spool/{spool_id}")
        resp.raise_for_status()
        return resp.json()

    async def _patch_spool(
        self,
        spool_id: int,
        extra_fields: dict[str, str | None] | None = None,
        basic_fields: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {}
        if extra_fields is not None:
            current = await self._get_spool(spool_id)
            merged = dict(current.get("extra") or {})
            merged.update(extra_fields)
            payload["extra"] = merged
        if basic_fields:
            payload.update(basic_fields)
        resp = await self._client.patch(f"/api/v1/spool/{spool_id}", json=payload)
        resp.raise_for_status()

    async def bind_spool_to_tray_uuid(self, *, spool_id: int, tray_uuid: str) -> None:
        """Set `bambu_tray_uuid` on `spool_id`, clearing the same uuid from any
        other spool first (move-the-binding).

        No-op when the target spool already holds this uuid. Self-heals the
        `bambu_tray_uuid` field definition if it doesn't yet exist.

        Spoolman PATCH rejects `null` for spool extras and merges (doesn't
        replace) the `extra` dict, so we can't actually delete the key.
        "Clearing" means writing the JSON-encoded empty string `""`.
        """
        await self.ensure_spool_extra_fields()

        encoded = self._json_encode(tray_uuid)
        cleared = self._json_encode("")

        # Find existing holders of this uuid
        resp = await self._client.get("/api/v1/spool")
        resp.raise_for_status()
        all_spools = resp.json() or []
        for spool in all_spools:
            extra = spool.get("extra") or {}
            if extra.get("bambu_tray_uuid") == encoded and spool.get("id") != spool_id:
                await self._patch_spool(spool["id"], extra_fields={"bambu_tray_uuid": cleared})

        # Set on target unless it already has it
        target = await self._get_spool(spool_id)
        target_extra = target.get("extra") or {}
        if target_extra.get("bambu_tray_uuid") == encoded:
            return
        await self._patch_spool(spool_id, extra_fields={"bambu_tray_uuid": encoded})

    async def _patch_filament(
        self,
        filament_id: int,
        extra_fields: dict[str, str] | None = None,
        basic_fields: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if extra_fields is not None:
            current_filament = await self.get_filament(filament_id)
            payload["extra"] = self._merge_extra_fields(current_filament.extra, extra_fields)
        if basic_fields:
            payload.update(basic_fields)

        response = await self._client.patch(
            f"/api/v1/filament/{filament_id}",
            json=payload,
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

    @staticmethod
    def _json_encode_range(values: tuple[int, int]) -> str:
        low, high = sorted((int(values[0]), int(values[1])))
        return json.dumps([low, high])

    @staticmethod
    def _merge_extra_fields(existing: dict[str, str], updates: dict[str, str]) -> dict[str, str]:
        merged = dict(existing or {})
        merged.update(updates)
        return merged
