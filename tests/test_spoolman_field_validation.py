import unittest

from app.services.spoolman import SpoolmanClient


class SpoolmanFieldValidationTests(unittest.TestCase):
    def test_validate_field_specs_accepts_expected_fields(self) -> None:
        fields = [
            {"key": "nozzle_temp", "name": "Nozzle Temperature", "field_type": "integer_range", "unit": "°C"},
            {"key": "bed_temp", "name": "Bed Temperature", "field_type": "integer_range", "unit": "°C"},
            {"key": "printing_speed", "name": "Printing Speed", "field_type": "integer_range", "unit": "mm/s"},
            {"key": "ams_filament_type", "name": "ams_filament_type", "field_type": "text"},
            {"key": "ams_filament_id", "name": "ams_filament_id", "field_type": "text"},
        ]

        validation = SpoolmanClient._validate_field_specs(fields, SpoolmanClient.REQUIRED_SETTINGS_FILAMENT_FIELDS)

        self.assertTrue(validation["is_valid"])
        self.assertEqual(validation["valid_count"], 5)
        self.assertEqual(validation["missing_count"], 0)
        self.assertEqual(validation["invalid_count"], 0)

    def test_validate_field_specs_flags_missing_and_invalid_fields(self) -> None:
        fields = [
            {"key": "nozzle_temp", "name": "Nozzle Temp", "field_type": "integer_range", "unit": "C"},
            {"key": "bed_temp", "name": "Bed Temperature", "field_type": "integer_range", "unit": "°C"},
            {"key": "ams_filament_type", "name": "ams_filament_type", "field_type": "integer"},
        ]

        validation = SpoolmanClient._validate_field_specs(fields, SpoolmanClient.REQUIRED_SETTINGS_FILAMENT_FIELDS)

        self.assertFalse(validation["is_valid"])
        self.assertEqual(validation["valid_count"], 1)
        self.assertEqual(validation["missing_count"], 2)
        self.assertEqual(validation["invalid_count"], 2)
        invalid_by_key = {item["expected"]["key"]: item for item in validation["invalid"]}
        self.assertIn("nozzle_temp", invalid_by_key)
        self.assertIn("ams_filament_type", invalid_by_key)
        self.assertEqual({item["key"] for item in validation["missing"]}, {"printing_speed", "ams_filament_id"})


if __name__ == "__main__":
    unittest.main()
