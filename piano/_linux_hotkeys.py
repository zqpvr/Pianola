"""Linux hotkeys, from whichever source this session allows.

There is no single way to see a key press while another program has focus, so
the first of these that works is used:

1. evdev: read the keyboards directly.  Works on X11 and Wayland under any
   desktop, but needs read access to ``/dev/input``, normally the input group.
2. The global-shortcuts portal on Wayland (KDE Plasma, GNOME 48+, Hyprland).
   The desktop asks once whether Pianola may have the keys.
3. A passive key grab on X11, which any X11 window manager honours.  On
   Wayland it still catches the keys while an XWayland window has focus.

Whatever is chosen, ``pianola --control ACTION`` also reaches the running
player through a socket, so a window manager without any of the above can bind
keys to that command in its own config.
"""

from __future__ import annotations

import fcntl
import logging
import os
import select
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from . import _dbus, _x11
from ._linux import (_EVENT, _ROW_KEYS, EV_KEY, EVIOCGNAME, KEY_F1, KEY_LEFTALT, KEY_LEFTCTRL,
                     KEY_LEFTSHIFT, KEY_RIGHTALT, KEY_RIGHTCTRL, KEY_RIGHTSHIFT, _eviocgbit,
                     _wayland)

log = logging.getLogger(__name__)

APP_ID = "io.github.zqpvr.Pianola"

# key name -> (evdev code, X keysym, name in the XDG shortcuts notation)
_KEYS: dict[str, tuple[int, int, str]] = {
    f"f{i}": (KEY_F1 + i - 1 if i <= 10 else 76 + i, 0xFFBE + i - 1, f"F{i}")
    for i in range(1, 13)
}
_KEYS.update({
    "escape": (1, 0xFF1B, "Escape"), "space": (57, 0x20, "space"),
    "home": (102, 0xFF50, "Home"), "end": (107, 0xFF57, "End"),
    "left": (105, 0xFF51, "Left"), "right": (106, 0xFF53, "Right"),
    "up": (103, 0xFF52, "Up"), "down": (108, 0xFF54, "Down"),
    "insert": (110, 0xFF63, "Insert"), "delete": (111, 0xFFFF, "Delete"),
    "pause": (119, 0xFF13, "Pause"),
})
_KEYS.update({c: (code, ord(c), c) for c, code in _ROW_KEYS.items()})

_X11_MODS = {"shift": _x11.SHIFT, "ctrl": _x11.CONTROL, "alt": _x11.MOD1}
_XDG_MODS = {"shift": "SHIFT", "ctrl": "CTRL", "alt": "ALT"}


def parse_hotkey(text: str) -> tuple[frozenset[str], str]:
    """'ctrl+shift+f1' -> ({'ctrl', 'shift'}, 'f1')."""
    mods: set[str] = set()
    key = ""
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods.add("ctrl")
        elif part in ("alt", "shift"):
            mods.add(part)
        elif part in _KEYS:
            key = part
    if not key:
        raise ValueError(f"unrecognised hotkey: {text!r}")
    return frozenset(mods), key


class _Binding(NamedTuple):
    name: str
    combo: str
    mods: frozenset[str]
    key: str
    action: Callable[[], None]


class _Unavailable(Exception):
    pass


def _fire(binding: _Binding, source: str) -> None:
    log.info("hotkey %s (%s)", binding.combo, source)
    try:
        binding.action()
    except Exception:                         # a bad callback must not kill the listener
        log.exception("hotkey %s failed", binding.combo)


# -- 1. evdev ----------------------------------------------------------------
_MOD_KEYS = {
    KEY_LEFTCTRL: "ctrl", KEY_RIGHTCTRL: "ctrl",
    KEY_LEFTSHIFT: "shift", KEY_RIGHTSHIFT: "shift",
    KEY_LEFTALT: "alt", KEY_RIGHTALT: "alt",
}


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


