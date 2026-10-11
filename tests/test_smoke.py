"""Smoke tests: the paths a release has to get right, on Windows and Linux.

    python -m unittest discover -s tests -v

Nothing here types into the desktop: players use a dry-run keyboard, and the
GUI test only builds the window and closes it.  Settings go to a temporary
folder rather than beside the app.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest

HOME = tempfile.mkdtemp(prefix="pianola-test-")
os.environ["PIANOLA_HOME"] = HOME                       # before anything reads settings
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DEMO = os.path.join(ROOT, "songs", "demo.txt")

import cli  # noqa: E402
from piano import config  # noqa: E402
from piano.arrange import ArrangeConfig, arrange  # noqa: E402
from piano.keyboard import Keyboard  # noqa: E402
from piano.midi import MidiError, read_midi  # noqa: E402
from piano.player import COUNTDOWN, FINISHED, IDLE, PAUSED, PLAYING, Player  # noqa: E402
from piano.sheet import read_sheet_file  # noqa: E402


def run_cli(*args: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(args))
    assert code == 0, f"exit {code}"
    return out.getvalue()


def wait_for(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def midi_bytes(tempo_usec: int) -> bytes:
    track = (b"\x00\xff\x51\x03" + tempo_usec.to_bytes(3, "big")
             + b"\x00\x90\x3c\x40" + b"\x83\x60\x80\x3c\x00" + b"\x00\xff\x2f\x00")
    return (b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
            + b"MTrk" + struct.pack(">I", len(track)) + track)


class Cli(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            cli.main(["--version"])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn("Pianola", out.getvalue())

    def test_reports(self):
        self.assertIn("t is middle C", run_cli("--keys"))
        self.assertIn("66/66 notes", run_cli(DEMO, "--analyze"))
        self.assertIn("time  keys", run_cli(DEMO, "--preview"))
        self.assertTrue(run_cli(DEMO, "--to-sheet").strip())


class Midi(unittest.TestCase):
    def write(self, data: bytes) -> str:
        path = os.path.join(HOME, "t.mid")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_reads_a_note(self):
        song = read_midi(self.write(midi_bytes(500000)))
        self.assertEqual([n.note for n in song.notes], [60])

    def test_zero_tempo_is_ignored(self):
        self.assertEqual(len(read_midi(self.write(midi_bytes(0))).notes), 1)

    def test_truncated_header(self):
        with self.assertRaises(MidiError):
            read_midi(self.write(b"MThd" + struct.pack(">I", 2) + b"\x00\x00"))


class FailingKeyboard(Keyboard):
    def press_batch(self, downs, ups):
        raise OSError(5, "Access is denied")


class Playback(unittest.TestCase):
    def player(self, keyboard=None, countdown=0.0, rate=20.0) -> Player:
        song = read_sheet_file(DEMO, 120, units_per_beat=2)
        player = Player(keyboard or Keyboard(dry_run=True))
        player.load(arrange(song, ArrangeConfig()))
        player.countdown, player.rate, player.focus_filter = countdown, rate, None
        self.messages: list[str] = []
        player.on_message = self.messages.append
        self.addCleanup(player.stop)
        return player

    def test_plays_to_the_end(self):
        player = self.player()
        player.play()
        self.assertTrue(wait_for(lambda: player.state == FINISHED))
        presses = [c for c, down in player.keyboard.log if down]
        self.assertEqual(len(presses), 66)
        self.assertEqual(player.keyboard.held, set())

    def test_send_failure_stops_cleanly(self):
        player = self.player(FailingKeyboard(dry_run=True))
        player.play()
        self.assertTrue(wait_for(lambda: not player.running))
        self.assertEqual(player.state, IDLE)
        self.assertTrue(any("Could not send keys" in m for m in self.messages), self.messages)

    def test_pause_during_countdown(self):
        player = self.player(countdown=0.4, rate=1.0)
        player.play()
        self.assertTrue(wait_for(lambda: player.state == COUNTDOWN))
        player.toggle()
        self.assertEqual(player.state, PAUSED)
        time.sleep(0.6)                              # longer than the countdown
        self.assertEqual(player.state, PAUSED)
        self.assertEqual(player.position, 0.0)
        player.toggle()
        self.assertEqual(player.state, COUNTDOWN)
        self.assertTrue(wait_for(lambda: player.state == PLAYING))

    def test_countdown_never_shows_zero(self):
        player = self.player(countdown=0.6, rate=1.0)
        player.play()
        self.assertTrue(wait_for(lambda: player.state == PLAYING))
        counts = [m for m in self.messages if m.startswith("Starting in")]
        self.assertTrue(counts)
        self.assertNotIn("Starting in 0...", counts)

    def test_waiting_names_the_focused_window(self):
        import piano.player
        real = piano.player.foreground_title
        piano.player.foreground_title = lambda: "Sober"
        self.addCleanup(setattr, piano.player, "foreground_title", real)
        player = self.player()
        player.focus_filter = "Roblox"
        player.play()
        self.assertTrue(wait_for(lambda: any("focused: 'Sober'" in m for m in self.messages)))

    def test_stop_during_countdown_goes_straight_to_idle(self):
        player = self.player(countdown=5.0, rate=1.0)
        states: list[str] = []
        player.on_state = states.append
        player.play()
        self.assertTrue(wait_for(lambda: player.state == COUNTDOWN))
        player.stop()
        self.assertTrue(wait_for(lambda: not player.running))
        self.assertNotIn(PLAYING, states)

    def test_stop_ends_a_loop(self):
        player = self.player(rate=50.0)
        player.loop = True
        player.play()
        self.assertTrue(wait_for(lambda: any(m == "Looping." for m in self.messages)))
        player.stop()
        self.assertTrue(wait_for(lambda: not player.running, timeout=2.0))


class Settings(unittest.TestCase):
    def test_saved_window_filter(self):
        loaded = config.Settings.from_dict({"focus_filter": "Roblox"}).focus_filter
        self.assertEqual(loaded, "Roblox" if sys.platform == "win32" else "Sober")
        self.assertEqual(config.Settings.from_dict({"focus_filter": "Virtual Piano"}).focus_filter,
                         "Virtual Piano")

    def test_settings_live_in_pianola_home(self):
        self.assertEqual(config.data_dir(), HOME)
        self.assertEqual(os.path.dirname(config.CONFIG_PATH), HOME)


class QuietHotkeys:
    """Stands in on Linux, where the real manager may ask the desktop to approve
    shortcuts, which is no thing to do mid-test."""

    failed: list[str] = []
    on_status = None

    def bind(self, *args):
        pass

    def start(self):
        pass

    def stop(self):
        pass


class Gui(unittest.TestCase):
    def test_closes_with_an_empty_spinbox(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:                     # no display, or no Tk at all
            self.skipTest(f"no Tk display: {exc}")
        import gui
        if sys.platform != "win32":
            gui.HotkeyManager = QuietHotkeys
        app = gui.App(root)
        root.update()
        app.vars["countdown"].set("")               # what clearing the box leaves behind
        app.vars["max_polyphony"].set("")
        app._changed(live=False)
        app.quit()                                   # saves settings and destroys the window
        with open(config.CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["countdown"], 3.0)
        self.assertEqual(saved["arrange"]["max_polyphony"], 6)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux backend")
class LinuxPieces(unittest.TestCase):
    def test_dbus_marshalling_round_trips(self):
        from piano import _dbus
        writer = _dbus._Writer()
        sig = "(sua(sa{sv})btxd)"
        value = ("hi", 7, [("a", {"k": _dbus.Variant("s", "v")}), ("b", {})], True, 2**40, -5, 1.5)
        writer.put(sig, value)
        back = _dbus._Reader(bytes(writer.buf), "<").get(sig)
        self.assertEqual(back, ("hi", 7, [("a", {"k": "v"}), ("b", {})], True, 2**40, -5, 1.5))

    def test_releases_alone_create_no_device(self):
        from piano import _linux
        Keyboard().release_all()
        self.assertIsNone(_linux._device)


if __name__ == "__main__":
    unittest.main()
