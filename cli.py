"""Command line front end - useful for scripting, batching and dry runs."""

from __future__ import annotations

import argparse
import os
import sys
import time

from piano import keymap
from piano.arrange import ArrangeConfig, arrange, best_transpose, score_transpose
from piano.hotkeys import HotkeyManager
from piano.keyboard import Keyboard
from piano.midi import MidiError, read_midi
from piano.player import Player
from piano.sheet import read_sheet_file, to_sheet


def load(path: str, bpm: float, units: float = 2.0):
    if path.lower().endswith((".mid", ".midi")):
        return read_midi(path)
    return read_sheet_file(path, bpm, units_per_beat=units)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="piano",
        description="Play a MIDI file or Virtual Piano sheet on a Roblox piano.")
    p.add_argument("song", nargs="?", help="a .mid/.midi file or a .txt sheet")
    p.add_argument("--gui", action="store_true", help="open the graphical player instead")

    p.add_argument("--speed", type=float, default=1.0, help="playback rate (default 1.0)")
    p.add_argument("--transpose", default="auto",
                   help="semitones, or 'auto' (default) to search for the best fit")
    p.add_argument("--any-key", action="store_true",
                   help="let auto transpose change the song's key, not just its octave")
    p.add_argument("--no-fold", action="store_true",
                   help="drop out-of-range notes instead of folding them into range")
    p.add_argument("--polyphony", type=int, default=6, help="max keys held at once")
    p.add_argument("--humanize", type=float, default=0.4, help="0 metronomic .. 1 loose")
    p.add_argument("--chord-window", type=float, default=32.0, metavar="MS")
    p.add_argument("--min-note", type=float, default=45.0, metavar="MS")
    p.add_argument("--min-gap", type=float, default=32.0, metavar="MS")
    p.add_argument("--keep-drums", action="store_true")
    p.add_argument("--sustain", action="store_true", help="follow the sustain pedal")
    p.add_argument("--bpm", type=float, default=120.0, help="tempo for text sheets")
    p.add_argument("--units", type=float, default=2.0, metavar="N",
                   help="letter slots per beat (2 = eighths, the site's default)")

    p.add_argument("--countdown", type=float, default=3.0, help="seconds before the first note")
    p.add_argument("--window", default="Roblox",
                   help="only type while a window with this in its title is focused")
    p.add_argument("--no-window-check", action="store_true")
    p.add_argument("--loop", action="store_true")

    p.add_argument("--analyze", action="store_true",
                   help="report what the arranger would do and exit")
    p.add_argument("--preview", action="store_true",
                   help="print the keystrokes instead of sending them")
    p.add_argument("--to-sheet", action="store_true",
                   help="print the song as Virtual Piano letters and exit")
    p.add_argument("--out-bpm", type=float, default=0.0,
                   help="tempo to write letters at (default: the song's own)")
    p.add_argument("--keys", action="store_true", help="print the key map and exit")
    return p


def config_from(args) -> ArrangeConfig:
    return ArrangeConfig(
        transpose=None if str(args.transpose).lower() == "auto" else int(args.transpose),
        octave_only=not args.any_key,
        fold_octaves=not args.no_fold,
        max_polyphony=max(1, args.polyphony),
        drop_drums=not args.keep_drums,
        chord_window_ms=args.chord_window,
        min_note_ms=args.min_note,
        min_gap_ms=args.min_gap,
        humanize=max(0.0, min(1.0, args.humanize)),
        sustain_pedal=args.sustain,
    )


def print_keys() -> None:
    print("61-key Virtual Piano layout (t is middle C):\n")
    for note in range(keymap.NOTE_MIN, keymap.NOTE_MAX + 1):
        end = "\n" if note % 12 == 11 else "  "
        print(f"{keymap.note_name(note):>4} {keymap.NOTE_TO_KEY[note]}", end=end)
    print("\n\nShifted characters are accidentals and share a physical key with"
          "\nthe natural below them, so C and C# can never sound together.")


