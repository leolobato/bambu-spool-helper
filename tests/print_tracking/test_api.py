from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.api import router


class TrackingStub:
    def __init__(self):
        self.enabled = False
        self.set_enabled = AsyncMock(side_effect=self._set)

    async def _set(self, enabled):
        self.enabled = enabled

    @property
    def status(self):
        return {
            "available": True,
            "enabled": self.enabled,
            "connected": self.enabled,
            "active": False,
            "job_key": None,
            "current_layer": None,
            "spool_totals_mm": {},
            "last_event": None,
            "last_error": None,
        }


class OrcaStub:
    async def get_profiles(self, _machine):
        return [object(), object()]


class ApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        app.state.settings = SimpleNamespace(
            port=9817, default_machine_profile_id="GM020",
        )
        app.state.orcaslicer = OrcaStub()
        app.state.print_tracking = TrackingStub()
        self.app = app
        self.client = TestClient(app)

    def test_status_exposes_tracking_block(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["profiles_loaded"], 2)
        self.assertFalse(response.json()["print_tracking"]["enabled"])

    def test_put_updates_setting(self):
        response = self.client.put(
            "/api/print-tracking/settings", json={"enabled": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        self.app.state.print_tracking.set_enabled.assert_awaited_once_with(True)
