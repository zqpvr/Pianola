# Pianola

Plays MIDI files and Virtual Piano sheets on the 61-key pianos in Roblox, and on
[virtualpiano.net](https://virtualpiano.net/).

A MIDI file is almost never playable as written: it spans more octaves than the
game has, stacks more voices than it wants, and repeats notes faster than a key
can register. Pianola arranges the song to fit the keyboard first, then plays it.

Pure Python, no third-party packages.

## Features

- Picks the transposition that keeps the most of the song in range.
- Thins chords to what the keyboard can hold, keeping the melody and the root.
- Reads and writes Virtual Piano letter sheets, including pasted ones.
- Humanised by default: chords roll slightly and onsets scatter a few ms.
- Keystrokes land within about a millisecond of where they belong.
- Only types into the game window, so alt-tabbing away pauses it.

## Windows

Download `Pianola-windows-x64.exe` from
[Releases](https://github.com/zqpvr/Pianola/releases) and run it. Nothing to
install.

- Run Roblox normally, not as administrator, or Windows drops the keystrokes.
- If SmartScreen warns about an unknown publisher, choose **More info > Run anyway**.

From source: Python 3.10 or newer, then `python main.py`.

## Linux

Download `pianola-linux-x86_64` from
[Releases](https://github.com/zqpvr/Pianola/releases), then
`chmod +x pianola-linux-x86_64` and run it. It works on any x86-64 distribution
with glibc 2.35 or newer, on X11 and Wayland. The Roblox client on Linux is
[Sober](https://sober.vinegarhq.org/), so the window box defaults to **Sober**.

One-time setup: Pianola types through `/dev/uinput`, a virtual keyboard, which
your user needs write access to. On most distributions:

```bash
echo uinput | sudo tee /etc/modules-load.d/uinput.conf && sudo modprobe uinput
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo usermod -aG input $USER     # then log out and back in
```

From source: Python 3.10 or newer with Tk (`python3-tk` on Debian and Ubuntu,
`python3-tkinter` on Fedora), then `./run.sh`.

**Hotkeys** work while Sober has focus on KDE Plasma, GNOME 48+, Hyprland and any
X11 desktop; KDE and GNOME ask once to approve them. Elsewhere, such as Sway, bind
keys in your compositor's config to `pianola --control ACTION`:

```
bindsym F1 exec /path/to/pianola-linux-x86_64 --control play_pause
```

The actions are `play_pause`, `stop`, `restart`, `panic`, `speed_down` and
`speed_up`. Being in the `input` group also makes hotkeys work everywhere.

**The window guard** (pause when Sober loses focus) works on KDE Plasma,
Hyprland, Sway, niri and any X11 desktop. GNOME on Wayland gives no way to ask
which window has focus, so there it is off: keep Sober in front while it plays.

## Using it

Add songs (or drop them in a `songs` folder next to the app), pick one, click into
the game, and press F1. The panel on the right tunes the arrangement and shows how
much of the song survived it.

| Key | Does |
| --- | --- |
| F1 | Play and pause |
| F2 | Stop |
| F3 | Restart |
| F4 | Release every held key |
| F5 / F6 | Speed down / up by 5% |

For virtualpiano.net, set the window box to **Virtual Piano** and click the
on-screen piano once.

The same works from a terminal, where `pianola` is the downloaded file (or
`python main.py` from source):

```bash
pianola song.mid --speed 1.1 --polyphony 5
pianola song.mid --analyze       # what the arranger would do, without playing
pianola song.mid --preview       # the keystrokes, with timestamps
pianola song.mid --to-sheet      # the song as Virtual Piano letters
pianola --log                    # write pianola.log, for bug reports
```

## Letter sheets

The **Letters** button opens an editor for sheets pasted from virtualpiano.net or
a forum post. It reports how many notes it found and which characters it ignored,
and can also write the loaded MIDI out as letters. Timing is per character: a
note or space is one slot, `|` is two, a blank line is a longer pause, `[...]` is
a chord, and `[ ... ]` with spaces is a quick roll. Capitals are the black keys.
`songs/demo.txt` shows every symbol.

## Notes

- Keys are sent the way a physical keyboard sends them. Nothing reads or modifies
  the game.
- Sheets use US keyboard positions, so the layout assumes a US keyboard.
- Settings, the log and saved sheets live next to the app, or in
  `%APPDATA%\Pianola` or `~/.config/pianola` if that folder isn't writable.

## Licence

MIT, see [LICENSE](LICENSE).
