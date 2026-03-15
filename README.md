# bambu-spool-helper

A FastAPI service that links [Spoolman](https://github.com/Donkie/Spoolman) filament inventory to OrcaSlicer/Bambu Lab filament profiles. It provides:

- A **REST API** compatible with the original macOS spool-helper app
- A **web UI** (HTMX) for browsing Spoolman filaments and linking them to Bambu profiles
- **MQTT activation** to push filament settings directly to a Bambu printer's AMS

## How It Works

1. Fetches filament profiles from an [orcaslicer-cli](../orcaslicer-cli/) HTTP API
2. Reads your filament inventory from Spoolman
3. Lets you link each Spoolman filament to a Bambu profile (stored as Spoolman extra fields)
4. On activation, sends MQTT commands to the printer to set the AMS tray filament type, color, and temperature

## Setup

### Docker Compose (recommended)

Create a `.env` file:

```env
PRINTER_IP=192.168.1.100
PRINTER_ACCESS_CODE=your_access_code
PRINTER_SERIAL=your_serial_number
```

Then:

```bash
docker network create spoolnet  # shared with orcaslicer-cli and Spoolman
docker compose up --build
```

The service will be available at `http://localhost:9817`.

### Local Run

For local development without Docker:

1. Point `.env` at your running services.

```env
ORCASLICER_URL=http://localhost:8070
SPOOLMAN_URL=http://localhost:7912
DEFAULT_MACHINE_PROFILE_ID=GM020
PORT=9817
```

2. If you do not need live printer access while developing, leave these empty so MQTT is skipped:

```env
PRINTER_IP=
PRINTER_ACCESS_CODE=
PRINTER_SERIAL=
```

3. Start the app:

```bash
bash scripts/run-local.sh
```

The script will:
- create `.venv` if needed
- install `requirements.txt` if dependencies are missing
- source `.env`
- run `uvicorn` with reload enabled

Open `http://localhost:9817/web/`.

Notes:
- The app also accepts the old env name `DEFAULT_MACHINE_SETTING_ID`, but `DEFAULT_MACHINE_PROFILE_ID` is the canonical one.
- If MQTT initialization fails locally, the web app still starts; only printer activation is affected.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCASLICER_URL` | `http://orcaslicer:8000` | orcaslicer-cli base URL |
| `SPOOLMAN_URL` | `http://spoolman:7912` | Spoolman base URL |
| `PRINTER_IP` | _(empty)_ | Bambu printer IP address |
| `PRINTER_ACCESS_CODE` | _(empty)_ | Printer access code (from printer settings) |
| `PRINTER_SERIAL` | _(empty)_ | Printer serial number |
| `DEFAULT_MACHINE_PROFILE_ID` | `GM020` | Machine profile for filtering (A1 mini) |
| `PORT` | `9817` | Server port |
| `DETAIL_FETCH_CONCURRENCY` | `10` | Max concurrent profile detail requests |

Printer variables are optional — without them, the service runs but skips MQTT commands.

## API

### `GET /status`

Returns service status and loaded profile count.

### `GET /profiles?search=term`

Lists filament profiles, optionally filtered by name or type.

### `POST /activate`

Activates a filament profile on a printer tray.

```json
{
  "filament_id": "GFB99",
  "tray": 0,
  "color_hex": "FF0000"
}
```

Tray values: 0-3 for AMS slots, 4 for external spool.

### `POST /reload`

Calls `orcaslicer-cli`'s `POST /profiles/reload`, then refreshes the selected machine's cached AMS-assignable profile list in `bambu-spool-helper`.

### `GET /web/`

Web interface for browsing and linking filaments.

Imported filament JSON is resolved through `orcaslicer-cli` first. If the resolved profile does not contain a valid AMS `filament_type`, the web UI prompts for one before saving so the profile remains AMS-assignable.

## License

MIT
