"""Tests for MQTTPrinterClient.get_tray_uuid."""

from __future__ import annotations

import unittest

from app.services.mqtt_printer import MQTTPrinterClient, TrayData


def _make_client() -> MQTTPrinterClient:
    return MQTTPrinterClient(ip="127.0.0.1", access_code="x", serial="S01")


class TestGetTrayUuidMqtt(unittest.TestCase):
    def test_returns_uuid_for_known_tray(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = "uuid-0"
        c._trays[0] = td
        self.assertEqual(c.get_tray_uuid(0), "uuid-0")

    def test_returns_none_for_unknown_tray(self):
        c = _make_client()
        self.assertIsNone(c.get_tray_uuid(99))

    def test_returns_none_when_uuid_empty(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = ""
        c._trays[0] = td
        self.assertIsNone(c.get_tray_uuid(0))

    def test_returns_none_for_all_zeros_placeholder(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = "00000000000000000000000000000000"
        c._trays[0] = td
        self.assertIsNone(c.get_tray_uuid(0))

    def test_returns_none_for_zeros_with_dashes(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = "00000000-0000-0000-0000-000000000000"
        c._trays[0] = td
        self.assertIsNone(c.get_tray_uuid(0))

    def test_synthetic_uuid_when_placeholder_but_filament_loaded(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = "00000000000000000000000000000000"
        td.tray_info_idx = "Pfd5d97d"
        c._trays[0] = td
        self.assertEqual(c.get_tray_uuid(0), "slot:S01:0")

    def test_returns_none_when_placeholder_and_empty_slot(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = "00000000000000000000000000000000"
        td.tray_info_idx = ""
        c._trays[0] = td
        self.assertIsNone(c.get_tray_uuid(0))
