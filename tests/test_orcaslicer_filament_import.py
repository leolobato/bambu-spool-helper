import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.orcaslicer import OrcaSlicerClient


def _make_client() -> OrcaSlicerClient:
    return OrcaSlicerClient(base_url="http://orcaslicer.test", machine_id="GM014")


def _mock_response(json_body: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.json.return_value = json_body
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    return response


class ImportFilamentProfileVersionStampTests(unittest.IsolatedAsyncioTestCase):
    """`import_profile` must stamp `version` so OrcaSlicer Desktop can re-import.

    OrcaSlicer Desktop's GUI silently rejects user JSONs without a parseable
    `version` field (`PresetBundle.cpp::load_user_presets` returns false on
    `Semver::parse("")`). Files saved through this client need one to remain
    round-trippable to the desktop GUI.
    """

    async def test_stamps_default_version_when_payload_has_none(self) -> None:
        client = _make_client()
        client._client.post = AsyncMock(return_value=_mock_response(
            {"setting_id": "X", "name": "X", "filament_id": "PXX"}
        ))
        # Stub the post-import reload so we don't hit additional endpoints.
        client.load_profiles = AsyncMock(return_value=[])

        await client.import_profile({"name": "X", "inherits": "Bambu PLA Basic @BBL A1M"})

        client._client.post.assert_awaited_once_with(
            "/profiles/filaments",
            json={
                "name": "X",
                "inherits": "Bambu PLA Basic @BBL A1M",
                "version": OrcaSlicerClient.DEFAULT_PROFILE_VERSION,
            },
        )

    async def test_preserves_caller_supplied_version(self) -> None:
        client = _make_client()
        client._client.post = AsyncMock(return_value=_mock_response(
            {"setting_id": "X", "name": "X", "filament_id": "PXX"}
        ))
        client.load_profiles = AsyncMock(return_value=[])

        await client.import_profile(
            {"name": "X", "inherits": "Bambu PLA Basic @BBL A1M", "version": "1.8.0.13"}
        )

        client._client.post.assert_awaited_once_with(
            "/profiles/filaments",
            json={
                "name": "X",
                "inherits": "Bambu PLA Basic @BBL A1M",
                "version": "1.8.0.13",
            },
        )

    async def test_stamps_version_when_field_is_blank_string(self) -> None:
        """An empty/whitespace `version` is treated as missing — Semver can't parse it."""
        client = _make_client()
        client._client.post = AsyncMock(return_value=_mock_response(
            {"setting_id": "X", "name": "X", "filament_id": "PXX"}
        ))
        client.load_profiles = AsyncMock(return_value=[])

        await client.import_profile({"name": "X", "version": "   "})

        sent = client._client.post.await_args.kwargs["json"]
        self.assertEqual(sent["version"], OrcaSlicerClient.DEFAULT_PROFILE_VERSION)


if __name__ == "__main__":
    unittest.main()
