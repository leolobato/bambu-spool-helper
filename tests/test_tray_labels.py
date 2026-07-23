from __future__ import annotations

import unittest

from app.models import TrayStatus
from app.tray_labels import migrate_legacy_tray_references, tray_label


class TrayLabelTests(unittest.TestCase):
    def test_ams_a_slots_are_one_based(self):
        self.assertEqual(
            [tray_label(index) for index in range(4)],
            ["A1", "A2", "A3", "A4"],
        )

    def test_external_spool_uses_a_distinct_name(self):
        self.assertEqual(tray_label(4), "External Spool")

    def test_tray_status_uses_the_shared_operator_label(self):
        self.assertEqual(TrayStatus(tray_index=2).label, "A3")

    def test_persisted_numeric_warning_is_migrated(self):
        self.assertEqual(
            migrate_legacy_tray_references(
                "Filament 0: tray 2 has no binding UUID",
            ),
            "Filament 0: A3 has no binding UUID",
        )


if __name__ == "__main__":
    unittest.main()
