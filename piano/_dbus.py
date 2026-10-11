"""A small D-Bus client in pure Python, for the session bus.

Enough of the protocol to call methods, subscribe to signals and answer the odd
method call, which is what the global-shortcuts portal and the KWin focus script
need.  Every basic type, arrays, structs, dict entries and variants marshal;
passing file descriptors does not.
"""

from __future__ import annotations

import itertools
import logging
import os
import socket
import struct
import threading
from collections.abc import Callable
from typing import NamedTuple
from urllib.parse import unquote

log = logging.getLogger(__name__)

BUS = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"

METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
NO_REPLY_EXPECTED = 0x1

# header field codes
PATH, INTERFACE, MEMBER, ERROR_NAME, REPLY_SERIAL, DESTINATION, SENDER, SIGNATURE = range(1, 9)


class DBusError(Exception):
    def __init__(self, name: str, message: str = ""):
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name


class Variant(NamedTuple):
    signature: str
    value: object


class Message(NamedTuple):
    type: int
    flags: int
    serial: int
    fields: dict
    body: list


# -- marshalling -------------------------------------------------------------
_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8,
          "h": 4, "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "{": 8, "v": 1}
_FIXED = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q",
          "t": "Q", "d": "d", "h": "I"}


def _type_end(sig: str, i: int) -> int:
    if sig[i] == "a":
        return _type_end(sig, i + 1)
    if sig[i] in "({":
        close = ")" if sig[i] == "(" else "}"
        i += 1
        while sig[i] != close:
            i = _type_end(sig, i)
    return i + 1


def split_signature(sig: str) -> list[str]:
    """'sa{sv}(ii)' -> ['s', 'a{sv}', '(ii)']"""
    types, i = [], 0
    while i < len(sig):
        end = _type_end(sig, i)
        types.append(sig[i:end])
        i = end
    return types


class _Writer:
    """Alignment is relative to the start of ``buf``, which must therefore be
    the start of the message, or of a body (bodies begin 8-aligned)."""

    def __init__(self):
        self.buf = bytearray()

    def pad(self, n: int) -> None:
        self.buf += bytes(-len(self.buf) % n)

    def put(self, t: str, value) -> None:
        c = t[0]
        self.pad(_ALIGN[c])
        if c in _FIXED:
            self.buf += struct.pack("<" + _FIXED[c], int(value) if c == "b" else value)
        elif c in "so":
            data = value.encode()
            self.buf += struct.pack("<I", len(data)) + data + b"\0"
        elif c == "g":
            data = value.encode()
            self.buf += bytes([len(data)]) + data + b"\0"
        elif c == "v":
            self.put("g", value.signature)
            self.put(value.signature, value.value)
        elif c == "a":
            at = len(self.buf)
            self.buf += bytes(4)
            self.pad(_ALIGN[t[1]])            # padding before the first element is not counted
            start = len(self.buf)
            for item in (value.items() if t[1] == "{" else value):
                self.put(t[1:], item)
            struct.pack_into("<I", self.buf, at, len(self.buf) - start)
        else:                                 # struct or dict entry
            for sub, item in zip(split_signature(t[1:-1]), value):
                self.put(sub, item)


class _Reader:
    def __init__(self, data: bytes, endian: str, pos: int = 0):
        self.data, self.endian, self.pos = data, endian, pos

    def get(self, t: str):
        c = t[0]
        self.pos += -self.pos % _ALIGN[c]
        if c in _FIXED:
            fmt = self.endian + _FIXED[c]
            (value,) = struct.unpack_from(fmt, self.data, self.pos)
            self.pos += struct.calcsize(fmt)
            return bool(value) if c == "b" else value
        if c in "so":
            n = self.get("u")
            value = self.data[self.pos:self.pos + n].decode("utf-8", "replace")
            self.pos += n + 1
            return value
        if c == "g":
            n = self.data[self.pos]
            value = self.data[self.pos + 1:self.pos + 1 + n].decode()
            self.pos += n + 2
            return value
        if c == "v":
            return self.get(self.get("g"))
        if c == "a":
            n = self.get("u")
            self.pos += -self.pos % _ALIGN[t[1]]
            end = self.pos + n
            items = []
            while self.pos < end:
                items.append(self.get(t[1:]))
            return dict(items) if t[1] == "{" else items
        return tuple(self.get(sub) for sub in split_signature(t[1:-1]))


