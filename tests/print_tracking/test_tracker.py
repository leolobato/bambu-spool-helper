from __future__ import annotations

import tempfile
import unittest
import zipfile
from io import BytesIO

from app.services.print_tracking.reporter import SpoolmanUsageReporter
from app.services.print_tracking.store import TrackingStore
from app.services.print_tracking.tracker import PrintTracker


def model_bytes(layer_count: int = 4) -> bytes:
    gcode = ["M83", "M620 S0"]
    for layer in range(layer_count):
        gcode.extend([f"M73 L{layer}", "G1 E1"])
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "\n".join(gcode))
    return out.getvalue()


def snapshot(
    *,
    job="job-1",
    state="RUNNING",
    layer=0,
    total=4,
    mapping=None,
    active_tray=None,
):
    return {
        "schema_version": 1,
        "job_key": job,
        "job_name": "test.3mf",
        "state": state,
        "layer": {"current": layer, "total": total},
        "gcode_entry": "Metadata/plate_1.gcode",
        "ams_mapping": [0] if mapping is None else mapping,
        "active_tray": active_tray,
        "source": {"available": True},
    }


class FakeSpoolman:
    def __init__(self):
        self.used = {10: 0.0}
        self.calls: list[tuple[int, float]] = []
        self.fail_after_apply = False
        self.fail_before_apply = False
        self.uuid_to_spool = {"uuid-0": 10}

    async def lookup_by_tray_uuid(self, uuid):
        return self.uuid_to_spool.get(uuid)

    async def get_spool_used_length(self, spool_id):
        return self.used[spool_id]

    async def use_length(self, *, spool_id, mm):
        self.calls.append((spool_id, mm))
        if self.fail_before_apply:
            raise RuntimeError("Spoolman unavailable")
        self.used[spool_id] += mm
        if self.fail_after_apply:
            self.fail_after_apply = False
            raise RuntimeError("connection lost after send")


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TrackingStore(self.temp.name)
        self.spoolman = FakeSpoolman()
        self.trays = {0: "uuid-0"}

        async def tray_uuids():
            return dict(self.trays)

        async def fetch(_job):
            return model_bytes()

        self.reporter = SpoolmanUsageReporter(self.spoolman, tray_uuids)
        self.tracker = PrintTracker(
            store=self.store,
            reporter=self.reporter,
            fetch_3mf=fetch,
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_charges_only_after_layer_advance_and_catches_skips(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        self.assertEqual(self.spoolman.calls, [])
        await self.tracker.handle_snapshot(snapshot(layer=3))
        self.assertEqual(self.spoolman.calls, [(10, 1.0)] * 3)

    async def test_finish_flushes_but_failure_ignores_partial_and_future(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        await self.tracker.handle_snapshot(snapshot(state="FAILED", layer=2))
        self.assertEqual(self.spoolman.calls, [(10, 1.0)] * 2)

        self.spoolman.calls.clear()
        await self.tracker.handle_snapshot(
            snapshot(job="job-2", layer=0),
        )
        await self.tracker.handle_snapshot(
            snapshot(job="job-2", state="FINISH", layer=0),
        )
        self.assertEqual(self.spoolman.calls, [(10, 1.0)] * 4)

    async def test_replayed_finish_snapshot_never_charges_again(self):
        finished = snapshot(state="FINISH", layer=0)
        await self.tracker.handle_snapshot(snapshot(layer=0))
        await self.tracker.handle_snapshot(finished)
        self.assertEqual(self.spoolman.used[10], 4.0)

        await self.tracker.handle_snapshot(finished)
        await self.tracker.handle_snapshot(finished)
        self.assertEqual(self.spoolman.used[10], 4.0)

        async def unexpected_fetch(_job):
            raise AssertionError("terminal snapshot must not fetch its 3MF")

        restarted = PrintTracker(
            store=self.store,
            reporter=self.reporter,
            fetch_3mf=unexpected_fetch,
        )
        await restarted.handle_snapshot(finished)
        self.assertEqual(self.spoolman.used[10], 4.0)
        self.assertFalse(restarted.status["active"])

    async def test_untracked_terminal_snapshot_does_not_charge_tail(self):
        await self.tracker.handle_snapshot(
            snapshot(state="FINISH", layer=2),
        )
        self.assertEqual(self.spoolman.calls, [])
        self.assertFalse(self.tracker.status["active"])

    async def test_idle_after_running_is_treated_as_cancellation(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        await self.tracker.handle_snapshot(snapshot(layer=2))
        await self.tracker.handle_snapshot(snapshot(state="IDLE", layer=2))
        self.assertEqual(self.spoolman.calls, [(10, 1.0)] * 2)
        self.assertFalse(self.tracker.status["active"])

    async def test_restart_matching_checkpoint_catches_up_without_replay(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        await self.tracker.handle_snapshot(snapshot(layer=2))
        self.assertEqual(self.spoolman.used[10], 2.0)

        async def tray_uuids():
            return dict(self.trays)

        second = PrintTracker(
            store=self.store,
            reporter=SpoolmanUsageReporter(self.spoolman, tray_uuids),
            fetch_3mf=lambda _: None,
        )
        await second.handle_snapshot(snapshot(layer=3))
        self.assertEqual(self.spoolman.used[10], 3.0)

    async def test_no_checkpoint_begins_at_current_layer(self):
        await self.tracker.handle_snapshot(snapshot(layer=2, total=9))
        self.assertEqual(self.spoolman.calls, [])
        self.assertEqual(self.tracker.status["total_layers"], 9)
        await self.tracker.handle_snapshot(snapshot(layer=3))
        self.assertEqual(self.spoolman.calls, [(10, 1.0)])

    async def test_single_filament_without_mapping_uses_reported_active_tray(self):
        self.trays = {2: "uuid-2"}
        self.spoolman.uuid_to_spool["uuid-2"] = 10
        event = snapshot(mapping=[], active_tray=2)

        await self.tracker.handle_snapshot(event)
        await self.tracker.handle_snapshot(
            snapshot(layer=1, mapping=[], active_tray=2),
        )

        self.assertEqual(self.spoolman.calls, [(10, 1.0)])
        self.assertEqual(
            self.tracker.checkpoint["routes"]["0"]["tray"], 2,
        )
        self.assertIn(
            "reported active tray 2",
            " ".join(self.tracker.status["warnings"]),
        )

    async def test_multicolor_without_mapping_is_blocked(self):
        custom = BytesIO()
        with zipfile.ZipFile(custom, "w") as archive:
            archive.writestr(
                "Metadata/plate_1.gcode",
                "\n".join([
                    "M83",
                    "M73 L0",
                    "M620 S0",
                    "G1 E1",
                    "M620 S1",
                    "G1 E1",
                ]),
            )

        async def fetch(_job):
            return custom.getvalue()

        self.tracker.fetch_3mf = fetch
        await self.tracker.handle_snapshot(
            snapshot(mapping=[], active_tray=2),
        )
        await self.tracker.handle_snapshot(
            snapshot(layer=1, mapping=[], active_tray=2),
        )

        self.assertEqual(self.spoolman.calls, [])
        self.assertIn(
            "multicolor job",
            " ".join(self.tracker.status["warnings"]),
        )

    async def test_ambiguous_success_is_reconciled_without_duplicate(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        self.spoolman.fail_after_apply = True
        await self.tracker.handle_snapshot(snapshot(layer=1))
        self.assertEqual(self.spoolman.used[10], 1.0)
        self.assertIsNotNone(self.tracker.status["pending_delivery"])

        await self.tracker.handle_snapshot(snapshot(layer=2))
        self.assertEqual(self.spoolman.used[10], 2.0)
        self.assertEqual(len(self.spoolman.calls), 2)

    async def test_replacement_preserves_pending_delivery_across_restart(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        self.spoolman.fail_before_apply = True
        await self.tracker.handle_snapshot(snapshot(layer=1))
        self.assertEqual(self.store.load()["job_key"], "job-1")

        async def tray_uuids():
            return dict(self.trays)

        async def fetch(_job):
            return model_bytes()

        restarted = PrintTracker(
            store=self.store,
            reporter=SpoolmanUsageReporter(self.spoolman, tray_uuids),
            fetch_3mf=fetch,
        )
        replacement = snapshot(job="job-2", layer=0)
        await restarted.handle_snapshot(replacement)
        self.assertEqual(self.store.load()["job_key"], "job-1")
        self.assertEqual(restarted.status["job_key"], "job-1")
        self.assertIsNotNone(restarted.status["pending_delivery"])

        self.spoolman.fail_before_apply = False
        await restarted.handle_snapshot(replacement)
        self.assertEqual(self.spoolman.used[10], 1.0)
        self.assertEqual(self.store.load()["job_key"], "job-2")
        self.assertEqual(restarted.status["job_key"], "job-2")

    async def test_tray_or_mapping_change_blocks_affected_filament(self):
        await self.tracker.handle_snapshot(snapshot(layer=0))
        self.trays[0] = "different-uuid"
        await self.tracker.handle_snapshot(snapshot(layer=1))
        self.assertEqual(self.spoolman.calls, [])
        self.assertTrue(self.tracker.status["warnings"])

        # A changed route remains blocked even if the physical UUID comes back.
        self.trays[0] = "uuid-0"
        await self.tracker.handle_snapshot(snapshot(layer=2, mapping=[1]))
        self.assertEqual(self.spoolman.calls, [])
