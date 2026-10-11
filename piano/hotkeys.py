"""System-wide hotkeys so the player can be driven while Roblox has focus.

Windows registers them with ``RegisterHotKey``; Linux watches the evdev
keyboards.  Both expose the same ``HotkeyManager``.  Callbacks fire on a worker
thread - anything touching a GUI should marshal back with ``after``.
"""

import sys

if sys.platform == "win32":
    from ._win import HotkeyManager
else:
    from ._linux import HotkeyManager

__all__ = ["HotkeyManager"]
