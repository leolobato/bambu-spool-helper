"""Tests for MQTTPrinterClient.activate_filament MQTT payload composition.

Locks in that `setting_id` and the spool-tracking extras land in the published
payload only when provided, mirroring bambu-gateway's gating so the two paths
stay in sync.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt

from app.services.mqtt_printer import MQTTPrinterClient


def _make_client() -> MQTTPrinterClient:
    return MQTTPrinterClient(
        ip="10.0.1.157",
        access_code="abcdefgh",
        serial="030xxxxxxxxx403",
    )


def _publish_payload(publish_mock: MagicMock) -> dict:
    topic, body = publish_mock.call_args.args
    return json.loads(body)["print"]


class TestActivateFilamentPayload(unittest.TestCase):
    def _wired_client(self):
        client = _make_client()
        fake_paho = MagicMock()
        publish_info = MagicMock()
        publish_info.rc = mqtt.MQTT_ERR_SUCCESS
        publish_info.is_published.return_value = True
        fake_paho.publish.return_value = publish_info
        client._client = fake_paho
        return client, fake_paho

    def _bypass_connect(self, client):
        return patch.object(client, "ensure_connected", return_value=(True, "ready"))

    def test_minimal_call_omits_setting_id_and_extras(self):
        client, fake_paho = self._wired_client()
        with self._bypass_connect(client):
            ok, _ = client.activate_filament(
                tray=0,
                tray_info_idx="GFA00",
                color_hex="00FF00",
                nozzle_temp_min=200,
                nozzle_temp_max=250,
                filament_type="PLA",
            )
        self.assertTrue(ok)
        payload = _publish_payload(fake_paho.publish)
        self.assertEqual(payload["command"], "ams_filament_setting")
        self.assertEqual(payload["tray_info_idx"], "GFA00")
        self.assertEqual(payload["tray_color"], "00FF00FF")
        for absent in (
            "setting_id", "tag_uid", "bed_temp", "tray_weight",
            "remain", "k", "n", "tray_uuid", "cali_idx",
        ):
            self.assertNotIn(absent, payload, f"unexpected key {absent!r}")

    def test_setting_id_lands_in_payload_when_provided(self):
        client, fake_paho = self._wired_client()
        with self._bypass_connect(client):
            client.activate_filament(
                tray=0,
                tray_info_idx="GFA00",
                color_hex="00FF00",
                nozzle_temp_min=200,
                nozzle_temp_max=250,
                filament_type="PLA",
                setting_id="GFSA00_02",
            )
        payload = _publish_payload(fake_paho.publish)
        self.assertEqual(payload["setting_id"], "GFSA00_02")

    def test_full_extras_land_in_payload(self):
        client, fake_paho = self._wired_client()
        with self._bypass_connect(client):
            client.activate_filament(
                tray=2,
                tray_info_idx="GFA00",
                color_hex="ABCDEF",
                nozzle_temp_min=210,
                nozzle_temp_max=240,
                filament_type="PETG",
                setting_id="GFSA00_02",
                tag_uid="tag-xyz",
                bed_temp=70,
                tray_weight=950,
                remain=80,
                k=0.025,
                n=1.4,
                tray_uuid="uuid-xyz",
                cali_idx=3,
            )
        payload = _publish_payload(fake_paho.publish)
        self.assertEqual(payload["setting_id"], "GFSA00_02")
        self.assertEqual(payload["tag_uid"], "tag-xyz")
        self.assertEqual(payload["bed_temp"], 70)
        self.assertEqual(payload["tray_weight"], 950)
        self.assertEqual(payload["remain"], 80)
        self.assertEqual(payload["k"], 0.025)
        self.assertEqual(payload["n"], 1.4)
        self.assertEqual(payload["tray_uuid"], "uuid-xyz")
        self.assertEqual(payload["cali_idx"], 3)

    def test_external_tray_maps_to_255_254(self):
        client, fake_paho = self._wired_client()
        with self._bypass_connect(client):
            client.activate_filament(
                tray=4,
                tray_info_idx="GFA00",
                color_hex="FFFFFF",
                nozzle_temp_min=200,
                nozzle_temp_max=250,
                filament_type="PLA",
            )
        payload = _publish_payload(fake_paho.publish)
        self.assertEqual(payload["ams_id"], 255)
        self.assertEqual(payload["tray_id"], 254)


if __name__ == "__main__":
    unittest.main()
