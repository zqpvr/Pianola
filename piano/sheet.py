"""Reader and writer for Virtual Piano letter notation.

This follows the semantics published at virtualpiano.net/how-to-play, which are
per-character rather than per-word: every note token advances the clock by one
unit, a space advances it by one more, and a pipe by two.  So ``asdf`` is a fast
run, ``a s d f`` is the same notes with a pause between each, ``as|df`` puts a
longer gap in the middle, and the documented ordering of ``as|df`` < ``as| df``
< ``as | df`` < ``as| |df`` falls out of the arithmetic.  Square brackets group
notes into a chord; a bracket containing spaces is the site's "play as fast as
possible" sequence and is rolled rather than struck together.

The model was checked against the Overworld Theme sheet, where it puts the high
G on unit 8, the low G on 12 and the third bar on 16 - the actual rhythm.

Roblox piano sheets use the same notation, since most of them are copied from
there in the first place.
"""

from __future__ import annotations

import os
import re

from .keymap import KEY_TO_NOTE
from .midi import MidiSong, Note

_COMMENT = re.compile(r"^\s*(//|#).*$", re.MULTILINE)

REST_CHARS = "-._"           # things sheet authors use to mean "nothing here"

SPACE_UNITS = 1.0            # one space is a short pause
PIPE_UNITS = 2.0             # a pipe is a longer one
PARAGRAPH_UNITS = 6.0        # a blank line is an extended pause
DEFAULT_UNITS_PER_BEAT = 2.0  # a unit is an eighth note
ROLL_SECONDS = 0.014         # spacing inside a "[a s d f]" fast sequence


def _resolve(char: str) -> int | None:
    """Map a sheet character to a MIDI note, forgiving casual capitalisation."""
    if char in KEY_TO_NOTE:
        return KEY_TO_NOTE[char]
    return KEY_TO_NOTE.get(char.swapcase())


def _clean(text: str) -> str:
    """Strip comments and the whitespace at both ends of every line.

    This matters more than it looks.  Under a per-character timing model each
    stray space lengthens a rest, and invisible indentation is exactly what
    arrives when a sheet is copied out of a web page or a chat message.  A line
    break is therefore always worth precisely one unit, whatever padding came
    with it, and a line of nothing but spaces still reads as a blank line.
    """
    text = _COMMENT.sub("", text)
    return "\n".join(line.strip() for line in text.splitlines())


def _whitespace_units(run: str) -> float:
    """How long a run of whitespace lasts.

    A single line break is just a break, worth the same as a space.  Two or more
    is a paragraph, which the notation treats as an extended pause.
    """
    if run.count("\n") >= 2:
        return PARAGRAPH_UNITS
    return sum(SPACE_UNITS for c in run if c in " \t\n")


def parse_sheet(text: str, bpm: float = 120.0, hold: float = 0.9,
                title: str = "sheet",
                units_per_beat: float = DEFAULT_UNITS_PER_BEAT) -> MidiSong:
    """Convert Virtual Piano letters into a song.

    ``bpm`` is the tempo in beats per minute and ``units_per_beat`` says how
    many note slots fit in a beat - two, the default, makes a unit an eighth
    note, which is what sheets on virtualpiano.net assume.
    """
    text = _clean(text)
    unit = 60.0 / max(bpm, 1.0) / max(units_per_beat, 0.1)
    length = unit * hold

    song = MidiSong(title=title, tempos=[(0.0, bpm)], track_names=["sheet"])
    add = song.notes.append

    t = 0.0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]

        if char.isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            t += _whitespace_units(text[start:i]) * unit
            continue

        if char == "|":
            t += PIPE_UNITS * unit
            i += 1
            continue

        if char == "[":
            close = text.find("]", i)
            if close == -1:
                close = n
            inner = text[i + 1:close]
            i = close + 1
            chars = [c for c in inner if not c.isspace()]
            notes = [(c, _resolve(c)) for c in chars]
            playable = [(c, m) for c, m in notes if m is not None]
            if not playable:
                t += unit
                continue
            # A bracket written with spaces is the site's "as fast as possible"
            # sequence rather than a true chord, so it gets rolled.
            spread = ROLL_SECONDS if any(c.isspace() for c in inner) else 0.0
            if spread and len(playable) > 1:
                spread = min(spread, length / len(playable))
            for index, (_c, midi) in enumerate(playable):
                offset = index * spread
                add(Note(t + offset, t + offset + length, midi, 96, 0, 0))
            t += unit
            continue

        if char in REST_CHARS:
            t += unit
            i += 1
            continue

        midi = _resolve(char)
        if midi is not None:
            add(Note(t, t + length, midi, 96, 0, 0))
        t += unit
        i += 1

    song.notes.sort(key=lambda note: (note.start, note.note))
    return song


def sheet_report(text: str) -> tuple[int, list[str]]:
    """Count playable notes and collect characters the layout has no key for.

    Sheets pasted off the web routinely carry stray punctuation, smart quotes or
    lyrics, and silently ignoring them makes a mangled paste look like a working
    one.  The editor shows this so the problem is visible before playback.
    """
    text = _clean(text)
    notes = 0
    unknown: set[str] = set()
    for char in text:
        if char.isspace() or char in "|[]" or char in REST_CHARS:
            continue
        if _resolve(char) is not None:
            notes += 1
        else:
            unknown.add(char)
    return notes, sorted(unknown)


# --------------------------------------------------------------------------
# writing sheets
# --------------------------------------------------------------------------

def _token(chord: list[str]) -> str:
    return chord[0] if len(chord) == 1 else "[" + "".join(chord) + "]"


def _separator(pause: int) -> str:
    """Spell a gap of ``pause`` units using pipes and spaces.

    Pipes carry two units and spaces one, so the shortest spelling is pipes
    first with a space to make up an odd remainder.
    """
    return "|" * (pause // 2) + " " * (pause % 2)


def to_sheet(arrangement, bpm: float, units_per_beat: float = DEFAULT_UNITS_PER_BEAT,
             width: int = 66) -> str:
    """Write an arrangement out as Virtual Piano letters.

    Onsets are quantised onto the unit grid, and gaps are spelled with the same
    pipes and spaces the notation uses, so parsing the result back at the same
    tempo reproduces the timing.  Lines are wrapped only where a gap contains a
    space that can become the line break, since a break is worth one unit and a
    pipe is worth two.
    """
    unit = 60.0 / max(bpm, 1.0) / max(units_per_beat, 0.1)
    grid: dict[int, list[str]] = {}
    for instant in arrangement.instants:
        if not instant.downs:
            continue
        slot = int(round(instant.t / unit))
        for char in instant.downs:
            if char not in grid.setdefault(slot, []):
                grid[slot].append(char)
    if not grid:
        return ""

    for chord in grid.values():
        chord.sort(key=lambda c: KEY_TO_NOTE.get(c, 0))

    out: list[str] = []
    line = 0
    previous: int | None = None
    for slot in sorted(grid):
        if previous is not None:
            pause = slot - previous - 1
            gap = _separator(pause)
            if line + len(gap) > width and " " in gap:
                gap = gap.replace(" ", "\n", 1)
                line = 0
            else:
                line += len(gap)
            out.append(gap)
        token = _token(grid[slot])
        out.append(token)
        line += len(token)
        previous = slot
    return "".join(out)


def read_sheet_file(path: str, bpm: float = 120.0, hold: float = 0.9,
                    units_per_beat: float = DEFAULT_UNITS_PER_BEAT) -> MidiSong:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    title = os.path.splitext(os.path.basename(path))[0]
    return parse_sheet(text, bpm, hold, title, units_per_beat)
