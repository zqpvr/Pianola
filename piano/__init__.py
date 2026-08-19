"""A smart auto player for the 61-key pianos found in Roblox."""

from .arrange import ArrangeConfig, Arrangement, arrange, best_transpose
from .config import Settings
from .keyboard import Keyboard, foreground_title
from .keymap import NOTE_MAX, NOTE_MIN, NOTE_TO_KEY, note_name
from .midi import MidiSong, Note, read_midi
from .player import Player
from .sheet import parse_sheet, read_sheet_file, sheet_report, to_sheet

__version__ = "1.0.0"

__all__ = [
    "ArrangeConfig", "Arrangement", "arrange", "best_transpose",
    "Settings", "Keyboard", "foreground_title",
    "NOTE_MIN", "NOTE_MAX", "NOTE_TO_KEY", "note_name",
    "MidiSong", "Note", "read_midi", "Player",
    "parse_sheet", "read_sheet_file", "sheet_report", "to_sheet",
]
