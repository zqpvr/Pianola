"""System-wide hotkeys so the player can be driven while Roblox has focus.

Windows registers them with ``RegisterHotKey``.  Linux takes the first source
the session allows (evdev, the desktop's shortcut portal, an X11 grab) and also
listens for ``pianola --control``.  Both expose the same ``HotkeyManager``.
Callbacks fire on a worker thread - anything touching a GUI should marshal back
with ``after``.
"""

import sys

CONTROL_ACTIONS = ("play_pause", "stop", "restart", "panic", "speed_down", "speed_up")

if sys.platform == "win32":
    from ._win import HotkeyManager

    def send_control(action: str) -> bool:
        raise NotImplementedError("--control is for Linux; Windows hotkeys work in every window")
else:
    from ._linux_hotkeys import HotkeyManager, send_control

__all__ = ["CONTROL_ACTIONS", "HotkeyManager", "send_control"]
