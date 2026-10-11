"""Linux backend: a ``/dev/uinput`` virtual keyboard, evdev hotkeys, and a
best-effort foreground window title.

Typing goes through the kernel rather than any display server, so it behaves the
same on X11 and Wayland and reaches Wine, Proton and native games alike.  The
price is permissions: ``/dev/uinput`` has to be writable by you, and the hotkeys
read ``/dev/input/event*``, which normally means being in the ``input`` group.

The rest of the package identifies keys by Windows virtual-key code, so this
module translates those to evdev codes at the edge.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import select
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# -- evdev constants ---------------------------------------------------------
EV_SYN, EV_KEY = 0x00, 0x01
SYN_REPORT = 0

KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT = 29, 42, 56
KEY_RIGHTSHIFT, KEY_RIGHTCTRL, KEY_RIGHTALT = 54, 97, 100
KEY_F1 = 59

_ROW_KEYS = {
    **{c: 16 + i for i, c in enumerate("qwertyuiop")},
    **{c: 30 + i for i, c in enumerate("asdfghjkl")},
    **{c: 44 + i for i, c in enumerate("zxcvbnm")},
    **{c: 2 + i for i, c in enumerate("1234567890")},
}


def _evdev_code(vk: int) -> int:
    """Windows virtual-key code -> evdev key code."""
    if vk == 0xA0:
        return KEY_LEFTSHIFT
    if vk == 0xA1:
        return KEY_RIGHTSHIFT
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return _ROW_KEYS[chr(vk).lower()]
    raise KeyError(vk)


# -- ioctl helpers -----------------------------------------------------------
def _ioc(direction: int, kind: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2
UI_DEV_CREATE = _ioc(_IOC_NONE, "U", 1, 0)
UI_DEV_DESTROY = _ioc(_IOC_NONE, "U", 2, 0)
UI_DEV_SETUP = _ioc(_IOC_WRITE, "U", 3, 92)
UI_SET_EVBIT = _ioc(_IOC_WRITE, "U", 100, 4)
UI_SET_KEYBIT = _ioc(_IOC_WRITE, "U", 101, 4)

_EVENT = struct.Struct("llHHi")           # struct input_event on 64-bit Linux


def _eviocgbit(ev: int, length: int) -> int:
    return _ioc(_IOC_READ, "E", 0x20 + ev, length)


EVIOCGNAME = _ioc(_IOC_READ, "E", 0x06, 256)


# -- typing ------------------------------------------------------------------
class _VirtualKeyboard:
    def __init__(self) -> None:
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise RuntimeError(f"Cannot open /dev/uinput: {exc.strerror} (see README, Linux)") from exc
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for vk in [0xA0, 0xA1, *range(0x30, 0x3A), *range(0x41, 0x5B)]:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, _evdev_code(vk))
            name = b"Pianola virtual keyboard".ljust(80, b"\0")
            # bustype USB, made-up vendor/product: some games ignore bus 0 devices.
            setup = struct.pack("HHHH80sI", 0x03, 0x1209, 0x0001, 1, name, 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError as exc:            # UI_DEV_SETUP needs Linux 4.5 or newer
            os.close(self.fd)
            self.fd = -1
            raise RuntimeError(f"Cannot create the virtual keyboard: {exc.strerror}") from exc
        time.sleep(0.25)                  # let the compositor notice the new device
        atexit.register(self.close)
        log.info("virtual keyboard created on /dev/uinput")

    def emit(self, items: list[tuple[int, bool]]) -> None:
        """One write per burst, so a chord reaches the system as a single report."""
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        data = b"".join(_EVENT.pack(sec, usec, EV_KEY, _evdev_code(vk), int(down))
                        for vk, down in items)
        data += _EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0)
        os.write(self.fd, data)

    def close(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1


_device: _VirtualKeyboard | None = None
_device_lock = threading.Lock()


def prepare() -> None:
    """Create the virtual keyboard now rather than on the first note."""
    global _device
    with _device_lock:
        if _device is None or _device.fd < 0:
            _device = _VirtualKeyboard()


def send_keys(items: list[tuple[int, bool]]) -> None:
    if _device is None or _device.fd < 0:
        if not any(down for _, down in items):
            # Releases only, as from stop or panic: nothing can be held on a
            # device that does not exist, so do not create one to say so.
            log.debug("no virtual keyboard yet; skipped %d releases", len(items))
            return
        prepare()
    assert _device is not None
    _device.emit(items)


def enable_dpi_awareness() -> bool:
    return True


def describe() -> str:
    env = os.environ
    session = ", ".join(f"{k}={env[k]}" for k in
                        ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "WAYLAND_DISPLAY", "DISPLAY")
                        if k in env)
    probe = _probe.__name__.removeprefix("_title_") if _probe else "none, guard off"
    return f"Linux uinput; focus probe: {probe}; {session or 'no session variables'}"


# -- focus -------------------------------------------------------------------
_failures: dict[str, str] = {}


def _run(*cmd: str) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1.0)
        failure = f"exit {out.returncode}: {out.stderr.strip()[:200]}" if out.returncode else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        out, failure = None, str(exc)
    if failure != _failures.get(cmd[0], ""):       # log changes, not every poll
        _failures[cmd[0]] = failure
        if failure:
            log.warning("%s failed: %s", cmd[0], failure)
        else:
            log.info("%s working again", cmd[0])
    return None if failure or out is None else out.stdout


def _sway_focused(node: dict) -> str | None:
    if node.get("focused"):
        return node.get("name") or ""
    for child in (*node.get("nodes", ()), *node.get("floating_nodes", ())):
        found = _sway_focused(child)
        if found is not None:
            return found
    return None


def _title_hyprland() -> str | None:
    out = _run("hyprctl", "-j", "activewindow")
    return json.loads(out).get("title", "") if out and out.strip() else None


def _title_sway() -> str | None:
    out = _run("swaymsg", "-t", "get_tree")
    return _sway_focused(json.loads(out)) if out else None


def _title_kdotool() -> str | None:
    out = _run("kdotool", "getactivewindow", "getwindowname")
    return out.strip() if out is not None else None


def _title_xdotool() -> str | None:
    out = _run("xdotool", "getactivewindow", "getwindowname")
    return out.strip() if out is not None else None


def _focus_probe() -> Callable[[], str | None] | None:
    """Pick the tool that can see the focused window in this session.

    Wayland has no portable way to ask, so this goes by the desktop actually
    running rather than by which tools happen to be installed: a probe aimed at
    the wrong compositor fails every time, and a failing probe keeps the guard
    closed.  ``xdotool`` only counts on X11, where it sees every window; under
    Wayland it would report a stale XWayland window.
    """
    env = os.environ
    wayland = env.get("XDG_SESSION_TYPE") == "wayland" or "WAYLAND_DISPLAY" in env
    candidates: list[tuple[str, Callable[[], str | None]]] = []
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        candidates.append(("hyprctl", _title_hyprland))
    if env.get("SWAYSOCK"):
        candidates.append(("swaymsg", _title_sway))
    if "KDE" in env.get("XDG_CURRENT_DESKTOP", "").upper():
        candidates.append(("kdotool", _title_kdotool))
    if not wayland and env.get("DISPLAY"):
        candidates.append(("xdotool", _title_xdotool))
    for tool, probe in candidates:
        if shutil.which(tool):
            return probe
    return None


class _FocusWatcher:
    """Polls the focused window's title on its own thread.

    Every answer costs a process and sometimes a D-Bus round trip, which the
    scheduler cannot afford mid-song, so it only ever reads the last answer.
    Polling stops a couple of seconds after the last question and resumes on the
    next one, which then waits for a fresh answer rather than trusting a stale one.
    """

    POLL = 0.2
    IDLE = 2.0

    def __init__(self, probe: Callable[[], str | None]):
        self._probe = probe
        self._title = ""
        self._asked = 0.0
        self._fresh = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def title(self) -> str:
        with self._lock:
            self._asked = time.monotonic()
            if self._thread is None:
                self._title = ""
                self._fresh.clear()
                self._thread = threading.Thread(target=self._poll, name="piano-focus",
                                                daemon=True)
                self._thread.start()
                log.debug("focus watcher started")
        self._fresh.wait(timeout=1.5)
        return self._title

    def _poll(self) -> None:
        while True:
            try:
                title = self._probe()
            except Exception:                 # malformed output must not kill the watcher
                log.exception("focus probe crashed")
                title = None
            title = title or ""               # a failed probe keeps the guard closed
            if title != self._title:
                log.info("focused window: %r", title)
            self._title = title
            self._fresh.set()
            time.sleep(self.POLL)
            with self._lock:
                if time.monotonic() - self._asked > self.IDLE:
                    self._thread = None
                    log.debug("focus watcher idle")
                    return


_probe = _focus_probe()
_watcher = _FocusWatcher(_probe) if _probe else None


def foreground_title() -> str | None:
    """Title of the focused window, or ``None`` when this session cannot say."""
    return _watcher.title() if _watcher else None


class TimerResolution:
    """Linux sleeps accurately already; kept so the player needs no platform checks."""

    def __init__(self, period: int = 1):
        self.period = period

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- hotkeys -----------------------------------------------------------------
_MOD_KEYS = {
    KEY_LEFTCTRL: "ctrl", KEY_RIGHTCTRL: "ctrl",
    KEY_LEFTSHIFT: "shift", KEY_RIGHTSHIFT: "shift",
    KEY_LEFTALT: "alt", KEY_RIGHTALT: "alt",
}

_NAMED_KEYS = {f"f{i}": KEY_F1 + i - 1 for i in range(1, 11)}
_NAMED_KEYS.update({"f11": 87, "f12": 88, "escape": 1, "space": 57, "home": 102,
                    "end": 107, "left": 105, "right": 106, "up": 103, "down": 108,
                    "insert": 110, "delete": 111, "pause": 119})


def parse_hotkey(text: str) -> tuple[frozenset[str], int]:
    """'ctrl+shift+f1' -> ({'ctrl', 'shift'}, evdev code)."""
    mods: set[str] = set()
    key = 0
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods.add("ctrl")
        elif part in ("alt", "shift"):
            mods.add(part)
        elif part in _NAMED_KEYS:
            key = _NAMED_KEYS[part]
        elif len(part) == 1 and part in _ROW_KEYS:
            key = _ROW_KEYS[part]
    if not key:
        raise ValueError(f"unrecognised hotkey: {text!r}")
    return frozenset(mods), key


def _keyboards() -> list[int]:
    """Open every evdev node that looks like a real keyboard (has F1)."""
    fds = []
    try:
        names = sorted(n for n in os.listdir("/dev/input") if n.startswith("event"))
    except OSError:
        return fds
    length = 96                              # bytes covering KEY_MAX bits
    for name in names:
        try:
            fd = os.open(f"/dev/input/{name}", os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            caps = bytearray(length)
            fcntl.ioctl(fd, _eviocgbit(EV_KEY, length), caps)
            if caps[KEY_F1 // 8] & (1 << (KEY_F1 % 8)):
                label = bytearray(256)
                fcntl.ioctl(fd, EVIOCGNAME, label)
                log.info("hotkeys: watching %s (%s)", name,
                         label.split(b"\0")[0].decode(errors="replace"))
                fds.append(fd)
                continue
        except OSError:
            pass
        os.close(fd)
    return fds


class HotkeyManager:
    """Watches the keyboards for the transport keys.

    Unlike ``RegisterHotKey`` this only observes: the game still receives F1 and
    friends.  Nothing here grabs the device, so typing is never intercepted.
    """

    def __init__(self):
        self._pending: list[tuple[str, Callable[[], None]]] = []
        self._thread: threading.Thread | None = None
        self._wake_w = -1
        self.failed: list[str] = []
        self.reason = ""

    def bind(self, combo: str, action: Callable[[], None]) -> None:
        self._pending.append((combo, action))

    def start(self) -> None:
        if self._thread:
            return
        self.failed, self.reason = [], ""
        bindings: dict[tuple[frozenset[str], int], tuple[str, Callable[[], None]]] = {}
        for combo, action in self._pending:
            try:
                bindings[parse_hotkey(combo)] = (combo, action)
            except ValueError:
                self.failed.append(combo)
        fds = _keyboards()
        if not fds:
            self.failed.extend(c for c, _ in self._pending if c not in self.failed)
            self.reason = ("no readable keyboard in /dev/input; add yourself to the "
                           "'input' group and log in again to enable hotkeys")
            log.warning("hotkeys off: %s", self.reason)
            return
        wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._pump, args=(wake_r, fds, bindings),
                                        name="piano-hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            os.close(self._wake_w)            # EOF on the pipe wakes the pump
            self._wake_w = -1
            self._thread.join(timeout=1.0)
        self._thread = None

    def _pump(self, wake_r: int, fds: list[int], bindings) -> None:
        held: set[str] = set()
        try:
            while True:
                ready, _, _ = select.select([wake_r, *fds], [], [])
                if wake_r in ready:
                    return
                for fd in ready:
                    try:
                        data = os.read(fd, _EVENT.size * 64)
                    except BlockingIOError:
                        continue
                    except OSError:
                        fds.remove(fd)        # unplugged
                        os.close(fd)
                        continue
                    for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                        _, _, etype, code, value = _EVENT.unpack_from(data, off)
                        if etype != EV_KEY:
                            continue
                        mod = _MOD_KEYS.get(code)
                        if mod:
                            # several physical keys can map to one modifier name
                            (held.add if value else held.discard)(f"{mod}:{code}")
                        elif value == 1:
                            mods = frozenset(m.split(":")[0] for m in held)
                            binding = bindings.get((mods, code))
                            if binding:
                                log.info("hotkey %s", binding[0])
                                try:
                                    binding[1]()
                                except Exception:   # a bad callback must not kill the pump
                                    log.exception("hotkey %s failed", binding[0])
        finally:
            for fd in (wake_r, *fds):
                os.close(fd)
