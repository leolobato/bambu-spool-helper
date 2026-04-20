import unittest

from app.models import FilamentProfileResponse, SpoolmanFilament
from app.routers.web import _infer_filament_type_from_name, _resolve_link_filament_type


class LinkFilamentTypeFallbackTests(unittest.TestCase):
    def test_resolve_link_filament_type_uses_spoolman_material_when_profile_type_missing(self) -> None:
        profile = FilamentProfileResponse(
            name="SUNLU PLA+ 2.0 @System",
            filament_id="OGFSNL04",
            setting_id="OSNLS04",
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
            id=13,
            name="SUNLU PLA+ 2.0 White",
            material="PLA",
            color_hex="FFFFFF",
            extra={},
        )

        resolved = _resolve_link_filament_type(profile, filament)

        self.assertEqual(resolved, "PLA")

    def test_infer_filament_type_from_name_recovers_pla(self) -> None:
        inferred = _infer_filament_type_from_name("SUNLU PLA+ 2.0 @System")
        self.assertEqual(inferred, "PLA")


if __name__ == "__main__":
    unittest.main()
