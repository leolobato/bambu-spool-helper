# Auto-Bind Spoolman Spool to AMS Tray UUID on Activate — Design

**Status:** Approved (brainstorm complete)
**Date:** 2026-05-13
**Scope:** `bambu-spool-helper` server + `../spool-browser` iOS app client change
**Depends on:** Spoolman spool extra-field support for `bambu_tray_uuid`.
This feature populates that field so the helper can remember which physical
spool is bound to each AMS slot.

---

## 1. Goal

Eliminate the manual step of "go to Spoolman and paste the AMS tray UUID
into the spool's `bambu_tray_uuid` extra field." Instead, write that
binding automatically as a side effect of the existing **activate**
action — the action the user already performs to load a filament profile
into an AMS tray.

When a user activates a Spoolman spool into tray N (via the helper web UI
or `spool-browser` iOS), the helper:

1. Looks up the tray's current `tray_uuid` from the gateway.
2. Clears any previous `bambu_tray_uuid` binding pointing at that uuid.
3. Writes `bambu_tray_uuid = <uuid>` on the activated spool.
4. Sends the MQTT activate command (existing behavior).

### Non-goals

- A standalone "bind without activating" UI.
- Auto-binding by inference (e.g., "if there's only one spool of this
  material, assume it").
- Backfilling bindings for spools currently loaded but never activated
  through the helper.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Existing /activate handler (app/routers/api.py)             │
│                                                              │
│   if spool_id is present:                                    │
│     1. mqtt.request_full_status()    ← refresh /api/ams      │
│     2. uuid = mqtt.get_tray_uuid(tray)                       │
│     3. 409 if uuid is None                                   │
│     4. spoolman.bind_spool_to_tray_uuid(spool_id, uuid)      │
│     5. (existing) mqtt.activate_filament(...)                │
│                                                              │
│   else:                                                      │
│     1. (existing) mqtt.activate_filament(...)                │
└──────────────────────────────────────────────────────────────┘
```

**New code:**

- `SpoolmanClient.bind_spool_to_tray_uuid` — atomic "move-the-binding"
  helper.
- `GatewayActivator.get_tray_uuid(tray_id)` — sync accessor over the
  cached AMS state.
- `MQTTPrinterClient.get_tray_uuid(tray_id)` — parallel implementation so
  the legacy MQTT-direct mode keeps working symmetrically.
- Web UI flow: a small HTMX partial that resolves candidate spools and
  conditionally renders a picker before posting `/activate`.
- iOS: one-line change in `SpoolHelperService.swift` to include
  `spool_id` in the activate body.

---

## 3. Component contracts

### `SpoolmanClient.bind_spool_to_tray_uuid`

```python
async def bind_spool_to_tray_uuid(self, *, spool_id: int, tray_uuid: str) -> None:
    """Set `bambu_tray_uuid` on `spool_id`, clearing the same uuid from any
    other spool first (move-the-binding).

    No-op when the target spool already holds this uuid. Calls
    `ensure_spool_extra_fields()` first as a self-heal in case the field
    has not been created yet on this Spoolman.

    Raises whatever the underlying httpx PATCH calls raise (we don't
    swallow Spoolman errors — the caller decides the HTTP response).
    """
```

Implementation outline:

1. `await self.ensure_spool_extra_fields()` — idempotent; cheap once
   the field exists.
2. `GET /api/v1/spool` to find spools where
   `extra.bambu_tray_uuid == _json_encode(tray_uuid)`.
3. For each such spool whose `id != spool_id`, PATCH `extra` to clear
   `bambu_tray_uuid` (set to `null` per Spoolman's extras semantics — see
   note below on what "clear" means).
4. If the target spool already has this uuid, return.
5. Otherwise PATCH the target spool to set
   `bambu_tray_uuid = _json_encode(tray_uuid)`.

**Note on clearing**: Spoolman's `PATCH /api/v1/spool/{id}` with
`{"extra": {"bambu_tray_uuid": null}}` removes the key from the spool's
extras. The existing `_patch_filament` pattern uses `_merge_extra_fields`
to merge — we'll add a parallel `_patch_spool` that merges the same way.
Setting `bambu_tray_uuid` to `null` (Python `None`) in the merge produces
JSON `null`, which Spoolman accepts as "delete this key."

### `GatewayActivator.get_tray_uuid` and `MQTTPrinterClient.get_tray_uuid`

```python
def get_tray_uuid(self, tray_id: int) -> str | None:
    """Return the currently-known tray_uuid for the given tray slot, or None
    if no fresh information is available. Sync — reads from the in-memory
    cache populated by the existing tray-data polling/MQTT path."""
```

The activate handler calls `request_full_status()` (already exists on
both implementations) immediately before this, so the cache is fresh
within the same request.

### `POST /activate` payload

```python
class ActivateRequest(BaseModel):
    filament_id: str
    filament_type: str
    color_hex: str
    tray: int
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    bed_temp: int | None = None
    # ... existing fields ...

    spool_id: int | None = None   # NEW, optional
```

### Web UI

The helper's tray-detail partial (`app/templates/partials/tray_detail.html`)
already renders a per-tray spool dropdown. The user picks a spool, and
the form POSTs `spool_id` to `POST /web/tray/{tray_index}/assign`
(`app/routers/web.py:1888`), which calls `mqtt.activate_filament(...)`
using the spool's linked filament profile.

**No new picker is needed.** We just hook the bind into the existing
`assign_spool_to_tray` handler:

1. Just before the existing `mqtt.activate_filament(...)` call (line
   ~1928), call `mqtt.request_full_status()` + `mqtt.get_tray_uuid(tray_index)`.
2. If `uuid is None`, return a 409 (matches the API behavior).
3. Otherwise call `spoolman.bind_spool_to_tray_uuid(spool_id=spool_id,
   tray_uuid=uuid)`.
4. Then proceed to the existing MQTT activation.

The web handler already has `spool_id` from the form — no UI change is
necessary.

### iOS `SpoolHelperService.activate`

`spool-browser/SpoolBrowser/Services/SpoolHelperService.swift:25` — add
one line to the body dict:

```swift
body["spool_id"] = spool.id
```

No other iOS changes. The Swift `SpoolHelperError.activationFailed(message)`
already exists and will propagate the server's 409/502 message verbatim.

---

## 4. Data flow

### Flow A — Direct API call with `spool_id` (iOS, or web-1-match)

```
1. POST /activate { spool_id: 42, tray: 0, filament_id: GFA00, ... }
2. handler: mqtt.request_full_status()                # refresh cache
3. handler: uuid = mqtt.get_tray_uuid(0)
4. If uuid is None  → 409 "tray 0 has no UUID yet; AMS hasn't reported
                            this slot yet — retry once scanned"
5. handler: await spoolman.bind_spool_to_tray_uuid(spool_id=42, tray_uuid=uuid)
6. handler: mqtt.activate_filament(...)               # existing
7. 200 OK { success: true, profile_name: ..., message: ... }
```

### Flow B — Helper web UI

```
1. User opens tray-detail panel for tray N (existing UI).
2. User picks a spool from the existing dropdown, submits.
3. POST /web/tray/N/assign { spool_id, machine }   (existing endpoint, unchanged URL)
4. handler: mqtt.request_full_status()
5. handler: uuid = mqtt.get_tray_uuid(N)
6. If uuid is None  → 409 "tray N has no UUID yet"
7. handler: await spoolman.bind_spool_to_tray_uuid(spool_id=..., tray_uuid=uuid)
8. handler: mqtt.activate_filament(...)             # existing
9. 200 with refreshed tray card partial
```

### Flow C — Legacy callers (no `spool_id`)

```
1. POST /activate { tray: 0, filament_id: GFA00, ... }   # spool_id absent
2. handler: mqtt.activate_filament(...)                  # unchanged
3. 200 OK
```

No new logic runs. Zero regression risk for existing integrations.

---

## 5. Error handling

| Case | HTTP | Notes |
| --- | --- | --- |
| `spool_id` absent | n/a | Legacy path; no new errors possible |
| `spool_id` present, tray uuid not available | `409` | MQTT not sent. Detail: which tray, hint to retry after AMS scan |
| `spool_id` present, uuid available, Spoolman PATCH fails | `502` | MQTT not sent. Detail includes Spoolman's response text |
| `spool_id` references nonexistent spool | `502` (from Spoolman 404) | MQTT not sent |
| `bambu_tray_uuid` extra field doesn't yet exist | self-heal | `bind_spool_to_tray_uuid` calls `ensure_spool_extra_fields` first; field is created on first use |
| Bind succeeds, MQTT command fails | `200` with `success:false` (existing) | Bind stays — it's still accurate; idempotent on retry |
| Target spool already has this uuid | `200` | Bind is a no-op (skip the PATCH) |

The principle: **never half-succeed**. We either bind-then-MQTT or
fail-before-MQTT. If MQTT fails after a successful bind, the bind is
still semantically correct (the physical spool *is* in that tray), and
the user's retry will re-attempt the MQTT command without harming the
bind.

---

## 6. Backwards compatibility

- `spool_id` is optional. All existing API consumers continue to work
  unchanged.
- New web-UI partial only fires when count≥2; existing 0/1 paths render
  the same as today plus the silent bind.
- Existing `bambu_tray_uuid` values written manually by users are
  honored — `bind_spool_to_tray_uuid` only clears uuids that conflict
  with the one we're about to write.
- The iOS app continues to work against older helpers (server ignores
  unknown `spool_id` field). One-line change is independently shippable.

---

## 7. Testing

### `tests/test_spoolman.py`

- `TestBindSpoolToTrayUuid`:
  - Sets uuid on target spool when no other spool holds it (single PATCH
    of target).
  - Clears uuid from a previously-bound spool, then sets on target (two
    PATCHes).
  - No-op when target already holds the same uuid (zero PATCHes).
  - Calls `ensure_spool_extra_fields` once before doing anything.

### `tests/test_gateway_activator.py` and `tests/test_mqtt_printer.py`

- `TestGetTrayUuid`:
  - Returns cached uuid for known tray.
  - Returns `None` for unknown tray.
  - Returns `None` when cached `TrayData.tray_uuid` is empty string.

### `tests/test_api.py` (or `test_api_activate.py`)

- `TestActivateLegacyBehavior`:
  - Posting without `spool_id` does not call `bind_spool_to_tray_uuid`
    and proceeds to `activate_filament`.

- `TestActivateWithSpoolId`:
  - Happy path: refresh-status called, bind called, then activate called.
  - Tray uuid `None` → `409`, bind not called, activate not called.
  - Bind raises `httpx.HTTPStatusError(500)` → `502`, activate not
    called.
  - Bind raises `httpx.HTTPStatusError(404)` (unknown spool) → `502`.

### `tests/test_web.py` (or `tests/test_web_assign.py`)

- `TestAssignSpoolToTrayBind`:
  - Posting to `/web/tray/N/assign` with `spool_id` calls
    `bind_spool_to_tray_uuid` before `activate_filament`.
  - Tray uuid `None` → `409`, bind not called, activate not called.
  - Bind raises → handler surfaces `502`, activate not called.

### iOS

No automated test. Manual smoke: tap activate on a spool from
`SpoolDetailView`, observe a successful 200 OR a 409 if the AMS hasn't
reported the tray uuid yet, both with expected toast messages.

---

## 8. Open follow-ups (out of scope)

- Add a "rescue" button next to the 409 message that triggers an AMS
  rescan and retries automatically.
- Surface the current binding in the helper web UI (small "bound to
  uuid abc-…" badge per tray) so users can verify.
- Consider color-hex normalization beyond case-insensitive (e.g., trim
  alpha channel) if real-world Spoolman colors diverge from Bambu colors.
