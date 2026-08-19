"""System-wide hotkeys so the player can be driven while Roblox has focus.

``RegisterHotKey`` bindings belong to the thread that created them and are
delivered as thread messages, so this runs its own pump instead of borrowing
Tk's.  Callbacks fire on that thread - anything touching a GUI should marshal
back with ``after``.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

VK = {f"f{i}": 0x70 + i - 1 for i in range(1, 13)}
VK.update({"escape": 0x1B, "space": 0x20, "home": 0x24, "end": 0x23,
           "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
           "insert": 0x2D, "delete": 0x2E, "pause": 0x13})


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+shift+f1' -> (modifier mask, virtual key)."""
    mods = 0
    key = 0
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in VK:
            key = VK[part]
        elif len(part) == 1:
            key = ord(part.upper())
    if not key:
        raise ValueError(f"unrecognised hotkey: {text!r}")
    return mods | MOD_NOREPEAT, key


class HotkeyManager:
    def __init__(self):
        self._bindings: dict[int, tuple[str, Callable[[], None]]] = {}
        self._pending: list[tuple[int, str, Callable[[], None]]] = []
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.failed: list[str] = []

    def bind(self, combo: str, action: Callable[[], None]) -> None:
        hotkey_id = len(self._pending) + 1
        self._pending.append((hotkey_id, combo, action))

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._pump, name="piano-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None

    def _pump(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        for hotkey_id, combo, action in self._pending:
            try:
                mods, vk = parse_hotkey(combo)
            except ValueError:
                self.failed.append(combo)
                continue
            if _user32.RegisterHotKey(None, hotkey_id, mods, vk):
                self._bindings[hotkey_id] = (combo, action)
            else:
                self.failed.append(combo)      # already claimed by another app
        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                binding = self._bindings.get(msg.wParam)
                if binding:
                    try:
                        binding[1]()
                    except Exception:           # a bad callback must not kill the pump
                        pass
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        for hotkey_id in self._bindings:
            _user32.UnregisterHotKey(None, hotkey_id)
        self._bindings.clear()
