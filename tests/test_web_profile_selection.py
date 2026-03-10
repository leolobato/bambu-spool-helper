import unittest

from app.models import FilamentProfileResponse
from app.routers.web import _find_profile_by_linked_id, _find_profile_by_setting_id


class WebProfileSelectionTests(unittest.TestCase):
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
