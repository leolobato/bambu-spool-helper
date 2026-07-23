"""Reconnectable SSE client and active-job file fetcher for bambu-gateway."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx


class GatewayPrintClient:
    def __init__(
        self,
        *,
        gateway_url: str,
        printer_serial: str,
        on_snapshot: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.printer_serial = printer_serial.strip()
        self.on_snapshot = on_snapshot
        self.connected = False
        self.last_event: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=None)

    @property
    def configured(self) -> bool:
        return bool(self.gateway_url and self.printer_serial)

    async def start(self) -> None:
        if not self.configured or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="print-tracking-sse",
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.connected = False

    async def close(self) -> None:
        await self.stop()
        await self._client.aclose()

    async def fetch_3mf(self, job_key: str) -> bytes:
        response = await self._client.get(
            f"{self.gateway_url}/api/printers/"
            f"{quote(self.printer_serial, safe='')}/current-job/file",
            params={"job_key": job_key},
            timeout=90.0,
        )
        response.raise_for_status()
        return response.content

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._stream_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
            await asyncio.sleep(_reconnect_delay(attempt))
            attempt += 1

    async def _stream_once(self) -> None:
        url = (
            f"{self.gateway_url}/api/printers/"
            f"{quote(self.printer_serial, safe='')}/print-events"
        )
        try:
            async with self._client.stream(
                "GET", url, headers={"Accept": "text/event-stream"},
            ) as response:
                response.raise_for_status()
                self.connected = True
                self.last_error = None
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        if data_lines:
                            await self._dispatch(data_lines)
                            data_lines = []
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
        finally:
            self.connected = False

    async def _dispatch(self, data_lines: list[str]) -> None:
        try:
            snapshot = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if not isinstance(snapshot, dict):
            return
        self.last_event = datetime.now(timezone.utc).isoformat()
        await self.on_snapshot(snapshot)


def _reconnect_delay(attempt: int) -> float:
    return min(60.0, 2.0 ** min(max(0, attempt), 6))
