"""Self-contained Standard MIDI File reader.

Only the parts a piano player cares about are decoded: note on/off, the tempo
map, sustain pedal (CC64) and a few informational meta events.  No third-party
dependencies, which keeps the whole tool a plain ``python main.py`` away.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import BinaryIO


class MidiError(Exception):
    pass


@dataclass
class Note:
    start: float          # seconds
    end: float            # seconds
    note: int             # MIDI note number
    velocity: int
    channel: int
    track: int

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class MidiSong:
    notes: list[Note] = field(default_factory=list)
    sustain: dict[int, list[tuple[float, bool]]] = field(default_factory=dict)
    track_names: list[str] = field(default_factory=list)
    tempos: list[tuple[float, float]] = field(default_factory=list)  # (seconds, bpm)
    title: str = ""

    @property
    def duration(self) -> float:
        return max((n.end for n in self.notes), default=0.0)

    @property
    def base_bpm(self) -> float:
        return self.tempos[0][1] if self.tempos else 120.0


# --------------------------------------------------------------------------
# low level chunk / event decoding
# --------------------------------------------------------------------------

class _Reader:
    """Cursor over one MTrk chunk."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise MidiError("unexpected end of track")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise MidiError("unexpected end of track")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def vlq(self) -> int:
        """Variable length quantity - 7 bits per byte, MSB is a continuation flag."""
        value = 0
        for _ in range(4):
            b = self.byte()
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                return value
        raise MidiError("over-long variable length quantity")


def _read_chunk(fh: BinaryIO) -> tuple[bytes, bytes] | None:
    header = fh.read(8)
    if len(header) < 8:
        return None
    ident, length = struct.unpack(">4sI", header)
    return ident, fh.read(length)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _parse_track(data: bytes) -> tuple[list[tuple[int, int, tuple]], str]:
    """Return (events, track_name) where each event is (tick, order, payload)."""
    r = _Reader(data)
    tick = 0
    status = 0
    events: list[tuple[int, int, tuple]] = []
    name = ""
    order = 0

    while not r.eof():
        tick += r.vlq()
        b = r.byte()

        if b == 0xFF:                                   # meta
            meta_type = r.byte()
            payload = r.take(r.vlq())
            if meta_type == 0x51 and len(payload) == 3:  # set tempo
                usec = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                events.append((tick, order, ("tempo", usec)))
            elif meta_type == 0x03 and not name:         # track name
                name = payload.decode("latin-1", "replace").strip()
            elif meta_type == 0x2F:                      # end of track
                break
            order += 1
            continue

        if b in (0xF0, 0xF7):                           # sysex - skip
            r.take(r.vlq())
            continue

        if b & 0x80:
            status = b
            d1 = r.byte()
        else:                                            # running status
            if not status:
                raise MidiError("running status with no prior status byte")
            d1 = b

        kind = status & 0xF0
        channel = status & 0x0F

        if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            d2 = r.byte()
        else:                                            # program change / aftertouch
            d2 = 0

        if kind == 0x90 and d2 > 0:
            events.append((tick, order, ("on", channel, d1, d2)))
        elif kind == 0x80 or (kind == 0x90 and d2 == 0):
            events.append((tick, order, ("off", channel, d1)))
        elif kind == 0xB0 and d1 == 64:
            events.append((tick, order, ("sustain", channel, d2 >= 64)))
        order += 1

    return events, name


def _tick_to_seconds_map(merged: list[tuple[int, int, int, tuple]], division: int):
    """Build a piecewise-linear tick->second conversion from the tempo events."""
    anchors = [(0, 0.0, 500000)]  # (tick, seconds, usec per quarter)
    seconds = 0.0
    last_tick = 0
    usec = 500000
    tempos: list[tuple[float, float]] = []

    for tick, _track, _order, payload in merged:
        if payload[0] != "tempo":
            continue
        seconds += (tick - last_tick) * usec / (division * 1_000_000.0)
        last_tick = tick
        usec = payload[1]
        anchors.append((tick, seconds, usec))
        tempos.append((seconds, 60_000_000.0 / usec))

    def convert(tick: int) -> float:
        lo, hi = 0, len(anchors) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if anchors[mid][0] <= tick:
                lo = mid
            else:
                hi = mid - 1
        a_tick, a_sec, a_usec = anchors[lo]
        return a_sec + (tick - a_tick) * a_usec / (division * 1_000_000.0)

    if not tempos:
        tempos = [(0.0, 120.0)]
    return convert, tempos


def read_midi(path: str) -> MidiSong:
    with open(path, "rb") as fh:
        chunk = _read_chunk(fh)
        if not chunk or chunk[0] != b"MThd":
            raise MidiError("not a Standard MIDI File (missing MThd header)")
        _fmt, ntracks, division = struct.unpack(">HHh", chunk[1][:6])

        raw_tracks: list[bytes] = []
        while len(raw_tracks) < ntracks:
            chunk = _read_chunk(fh)
            if chunk is None:
                break
            if chunk[0] == b"MTrk":
                raw_tracks.append(chunk[1])

    if not raw_tracks:
        raise MidiError("file contains no track data")

    # SMPTE time division: the high byte is a negative frame rate.
    smpte = division < 0
    if smpte:
        frames = -(division >> 8)
        ticks_per_frame = division & 0xFF
        division = int(frames * ticks_per_frame)
    if division <= 0:
        division = 480

    merged: list[tuple[int, int, int, tuple]] = []
    names: list[str] = []
    for i, raw in enumerate(raw_tracks):
        events, name = _parse_track(raw)
        names.append(name or f"Track {i}")
        for tick, order, payload in events:
            merged.append((tick, i, order, payload))
    merged.sort(key=lambda e: (e[0], e[2]))

    if smpte:                       # SMPTE files are already absolute time
        def convert(tick: int) -> float:
            return tick / division
        tempos = [(0.0, 120.0)]
    else:
        convert, tempos = _tick_to_seconds_map(merged, division)

    song = MidiSong(track_names=names, tempos=tempos)
    song.title = next((n for n in names if n), "")

    open_notes: dict[tuple[int, int, int], tuple[float, int]] = {}
    for tick, track, _order, payload in merged:
        t = convert(tick)
        if payload[0] == "on":
            _, channel, note, vel = payload
            key = (track, channel, note)
            if key in open_notes:            # retrigger without a note-off
                start, v = open_notes.pop(key)
                if t > start:
                    song.notes.append(Note(start, t, note, v, channel, track))
            open_notes[key] = (t, vel)
        elif payload[0] == "off":
            _, channel, note = payload
            key = (track, channel, note)
            if key in open_notes:
                start, vel = open_notes.pop(key)
                song.notes.append(Note(start, max(t, start + 0.01), note, vel, channel, track))
        elif payload[0] == "sustain":
            _, channel, down = payload
            song.sustain.setdefault(channel, []).append((t, down))

    tail = max((s for s, _ in open_notes.values()), default=0.0)
    for (track, channel, note), (start, vel) in open_notes.items():
        song.notes.append(Note(start, tail + 0.5, note, vel, channel, track))

    song.notes.sort(key=lambda n: (n.start, n.note))
    return song
