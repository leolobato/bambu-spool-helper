# Filament Usage Tracking

## Non-technical summary

Filament usage tracking keeps Spoolman’s remaining-spool values up to date
while a Bambu printer is printing.

When tracking is enabled, bambu-spool-helper watches the printer through
bambu-gateway. It reads the exact extrusion instructions from the print’s 3MF
file, waits for each layer to finish, and then tells Spoolman how much filament
the completed layer used. Spoolman converts that length into weight using the
filament diameter and density stored in its inventory.

The important safety rules are:

- The current, partially printed layer is not charged.
- A successful print charges the final layer when the printer reports
  `FINISH`.
- A cancelled or failed print charges only layers that were already confirmed
  complete.
- Restarting either service does not normally charge the same layer twice.
- A retained completed/failed job event is ignored unless a matching recovery
  checkpoint proves that work is still owed.
- If a tray, tray-to-filament route, or Spoolman binding changes ambiguously
  during a print, tracking stops charging that affected filament instead of
  guessing.
- If an externally started print has no retrievable 3MF file, the UI reports
  that tracking is unavailable for that job. It does not estimate usage.

Tracking is off by default. Enable it with the **Filament usage tracking**
toggle on the **Settings** screen. The setting persists across restarts.

## Components and data flow

The feature is split between bambu-gateway and bambu-spool-helper:

```text
Bambu printer
    │ partial LAN MQTT or cloud reports
    ▼
bambu-gateway
    │ accumulated SSE snapshots + validated active 3MF
    ▼
bambu-spool-helper
    │ parse G-code, confirm completed layers, resolve tray bindings
    ▼
Spoolman
    └─ PUT /api/v1/spool/{id}/use {"use_length": millimetres}
```

bambu-gateway owns printer transport details. bambu-spool-helper receives the
same tracking interface whether the printer is connected through LAN MQTT or
the gateway’s cloud transport.

Spoolman remains authoritative for spool inventory and tray bindings. The
helper stores only a small local settings and recovery ledger.

## Gateway interface

### Print-event stream

The helper connects to:

```http
GET /api/printers/{printer_id}/print-events
Accept: text/event-stream
```

Each `snapshot` event is a complete accumulated view rather than a raw,
possibly partial firmware message. A new subscriber receives the latest
snapshot immediately.

Example:

```text
event: snapshot
data: {
  "schema_version": 1,
  "sequence": 42,
  "printer_id": "01P00A...",
  "job_key": "gateway:81f2...:1:part.3mf",
  "task_id": "12345",
  "subtask_id": "67890",
  "job_name": "part.3mf",
  "state": "RUNNING",
  "layer": {"current": 18, "total": 120},
  "gcode_entry": "Metadata/plate_1.gcode",
  "ams_mapping": [0, 2],
  "active_tray": 2,
  "source": {
    "available": true,
    "kind": "registered",
    "filename": "part.3mf",
    "url": null,
    "reason": null
  },
  "updated_at": 1784790000.0
}
```

The broker:

- recursively accumulates partial printer reports;
- publishes the same schema for LAN and cloud printers;
- gives each subscriber a bounded queue;
- replaces an old queued snapshot with the newest snapshot when a consumer is
  slow;
- sends SSE comment keepalives during quiet periods;
- keeps a LAN MQTT connection leased while an SSE subscriber exists;
- clears accumulated job-specific fields at each detected job boundary; and
- retains only the active job’s registered 3MF in memory.

`job_key` is the identity used for checkpoints and file validation.
Gateway-submitted files receive a hash-based key that remains stable when the
printer later reports its own task identifiers. External jobs use the
available printer task identity, start time, and job name.

### Active 3MF endpoint

The helper downloads the source from:

```http
GET /api/printers/{printer_id}/current-job/file?job_key={job_key}
```

The gateway compares the requested key with the active snapshot:

- `200` returns the active 3MF.
- `404` means there is no retrievable active source.
- `409` means the requested job is stale and another job is active.
- `502` means an HTTP or printer FTPS download failed.

Source handling:

1. Files submitted through the gateway are registered before the print
   command is sent and retained as active in-memory bytes.
2. A reported `http://` or `https://` source is fetched over HTTP.
3. A reported `file://`, `ftp://`, or `ftps://` source is read from the
   printer over implicit FTPS using the configured printer credentials.
4. When Bambu Studio reports only a bare `.3mf` `gcode_file` name, the gateway
   resolves that explicit filename in the printer’s standard `/cache`
   directory and reads it over FTPS.
5. If no source is usable, the snapshot explicitly reports
   `source.available: false`.

## G-code accounting

The helper opens the 3MF as a ZIP archive and reads the snapshot’s
`gcode_entry`. If that entry is absent, it uses the first `.gcode` entry in the
archive.

