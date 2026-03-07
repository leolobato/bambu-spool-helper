# bambu-spool-helper: Python/Docker Port

## Context

The macOS spool-helper app bridges Spoolman filament inventory with Bambu Lab printers by:
1. Reading filament profiles from BambuStudio config files
2. Exposing an HTTP API (`GET /status`, `GET /profiles`, `POST /activate`)
3. Activating profiles by modifying BambuStudio's config and restarting it
4. Providing a SwiftUI UI for linking Spoolman filaments to BambuStudio profiles

This port replaces macOS-specific parts with Docker-friendly alternatives:
- **Profiles** come from orcaslicer-cli's HTTP API (not BambuStudio files)
- **Activation** sends MQTT commands directly to the printer's AMS (not BambuStudio config editing)
- **Spoolman linking UI** is a web app (Jinja2 + HTMX) instead of SwiftUI

---

## Prerequisite: New endpoint in orcaslicer-cli

The existing `/profiles/filaments` endpoint remains unchanged. We add a **new** detail endpoint that returns the full resolved profile with all fields needed for MQTT activation and the spool-helper API.

### Files to modify in `../orcaslicer-cli/`

**`app/main.py`** — Add `GET /profiles/filaments/{setting_id}` endpoint:
- Uses existing `get_profile("filament", setting_id)` which returns the full resolved+cleaned profile dict
- Returns the raw dict as JSON (no Pydantic model filtering), so all fields are available:
  `filament_id`, `filament_type`, `nozzle_temperature_range_low/high`, `hot_plate_temp`,
  `filament_dev_ams_drying_temperature`, `filament_dev_ams_drying_time`,
  `slow_down_min_speed`, `filament_max_volumetric_speed`, etc.
- 404 if setting_id not found (raises `ProfileNotFoundError`)

**No changes** to `app/models.py` or `app/profiles.py` — existing `FilamentProfile` model and `get_filament_profiles()` stay as-is.

---

## Project Structure

```
bambu-spool-helper/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, lifespan, service init
│   ├── config.py                # Env var configuration
│   ├── models.py                # Pydantic models (API request/response)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── orcaslicer.py        # HTTP client for orcaslicer-cli profiles
│   │   ├── mqtt_printer.py      # MQTT client for Bambu printer AMS
│   │   └── spoolman.py          # HTTP client for Spoolman API
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── api.py               # REST API: /status, /profiles, /activate
│   │   └── web.py               # Web UI routes (Jinja2 + HTMX)
│   └── templates/
│       ├── base.html            # Layout with Tailwind CDN + HTMX
│       ├── index.html           # Main Spoolman linking page
│       └── partials/
│           ├── filament_list.html
│           ├── filament_detail.html
│           └── profile_picker.html
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── CLAUDE.md
```

---

## Configuration (`app/config.py`)

Environment variables:
| Variable | Default | Description |
|----------|---------|-------------|
| `ORCASLICER_URL` | `http://orcaslicer:8000` | orcaslicer-cli base URL |
| `SPOOLMAN_URL` | `http://spoolman:7912` | Spoolman base URL |
| `PRINTER_IP` | _(required)_ | Bambu printer IP |
| `PRINTER_ACCESS_CODE` | _(required)_ | Printer access code |
| `PRINTER_SERIAL` | _(required)_ | Printer serial number |
| `DEFAULT_MACHINE_SETTING_ID` | `GM020` | Machine profile for filtering (A1 mini) |
| `PORT` | `9817` | Server port |

---

## Implementation Details

### 1. `app/services/orcaslicer.py` — Profile Client

- `OrcaSlicerClient(base_url, machine_id)` using `httpx.AsyncClient`
- `load_profiles()`:
  1. `GET /profiles/filaments?machine={machine_id}` → list of `{setting_id, name, filament_type, compatible_printers}`
  2. For each profile, `GET /profiles/filaments/{setting_id}` → full resolved profile with all fields
  3. Build `FilamentProfileResponse` from merged data
- Field extraction from the detail response (array-of-strings format, same as Swift `extractFirstInt`):
  - `nozzle_temp_min` ← first element of `nozzle_temperature_range_low` array, int
  - `nozzle_temp_max` ← first element of `nozzle_temperature_range_high` array, int
  - `bed_temp_min = bed_temp_max` ← first element of `hot_plate_temp` array, int
  - `drying_temp_min/max` ← first two elements of `filament_dev_ams_drying_temperature`, sorted as min/max
  - `drying_time` ← first element of `filament_dev_ams_drying_time`, int
  - `print_speed_min` ← first element of `slow_down_min_speed`, int
  - `print_speed_max` ← first element of `filament_max_volumetric_speed`, int
  - `filament_id` ← `filament_id` field (string, e.g. `"GFL99"`)
  - `source` = `"system"` for all profiles
- Use `asyncio.gather` with concurrency limit to fetch details in parallel (avoid sequential N requests)
- Profiles cached in memory, reloaded via `POST /reload` or web UI button

### 2. `app/services/mqtt_printer.py` — MQTT Client

