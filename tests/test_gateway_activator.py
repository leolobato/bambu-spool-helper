"""Tests for GatewayActivator HTTP routing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx


def _resp(status: int, json: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=json if json is not None else {},
        request=httpx.Request("GET", "http://gateway:8080/"),
    )

from app.services.gateway_activator import GatewayActivator


def _activator() -> GatewayActivator:
    a = GatewayActivator(gateway_url="http://gateway:8080", printer_serial="P01")
    a._client = MagicMock()
    return a


class TestActivateFilament(unittest.TestCase):
    def test_unconfigured_returns_skip(self):
        a = GatewayActivator(gateway_url="", printer_serial="P01")
        ok, msg = a.activate_filament(0, "GFA00", "FFFFFF", 200, 250, "PLA", setting_id="X")
        self.assertTrue(ok)
        self.assertIn("skipped", msg.lower())

    def test_missing_setting_id_returns_error(self):
        a = _activator()
        ok, msg = a.activate_filament(0, "GFA00", "FFFFFF", 200, 250, "PLA")
        self.assertFalse(ok)
        self.assertIn("setting_id", msg)

    def test_invalid_tray_returns_error(self):
        a = _activator()
        ok, msg = a.activate_filament(7, "GFA00", "FFFFFF", 200, 250, "PLA", setting_id="X")
        self.assertFalse(ok)
        self.assertIn("Tray", msg)

    def test_posts_to_correct_url_and_body(self):
        a = _activator()
        a._client.post.return_value = _resp(200, {"printer_id": "P01", "command": "ok"})
        ok, msg = a.activate_filament(
            tray=2,
            tray_info_idx="GFA00",
            color_hex="00FF00",
            nozzle_temp_min=200,
            nozzle_temp_max=250,
            filament_type="PLA",
            setting_id="GFSA00_02",
            tag_uid="abc",
            k=0.025,
            n=1.4,
            cali_idx=-1,
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "Command sent via gateway")
        a._client.post.assert_called_once()
        url = a._client.post.call_args.args[0]
        body = a._client.post.call_args.kwargs["json"]
        self.assertEqual(
            url,
            "http://gateway:8080/api/printers/P01/ams/0/tray/2/filament",
        )
        self.assertEqual(body["setting_id"], "GFSA00_02")
        self.assertEqual(body["tray_color"], "00FF00FF")
        self.assertEqual(body["tag_uid"], "abc")
        self.assertEqual(body["k"], 0.025)
        self.assertEqual(body["n"], 1.4)
        self.assertEqual(body["cali_idx"], -1)
        # Optionals not provided must be absent
        for absent in ("bed_temp", "tray_weight", "remain", "tray_uuid"):
            self.assertNotIn(absent, body, f"unexpected key {absent}")

    def test_external_tray_maps_to_255_254(self):
        a = _activator()
        a._client.post.return_value = _resp(200, {})
        a.activate_filament(4, "GFA00", "FFFFFF", 200, 250, "PLA", setting_id="X")
        url = a._client.post.call_args.args[0]
        self.assertIn("/ams/255/tray/254/filament", url)

    def test_gateway_4xx_returns_error_with_detail(self):
        a = _activator()
        a._client.post.return_value = _resp(
            404, {"detail": "Filament profile 'X' not found"}
        )
        ok, msg = a.activate_filament(0, "GFA00", "FFFFFF", 200, 250, "PLA", setting_id="X")
        self.assertFalse(ok)
        self.assertIn("404", msg)
        self.assertIn("not found", msg)

    def test_network_error_surfaces_in_message_and_status(self):
        a = _activator()
        a._client.post.side_effect = httpx.ConnectError("connection refused")
        ok, msg = a.activate_filament(0, "GFA00", "FFFFFF", 200, 250, "PLA", setting_id="X")
        self.assertFalse(ok)
        self.assertIn("Gateway request failed", msg)
        self.assertEqual(a.get_connection_status()["last_error"], msg)


class TestTrayDataFetch(unittest.TestCase):
    def test_get_tray_data_maps_ams_response(self):
        a = _activator()
        a._client.get.return_value = _resp(
            200,
            {
                "printer_id": "P01",
                "trays": [
                    {
                        "ams_id": 0,
                        "tray_id": 0,
                        "tray_type": "PLA",
                        "tray_color": "FF0000FF",
                        "filament_id": "GFA00",
                        "tag_uid": "tag-0",
                        "k": 0.02,
                        "n": 1.4,
                        "tray_uuid": "uuid-0",
                        "cali_idx": 2,
                        "remain": 80,
                    },
                    {
                        "ams_id": 0,
                        "tray_id": 1,
                        "tray_type": "PETG",
                        "filament_id": "GFB00",
                    },
                ],
                "vt_tray": {
                    "ams_id": 255,
                    "tray_id": 254,
                    "tray_type": "PLA",
                    "filament_id": "GFA01",
                },
            },
        )
        data = a.get_tray_data()
        self.assertIn(0, data)
        self.assertIn(1, data)
        self.assertIn(4, data)
        self.assertEqual(data[0].tray_info_idx, "GFA00")
        self.assertEqual(data[0].tag_uid, "tag-0")
        self.assertEqual(data[0].k, 0.02)
        self.assertEqual(data[0].cali_idx, 2)
        self.assertEqual(data[0].remain, 80)
        self.assertEqual(data[1].tray_type, "PETG")
        self.assertEqual(data[4].tray_info_idx, "GFA01")

    def test_get_tray_data_returns_empty_when_unconfigured(self):
        a = GatewayActivator(gateway_url="", printer_serial="")
        self.assertEqual(a.get_tray_data(), {})

    def test_gateway_unreachable_does_not_raise(self):
        a = _activator()
        a._client.get.side_effect = httpx.ConnectError("boom")
        data = a.get_tray_data()
        self.assertEqual(data, {})
        status = a.get_connection_status()
        self.assertFalse(status["connected"])
        self.assertIn("Gateway unreachable", status["last_error"])

    def test_get_tray_data_retries_after_transient_error(self):
        """A failed first refresh must not freeze the cache forever — the
        next get_tray_data() call should re-attempt.
        """
        a = _activator()
        a._client.get.side_effect = [
            httpx.ConnectError("transient"),
            _resp(200, {
                "printer_id": "P01",
                "trays": [{"ams_id": 0, "tray_id": 0, "filament_id": "GFA00", "tray_type": "PLA"}],
            }),
        ]
        first = a.get_tray_data()
        self.assertEqual(first, {})
        second = a.get_tray_data()
        self.assertIn(0, second)
        self.assertEqual(second[0].tray_info_idx, "GFA00")


class TestStatus(unittest.TestCase):
    def test_unconfigured_status(self):
        a = GatewayActivator(gateway_url="", printer_serial="")
        s = a.get_connection_status()
        self.assertFalse(s["configured"])
        self.assertFalse(s["connected"])
        self.assertEqual(s["tray_count"], 0)

    def test_request_full_status_uses_gateway_printer_online_state(self):
        a = _activator()
        a._client.get.return_value = _resp(
            200,
            {
                "printers": [
                    {
                        "id": "P01",
                        "online": False,
                        "state": "offline",
                    }
                ]
            },
        )

        a.request_full_status()

        s = a.get_connection_status()
        self.assertTrue(s["configured"])
        self.assertFalse(s["connected"])
        self.assertEqual(s["tray_count"], 0)
        self.assertIn("offline", s["last_error"].lower())
        a._client.get.assert_called_once_with("http://gateway:8080/api/printers")


class TestGetTrayUuids(unittest.IsolatedAsyncioTestCase):
    async def test_returns_tray_id_to_uuid_dict(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        fake_trays = {
            0: type("T", (), {"tray_uuid": "uuid-0"})(),
            1: type("T", (), {"tray_uuid": "uuid-1"})(),
            255: type("T", (), {"tray_uuid": "uuid-ext"})(),
        }
        with patch.object(a, "get_tray_data", return_value=fake_trays):
            result = await a.get_tray_uuids()
        self.assertEqual(result, {0: "uuid-0", 1: "uuid-1", 255: "uuid-ext"})


class TestGetTrayUuidGateway(unittest.TestCase):
    def test_returns_uuid_for_known_tray(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {0: type("T", (), {"tray_uuid": "uuid-0"})()}
        self.assertEqual(a.get_tray_uuid(0), "uuid-0")

    def test_returns_none_for_unknown_tray(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {}
        self.assertIsNone(a.get_tray_uuid(99))

    def test_returns_none_when_uuid_empty(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {0: type("T", (), {"tray_uuid": ""})()}
        self.assertIsNone(a.get_tray_uuid(0))

    def test_returns_none_for_all_zeros_placeholder_when_slot_empty(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {0: type("T", (), {"tray_uuid": "00000000000000000000000000000000", "tray_info_idx": ""})()}
        self.assertIsNone(a.get_tray_uuid(0))

    def test_returns_synthetic_when_placeholder_but_filament_loaded(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {0: type("T", (), {"tray_uuid": "00000000000000000000000000000000", "tray_info_idx": "Pfd5d97d"})()}
        self.assertEqual(a.get_tray_uuid(0), "slot:P01:0")


class TestGetTrayUuidsFiltersPlaceholders(unittest.IsolatedAsyncioTestCase):
    async def test_get_tray_uuids_skips_placeholder(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        fake_trays = {
            0: type("T", (), {"tray_uuid": "real-uuid"})(),
            1: type("T", (), {"tray_uuid": "00000000000000000000000000000000"})(),
            2: type("T", (), {"tray_uuid": ""})(),
        }
        with patch.object(a, "get_tray_data", return_value=fake_trays):
            result = await a.get_tray_uuids()
        self.assertEqual(result, {0: "real-uuid"})

    async def test_get_tray_uuids_returns_synthetic_for_loaded_non_rfid_slot(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        fake_trays = {
            0: type("T", (), {"tray_uuid": "real-uuid", "tray_info_idx": "GFA00"})(),
            1: type("T", (), {"tray_uuid": "00000000000000000000000000000000", "tray_info_idx": "GFA01"})(),
            2: type("T", (), {"tray_uuid": "", "tray_info_idx": "GFA02"})(),
            3: type("T", (), {"tray_uuid": "00000000000000000000000000000000", "tray_info_idx": ""})(),
        }
        with patch.object(a, "get_tray_data", return_value=fake_trays):
            result = await a.get_tray_uuids()
        self.assertEqual(
            result,
            {
                0: "real-uuid",
                1: "slot:P01:1",
                2: "slot:P01:2",
            },
        )


if __name__ == "__main__":
    unittest.main()
