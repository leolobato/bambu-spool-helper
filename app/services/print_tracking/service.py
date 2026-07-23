"""Lifecycle and operator-control facade for print tracking."""

from __future__ import annotations

import asyncio

from app.services.print_tracking.gateway import GatewayPrintClient
from app.services.print_tracking.reporter import SpoolmanUsageReporter
from app.services.print_tracking.store import TrackingStore
from app.services.print_tracking.tracker import PrintTracker


class PrintTrackingService:
    def __init__(
        self,
        *,
        gateway_url: str,
        printer_serial: str,
        directory: str,
        spoolman,
        get_tray_uuids,
    ) -> None:
        self.store = TrackingStore(directory)
        self.available = bool(gateway_url and printer_serial)
        self.enabled = self.store.enabled()
        self._lock = asyncio.Lock()
        self._last_snapshot: dict | None = None
        reporter = SpoolmanUsageReporter(spoolman, get_tray_uuids)
        self.gateway = GatewayPrintClient(
            gateway_url=gateway_url,
            printer_serial=printer_serial,
            on_snapshot=self._handle_snapshot,
        )
        self.tracker = PrintTracker(
            store=self.store,
            reporter=reporter,
            fetch_3mf=self.gateway.fetch_3mf,
        )

    async def start(self) -> None:
        if self.available and self.enabled:
            await self.gateway.start()

    async def close(self) -> None:
        await self.gateway.close()

    async def set_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self.store.set_enabled(enabled)
            self.enabled = enabled
            if not enabled:
                # The lock waits for any in-flight Spoolman request. Stopping
                # then clearing ensures later snapshots cannot backfill.
                await self.gateway.stop()
                self.tracker.clear()
                self._last_snapshot = None
            elif self.available:
                await self.gateway.start()

    async def _handle_snapshot(self, snapshot: dict) -> None:
        async with self._lock:
            if not self.enabled:
                return
            self._last_snapshot = snapshot
            await self.tracker.handle_snapshot(snapshot)

    @property
    def status(self) -> dict:
        tracker = self.tracker.status
        snapshot = self._last_snapshot or {}
        return {
            "available": self.available,
            "enabled": self.enabled,
            "connected": self.gateway.connected,
            "active": tracker["active"],
            "job_key": tracker["job_key"],
            "job_name": tracker["job_name"],
            "state": tracker["state"] or snapshot.get("state"),
            "current_layer": tracker["current_layer"],
            "completed_through": tracker["completed_through"],
            "total_layers": tracker["total_layers"],
            "spool_totals_mm": tracker["spool_totals_mm"],
            "pending_delivery": tracker["pending_delivery"],
            "warnings": tracker["warnings"],
            "last_event": self.gateway.last_event,
            "last_error": tracker["last_error"] or self.gateway.last_error,
        }