def analyze(song, cfg: ArrangeConfig) -> None:
    print(f"title      : {song.title or '(untitled)'}")
    print(f"notes      : {len(song.notes)}")
    print(f"length     : {song.duration:.1f}s at {song.base_bpm:.0f} bpm")
    if song.notes:
        low = min(n.note for n in song.notes)
        high = max(n.note for n in song.notes)
        print(f"range      : {keymap.note_name(low)} to {keymap.note_name(high)} "
              f"({high - low} semitones, the keyboard holds 60)")
    octave, cov = best_transpose(song.notes, 24, True)
    free = max(range(-24, 25), key=lambda s: (score_transpose(song.notes, s), -abs(s)))
    print(f"transpose  : {octave:+d} keeps the key ({cov * 100:.1f}% in range); "
          f"best of any interval is {free:+d} at "
          f"{score_transpose(song.notes, free) * 100:.1f}% (--any-key)")
    print(f"unshifted  : {score_transpose(song.notes, 0) * 100:.1f}% in range")
    arrangement = arrange(song, cfg)
    print(f"arranged   : {arrangement.summary()}")
    bursts = sum(1 for i in arrangement.instants if i.downs)
    print(f"keystrokes : {sum(len(i.downs) for i in arrangement.instants)} presses "
          f"in {bursts} bursts")


def preview(arrangement, limit: int = 60) -> None:
    print(f"{arrangement.summary()}\n")
    print(f"{'time':>8}  keys")
    shown = 0
    for instant in arrangement.instants:
        if not instant.downs:
            continue
        print(f"{instant.t:8.3f}  {''.join(instant.downs)}")
        shown += 1
        if shown >= limit:
            print(f"... {len(arrangement.instants) - shown} more bursts")
            break


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.keys:
        print_keys()
        return 0
    if args.gui or not args.song:
        from gui import main as gui_main
        gui_main()
        return 0
    if not os.path.exists(args.song):
        print(f"no such file: {args.song}", file=sys.stderr)
        return 2

    try:
        song = load(args.song, args.bpm, args.units)
    except (MidiError, OSError, ValueError) as exc:
        print(f"could not read {args.song}: {exc}", file=sys.stderr)
        return 2
    if not song.notes:
        print("that file has no playable notes", file=sys.stderr)
        return 2
    if not song.title:
        song.title = os.path.splitext(os.path.basename(args.song))[0]

    cfg = config_from(args)
    if args.analyze:
        analyze(song, cfg)
        return 0

    if args.to_sheet:
        # Quantising a humanised performance would only blur the grid, so the
        # written sheet is always taken from a straight reading.
        cfg.humanize = 0.0
        bpm = args.out_bpm or song.base_bpm
        print(to_sheet(arrange(song, cfg), bpm, args.units))
        return 0

    arrangement = arrange(song, cfg)
    if args.preview:
        preview(arrangement)
        return 0

    player = Player(Keyboard())
    player.load(arrangement)
    player.rate = max(0.05, args.speed)
    player.loop = args.loop
    player.countdown = args.countdown
    player.focus_filter = None if args.no_window_check else args.window

    player.on_message = lambda text: print(f"\r{text:<60}", end="", flush=True)

    def progress(pos: float, total: float) -> None:
        bar = int(24 * pos / total) if total else 0
        print(f"\r[{'=' * bar}{' ' * (24 - bar)}] {pos:6.1f}s / {total:.1f}s ",
              end="", flush=True)

    player.on_progress = progress

    hotkeys = HotkeyManager()
    hotkeys.bind("f1", player.toggle)
    hotkeys.bind("f2", player.stop)
    hotkeys.bind("f4", player.panic)
    hotkeys.start()

    print(f"{song.title}  -  {arrangement.summary()}")
    print("F1 play/pause   F2 stop   F4 release keys   Ctrl+C quit")
    try:
        player.play()
        while player.running:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()
        hotkeys.stop()
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
