import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.orcaslicer import OrcaSlicerClient


def _make_client() -> OrcaSlicerClient:
    client = OrcaSlicerClient(base_url="http://orcaslicer.test", machine_id="GM014")
    return client


def _mock_response(json_body: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.json.return_value = json_body
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    return response


class ResolveImportProcessProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_to_resolve_endpoint_and_returns_payload(self) -> None:
        client = _make_client()
        expected_payload = {
            "setting_id": "CUSTOM001",
            "name": "Custom Process",
            "inherits_resolved": "0.20mm Standard @BBL P1S",
            "resolved_payload": {"layer_height": "0.2"},
        }
        client._client.post = AsyncMock(return_value=_mock_response(expected_payload))

        result = await client.resolve_import_process_profile({"name": "Custom Process"})

        client._client.post.assert_awaited_once_with(
            "/profiles/processes/resolve-import",
            json={"name": "Custom Process"},
        )
        self.assertEqual(result, expected_payload)


class ImportProcessProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_to_processes_endpoint_without_replace(self) -> None:
        client = _make_client()
        expected_payload = {
            "setting_id": "CUSTOM001",
            "name": "Custom Process",
            "message": "Imported",
        }
        client._client.post = AsyncMock(return_value=_mock_response(expected_payload))

        result = await client.import_process_profile({"name": "Custom Process"})

        client._client.post.assert_awaited_once_with(
            "/profiles/processes",
            json={"name": "Custom Process"},
            params={},
        )
        self.assertEqual(result, expected_payload)

    async def test_posts_with_replace_true_query_param(self) -> None:
        client = _make_client()
        client._client.post = AsyncMock(return_value=_mock_response({"setting_id": "X", "name": "X"}))

        await client.import_process_profile({"name": "X"}, replace=True)

        client._client.post.assert_awaited_once_with(
            "/profiles/processes",
            json={"name": "X"},
            params={"replace": "true"},
        )


if __name__ == "__main__":
    unittest.main()
