"""Keystroke delivery via ``SendInput``.

Roblox listens to the normal Windows keyboard stream, so scancode-level
``SendInput`` events are indistinguishable from a real key press.  The layer
also owns the shift state: accidentals live on shifted keys, and a natural sent
while shift happens to be down produces the wrong note, so shift is raised and
lowered around each burst rather than left to chance.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT
_user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
_user32.MapVirtualKeyW.restype = wintypes.UINT


# Explicit US-layout table.  Virtual Piano sheets describe physical key
# positions, so we deliberately do not follow the user's active layout.
_CHAR_VK: dict[str, tuple[int, bool]] = {}
for _c in "0123456789":
    _CHAR_VK[_c] = (0x30 + int(_c), False)
for _i in range(26):
    _CHAR_VK[chr(ord("a") + _i)] = (0x41 + _i, False)
    _CHAR_VK[chr(ord("A") + _i)] = (0x41 + _i, True)
for _shifted, _base in zip(")!@#$%^&*(", "0123456789"):
    _CHAR_VK[_shifted] = (0x30 + int(_base), True)


def scancode(vk: int) -> int:
    return _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)


def needs_shift(char: str) -> bool:
    entry = _CHAR_VK.get(char)
    return bool(entry and entry[1])


class Keyboard:
    """Sends key events, tracking every key it is currently holding down.

    ``dry_run`` swaps real input for a recorded log, which is how the test
    harness and the preview mode exercise the whole pipeline safely.
    """

    def __init__(self, dry_run: bool = False, shift_vk: int = VK_LSHIFT):
        self.dry_run = dry_run
        self.shift_vk = shift_vk
        self.log: list[tuple[str, bool]] = []
        self.raw_log: list[list[tuple[int, bool]]] = []
        self._held: set[str] = set()
        self._shift_down = False
        self._lock = threading.Lock()

    # -- raw ---------------------------------------------------------------
    def _send(self, items: list[tuple[int, bool]]) -> None:
        """items: list of (virtual key, is_key_down)."""
        if not items:
            return
        if self.dry_run:
            self.raw_log.append(list(items))
            return
        buf = (INPUT * len(items))()
        for i, (vk, down) in enumerate(items):
            flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
            buf[i].type = INPUT_KEYBOARD
            buf[i].ki = _KEYBDINPUT(wVk=0, wScan=scancode(vk), dwFlags=flags,
                                    time=0, dwExtraInfo=None)
        sent = _user32.SendInput(len(items), buf, ctypes.sizeof(INPUT))
        if sent != len(items):
            raise ctypes.WinError(ctypes.get_last_error())

    def _set_shift(self, want: bool, batch: list[tuple[int, bool]]) -> None:
        if want != self._shift_down:
            batch.append((self.shift_vk, want))
            self._shift_down = want

    # -- note level --------------------------------------------------------
    def press_batch(self, downs: list[str], ups: list[str]) -> None:
        """Apply one instant of the score: releases first, then presses.

        Presses are ordered naturals-before-accidentals so shift toggles at most
        once per instant no matter how mixed the chord is.
        """
        with self._lock:
            batch: list[tuple[int, bool]] = []

            for char in ups:
                entry = _CHAR_VK.get(char)
                if entry and char in self._held:
                    batch.append((entry[0], False))
                    self._held.discard(char)
                    self.log.append((char, False))

            plain = [c for c in downs if c in _CHAR_VK and not needs_shift(c)]
            shifted = [c for c in downs if c in _CHAR_VK and needs_shift(c)]

            if plain:
                self._set_shift(False, batch)
                for char in plain:
                    batch.append((_CHAR_VK[char][0], True))
                    self._held.add(char)
                    self.log.append((char, True))
            if shifted:
                self._set_shift(True, batch)
                for char in shifted:
                    batch.append((_CHAR_VK[char][0], True))
                    self._held.add(char)
                    self.log.append((char, True))

            self._send(batch)

    def release_all(self) -> None:
        """Panic button - lets go of every key this object still holds."""
        with self._lock:
            batch = [(_CHAR_VK[c][0], False) for c in self._held if c in _CHAR_VK]
            self._held.clear()
            if self._shift_down:
                batch.append((self.shift_vk, False))
                self._shift_down = False
            # Both shifts, unconditionally: cheap insurance against a stuck modifier.
            batch.append((VK_LSHIFT, False))
            batch.append((VK_RSHIFT, False))
            self._send(batch)

    @property
    def held(self) -> set[str]:
        return set(self._held)


# --------------------------------------------------------------------------
# window focus + timer resolution
# --------------------------------------------------------------------------

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)


def foreground_title() -> str:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


class TimerResolution:
    """1 ms scheduler granularity for the duration of playback."""

    def __init__(self, period: int = 1):
        self.period = period
        self._active = False

    def __enter__(self):
        try:
            _kernel32.LoadLibraryW("winmm.dll")
            self._winmm = ctypes.WinDLL("winmm")
            if self._winmm.timeBeginPeriod(self.period) == 0:
                self._active = True
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if self._active:
            self._winmm.timeEndPeriod(self.period)
            self._active = False
        return False