The parser produces:

```python
{
    layer_number: {
        filament_index: deposited_length_mm,
    },
}
```

It understands:

- `M73 L...` layer markers;
- `M620 S...` Bambu filament changes;
- standard `T...` tool changes;
- `M82` absolute extrusion;
- `M83` relative extrusion;
- `G92 E...` extrusion-coordinate resets;
- extrusion on `G0`, `G1`, `G2`, and `G3`;
- retractions and subsequent unretractions;
- unload markers such as `M620 S255` and `T255`;
- Bambu’s `T1000` printer-mode command, which preserves the preceding real
  filament selection;
- sparse filament indices; and
- malformed or unrelated lines, which are ignored.

Only positive deposited extrusion is counted. Retraction length becomes a
debt that must be repaid by a later positive move before that move counts as
newly deposited material.

## Completed-layer rule

If the printer reports that it is currently printing layer `N`, layers below
`N` are considered complete. Layer `N` is still partial and is not charged.

For example:

| Printer event | Newly chargeable layers |
|---|---|
| Starts at layer 0 | None |
| Advances to layer 1 | Layer 0 |
| Jumps from layer 1 to layer 4 | Layers 1, 2, and 3 |
| Fails while on layer 4 | None beyond layer 3 |
| Reports `FINISH` | Every uncharged layer, including the final layer |

Skipped events are safe because each gateway event is a complete snapshot. A
matching checkpoint lets the helper charge the missing completed layers after
reconnection.

If there is no matching checkpoint—for example, tracking was just enabled
mid-print—the helper establishes the current layer as its baseline and does
not backfill earlier layers.

Terminal snapshots are a special case. The gateway retains the latest complete
snapshot so reconnecting clients can recover, but the helper only processes a
finished, cancelled, or failed snapshot when the same job has a persisted
checkpoint. With no matching checkpoint, it does not fetch the 3MF or charge
any tail layers.

## Resolving a G-code filament to a Spoolman spool

For every filament index found in the G-code:

1. Read the corresponding printer tray from `ams_mapping`.
2. Normalize printer external-spool values `-1`, `254`, and `255` to helper
   tray `4`.
3. Read that tray’s real RFID UUID or synthetic
   `slot:{printer_serial}:{tray}` UUID.
4. Find the single Spoolman spool whose `bambu_tray_uuid` extra field matches
   that UUID.
5. Save the resolved filament → tray → UUID → spool route in the checkpoint.

Some Bambu Studio single-filament jobs omit `ams_mapping` but report the active
tray. For those jobs only, the helper routes the sole G-code filament through
that explicitly reported tray and exposes an informational warning. A
multicolor job without `ams_mapping` is blocked rather than inferred.

The route is revalidated against a fresh gateway `/api/ams` response before
every layer delivery, including during unattended prints. If that refresh
fails, delivery pauses with an error and is retried from the checkpoint; stale
cached tray data is not treated as confirmation.

The affected filament is blocked for the rest of the job if:

- the tray UUID changes;
- the `ams_mapping` entry changes;
- the UUID resolves to a different Spoolman spool;
- no tray UUID is available; or
- the binding is absent or ambiguous.

Other, unambiguous filaments in a multicolor layer continue to be charged.

If multiple G-code filament indices resolve to the same spool in one layer,
their lengths are added together. The helper sends at most one Spoolman use
request per spool for that layer.

## Spoolman delivery and duplicate protection

For each spool/layer delivery, the helper:

1. Reads the spool’s current `used_length`.
2. Writes a pending-delivery record to disk.
3. Calls:

   ```http
   PUT /api/v1/spool/{spool_id}/use
   Content-Type: application/json

   {"use_length": 123.45}
   ```

4. Marks the delivery complete and updates the checkpoint’s per-spool total
   after a successful response.

If the connection fails after the request may have reached Spoolman, the
outcome is ambiguous. Before retrying, the helper compares the current
`used_length` with:

- the value recorded before the request; and
- the expected value after applying the request.

If the expected value is present, the request is treated as delivered. If the
old value is present, the request can be retried. If neither value matches,
automatic charging stops for that spool and a warning is exposed rather than
risking a duplicate deduction.

This reduces duplicate charging but cannot provide a distributed transaction
across the helper and Spoolman. Operators should investigate any ambiguity
warning before manually changing spool usage.

If the printer starts a replacement job while an earlier job still has a
pending delivery, the helper keeps and reconciles the earlier checkpoint
before creating the replacement job’s checkpoint. The new job begins from its
then-current layer, so waiting for reconciliation does not cause backfill.

## Persistent files

`PRINT_TRACKING_DIR` contains:

