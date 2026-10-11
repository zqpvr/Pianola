"""Keystroke delivery: the key bookkeeping, over a per-platform sender.

Roblox listens to the normal keyboard stream, so scancode-level events are
indistinguishable from a real key press.  The layer also owns the shift state:
accidentals live on shifted keys, and a natural sent while shift happens to be
down produces the wrong note, so shift is raised and lowered around each burst
rather than left to chance.

Keys are identified by Windows virtual-key code throughout; the Linux backend
translates them to evdev codes at the edge.
"""

from __future__ import annotations

import logging
import sys
import threading

if sys.platform == "win32":
    from ._win import TimerResolution, describe, foreground_title, send_keys
    from ._win import prepare as _prepare
else:
    from ._linux import TimerResolution, describe, foreground_title, send_keys
    from ._linux import prepare as _prepare

log = logging.getLogger(__name__)

VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1


# Explicit US-layout table.  Virtual Piano sheets describe physical key
# positions, so we deliberately do not follow the user's active layout.
_CHAR_VK: dict[str, tuple[int, bool]] = {}
for _c in "0123456789":
    _CHAR_VK[_c] = (0x30 + int(_c), False)
for _i in range(26):
    _CHAR_VK[chr(ord("a") + _i)] = (0x41 + _i, False)
    _CHAR_VK[chr(ord("A") + _i)] = (0x41 + _i, True)
for _shifted, _base in zip(")!@#$%^&*(", "0123456789"):
    _CHAR_VK[_shifted] = (0x30 + int(_base), True)


def _key_name(vk: int) -> str:
    if vk == VK_LSHIFT:
        return "Shift"
    if vk == VK_RSHIFT:
        return "RShift"
    return chr(vk).lower()


def needs_shift(char: str) -> bool:
    entry = _CHAR_VK.get(char)
    return bool(entry and entry[1])


class Keyboard:
    """Sends key events, tracking every key it is currently holding down.

    ``dry_run`` swaps real input for a recorded log, which is how the test
    harness and the preview mode exercise the whole pipeline safely.
    """

    def __init__(self, dry_run: bool = False, shift_vk: int = VK_LSHIFT):
        self.dry_run = dry_run
        self.shift_vk = shift_vk
        self.log: list[tuple[str, bool]] = []
        self.raw_log: list[list[tuple[int, bool]]] = []
        self._held: set[str] = set()
        self._shift_down = False
        self._lock = threading.Lock()

    def prepare(self) -> None:
        """Do any slow one-off setup now, so the first note is not late."""
        if not self.dry_run:
            _prepare()

    # -- raw ---------------------------------------------------------------
    def _send(self, items: list[tuple[int, bool]]) -> None:
        """items: list of (virtual key, is_key_down)."""
        if not items:
            return
        if self.dry_run:
            self.raw_log.append(list(items))
            return
        if log.isEnabledFor(logging.DEBUG):
            log.debug("keys %s", " ".join(("+" if down else "-") + _key_name(vk)
                                         for vk, down in items))
        send_keys(items)

    def _set_shift(self, want: bool, batch: list[tuple[int, bool]]) -> None:
        if want != self._shift_down:
            batch.append((self.shift_vk, want))
            self._shift_down = want

    # -- note level --------------------------------------------------------
    def press_batch(self, downs: list[str], ups: list[str]) -> None:
        """Apply one instant of the score: releases first, then presses.

        Presses are ordered naturals-before-accidentals so shift toggles at most
        once per instant no matter how mixed the chord is.
        """
        with self._lock:
            batch: list[tuple[int, bool]] = []

            for char in ups:
                entry = _CHAR_VK.get(char)
                if entry and char in self._held:
                    batch.append((entry[0], False))
                    self._held.discard(char)
                    self.log.append((char, False))

            plain = [c for c in downs if c in _CHAR_VK and not needs_shift(c)]
            shifted = [c for c in downs if c in _CHAR_VK and needs_shift(c)]

            if plain:
                self._set_shift(False, batch)
                for char in plain:
                    batch.append((_CHAR_VK[char][0], True))
                    self._held.add(char)
                    self.log.append((char, True))
            if shifted:
                self._set_shift(True, batch)
                for char in shifted:
                    batch.append((_CHAR_VK[char][0], True))
                    self._held.add(char)
                    self.log.append((char, True))

            self._send(batch)

    def release_all(self) -> None:
        """Panic button - lets go of every key this object still holds."""
        with self._lock:
            batch = [(_CHAR_VK[c][0], False) for c in self._held if c in _CHAR_VK]
            self._held.clear()
            if self._shift_down:
                batch.append((self.shift_vk, False))
                self._shift_down = False
            # Both shifts, unconditionally: cheap insurance against a stuck modifier.
            batch.append((VK_LSHIFT, False))
            batch.append((VK_RSHIFT, False))
            try:
                self._send(batch)
            except OSError as exc:  # stop and panic must never be what fails
                log.warning("could not release keys: %s", exc)

    @property
    def held(self) -> set[str]:
        return set(self._held)
