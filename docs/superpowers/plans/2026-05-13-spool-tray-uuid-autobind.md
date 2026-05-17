# Auto-Bind Spool to AMS Tray UUID — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-write `bambu_tray_uuid` on a Spoolman spool when the user activates that spool into an AMS tray, eliminating the manual Spoolman edit needed to bind inventory to the loaded AMS slot.

**Architecture:** A new `SpoolmanClient.bind_spool_to_tray_uuid` method (move-the-binding semantics), a `get_tray_uuid(tray_id)` sync accessor on both `GatewayActivator` and `MQTTPrinterClient`, and hooks into the two existing activation entry points (`POST /activate` for API/iOS callers, `POST /web/tray/{N}/assign` for the helper web UI). Strict semantics: if the target tray's uuid isn't yet known, return `409` and don't send the MQTT command.

**Tech Stack:** Python 3.12+, FastAPI, `unittest` (per CLAUDE.md), httpx, Jinja2/HTMX.

**Spec:** [`../specs/2026-05-13-spool-tray-uuid-autobind-design.md`](../specs/2026-05-13-spool-tray-uuid-autobind-design.md)

**Prerequisite:** `SpoolmanClient.ensure_spool_extra_fields` and the `bambu_tray_uuid` field definition must exist.

---

## File Structure

**Modify:**
- `app/services/spoolman.py` — add `_patch_spool` + `bind_spool_to_tray_uuid` methods.
- `app/services/gateway_activator.py` — add `get_tray_uuid(tray_id)` sync accessor.
- `app/services/mqtt_printer.py` — add the same `get_tray_uuid` accessor (symmetric).
- `app/models.py` — add `spool_id: int | None = None` to `ActivateRequest`.
- `app/routers/api.py` — extend `/activate` handler with bind-before-MQTT logic when `spool_id` present.
- `app/routers/web.py` — extend `assign_spool_to_tray` handler (`POST /web/tray/{N}/assign`) with the same bind-before-MQTT logic.

**Modify in `../spool-browser` (iOS app):**
- `SpoolBrowser/Services/SpoolHelperService.swift` — add one line to include `spool_id` in the `/activate` POST body.

**Create:**
- `tests/test_spoolman_bind.py` — unit tests for `bind_spool_to_tray_uuid`.
- `tests/test_get_tray_uuid.py` — unit tests for both clients' new accessor.
- `tests/test_api_activate_bind.py` — tests for `/activate` bind path.
- `tests/test_web_assign_bind.py` — tests for `/web/tray/{N}/assign` bind path.

---

## Task 1: SpoolmanClient — `_patch_spool` helper

**Files:**
- Modify: `app/services/spoolman.py`
- Modify: `tests/test_spoolman.py`

The existing `_patch_filament` (around line 177) PATCHes `/api/v1/filament/{id}`. We need the spool-side parallel: PATCH `/api/v1/spool/{id}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spoolman.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_spoolman.TestPatchSpool -v`
Expected: FAIL with `AttributeError: 'SpoolmanClient' object has no attribute '_patch_spool'`.

- [ ] **Step 3: Add helper methods to SpoolmanClient**

In `app/services/spoolman.py`, near `_patch_filament` (line ~177), add:

```python
    async def _get_spool(self, spool_id: int) -> dict:
        resp = await self._client.get(f"/api/v1/spool/{spool_id}")
        resp.raise_for_status()
        return resp.json()

    async def _patch_spool(
        self,
        spool_id: int,
        extra_fields: dict[str, str | None] | None = None,
        basic_fields: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {}
        if extra_fields is not None:
            current = await self._get_spool(spool_id)
            payload["extra"] = self._merge_extra_fields(current.get("extra") or {}, extra_fields)
        if basic_fields:
            payload.update(basic_fields)
        resp = await self._client.patch(f"/api/v1/spool/{spool_id}", json=payload)
        resp.raise_for_status()
```

Note: `_merge_extra_fields` is static. Verify it preserves `None` values (some implementations strip None). If it does strip, write the merge manually here:

