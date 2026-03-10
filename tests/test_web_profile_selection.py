import unittest

from app.models import FilamentProfileResponse
from app.routers.web import (
    _extract_payload_filament_type,
    _find_profile_by_linked_id,
    _find_profile_by_setting_id,
    _normalize_valid_filament_type,
    _set_payload_filament_type,
)


class WebProfileSelectionTests(unittest.TestCase):
    def test_normalize_valid_filament_type_accepts_known_values(self) -> None:
        self.assertEqual(_normalize_valid_filament_type(" petg "), "PETG")
        self.assertEqual(_normalize_valid_filament_type(""), "")
        self.assertEqual(_normalize_valid_filament_type("PETG-HF"), "")

    def test_set_payload_filament_type_updates_resolved_payload(self) -> None:
        payload = {"name": "Resolved profile", "filament_type": [""]}

        _set_payload_filament_type(payload, "petg")

        self.assertEqual(_extract_payload_filament_type(payload), "PETG")

    def test_find_profile_by_setting_id_disambiguates_shared_filament_id(self) -> None:
        profiles = [
            FilamentProfileResponse(
                name="Broken PLA Profile",
                filament_id="GFSA00",
                setting_id="broken-setting",
                filament_type="",
                nozzle_temp_min=0,
                nozzle_temp_max=0,
                bed_temp_min=0,
                bed_temp_max=0,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
                print_speed_min=0,
                print_speed_max=0,
            ),
            FilamentProfileResponse(
                name="Correct PLA Profile",
                filament_id="GFSA00",
                setting_id="correct-setting",
                filament_type="PLA",
                nozzle_temp_min=0,
                nozzle_temp_max=0,
                bed_temp_min=0,
                bed_temp_max=0,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
                print_speed_min=0,
                print_speed_max=0,
            ),
        ]

        matched_by_filament_id = _find_profile_by_linked_id(profiles, "GFSA00")
        matched_by_setting_id = _find_profile_by_setting_id(profiles, "correct-setting")

        self.assertIsNotNone(matched_by_filament_id)
        self.assertIsNotNone(matched_by_setting_id)
        self.assertEqual(matched_by_filament_id.setting_id, "broken-setting")
        self.assertEqual(matched_by_setting_id.setting_id, "correct-setting")
        self.assertEqual(matched_by_setting_id.filament_type, "PLA")


if __name__ == "__main__":
    unittest.main()
