"""Tests for /web/tray/{N}/assign auto-bind behavior."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.models import TrayStatus


def _setup_state(mqtt_tray_uuid: str | None, *, bind_raises: Exception | None = None,
                 found_spool: object | None = None, found_profile: object | None = None):
    mqtt = MagicMock()
    mqtt.request_full_status = MagicMock()
    mqtt.get_tray_uuid = MagicMock(return_value=mqtt_tray_uuid)
    mqtt.activate_filament = MagicMock(return_value=(True, "Command sent to printer"))
    mqtt.get_tray_data = MagicMock(return_value={})
    mqtt.get_connection_status = MagicMock(return_value={
        "configured": True, "connected": True, "tray_count": 0,
        "last_error": None, "last_message_at": None,
    })
    app.state.mqtt = mqtt

    settings = MagicMock()
    settings.printer_serial = "TESTSERIAL"
    app.state.settings = settings

    spoolman = MagicMock()
    if bind_raises is not None:
        spoolman.bind_spool_to_tray_uuid = AsyncMock(side_effect=bind_raises)
    else:
        spoolman.bind_spool_to_tray_uuid = AsyncMock()
    spoolman.get_spools = AsyncMock(return_value=[found_spool] if found_spool else [])
    app.state.spoolman = spoolman

    orcaslicer = MagicMock()
    orcaslicer.get_profiles = AsyncMock(return_value=[found_profile] if found_profile else [])
    app.state.orcaslicer = orcaslicer
    return mqtt, spoolman


def _mock_spool(spool_id: int = 42) -> MagicMock:
    spool = MagicMock()
    spool.id = spool_id
    spool.display_name = f"Spool #{spool_id}"
    spool.remaining_weight = 500
    spool.archived = False
    spool.bambu_tray_uuid = None
    spool.filament = MagicMock(
        is_linked=True,
        ams_filament_id="GFA00",
        color_hex="FFFFFF",
        material="PLA",
    )
    return spool


def _mock_profile() -> MagicMock:
    profile = MagicMock()
    profile.filament_id = "GFA00"
    profile.filament_type = "PLA"
    profile.nozzle_temp_min = 200
    profile.nozzle_temp_max = 250
    profile.bed_temp_min = 55
    profile.setting_id = "GFSA00_01"
    profile.k = None
    profile.n = None
    return profile


def _mock_tray(tray_index: int = 0) -> TrayStatus:
    return TrayStatus(
        tray_index=tray_index,
        tray_uuid="",
        tray_weight=0,
        tag_uid="",
        cali_idx=-1,
    )


# Because /web/tray/N/assign depends on the spool model, ams_filament_id link,
# and OrcaSlicer profile lookup, the test patches the helpers it relies on
# rather than constructing the full chain.

class TestWebAssignBind(unittest.TestCase):
    def test_missing_tray_uuid_emits_error_toast_header(self):
        """User-facing assign failures stay HTTP 200 for HTMX swaps, but the
        message is delivered through HX-Trigger instead of an inline card banner.
        Error responses must not close the modal.
        """
        mqtt, spoolman = _setup_state(mqtt_tray_uuid=None)
        with patch("app.routers.web._load_spools", new=AsyncMock(return_value=([_mock_spool()], None))), \
             patch("app.routers.web._build_tray_statuses", return_value=[_mock_tray(0)]), \
             patch("app.routers.web._machine_context", new=AsyncMock(return_value=("", None))), \
             patch("app.routers.web._find_linked_profile", return_value=_mock_profile()), \
             patch("app.routers.web._resolve_link_filament_type", return_value="PLA"), \
             patch("app.routers.web._build_tray_bindings", return_value={}):
            client = TestClient(app)
            resp = client.post("/web/tray/0/assign", data={"machine": "", "spool_id": "42"})

        self.assertEqual(resp.status_code, 200)
        trigger = json.loads(resp.headers["HX-Trigger"])
        self.assertEqual(trigger["toast"]["level"], "error")
        self.assertIn("no UUID yet", trigger["toast"]["message"])
        self.assertNotIn("close-modal", trigger)
        self.assertNotIn("assign_error", resp.text)
        spoolman.bind_spool_to_tray_uuid.assert_not_awaited()
        mqtt.activate_filament.assert_not_called()

    def test_success_emits_toast_and_close_modal_trigger(self):
        spool = _mock_spool()
        profile = _mock_profile()
        mqtt, spoolman = _setup_state(
            mqtt_tray_uuid="uuid-0",
            found_spool=spool,
            found_profile=profile,
        )
        binding = {
            0: {
                "binding_key": "uuid-0",
                "bound_spool": spool,
                "suggested_spool": None,
            }
        }
        with patch("app.routers.web._load_spools", new=AsyncMock(return_value=([spool], None))), \
             patch("app.routers.web._build_tray_statuses", return_value=[_mock_tray(0)]), \
             patch("app.routers.web._machine_context", new=AsyncMock(return_value=("", None))), \
             patch("app.routers.web._find_linked_profile", return_value=profile), \
             patch("app.routers.web._resolve_link_filament_type", return_value="PLA"), \
             patch("app.routers.web._build_tray_bindings", return_value=binding):
            client = TestClient(app)
            resp = client.post("/web/tray/0/assign", data={"machine": "", "spool_id": "42"})

        self.assertEqual(resp.status_code, 200)
        trigger = json.loads(resp.headers["HX-Trigger"])
        self.assertEqual(trigger["toast"]["level"], "success")
        self.assertIn("Assigned Spool #42", trigger["toast"]["message"])
        self.assertTrue(trigger["close-modal"])
        spoolman.bind_spool_to_tray_uuid.assert_awaited_once_with(
            spool_id=42, tray_uuid="uuid-0",
        )
        mqtt.activate_filament.assert_called_once()
