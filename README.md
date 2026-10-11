# Pianola

Plays MIDI files and Virtual Piano sheets on the 61-key pianos in Roblox.

It reads Standard MIDI files or plain-text Virtual Piano sheets and performs them
on the keyboard, for the pianos across Roblox (Got Talent, Piano Sheets, the
free-play games) and for [virtualpiano.net](https://virtualpiano.net/) itself.
The name comes from the self-playing pianos of the 1900s, which read a
perforated paper roll and worked the keys themselves.

Sending the keystrokes is the easy half, about a dozen lines of `ctypes`. The
hard half is that a MIDI file is almost never playable as written. A piano roll
routinely spans seven octaves where the game offers five, stacks ten voices where
it wants a handful, and retriggers notes faster than a keyboard event can
round-trip through Windows. Most auto players blast the raw note stream at the
game and produce something that is recognisably the wrong song. This one arranges
first.

Pure Python, no third-party packages. `python main.py` (`./run.sh` on Linux) is the whole installation.

## Features

- Transposition chosen by scoring every shift on how much of the song lands in
  range, weighted by duration rather than note count.
- Chord thinning that keeps the melody and the root, and prefers pitch classes
  nothing else in the chord covers.
- Detects notes that share a physical key, which are unplayable together no
  matter what the MIDI says.
- Reads and writes Virtual Piano letter notation, with per-character timing.
- Converts a loaded MIDI back out as a sheet.
- Humanisation on by default: chords roll from the bottom, onsets scatter a few
  milliseconds.
- Events land within about a millisecond of where they belong.
- Only types into a window whose title matches, so alt-tabbing away pauses
  rather than typing a solo into something else.

## Requirements

Windows or Linux, and Python 3.10 or newer (with Tk for the window). Nothing else is needed: the MIDI parser,
the arranger, the input backend and the scheduler are all in this repository.

Run Roblox unelevated, or Windows silently discards input from an unelevated
sender.

Linux needs a little setup, see [Linux](#linux).

The layout assumes a US keyboard, deliberately, because sheets describe physical
key positions rather than characters.

## Linux

Run `./run.sh` instead of `python main.py`; it takes the same arguments.

Keystrokes are typed through `/dev/uinput`, a virtual keyboard created at the
kernel level, so it works the same on X11 and Wayland and reaches Wine, Proton
and native Roblox clients alike. Your user has to be able to write to it:

- Most distros: `sudo modprobe uinput`, then a udev rule such as
  `KERNEL=="uinput", GROUP="input", MODE="0660"` and add yourself to `input`.

Two things are best-effort and degrade rather than fail:

- **Hotkeys** read `/dev/input/event*`, which needs the `input` group. Without it
  the window buttons still work and the player says the hotkeys are unavailable.
  They observe rather than grab, so the game also receives F1 and friends.
- **The window guard** needs a way to ask the desktop for the focused window:
  `hyprctl` on Hyprland, `swaymsg` on Sway, `kdotool` on KDE Plasma, or `xdotool`
  on any X11 session. Without the one for your desktop the guard is off and keys
  go to whatever has focus, so start playback with the right window in front.
  With it, a failed check pauses rather than plays. Set the window box to match
  your client's title (Sober's may not say "Roblox").

## Running it

```bash
python main.py
```

The playlist picks up anything in the `songs` folder. The right-hand column
exposes the arranger (transposition, polyphony, chord window, timing floors,
humanisation) and re-arranges as you change it, with a summary line reporting
how much of the song survived. The strip along the bottom is a live 61-key
keyboard showing what is held and which character each key is.

Hotkeys work while Roblox has focus:

| Key | Does |
| --- | --- |
| F1 | Play and pause |
| F2 | Stop |
| F3 | Restart |
| F4 | Release every held key |
| F5 / F6 | Nudge speed by five percent |

F4 is worth remembering. A stuck modifier is the failure mode that turns a
browser into a mess of keyboard shortcuts, so it releases both shifts
unconditionally alongside anything the player is holding.

The CLI covers the same ground:

```bash
python main.py song.mid --speed 1.1 --polyphony 5 --humanize 0.3
```

`--log` records what the player does (keystroke batches, focus changes, hotkeys,
state, timing) to the terminal and to `pianola.log` beside the app, which is the
thing to attach when something misbehaves. `--analyze` reports the song's range, what each transposition strategy would
achieve, and what the arranger did, without playing. `--preview` prints the
keystroke stream with timestamps, which is the fastest way to see whether a file
arranged sensibly. `--keys` prints the layout. `--no-window-check` removes the
focus guard, and `--loop` repeats.

## What the arranger does

Transposition scores every candidate shift on the fraction of the song landing
inside the playable C2 to C7 window, weighted by note duration rather than count,
so a flurry of grace notes cannot outvote the sustained melody a listener
actually hears. Whole octaves are tried first and win by default, since moving a
piece bodily keeps it in its original key. A semitone shift buys a sliver of
range in exchange for changing key, so it is only considered under `--any-key`
and only when the gain is decisive. Whatever still falls off the ends is folded
back in by octaves rather than dropped, which keeps a bass line audible.

Chords are grouped from onsets within a tolerance window and thinned to the
polyphony limit. Which notes survive is decided rather than arbitrary: the top
note stays because it carries the melody and the bottom because it carries the
root, and the rest are ranked by velocity with a bonus for contributing a pitch
class nothing else covers. A doubled octave adds far less than the third that
tells you whether the chord is major or minor.

Two constraints come from the keyboard rather than the music:

| Constraint | Handling |
| --- | --- |
| A repeated note needs a release and a press far enough apart to register as two events | The previous note is clipped short to open a gap, or the two are tied into one longer press if clipping would leave nothing audible |
| Every accidental shares a physical key with the natural below it, `T` being shift plus the same key as `t` | Collisions are detected and resolved, rather than emitting a sequence that leaves shift in the wrong state |

A chord containing both C and C sharp is unplayable on real hardware whatever the
MIDI says. That second constraint is the one most auto players get wrong.

## Letters

Alongside MIDI the player reads and writes the letter notation used by
virtualpiano.net, which is also what nearly every Roblox piano sheet uses,
because nearly every Roblox piano sheet was copied from there. The semantics are
per-character rather than per-word, which is the part that is easy to get wrong.

| Token | Advances the clock by |
| --- | --- |
| A note | One slot |
| Space, or a single line break | One more |
| Pipe | Two |
| Blank line | An extended pause |
| `[...]` | Nothing, played together |
| `[ ... ]` with spaces | Rolled quickly, the site's "as fast as possible" |

Capitals are the black keys. The site's documented ordering of `as|df` shorter
than `as| df` shorter than `as | df` shorter than `as| |df` falls out of that
arithmetic instead of needing to be special-cased.

The model was checked against the
[Overworld Theme sheet](https://virtualpiano.net/music-sheet/overworld-theme-super-mario-bros/)
rather than assumed. It places the high G on slot 8, the low G on slot 12 and the
third bar on slot 16, which is the tune's actual rhythm, and the full sheet comes
out at very nearly the 1:58 the site lists. Pasting it gives 128 notes, no
unrecognised characters, and nothing dropped in arrangement.

Whitespace at both ends of every line is stripped first. Under per-character
timing the invisible indentation that comes with copying out of a web page or a
chat message would otherwise stretch every rest in the piece.

Tempo is two numbers: beats per minute, and how many slots fill a beat. Two slots
per beat, making a slot an eighth note, is what sheets on the site assume and is
the default. `songs/demo.txt` demonstrates every symbol.

Sheets arrive by clipboard from a forum post rather than as a tidy file, so the
**Letters** button opens an editor that treats pasted text as a first-class
source. It reports how many notes it found, roughly how long the result runs, and
which characters it ignored. That last part matters, because stray punctuation,
smart quotes and stretches of lyrics are common in pasted sheets, and a silent
skip makes a mangled paste look like a working one. It does not read the
clipboard until you press Paste, since every letter of the alphabet is a valid
piano key and any old clipboard text would otherwise parse as music.

The same window converts the other way. **From loaded song** writes whatever MIDI
you have loaded out as letters, respecting the transposition and polyphony limit
you have set. Onsets are quantised onto the slot grid and gaps are spelled with
the same pipes and spaces, so parsing the output back at the same tempo
reproduces the timing.

```bash
python main.py song.mid --to-sheet > song.txt
```

## Timing

The default Windows scheduler granularity is around 15 ms, enough to smear a
chord audibly. The player raises the timer resolution to 1 ms for the duration of
playback, and the scheduling loop sleeps until a note is nearly due before
spinning out the last two and a half milliseconds.

The clock is virtual rather than wall-clock, so speed changes, pausing, seeking
and the focus guard all work mid-song without rebuilding anything. Keystrokes
destined for the same instant are fused into one `SendInput` call, with naturals
pressed before accidentals so shift toggles at most once per chord.

## Notes

Keys are delivered as scancodes through `SendInput`, the same path a physical
keyboard takes. Nothing is injected into or read from the game. This is a program
that types, and it can only do what your fingers could do more slowly.

Sticky Keys (Windows) will not trigger from the shift traffic, since that shortcut needs
five consecutive shift presses with nothing in between, and there is always a
note key between ours.

Playing virtualpiano.net needs nothing special: the site uses the identical
layout and listens to the ordinary browser keyboard stream. Switch the window box
to the **Virtual Piano** preset, then click the on-screen piano once so the page
has focus. The box is free text, so any other title fragment works for any other
target.

Settings persist to `settings.json` next to the application when the window
closes.

## Layout

```
main.py           entry point
gui.py            the window, the arranger controls, the live keyboard
cli.py            the command line
piano/
  midi.py         Standard MIDI parsing
  sheet.py        Virtual Piano letter notation, both directions
  arrange.py      transposition, chord thinning, keyboard constraints
  keymap.py       the 61-key layout
  keyboard.py     key bookkeeping and shift handling
  _win.py         SendInput, RegisterHotKey, window title, timer (Windows)
  _linux.py       uinput, evdev hotkeys, window title (Linux)
  player.py       the scheduler and the virtual clock
  hotkeys.py      the transport keys
  config.py       settings.json
```

## Licence

MIT, see [LICENSE](LICENSE).