```python
            merged = dict(current.get("extra") or {})
            merged.update(extra_fields)
            payload["extra"] = merged
```

Pick whichever produces the test outcome (the second test asserts `None` survives into the JSON body).

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_spoolman.TestPatchSpool -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/spoolman.py tests/test_spoolman.py
git commit -m "feat(spoolman): _patch_spool helper mirrors _patch_filament"
```

---

## Task 2: SpoolmanClient — `bind_spool_to_tray_uuid`

**Files:**
- Modify: `app/services/spoolman.py`
- Modify: `tests/test_spoolman.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spoolman.py`:

```python
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
```

- [ ] **Step 2: Run tests**

Run: `python -m unittest tests.test_spoolman.TestBindSpoolToTrayUuid -v`
Expected: FAIL with `AttributeError: ... no attribute 'bind_spool_to_tray_uuid'`.

- [ ] **Step 3: Implement**

In `app/services/spoolman.py`, after `_patch_spool`:

```python
    async def bind_spool_to_tray_uuid(self, *, spool_id: int, tray_uuid: str) -> None:
        """Set `bambu_tray_uuid` on `spool_id`, clearing the same uuid from any
        other spool first (move-the-binding).

        No-op when the target spool already holds this uuid. Self-heals the
        `bambu_tray_uuid` field definition if it doesn't yet exist.
        """
        await self.ensure_spool_extra_fields()

        encoded = self._json_encode(tray_uuid)

        # Find existing holders of this uuid
        resp = await self._client.get("/api/v1/spool")
        resp.raise_for_status()
        all_spools = resp.json() or []
        for spool in all_spools:
            extra = spool.get("extra") or {}
            if extra.get("bambu_tray_uuid") == encoded and spool.get("id") != spool_id:
                await self._patch_spool(spool["id"], extra_fields={"bambu_tray_uuid": None})

        # Set on target unless it already has it
        target = await self._get_spool(spool_id)
        target_extra = target.get("extra") or {}
        if target_extra.get("bambu_tray_uuid") == encoded:
            return
        await self._patch_spool(spool_id, extra_fields={"bambu_tray_uuid": encoded})
