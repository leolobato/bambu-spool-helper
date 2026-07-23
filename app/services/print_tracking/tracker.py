"""Completed-layer print state machine."""

from __future__ import annotations

import logging

from app.services.print_tracking.gcode_parser import (
    evaluate_gcode,
    gcode_from_3mf,
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
            "warnings": list(cp.get("warnings", [])) if cp else [],
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
        if total_layers:
            self.checkpoint["total_layers"] = total_layers
        self._detect_mapping_change(snapshot.get("ams_mapping") or [])
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
        self.checkpoint = None
        self.model = None
        self.store.clear_checkpoint()

    def clear(self) -> None:
        if self.checkpoint:
            self.last_job_key = self.checkpoint.get("job_key")
            self.last_totals = dict(
                self.checkpoint.get("spool_totals_mm") or {},
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
    filament = next(iter(filament_indices))
    inferred = [-1] * (filament + 1)
    inferred[filament] = tray
    return inferred, (
        f"Printer did not report an AMS mapping; single-filament usage is "
        f"routed through the reported active tray {tray}"
    )
