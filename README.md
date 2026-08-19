# Pianola

Pianola is an auto player for the 61-key pianos found across Roblox, including Got Talent, Piano Sheets and the various free-play piano games, and for virtualpiano.net itself. It reads Standard MIDI files or plain-text Virtual Piano sheets and performs them on the keyboard. The name comes from the self-playing pianos of the 1900s, which read a perforated paper roll and worked the keys themselves. It is pure Python with no third-party packages. The MIDI parser, the arranger, the input backend and the scheduler are all in this repository, so `python main.py` is the entire installation procedure on any machine with Python 3.10 or newer.

Sending the keystrokes is the easy half, about a dozen lines of `ctypes`. The hard half is that a MIDI file is almost never playable as written. A piano roll routinely spans seven octaves where the game offers five, stacks ten simultaneous voices where the game wants a handful, and retriggers notes faster than a keyboard event can round-trip through Windows. Most auto players ignore all of that, blast the raw note stream at the game, and produce something that is recognisably the wrong song. This one runs an arranging pass first.

## What the arranger does

Transposition is chosen by scoring every candidate shift on the fraction of the song that lands inside the playable C2 to C7 window, weighted by note duration rather than note count so that a flurry of grace notes cannot outvote the sustained melody a listener actually hears. Whole octaves are tried first and win by default, since moving a piece bodily up or down keeps it in its original key and still sounds like the song. A semitone shift buys at most a sliver of extra range in exchange for transposing the piece into a different key, so it is only considered when you pass `--any-key`, and only when the gain is decisive. Whatever still falls off the ends is folded back in by octaves rather than discarded, which keeps a bass line audible instead of silent.

Chords are grouped from note onsets within a tolerance window and then thinned to fit the polyphony limit. Which notes survive is decided rather than arbitrary. The top note stays because it carries the melody and the bottom note stays because it carries the harmony's root, while the notes competing for the remaining slots are ranked by velocity with a bonus for contributing a pitch class nothing else in the chord already covers. A doubled octave adds far less than the third that tells you whether the chord is major or minor.

Two constraints come from the keyboard rather than the music. Repeated notes need a release and a press separated by enough time for the game to register two distinct events, so a repeat that arrives too soon has the previous note clipped short to open a gap, and the two are tied into one longer press only if clipping would leave nothing audible. The second constraint is subtler, and is the one most auto players get wrong. On this layout every accidental shares a physical key with the natural a semitone below it, `T` being shift plus the same key as `t`. A chord containing both C and C sharp is therefore unplayable on real hardware no matter what the MIDI says. The arranger detects those collisions and resolves them instead of emitting a keystroke sequence that would leave shift in the wrong state and sound a wrong note.

Humanisation is on by default at a modest setting. Chords roll from the bottom up over a few milliseconds and onsets scatter by a few more, which is roughly what a pianist's hands do. A perfectly simultaneous ten-finger chord is something only a sequencer produces. Set it to zero from the CLI or the slider if you want it metronomic.

## Running it

`python main.py` opens the window. The playlist picks up anything in the `songs` folder automatically, and you can add MIDI files, sheets or whole folders from there. The right-hand column exposes the arranger, covering transposition, polyphony, chord window, timing floors and humanisation, and re-arranges as you change it, with the summary line at the top reporting how much of the song survived. The strip along the bottom is a live 61-key keyboard showing what is being held and which character each key corresponds to.

The transport is driven by hotkeys that work while Roblox has focus. F1 plays and pauses, F2 stops, F3 restarts, F4 releases every held key, and F5 and F6 nudge the speed in five percent steps. F4 is worth remembering. A stuck modifier is the failure mode that turns a browser into a mess of keyboard shortcuts, so it releases both shifts unconditionally alongside anything the player is holding.

Playback only types into a window whose title contains "Roblox" by default. Alt-tab away mid-song and the player releases its keys, freezes the clock and waits, rather than typing a piano solo into whatever you switched to. Clear the field to disable the guard.

That same mechanism is what lets this drive virtualpiano.net. The site uses the identical 61-key layout, and it listens to the ordinary browser keyboard stream, so the player needs nothing special to play it. Switch the window box to the "Virtual Piano" preset, which matches the browser tab title on any of its sheet pages, then click the on-screen piano once so the page has focus before starting. The box is a free text field, so any other title fragment works for any other target.

The CLI covers the same ground for scripting:

```bash
python main.py song.mid --speed 1.1 --polyphony 5 --humanize 0.3
```

