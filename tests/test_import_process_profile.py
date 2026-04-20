import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app


def _install_mocks(orcaslicer_mock: MagicMock, spoolman_mock: MagicMock) -> None:
    app.state.orcaslicer = orcaslicer_mock
    app.state.spoolman = spoolman_mock


def _base_orcaslicer_mock() -> MagicMock:
    mock = MagicMock()
    mock.default_machine_id = "GM014"
    mock.get_machines = MagicMock(return_value=[])
    mock.load_machines = AsyncMock(return_value=[])
    mock.has_machine = MagicMock(return_value=True)
    return mock


def _base_spoolman_mock() -> MagicMock:
    mock = MagicMock()
    mock.REQUIRED_SETTINGS_FILAMENT_FIELDS = []
    return mock


class ImportProcessProfileRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orcaslicer = _base_orcaslicer_mock()
        self.spoolman = _base_spoolman_mock()
        _install_mocks(self.orcaslicer, self.spoolman)
        # IMPORTANT: do NOT use `with TestClient(app) as client:` — entering the
        # context manager starts the app lifespan, which tries to reach a real
        # orcaslicer service. Plain instantiation skips the lifespan, leaving
        # our injected mocks intact.
        self.client = TestClient(app)

    def test_kind_process_happy_path(self) -> None:
        self.orcaslicer.resolve_import_process_profile = AsyncMock(
            return_value={
                "setting_id": "CUSTOM001",
                "name": "Custom Process",
                "inherits_resolved": "0.20mm Standard",
                "resolved_payload": {
                    "name": "Custom Process",
                    "setting_id": "CUSTOM001",
                    "layer_height": "0.2",
                    "vendor": "BBL",
                },
            }
        )
        self.orcaslicer.import_process_profile = AsyncMock(
            return_value={
                "setting_id": "CUSTOM001",
                "name": "Custom Process",
                "message": "Imported",
            }
        )

        payload_bytes = b'{"name": "Custom Process", "setting_id": "CUSTOM001"}'
        response = self.client.post(
            "/web/import-profile",
            data={"machine": "GM014", "kind": "process"},
            files={"profile_file": ("custom.json", payload_bytes, "application/json")},
        )

        self.assertEqual(response.status_code, 200)
        self.orcaslicer.resolve_import_process_profile.assert_awaited_once()
        self.orcaslicer.import_process_profile.assert_awaited_once()
        body = response.text
        self.assertIn("Custom Process", body)
        self.assertIn("CUSTOM001", body)

    def test_kind_process_resolve_failure_renders_error(self) -> None:
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "bad payload"
        self.orcaslicer.resolve_import_process_profile = AsyncMock(
            side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=mock_response)
        )

        response = self.client.post(
            "/web/import-profile",
            data={"machine": "GM014", "kind": "process"},
            files={"profile_file": ("bad.json", b"{}", "application/json")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Profile resolution failed", response.text)
        self.assertIn("bad payload", response.text)


if __name__ == "__main__":
    unittest.main()
