"""Playback engine: walks an arrangement against a high-resolution clock.

Timing is the whole job here.  ``time.sleep`` alone drifts by several
milliseconds, which is audible as a smeared chord, so the loop sleeps until it
is nearly due and then spins out the last stretch.  The clock is virtual rather
than wall-clock, which is what makes live speed changes, pausing and seeking
possible without rebuilding the arrangement.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from .arrange import Arrangement
from .config import DEFAULT_WINDOW
from .keyboard import Keyboard, TimerResolution, foreground_title

IDLE = "idle"
COUNTDOWN = "countdown"
PLAYING = "playing"
PAUSED = "paused"
WAITING = "waiting"          # correct window is not focused
FINISHED = "finished"

_SPIN_THRESHOLD = 0.0025     # spin out the final 2.5 ms for sub-ms accuracy
_MAX_NAP = 0.005             # never sleep so long that a stop request lags
_LATE = 0.005                # a burst this far behind its slot counts as late in the log

log = logging.getLogger(__name__)


class Player:
    def __init__(self, keyboard: Keyboard | None = None):
        self.keyboard = keyboard or Keyboard()
        self.arrangement: Arrangement | None = None

        self.rate = 1.0
        self.loop = False
        self.countdown = 3.0
        self.focus_filter: str | None = DEFAULT_WINDOW

        self.on_state: Callable[[str], None] | None = None
        self.on_progress: Callable[[float, float], None] | None = None
        self.on_message: Callable[[str], None] | None = None

        self._state = IDLE
        self._pos = 0.0
        self._index = 0
        self._skip_countdown = False
        self._counting = False
        self._guard_noted = False
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
            log.info("state %s -> %s", self._state, state)
            self._state = state
            if self.on_state:
                self.on_state(state)

    def _say(self, text: str) -> None:
        log.info("%s", text)
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
        self._spawn()

    def _spawn(self) -> None:
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
            self._set_state(COUNTDOWN if self._counting else PLAYING)

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
        title = foreground_title()
        if title is None:           # this session cannot report the focused window
            if not self._guard_noted:
                self._guard_noted = True
                self._say("This desktop cannot report the focused window, "
                          "so the window guard is off.")
            return True
        return self.focus_filter.lower() in title.lower()

    def _run(self) -> None:
        arrangement = self.arrangement
        assert arrangement is not None
        instants = arrangement.instants
        released_for_wait = False

        try:
            self.keyboard.prepare()
        except RuntimeError as exc:
            self._say(str(exc))
            self._thread = None
            self._set_state(IDLE)
            return

        with TimerResolution(1):
            if self.countdown > 0 and self._pos <= 0 and not self._skip_countdown:
                self._counting = True
                self._set_state(PAUSED if self._pause.is_set() else COUNTDOWN)
                end = time.perf_counter() + self.countdown
                while not self._stop.is_set():
                    if self._pause.is_set():          # a paused countdown waits too
                        paused_at = time.perf_counter()
                        time.sleep(0.05)
                        end += time.perf_counter() - paused_at
                        continue
                    left = end - time.perf_counter()
                    if left <= 0:
                        break
                    self._say(f"Starting in {math.ceil(left)}...")
                    time.sleep(min(0.25, left))
                self._counting = False

            self._skip_countdown = False
            if not self._stop.is_set():     # a stop during the countdown goes straight to idle
                self._set_state(PAUSED if self._pause.is_set() else PLAYING)
            send_error: OSError | None = None
            last = time.perf_counter()
            last_report = 0.0
            bursts, late, worst = 0, 0, 0.0

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
                        # Naming what does have focus makes a wrong filter obvious.
                        focused = foreground_title()
                        self._say(f"Waiting for a window matching '{self.focus_filter}'"
                                  + (f" (focused: '{focused}')." if focused else "."))
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
                        try:
                            self.keyboard.press_batch(instant.downs, instant.ups)
                        except OSError as exc:
                            # Input refused, as when Windows locks or a UAC prompt
                            # takes the desktop: stop rather than die mid-song.
                            send_error = exc
                            break
                        behind = (pos - instant.t) / max(self.rate, 0.01)
                        bursts += 1
                        late += behind > _LATE
                        worst = max(worst, behind)
                        index += 1
                        fired = True
                    self._index = index

                if send_error:
                    break

                if self.on_progress and (pos - last_report > 0.05 or fired):
                    last_report = pos
                    self.on_progress(pos, arrangement.duration)

                if index >= len(instants):
                    break

                wait = (instants[index].t - self._pos) / max(self.rate, 0.01)
                if wait > 0:
                    self._sleep_until(time.perf_counter() + wait)

            log.info("timing: %d bursts, worst %.1f ms behind, %d over %.0f ms",
                     bursts, worst * 1000, late, _LATE * 1000)
            self.keyboard.release_all()

        if send_error:
            log.error("sending keys failed: %s", send_error)
            self._thread = None
            self._set_state(IDLE)
            self._say(f"Could not send keys ({send_error.strerror or send_error}); stopped.")
            return

        if self._stop.is_set():
            self._set_state(IDLE)
            return

        if self.loop:
            self._pos = 0.0
            self._index = 0
            self._skip_countdown = True
            self._say("Looping.")
            self._spawn()           # not play(): that would clear a stop requested meanwhile
            return

        self._thread = None
        self._set_state(FINISHED)
        self._say("Done.")
        if self.on_progress:
            self.on_progress(arrangement.duration, arrangement.duration)
