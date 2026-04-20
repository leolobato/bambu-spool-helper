import unittest

from app.models import FilamentProfileResponse, SpoolmanFilament, SpoolmanSpool, TrayStatus
from app.routers.web import (
    _apply_assignment_to_tray_view,
    _build_tray_profile_matches,
    _filter_filaments,
    _find_linked_profile,
    _resolve_link_filament_type,
)


class TrayProfileMatchingTests(unittest.TestCase):
    def test_find_linked_profile_prefers_profile_matching_filament_material(self) -> None:
        profiles = [
            FilamentProfileResponse(
                name="Wrong PETG Profile",
                filament_id="GFSA00",
                setting_id="wrong-setting",
                filament_type="PETG",
                nozzle_temp_min=190,
                nozzle_temp_max=220,
                bed_temp_min=55,
                bed_temp_max=55,
                drying_temp_min=0,
                drying_temp_max=0,
                drying_time=0,
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
            ),
        ]
        filament = SpoolmanFilament(
            id=1,
            name="Test Filament",
            material="PLA",
            extra={
                "ams_filament_id": '"GFSA00"',
                "ams_filament_type": '"PLA"',
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
                drying_time=0,                k=0.02,
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
                drying_time=0,                k=0.03,
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
            )
        filament = SpoolmanFilament(
            id=2,
            name="Custom material",
            material="",
            extra={
                "ams_filament_id": '"CUSTOM01"',
                "ams_filament_type": '"PCTG"',
            },
        )

        resolved = _resolve_link_filament_type(profile, filament)

        self.assertEqual(resolved, "PCTG")

    def test_resolve_link_filament_type_skips_invalid_spool_material_and_uses_profile_name_hint(self) -> None:
        profile = FilamentProfileResponse(
            name="SUNLU PLA+ @BBL A1M",
            filament_id="OGFSNL03",
            setting_id="GFSNLS03_07",
            filament_type="",
            nozzle_temp_min=0,
            nozzle_temp_max=0,
            bed_temp_min=0,
            bed_temp_max=0,
            drying_temp_min=0,
            drying_temp_max=0,
            drying_time=0,
            )
        filament = SpoolmanFilament(
            id=3,
            name="White",
            material="PLA+",
            extra={
                "ams_filament_id": '"OGFSNL03"',
                "ams_filament_type": '""',
            },
        )

        resolved = _resolve_link_filament_type(profile, filament)

        self.assertEqual(resolved, "PLA")

    def test_filter_filaments_matches_linked_filament_id(self) -> None:
        filaments = [
            SpoolmanFilament(
                id=4,
                name="Black PLA",
                material="PLA",
                extra={
                    "ams_filament_id": '"GFSNL04"',
                    "ams_filament_type": '"PLA"',
                },
            ),
            SpoolmanFilament(
                id=5,
                name="White PETG",
                material="PETG",
                extra={
                    "ams_filament_id": '"GFSPETG01"',
                    "ams_filament_type": '"PETG"',
                },
            ),
        ]

        filtered = _filter_filaments(filaments, "all", "gfsnl04")

        self.assertEqual([filament.id for filament in filtered], [4])

    def test_apply_assignment_to_tray_view_updates_filament_id_immediately(self) -> None:
        tray = TrayStatus(
            tray_index=0,
            tray_type="PLA",
            tray_info_idx="OLD01",
            tray_color="FFFFFF",
            nozzle_temp_min=190,
            nozzle_temp_max=220,
            bed_temp=55,
        )
        spool = SpoolmanSpool(
            id=7,
            filament=SpoolmanFilament(
                id=11,
                name="Black PLA",
                material="PLA",
                color_hex="000000",
                extra={
                    "ams_filament_id": '"GFSNL04"',
                    "ams_filament_type": '"PLA"',
                },
            ),
        )
        profile = FilamentProfileResponse(
            name="SUNLU PLA",
            filament_id="GFSNL04",
            setting_id="sunlu-pla",
            filament_type="PLA",
            nozzle_temp_min=220,
            nozzle_temp_max=230,
            bed_temp_min=65,
            bed_temp_max=65,
            drying_temp_min=0,
            drying_temp_max=0,
            drying_time=0,
            )

        updated_tray = _apply_assignment_to_tray_view(tray, spool, profile, "PLA")

        self.assertEqual(updated_tray.tray_info_idx, "GFSNL04")
        self.assertEqual(updated_tray.tray_type, "PLA")
        self.assertEqual(updated_tray.tray_color, "000000")
        self.assertEqual(updated_tray.nozzle_temp_min, 220)
        self.assertEqual(updated_tray.nozzle_temp_max, 230)
        self.assertEqual(updated_tray.bed_temp, 65)


if __name__ == "__main__":
    unittest.main()
