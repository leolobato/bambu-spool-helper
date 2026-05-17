"""Tests for /activate auto-bind behavior."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app


def _setup_state(mqtt_tray_uuid: str | None, *, bind_raises: Exception | None = None):
    """Stub app.state with the minimum needed for /activate."""
    mqtt = MagicMock()
    mqtt.request_full_status = MagicMock()
    mqtt.get_tray_uuid = MagicMock(return_value=mqtt_tray_uuid)
    mqtt.activate_filament = MagicMock(return_value=(True, "Command sent to printer"))
    app.state.mqtt = mqtt

    spoolman = MagicMock()
    if bind_raises is not None:
        spoolman.bind_spool_to_tray_uuid = AsyncMock(side_effect=bind_raises)
    else:
        spoolman.bind_spool_to_tray_uuid = AsyncMock()
    app.state.spoolman = spoolman

    # The endpoint also touches orcaslicer.find_profile in some branches
    orcaslicer = MagicMock()
    orcaslicer.find_profile = AsyncMock(return_value=None)
    app.state.orcaslicer = orcaslicer

    app.state.recent_activations = []
    return mqtt, spoolman


def _activate_body(**overrides):
    body = {
        "filament_id": "GFA00",
        "filament_type": "PLA",
        "color_hex": "FFFFFF",
        "tray": 0,
    }
    body.update(overrides)
    return body


class TestActivateLegacy(unittest.TestCase):
    def test_no_spool_id_does_not_call_bind(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid="uuid-0")
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body())
        self.assertEqual(resp.status_code, 200)
        spoolman.bind_spool_to_tray_uuid.assert_not_awaited()
        mqtt.activate_filament.assert_called_once()


class TestActivateWithSpoolId(unittest.TestCase):
    def test_happy_path_binds_then_activates(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid="uuid-0")
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body(spool_id=42))
        self.assertEqual(resp.status_code, 200)
        spoolman.bind_spool_to_tray_uuid.assert_awaited_once_with(
            spool_id=42, tray_uuid="uuid-0",
        )
        mqtt.activate_filament.assert_called_once()
        # bind must come before activate
        self.assertTrue(
            spoolman.bind_spool_to_tray_uuid.await_args is not None
        )

    def test_409_when_tray_uuid_missing(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid=None)
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body(spool_id=42))
        self.assertEqual(resp.status_code, 409)
        spoolman.bind_spool_to_tray_uuid.assert_not_awaited()
        mqtt.activate_filament.assert_not_called()

    def test_502_when_bind_fails(self):
        import httpx
        err = httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("PATCH", "http://s/x"),
            response=httpx.Response(500, request=httpx.Request("PATCH", "http://s/x")),
        )
        mqtt, spoolman = _setup_state(mqtt_tray_uuid="uuid-0", bind_raises=err)
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body(spool_id=42))
        self.assertEqual(resp.status_code, 502)
        mqtt.activate_filament.assert_not_called()