- `MQTTPrinterClient(ip, access_code, serial)` using `paho-mqtt` v2
- Persistent TLS connection: port 8883, username `"bblp"`, self-signed cert (`CERT_NONE`)
- `loop_start()` for background network thread (compatible with FastAPI async)
- `activate_filament(tray, filament_id, color_hex, nozzle_temp_min, nozzle_temp_max, filament_type)`
  - Tray mapping: 0-3 → `ams_id=0, tray_id=0-3`; 4 → `ams_id=255, tray_id=254`
  - Color: append `"FF"` alpha if 6-char hex
  - MQTT payload:
    ```json
    {"print": {
      "sequence_id": "N", "command": "ams_filament_setting",
      "ams_id": 0, "tray_id": 0,
      "tray_info_idx": "GFL99", "tray_color": "FF0000FF",
      "nozzle_temp_min": 190, "nozzle_temp_max": 230,
      "tray_type": "PLA"
    }}
    ```
  - Publishes to `device/{serial}/request`
- Graceful handling when MQTT not configured (empty PRINTER_IP): activate returns success with warning

### 3. `app/services/spoolman.py` — Spoolman Client

Port of `SpoolmanService.swift` using `httpx.AsyncClient`:
- `get_filaments()` → `GET /api/v1/filament`
- `ensure_extra_fields()` → checks/creates `bambu_filament_id`, `bambu_setting_id`, `bambu_filament_type`
- `link_filament(id, filament_id, setting_id, filament_type)` → `PATCH /api/v1/filament/{id}` with JSON-encoded extra fields
- `unlink_filament(id)` → same PATCH with empty values
- **Critical detail**: values must be double-JSON-encoded (`json.dumps("GFL99")` → `'"GFL99"'`)

### 4. `app/routers/api.py` — REST API (same as spool-helper)

**`GET /status`** → `{"status": "ok", "port": 9817, "profiles_loaded": N}`

**`GET /profiles?search=term`** → Array of profiles filtered by name/filament_type (case-insensitive)

**`POST /activate`** → Request: `{"setting_id", "filament_id", "tray" (0-4), "color_hex"}`
- Profile matching (same as Swift): exact `(setting_id, filament_id)` match first, fallback to `filament_id` only
- Sends MQTT command, returns `{"success", "profile_name", "message"}`
- Tracks recent activations (last 10)

**`POST /reload`** → Re-fetches profiles from orcaslicer-cli

### 5. `app/routers/web.py` — Web UI

**`GET /`** — Main page with sidebar + detail layout

**HTMX partials:**
- `GET /web/filaments?filter=all|linked|unlinked&search=term` — filament list
- `GET /web/filament/{id}` — filament detail panel
- `POST /web/link/{filament_id}` — link to profile (form: `setting_id`)
- `POST /web/unlink/{filament_id}` — unlink
- `GET /web/profiles?search=term` — profile picker

### 6. Templates (Jinja2 + HTMX + Tailwind CDN)

**`base.html`** — Full-height layout, dark mode, Tailwind CDN, HTMX CDN

**`index.html`** — Two-column layout:
- Left sidebar: filter tabs (All/Linked/Unlinked), search, filament list with color swatches
- Right panel: selected filament detail (linked info or profile picker)
- All interactions via HTMX (no full page reloads)

**Partials** render HTML fragments (no base template extension)

### 7. `app/main.py` — App Entry Point

- `asynccontextmanager` lifespan: init services, load profiles, connect MQTT
- Services stored in `app.state` for route access
- Include `api.router` and `web.router`

---

## Docker

### `requirements.txt`
```
fastapi>=0.115
uvicorn[standard]>=0.34
httpx>=0.27
paho-mqtt>=2.1
jinja2>=3.1
```

### `Dockerfile`
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
EXPOSE 9817
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9817"]
```

### `docker-compose.yml`
- Single service, port 9817, env vars from `.env` or inline
- Shared external Docker network with orcaslicer-cli and Spoolman

---

## Implementation Order

1. **orcaslicer-cli change** — Add `GET /profiles/filaments/{setting_id}` detail endpoint (single new route in `main.py`)
2. **Project scaffolding** — config, models, main.py with lifespan
3. **OrcaSlicer client** — Profile fetching and caching
4. **API routes** — `/status`, `/profiles`, `/activate` (without MQTT first)
5. **MQTT client** — Printer connection and AMS activation
6. **Spoolman client** — Link/unlink integration
7. **Web UI** — Templates and web routes
8. **Docker** — Dockerfile and docker-compose.yml

## Verification

1. `curl localhost:9817/status` → check `profiles_loaded > 0`
2. `curl localhost:9817/profiles` → check array with expected fields
3. `curl localhost:9817/profiles?search=PLA` → check filtering
4. `curl -X POST localhost:9817/activate -d '{"setting_id":"...","filament_id":"...","tray":0,"color_hex":"FF0000"}'` → check success + verify AMS tray updated on printer
5. Open `http://localhost:9817/` → verify web UI loads, filament list shows, linking works
6. `docker compose up --build` → verify container starts and connects to orcaslicer-cli + Spoolman
