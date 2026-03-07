"""MQTT client for activating AMS filament settings on a Bambu printer."""

from __future__ import annotations

import json
import logging
import ssl
import threading

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

MQTT_PORT = 8883
MQTT_USERNAME = "bblp"


class MQTTPrinterClient:
    def __init__(self, ip: str, access_code: str, serial: str) -> None:
        self._ip = ip.strip()
        self._access_code = access_code.strip()
        self._serial = serial.strip()

        self._client: mqtt.Client | None = None
        self._connected = False
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._ip and self._access_code and self._serial)

    def connect(self) -> None:
        if not self.configured:
            logger.warning("MQTT not configured; activation commands will be accepted but not sent")
            return

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        client.username_pw_set(MQTT_USERNAME, self._access_code)

        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls_context.check_hostname = False
        tls_context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(tls_context)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect

        try:
            client.connect(self._ip, MQTT_PORT, keepalive=60)
        except Exception:
            logger.exception("Failed to connect to printer MQTT broker at %s:%d", self._ip, MQTT_PORT)
            self._connected = False
            return

        client.loop_start()
        self._client = client

    def disconnect(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None
        self._connected = False

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
        publish_info = client.publish(topic, json.dumps(payload))
        if publish_info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False, f"MQTT publish failed (rc={publish_info.rc})"

        publish_info.wait_for_publish(timeout=3.0)
        if not publish_info.is_published():
            return False, "MQTT publish timeout"

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

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        code = int(reason_code)
        self._connected = code == 0
        if self._connected:
            logger.info("Connected to printer MQTT broker")
        else:
            logger.error("Failed to connect to printer MQTT broker (rc=%s)", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected = False
        logger.warning("Disconnected from printer MQTT broker (rc=%s)", reason_code)
