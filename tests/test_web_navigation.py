"""Tests for web app top-level navigation and trays shell loading."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


class TestWebNavigation(unittest.TestCase):
    def test_web_root_redirects_to_ams_trays_by_default(self):
        client = TestClient(app)

        resp = client.get("/web/?machine=GM020", follow_redirects=False)

        self.assertIn(resp.status_code, (302, 307))
        self.assertEqual(resp.headers["location"], "/web/trays?machine=GM020")

    def test_trays_page_renders_loading_shell_without_fetching_tray_context(self):
        with patch(
            "app.routers.web._machine_context",
            new=AsyncMock(return_value=([], "GM020")),
        ), patch(
            "app.routers.web._build_trays_context",
            new=AsyncMock(side_effect=AssertionError("tray context should load via HTMX")),
        ) as build_context:
            client = TestClient(app)
            resp = client.get("/web/trays?machine=GM020")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("AMS Trays", resp.text)
        self.assertIn("Loading AMS trays", resp.text)
        self.assertIn('hx-get="/web/trays/content?machine=GM020"', resp.text)
        self.assertIn('hx-trigger="load, every 8s"', resp.text)
        build_context.assert_not_awaited()

    def test_top_nav_orders_ams_trays_before_filaments_and_settings(self):
        with patch(
            "app.routers.web._machine_context",
            new=AsyncMock(return_value=([], "GM020")),
        ):
            client = TestClient(app)
            resp = client.get("/web/trays?machine=GM020")

        self.assertEqual(resp.status_code, 200)
        ams_pos = resp.text.index("AMS Trays")
        filaments_pos = resp.text.index("Filaments")
        settings_pos = resp.text.index("Settings")
        self.assertLess(ams_pos, filaments_pos)
        self.assertLess(filaments_pos, settings_pos)

    def test_trays_content_shows_gateway_offline_status(self):
        mqtt = MagicMock()
        mqtt.request_full_status = MagicMock()
        mqtt.get_tray_data = MagicMock(return_value={})
        mqtt.get_connection_status = MagicMock(return_value={
            "configured": True,
            "connected": False,
            "tray_count": 0,
            "last_error": "Gateway reports printer P01 is offline.",
            "last_message_at": None,
        })
        app.state.mqtt = mqtt

        settings = MagicMock()
        settings.printer_serial = "P01"
        app.state.settings = settings

        spoolman = MagicMock()
        spoolman.get_spools = AsyncMock(return_value=[])
        app.state.spoolman = spoolman

        orcaslicer = MagicMock()
        orcaslicer.get_profiles = AsyncMock(return_value=[])
        app.state.orcaslicer = orcaslicer

        with patch(
            "app.routers.web._machine_context",
            new=AsyncMock(return_value=([], "GM020")),
        ):
            client = TestClient(app)
            resp = client.get("/web/trays/content?machine=GM020")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Printer offline", resp.text)
        self.assertIn("Gateway reports printer P01 is offline.", resp.text)


if __name__ == "__main__":
    unittest.main()
