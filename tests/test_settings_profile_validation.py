import unittest

from app.models import FilamentProfileResponse, SpoolmanFilament, SpoolmanVendor
from app.routers.web import _build_linked_profile_validation


class SettingsProfileValidationTests(unittest.TestCase):
    def test_validation_counts_only_linked_filaments_with_matching_profiles(self) -> None:
        profiles = [
            FilamentProfileResponse(
                name="PETG Match",
                filament_id="PETG-001",
                setting_id="petg-match",
                filament_type="PETG",
                nozzle_temp_min=0,
                nozzle_temp_max=0,
                bed_temp_min=0,
                bed_temp_max=0,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
            ),
        ]
        filaments = [
            SpoolmanFilament(
                id=1,
                name="Good PETG",
                material="PETG",
                vendor=SpoolmanVendor(name="eSUN"),
                extra={
                    "ams_filament_id": '"PETG-001"',
                    "ams_filament_type": '"PETG"',
                },
            ),
            SpoolmanFilament(
                id=2,
                name="PLA Missing",
                material="PLA",
                vendor=SpoolmanVendor(name="eSUN"),
                extra={
                    "ams_filament_id": '"PLA-404"',
                    "ams_filament_type": '"PLA"',
                },
            ),
            SpoolmanFilament(
                id=3,
                name="Unlinked",
                material="PLA",
                vendor=SpoolmanVendor(name="eSUN"),
                extra={},
            ),
        ]

        validation = _build_linked_profile_validation(filaments, profiles)

        self.assertEqual(validation["linked_count"], 2)
        self.assertEqual(validation["matched_count"], 1)
        self.assertEqual(validation["missing_count"], 1)
        self.assertEqual(validation["matched"][0]["filament"].id, 1)
        self.assertEqual(validation["matched"][0]["profile"].setting_id, "petg-match")
        self.assertEqual(validation["missing"][0]["filament"].id, 2)
        self.assertEqual(validation["missing"][0]["linked_filament_id"], "PLA-404")


if __name__ == "__main__":
    unittest.main()
