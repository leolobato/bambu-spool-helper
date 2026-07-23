from __future__ import annotations

import tempfile
import unittest

from app.services.print_tracking.store import TrackingStore


class StoreTests(unittest.TestCase):
    def test_toggle_defaults_off_and_checkpoint_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TrackingStore(directory)
            self.assertFalse(store.enabled())
            store.set_enabled(True)
            self.assertTrue(store.enabled())
            store.save_model(b"3mf")
            store.save({"job_key": "one", "pending_delivery": {"spool": 3}})
            self.assertEqual(store.load()["job_key"], "one")
            store.clear_checkpoint()
            self.assertIsNone(store.load())
            self.assertTrue(store.enabled())
