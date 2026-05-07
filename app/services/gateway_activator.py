"""HTTP-based activator that routes through bambu-gateway.

Mirrors `MQTTPrinterClient`'s public surface so `app.state.mqtt` can hold
either implementation. When this is in use, spool-helper does not open its
own MQTT slot to the printer — bambu-gateway owns the printer connection
and we go through its HTTP API for both reads (`/api/ams`) and writes
(`/api/printers/{serial}/ams/{ams}/tray/{tray}/filament`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.services.mqtt_printer import TrayData

logger = logging.getLogger(__name__)

GATEWAY_REQUEST_TIMEOUT_SECONDS = 10.0


class GatewayActivator:
    def __init__(self, gateway_url: str, printer_serial: str) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._serial = printer_serial.strip()
        self._client = httpx.Client(timeout=GATEWAY_REQUEST_TIMEOUT_SECONDS)
        self._last_error: str | None = None
        self._last_message_at: datetime | None = None
        self._trays: dict[int, TrayData] = {}

    @property
    def configured(self) -> bool:
        return bool(self._gateway_url and self._serial)

    def disconnect(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.exception("Failed to close gateway HTTP client cleanly")

    def request_full_status(self) -> None:
        if not self.configured:
            return
        self._refresh_trays_safely()

    def get_tray_data(self) -> dict[int, TrayData]:
        if not self.configured:
            return {}
        if not self._trays:
            self._refresh_trays_safely()
        return dict(self._trays)

    def get_connection_status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "connected": self.configured and self._last_error is None,
            "tray_count": len(self._trays),
            "last_error": self._last_error,
            "last_message_at": (
                self._last_message_at.isoformat() if self._last_message_at else None
            ),
        }

    def activate_filament(
        self,
        tray: int,
        tray_info_idx: str,
        color_hex: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        filament_type: str,
        *,
        setting_id: str | None = None,
        tag_uid: str | None = None,
        bed_temp: int | None = None,
        tray_weight: int | None = None,
        remain: int | None = None,
        k: float | None = None,
        n: float | None = None,
        tray_uuid: str | None = None,
        cali_idx: int | None = None,
    ) -> tuple[bool, str]:
        if not self.configured:
            return True, "Gateway not configured; command skipped"
        if not setting_id:
            return False, (
                "setting_id is required for gateway routing — "
                "spool-helper should resolve it from the linked filament profile"
            )
        try:
            ams_id, tray_id = _map_tray(tray)
        except ValueError as exc:
            return False, str(exc)

        url = (
            f"{self._gateway_url}/api/printers/{self._serial}"
            f"/ams/{ams_id}/tray/{tray_id}/filament"
        )
        body: dict[str, Any] = {
            "setting_id": setting_id,
            "tray_color": _normalize_color(color_hex),
        }
        for key, value in (
            ("tag_uid", tag_uid),
            ("bed_temp", bed_temp),
            ("tray_weight", tray_weight),
            ("remain", remain),
            ("k", k),
            ("n", n),
            ("tray_uuid", tray_uuid),
            ("cali_idx", cali_idx),
        ):
            if value is not None:
                body[key] = value

        try:
            resp = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            self._last_error = f"Gateway request failed: {exc}"
            logger.error("Gateway POST %s failed: %s", url, exc)
            return False, self._last_error

        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            msg = f"Gateway returned {resp.status_code}: {detail}"
            self._last_error = msg
            logger.error(msg)
            return False, msg

        self._last_error = None
        return True, "Command sent via gateway"

    def _refresh_trays_safely(self) -> None:
        try:
            url = f"{self._gateway_url}/api/ams"
            resp = self._client.get(url, params={"printer_id": self._serial})
            resp.raise_for_status()
            self._trays = _build_tray_data(resp.json())
            self._last_message_at = datetime.now(timezone.utc)
            self._last_error = None
        except httpx.HTTPError as exc:
            self._last_error = f"Gateway unreachable: {exc}"
            logger.warning("Gateway tray refresh failed: %s", exc)


def _build_tray_data(ams_resp: dict) -> dict[int, TrayData]:
    trays: dict[int, TrayData] = {}
    for raw in ams_resp.get("trays") or []:
        ams_id = _coerce_int(raw.get("ams_id"))
        tray_id = _coerce_int(raw.get("tray_id"))
        trays[ams_id * 4 + tray_id] = _to_tray_data(raw)
    vt = ams_resp.get("vt_tray")
    if vt:
        trays[4] = _to_tray_data(vt)
    return trays


def _to_tray_data(raw: dict) -> TrayData:
    td = TrayData()
    td.tray_type = str(raw.get("tray_type") or "")
    td.tray_color = str(raw.get("tray_color") or "")
    td.tray_info_idx = str(raw.get("filament_id") or raw.get("tray_info_idx") or "")
    td.tray_sub_brands = str(raw.get("tray_sub_brands") or "")
    td.tag_uid = str(raw.get("tag_uid") or "")
    td.nozzle_temp_min = _coerce_int(raw.get("nozzle_temp_min"))
    td.nozzle_temp_max = _coerce_int(raw.get("nozzle_temp_max"))
    td.bed_temp = _coerce_int(raw.get("bed_temp"))
    td.remain = _coerce_int(raw.get("remain"), default=-1)
    td.tray_weight = _coerce_int(raw.get("tray_weight"))
    td.tray_uuid = str(raw.get("tray_uuid") or "")
    td.cali_idx = _coerce_int(raw.get("cali_idx"), default=-1)
    k = raw.get("k")
    if k is not None:
        try:
            td.k = float(k)
        except (TypeError, ValueError):
            pass
    n = raw.get("n")
    if n is not None:
        try:
            td.n = float(n)
        except (TypeError, ValueError):
            pass
    return td


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _map_tray(tray: int) -> tuple[int, int]:
    if 0 <= tray <= 3:
        return 0, tray
    if tray == 4:
        return 255, 254
    raise ValueError("Tray must be 0-4")


def _normalize_color(color_hex: str) -> str:
    compact = color_hex.strip().lstrip("#").upper()
    if len(compact) == 6:
        return f"{compact}FF"
    if len(compact) == 8:
        return compact
    return "FFFFFFFF"


def _extract_error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text or "(no body)"
    if isinstance(body, dict):
        detail = body.get("detail")
        if detail:
            return str(detail)
    return str(body)