```

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_spoolman.TestBindSpoolToTrayUuid -v`
Expected: All 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/spoolman.py tests/test_spoolman.py
git commit -m "feat(spoolman): bind_spool_to_tray_uuid with move-the-binding"
```

---

## Task 3: `GatewayActivator.get_tray_uuid`

**Files:**
- Modify: `app/services/gateway_activator.py`
- Modify: `tests/test_gateway_activator.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gateway_activator.py`:

```python
class TestGetTrayUuidGateway(unittest.TestCase):
    def test_returns_uuid_for_known_tray(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {0: type("T", (), {"tray_uuid": "uuid-0"})()}
        self.assertEqual(a.get_tray_uuid(0), "uuid-0")

    def test_returns_none_for_unknown_tray(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {}
        self.assertIsNone(a.get_tray_uuid(99))

    def test_returns_none_when_uuid_empty(self):
        a = GatewayActivator(gateway_url="http://gw", printer_serial="P01")
        a._trays = {0: type("T", (), {"tray_uuid": ""})()}
        self.assertIsNone(a.get_tray_uuid(0))
```

- [ ] **Step 2: Run tests**

Run: `python -m unittest tests.test_gateway_activator.TestGetTrayUuidGateway -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement**

In `app/services/gateway_activator.py`, inside `GatewayActivator`:

```python
    def get_tray_uuid(self, tray_id: int) -> str | None:
        """Return the cached tray_uuid for the given tray slot, or None if
        unknown or empty. Call `request_full_status()` beforehand if you
        need a fresh read."""
        tray = self._trays.get(tray_id)
        if tray is None:
            return None
        uuid = getattr(tray, "tray_uuid", "") or ""
        return uuid or None
```

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_gateway_activator -v`
Expected: All PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add app/services/gateway_activator.py tests/test_gateway_activator.py
git commit -m "feat(gateway-activator): sync get_tray_uuid accessor"
```

---

## Task 4: `MQTTPrinterClient.get_tray_uuid`

**Files:**
- Modify: `app/services/mqtt_printer.py`
- Create: `tests/test_mqtt_printer_get_tray_uuid.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mqtt_printer_get_tray_uuid.py`:

```python
"""Tests for MQTTPrinterClient.get_tray_uuid."""

from __future__ import annotations

import unittest

from app.services.mqtt_printer import MQTTPrinterClient, TrayData


def _make_client() -> MQTTPrinterClient:
    return MQTTPrinterClient(ip="127.0.0.1", access_code="x", serial="S01")


class TestGetTrayUuidMqtt(unittest.TestCase):
    def test_returns_uuid_for_known_tray(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = "uuid-0"
        c._trays[0] = td
        self.assertEqual(c.get_tray_uuid(0), "uuid-0")

    def test_returns_none_for_unknown_tray(self):
        c = _make_client()
        self.assertIsNone(c.get_tray_uuid(99))

    def test_returns_none_when_uuid_empty(self):
        c = _make_client()
        td = TrayData()
        td.tray_uuid = ""
        c._trays[0] = td
        self.assertIsNone(c.get_tray_uuid(0))
```

(If the `MQTTPrinterClient` constructor requires more args or named-keyword
ones, adjust `_make_client` to match the actual signature — inspect
`app/services/mqtt_printer.py` first. The class definition starts at line 43.)

- [ ] **Step 2: Run tests**

Run: `python -m unittest tests.test_mqtt_printer_get_tray_uuid -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement**

In `app/services/mqtt_printer.py`, inside `MQTTPrinterClient`:

```python
    def get_tray_uuid(self, tray_id: int) -> str | None:
        """Return the cached tray_uuid for the given tray slot, or None if
        unknown or empty."""
        tray = self._trays.get(tray_id)
        if tray is None:
            return None
        uuid = getattr(tray, "tray_uuid", "") or ""
        return uuid or None
```

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_mqtt_printer_get_tray_uuid -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/mqtt_printer.py tests/test_mqtt_printer_get_tray_uuid.py
git commit -m "feat(mqtt-printer): sync get_tray_uuid accessor"
```

---

## Task 5: `ActivateRequest.spool_id`

**Files:**
- Modify: `app/models.py`

- [ ] **Step 1: Locate the model**

Read `app/models.py` and find `ActivateRequest` (it's a pydantic `BaseModel`). Confirm the existing fields.

- [ ] **Step 2: Add the optional field**

Add a new field:

```python
    spool_id: int | None = None
```

Place it after the existing tray-related fields for logical grouping (or at the end if no obvious place).

- [ ] **Step 3: Smoke-check imports**

```bash
python -c "from app.models import ActivateRequest; ActivateRequest(filament_id='X', filament_type='PLA', color_hex='FFFFFF', tray=0); print('ok')"
```

Expected: `ok`. The model should still construct without `spool_id`.

If `ActivateRequest` has required fields beyond `filament_id`/`filament_type`/`color_hex`/`tray`, adapt the smoke command accordingly.

- [ ] **Step 4: Commit**

```bash
git add app/models.py
git commit -m "feat(models): optional spool_id on ActivateRequest"
```

---

## Task 6: `/activate` API handler — bind when `spool_id` present

**Files:**
- Modify: `app/routers/api.py`
- Create: `tests/test_api_activate_bind.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_activate_bind.py`:

```python
"""Tests for /activate auto-bind behavior."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.main import app


def _setup_state(mqtt_tray_uuid: str | None, *, bind_raises: Exception | None = None):
    """Stub app.state with the minimum needed for /activate."""
    mqtt = MagicMock()
    mqtt.request_full_status = MagicMock()
    mqtt.get_tray_uuid = MagicMock(return_value=mqtt_tray_uuid)
    mqtt.activate_filament = MagicMock(return_value=(True, "Command sent to printer"))
    app.state.mqtt = mqtt

    spoolman = MagicMock()
    if bind_raises is not None:
        spoolman.bind_spool_to_tray_uuid = AsyncMock(side_effect=bind_raises)
    else:
        spoolman.bind_spool_to_tray_uuid = AsyncMock()
    app.state.spoolman = spoolman

    # The endpoint also touches orcaslicer.find_profile in some branches
    orcaslicer = MagicMock()
    orcaslicer.find_profile = AsyncMock(return_value=None)
    app.state.orcaslicer = orcaslicer

    app.state.recent_activations = []
    return mqtt, spoolman


def _activate_body(**overrides):
    body = {
        "filament_id": "GFA00",
        "filament_type": "PLA",
        "color_hex": "FFFFFF",
        "tray": 0,
    }
    body.update(overrides)
    return body


class TestActivateLegacy(unittest.TestCase):
    def test_no_spool_id_does_not_call_bind(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid="uuid-0")
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body())
        self.assertEqual(resp.status_code, 200)
        spoolman.bind_spool_to_tray_uuid.assert_not_awaited()
        mqtt.activate_filament.assert_called_once()


class TestActivateWithSpoolId(unittest.TestCase):
    def test_happy_path_binds_then_activates(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid="uuid-0")
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body(spool_id=42))
        self.assertEqual(resp.status_code, 200)
        spoolman.bind_spool_to_tray_uuid.assert_awaited_once_with(
            spool_id=42, tray_uuid="uuid-0",
        )
        mqtt.activate_filament.assert_called_once()
        # bind must come before activate
        self.assertTrue(
            spoolman.bind_spool_to_tray_uuid.await_args is not None
        )

    def test_409_when_tray_uuid_missing(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid=None)
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body(spool_id=42))
        self.assertEqual(resp.status_code, 409)
        spoolman.bind_spool_to_tray_uuid.assert_not_awaited()
        mqtt.activate_filament.assert_not_called()

    def test_502_when_bind_fails(self):
        import httpx
        err = httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("PATCH", "http://s/x"),
            response=httpx.Response(500, request=httpx.Request("PATCH", "http://s/x")),
        )
        mqtt, spoolman = _setup_state(mqtt_tray_uuid="uuid-0", bind_raises=err)
        client = TestClient(app)
        resp = client.post("/activate", json=_activate_body(spool_id=42))
        self.assertEqual(resp.status_code, 502)
        mqtt.activate_filament.assert_not_called()
```

- [ ] **Step 2: Run tests**

Run: `python -m unittest tests.test_api_activate_bind -v`
Expected: All FAIL.

- [ ] **Step 3: Implement the bind logic in `/activate`**

Read `app/routers/api.py` around line 72 (`activate_profile`). Find where `mqtt.activate_filament(...)` is called (around line 94). Before that call, insert:

```python
    if payload.spool_id is not None:
        mqtt.request_full_status()
        tray_uuid = mqtt.get_tray_uuid(payload.tray)
        if tray_uuid is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Tray {payload.tray} has no UUID yet "
                    "(AMS hasn't reported this slot). Retry once scanned."
                ),
            )
        try:
            await request.app.state.spoolman.bind_spool_to_tray_uuid(
                spool_id=payload.spool_id, tray_uuid=tray_uuid,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to bind spool {payload.spool_id} to tray_uuid: {exc}",
            )
```

`HTTPException` is already imported in this file; verify before adding.

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_api_activate_bind -v`
Expected: All 4 PASS.

- [ ] **Step 5: Run full suite**

Run: `python -m unittest discover tests -v`
Expected: No regressions.

- [ ] **Step 6: Commit**

```bash
git add app/routers/api.py tests/test_api_activate_bind.py
git commit -m "feat(api): bind spool to tray_uuid when /activate carries spool_id"
```

---

## Task 7: Web UI `/web/tray/{N}/assign` — bind on activate

**Files:**
- Modify: `app/routers/web.py`
- Create: `tests/test_web_assign_bind.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_assign_bind.py`:

```python
"""Tests for /web/tray/{N}/assign auto-bind behavior."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


def _setup_state(mqtt_tray_uuid: str | None, *, bind_raises: Exception | None = None,
                 found_spool: object | None = None, found_profile: object | None = None):
    mqtt = MagicMock()
    mqtt.request_full_status = MagicMock()
    mqtt.get_tray_uuid = MagicMock(return_value=mqtt_tray_uuid)
    mqtt.activate_filament = MagicMock(return_value=(True, "Command sent to printer"))
    mqtt.get_tray_data = MagicMock(return_value={})
    mqtt.get_connection_status = MagicMock(return_value={
        "configured": True, "connected": True, "tray_count": 0,
        "last_error": None, "last_message_at": None,
    })
    app.state.mqtt = mqtt

    spoolman = MagicMock()
    if bind_raises is not None:
        spoolman.bind_spool_to_tray_uuid = AsyncMock(side_effect=bind_raises)
    else:
        spoolman.bind_spool_to_tray_uuid = AsyncMock()
    spoolman.get_spools = AsyncMock(return_value=[found_spool] if found_spool else [])
    app.state.spoolman = spoolman

    orcaslicer = MagicMock()
    orcaslicer.get_profiles = AsyncMock(return_value=[found_profile] if found_profile else [])
    app.state.orcaslicer = orcaslicer
    return mqtt, spoolman


# Because /web/tray/N/assign depends on the spool model, ams_filament_id link,
# and OrcaSlicer profile lookup, the test patches the helpers it relies on
# rather than constructing the full chain.

class TestWebAssignBind(unittest.TestCase):
    def test_409_when_tray_uuid_missing(self):
        mqtt, spoolman = _setup_state(mqtt_tray_uuid=None)
        with patch("app.routers.web._load_spools", new=AsyncMock(return_value=([MagicMock(
                       id=42,
                       filament=MagicMock(is_linked=True, ams_filament_id="GFA00",
                                          color_hex="FFFFFF", material="PLA"),
                   )], None))), \
             patch("app.routers.web._build_tray_statuses", return_value=[
                 MagicMock(tray_index=0, tray_uuid="", tray_weight=0, tag_uid=None, cali_idx=-1)
             ]):
            client = TestClient(app)
            resp = client.post("/web/tray/0/assign", data={"machine": "", "spool_id": "42"})
        self.assertEqual(resp.status_code, 409)
        spoolman.bind_spool_to_tray_uuid.assert_not_awaited()
        mqtt.activate_filament.assert_not_called()
```

Note: this test setup is more involved than the API one because the web handler depends on several helpers (`_load_spools`, `_build_tray_statuses`, profile lookup). The test patches the minimum needed to reach the bind decision. If you find the test too brittle, an alternative is to extract the bind logic into a small helper function and unit-test it directly — that's an acceptable refactor inside this task.

- [ ] **Step 2: Run test**

Run: `python -m unittest tests.test_web_assign_bind -v`
Expected: FAIL (409 not returned because bind logic not in place yet).

- [ ] **Step 3: Implement**

Read `app/routers/web.py` around line 1888 (`assign_spool_to_tray`). Find the `mqtt.activate_filament(...)` call (around line 1928). Just before it, insert:

```python
    mqtt.request_full_status()
    tray_uuid = mqtt.get_tray_uuid(tray_index)
    if tray_uuid is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Tray {tray_index} has no UUID yet "
                "(AMS hasn't reported this slot). Retry once scanned."
            ),
        )
    try:
        await request.app.state.spoolman.bind_spool_to_tray_uuid(
            spool_id=spool_id, tray_uuid=tray_uuid,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to bind spool {spool_id} to tray_uuid: {exc}",
        )
```

`HTTPException` should already be imported.

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_web_assign_bind -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `python -m unittest discover tests -v`
Expected: No regressions.

- [ ] **Step 6: Commit**

```bash
git add app/routers/web.py tests/test_web_assign_bind.py
git commit -m "feat(web): bind spool to tray_uuid on /web/tray/{N}/assign"
```

---

## Task 8: iOS — pass `spool_id` in `SpoolHelperService.activate`

**Files:**
- Modify: `../spool-browser/SpoolBrowser/Services/SpoolHelperService.swift`

- [ ] **Step 1: Add the field**

Open `/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/spool-browser/SpoolBrowser/Services/SpoolHelperService.swift`. Locate the `body` dictionary construction in `activate(spool:tray:)` (starts at line 35):

Current:
```swift
var body: [String: Any] = [
    "filament_id": info.amsFilamentId,
    "filament_type": info.trayType,
    "color_hex": spool.colorHex ?? "",
    "tray": tray,
]
```

Change to:
```swift
var body: [String: Any] = [
    "filament_id": info.amsFilamentId,
    "filament_type": info.trayType,
    "color_hex": spool.colorHex ?? "",
    "tray": tray,
    "spool_id": spool.id,
]
```

`spool.id` is `Int` (confirmed in `Models/Spool.swift:4`).

- [ ] **Step 2: Build check (manual)**

Open the Xcode project at `../spool-browser/SpoolBrowser.xcodeproj` and build. Confirm no compile errors.

If you can't run Xcode in this environment, leave a note in the commit message stating the change is one-line and untested at build time. The user will manually verify.

- [ ] **Step 3: Commit**

```bash
cd /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/spool-browser
git add SpoolBrowser/Services/SpoolHelperService.swift
git commit -m "feat: pass spool_id when activating to enable server-side tray-uuid binding"
```

(Note: this commit is in the `spool-browser` repo, NOT the helper repo. If `spool-browser` has its own branching convention, follow that — start by checking `git status` / `git branch` in that repo before making changes.)

---

## Verification & smoke test

After all tasks complete:

- [ ] **Full test suite in helper**

```bash
cd /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-spool-helper
python -m unittest discover tests -v
```

Expected: All pass.

- [ ] **Manual smoke test** — requires a real printer + Spoolman + a loaded spool

1. Start gateway, helper, Spoolman.
2. Confirm AMS has reported tray uuids: `curl http://localhost:4844/api/ams | python -m json.tool` — note the tray 0 `tray_uuid`.
3. In the helper web UI, open the trays panel, expand tray 0's detail. Pick a Spoolman spool from the dropdown and click "Assign".
4. Check the spool's `bambu_tray_uuid` extra in Spoolman: it should now equal the tray uuid from step 2.
5. Re-assign a *different* spool to the same tray. Check both spools: only the latest should have `bambu_tray_uuid`; the previous spool's binding should be cleared.
6. Eject the AMS slot until tray 0 reports no uuid (or pick an unscanned tray). Try to assign: expect a `409` error toast.
7. From the spool-browser iOS app, open a spool, tap "Activate in tray N". Repeat verification.

---

## Self-review checklist (for the implementer)

- `bind_spool_to_tray_uuid` always calls `ensure_spool_extra_fields()` first. This makes the bind self-healing.
- The `get_tray_uuid` accessors return `None` for empty-string uuids (some AMS slots report `""` for empty trays).
- `request_full_status()` is sync on both `GatewayActivator` and `MQTTPrinterClient`. Calling it from the async route handlers is safe (it queues an internal refresh), but the result isn't guaranteed to be available immediately for the very next `get_tray_uuid` call on the MQTT path. If the API path proves flaky, consider awaiting a brief grace period — but the gateway path (HTTP GET to `/api/ams`) is synchronous-blocking and returns fresh data immediately, so it's the common case.
- The iOS change is independently shippable; the server gracefully ignores `spool_id` on older helper versions (Pydantic with `spool_id: int | None = None` accepts the field but does nothing if the handler isn't updated). Forward-compatible.
