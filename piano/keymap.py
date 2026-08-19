"""The 61-key Virtual Piano layout used by essentially every Roblox piano.

Five octaves of naturals across the number row / QWERTY rows plus a final top C,
with the accidentals sitting on the shifted version of the same physical key.
``t`` is middle C (MIDI 60), so the board spans C2 (36) to C7 (96).
"""

from __future__ import annotations

NOTE_MIN = 36           # C2  -> "1"
NOTE_MAX = 96           # C7  -> "m"

_WHITE_OFFSETS = (0, 2, 4, 5, 7, 9, 11)
_BLACK_OFFSETS = (1, 3, 6, 8, 10)

_WHITE_ROWS = ("1234567", "890qwer", "tyuiopa", "sdfghjk", "lzxcvbn")
_BLACK_ROWS = ("!@$%^", "*(QWE", "TYIOP", "SDGHJ", "LZCVB")

NOTE_TO_KEY: dict[int, str] = {}
for _octave, (_whites, _blacks) in enumerate(zip(_WHITE_ROWS, _BLACK_ROWS)):
    _base = NOTE_MIN + 12 * _octave
    for _offset, _char in zip(_WHITE_OFFSETS, _whites):
        NOTE_TO_KEY[_base + _offset] = _char
    for _offset, _char in zip(_BLACK_OFFSETS, _blacks):
        NOTE_TO_KEY[_base + _offset] = _char
NOTE_TO_KEY[NOTE_MAX] = "m"

KEY_TO_NOTE: dict[str, int] = {v: k for k, v in NOTE_TO_KEY.items()}

_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


_UNSHIFT = {")": "0", "!": "1", "@": "2", "#": "3", "$": "4",
            "%": "5", "^": "6", "&": "7", "*": "8", "(": "9"}


def physical(char: str) -> str:
    """The physical key a sheet character sits on, ignoring shift.

    Every accidental shares its key with the natural a semitone below it, so
    'T' and 't' are one key and can never sound at the same time.  Callers use
    this to detect and resolve those collisions.
    """
    return _UNSHIFT.get(char, char.lower())


def collides(a: int, b: int) -> bool:
    """True if two notes would fight over the same physical key."""
    ka, kb = NOTE_TO_KEY.get(a), NOTE_TO_KEY.get(b)
    return bool(ka and kb and physical(ka) == physical(kb))


def in_range(note: int) -> bool:
    return NOTE_MIN <= note <= NOTE_MAX


def key_for(note: int) -> str | None:
    return NOTE_TO_KEY.get(note)


def fold_into_range(note: int) -> int:
    """Drop or raise by whole octaves until the note lands on the board."""
    while note < NOTE_MIN:
        note += 12
    while note > NOTE_MAX:
        note -= 12
    return note


def note_name(note: int) -> str:
    return f"{_NAMES[note % 12]}{note // 12 - 1}"


def is_sharp(note: int) -> bool:
    return note % 12 in _BLACK_OFFSETS