| File | Purpose |
|---|---|
| `settings.json` | Persisted `enabled` toggle |
| `checkpoint.json` | Active job identity, mappings, routes, layer progress, deliveries, totals, and warnings |
| `active.3mf` | Cached source for the active checkpoint |

Writes use a temporary file, `fsync`, and atomic rename.

No `settings.json` means tracking is disabled. Removing or corrupting a
checkpoint does not cause historical usage to be backfilled: the next valid
snapshot starts from its current layer.

Docker Compose mounts a named volume at `/data` and sets:

```env
PRINT_TRACKING_DIR=/data/print_tracking
```

For local development, the default is:

```env
PRINT_TRACKING_DIR=./data/print_tracking
```

## Enabling and disabling

The Settings UI sends the toggle to an HTMX endpoint. Programmatic clients can
use:

```http
PUT /api/print-tracking/settings
Content-Type: application/json

{"enabled": true}
```

When enabled, the helper opens the SSE stream. If a print is already running
and there is no matching checkpoint, tracking starts at the current layer.

When disabled, the helper:

1. waits for an in-flight Spoolman request to finish;
2. stops the SSE consumer;
3. clears the active checkpoint and cached 3MF; and
4. does not backfill the disabled interval if tracking is re-enabled.

## Status and UI

`GET /status` includes a `print_tracking` object:

```json
{
  "available": true,
  "enabled": true,
  "connected": true,
  "active": true,
  "job_key": "gateway:81f2...:1:part.3mf",
  "job_name": "part.3mf",
  "state": "RUNNING",
  "current_layer": 18,
  "completed_through": 17,
  "total_layers": 120,
  "spool_totals_mm": {"42": 3845.7},
  "pending_delivery": null,
  "warnings": [],
  "last_event": "2026-07-23T12:34:56+00:00",
  "last_error": null
}
```

Meaning of the main fields:

| Field | Meaning |
|---|---|
| `available` | Gateway URL and printer serial are configured |
| `enabled` | Persisted operator setting |
| `connected` | SSE connection is currently open |
| `active` | A 3MF is parsed and an active checkpoint exists |
| `current_layer` | Layer currently reported by the printer |
| `completed_through` | Highest layer fully processed by the helper |
| `spool_totals_mm` | Successfully delivered usage for the current or most recent job |
| `pending_delivery` | Request whose outcome still needs reconciliation |
| `warnings` | Filaments or spools intentionally blocked to avoid guessing |
| `last_error` | Latest gateway, source, parsing, or delivery error |

Missing gateway configuration, an inaccessible 3MF, and an unbound spool are
non-fatal. They appear in this status block and in the tray UI.

## Configuration checklist

The helper needs:

```env
BAMBU_GATEWAY_URL=http://bambu-gateway:4844
PRINTER_SERIAL=your_printer_serial
PRINT_TRACKING_DIR=/data/print_tracking
```

For each spool that should be charged:

- the spool must exist in Spoolman;
- it must be bound to the loaded tray through `bambu_tray_uuid`; and
- its filament should have valid diameter and density values so Spoolman can
  derive used weight.

The gateway must be able to observe the target printer. External jobs also
need either a reachable reported HTTP(S) source or printer IP/access-code
credentials for FTPS retrieval.

## Troubleshooting

### Tracking says “Unavailable”

Set both `BAMBU_GATEWAY_URL` and `PRINTER_SERIAL`, then restart the helper.

### Tracking is enabled but disconnected

Verify that the helper can reach:

```text
{BAMBU_GATEWAY_URL}/api/printers/{PRINTER_SERIAL}/print-events
```

The client reconnects with exponential backoff, up to 60 seconds.

### The print is visible but tracking is inactive

Inspect `source` in the gateway’s latest event. External cloud jobs sometimes
do not expose a retrievable 3MF. These jobs are intentionally not estimated.

### A spool is not being charged

Check `warnings` in `GET /status` or the tray UI. Confirm that:

- the correct spool is bound to the tray;
- the tray UUID has not changed;
- the AMS mapping still matches the route captured at print start; and
- only one spool has the relevant `bambu_tray_uuid`.

### A pending delivery remains

The next matching snapshot or reconnect attempts reconciliation. Do not
manually adjust the spool while a request is pending; that can make the result
ambiguous and intentionally stop further automatic charging.

### Usage before enabling or after disabling is missing

This is expected. The helper never backfills an interval for which it has no
matching checkpoint.

## Known boundaries

- The helper tracks one configured printer in this version.
- There is no print-history screen; visibility is through `/status`, the tray
  UI, and logs.
- The gateway retains a submitted active 3MF in memory, not as durable print
  history.
- Exact accounting depends on a retrievable sliced 3MF with usable layer and
  tool markers.
- Live printer acceptance still requires testing with the actual printer,
  gateway transport, and Spoolman deployment.
