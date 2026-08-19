"""Turn a MIDI song into something a 61-key Roblox piano can actually play.

A raw MIDI is almost never playable as-is: it spans more than five octaves, it
stacks ten voices where the game accepts a handful, and it retriggers notes
faster than a keyboard event round-trip. This module is the arranger that sits
between the file and the keyboard - it picks a transposition, folds strays back
onto the board, thins chords down to the notes that carry the music, and spaces
repeats far enough apart that the game sees two presses instead of one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import keymap
from .midi import MidiSong, Note

DRUM_CHANNEL = 9


@dataclass
class ArrangeConfig:
    # pitch
    transpose: int | None = None            # None -> search for the best shift
    search_range: int = 24
    octave_only: bool = True                # keep the original key when shifting
    fold_octaves: bool = True               # else out-of-range notes are dropped

    # texture
    max_polyphony: int = 6
    drop_drums: bool = True
    velocity_floor: int = 1
    chord_window_ms: float = 32.0

    # mechanics
    min_note_ms: float = 45.0
    min_gap_ms: float = 32.0
    max_hold_ms: float = 4000.0
    legato: float = 0.95
    sustain_pedal: bool = False

    # performance (playback rate lives on the Player, not here)
    humanize: float = 0.4                   # 0 = metronomic, 1 = sloppy human
    roll_ms: float = 11.0                   # chord spread at humanize == 1
    jitter_ms: float = 9.0                  # onset scatter at humanize == 1
    batch_ms: float = 2.0                   # events closer than this are fused

    tracks: set[int] | None = None          # None -> every track


@dataclass
class Instant:
    t: float
    downs: list[str] = field(default_factory=list)
    ups: list[str] = field(default_factory=list)


@dataclass
class Arrangement:
    instants: list[Instant] = field(default_factory=list)
    duration: float = 0.0
    transpose: int = 0
    total_notes: int = 0
    played_notes: int = 0
    folded_notes: int = 0
    dropped_notes: int = 0
    thinned_notes: int = 0
    coverage: float = 1.0
    title: str = ""

    def summary(self) -> str:
        m, s = divmod(int(self.duration), 60)
        parts = [f"{self.played_notes}/{self.total_notes} notes",
                 f"{m}:{s:02d}",
                 f"transpose {self.transpose:+d}",
                 f"in-range {self.coverage * 100:.0f}%"]
        if self.folded_notes:
            parts.append(f"{self.folded_notes} folded")
        if self.thinned_notes:
            parts.append(f"{self.thinned_notes} thinned")
        if self.dropped_notes:
            parts.append(f"{self.dropped_notes} dropped")
        return " | ".join(parts)


# --------------------------------------------------------------------------
# transposition
# --------------------------------------------------------------------------

def score_transpose(notes: list[Note], shift: int) -> float:
    """Duration-weighted share of the song that lands on the keyboard.

    Weighting by duration rather than note count stops a flurry of grace notes
    from outvoting the sustained melody that the listener actually hears.
    """
    hit = 0.0
    total = 0.0
    for n in notes:
        w = min(n.duration, 2.0) + 0.05
        total += w
        if keymap.in_range(n.note + shift):
            hit += w
    return hit / total if total else 0.0


def best_transpose(notes: list[Note], search: int = 24,
                   octave_only: bool = True) -> tuple[int, float]:
    """Pick the shift that fits the most music onto the board.

    Whole octaves are tried first and win by default: they move a piece bodily
    up or down without changing its key, so it still sounds like the song.
    A semitone shift buys at most a sliver of extra range while transposing the
    piece into a different key, so it is only considered when the caller asks
    and only if it is worth a clear margin.
    """
    def sweep(candidates):
        best = (0, -1.0)
        for shift in candidates:
            cov = score_transpose(notes, shift)
            if cov > best[1] + 1e-9 or (abs(cov - best[1]) <= 1e-9 and abs(shift) < abs(best[0])):
                best = (shift, cov)
        return best

    octaves = [s for s in range(-search, search + 1) if s % 12 == 0]
    shift, coverage = sweep(octaves)
    if octave_only or coverage > 0.995:
        return shift, coverage

    other, other_cov = sweep(range(-search, search + 1))
    if other_cov > coverage + 0.05:          # only for a decisive gain
        return other, other_cov
    return shift, coverage


# --------------------------------------------------------------------------
# texture reduction
# --------------------------------------------------------------------------

def _thin_chord(chord: list[Note], limit: int) -> list[Note]:
    """Keep the notes a listener would miss: melody on top, bass at the bottom.

    Everything in between competes on velocity, and duplicate pitch classes lose
    to fresh ones - a doubled octave adds far less than the third of the chord.
    """
    if len(chord) <= limit:
        return chord

    ordered = sorted(chord, key=lambda n: n.note)
    keep = [ordered[-1]]                                # melody
    if limit > 1:
        keep.append(ordered[0])                         # bass
    middle = ordered[1:-1]

    seen = {n.note % 12 for n in keep}
    scored = []
    for n in middle:
        bonus = 0.0 if n.note % 12 in seen else 30.0
        scored.append((n.velocity + bonus, n.note, n))
    scored.sort(key=lambda item: (-item[0], -item[1]))

    for _, _, n in scored:
        if len(keep) >= limit:
            break
        keep.append(n)
    return keep


def _group_chords(notes: list[Note], window: float) -> list[list[Note]]:
    groups: list[list[Note]] = []
    current: list[Note] = []
    anchor = 0.0
    for n in notes:
        if current and n.start - anchor > window:
            groups.append(current)
            current = []
        if not current:
            anchor = n.start
        current.append(n)
    if current:
        groups.append(current)
    return groups


def _apply_sustain(notes: list[Note], song: MidiSong, cap: float) -> None:
    """Stretch notes to the next pedal lift, the way a real sustain works."""
    for channel, pedal in song.sustain.items():
        downs = sorted(pedal)
        if not downs:
            continue
        lifts = [t for t, down in downs if not down]
        for n in notes:
            if n.channel != channel:
                continue
            later = [t for t in lifts if t > n.end]
            if later:
                n.end = min(later[0], n.start + cap)


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def arrange(song: MidiSong, cfg: ArrangeConfig, seed: int | None = None) -> Arrangement:
    rng = random.Random(seed)
    result = Arrangement(title=song.title)

    source = [n for n in song.notes
              if n.velocity >= cfg.velocity_floor
              and not (cfg.drop_drums and n.channel == DRUM_CHANNEL)
              and (cfg.tracks is None or n.track in cfg.tracks)]
    result.total_notes = len(source)
    if not source:
        return result

    notes = [Note(n.start, n.end, n.note, n.velocity, n.channel, n.track) for n in source]

    if cfg.sustain_pedal:
        _apply_sustain(notes, song, cfg.max_hold_ms / 1000.0)

    if cfg.transpose is None:
        shift, coverage = best_transpose(notes, cfg.search_range, cfg.octave_only)
    else:
        shift, coverage = cfg.transpose, score_transpose(notes, cfg.transpose)
    result.transpose = shift
    result.coverage = coverage

    playable: list[Note] = []
    for n in notes:
        pitch = n.note + shift
        if not keymap.in_range(pitch):
            if not cfg.fold_octaves:
                result.dropped_notes += 1
                continue
            pitch = keymap.fold_into_range(pitch)
            result.folded_notes += 1
        n.note = pitch
        playable.append(n)

    playable.sort(key=lambda n: (n.start, n.note))

    # ---- chord grouping, thinning, humanising ----------------------------
    window = cfg.chord_window_ms / 1000.0
    roll = cfg.roll_ms / 1000.0 * cfg.humanize
    jitter = cfg.jitter_ms / 1000.0 * cfg.humanize
    min_note = cfg.min_note_ms / 1000.0
    max_hold = cfg.max_hold_ms / 1000.0

    spans: dict[str, list[list]] = {}                    # physical key -> [start, end, char]
    for chord in _group_chords(playable, window):
        # One physical key can only sound one note.  That collapses exact
        # duplicates and also the semitone clusters this layout cannot voice,
        # since every accidental shares a key with the natural below it.
        by_key: dict[str, Note] = {}
        for n in chord:
            key = keymap.key_for(n.note)
            if key is None:
                continue
            phys = keymap.physical(key)
            prev = by_key.get(phys)
            if prev is None or (n.velocity, n.note) > (prev.velocity, prev.note):
                by_key[phys] = n
        unique = list(by_key.values())
        result.thinned_notes += len(chord) - len(unique)

        kept = _thin_chord(unique, cfg.max_polyphony)
        result.thinned_notes += len(unique) - len(kept)

        offset = rng.gauss(0.0, jitter) if jitter > 0 else 0.0
        kept.sort(key=lambda n: n.note)
        for i, n in enumerate(kept):
            spread = i * roll * rng.uniform(0.6, 1.0) if roll > 0 else 0.0
            start = max(0.0, n.start + offset + spread)
            length = max(n.duration * cfg.legato, min_note)
            if jitter > 0:
                length *= rng.uniform(1.0 - 0.12 * cfg.humanize, 1.0 + 0.12 * cfg.humanize)
            length = min(max(length, min_note), max_hold)
            char = keymap.key_for(n.note)
            spans.setdefault(keymap.physical(char), []).append([start, start + length, char])

    # ---- per-key repeat spacing -----------------------------------------
    min_gap = cfg.min_gap_ms / 1000.0
    events: list[tuple[float, str, bool]] = []
    for raw in spans.values():
        raw.sort(key=lambda s: s[0])
        merged: list[list] = []
        for span in raw:
            if not merged:
                merged.append(span)
                continue
            prev = merged[-1]
            if span[0] - prev[1] >= min_gap:
                merged.append(span)
                continue
            # Too close to retrigger cleanly: clip the previous note short if
            # that still leaves an audible press.  Failing that, an exact repeat
            # can be tied into one long press, but a neighbour fighting for the
            # same physical key has to give way.
            clipped = span[0] - min_gap
            if clipped - prev[0] >= min_note * 0.5:
                prev[1] = clipped
                merged.append(span)
            elif span[2] == prev[2]:
                prev[1] = max(prev[1], span[1])
            else:
                result.dropped_notes += 1
        for start, end, char in merged:
            events.append((start, char, True))
            events.append((max(end, start + min_note), char, False))
            result.played_notes += 1

    # ---- fuse near-simultaneous events into single SendInput bursts ------
    events.sort(key=lambda e: (e[0], e[2]))     # releases before presses
    batch = cfg.batch_ms / 1000.0
    instants: list[Instant] = []
    for t, key, down in events:
        if not instants or t - instants[-1].t > batch:
            instants.append(Instant(t))
        current = instants[-1]
        (current.downs if down else current.ups).append(key)

    result.instants = instants
    result.duration = max((i.t for i in instants), default=0.0)
    return result
