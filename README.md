# Bambu Spool Helper

A self-hosted bridge between [Spoolman](https://github.com/Donkie/Spoolman)
filament inventory and Bambu Lab printers. It lets you keep your real spools in
Spoolman, link each one to an OrcaSlicer / Bambu filament profile, and push
those settings straight into the printer's AMS over MQTT — no slicer round-trip
required.

It pairs with [orcaslicer-cli](https://github.com/leolobato/orcaslicer-cli) for
the profile catalog (so you can use your own custom filament profiles, not just
the built-in Bambu ones) and works with any Bambu Lab printer in
**developer / LAN mode**.

This is a Python/Docker port of the macOS [spool-helper](https://github.com/leolobato/spool-helper)
app. Same idea, but headless, multi-user, and runnable on a NAS or
Raspberry Pi alongside Spoolman.

## Features

- **Web UI** (HTMX + Tailwind) for browsing Spoolman filaments and linking them to Bambu profiles
- **REST API** compatible with the original macOS spool-helper app
- **Direct AMS activation** over MQTT — sets filament type, color, and nozzle/bed temperatures on a chosen tray
- **AMS + external spool support** — trays 0–3 for AMS slots, tray 4 for the external spool holder
- **Custom profile support** via orcaslicer-cli — works with user profiles, not just system ones
- **Create profiles from filament JSON** — paste an exported Bambu/OrcaSlicer filament JSON and the UI builds an AMS-assignable profile, prompting for missing fields (e.g. textured-plate temperature)
- **Smart matching** — exact `(setting_id, filament_id)` first, falling back to filament ID, then scoring by material, type, and source priority (user > system)
- **Graceful degradation** — runs fine without printer credentials; only the activation step is skipped
- **Stateless** — all linkage data lives in Spoolman as `bambu_*` extra fields, so nothing is locked to this app

## How It Works

1. On startup, fetches the filament profile catalog from `orcaslicer-cli` and caches it in memory
2. Reads your filament inventory from Spoolman
3. You link each Spoolman filament to a Bambu profile in the web UI; the link is stored back in Spoolman as extra fields (double-JSON-encoded `bambu_filament_id`, `bambu_setting_id`, etc.)
4. When you click **Activate** on a tray, the app publishes an `ams_filament_setting` MQTT command to `device/{serial}/request` over TLS (port 8883), and the printer's AMS picks up the new settings instantly

No printer firmware modifications, no Bambu cloud, no slicer GUI — everything stays on your LAN.

## Quick Start

You'll need:

- A Bambu Lab printer with **Developer Mode** enabled (the access code, IP, and serial are in the printer's network settings)
- A running [Spoolman](https://github.com/Donkie/Spoolman) instance
- A running [orcaslicer-cli](https://github.com/leolobato/orcaslicer-cli) instance

### Run with Docker (recommended)

Released images are published to GHCR on every `v*` tag:

```bash
docker run -d --name bambu-spool-helper \
  -p 9817:9817 \
  -e ORCASLICER_URL=http://10.0.1.9:8070 \
  -e SPOOLMAN_URL=http://10.0.1.9:7912 \
  -e PRINTER_IP=192.168.1.100 \
  -e PRINTER_ACCESS_CODE=your_access_code \
  -e PRINTER_SERIAL=your_serial_number \
  ghcr.io/leolobato/bambu-spool-helper:latest
```

Or with `docker compose` (uses a shared external network so the helper can talk
to other containers by hostname):

```bash
# Create the .env file with your printer credentials, then:
docker network create spoolnet   # shared with orcaslicer-cli and Spoolman
docker compose up -d
```

`docker-compose.yml` defaults `ORCASLICER_URL` to `http://orcaslicer:8000` and
`SPOOLMAN_URL` to `http://spoolman:7912` — both resolved over the `spoolnet`
network. Override them in `.env` if your services live elsewhere.

Open [http://localhost:9817/web/](http://localhost:9817/web/) once it's up.

### Run from source

Requires Python 3.12+.

```bash
git clone https://github.com/leolobato/bambu-spool-helper.git
cd bambu-spool-helper
bash scripts/run-local.sh
```

`scripts/run-local.sh` creates `.venv` if needed, installs `requirements.txt`,
sources `.env`, and starts `uvicorn` with reload enabled.

For local development without a printer, leave `PRINTER_IP`,
`PRINTER_ACCESS_CODE`, and `PRINTER_SERIAL` empty — the web UI still works,
only MQTT activation is skipped.

## Configuration

All configuration is via environment variables (loaded from `.env` if present).

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCASLICER_URL` | `http://orcaslicer:8000` | orcaslicer-cli base URL — required for the profile catalog |
| `SPOOLMAN_URL` | `http://spoolman:7912` | Spoolman base URL — required for filament inventory |
| `PRINTER_IP` | _(empty)_ | Bambu printer IP address — leave empty to disable MQTT |
| `PRINTER_ACCESS_CODE` | _(empty)_ | Printer access code (from printer's network settings) |
| `PRINTER_SERIAL` | _(empty)_ | Printer serial number |
| `DEFAULT_MACHINE_PROFILE_ID` | `GM020` | Machine profile used to filter AMS-assignable filaments (`GM020` = A1 mini). Also accepts the legacy name `DEFAULT_MACHINE_SETTING_ID`. |
| `PORT` | `9817` | HTTP server port |
| `DETAIL_FETCH_CONCURRENCY` | `10` | Max concurrent profile-detail requests against orcaslicer-cli |

Printer variables are optional. Without them the service runs as a read-only
inventory linker — useful for prepping spool data ahead of time, or for running
on a host that can't reach the printer.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Health check + loaded profile count |
| `GET` | `/profiles?search=term` | List filament profiles, optionally filtered case-insensitively |
| `POST` | `/activate` | Send a filament profile to a printer tray over MQTT |
| `POST` | `/reload` | Trigger orcaslicer-cli's `POST /profiles/reload`, then refresh the cached AMS-assignable profile list |
| `GET` | `/web/` | HTMX web interface |

### `POST /activate`

```json
{
  "filament_id": "GFB99",
  "tray": 0,
  "color_hex": "FF0000"
}
```

Tray values: **0–3** for AMS slots, **4** for the external spool holder.

Interactive API docs are available at `/docs` (Swagger UI) and `/redoc`.

## Web UI

The web UI lives at `/web/` and lets you:

- Browse all filaments from Spoolman
- Search and pick a Bambu/OrcaSlicer profile for each one
- See exactly which fields will be written to Spoolman as `bambu_*` extras
- Activate a linked filament directly into any AMS tray (or the external spool)
- Create a new profile by pasting a Bambu/OrcaSlicer filament JSON — if the
  imported profile is missing AMS-required fields (e.g. textured-plate temp),
  the UI prompts for them before saving so the result is always
  AMS-assignable

Imported filament JSON is resolved through orcaslicer-cli first; only when the
resolved profile lacks a valid `filament_type` does the form fall back to
asking for one.

## Releases

Tagged commits matching `v*` (e.g. `v1.1.2`) trigger
`.github/workflows/release.yml`, which:

1. Builds and pushes a Docker image to `ghcr.io/leolobato/bambu-spool-helper`
   tagged with the version and `latest`
2. Creates a GitHub Release with auto-generated notes from the commit history

To cut a release:

```bash
git tag v1.1.2
git push origin v1.1.2
```

## Related Projects

Bambu Spool Helper is the **Spoolman ↔ AMS bridge** in a suite of self-hosted projects that together replace the Bambu Handy app for printers in **Developer Mode** — keeping everything on your LAN, with no Bambu cloud.

**Self-hosted services**

- **[bambu-gateway](https://github.com/leolobato/bambu-gateway)** — Printer control plane and slicing web app. Talks to printers over MQTT/FTPS to monitor status, send commands, and upload jobs. Slices and prints 3MF files from the browser using `orcaslicer-cli`.
- **[orcaslicer-cli](https://github.com/leolobato/orcaslicer-cli)** — Headless OrcaSlicer wrapped in a REST API. Owns the filament/process/machine profile catalog (including custom user profiles) and does the actual slicing. Other services in the suite call it for slicing and profile data.
- **Bambu Spool Helper** — this project.

**iOS apps**

- **[bambu-gateway-ios](https://github.com/leolobato/bambu-gateway-ios)** — Phone client for `bambu-gateway`. Browse printers, import 3MF files (including from MakerWorld), preview G-code, and start prints. Live Activities and push notifications for print state changes.
- **[spool-browser](https://github.com/leolobato/spool-browser)** — Phone client for `bambu-spool-helper` and Spoolman. Browse the spool inventory, link Bambu profiles to spools, activate filaments on the AMS, and print physical spool labels over Bluetooth.

## License

Bambu Spool Helper is available under the MIT License. See [LICENSE](LICENSE)
for details.
