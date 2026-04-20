import unittest

from app.services.orcaslicer import OrcaSlicerClient


class BuildProfileResolvedDetailTests(unittest.TestCase):
    def test_extracts_values_from_resolved_block(self) -> None:
        summary = {
            "setting_id": "SUNLU PLA + GEN2@Bambu Lab A1 mini 0.4 nozzle",
            "name": "SUNLU PLA + GEN2@Bambu Lab A1 mini 0.4 nozzle",
            "filament_id": "S5194737",
            "filament_type": "PLA",
            "vendor": "User",
        }
        detail = {
            "setting_id": summary["setting_id"],
            "name": summary["name"],
            "vendor": "User",
            "inheritance_chain": [],
            "resolved": {
                "filament_id": "S5194737",
                "filament_type": ["PLA"],
                "nozzle_temperature": ["220", "220"],
                "nozzle_temperature_initial_layer": ["220"],
                "nozzle_temperature_range_low": ["190"],
                "nozzle_temperature_range_high": ["240"],
                "hot_plate_temp": ["55"],
                "slow_down_min_speed": ["20", "20"],
                "filament_max_volumetric_speed": ["12", "12"],
                "filament_dev_ams_drying_temperature": ["50", "55"],
                "filament_dev_ams_drying_time": ["8"],
            },
        }

        profile = OrcaSlicerClient._build_profile(summary, detail)

        self.assertEqual(profile.filament_id, "S5194737")
        self.assertEqual(profile.filament_type, "PLA")
        self.assertEqual(profile.nozzle_temp_min, 190)
        self.assertEqual(profile.nozzle_temp_max, 240)
        self.assertEqual(profile.bed_temp_min, 55)
        self.assertEqual(profile.bed_temp_max, 55)
        self.assertEqual(profile.extruder_temp, 220)
        self.assertEqual(profile.extruder_temp_initial_layer, 220)
        self.assertEqual(profile.drying_temp_min, 50)
        self.assertEqual(profile.drying_temp_max, 55)
        self.assertEqual(profile.drying_time, 8)

    def test_falls_back_to_top_level_when_resolved_missing(self) -> None:
        summary = {
            "setting_id": "Legacy profile",
            "name": "Legacy profile",
            "filament_id": "L123",
            "filament_type": "PLA",
        }
        detail = {
            "setting_id": summary["setting_id"],
            "name": summary["name"],
            "filament_id": "L123",
            "filament_type": ["PLA"],
            "nozzle_temperature_range_low": ["200"],
            "nozzle_temperature_range_high": ["230"],
            "hot_plate_temp": ["60"],
            "slow_down_min_speed": ["25"],
            "filament_max_volumetric_speed": ["15"],
        }

        profile = OrcaSlicerClient._build_profile(summary, detail)

        self.assertEqual(profile.nozzle_temp_min, 200)
        self.assertEqual(profile.nozzle_temp_max, 230)
        self.assertEqual(profile.bed_temp_min, 60)


if __name__ == "__main__":
    unittest.main()
