"""Operator-facing names for flattened helper tray indices."""

from __future__ import annotations

import re

EXTERNAL_HELPER_TRAY = 4
TRAYS_PER_AMS = 4
_LEGACY_TRAY_REFERENCE_RE = re.compile(r"\b[Tt]ray (-?\d+)\b")


def _ams_name(index: int) -> str:
    """Return spreadsheet-style AMS names: A..Z, AA..AZ, ..."""
    name = ""
    value = index
    while True:
        value, remainder = divmod(value, 26)
        name = chr(ord("A") + remainder) + name
        if value == 0:
            return name
        value -= 1


def tray_label(tray_index: int) -> str:
    """Format a helper tray index as A1..A4 or External Spool.

    The current helper protocol reserves flattened index 4 for the external
    spool. Higher indices retain the general AMS-letter/one-based-slot format
    for forward-compatible operator messages.
    """
    try:
        index = int(tray_index)
    except (TypeError, ValueError):
        return "Unknown tray"
    if index == EXTERNAL_HELPER_TRAY:
        return "External Spool"
    if index < 0:
        return "Unknown tray"
    ams_index, slot_index = divmod(index, TRAYS_PER_AMS)
    return f"{_ams_name(ams_index)}{slot_index + 1}"


def migrate_legacy_tray_references(message: str) -> str:
    """Convert persisted pre-label messages such as ``tray 2`` to ``A3``."""
    return _LEGACY_TRAY_REFERENCE_RE.sub(
        lambda match: tray_label(int(match.group(1))),
        str(message),
    )
