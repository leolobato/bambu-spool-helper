"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.models import ActivationRecord
from app.routers import api, web
from app.services.mqtt_printer import MQTTPrinterClient
from app.services.orcaslicer import OrcaSlicerClient
from app.services.spoolman import SpoolmanClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    orcaslicer = OrcaSlicerClient(
        base_url=settings.orcaslicer_url,
        machine_id=settings.default_machine_profile_id,
        detail_fetch_concurrency=settings.detail_fetch_concurrency,
    )
    spoolman = SpoolmanClient(settings.spoolman_url)
    mqtt = MQTTPrinterClient(
        ip=settings.printer_ip,
        access_code=settings.printer_access_code,
        serial=settings.printer_serial,
    )

    app.state.settings = settings
    app.state.orcaslicer = orcaslicer
    app.state.spoolman = spoolman
    app.state.mqtt = mqtt
    app.state.recent_activations: list[ActivationRecord] = []

    try:
        machines = await orcaslicer.load_machines()
        loaded = await orcaslicer.load_profiles()
        logger.info("Loaded %d machines and %d filament profiles for %s", len(machines), len(loaded), settings.default_machine_profile_id)
    except Exception:
        logger.exception("Failed to load profiles from OrcaSlicer during startup")

    try:
        mqtt.connect()
    except Exception:
        logger.exception("Failed to initialize MQTT during startup")

    try:
        yield
    finally:
        try:
            mqtt.disconnect()
        except Exception:
            logger.exception("Failed to shut down MQTT cleanly")
        await orcaslicer.close()
        await spoolman.close()


app = FastAPI(title="bambu-spool-helper", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/web/")


app.include_router(api.router)
app.include_router(web.router)
