"""Settings persistence: one JSON file next to the application."""

from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
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


@functools.cache
def data_dir() -> str:
    """Where settings, the log and saved sheets go.

    Beside the application when that folder is writable, which keeps a copy on
    a USB stick self-contained; otherwise the per-user config folder, for a copy
    installed somewhere read-only such as Program Files or /usr/local/bin.
    ``PIANOLA_HOME`` overrides both.  Writability is tested by writing, because
    on Windows ``os.access`` only reads the read-only flag and ignores ACLs.
    """
    override = os.environ.get("PIANOLA_HOME")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    here = app_dir()
    try:
        with tempfile.TemporaryFile(dir=here):
            return here
    except OSError:
        pass
    if sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Pianola")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                            "pianola")
    os.makedirs(base, exist_ok=True)
    return base


CONFIG_PATH = os.path.join(data_dir(), "settings.json")

# Roblox's own client on Windows; on Linux the client is Sober, whose window
# carries its own name.
DEFAULT_WINDOW = "Roblox" if sys.platform == "win32" else "Sober"
WINDOW_PRESETS = (("Roblox", "Virtual Piano") if sys.platform == "win32"
                  else ("Sober", "Roblox", "Virtual Piano"))


@dataclass
class Settings:
    arrange: ArrangeConfig = field(default_factory=ArrangeConfig)
    rate: float = 1.0
    loop: bool = False
    countdown: float = 3.0
    focus_filter: str = DEFAULT_WINDOW
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
        # Roblox's own client does not run on Linux, so a saved "Roblox" there
        # is the old default and can never match; Sober is the client.
        if sys.platform != "win32" and settings.focus_filter == "Roblox":
            settings.focus_filter = DEFAULT_WINDOW
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
