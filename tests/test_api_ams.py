"""Tests for the GET /ams endpoint and active-tray normalization."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.services.mqtt_printer import TrayData, normalize_active_tray


def _tray(
    tray_type: str = "",
    tray_color: str = "",
    tray_info_idx: str = "",
    tray_sub_brands: str = "",
    remain: int = -1,
) -> TrayData:
    td = TrayData()
    td.tray_type = tray_type
    td.tray_color = tray_color
    td.tray_info_idx = tray_info_idx
    td.tray_sub_brands = tray_sub_brands
    td.remain = remain
    return td


def _setup_state(
    tray_data: dict[int, TrayData],
    *,
    active_tray: int | None = None,
    uuids: dict[int, str] | None = None,
    spools: list | None = None,
    spoolman_raises: Exception | None = None,
):
    mqtt = MagicMock()
    mqtt.request_full_status = MagicMock()
    mqtt.get_tray_data = MagicMock(return_value=tray_data)
    mqtt.get_active_tray = MagicMock(return_value=active_tray)
    mqtt.get_tray_uuids = AsyncMock(return_value=uuids or {})
    mqtt.get_connection_status = MagicMock(return_value={
        "configured": True,
        "connected": True,
        "printer_state": "idle",
    })
    app.state.mqtt = mqtt

    spoolman = MagicMock()
    if spoolman_raises is not None:
        spoolman.get_spools = AsyncMock(side_effect=spoolman_raises)
    else:
        spoolman.get_spools = AsyncMock(return_value=spools or [])
    app.state.spoolman = spoolman
    return mqtt, spoolman


def _spool(spool_id: int, tray_uuid: str | None):
    return SimpleNamespace(id=spool_id, bambu_tray_uuid=tray_uuid)


class TestGetAms(unittest.TestCase):
    def test_full_snapshot_with_bindings(self):
        _setup_state(
            {
                0: _tray("PLA", "ECE9E2FF", "GFA00", "PLA Basic", remain=71),
                4: _tray("PLA", "0D9488FF", "GFA01", "PLA+", remain=21),
            },
            active_tray=0,
            uuids={0: "uuid-0", 4: "slot:SER:4"},
            spools=[_spool(14, "uuid-0"), _spool(31, "slot:SER:4"), _spool(9, None)],
        )
        resp = TestClient(app).get("/ams")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertTrue(body["connected"])
        self.assertEqual(body["printer_state"], "idle")
        self.assertEqual(body["active_tray"], 0)
        self.assertEqual(len(body["trays"]), 5)

        a1 = body["trays"][0]
        self.assertEqual(a1["label"], "A1")
        self.assertFalse(a1["empty"])
        self.assertEqual(a1["tray_color"], "#ECE9E2")
        self.assertEqual(a1["filament_id"], "GFA00")
        self.assertEqual(a1["remain"], 71)
        self.assertEqual(a1["spool_id"], 14)

        a2 = body["trays"][1]
        self.assertTrue(a2["empty"])
        self.assertEqual(a2["label"], "A2")
        self.assertIsNone(a2["spool_id"])

        external = body["trays"][4]
        self.assertEqual(external["label"], "External Spool")
        self.assertEqual(external["spool_id"], 31)

    def test_spoolman_down_still_returns_trays(self):
        _setup_state(
            {0: _tray("PLA", "FF6A13FF", "GFA00")},
            uuids={0: "uuid-0"},
            spoolman_raises=RuntimeError("spoolman down"),
        )
        resp = TestClient(app).get("/ams")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["trays"][0]["spool_id"])

    def test_ambiguous_binding_resolves_to_none(self):
        _setup_state(
            {0: _tray("PLA", "FF6A13FF", "GFA00")},
            uuids={0: "uuid-0"},
            spools=[_spool(1, "uuid-0"), _spool(2, "uuid-0")],
        )
        resp = TestClient(app).get("/ams")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["trays"][0]["spool_id"])

    def test_loaded_slot_with_placeholder_color(self):
        _setup_state({0: _tray("PLA", "", "GFA00")})
        resp = TestClient(app).get("/ams")
        tray = resp.json()["trays"][0]
        self.assertFalse(tray["empty"])
        self.assertEqual(tray["tray_color"], "")


class TestNormalizeActiveTray(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(normalize_active_tray("2"), 2)
        self.assertEqual(normalize_active_tray(0), 0)
        self.assertEqual(normalize_active_tray(254), 4)
        self.assertIsNone(normalize_active_tray(255))
        self.assertIsNone(normalize_active_tray("255"))
        self.assertIsNone(normalize_active_tray(None))
        self.assertIsNone(normalize_active_tray("garbage"))
        self.assertIsNone(normalize_active_tray(-1))


if __name__ == "__main__":
    unittest.main()
