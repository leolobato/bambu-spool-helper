import unittest
from unittest.mock import AsyncMock

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

    def test_merge_extra_fields_preserves_existing_values(self) -> None:
        merged = SpoolmanClient._merge_extra_fields(
            {
                "ams_filament_id": '"GFSNL04"',
                "ams_filament_type": '"PLA"',
                "vendor_url": '"https://example.com"',
            },
            {
                "nozzle_temp": "[220,230]",
                "bed_temp": "[65,65]",
                "printing_speed": "[18,25]",
            },
        )

        self.assertEqual(merged["ams_filament_id"], '"GFSNL04"')
        self.assertEqual(merged["ams_filament_type"], '"PLA"')
        self.assertEqual(merged["vendor_url"], '"https://example.com"')
        self.assertEqual(merged["nozzle_temp"], "[220,230]")
        self.assertEqual(merged["bed_temp"], "[65,65]")
        self.assertEqual(merged["printing_speed"], "[18,25]")

    def test_update_filament_profile_fields_uses_spoolman_settings_keys_for_basic_fields(self) -> None:
        client = SpoolmanClient("http://example.com")
        client.ensure_required_filament_fields = AsyncMock(return_value={"validation": {"invalid_count": 0}})
        client._patch_filament = AsyncMock()

        try:
            result = self._run_async(
                client.update_filament_profile_fields(
                    7,
                    extruder_temp=225,
                    nozzle_temp=(220, 230),
                    bed_temp=(65, 65),
                    printing_speed=(18, 25),
                    basic_bed_temp=65,
                )
            )
        finally:
            self._run_async(client.close())

        self.assertEqual(result, {"validation": {"invalid_count": 0}})
        client._patch_filament.assert_awaited_once_with(
            7,
            extra_fields={
                "nozzle_temp": "[220, 230]",
                "bed_temp": "[65, 65]",
                "printing_speed": "[18, 25]",
            },
            basic_fields={
                "settings_extruder_temp": 225,
                "settings_bed_temp": 65,
            },
        )

    def _run_async(self, coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
