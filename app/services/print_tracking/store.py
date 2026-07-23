"""Atomic settings/checkpoint ledger for print tracking."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class TrackingStore:
    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.directory / "settings.json"
        self.checkpoint_path = self.directory / "checkpoint.json"
        self.model_path = self.directory / "active.3mf"

    def enabled(self) -> bool:
        data = self._read(self.settings_path)
        return bool(data and data.get("enabled") is True)

    def set_enabled(self, enabled: bool) -> None:
        self._write_json(self.settings_path, {"enabled": bool(enabled)})

    def load(self) -> dict | None:
        data = self._read(self.checkpoint_path)
        if data is None or not self.model_path.is_file():
            return None
        return data

    def save(self, checkpoint: dict) -> None:
        self._write_json(self.checkpoint_path, checkpoint)

    def save_model(self, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(
            dir=self.directory, prefix="model.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.model_path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def model_bytes(self) -> bytes:
        return self.model_path.read_bytes()

    def clear_checkpoint(self) -> None:
        for path in (self.checkpoint_path, self.model_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write_json(self, path: Path, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(
            dir=self.directory, prefix=f"{path.stem}.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
