"""Render tests for the AMS tray card states."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models import TrayStatus


class TestTrayCardRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.env = Environment(
            loader=FileSystemLoader("app/templates"),
            autoescape=select_autoescape(["html"]),
        )
        cls.template = cls.env.get_template("partials/tray_card.html")

    def _render(self, *, tray: TrayStatus, binding=None, matched_profile=None) -> str:
        return self.template.render(
            tray=tray,
            binding=binding,
            matched_profile=matched_profile,
            machine_id="TESTSERIAL",
        )

    def test_empty_state_hides_binding_action_and_profile_badge(self):
        html = self._render(tray=TrayStatus(tray_index=0))

        self.assertIn("Empty", html)
        self.assertNotIn("Bind spool", html)
        self.assertNotIn("Change spool", html)
        self.assertNotIn("Profile matched", html)
        self.assertNotIn("No profile match", html)

    def test_unbound_state_shows_bind_action_and_profile_badge(self):
        tray = TrayStatus(
            tray_index=1,
            tray_type="PLA",
            tray_color="DC2626FF",
            tray_info_idx="GFA00",
            tray_sub_brands="Bambu PLA Basic",
        )
        binding = {
            "binding_key": "slot:TESTSERIAL:1",
            "bound_spool": None,
            "suggested_spool": None,
        }

        html = self._render(
            tray=tray,
            binding=binding,
            matched_profile=SimpleNamespace(name="PLA Basic"),
        )

        self.assertIn("PLA", html)
        self.assertIn("Bambu PLA Basic", html)
        self.assertIn("Not bound to a Spoolman spool.", html)
        self.assertIn("Profile matched", html)
        self.assertIn("Bind spool", html)
        self.assertIn("/web/tray/1/bind", html)
        self.assertIn("machine=TESTSERIAL", html)

    def test_bound_state_shows_spool_summary_and_change_action(self):
        tray = TrayStatus(
            tray_index=2,
            tray_type="PLA",
            tray_color="0EA5E9FF",
            tray_info_idx="GFA00",
            tray_sub_brands="AMS Brand",
        )
        bound_spool = SimpleNamespace(
            id=142,
            display_name="Prusament PLA Galaxy",
            remaining_weight=480.0,
            filament=SimpleNamespace(material="PLA"),
        )
        binding = {
            "binding_key": "uuid-142",
            "bound_spool": bound_spool,
            "suggested_spool": None,
        }

        html = self._render(tray=tray, binding=binding)

        self.assertIn("#142", html)
        self.assertIn("480g left", html)
        self.assertIn("Prusament PLA Galaxy", html)
        self.assertIn("Change spool", html)
        self.assertNotIn("Bind spool", html)

    def test_bound_state_hides_weight_when_remaining_unknown(self):
        tray = TrayStatus(
            tray_index=3,
            tray_type="PETG",
            tray_color="333333FF",
            tray_info_idx="GFB00",
            tray_sub_brands="Vendor PETG",
        )
        bound_spool = SimpleNamespace(
            id=99,
            display_name="Vendor PETG (no weight tracked)",
            remaining_weight=None,
            filament=SimpleNamespace(material="PETG"),
        )
        binding = {
            "binding_key": "uuid-99",
            "bound_spool": bound_spool,
            "suggested_spool": None,
        }

        html = self._render(tray=tray, binding=binding)

        self.assertIn("#99", html)
        self.assertNotIn("g left", html)
        self.assertIn("Change spool", html)
