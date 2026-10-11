"""Linux backend: a ``/dev/uinput`` virtual keyboard and the focused window's title.

Typing goes through the kernel rather than any display server, so it behaves the
same on X11 and Wayland and reaches Wine, Proton and native games alike.  The
price is a permission: ``/dev/uinput`` has to be writable by you.  The hotkeys
live in ``_linux_hotkeys``.

The rest of the package identifies keys by Windows virtual-key code, so this
module translates those to evdev codes at the edge.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import socket
import struct
import tempfile
import threading
import time
from collections.abc import Callable

from . import _dbus, _x11

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
    return f"Linux uinput; focus: {_focus_kind() or 'none, guard off'}; {session or 'no session variables'}"


# -- focus -------------------------------------------------------------------
#
# Wayland has no portable way to ask which window has focus, so each desktop
# that offers one gets its own source: KWin runs a small script that reports
# every change, Hyprland, Sway and niri answer on their IPC sockets, and any
# X11 session answers through the EWMH root properties.  GNOME on Wayland, and
# compositors without an IPC, have nothing to ask, and the guard is off there.

def _recv_all(sock: socket.socket) -> bytes:
    chunks = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("socket closed mid-reply")
        data += chunk
    return data


def _ipc(path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        raise
    return sock


def _title_hyprland() -> str:
    signature = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    path = f"{runtime}/hypr/{signature}/.socket.sock"
    if not os.path.exists(path):
        path = f"/tmp/hypr/{signature}/.socket.sock"   # Hyprland before 0.40
    with _ipc(path) as sock:
        sock.sendall(b"j/activewindow")
        info = json.loads(_recv_all(sock) or b"{}")
    return info.get("title", "") if isinstance(info, dict) else ""


def _sway_focused(node: dict) -> str | None:
    if node.get("focused"):
        return node.get("name") or ""
    for child in (*node.get("nodes", ()), *node.get("floating_nodes", ())):
        found = _sway_focused(child)
        if found is not None:
            return found
    return None


def _title_sway() -> str:
    with _ipc(os.environ["SWAYSOCK"]) as sock:
        sock.sendall(b"i3-ipc" + struct.pack("=II", 0, 4))          # GET_TREE
        length, _kind = struct.unpack("=II", _recv_exact(sock, 14)[6:])
        tree = json.loads(_recv_exact(sock, length))
    return _sway_focused(tree) or ""


def _title_niri() -> str:
    with _ipc(os.environ["NIRI_SOCKET"]) as sock:
        sock.sendall(b'"FocusedWindow"\n')
        reply = json.loads(_recv_all(sock).split(b"\n", 1)[0])
    if "Ok" not in reply:
        raise ValueError(f"niri refused: {reply.get('Err')}")
    window = reply["Ok"].get("FocusedWindow") or {}
    return window.get("title") or ""


_x11_display: _x11.Display | None = None


def _title_x11() -> str:
    global _x11_display
    if _x11_display is None:
        _x11_display = _x11.Display()
    try:
        return _x11_display.active_window_title()
    except (OSError, _x11.X11Error):
        _x11_display.close()
        _x11_display = None                   # reconnect on the next poll
        raise


class _FocusWatcher:
    """Polls a focus source on its own thread.

    Even a quick socket round trip is more than the scheduler should spend
    mid-song, so it only ever reads the last answer.  Polling stops a couple of
    seconds after the last question and resumes on the next one, which then
    waits for a fresh answer rather than trusting a stale one.
    """

    POLL = 0.1
    IDLE = 2.0

    def __init__(self, probe: Callable[[], str]):
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
        failure = ""
        while True:
            try:
                title = self._probe()
                if failure:
                    log.info("focus source working again")
                    failure = ""
            except Exception as exc:         # a failed check keeps the guard closed
                if str(exc) != failure:
                    failure = str(exc)
                    log.warning("focus check failed: %s", exc)
                title = ""
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


_KWIN_SCRIPT = """\
var service = %s;
var current = null;
function report() {
    callDBus(service, "/Pianola", "io.github.zqpvr.Pianola", "Focus",
             current ? String(current.caption || "") : "");
}
function activated(window) {
    if (current) {
        try { current.captionChanged.disconnect(report); } catch (e) {}
    }
    current = window;
    if (current) current.captionChanged.connect(report);
    report();
}
if (workspace.windowActivated) {            // Plasma 6
    workspace.windowActivated.connect(activated);
    activated(workspace.activeWindow);
} else {                                    // Plasma 5
    workspace.clientActivated.connect(activated);
    activated(workspace.activeClient);
}
"""


class _KWinFocus:
    """Loads a KWin script that calls back over D-Bus whenever focus moves.

    Push rather than poll, so the answer is always current.  The script stays
    loaded until Pianola exits; a leftover from a run that crashed is unloaded
    by name before the new one goes in.
    """

    PLUGIN = "pianola-focus"
    SCRIPTING = ("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting")

    def __init__(self):
        self._title = ""
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._bus: _dbus.Bus | None = None
        self._path = ""
        self._failed = False

    def title(self) -> str | None:
        with self._lock:
            if self._failed:
                return None
            if self._bus is None or self._bus.closed.is_set():
                try:
                    self._start()
                except (OSError, _dbus.DBusError) as exc:
                    log.warning("KWin focus script unavailable: %s", exc)
                    self._failed = True
                    return None
        self._ready.wait(timeout=1.5)
        return self._title

    def _start(self) -> None:
        bus = _dbus.Bus()
        try:
            bus.export("/Pianola", "Focus", self._report)
            fd, path = tempfile.mkstemp(prefix="pianola-kwin-", suffix=".js",
                                        dir=os.environ.get("XDG_RUNTIME_DIR"))
            with os.fdopen(fd, "w") as fh:
                fh.write(_KWIN_SCRIPT % json.dumps(bus.unique_name))
            try:
                bus.call(*self.SCRIPTING, "unloadScript", "s", [self.PLUGIN])
            except _dbus.DBusError:
                pass
            script_id = bus.call(*self.SCRIPTING, "loadScript", "ss", [path, self.PLUGIN])[0]
            if script_id < 0:
                raise _dbus.DBusError("org.kde.kwin.Scripting", "loadScript refused the script")
            bus.call(*self.SCRIPTING, "start")
        except BaseException:
            bus.close()
            raise
        self._bus, self._path = bus, path
        atexit.register(self.close)
        log.info("KWin focus script loaded")

    def _report(self, caption: str) -> None:
        if caption != self._title:
            log.info("focused window: %r", caption)
        self._title = caption
        self._ready.set()

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.call(*self.SCRIPTING, "unloadScript", "s", [self.PLUGIN], timeout=2.0)
            except _dbus.DBusError:
                pass
            self._bus.close()
            self._bus = None
        if self._path:
            try:
                os.unlink(self._path)
            except OSError:
                pass


def _wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or "WAYLAND_DISPLAY" in os.environ


def _focus_kind() -> str | None:
    """Which focus source this session has, judged from the environment alone."""
    env = os.environ
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if env.get("SWAYSOCK"):
        return "sway"
    if env.get("NIRI_SOCKET"):
        return "niri"
    if _wayland():
        return "kwin" if "KDE" in env.get("XDG_CURRENT_DESKTOP", "").upper() else None
    return "x11" if env.get("DISPLAY") else None


def _focus_source():
    kind = _focus_kind()
    if kind == "kwin":
        return _KWinFocus()
    if kind == "x11":
        try:
            display = _x11.Display()
            supported = display.supports_active_window()
            display.close()
        except (OSError, _x11.X11Error) as exc:
            log.warning("X11 focus check unavailable: %s", exc)
            return None
        if not supported:
            log.warning("the window manager does not publish _NET_ACTIVE_WINDOW")
            return None
    probes = {"hyprland": _title_hyprland, "sway": _title_sway, "niri": _title_niri,
              "x11": _title_x11}
    return _FocusWatcher(probes[kind]) if kind else None


_source = None
_source_ready = False
_source_lock = threading.Lock()


def foreground_title() -> str | None:
    """Title of the focused window, or ``None`` when this session cannot say."""
    global _source, _source_ready
    if not _source_ready:
        with _source_lock:
            if not _source_ready:
                _source = _focus_source()
                _source_ready = True
    return _source.title() if _source else None


class TimerResolution:
    """Linux sleeps accurately already; kept so the player needs no platform checks."""

    def __init__(self, period: int = 1):
        self.period = period

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
