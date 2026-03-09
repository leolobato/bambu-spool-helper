# bambu-spool-helper

A FastAPI service that links [Spoolman](https://github.com/Donkie/Spoolman) filament inventory to OrcaSlicer/Bambu Lab filament profiles. It provides:

- A **REST API** compatible with the original macOS spool-helper app
- A **web UI** (HTMX) for browsing Spoolman filaments and linking them to Bambu profiles
- **MQTT activation** to push filament settings directly to a Bambu printer's AMS

## How It Works

1. Fetches filament profiles from an [orcaslicer-cli](https://github.com/leolobato/orcaslicer-cli) HTTP API
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
  "tray_info_idx": "GFB99",
  "filament_id": "GFB99",
  "tray": 0,
  "color_hex": "FF0000"
}
```

Tray values: 0-3 for AMS slots, 4 for external spool.

### `POST /reload`

Re-fetches profiles from orcaslicer-cli.

### `GET /web/`

Web interface for browsing and linking filaments.
