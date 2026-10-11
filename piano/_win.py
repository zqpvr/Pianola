"""Windows backend: ``SendInput`` keystrokes, ``RegisterHotKey`` hotkeys, the
foreground window title and the 1 ms timer.

Roblox listens to the normal Windows keyboard stream, so scancode-level
``SendInput`` events are indistinguishable from a real key press.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

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


def scancode(vk: int) -> int:
    return _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)


def send_keys(items: list[tuple[int, bool]]) -> None:
    """items: list of (virtual key, is_key_down)."""
    buf = (INPUT * len(items))()
    for i, (vk, down) in enumerate(items):
        flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
        buf[i].type = INPUT_KEYBOARD
        buf[i].ki = _KEYBDINPUT(wVk=0, wScan=scancode(vk), dwFlags=flags,
                                time=0, dwExtraInfo=None)
    sent = _user32.SendInput(len(items), buf, ctypes.sizeof(INPUT))
    if sent != len(items):
        raise ctypes.WinError(ctypes.get_last_error())


def prepare() -> None:
    """Nothing to set up; SendInput needs no device."""


def describe() -> str:
    return "Windows SendInput"


def enable_dpi_awareness() -> bool:
    """Opt in to DPI awareness so Windows does not stretch the window bitmap.

    System-aware (1) rather than per-monitor (2): Tk does not handle
    WM_DPICHANGED, so per-monitor would leave it wrongly sized on a second screen.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            return False
    return True


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


# --------------------------------------------------------------------------
# system-wide hotkeys
# --------------------------------------------------------------------------
#
# ``RegisterHotKey`` bindings belong to the thread that created them and are
# delivered as thread messages, so this runs its own pump instead of borrowing
# Tk's.  Callbacks fire on that thread - anything touching a GUI should marshal
# back with ``after``.

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

    def bind(self, combo: str, action: Callable[[], None], name: str = "") -> None:
        """``name`` labels the action for the Linux portal; Windows needs none."""
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