# -- connection --------------------------------------------------------------
def _session_address() -> str:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address and os.environ.get("XDG_RUNTIME_DIR"):
        address = f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus"
    for entry in address.split(";"):
        kind, _, params = entry.partition(":")
        if kind != "unix":
            continue
        keys = dict(p.split("=", 1) for p in params.split(",") if "=" in p)
        if "path" in keys:
            return unquote(keys["path"])
        if "abstract" in keys:
            return "\0" + unquote(keys["abstract"])
    raise DBusError("org.freedesktop.DBus.Error.BadAddress", "no usable session bus address")


class Bus:
    """One connection to the session bus, with a reader thread.

    Handlers given to ``subscribe`` and ``export`` run on the reader thread, so
    they must not use ``call`` (its reply could never be read); hand anything
    that needs to talk back to another thread.
    """

    def __init__(self, timeout: float = 5.0):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.settimeout(timeout)
            self._sock.connect(_session_address())
            uid = str(os.getuid()).encode().hex().encode()
            self._sock.sendall(b"\0AUTH EXTERNAL " + uid + b"\r\n")
            self._rfile = self._sock.makefile("rb")
            reply = self._rfile.readline()
            if not reply.startswith(b"OK"):
                raise DBusError("org.freedesktop.DBus.Error.AuthFailed",
                                reply.decode(errors="replace").strip())
            self._sock.sendall(b"BEGIN\r\n")
            self._sock.settimeout(None)
        except BaseException:
            self._sock.close()
            raise
        self._serial = itertools.count(1)
        self._send_lock = threading.Lock()
        self._pending: dict[int, list] = {}
        self._signal_handlers: list[Callable[[Message], None]] = []
        self._methods: dict[tuple[str, str], Callable] = {}
        self.closed = threading.Event()
        threading.Thread(target=self._read_loop, name="piano-dbus", daemon=True).start()
        self.unique_name: str = self.call(BUS, BUS_PATH, BUS, "Hello")[0]

    # -- sending -----------------------------------------------------------
    def _send(self, mtype: int, serial: int, fields: dict, signature: str = "",
              args=(), flags: int = 0) -> None:
        body = _Writer()
        for t, value in zip(split_signature(signature), args):
            body.put(t, value)
        if signature:
            fields[SIGNATURE] = Variant("g", signature)
        head = _Writer()
        head.buf += struct.pack("<cBBBII", b"l", mtype, flags, 1, len(body.buf), serial)
        head.put("a(yv)", sorted(fields.items()))
        head.pad(8)
        with self._send_lock:
            self._sock.sendall(bytes(head.buf + body.buf))

    def call(self, destination: str, path: str, interface: str, member: str,
             signature: str = "", args=(), timeout: float = 10.0) -> list:
        serial = next(self._serial)
        slot: list = [threading.Event(), None]
        self._pending[serial] = slot
        fields = {PATH: Variant("o", path), MEMBER: Variant("s", member),
                  DESTINATION: Variant("s", destination)}
        if interface:
            fields[INTERFACE] = Variant("s", interface)
        try:
            self._send(METHOD_CALL, serial, fields, signature, args)
            if not slot[0].wait(timeout):
                raise DBusError("org.freedesktop.DBus.Error.Timeout", f"{member} timed out")
        finally:
            self._pending.pop(serial, None)
        reply: Message | None = slot[1]
        if reply is None:
            raise DBusError("org.freedesktop.DBus.Error.Disconnected", "connection closed")
        if reply.type == ERROR:
            text = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
            raise DBusError(reply.fields.get(ERROR_NAME, "unknown error"), text)
        return reply.body

    def subscribe(self, callback: Callable[..., None], *, interface: str | None = None,
                  member: str | None = None, path: str | None = None) -> None:
        """Call ``callback(*body)`` for each matching signal."""
        def handler(msg: Message) -> None:
            f = msg.fields
            if ((interface and f.get(INTERFACE) != interface)
                    or (member and f.get(MEMBER) != member)
                    or (path and f.get(PATH) != path)):
                return
            callback(*msg.body)

        # Registered before AddMatch so a signal racing the reply is not lost.
        self._signal_handlers.append(handler)
        rule = ",".join(["type='signal'"] + [f"{k}='{v}'" for k, v in
                        (("interface", interface), ("member", member), ("path", path)) if v])
        self.call(BUS, BUS_PATH, BUS, "AddMatch", "s", [rule])

    def export(self, path: str, member: str, callback: Callable[..., None]) -> None:
        """Answer method calls to ``path``/``member`` with ``callback(*body)``."""
        self._methods[(path, member)] = callback

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)   # wakes the reader
        except OSError:
            pass
        self._sock.close()

    # -- receiving ---------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        data = self._rfile.read(n)
        if len(data) < n:
            raise EOFError("bus connection closed")
        return data

    def _read_message(self) -> Message:
        fixed = self._read_exact(16)
        endian = "<" if fixed[:1] == b"l" else ">"
        mtype, flags, _version, body_len, serial, fields_len = struct.unpack(
            endian + "BBBIII", fixed[1:])
        header_len = 16 + fields_len
        header_len += -header_len % 8
        data = fixed + self._read_exact(header_len - 16 + body_len)
        fields = dict(_Reader(data, endian, 12).get("a(yv)"))
        reader = _Reader(data[header_len:], endian)
        body = [reader.get(t) for t in split_signature(fields.get(SIGNATURE, ""))]
        return Message(mtype, flags, serial, fields, body)

    def _read_loop(self) -> None:
        try:
            while True:
                msg = self._read_message()
                if msg.type in (METHOD_RETURN, ERROR):
                    slot = self._pending.get(msg.fields.get(REPLY_SERIAL))
                    if slot:
                        slot[1] = msg
                        slot[0].set()
                elif msg.type == SIGNAL:
                    for handler in list(self._signal_handlers):
                        try:
                            handler(msg)
                        except Exception:      # one bad handler must not end the connection
                            log.exception("signal handler failed")
                elif msg.type == METHOD_CALL:
                    self._answer(msg)
        except (OSError, EOFError, ValueError, struct.error, IndexError) as exc:
            if not self.closed.is_set():
                log.debug("bus connection ended: %s", exc)
        finally:
            self.closed.set()
            try:
                self._rfile.close()
            except OSError:
                pass
            for slot in list(self._pending.values()):
                slot[0].set()                  # wake callers; their reply stays None

    def _answer(self, msg: Message) -> None:
        handler = self._methods.get((msg.fields.get(PATH), msg.fields.get(MEMBER)))
        error = None
        if handler is None:
            error = "org.freedesktop.DBus.Error.UnknownMethod"
        else:
            try:
                handler(*msg.body)
            except Exception:
                log.exception("exported method %s failed", msg.fields.get(MEMBER))
                error = "org.freedesktop.DBus.Error.Failed"
        if msg.flags & NO_REPLY_EXPECTED or SENDER not in msg.fields:
            return
        fields = {REPLY_SERIAL: Variant("u", msg.serial),
                  DESTINATION: Variant("s", msg.fields[SENDER])}
        if error:
            fields[ERROR_NAME] = Variant("s", error)
        self._send(ERROR if error else METHOD_RETURN, next(self._serial), fields)
