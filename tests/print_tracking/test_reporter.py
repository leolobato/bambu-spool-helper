from __future__ import annotations

import unittest

from app.services.print_tracking.reporter import (
    SpoolmanUsageReporter,
    helper_tray,
)


class FakeSpoolman:
    def __init__(self):
        self.bindings = {
            "slot:SERIAL:0": 1,
            "rfid-2": 2,
            "external": 3,
        }
        self.used = {1: 0.0, 2: 0.0, 3: 0.0}
        self.calls = []

    async def lookup_by_tray_uuid(self, uuid):
        return self.bindings.get(uuid)

    async def get_spool_used_length(self, spool_id):
        return self.used[spool_id]

    async def use_length(self, *, spool_id, mm):
        self.used[spool_id] += mm
        self.calls.append((spool_id, mm))


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_synthetic_and_external_routes_batch_per_spool(self):
        trays = {
            0: "slot:SERIAL:0",
            2: "rfid-2",
            4: "external",
        }

        async def get_trays():
            return dict(trays)

        spoolman = FakeSpoolman()
        reporter = SpoolmanUsageReporter(spoolman, get_trays)
        routes, warnings = await reporter.snapshot_routes(
            {0, 2, 7}, [0, 99, 2],
        )
        # Sparse filament 7 has no mapping and therefore uses helper tray 4.
        self.assertEqual(routes["0"]["spool_id"], 1)
        self.assertEqual(routes["2"]["spool_id"], 2)
        self.assertEqual(routes["7"]["spool_id"], 3)
        self.assertEqual(warnings, [])

        checkpoint = {
            "routes": routes,
            "deliveries": {},
            "spool_totals_mm": {},
            "warnings": [],
        }
        ok, error = await reporter.deliver_layer(
            layer=4,
            usage={0: 2.0, 2: 3.0, 7: 4.0},
            checkpoint=checkpoint,
            save=lambda: None,
        )
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertCountEqual(
            spoolman.calls, [(1, 2.0), (2, 3.0), (3, 4.0)],
        )

    def test_external_printer_values_normalize_to_helper_tray_four(self):
        for raw in (-1, 254, 255):
            self.assertEqual(helper_tray(0, [raw]), 4)

    async def test_route_warnings_use_operator_facing_ams_labels(self):
        async def no_trays():
            return {}

        reporter = SpoolmanUsageReporter(FakeSpoolman(), no_trays)
        _, warnings = await reporter.snapshot_routes({0}, [2])

        self.assertEqual(
            warnings,
            ["Filament 0: A3 has no binding UUID"],
        )

    async def test_tray_refresh_failure_pauses_delivery_without_blocking_route(self):
        async def unavailable_trays():
            raise RuntimeError("gateway offline")

        reporter = SpoolmanUsageReporter(FakeSpoolman(), unavailable_trays)
        route = {
            "tray": 0,
            "tray_uuid": "slot:SERIAL:0",
            "spool_id": 1,
            "blocked": False,
            "warning": None,
        }
        checkpoint = {
            "routes": {"0": route},
            "deliveries": {},
            "spool_totals_mm": {},
            "warnings": [],
        }

        ok, error = await reporter.deliver_layer(
            layer=0,
            usage={0: 1.0},
            checkpoint=checkpoint,
            save=lambda: None,
        )

        self.assertFalse(ok)
        self.assertIn("gateway offline", error)
        self.assertFalse(route["blocked"])