class _EvdevHotkeys:
    """Observes rather than grabs: the game still receives the keys."""

    label = "evdev"

    def __init__(self, bindings: list[_Binding]):
        fds = _keyboards()
        if not fds:
            raise _Unavailable("no readable keyboard in /dev/input (join the input group)")
        self._bindings = {(b.mods, _KEYS[b.key][0]): b for b in bindings}
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._pump, args=(fds,),
                                        name="piano-evdev", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        os.close(self._wake_w)                # EOF on the pipe wakes the pump
        self._thread.join(timeout=1.0)

    def _pump(self, fds: list[int]) -> None:
        held: set[tuple[str, int]] = set()
        try:
            while True:
                ready, _, _ = select.select([self._wake_r, *fds], [], [])
                if self._wake_r in ready:
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
                            # several physical keys share one modifier name
                            (held.add if value else held.discard)((mod, code))
                        elif value == 1:      # press; 2 is auto-repeat
                            binding = self._bindings.get((frozenset(m for m, _ in held), code))
                            if binding:
                                _fire(binding, self.label)
        finally:
            for fd in (self._wake_r, *fds):
                os.close(fd)


# -- 2. the global-shortcuts portal ------------------------------------------
def _desktop_entry() -> None:
    """Write a hidden desktop entry for APP_ID, if it is missing or stale.

    The portal only accepts an app ID it can find a desktop entry for.  Without
    one it files the shortcuts under whatever launched Pianola (Konsole, Dolphin),
    a different owner each way it is started, and the second owner finds F1 to F6
    already taken by the first.  NoDisplay keeps it out of the menus.
    """
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        command = f'"{sys.executable}" "{main}"'
    text = ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Pianola\n"
            "Comment=Plays MIDI files and Virtual Piano sheets on Roblox pianos\n"
            f"Exec={command}\n"
            "Icon=input-keyboard\n"
            "Terminal=false\n"
            "NoDisplay=true\n")
    folder = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                          "applications")
    path = os.path.join(folder, f"{APP_ID}.desktop")
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        log.info("wrote %s", path)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


_PORTAL = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"


class _PortalHotkeys:
    """Registers the keys with the desktop, which may ask the user first.

    The session belongs to this D-Bus connection, so closing it on stop is all
    the cleanup there is.
    """

    label = "desktop shortcuts portal"
    APPROVAL_TIMEOUT = 300.0                  # the desktop may be waiting on a dialog

    def __init__(self, bindings: list[_Binding]):
        try:
            self._bus = _dbus.Bus()
        except (OSError, _dbus.DBusError) as exc:
            raise _Unavailable(f"no session bus: {exc}") from exc
        self._tokens = iter(range(1, 1_000_000))
        self._by_id = {b.name: b for b in bindings}
        self.unbound: list[str] = []
        try:
            self._setup(bindings)
        except _dbus.DBusError as exc:
            self._bus.close()
            raise _Unavailable(str(exc)) from exc
        except BaseException:
            self._bus.close()
            raise

    def _setup(self, bindings: list[_Binding]) -> None:
        bus = self._bus
        # A program outside Flatpak names itself before its first portal call,
        # or the desktop files the shortcuts under whatever launched it.
        _desktop_entry()
        for attempt in range(3):
            try:
                bus.call(_PORTAL, _PORTAL_PATH, "org.freedesktop.host.portal.Registry",
                         "Register", "sa{sv}", [APP_ID, {}])
                log.info("portal: registered as %s", APP_ID)
                break
            except _dbus.DBusError as exc:
                if exc.name == "org.freedesktop.DBus.Error.UnknownMethod":
                    log.info("portal: no app registry (older portal); shortcuts go under the launcher")
                    break
                if attempt == 2:
                    log.warning("portal: could not register as %s: %s", APP_ID, exc)
                time.sleep(0.5)        # a desktop entry written a moment ago may not be indexed yet
        bus.call(_PORTAL, _PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get", "ss",
                 [_SHORTCUTS, "version"])        # fails fast when there is no such portal

        token = f"pianola{os.getpid()}"
        results = self._request("CreateSession", "a{sv}",
                                [{"session_handle_token": _dbus.Variant("s", token)}])
        self._session = str(results["session_handle"])
        bus.subscribe(self._activated, interface=_SHORTCUTS, member="Activated",
                      path=_PORTAL_PATH)

        shortcuts = []
        for b in bindings:
            trigger = "+".join([_XDG_MODS[m] for m in sorted(b.mods)] + [_KEYS[b.key][2]])
            shortcuts.append((b.name, {
                "description": _dbus.Variant("s", b.name.replace("_", " ").capitalize()),
                "preferred_trigger": _dbus.Variant("s", trigger)}))
        results = self._request("BindShortcuts", "oa(sa{sv})sa{sv}",
                                [self._session, shortcuts, "", {}],
                                timeout=self.APPROVAL_TIMEOUT)
        bound = {sid: info.get("trigger_description", "")
                 for sid, info in results.get("shortcuts", [])}
        log.info("portal bound: %s", ", ".join(f"{k}={v or '(no key)'}" for k, v in bound.items())
                 or "nothing")
        self.unbound = [b.combo for b in bindings if not bound.get(b.name)]

    def _request(self, method: str, signature: str, args: list, timeout: float = 30.0) -> dict:
        """Call a portal method and wait for its Request's Response signal."""
        token = f"pianola{next(self._tokens)}"
        sender = self._bus.unique_name[1:].replace(".", "_")
        path = f"{_PORTAL_PATH}/request/{sender}/{token}"
        done = threading.Event()
        answer: list = []

        def response(code: int, results: dict) -> None:
            answer.extend((code, results))
            done.set()

        self._bus.subscribe(response, interface="org.freedesktop.portal.Request",
                            member="Response", path=path)
        args[-1]["handle_token"] = _dbus.Variant("s", token)
        self._bus.call(_PORTAL, _PORTAL_PATH, _SHORTCUTS, method, signature, args)
        if not done.wait(timeout) or not answer:
            raise _Unavailable(f"{method}: the desktop did not answer")
        code, results = answer
        if code == 1:
            raise _Unavailable("the shortcuts were declined")
        if code != 0:
            raise _Unavailable(f"{method} failed")
        return results

    def _activated(self, session: str, shortcut_id: str, _timestamp: int, _options: dict) -> None:
        binding = self._by_id.get(shortcut_id)
        if binding and str(session) == self._session:
            _fire(binding, self.label)

    def stop(self) -> None:
        self._bus.close()


