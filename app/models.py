"""Pydantic models for API and web data handling."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FilamentProfileResponse(BaseModel):
    name: str
    filament_id: str
    setting_id: str
    filament_type: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    bed_temp_min: int
    bed_temp_max: int
    drying_temp_min: int
    drying_temp_max: int
    drying_time: int
    print_speed_min: int
    print_speed_max: int
    k: float | None = None
    n: float | None = None
    source: Literal["system", "user"] = "system"


class StatusResponse(BaseModel):
    status: str = "ok"
    port: int
    profiles_loaded: int


class ActivateRequest(BaseModel):
    filament_id: str
    tray: int = Field(ge=0, le=4)
    color_hex: str


class ActivateResponse(BaseModel):
    success: bool
    profile_name: str
    message: str


class ActivationRecord(BaseModel):
    created_at: datetime
    profile_name: str
    tray: int
    color_hex: str
    success: bool


class TrayStatus(BaseModel):
    tray_index: int  # 0-3 for AMS, 4 for external
    tray_type: str = ""
    tray_color: str = ""
    tray_info_idx: str = ""
    tray_sub_brands: str = ""
    tag_uid: str = ""
    nozzle_temp_min: int = 0
    nozzle_temp_max: int = 0
    bed_temp: int = 0
    remain: int = -1
    tray_weight: int = 0
    k: float | None = None
    n: float | None = None
    tray_uuid: str = ""
    cali_idx: int = -1

    @property
    def label(self) -> str:
        if self.tray_index == 4:
            return "External Spool"
        return f"Tray {self.tray_index + 1}"

    @property
    def is_empty(self) -> bool:
        return not self.tray_type and not self.tray_info_idx

    @property
    def color_css(self) -> str:
        raw = self.tray_color.strip().upper()
        if len(raw) >= 6:
            return f"#{raw[:6]}"
        return "#4b5563"


class SpoolmanVendor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    name: str | None = None


class SpoolmanFilament(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str | None = None
    material: str | None = None
    color_hex: str | None = Field(default=None, alias="color_hex")
    vendor: SpoolmanVendor | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        parts = [self.vendor.name if self.vendor else None, self.name]
        compact = [part for part in parts if part]
        if compact:
            return " ".join(compact)
        return f"Filament #{self.id}"

    @property
    def color_css(self) -> str:
        raw = (self.color_hex or "").lstrip("#")
        if len(raw) >= 6:
            return f"#{raw[:6]}"
        return "#4b5563"

    def _decode_extra_field(self, key: str) -> str | None:
        raw = self.extra.get(key, "")
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, str) and decoded:
                return decoded
            return None
        except json.JSONDecodeError:
            return raw or None

    @property
    def ams_filament_id(self) -> str | None:
        return self._decode_extra_field("ams_filament_id")

    @property
    def ams_filament_type(self) -> str | None:
        return self._decode_extra_field("ams_filament_type")

    @property
    def is_linked(self) -> bool:
        return bool(self.ams_filament_id)


class SpoolmanSpool(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    filament: SpoolmanFilament
    remaining_weight: float | None = None
    remaining_length: float | None = None
    archived: bool = False

    @property
    def display_name(self) -> str:
        return self.filament.display_name

    @property
    def color_css(self) -> str:
        return self.filament.color_css
