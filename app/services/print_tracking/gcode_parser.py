"""Parse sliced Bambu G-code into per-layer, per-filament extrusion lengths."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

_COMMAND_RE = re.compile(r"^\s*([GMT]\d+)\b(.*?)(?:;.*)?$", re.IGNORECASE)
_PARAM_RE = re.compile(
    r"(?:^|\s)([A-Z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?[A-Z]?)",
    re.IGNORECASE,
)
_TOOL_RE = re.compile(r"^\s*T(\d+)\b", re.IGNORECASE)


def _params(text: str) -> dict[str, str]:
    return {key.upper(): value for key, value in _PARAM_RE.findall(text)}


def _number(raw: str | None) -> float | None:
    if raw is None:
        return None
    match = re.match(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", raw,
    )
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None


def evaluate_gcode(gcode: str) -> dict[int, dict[int, float]]:
    """Return positive deposited millimetres grouped by layer and filament.

    Supports absolute/relative extrusion (M82/M83), G92 resets, retractions,
    linear and arc moves, Bambu M620 tool selection, standard Tn changes, and
    M73 layer markers.  Negative extrusion is never charged.
    """
    layers: dict[int, dict[int, float]] = {}
    layer = 0
    filament: int | None = 0
    relative = False
    e_position = 0.0
    retraction_debt: dict[int, float] = {}

    for raw_line in gcode.splitlines():
        tool = _TOOL_RE.match(raw_line)
        if tool:
            index = int(tool.group(1))
            # Bambu change/unload macros use high pseudo-tool values. They do
            # not identify a printable filament and must not fall through to
            # the helper's external-spool route.
            if index == 1000:
                # T1000 is a printer-mode command emitted after the real
                # M620/Tn selection and before normal printing. It does not
                # unload that selected filament.
                continue
            filament = None if index >= 255 else index
            continue
        match = _COMMAND_RE.match(raw_line)
        if not match:
            continue
        command = match.group(1).upper()
        params = _params(match.group(2))

        if command == "M82":
            relative = False
            continue
        if command == "M83":
            relative = True
            continue
        if command == "G92":
            reset = _number(params.get("E"))
            if reset is not None:
                e_position = reset
            continue
        if command == "M73":
            marker = _number(params.get("L"))
            if marker is not None and marker >= 0:
                layer = int(marker)
            continue
        if command == "M620":
            selected = _number(params.get("S"))
            if selected is None:
                continue
            index = int(selected)
            filament = None if index in {255, 65535} or index > 65000 else index
            continue
        if command not in {"G0", "G1", "G2", "G3"}:
            continue

        value = _number(params.get("E"))
        if value is None:
            continue
        if relative:
            delta = value
            e_position += value
        else:
            delta = value - e_position
            e_position = value
        if filament is None:
            continue
        if delta < 0:
            retraction_debt[filament] = (
                retraction_debt.get(filament, 0.0) - delta
            )
            continue
        if delta == 0:
            continue
        debt = retraction_debt.get(filament, 0.0)
        if debt:
            repaid = min(debt, delta)
            delta -= repaid
            retraction_debt[filament] = debt - repaid
        if delta <= 0:
            continue
        bucket = layers.setdefault(layer, {})
        bucket[filament] = bucket.get(filament, 0.0) + delta

    return {
        number: {
            index: length for index, length in usage.items() if length > 0
        }
        for number, usage in layers.items()
        if usage
    }


def gcode_from_3mf(data: bytes, entry: str | None = None) -> tuple[str, str]:
    """Return ``(entry_name, text)`` from a 3MF zip blob."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        selected = entry if entry in names else None
        if selected is None:
            selected = next(
                (name for name in names if name.lower().endswith(".gcode")),
                None,
            )
        if selected is None:
            raise KeyError("3MF contains no G-code entry")
        return selected, archive.read(selected).decode(
            "utf-8", errors="replace",
        )