# -- 3. X11 key grabs ----------------------------------------------------------
class _X11Hotkeys:
    label = "X11 key grab"
    # Grab once per state of the lock keys, or Caps Lock or Num Lock being on
    # would make every combination miss.
    _LOCKS = (0, _x11.LOCK, _x11.MOD2, _x11.LOCK | _x11.MOD2)

    def __init__(self, bindings: list[_Binding]):
        if not os.environ.get("DISPLAY"):
            raise _Unavailable("no X display")
        try:
            self._display = display = _x11.Display()
            keycodes = display.keyboard_mapping()
        except (OSError, _x11.X11Error) as exc:
            raise _Unavailable(str(exc)) from exc
        self._bindings: dict[tuple[int, int], _Binding] = {}
        requests: dict[int, _Binding] = {}
        for b in bindings:
            keycode = keycodes.get(_KEYS[b.key][1])
            if keycode is None:
                continue
            mods = sum(_X11_MODS[m] for m in b.mods)
            self._bindings[(keycode, mods)] = b
            for lock in self._LOCKS:
                requests[display.grab_key(keycode, mods | lock)] = b
        taken = {requests[seq].combo for seq, code in display.sync()
                 if code == _x11.BAD_ACCESS and seq in requests}
        if taken:
            log.warning("X11: already grabbed by another program: %s", ", ".join(sorted(taken)))
        self.unbound = sorted(taken)
        if len(taken) == len(bindings):
            display.close()
            raise _Unavailable("every key is already grabbed by another program")
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._pump, name="piano-x11keys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        os.close(self._wake_w)
        self._thread.join(timeout=1.0)

    def _pump(self) -> None:
        relevant = _x11.SHIFT | _x11.CONTROL | _x11.MOD1
        released: tuple[int, int] | None = None
        try:
            while True:
                event = self._display.next_event(self._wake_r)
                if event is None:
                    return
                kind = event[0] & 0x7F
                keycode = event[1]
                stamp = int.from_bytes(event[4:8], "little")
                if kind == _x11.KEY_RELEASE:
                    released = (keycode, stamp)
                elif kind == _x11.KEY_PRESS:
                    # Auto-repeat arrives as release+press with one timestamp.
                    if released == (keycode, stamp):
                        continue
                    state = int.from_bytes(event[28:30], "little")
                    binding = self._bindings.get((keycode, state & relevant))
                    if binding:
                        _fire(binding, self.label)
        except (OSError, _x11.X11Error) as exc:
            log.warning("X11 hotkeys stopped: %s", exc)
        finally:
            self._display.close()
            os.close(self._wake_r)


