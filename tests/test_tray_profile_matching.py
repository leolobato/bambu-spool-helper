import unittest

from app.models import FilamentProfileResponse, SpoolmanFilament, TrayStatus
from app.routers.web import _build_tray_profile_matches, _find_linked_profile, _resolve_link_filament_type


class TrayProfileMatchingTests(unittest.TestCase):
    def test_find_linked_profile_prefers_saved_setting_id(self) -> None:
        profiles = [
            FilamentProfileResponse(
                name="Wrong PLA Profile",
                filament_id="GFSA00",
                setting_id="wrong-setting",
                filament_type="PLA",
                nozzle_temp_min=190,
                nozzle_temp_max=220,
                bed_temp_min=55,
                bed_temp_max=55,
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
                nozzle_temp_min=220,
                nozzle_temp_max=230,
                bed_temp_min=65,
                bed_temp_max=65,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
                print_speed_min=0,
                print_speed_max=0,
            ),
        ]
        filament = SpoolmanFilament(
            id=1,
            name="Test Filament",
            material="PLA",
            extra={
                "ams_filament_id": '"GFSA00"',
                "ams_filament_type": '"PLA"',
                "ams_setting_id": '"correct-setting"',
            },
        )

        matched = _find_linked_profile(profiles, filament)

        self.assertIsNotNone(matched)
        self.assertEqual(matched.setting_id, "correct-setting")

    def test_build_tray_profile_matches_uses_tray_metadata_to_disambiguate_duplicates(self) -> None:
        profiles = [
            FilamentProfileResponse(
                name="Generic PLA",
                filament_id="GFSA00",
                setting_id="generic-setting",
                filament_type="PLA",
                nozzle_temp_min=190,
                nozzle_temp_max=220,
                bed_temp_min=55,
                bed_temp_max=55,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
                print_speed_min=0,
                print_speed_max=0,
                k=0.02,
                n=1.05,
            ),
            FilamentProfileResponse(
                name="High Temp PLA",
                filament_id="GFSA00",
                setting_id="high-temp-setting",
                filament_type="PLA",
                nozzle_temp_min=220,
                nozzle_temp_max=230,
                bed_temp_min=65,
                bed_temp_max=65,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
                print_speed_min=0,
                print_speed_max=0,
                k=0.03,
                n=1.11,
            ),
        ]
        trays = [
            TrayStatus(
                tray_index=0,
                tray_type="PLA",
                tray_info_idx="GFSA00",
                nozzle_temp_min=220,
                nozzle_temp_max=230,
                bed_temp=65,
                k=0.03,
                n=1.11,
            )
        ]

        matches = _build_tray_profile_matches(trays, profiles)

        self.assertIn(0, matches)
        self.assertEqual(matches[0].setting_id, "high-temp-setting")

    def test_resolve_link_filament_type_uses_saved_filament_type_when_profile_is_blank(self) -> None:
        profile = FilamentProfileResponse(
            name="Opaque profile name",
            filament_id="CUSTOM01",
            setting_id="custom-setting",
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
        )
        filament = SpoolmanFilament(
            id=2,
            name="Custom material",
            material="",
            extra={
                "ams_filament_id": '"CUSTOM01"',
                "ams_filament_type": '"PCTG"',
                "ams_setting_id": '"custom-setting"',
            },
        )

        resolved = _resolve_link_filament_type(profile, filament)

        self.assertEqual(resolved, "PCTG")


if __name__ == "__main__":
    unittest.main()
