import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.models import FilamentProfileResponse, SpoolmanFilament


def _install_mocks(orcaslicer_mock: MagicMock, spoolman_mock: MagicMock) -> None:
    app.state.orcaslicer = orcaslicer_mock
    app.state.spoolman = spoolman_mock


def _base_profile() -> FilamentProfileResponse:
    return FilamentProfileResponse(
        name="Generic PLA",
        filament_id="GFA00",
        setting_id="GFA00",
        filament_type="PLA",
        nozzle_temp_min=190,
        nozzle_temp_max=240,
        bed_temp_min=55,
        bed_temp_max=55,
        drying_temp_min=0,
        drying_temp_max=0,
        drying_time=0,
        source="system",
    )


def _filament() -> SpoolmanFilament:
    return SpoolmanFilament(
        id=42,
        name="Marble Brick Red",
        material="PLA",
        settings_extruder_temp=210,
        settings_bed_temp=55,
    )


class CreateProfilePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orcaslicer = MagicMock()
        self.orcaslicer.default_machine_id = "GM014"
        self.orcaslicer.has_machine = MagicMock(return_value=True)
        self.orcaslicer.get_profiles = AsyncMock(return_value=[_base_profile()])
        self.orcaslicer.import_profile = AsyncMock(
            return_value={"setting_id": "USER001", "name": "Custom", "filament_id": "UFA00"}
        )

        self.spoolman = MagicMock()
        self.spoolman.REQUIRED_SETTINGS_FILAMENT_FIELDS = []
        self.spoolman.get_filaments = AsyncMock(return_value=[_filament()])
        self.spoolman.link_filament = AsyncMock(return_value=None)

        _install_mocks(self.orcaslicer, self.spoolman)
        self.client = TestClient(app)

    def test_filament_values_emitted_as_single_element_string_arrays(self) -> None:
        response = self.client.post(
            "/web/create-profile/42",
            data={
                "machine": "GM014",
                "profile_name": "Custom Marble Red",
                "base_setting_id": "GFA00",
                "filament_type": "PLA",
                "nozzle_temp_min": "190",
                "nozzle_temp_max": "240",
                "nozzle_temperature": "210",
                "nozzle_temperature_initial_layer": "210",
                "textured_plate_temp": "55",
                "textured_plate_temp_initial_layer": "55",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.orcaslicer.import_profile.assert_awaited_once()
        payload = self.orcaslicer.import_profile.await_args.args[0]

        self.assertEqual(payload["textured_plate_temp"], ["55"])
        self.assertEqual(payload["textured_plate_temp_initial_layer"], ["55"])
        self.assertEqual(payload["nozzle_temperature"], ["210"])
        self.assertEqual(payload["nozzle_temperature_initial_layer"], ["210"])
        self.assertEqual(payload["nozzle_temperature_range_low"], ["190"])
        self.assertEqual(payload["nozzle_temperature_range_high"], ["240"])
        self.assertNotIn("hot_plate_temp", payload)
        self.assertNotIn("hot_plate_temp_initial_layer", payload)


if __name__ == "__main__":
    unittest.main()
