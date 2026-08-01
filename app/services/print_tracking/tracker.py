"""Completed-layer print state machine."""

from __future__ import annotations

import logging

from app.services.print_tracking.gcode_parser import (
    evaluate_gcode,
    gcode_from_3mf,
)
from app.tray_labels import (
    EXTERNAL_HELPER_TRAY,
    migrate_legacy_tray_references,
    tray_label,
)

logger = logging.getLogger(__name__)

FINISHED_STATES = {"FINISH", "FINISHED", "COMPLETED"}
FAILED_STATES = {"FAIL", "FAILED", "FAILURE", "CANCEL", "CANCELLED", "CANCELED"}
ACTIVE_STATES = {"RUNNING", "PAUSE", "PAUSED", "PREPARE", "PREPARING"}


class PrintTracker:
    def __init__(self, *, store, reporter, fetch_3mf) -> None:
        self.store = store
        self.reporter = reporter
        self.fetch_3mf = fetch_3mf
        self.checkpoint: dict | None = None
        self.model: dict[int, dict[int, float]] | None = None
        self.last_error: str | None = None
        self.last_job_key: str | None = None
        self.last_totals: dict[str, float] = {}
        self.last_warnings: list[str] = []

    @property
    def status(self) -> dict:
        cp = self.checkpoint
        return {
            "active": cp is not None and self.model is not None,
            "job_key": cp.get("job_key") if cp else self.last_job_key,
            "job_name": cp.get("job_name") if cp else None,
            "state": cp.get("state") if cp else None,
            "current_layer": cp.get("current_layer") if cp else None,
            "completed_through": cp.get("completed_through") if cp else None,
            "total_layers": cp.get("total_layers") if cp else None,
            "spool_totals_mm": (
                cp.get("spool_totals_mm", {}) if cp else self.last_totals
            ),
            "pending_delivery": cp.get("pending_delivery") if cp else None,
            "usage_validation": cp.get("usage_validation") if cp else None,
            "warnings": (
                list(cp.get("warnings", [])) if cp else list(self.last_warnings)
            ),
            "last_error": self.last_error,
        }

    async def handle_snapshot(self, snapshot: dict) -> None:
        if int(snapshot.get("schema_version") or 0) != 1:
            self.last_error = "Unsupported gateway print snapshot version"
            return
        job_key = str(snapshot.get("job_key") or "")
        state = str(snapshot.get("state") or "IDLE").upper()
        layer_data = snapshot.get("layer") or {}
        try:
            current_layer = max(0, int(layer_data.get("current") or 0))
        except (TypeError, ValueError):
            current_layer = 0
        try:
            total_layers = max(0, int(layer_data.get("total") or 0))
        except (TypeError, ValueError):
            total_layers = 0

        if not job_key:
            if self.checkpoint is not None and state == "IDLE":
                await self._finalize(flush=False, current_layer=current_layer)
            return
        if state == "IDLE":
            if (
                self.checkpoint is not None
                and self.checkpoint.get("job_key") == job_key
            ):
                await self._finalize(
                    flush=False, current_layer=current_layer,
                )
            return
        if state not in ACTIVE_STATES | FINISHED_STATES | FAILED_STATES:
            return

        # A process can restart after the printer has already moved on to a
        # replacement job while the old checkpoint still has a delivery to
        # reconcile. Restore and finish that ledger before activating the new
        # job; never discard an ambiguous Spoolman request.
        saved = self.store.load() if self.checkpoint is None else None
        if saved is not None and saved.get("job_key") != job_key:
            if not self._restore(saved):
                return
            await self._finalize(
                flush=False,
                current_layer=int(saved.get("current_layer") or 0),
            )
            if self.checkpoint is not None:
                return

        if (
            self.checkpoint is None
            or self.checkpoint.get("job_key") != job_key
        ):
            if self.checkpoint is not None:
                # Job replacement is a cancellation of the previous job:
                # never charge its unconfirmed partial layer.
                await self._finalize(
                    flush=False,
                    current_layer=int(
                        self.checkpoint.get("current_layer") or 0
                    ),
                )
                if self.checkpoint is not None:
                    # The old job still has an unresolved delivery. Keep its
                    # checkpoint and retry it on a later snapshot.
                    return
            if not await self._activate(snapshot, current_layer):
                return

        assert self.checkpoint is not None
        self.checkpoint["state"] = state
        self.checkpoint["current_layer"] = current_layer
        # Firmware keeps repeating the previous job's final layer_num until
        # the replacement job lays its first layer, so an activation inside
        # that window baselines completed_through too high. Layer numbers are
        # monotonic within one job; a backward report proves the baseline was
        # stale — lower it so the skipped layers still get charged. The
        # per-layer deliveries ledger keeps this from double-charging.
        if current_layer - 1 < int(
            self.checkpoint.get("completed_through", -1)
        ):
            self.checkpoint["completed_through"] = current_layer - 1
        if total_layers:
            self.checkpoint["total_layers"] = total_layers
        self._detect_mapping_change(snapshot.get("ams_mapping") or [])
        self._apply_usage_reference(snapshot.get("usage_reference"))
        self.store.save(self.checkpoint)

        if state in FINISHED_STATES:
            await self._finalize(flush=True, current_layer=current_layer)
        elif state in FAILED_STATES:
            await self._finalize(flush=False, current_layer=current_layer)
        else:
            await self._charge_through(current_layer - 1)

    async def _activate(self, snapshot: dict, current_layer: int) -> bool:
        job_key = str(snapshot["job_key"])
        saved = self.store.load()
        if saved is not None and saved.get("job_key") == job_key:
            try:
                if not self._restore(saved):
                    return False
                self._detect_mapping_change(
                    snapshot.get("ams_mapping") or [],
                )
                self.store.save(self.checkpoint)
                self.last_error = None
                # A matching checkpoint authorizes reconnect catch-up.
                await self._charge_through(current_layer - 1)
                return True
            except Exception as exc:
                self.last_error = f"Tracking checkpoint is unreadable: {exc}"
                logger.exception("Failed to restore tracking checkpoint")
                return False

        # The gateway deliberately retains its last complete snapshot. A
        # terminal snapshot without a matching checkpoint cannot represent
        # work this helper still owes, so activating it would replay tail
        # layers after every reconnect or enable.
        state = str(snapshot.get("state") or "").upper()
        if state in FINISHED_STATES | FAILED_STATES:
            return False

        # No matching checkpoint: deliberately baseline at the current layer.
        # This is also the re-enable behavior after a disabled interval.
        self.store.clear_checkpoint()
        source = snapshot.get("source") or {}
        if not source.get("available"):
            self.last_error = source.get("reason") or "Active 3MF unavailable"
            return False
        try:
            data = await self.fetch_3mf(job_key)
            entry, text = gcode_from_3mf(data, snapshot.get("gcode_entry"))
            model = evaluate_gcode(text)
            mapping = [
                int(value) for value in (snapshot.get("ams_mapping") or [])
            ]
            indices = {
                filament
                for layer in model.values()
                for filament in layer
            }
            mapping, route_warning = _resolve_missing_mapping(
                mapping, indices, snapshot.get("active_tray"),
            )
            if mapping:
                routes, warnings = await self.reporter.snapshot_routes(
                    indices, mapping,
                )
                if route_warning:
                    warnings.insert(0, route_warning)
            else:
                warning = route_warning or (
                    "Printer did not report an AMS mapping; automatic "
                    "charging is blocked for this job"
                )
                warnings = [warning]
                routes = {
                    str(index): {
                        "tray": None,
                        "tray_uuid": None,
                        "spool_id": None,
                        "blocked": True,
                        "warning": warning,
                    }
                    for index in sorted(indices)
                }
            self.store.save_model(data)
            self.model = model
            reported_total = snapshot.get("layer", {}).get("total", 0)
            try:
                total_layers = max(0, int(reported_total or 0))
            except (TypeError, ValueError):
                total_layers = 0
            self.checkpoint = {
                "job_key": job_key,
                "job_name": snapshot.get("job_name"),
                "gcode_entry": entry,
                "ams_mapping": mapping,
                "routes": routes,
                "state": str(snapshot.get("state") or "RUNNING").upper(),
                "current_layer": current_layer,
                "completed_through": current_layer - 1,
                "total_layers": total_layers or _model_layer_count(model),
                "spool_totals_mm": {},
                "deliveries": {},
                "pending_delivery": None,
                "blocked_spools": [],
                "usage_validation": None,
                "usage_reference_signature": None,
                "warnings": warnings,
            }
            self.store.save(self.checkpoint)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = f"Unable to load active 3MF: {exc}"
            logger.exception("Unable to start print tracking")
            self.model = None
            self.checkpoint = None
            self.store.clear_checkpoint()
            return False

    def _restore(self, saved: dict) -> bool:
        try:
            _, text = gcode_from_3mf(
                self.store.model_bytes(), saved.get("gcode_entry"),
            )
            self.model = evaluate_gcode(text)
            saved.setdefault("total_layers", _model_layer_count(self.model))
            saved.setdefault("usage_validation", None)
            saved.setdefault("usage_reference_signature", None)
            saved["warnings"] = [
                migrate_legacy_tray_references(warning)
                for warning in saved.get("warnings", [])
            ]
            for route in saved.get("routes", {}).values():
                if route.get("warning"):
                    route["warning"] = migrate_legacy_tray_references(
                        route["warning"],
                    )
            self.checkpoint = saved
            return True
        except Exception as exc:
            self.last_error = f"Tracking checkpoint is unreadable: {exc}"
            logger.exception("Failed to restore tracking checkpoint")
            self.model = None
            self.checkpoint = None
            return False

    def _detect_mapping_change(self, mapping) -> None:
        assert self.checkpoint is not None
        try:
            current = [int(value) for value in mapping]
        except (TypeError, ValueError):
            current = []
        original = self.checkpoint.get("ams_mapping") or []
        if not current or current == original:
            return
        for index, route in self.checkpoint.get("routes", {}).items():
            i = int(index)
            old = original[i] if i < len(original) else None
            new = current[i] if i < len(current) else None
            if old == new:
                continue
            warning = (
                f"AMS mapping for filament {i} changed during the print; "
                "automatic charging stopped for that filament"
            )
            route["blocked"] = True
            route["warning"] = warning
            if warning not in self.checkpoint["warnings"]:
                self.checkpoint["warnings"].append(warning)

    def _apply_usage_reference(self, reference) -> None:
        """Compare cloud slicer totals with parsed G-code, without charging it."""
        if (
            self.checkpoint is None
            or self.model is None
            or not isinstance(reference, dict)
            or not reference.get("available")
        ):
            return
        expected_items = reference.get("filaments")
        if not isinstance(expected_items, list):
            return

        expected: dict[int, float] = {}
        for item in expected_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("filament_index"))
                amount = float(item.get("used_length_mm"))
            except (TypeError, ValueError):
                continue
            if index >= 0 and amount >= 0:
                expected[index] = amount
        if not expected:
            return

        signature = {
            "subtask_id": str(reference.get("subtask_id") or ""),
            "expected_mm": {
                str(index): round(amount, 3)
                for index, amount in sorted(expected.items())
            },
        }
        if self.checkpoint.get("usage_reference_signature") == signature:
            return

        parsed: dict[int, float] = {}
        for layer in self.model.values():
            for index, amount in layer.items():
                parsed[index] = parsed.get(index, 0.0) + amount

        comparisons = []
        mismatches = []
        for index in sorted(set(expected) | set(parsed)):
            expected_mm = expected.get(index, 0.0)
            parsed_mm = parsed.get(index, 0.0)
            difference_mm = parsed_mm - expected_mm
            tolerance_mm = max(50.0, expected_mm * 0.15)
            matched = abs(difference_mm) <= tolerance_mm
            difference_percent = (
                difference_mm / expected_mm * 100.0
                if expected_mm > 0 else None
            )
            comparisons.append({
                "filament_index": index,
                "expected_mm": round(expected_mm, 3),
                "parsed_mm": round(parsed_mm, 3),
                "difference_mm": round(difference_mm, 3),
                "difference_percent": (
                    round(difference_percent, 1)
                    if difference_percent is not None else None
                ),
                "matched": matched,
            })
            if not matched:
                difference = (
                    f"{abs(difference_percent):.1f}%"
                    if difference_percent is not None
                    else f"{abs(difference_mm):.0f} mm"
                )
                mismatches.append(
                    f"filament {index} parsed {parsed_mm:.0f} mm versus "
                    f"Bambu Cloud's {expected_mm:.0f} mm ({difference} apart)"
                )

        self.checkpoint["usage_validation"] = {
            "source": "bambu_cloud",
            "available": True,
            "matched": not mismatches,
            "tolerance_percent": 15,
            "minimum_tolerance_mm": 50,
            "filaments": comparisons,
        }
        self.checkpoint["usage_reference_signature"] = signature
        if mismatches:
            warning = (
                "G-code usage validation warning: "
                + "; ".join(mismatches)
                + ". Charging still follows parsed completed-layer G-code."
            )
            if warning not in self.checkpoint["warnings"]:
                self.checkpoint["warnings"].append(warning)

    async def _charge_through(self, target_layer: int) -> bool:
        if self.checkpoint is None or self.model is None:
            return False
        start = int(self.checkpoint.get("completed_through", -1)) + 1
        for layer in range(start, target_layer + 1):
            usage = self.model.get(layer, {})
            if usage:
                ok, error = await self.reporter.deliver_layer(
                    layer=layer,
                    usage=usage,
                    checkpoint=self.checkpoint,
                    save=lambda: self.store.save(self.checkpoint),
                )
                if not ok:
                    self.last_error = error
                    return False
            self.checkpoint["completed_through"] = layer
            self.store.save(self.checkpoint)
        self.last_error = None
        return True

    async def _finalize(self, *, flush: bool, current_layer: int) -> None:
        if self.checkpoint is None:
            return
        target = (
            max(self.model, default=-1)
            if flush and self.model is not None
            else current_layer - 1
        )
        if not await self._charge_through(target):
            return
        self.last_job_key = self.checkpoint.get("job_key")
        self.last_totals = dict(
            self.checkpoint.get("spool_totals_mm") or {},
        )
        self.last_warnings = list(self.checkpoint.get("warnings") or [])
        self.checkpoint = None
        self.model = None
        self.store.clear_checkpoint()

    def clear(self) -> None:
        if self.checkpoint:
            self.last_job_key = self.checkpoint.get("job_key")
            self.last_totals = dict(
                self.checkpoint.get("spool_totals_mm") or {},
            )
            self.last_warnings = list(
                self.checkpoint.get("warnings") or [],
            )
        self.checkpoint = None
        self.model = None
        self.last_error = None
        self.store.clear_checkpoint()


