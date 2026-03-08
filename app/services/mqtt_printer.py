"""MQTT client for activating AMS filament settings on a Bambu printer."""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

MQTT_PORT = 8883
MQTT_USERNAME = "bblp"


class TrayData:
    """Raw tray data from MQTT report."""

    def __init__(self) -> None:
        self.tray_type: str = ""
        self.tray_color: str = ""
        self.tray_info_idx: str = ""
        self.nozzle_temp_min: int = 0
        self.nozzle_temp_max: int = 0


class MQTTPrinterClient:
    def __init__(self, ip: str, access_code: str, serial: str) -> None:
        self._ip = ip.strip()
        self._access_code = access_code.strip()
        self._serial = serial.strip()

        self._client: mqtt.Client | None = None
        self._connected = False
        self._sequence = 0
        self._lock = threading.Lock()
        self._trays: dict[int, TrayData] = {}  # tray_index -> TrayData
        self._last_error: str | None = None
        self._last_message_at: datetime | None = None

    def _serial_masked(self) -> str:
        if len(self._serial) <= 6:
            return self._serial
        return f"{self._serial[:3]}...{self._serial[-3:]}"

    @staticmethod
    def _reason_details(reason_code: object) -> str:
        try:
            numeric = int(reason_code)  # type: ignore[arg-type]
            return f"{reason_code} (code={numeric})"
        except (TypeError, ValueError):
            return str(reason_code)

    @staticmethod
    def _to_int(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @property
    def configured(self) -> bool:
        return bool(self._ip and self._access_code and self._serial)

    def connect(self) -> None:
        if not self.configured:
            logger.warning("MQTT not configured; activation commands will be accepted but not sent")
            with self._lock:
                self._connected = False
                self._last_error = "MQTT not configured"
            return

        logger.info(
            "Starting MQTT connection to %s:%d (serial=%s, user=%s, access_code_len=%d)",
            self._ip,
            MQTT_PORT,
            self._serial_masked(),
            MQTT_USERNAME,
            len(self._access_code),
        )
        client_id = f"spool-helper-{self._serial[-6:]}-{int(time.time())}"
        logger.info("Using MQTT client_id=%s", client_id)
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(MQTT_USERNAME, self._access_code)
        client.enable_logger(logger)

        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls_context.check_hostname = False
        tls_context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(tls_context)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe
        client.on_publish = self._on_publish
        client.on_message = self._on_message

        self._client = client

        try:
            client.connect_async(self._ip, MQTT_PORT, keepalive=60)
            logger.info("MQTT async connect scheduled")
            with self._lock:
                self._last_error = None
        except Exception:
            logger.exception("Failed to connect to printer MQTT broker at %s:%d", self._ip, MQTT_PORT)
            with self._lock:
                self._connected = False
                self._last_error = f"Failed to connect to {self._ip}:{MQTT_PORT}"
            return

        client.loop_start()

    def disconnect(self) -> None:
        if self._client is None:
            return
        logger.info("Stopping MQTT client loop and disconnecting")
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None
        with self._lock:
            self._connected = False
            self._last_error = "Disconnected"

    def activate_filament(
        self,
        tray: int,
        filament_id: str,
        color_hex: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        filament_type: str,
    ) -> tuple[bool, str]:
        if not self.configured:
            return True, "MQTT not configured; command skipped"

        client = self._client
        if client is None:
            return False, "MQTT client is not initialized"

        if not self._connected:
            return False, "MQTT connection is not ready"

        try:
            ams_id, tray_id = self._map_tray(tray)
        except ValueError as exc:
            return False, str(exc)

        payload = {
            "print": {
                "sequence_id": str(self._next_sequence()),
                "command": "ams_filament_setting",
                "ams_id": ams_id,
                "tray_id": tray_id,
                "tray_info_idx": filament_id,
                "tray_color": self._normalize_color(color_hex),
                "nozzle_temp_min": nozzle_temp_min,
                "nozzle_temp_max": nozzle_temp_max,
                "tray_type": filament_type,
            }
        }

        topic = f"device/{self._serial}/request"
        logger.info(
            "Publishing AMS filament setting to topic=%s tray=%d filament_id=%s",
            topic,
            tray,
            filament_id,
        )
        publish_info = client.publish(topic, json.dumps(payload))
        if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish failed to topic=%s rc=%s", topic, publish_info.rc)
            return False, f"MQTT publish failed (rc={publish_info.rc})"

        publish_info.wait_for_publish(timeout=3.0)
        if not publish_info.is_published():
            logger.error("MQTT publish timeout to topic=%s", topic)
            return False, "MQTT publish timeout"

        logger.info("MQTT publish succeeded for tray=%d filament_id=%s", tray, filament_id)
        return True, "Command sent to printer"

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    @staticmethod
    def _map_tray(tray: int) -> tuple[int, int]:
        if 0 <= tray <= 3:
            return 0, tray
        if tray == 4:
            return 255, 254
        raise ValueError("Tray must be 0-4")

    @staticmethod
    def _normalize_color(color_hex: str) -> str:
        compact = color_hex.strip().lstrip("#").upper()
        if len(compact) == 6:
            return f"{compact}FF"
        if len(compact) == 8:
            return compact
        return "FFFFFFFF"

    def get_tray_data(self) -> dict[int, TrayData]:
        with self._lock:
            return dict(self._trays)

    def get_connection_status(self) -> dict[str, object]:
        with self._lock:
            return {
                "configured": self.configured,
                "connected": self._connected,
                "tray_count": len(self._trays),
                "last_error": self._last_error,
                "last_message_at": self._last_message_at.isoformat() if self._last_message_at else None,
            }

    def request_full_status(self) -> None:
        self._request_full_status()

    def _request_full_status(self) -> None:
        if self._client is None:
            logger.debug("Skipping pushall request: MQTT client not initialized")
            return
        if not self._connected:
            logger.debug("Skipping pushall request: MQTT is not connected")
            return
        payload = json.dumps({
            "pushing": {"sequence_id": str(self._next_sequence()), "command": "pushall"},
        })
        topic = f"device/{self._serial}/request"
        publish_info = self._client.publish(topic, payload)
        if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("Failed to request full status via MQTT topic=%s rc=%s", topic, publish_info.rc)
        else:
            logger.info("Requested full AMS status via topic=%s", topic)

    def _parse_ams_report(self, data: dict) -> None:
        print_data = data.get("print", {})
        ams_data = print_data.get("ams", {})
        ams_units = ams_data.get("ams", [])

        with self._lock:
            for unit in ams_units:
                ams_id = self._to_int(unit.get("id", 0), default=0)
                for tray in unit.get("tray", []):
                    tray_id = self._to_int(tray.get("id", 0), default=0)
                    tray_index = ams_id * 4 + tray_id
                    td = self._trays.setdefault(tray_index, TrayData())
                    if "tray_type" in tray:
                        td.tray_type = str(tray["tray_type"])
                    if "tray_color" in tray:
                        td.tray_color = str(tray["tray_color"])
                    if "tray_info_idx" in tray:
                        td.tray_info_idx = str(tray["tray_info_idx"])
                    if "nozzle_temp_min" in tray:
                        td.nozzle_temp_min = self._to_int(tray["nozzle_temp_min"], default=0)
                    if "nozzle_temp_max" in tray:
                        td.nozzle_temp_max = self._to_int(tray["nozzle_temp_max"], default=0)

            vt_tray = print_data.get("vt_tray")
            if vt_tray:
                td = self._trays.setdefault(4, TrayData())
                if "tray_type" in vt_tray:
                    td.tray_type = str(vt_tray["tray_type"])
                if "tray_color" in vt_tray:
                    td.tray_color = str(vt_tray["tray_color"])
                if "tray_info_idx" in vt_tray:
                    td.tray_info_idx = str(vt_tray["tray_info_idx"])
                if "nozzle_temp_min" in vt_tray:
                    td.nozzle_temp_min = self._to_int(vt_tray["nozzle_temp_min"], default=0)
                if "nozzle_temp_max" in vt_tray:
                    td.nozzle_temp_max = self._to_int(vt_tray["nozzle_temp_max"], default=0)
            tray_count = len(self._trays)
        logger.debug("Parsed AMS report; cached trays=%d", tray_count)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        connected = reason_code == 0
        with self._lock:
            self._connected = connected
            self._last_error = None if connected else f"MQTT connect failed (rc={reason_code})"

        if connected:
            report_topic = f"device/{self._serial}/report"
            logger.info("Connected to printer MQTT broker; subscribing to topic=%s", report_topic)
            subscribe_result = client.subscribe(report_topic)
            logger.info("MQTT subscribe request sent result=%s", subscribe_result)
            self._request_full_status()
        else:
            logger.error(
                "Failed to connect to printer MQTT broker (reason=%s)",
                self._reason_details(reason_code),
            )

    def _on_connect_fail(self, client, userdata) -> None:
        with self._lock:
            self._connected = False
            self._last_error = "MQTT network connect failed"
        logger.error("MQTT network connect failed (IP unreachable, TLS negotiation, or socket failure)")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        reason_details = self._reason_details(reason_code)
        with self._lock:
            self._connected = False
            if reason_code != 0:
                self._last_error = f"Disconnected from printer ({reason_details})"
        if reason_code == 0:
            logger.info("MQTT disconnected cleanly")
        else:
            logger.warning(
                "Disconnected from printer MQTT broker (reason=%s, flags=%s)",
                reason_details,
                disconnect_flags,
            )

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None) -> None:
        logger.info("MQTT subscribe acknowledged mid=%s reason_codes=%s", mid, reason_codes)

    def _on_publish(self, client, userdata, mid, reason_codes=None, properties=None) -> None:
        logger.debug("MQTT publish acknowledged mid=%s reason_codes=%s", mid, reason_codes)

    def _on_message(self, client, userdata, msg) -> None:
        logger.debug("MQTT message received topic=%s payload_bytes=%d", msg.topic, len(msg.payload))
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Ignoring MQTT message with invalid JSON payload from topic=%s", msg.topic)
            return
        try:
            self._parse_ams_report(data)
        except Exception:
            logger.exception("Failed parsing MQTT report payload from topic=%s", msg.topic)
            with self._lock:
                self._last_error = "Failed to parse MQTT report payload"
            return
        with self._lock:
            self._last_message_at = datetime.now(timezone.utc)
            tray_count = len(self._trays)
        logger.info("MQTT report processed from topic=%s (cached trays=%d)", msg.topic, tray_count)
