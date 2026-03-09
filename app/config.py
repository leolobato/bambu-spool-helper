"""Application configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


@dataclass(frozen=True)
class Settings:
    orcaslicer_url: str
    spoolman_url: str
    printer_ip: str
    printer_access_code: str
    printer_serial: str
    default_machine_profile_id: str
    port: int
    detail_fetch_concurrency: int

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.printer_ip and self.printer_access_code and self.printer_serial)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        orcaslicer_url=_env_str("ORCASLICER_URL", "http://orcaslicer:8000").rstrip("/"),
        spoolman_url=_env_str("SPOOLMAN_URL", "http://spoolman:7912").rstrip("/"),
        printer_ip=_env_str("PRINTER_IP"),
        printer_access_code=_env_str("PRINTER_ACCESS_CODE"),
        printer_serial=_env_str("PRINTER_SERIAL"),
        default_machine_profile_id=_env_str("DEFAULT_MACHINE_PROFILE_ID", "GM020"),
        port=_env_int("PORT", 9817),
        detail_fetch_concurrency=max(1, _env_int("DETAIL_FETCH_CONCURRENCY", 10)),
    )
