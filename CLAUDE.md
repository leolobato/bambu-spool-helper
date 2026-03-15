# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

FastAPI service that bridges Spoolman filament inventory with Bambu Lab printers. It fetches filament profiles from an orcaslicer-cli HTTP API, lets users link Spoolman filaments to those profiles via a web UI, and sends MQTT commands to the printer's AMS to activate filament settings.

This is a Python/Docker port of a macOS Swift app (`../spool-helper/`). Key differences: profiles come from orcaslicer-cli (not BambuStudio files), activation uses MQTT directly (not BambuStudio config editing), and the UI is Jinja2+HTMX (not SwiftUI).

## Running

```bash
# Local development (uses scripts/run-local.sh which creates .venv, installs deps, sources .env)
bash scripts/run-local.sh

# Or manually (requires Python 3.12+)
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 9817 --reload

# Docker
docker compose up --build
```

Requires external services: orcaslicer-cli (`ORCASLICER_URL`), Spoolman (`SPOOLMAN_URL`), and optionally a Bambu printer (`PRINTER_IP`, `PRINTER_ACCESS_CODE`, `PRINTER_SERIAL`). See `app/config.py` for all env vars. MQTT/printer features degrade gracefully when printer env vars are unset.

## Testing

Tests use `unittest` (no pytest). No external services required — all tests mock HTTP/MQTT.

```bash
# Run all tests
python -m unittest discover tests

# Run a single test file
python -m unittest tests.test_tray_profile_matching

# Run a single test case
python -m unittest tests.test_tray_profile_matching.TestTrayProfileMatching.test_exact_match
```

## Architecture

**Service layer** (`app/services/`): Three clients stored in `app.state` during lifespan:
- `OrcaSlicerClient` — fetches profile list + detail from orcaslicer-cli, caches in memory. Profile detail fields use array-of-strings format (extracted via `_extract_first_int`/`_extract_first_str`).
- `MQTTPrinterClient` — persistent TLS connection (paho-mqtt v2, port 8883) to printer. Publishes `ams_filament_setting` commands to `device/{serial}/request`. Gracefully degrades when unconfigured.
- `SpoolmanClient` — reads filaments and manages `bambu_*` extra fields. Extra field values are double-JSON-encoded (`json.dumps("value")` produces `'"value"'`).

**Router layer** (`app/routers/`):
- `api.py` — REST endpoints: `GET /status`, `GET /profiles`, `POST /activate`, `POST /reload`
- `web.py` — HTMX UI: full page at `/web/`, partials for filament list/detail/profile picker

**Templates** (`app/templates/`): Jinja2 + HTMX + Tailwind CDN. Partials render HTML fragments without base template extension.

**Profile matching** (in `OrcaSlicerClient.find_profile`): exact `(setting_id, filament_id)` first, fallback to `filament_id` only. Secondary scoring in `web.py` uses material match, filament type match, and source priority (user > system).

**Tray mapping**: 0-3 = AMS trays (ams_id=0, tray_id=0-3), 4 = external spool (ams_id=255, tray_id=254).

## Key Gotchas

- **Double-JSON encoding**: Spoolman extra field values are double-JSON-encoded — `json.dumps("value")` produces `'"value"'`. Decoded via `_decode_extra_field()` in `SpoolmanClient`.
- **Profile detail arrays**: OrcaSlicer profile detail fields come as arrays of strings. Use `_extract_first_int`/`_extract_first_str` helpers to extract scalar values.
- **paho-mqtt v2 API**: This project uses paho-mqtt v2 (not v1). The v2 API has different callback signatures and connection patterns.
- **`web.py` is the largest file** (~1800 lines): Contains all HTMX route handlers and the profile selection/matching logic.

## Endpoints

- `GET /status` — health check with profile count
- `GET /profiles?search=term` — list profiles, optional case-insensitive filter
- `POST /activate` — send filament to printer tray via MQTT
- `POST /reload` — re-fetch profiles from orcaslicer-cli
- `GET /web/` — HTMX web UI for linking Spoolman filaments to profiles

## Docker

Uses shared external Docker network `spoolnet` to communicate with orcaslicer-cli and Spoolman containers. Port 9817.
