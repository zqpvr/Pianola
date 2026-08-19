"""Playback engine: walks an arrangement against a high-resolution clock.

Timing is the whole job here.  ``time.sleep`` alone drifts by several
milliseconds, which is audible as a smeared chord, so the loop sleeps until it
is nearly due and then spins out the last stretch.  The clock is virtual rather
than wall-clock, which is what makes live speed changes, pausing and seeking
possible without rebuilding the arrangement.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .arrange import Arrangement
from .keyboard import Keyboard, TimerResolution, foreground_title

IDLE = "idle"
COUNTDOWN = "countdown"
PLAYING = "playing"
PAUSED = "paused"
WAITING = "waiting"          # correct window is not focused
FINISHED = "finished"

_SPIN_THRESHOLD = 0.0025     # spin out the final 2.5 ms for sub-ms accuracy
_MAX_NAP = 0.005             # never sleep so long that a stop request lags


class Player:
    def __init__(self, keyboard: Keyboard | None = None):
        self.keyboard = keyboard or Keyboard()
        self.arrangement: Arrangement | None = None

        self.rate = 1.0
        self.loop = False
        self.countdown = 3.0
        self.focus_filter: str | None = "Roblox"

        self.on_state: Callable[[str], None] | None = None
        self.on_progress: Callable[[float, float], None] | None = None
        self.on_message: Callable[[str], None] | None = None

        self._state = IDLE
        self._pos = 0.0
        self._index = 0
        self._skip_countdown = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()

    # -- observation -------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def position(self) -> float:
        return self._pos

    @property
    def duration(self) -> float:
        return self.arrangement.duration if self.arrangement else 0.0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            if self.on_state:
                self.on_state(state)

    def _say(self, text: str) -> None:
        if self.on_message:
            self.on_message(text)

    # -- transport ---------------------------------------------------------
    def load(self, arrangement: Arrangement) -> None:
        self.stop()
        self.arrangement = arrangement
        self._pos = 0.0
        self._index = 0
        self._set_state(IDLE)

    def play(self, from_start: bool = True) -> None:
        if not self.arrangement or not self.arrangement.instants:
            self._say("Nothing loaded.")
            return
        if self.running:
            if self._pause.is_set():
                self.resume()
            return
        if from_start:
            self._pos = 0.0
            self._index = 0
        self._stop.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._run, name="piano-player", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.running and not self._pause.is_set():
            self._pause.set()
            self.keyboard.release_all()
            self._set_state(PAUSED)

    def resume(self) -> None:
        if self.running and self._pause.is_set():
            self._pause.clear()
            self._set_state(PLAYING)

    def toggle(self) -> None:
        if not self.running:
            self.play(from_start=self._state in (IDLE, FINISHED))
        elif self._pause.is_set():
            self.resume()
        else:
            self.pause()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self.keyboard.release_all()
        self._thread = None
        if self._state != IDLE:
            self._set_state(IDLE)

    def seek(self, seconds: float) -> None:
        """Jump the virtual clock, re-homing the event cursor and dropping holds."""
        if not self.arrangement:
            return
        with self._lock:
            self._pos = max(0.0, min(seconds, self.arrangement.duration))
            instants = self.arrangement.instants
            lo, hi = 0, len(instants)
            while lo < hi:
                mid = (lo + hi) // 2
                if instants[mid].t < self._pos:
                    lo = mid + 1
                else:
                    hi = mid
            self._index = lo
        self.keyboard.release_all()

    def panic(self) -> None:
        self.stop()
        self.keyboard.release_all()
        self._say("All keys released.")

    # -- the loop ----------------------------------------------------------
    def _sleep_until(self, deadline: float) -> None:
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0 or self._stop.is_set():
                return
            if remaining > _SPIN_THRESHOLD:
                time.sleep(min(remaining - _SPIN_THRESHOLD, _MAX_NAP))
                if remaining > _MAX_NAP:
                    return          # re-evaluate state between naps
            else:
                while time.perf_counter() < deadline:
                    pass
                return

    def _focus_ok(self) -> bool:
        if not self.focus_filter:
            return True
        return self.focus_filter.lower() in foreground_title().lower()

    def _run(self) -> None:
        arrangement = self.arrangement
        assert arrangement is not None
        instants = arrangement.instants
        released_for_wait = False

        with TimerResolution(1):
            if self.countdown > 0 and self._pos <= 0 and not self._skip_countdown:
                self._set_state(COUNTDOWN)
                end = time.perf_counter() + self.countdown
                while not self._stop.is_set():
                    left = end - time.perf_counter()
                    if left <= 0:
                        break
                    self._say(f"Starting in {left:.0f}...")
                    time.sleep(min(0.25, left))

            self._skip_countdown = False
            self._set_state(PLAYING)
            last = time.perf_counter()
            last_report = 0.0

            while not self._stop.is_set():
                now = time.perf_counter()
                elapsed = now - last
                last = now

                if self._pause.is_set():
                    time.sleep(0.02)
                    continue

                if not self._focus_ok():
                    if not released_for_wait:
                        self.keyboard.release_all()
                        released_for_wait = True
                        self._set_state(WAITING)
                        self._say(f"Waiting for a window matching '{self.focus_filter}'.")
                    time.sleep(0.05)
                    last = time.perf_counter()
                    continue
                if released_for_wait:
                    released_for_wait = False
                    self._set_state(PLAYING)

                with self._lock:
                    self._pos += elapsed * max(self.rate, 0.01)
                    pos = self._pos
                    index = self._index

                    fired = False
                    while index < len(instants) and instants[index].t <= pos:
                        instant = instants[index]
                        self.keyboard.press_batch(instant.downs, instant.ups)
                        index += 1
                        fired = True
                    self._index = index

                if self.on_progress and (pos - last_report > 0.05 or fired):
                    last_report = pos
                    self.on_progress(pos, arrangement.duration)

                if index >= len(instants):
                    break

                wait = (instants[index].t - self._pos) / max(self.rate, 0.01)
                if wait > 0:
                    self._sleep_until(time.perf_counter() + wait)

            self.keyboard.release_all()

        if self._stop.is_set():
            self._set_state(IDLE)
            return

        if self.loop:
            self._pos = 0.0
            self._index = 0
            self._thread = None
            self._skip_countdown = True
            self._say("Looping.")
            self.play(from_start=True)
            return

        self._thread = None
        self._set_state(FINISHED)
        self._say("Done.")
        if self.on_progress:
            self.on_progress(arrangement.duration, arrangement.duration)
