"""Settings persistence: one JSON file next to the application."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields

from .arrange import ArrangeConfig


def app_dir() -> str:
    """The folder the application lives in.

    A PyInstaller build unpacks itself into a temporary directory that is
    deleted again on exit, so anything meant to persist has to sit beside the
    executable rather than beside this module.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(app_dir(), "settings.json")


@dataclass
class Settings:
    arrange: ArrangeConfig = field(default_factory=ArrangeConfig)
    rate: float = 1.0
    loop: bool = False
    countdown: float = 3.0
    focus_filter: str = "Roblox"
    last_folder: str = ""
    sheet_bpm: float = 120.0
    sheet_units: float = 2.0          # note slots per beat in letter sheets
    hotkeys: dict[str, str] = field(default_factory=lambda: {
        "play_pause": "f1",
        "stop": "f2",
        "restart": "f3",
        "panic": "f4",
        "speed_down": "f5",
        "speed_up": "f6",
    })

    def to_dict(self) -> dict:
        data = asdict(self)
        data["arrange"].pop("tracks", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        settings = cls()
        arrange_data = data.get("arrange", {})
        valid = {f.name for f in fields(ArrangeConfig)}
        for key, value in arrange_data.items():
            if key in valid and key != "tracks":
                setattr(settings.arrange, key, value)
        for f in fields(cls):
            if f.name in ("arrange", "hotkeys"):
                continue
            if f.name in data:
                setattr(settings, f.name, data[f.name])
        settings.hotkeys.update(data.get("hotkeys", {}))
        return settings

    def save(self, path: str = CONFIG_PATH) -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "Settings":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (OSError, ValueError):
            return cls()
