"""Resolve print filaments to bound spools and deliver idempotent-ish usage."""

from __future__ import annotations

import math
from collections.abc import Callable

EXTERNAL_HELPER_TRAY = 4
EXTERNAL_PRINTER_TRAYS = {-1, 254, 255}


def helper_tray(filament_index: int, mapping: list[int]) -> int:
    if 0 <= filament_index < len(mapping):
        tray = mapping[filament_index]
        return EXTERNAL_HELPER_TRAY if tray in EXTERNAL_PRINTER_TRAYS else tray
    return EXTERNAL_HELPER_TRAY


class SpoolmanUsageReporter:
    def __init__(self, spoolman, get_tray_uuids) -> None:
        self.spoolman = spoolman
        self.get_tray_uuids = get_tray_uuids

    async def snapshot_routes(
        self, filament_indices: set[int], mapping: list[int],
    ) -> tuple[dict[str, dict], list[str]]:
        tray_uuids = await self.get_tray_uuids()
        routes: dict[str, dict] = {}
        warnings: list[str] = []
        for index in sorted(filament_indices):
            tray = helper_tray(index, mapping)
            uuid = tray_uuids.get(tray)
            spool_id = (
                await self.spoolman.lookup_by_tray_uuid(uuid) if uuid else None
            )
            warning = None
            if not uuid:
                warning = f"Filament {index}: tray {tray} has no binding UUID"
            elif spool_id is None:
                warning = (
                    f"Filament {index}: no uniquely bound Spoolman spool "
                    f"for tray {tray}"
                )
            if warning:
                warnings.append(warning)
            routes[str(index)] = {
                "tray": tray,
                "tray_uuid": uuid,
                "spool_id": spool_id,
                "blocked": bool(warning),
                "warning": warning,
            }
        return routes, warnings

    async def deliver_layer(
        self,
        *,
        layer: int,
        usage: dict[int, float],
        checkpoint: dict,
        save: Callable[[], None],
    ) -> tuple[bool, str | None]:
        """Deliver one aggregate PUT per spool; resume partial layers safely."""
        current_uuids = await self.get_tray_uuids()
        by_spool: dict[int, float] = {}
        for filament_index, amount in usage.items():
            route = checkpoint["routes"].get(str(filament_index))
            if route is None or route.get("blocked"):
                continue
            tray = int(route["tray"])
            current_uuid = current_uuids.get(tray)
            if current_uuid != route.get("tray_uuid"):
                self._block_route(
                    checkpoint,
                    route,
                    f"Tray {tray} changed during the print; filament "
                    f"{filament_index} is no longer charged",
                )
                save()
                continue
            current_spool = await self.spoolman.lookup_by_tray_uuid(
                current_uuid,
            )
            if current_spool != route.get("spool_id"):
                self._block_route(
                    checkpoint,
                    route,
                    f"Tray {tray} binding changed during the print; filament "
                    f"{filament_index} is no longer charged",
                )
                save()
                continue
            spool_id = int(current_spool)
            by_spool[spool_id] = by_spool.get(spool_id, 0.0) + float(amount)

        deliveries = checkpoint.setdefault("deliveries", {}).setdefault(
            str(layer), {},
        )
        for spool_id, amount in by_spool.items():
            key = str(spool_id)
            record = deliveries.get(key)
            if record and record.get("status") == "delivered":
                continue
            if record and record.get("status") == "pending":
                resolved, error = await self._reconcile(
                    spool_id, record, checkpoint, save,
                )
                if error:
                    return False, error
                if resolved:
                    continue

            before = await self.spoolman.get_spool_used_length(spool_id)
            record = {
                "spool_id": spool_id,
                "use_length": amount,
                "before_used_length": before,
                "status": "pending",
            }
            deliveries[key] = record
            checkpoint["pending_delivery"] = {
                "layer": layer,
                "spool_id": spool_id,
                **record,
            }
            save()
            try:
                await self.spoolman.use_length(
                    spool_id=spool_id, mm=amount,
                )
            except Exception as exc:
                record["error"] = str(exc)
                save()
                return False, (
                    f"Spoolman delivery outcome is pending for spool "
                    f"{spool_id}: {exc}"
                )
            self._mark_delivered(record, checkpoint, spool_id, amount)
            checkpoint["pending_delivery"] = None
            save()
        return True, None

    async def _reconcile(
        self, spool_id: int, record: dict, checkpoint: dict, save,
    ) -> tuple[bool, str | None]:
        current = await self.spoolman.get_spool_used_length(spool_id)
        before = float(record["before_used_length"])
        amount = float(record["use_length"])
        expected = before + amount
        tolerance = max(0.01, amount * 0.001)
        if math.isclose(current, expected, abs_tol=tolerance):
            self._mark_delivered(record, checkpoint, spool_id, amount)
            checkpoint["pending_delivery"] = None
            save()
            return True, None
        if math.isclose(current, before, abs_tol=tolerance):
            return False, None

        warning = (
            f"Spool {spool_id} used_length changed ambiguously while a "
            "tracking request was pending; automatic charging stopped"
        )
        record["status"] = "ambiguous"
        checkpoint.setdefault("blocked_spools", []).append(spool_id)
        checkpoint.setdefault("warnings", []).append(warning)
        checkpoint["pending_delivery"] = None
        for route in checkpoint.get("routes", {}).values():
            if route.get("spool_id") == spool_id:
                route["blocked"] = True
                route["warning"] = warning
        save()
        return True, None

    @staticmethod
    def _mark_delivered(
        record: dict, checkpoint: dict, spool_id: int, amount: float,
    ) -> None:
        record["status"] = "delivered"
        record.pop("error", None)
        totals = checkpoint.setdefault("spool_totals_mm", {})
        key = str(spool_id)
        totals[key] = float(totals.get(key, 0.0)) + amount

    @staticmethod
    def _block_route(checkpoint: dict, route: dict, warning: str) -> None:
        route["blocked"] = True
        route["warning"] = warning
        warnings = checkpoint.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