# -- the control socket --------------------------------------------------------
def control_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(runtime, "pianola.sock") if runtime else f"/tmp/pianola-{os.getuid()}.sock"


def send_control(action: str) -> bool:
    """Hand ``action`` to the running player; False when none is listening."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        try:
            sock.sendto(action.encode(), control_path())
        except OSError:
            return False
    return True


class _ControlSocket:
    def __init__(self, actions: dict[str, _Binding]):
        self._actions = actions
        self._path = control_path()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            try:
                os.unlink(self._path)             # left behind by a run that crashed
            except FileNotFoundError:
                pass
            self._sock.bind(self._path)
            os.chmod(self._path, 0o600)
        except OSError:
            self._sock.close()
            raise
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="piano-control", daemon=True)
        self._thread.start()
        log.info("control socket at %s", self._path)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._sock.recv(64)
            except socket.timeout:
                continue
            except OSError:
                return
            binding = self._actions.get(data.decode(errors="replace").strip())
            if binding:
                _fire(binding, "control socket")
            else:
                log.info("control socket: no action %r", data)


# -- the manager -------------------------------------------------------------
class HotkeyManager:
    """Same surface as the Windows one, but sources are tried on a worker thread,
    since the portal may sit waiting for the user to approve.  ``on_status``, when
    set, hears about anything the user should know."""

    def __init__(self):
        self._bindings: list[tuple[str, Callable[[], None], str]] = []
        self._backend = None
        self._control: _ControlSocket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.failed: list[str] = []
        self.reason = ""
        self.on_status: Callable[[str], None] | None = None

    def bind(self, combo: str, action: Callable[[], None], name: str = "") -> None:
        self._bindings.append((combo, action, name or combo))

    def start(self) -> None:
        if self._thread:
            return
        self._stopping.clear()
        self.failed, self.reason = [], ""
        bindings = []
        for combo, action, name in self._bindings:
            try:
                mods, key = parse_hotkey(combo)
            except ValueError as exc:
                log.warning("%s", exc)
                self.failed.append(combo)
                continue
            bindings.append(_Binding(name, combo, mods, key, action))
        try:
            self._control = _ControlSocket({b.name: b for b in bindings})
        except OSError as exc:
            log.warning("control socket unavailable: %s", exc)
        self._thread = threading.Thread(target=self._choose, args=(bindings,),
                                        name="piano-hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._backend is not None:
            self._backend.stop()
            self._backend = None
        if self._control is not None:
            self._control.stop()
            self._control = None
        self._thread = None

    def _status(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)

    def _choose(self, bindings: list[_Binding]) -> None:
        if not bindings:
            return
        sources: list[type] = [_EvdevHotkeys]
        if _wayland():
            sources += [_PortalHotkeys, _X11Hotkeys]
        else:
            sources += [_X11Hotkeys, _PortalHotkeys]
        reasons = []
        for source in sources:
            if self._stopping.is_set():
                return
            try:
                backend = source(bindings)
            except _Unavailable as exc:
                log.info("hotkeys: %s unavailable: %s", source.label, exc)
                reasons.append(f"{source.label}: {exc}")
                continue
            if self._stopping.is_set():
                backend.stop()
                return
            self._backend = backend
            log.info("hotkeys: using %s", source.label)
            unbound = getattr(backend, "unbound", [])
            if source is _X11Hotkeys and _wayland():
                self._status("F-keys only work while an X11 window has focus here; "
                             "see Hotkeys in the README for other ways.")
            elif unbound:
                self.failed = unbound
                self._status("Some hotkeys got no key: " + ", ".join(unbound)
                             + ". Assign them in your desktop's shortcut settings.")
            return
        self.failed = [b.combo for b in bindings]
        self.reason = "; ".join(reasons)
        log.warning("hotkeys off: %s", self.reason)
        self._status("F-keys unavailable on this desktop; the buttons still work. "
                     "See Hotkeys in the README.")
