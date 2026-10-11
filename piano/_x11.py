"""A small X11 protocol client in pure Python.

It speaks just enough of the core protocol over the display socket to read the
focused window's title through EWMH and to grab keys on the root window.  Going
around libX11 sidesteps its threading rules (Tk is using it on another thread)
and the question of where libX11 lives, which not every distribution answers alike.

One ``Display`` belongs to one thread.
"""

from __future__ import annotations

import os
import select
import socket
import struct

SHIFT, LOCK, CONTROL, MOD1, MOD2 = 0x01, 0x02, 0x04, 0x08, 0x10
KEY_PRESS, KEY_RELEASE = 2, 3
BAD_ACCESS = 10

# opcodes
_INTERN_ATOM, _GET_PROPERTY, _GRAB_KEY, _GET_INPUT_FOCUS, _GET_KEYBOARD_MAPPING = 16, 20, 33, 43, 101


class X11Error(Exception):
    pass


def _pad(n: int) -> int:
    return -n % 4


def _xauth_cookie(display: str) -> tuple[bytes, bytes]:
    """The MIT-MAGIC-COOKIE-1 for ``display`` from the Xauthority file, if any."""
    path = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return b"", b""
    hostname = socket.gethostname().encode()
    best: tuple[int, bytes] | None = None
    pos = 0
    try:
        while pos < len(data):
            (family,) = struct.unpack_from(">H", data, pos)
            pos += 2
            fields = []
            for _ in range(4):
                (n,) = struct.unpack_from(">H", data, pos)
                fields.append(data[pos + 2:pos + 2 + n])
                pos += 2 + n
            address, number, name, cookie = fields
            if name != b"MIT-MAGIC-COOKIE-1" or (number and number.decode() != display):
                continue
            # Prefer this host's own entry, then a wildcard, then anything.
            rank = 0 if family == 256 and address == hostname else 1 if family == 65535 else 2
            if best is None or rank < best[0]:
                best = (rank, cookie)
    except struct.error:
        pass
    return (b"MIT-MAGIC-COOKIE-1", best[1]) if best else (b"", b"")


