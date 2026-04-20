import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models import FilamentProfileResponse, SpoolmanFilament
from app.routers.web import (
    _build_create_profile_field_mappings,
    _build_profile_field_sync,
    _extract_payload_filament_type,
    _find_profile_by_linked_id,
    _find_profile_by_setting_id,
    _render_filament_detail,
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
            ),
        ]

        matched_by_filament_id = _find_profile_by_linked_id(profiles, "GFSA00")
        matched_by_setting_id = _find_profile_by_setting_id(profiles, "correct-setting")

        self.assertIsNotNone(matched_by_filament_id)
        self.assertIsNotNone(matched_by_setting_id)
        self.assertEqual(matched_by_filament_id.setting_id, "broken-setting")
        self.assertEqual(matched_by_setting_id.setting_id, "correct-setting")
        self.assertEqual(matched_by_setting_id.filament_type, "PLA")

    def test_build_profile_field_sync_compares_current_spoolman_values_to_linked_profile(self) -> None:
        filament = SpoolmanFilament(
            id=5,
            name="Linked PLA",
            material="PLA",
            extruder_temp=210,
            bed_temp=50,
            extra={
                "nozzle_temp": "[190,220]",
                "bed_temp": "[55,55]",
                "printing_speed": "[12,18]",
            },
        )
        profile = FilamentProfileResponse(
            name="PLA Match",
            filament_id="GFSNL04",
            setting_id="linked-setting",
            filament_type="PLA",
            extruder_temp=225,
            extruder_temp_initial_layer=230,
            nozzle_temp_min=220,
            nozzle_temp_max=230,
            bed_temp_min=65,
            bed_temp_max=65,
            drying_temp_min=0,
            drying_temp_max=0,
            drying_time=0,
            )

        sync = _build_profile_field_sync(filament, profile)

        self.assertIsNotNone(sync)
        assert sync is not None
        self.assertTrue(sync["has_changes"])
        custom_field_by_key = {field["key"]: field for field in sync["custom_fields"]}
        basic_field_by_key = {field["key"]: field for field in sync["basic_fields"]}
        self.assertEqual(custom_field_by_key["nozzle_temp"]["current"], (190, 220))
        self.assertEqual(custom_field_by_key["nozzle_temp"]["target"], (220, 230))
        self.assertEqual(custom_field_by_key["bed_temp"]["current_label"], "55 °C")
        self.assertNotIn("printing_speed", custom_field_by_key)
        self.assertEqual(basic_field_by_key["extruder_temp"]["current_label"], "210 °C")
        self.assertEqual(basic_field_by_key["extruder_temp"]["target"], 225)
        self.assertEqual(basic_field_by_key["extruder_temp"]["target_label"], "225 °C (initial layer 230 °C)")
        self.assertEqual(basic_field_by_key["extruder_temp"]["source_label"], "nozzle_temperature + nozzle_temperature_initial_layer")
        self.assertEqual(basic_field_by_key["bed_temp_basic"]["current_label"], "50 °C")
        self.assertEqual(basic_field_by_key["bed_temp_basic"]["target_label"], "65 °C")
        self.assertEqual(basic_field_by_key["bed_temp_basic"]["source_label"], "hot_plate_temp")
        self.assertFalse(sync["is_fully_synced"])

    def test_build_profile_field_sync_marks_matching_fields_as_fully_synced(self) -> None:
        filament = SpoolmanFilament(
            id=6,
            name="Synced PLA",
            material="PLA",
            extruder_temp=225,
            bed_temp=65,
            extra={
                "nozzle_temp": "[220,230]",
                "bed_temp": "[65,65]",
                "printing_speed": "[18,25]",
            },
        )
        profile = FilamentProfileResponse(
            name="PLA Match",
            filament_id="GFSNL04",
            setting_id="linked-setting",
            filament_type="PLA",
            extruder_temp=225,
            nozzle_temp_min=220,
            nozzle_temp_max=230,
            bed_temp_min=65,
            bed_temp_max=65,
            drying_temp_min=0,
            drying_temp_max=0,
            drying_time=0,
            )

        sync = _build_profile_field_sync(filament, profile)

        self.assertIsNotNone(sync)
        assert sync is not None
        self.assertFalse(sync["has_changes"])
        self.assertTrue(sync["is_fully_synced"])

    def test_build_create_profile_field_mappings_describes_spoolman_to_orca_flow(self) -> None:
        mappings = _build_create_profile_field_mappings(
            filament_type="PLA",
            nozzle_temp=(230, 190),
            bed_temp=60,
            printing_speed=(20, 18),
        )

        mapping_by_label = {item["label"]: item for item in mappings}
        self.assertEqual(mapping_by_label["Nozzle Temperature Range"]["source_value"], "190-230 °C")
        self.assertEqual(
            mapping_by_label["Nozzle Temperature Range"]["target_fields"],
            "nozzle_temperature_range_low + nozzle_temperature_range_high",
        )
        self.assertEqual(mapping_by_label["Bed Temperature"]["target_fields"], "hot_plate_temp")
        self.assertEqual(mapping_by_label["Printing Speed Range"]["source_value"], "18-20 mm/s")
        self.assertEqual(mapping_by_label["Printing Speed Range"]["target_fields"], "slow_down_min_speed")
        self.assertIn("Only the lower bound", mapping_by_label["Printing Speed Range"]["meaning"])

    def test_spoolman_filament_reads_settings_basic_fields_from_api_aliases(self) -> None:
        filament = SpoolmanFilament.model_validate(
            {
                "id": 9,
                "name": "Alias Test",
                "settings_extruder_temp": 215,
                "settings_bed_temp": 60,
                "extra": {},
            }
        )

        self.assertEqual(filament.extruder_temp, 215)
        self.assertEqual(filament.bed_temp, 60)

    def test_render_filament_detail_prefers_single_filament_fetch_for_latest_state(self) -> None:
        filament = SpoolmanFilament(
            id=5,
            name="Linked PLA",
            material="PLA",
            extra={
                "ams_filament_id": '"GFSNL04"',
                "ams_filament_type": '"PLA"',
            },
        )
        profile = FilamentProfileResponse(
            name="PLA Match",
            filament_id="GFSNL04",
            setting_id="linked-setting",
            filament_type="PLA",
            extruder_temp=220,
            nozzle_temp_min=220,
            nozzle_temp_max=230,
            bed_temp_min=65,
            bed_temp_max=65,
            drying_temp_min=0,
            drying_temp_max=0,
            drying_time=0,
            )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    spoolman=SimpleNamespace(get_filament=AsyncMock(return_value=filament)),
                    orcaslicer=SimpleNamespace(get_profiles=AsyncMock(return_value=[profile])),
                )
            )
        )

        response = self._run_async(_render_filament_detail(request, 5, "GM020"))

        self.assertEqual(response.status_code, 200)
        request.app.state.spoolman.get_filament.assert_awaited_once_with(5)
        hx_trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(hx_trigger["filament-selected"]["filamentId"], 5)

    def _run_async(self, coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
