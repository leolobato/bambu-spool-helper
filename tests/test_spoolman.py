"""Tests for SpoolmanClient.ensure_spool_extra_fields."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services.spoolman import SpoolmanClient


def _resp(status: int, json=None) -> httpx.Response:
    return httpx.Response(status, json=json or {}, request=httpx.Request("GET", "http://s/"))


class TestEnsureSpoolExtraFields(unittest.IsolatedAsyncioTestCase):
    async def test_creates_bambu_tray_uuid_when_missing(self):
        c = SpoolmanClient("http://s")
        try:
            with patch.object(c._client, "get", new=AsyncMock(return_value=_resp(200, []))) as g, \
                 patch.object(c._client, "post", new=AsyncMock(return_value=_resp(201, {}))) as p:
                await c.ensure_spool_extra_fields()
            g.assert_called_once_with("/api/v1/field/spool")
            p.assert_called_once()
            args, kwargs = p.call_args
            assert "bambu_tray_uuid" in args[0]
        finally:
            await c.close()

    async def test_skips_when_field_already_exists(self):
        c = SpoolmanClient("http://s")
        existing = [{"key": "bambu_tray_uuid", "name": "Bambu Tray UUID", "field_type": "text"}]
        try:
            with patch.object(c._client, "get", new=AsyncMock(return_value=_resp(200, existing))), \
                 patch.object(c._client, "post", new=AsyncMock(return_value=_resp(201, {}))) as p:
                await c.ensure_spool_extra_fields()
            p.assert_not_called()
        finally:
            await c.close()


class TestBindSpoolToTrayUuid(unittest.IsolatedAsyncioTestCase):
    async def _spoolman_with(self, all_spools, target_spool):
        c = SpoolmanClient("http://s")
        # ensure_spool_extra_fields is called inside bind; stub it to a no-op
        c.ensure_spool_extra_fields = AsyncMock()
        # GET /api/v1/spool returns all_spools
        # GET /api/v1/spool/{id} returns target_spool when patching
        async def _get(url, *a, **kw):
            if url == "/api/v1/spool":
                return _resp(200, all_spools)
            if url.startswith("/api/v1/spool/"):
                return _resp(200, target_spool)
            return _resp(404, {})
        c._client.get = AsyncMock(side_effect=_get)
        c._client.patch = AsyncMock(return_value=_resp(200, {}))
        return c

    async def test_sets_uuid_on_target_when_no_others_hold_it(self):
        c = await self._spoolman_with(
            all_spools=[{"id": 1, "extra": {}}, {"id": 2, "extra": {"bambu_tray_uuid": '"other"'}}],
            target_spool={"id": 1, "extra": {}},
        )
        try:
            await c.bind_spool_to_tray_uuid(spool_id=1, tray_uuid="new-uuid")
            # Exactly one PATCH (the set), no clear needed
            self.assertEqual(c._client.patch.call_count, 1)
            args, kwargs = c._client.patch.call_args
            self.assertEqual(args[0], "/api/v1/spool/1")
            self.assertEqual(kwargs["json"]["extra"]["bambu_tray_uuid"], '"new-uuid"')
        finally:
            await c.close()

    async def test_clears_uuid_from_others_then_sets_on_target(self):
        c = await self._spoolman_with(
            all_spools=[
                {"id": 1, "extra": {"bambu_tray_uuid": '"abc"'}},
                {"id": 7, "extra": {"bambu_tray_uuid": '"abc"'}},  # stale, must be cleared
            ],
            target_spool={"id": 1, "extra": {"bambu_tray_uuid": '"abc"'}},
        )
        try:
            await c.bind_spool_to_tray_uuid(spool_id=1, tray_uuid="abc")
            # Target already has it → only the clear PATCH on id=7, no set on target
            patched_urls = [call.args[0] for call in c._client.patch.call_args_list]
            self.assertIn("/api/v1/spool/7", patched_urls)
            self.assertNotIn("/api/v1/spool/1", patched_urls)

            # The clear writes the JSON-encoded empty string (not null),
            # because Spoolman rejects null for spool extras.
            clear_call = next(
                call for call in c._client.patch.call_args_list
                if call.args[0] == "/api/v1/spool/7"
            )
            self.assertEqual(
                clear_call.kwargs["json"]["extra"]["bambu_tray_uuid"], '""',
            )
        finally:
            await c.close()

    async def test_noop_when_target_already_bound(self):
        c = await self._spoolman_with(
            all_spools=[{"id": 1, "extra": {"bambu_tray_uuid": '"abc"'}}],
            target_spool={"id": 1, "extra": {"bambu_tray_uuid": '"abc"'}},
        )
        try:
            await c.bind_spool_to_tray_uuid(spool_id=1, tray_uuid="abc")
            self.assertEqual(c._client.patch.call_count, 0)
        finally:
            await c.close()

    async def test_calls_ensure_spool_extra_fields_first(self):
        c = await self._spoolman_with(
            all_spools=[],
            target_spool={"id": 1, "extra": {}},
        )
        try:
            await c.bind_spool_to_tray_uuid(spool_id=1, tray_uuid="x")
            c.ensure_spool_extra_fields.assert_awaited_once()
        finally:
            await c.close()


class TestPatchSpool(unittest.IsolatedAsyncioTestCase):
    async def test_patches_spool_extra_field(self):
        c = SpoolmanClient("http://s")
        current = {"id": 42, "extra": {"existing": '"x"'}}
        try:
            with patch.object(c._client, "get", new=AsyncMock(return_value=_resp(200, current))), \
                 patch.object(c._client, "patch", new=AsyncMock(return_value=_resp(200, {}))) as p:
                await c._patch_spool(42, extra_fields={"bambu_tray_uuid": '"abc"'})
            args, kwargs = p.call_args
            self.assertEqual(args[0], "/api/v1/spool/42")
            self.assertEqual(kwargs["json"]["extra"]["bambu_tray_uuid"], '"abc"')
            # Existing extras preserved
            self.assertEqual(kwargs["json"]["extra"]["existing"], '"x"')
        finally:
            await c.close()

    async def test_patches_spool_clears_extra_when_value_none(self):
        c = SpoolmanClient("http://s")
        current = {"id": 42, "extra": {"bambu_tray_uuid": '"abc"'}}
        try:
            with patch.object(c._client, "get", new=AsyncMock(return_value=_resp(200, current))), \
                 patch.object(c._client, "patch", new=AsyncMock(return_value=_resp(200, {}))) as p:
                await c._patch_spool(42, extra_fields={"bambu_tray_uuid": None})
            args, kwargs = p.call_args
            self.assertIsNone(kwargs["json"]["extra"]["bambu_tray_uuid"])
        finally:
            await c.close()