class Display:
    def __init__(self, name: str | None = None):
        name = name if name is not None else os.environ.get("DISPLAY", "")
        host, _, rest = name.rpartition(":")
        number = rest.split(".")[0]
        if host not in ("", "unix") or not number.isdigit():
            raise X11Error(f"only local displays are supported, not {name!r}")
        self._sock = self._connect(number)
        self._events: list[bytes] = []
        self._errors: list[tuple[int, int]] = []
        self._seq = 0
        self._atoms: dict[str, int] = {}
        try:
            self._setup(*_xauth_cookie(number))
        except BaseException:
            self._sock.close()
            raise

    @staticmethod
    def _connect(number: str) -> socket.socket:
        last: OSError | None = None
        for address in (f"/tmp/.X11-unix/X{number}", f"\0/tmp/.X11-unix/X{number}"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            try:
                sock.connect(address)
            except OSError as exc:
                sock.close()
                last = exc
                continue
            sock.settimeout(None)
            return sock
        raise X11Error(f"cannot connect to display :{number}: {last}")

    def _setup(self, auth_name: bytes, auth_data: bytes) -> None:
        self._sock.sendall(struct.pack("<BxHHHH2x", 0x6C, 11, 0, len(auth_name), len(auth_data))
                           + auth_name + bytes(_pad(len(auth_name)))
                           + auth_data + bytes(_pad(len(auth_data))))
        head = self._read_exact(8)
        status, reason_len, _major, _minor, length = struct.unpack("<BBHHH", head)
        data = self._read_exact(length * 4)
        if status != 1:
            raise X11Error("display refused the connection: "
                           + data[:reason_len].decode(errors="replace").strip())
        (_release, _base, _mask, _motion, vendor_len, _max_req, _screens, formats,
         _, _, _, _, self.min_keycode, self.max_keycode) = struct.unpack_from(
            "<IIIIHHBBBBBBBB4x", data)
        offset = 32 + vendor_len + _pad(vendor_len) + 8 * formats
        (self.root,) = struct.unpack_from("<I", data, offset)

    # -- plumbing ----------------------------------------------------------
    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        self._sock.close()

    def _read_exact(self, n: int) -> bytes:
        chunks, got = [], 0
        while got < n:
            chunk = self._sock.recv(n - got)
            if not chunk:
                raise X11Error("display connection closed")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _read_packet(self) -> bytes:
        head = self._read_exact(32)
        if head[0] & 0x7F in (1, 35):         # reply, generic event: longer than 32
            return head + self._read_exact(struct.unpack_from("<I", head, 4)[0] * 4)
        return head

    def _request(self, data: bytes, reply: bool = True) -> bytes | int:
        self._sock.sendall(data)
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        if not reply:
            return seq
        while True:
            packet = self._read_packet()
            kind = packet[0] & 0x7F
            (packet_seq,) = struct.unpack_from("<H", packet, 2)
            if kind == 1 and packet_seq == seq:
                return packet
            if kind == 0:
                if packet_seq == seq:
                    raise X11Error(f"X error {packet[1]} on request {packet[10]}")
                self._errors.append((packet_seq, packet[1]))
            elif kind != 1:
                self._events.append(packet)

    # -- requests ----------------------------------------------------------
    def atom(self, name: str) -> int:
        if name not in self._atoms:
            raw = name.encode()
            reply = self._request(struct.pack("<BBHH2x", _INTERN_ATOM, 0,
                                              2 + (len(raw) + _pad(len(raw))) // 4, len(raw))
                                  + raw + bytes(_pad(len(raw))))
            (self._atoms[name],) = struct.unpack_from("<I", reply, 8)
        return self._atoms[name]

    def get_property(self, window: int, prop: str, max_bytes: int = 4096) -> tuple[int, bytes]:
        """(type atom, raw value); type is 0 when the property is absent."""
        reply = self._request(struct.pack("<BBHIIIII", _GET_PROPERTY, 0, 6, window,
                                          self.atom(prop), 0, 0, (max_bytes + 3) // 4))
        fmt = reply[1]
        prop_type, _after, count = struct.unpack_from("<III", reply, 8)
        return prop_type, reply[32:32 + count * (fmt // 8)]

    def sync(self) -> list[tuple[int, int]]:
        """Round-trip, then hand back (sequence, code) for errors since the last sync."""
        self._request(struct.pack("<BxH", _GET_INPUT_FOCUS, 1))
        errors, self._errors = self._errors, []
        return errors

    def keyboard_mapping(self) -> dict[int, int]:
        """keysym -> keycode, taking the first keycode that produces each keysym."""
        count = self.max_keycode - self.min_keycode + 1
        reply = self._request(struct.pack("<BxHBB2x", _GET_KEYBOARD_MAPPING, 2,
                                          self.min_keycode, count))
        per = reply[1]
        syms = struct.unpack_from(f"<{count * per}I", reply, 32)
        mapping: dict[int, int] = {}
        for i in range(count):
            for sym in syms[i * per:(i + 1) * per]:
                if sym:
                    mapping.setdefault(sym, self.min_keycode + i)
        return mapping

    def grab_key(self, keycode: int, modifiers: int) -> int:
        """Passive grab on the root window; returns the request's sequence number."""
        return self._request(struct.pack("<BBHIHBBB3x", _GRAB_KEY, 1, 4, self.root,
                                         modifiers, keycode, 1, 1), reply=False)

    def next_event(self, wake: int | None = None) -> bytes | None:
        """The next event, or None once ``wake`` (a pipe fd) becomes readable."""
        while not self._events:
            ready, _, _ = select.select([self._sock] + ([wake] if wake is not None else []), [], [])
            if wake is not None and wake in ready:
                return None
            packet = self._read_packet()
            if packet[0] & 0x7F == 0:
                self._errors.append((struct.unpack_from("<H", packet, 2)[0], packet[1]))
            elif packet[0] & 0x7F != 1:
                self._events.append(packet)
        return self._events.pop(0)

    # -- EWMH --------------------------------------------------------------
    def supports_active_window(self) -> bool:
        prop_type, data = self.get_property(self.root, "_NET_SUPPORTED", 65536)
        wanted = self.atom("_NET_ACTIVE_WINDOW")
        return bool(prop_type) and wanted in struct.unpack(f"<{len(data) // 4}I", data)

    def active_window_title(self) -> str:
        _, data = self.get_property(self.root, "_NET_ACTIVE_WINDOW")
        window = struct.unpack_from("<I", data)[0] if len(data) >= 4 else 0
        if not window:
            return ""
        try:
            for prop, encoding in (("_NET_WM_NAME", "utf-8"), ("WM_NAME", "latin-1")):
                _, value = self.get_property(window, prop)
                if value:
                    return value.decode(encoding, "replace")
        except X11Error:                      # the window went away between the two reads
            pass
        return ""