def _model_layer_count(model: dict[int, dict[int, float]]) -> int:
    return max(model, default=-1) + 1


def _resolve_missing_mapping(
    mapping: list[int],
    filament_indices: set[int],
    active_tray,
) -> tuple[list[int], str | None]:
    if mapping:
        return mapping, None
    if len(filament_indices) != 1:
        return [], (
            "Printer did not report an AMS mapping for this multicolor job; "
            "automatic charging is blocked"
        )
    try:
        tray = int(active_tray)
    except (TypeError, ValueError):
        return [], (
            "Printer did not report an AMS mapping or active tray; automatic "
            "charging is blocked"
        )
    # tray_now 255 means no tray is feeding (e.g. during PREPARE) — only 254
    # identifies the external spool. Routing 255 would charge whichever spool
    # is bound to the external slot.
    if tray == 255:
        return [], (
            "Printer did not report an AMS mapping and no tray is loaded "
            "yet; automatic charging is blocked"
        )
    filament = next(iter(filament_indices))
    inferred = [-1] * (filament + 1)
    inferred[filament] = tray
    label = tray_label(EXTERNAL_HELPER_TRAY if tray == 254 else tray)
    return inferred, (
        f"Printer did not report an AMS mapping; single-filament usage is "
        f"routed through the reported active tray {label}"
    )