`--analyze` reports the song's range, what each transposition strategy would achieve, and what the arranger did, without playing anything. `--preview` prints the keystroke stream with timestamps, which is the fastest way to see whether a file arranged sensibly. `--keys` prints the layout. `--no-window-check` removes the focus guard and `--loop` repeats.

## Letters

Alongside MIDI the player reads and writes the letter notation used by [virtualpiano.net](https://virtualpiano.net/), which is also what nearly every Roblox piano sheet uses, because nearly every Roblox piano sheet was copied from there. The notation follows the semantics the site publishes under [How To Play](https://virtualpiano.net/how-to-play/). Those semantics are per-character rather than per-word, which is the part that is easy to get wrong.

Every note token advances the clock by one slot. A space advances it by one more, and a pipe by two. So `asdf` is a fast run and `a s d f` is the same four notes with a pause between each, while the site's documented ordering of `as|df` shorter than `as| df` shorter than `as | df` shorter than `as| |df` falls straight out of the arithmetic instead of needing to be special-cased. A single line break is worth one slot, the same as a space, and a blank line is an extended pause. Square brackets play together. A bracket written with spaces inside is the site's "as fast as possible" sequence, and gets rolled quickly instead of struck as one chord. Capitals are the black keys.

That model was checked against the [Overworld Theme sheet](https://virtualpiano.net/music-sheet/overworld-theme-super-mario-bros/) rather than assumed. It places the high G on slot 8, the low G on slot 12 and the third bar on slot 16, which is the tune's actual rhythm, and the full sheet comes out at very nearly the 1:58 the site lists for it. Pasting that sheet in gives 128 notes, no unrecognised characters, and nothing dropped in arrangement.

Whitespace at both ends of every line is stripped before any of this is measured. Under per-character timing the invisible indentation that comes with copying out of a web page or a chat message would otherwise stretch every rest in the piece.

Tempo is set by two numbers, the beats per minute and how many note slots fill a beat. Two slots per beat, making a slot an eighth note, is what sheets on the site assume and is the default. The `--units` flag and the sheet notes/beat box change it. `songs/demo.txt` demonstrates every symbol.

Sheets arrive by clipboard from a forum post rather than as a tidy file on disk, so the **Letters** button opens an editor that treats pasted text as a first-class source. Paste, and it reports how many notes it found, roughly how long the result runs, and which characters it had to ignore. That last part matters, because stray punctuation, smart quotes and stretches of lyrics are common in pasted sheets, and a silent skip makes a mangled paste look like a working one. From there the text can be played directly, saved into the songs folder, or copied back out. The editor does not read your clipboard until you press Paste, since every letter of the alphabet happens to be a valid piano key and any old clipboard text would otherwise parse as music and appear on screen unasked.

The same window converts in the other direction. **From loaded song** writes whatever MIDI you have loaded out as letters, respecting the transposition, polyphony limit and everything else you have set, which is a quick way to turn a MIDI into a sheet you can read, share, or hand to someone playing by hand. Onsets are quantised onto the slot grid and gaps are spelled with the same pipes and spaces the notation uses, so parsing the output back at the same tempo reproduces the timing. The CLI equivalent is `--to-sheet`, with `--out-bpm` if you want a tempo other than the song's own:

```bash
python main.py song.mid --to-sheet > song.txt
```

## Timing

The default Windows scheduler granularity is around 15 ms, enough to smear a chord audibly. The player raises the timer resolution to 1 ms for the duration of playback, and the scheduling loop sleeps until a note is nearly due before spinning out the last two and a half milliseconds, which lands events within about a millisecond of where they belong. The clock is virtual rather than wall-clock, so speed changes, pausing, seeking and the focus guard all work mid-song without rebuilding anything. Keystrokes destined for the same instant are fused into a single `SendInput` call, with naturals pressed before accidentals so that shift toggles at most once per chord however mixed it is.

## Notes

Keys are delivered as scancodes through `SendInput`, the same path a physical keyboard takes. Nothing is injected into or read from the game. This is a program that types, and it can only do what your fingers could do more slowly. Run Roblox unelevated, or Windows will silently discard input from an unelevated sender.

The layout assumes a US keyboard, deliberately, because sheets describe physical key positions rather than characters. Sticky Keys will not trigger from the shift traffic, since that shortcut requires five consecutive shift presses with nothing in between and there is always a note key between ours.

Settings persist to `settings.json` next to the application when the window closes.
