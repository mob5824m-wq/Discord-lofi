"""A lofi generator, so the bot always has something to play.

Every other station in Lofi depends on somebody else: a YouTube broadcast that
can be taken down, an icecast host that can move, a ``./music`` folder that
starts out empty. This module is the one source the bot controls end to end -
it renders short lofi tracks on the machine it runs on, with no network, no
downloads and nothing to license. That makes a fresh install playable in the
first ten seconds, and it is what the dashboard's demo mode listens to.

The engine is a small subtractive/additive synth written against numpy:

* **harmony** - a jazz-ish progression (ii-V-I, I-vi-ii-V, ...) voiced as 7th
  and 9th chords in a Rhodes-like additive timbre, with a detuned second
  partial for the bell attack and a slow tremolo;
* **bass** - a soft sine with one harmonic, following the chord roots;
* **drums** - a swung eighth-note pattern: a pitch-sweeping kick, a filtered
  noise snare with a tonal body, and hats whose velocity breathes;
* **melody** - sparse pentatonic notes with vibrato, sent through a tape echo;
* **texture** - vinyl crackle (sparse impulses) under a pink-noise floor;
* **the "lofi" part** - a low-pass/tilt curve applied in the frequency domain,
  wow and flutter as an LFO on every oscillator's instantaneous frequency,
  ``tanh`` tape saturation, and a Haas-widened stereo image.

Filters are FFT-domain multiplications and wow/flutter is a ``cumsum`` of a
modulated frequency, so the whole thing stays vectorised: a 50-second stereo
track renders in well under a second. Recursive IIR filters would need a
per-sample Python loop, which is the one thing that would make this slow.

Rendered tracks are cached in the data directory under a name derived from
their parameters, so a station that loops for hours renders a handful of files
rather than one per track.

numpy is imported lazily and is optional: when it is missing,
:func:`available` returns ``False`` and :func:`render_track` raises
:class:`GenerativeUnavailable` with the install command, which the player turns
into a readable message instead of a traceback.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import paths


logger = logging.getLogger("lofi.generative")

try:  # pragma: no cover - depends on the environment
    import numpy as np

    NUMPY_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # pragma: no cover - depends on the environment
    np = None  # type: ignore[assignment]
    NUMPY_IMPORT_ERROR = (
        "Studio Lofi needs numpy to render audio. Install it with "
        "'pip install numpy' (or 'pip install -r requirements.txt'), or pick a "
        "different station."
    )

SAMPLE_RATE = 44100
#: Seconds of audio per rendered track. Long enough to feel like a tune, short
#: enough that the first play of a station is almost instant.
TRACK_SECONDS = 52.0
BARS_PER_TRACK = 16
#: Peak level the master bus is normalised to, in linear amplitude (-1.5 dBFS).
#: Headroom matters because Discord re-encodes to Opus at a fixed bitrate.
TARGET_PEAK = 0.84


class GenerativeUnavailable(RuntimeError):
    """Raised when the generative station cannot run (numpy missing)."""


def available() -> bool:
    """True when a track could be rendered right now."""
    return np is not None


def _require_numpy() -> None:
    if np is None:  # pragma: no cover - depends on the environment
        raise GenerativeUnavailable(NUMPY_IMPORT_ERROR or "numpy is required.")


# --------------------------------------------------------------------------- #
# Music theory, kept deliberately small
# --------------------------------------------------------------------------- #
CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "maj7": (0, 4, 7, 11),
    "maj9": (0, 4, 7, 11, 14),
    "min7": (0, 3, 7, 10),
    "min9": (0, 3, 7, 10, 14),
    "min11": (0, 3, 7, 10, 14, 17),
    "dom7": (0, 4, 7, 10),
    "dom9": (0, 4, 7, 10, 14),
    "dom7sus4": (0, 5, 7, 10),
    "dim7": (0, 3, 6, 9),
    "min7b5": (0, 3, 6, 10),
}

CHORD_NAMES: dict[str, str] = {
    "maj7": "maj7",
    "maj9": "maj9",
    "min7": "m7",
    "min9": "m9",
    "min11": "m11",
    "dom7": "7",
    "dom9": "9",
    "dom7sus4": "7sus4",
    "dim7": "dim7",
    "min7b5": "m7♭5",
}

NOTE_NAMES = ("C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B")


@dataclass(frozen=True)
class Recipe:
    """A mood: tempo, key, progression and the mix settings that go with it."""

    id: str
    name: str
    bpm: float
    key_root: int
    progression: tuple[tuple[int, str], ...]
    swing: float = 0.60
    melody_density: float = 0.55
    cutoff_hz: float = 6200.0
    saturation: float = 1.35
    crackle: float = 0.55
    echo_mix: float = 0.26
    drums: float = 0.9
    description: str = ""

    @property
    def seconds_per_beat(self) -> float:
        return 60.0 / self.bpm

    def chord_name(self, degree: int, quality: str) -> str:
        """Human-readable chord, e.g. ``F♯m7`` - shown in the dashboard."""
        root = (self.key_root + degree) % 12
        return f"{NOTE_NAMES[root]}{CHORD_NAMES.get(quality, quality)}"

    def progression_text(self) -> str:
        return " – ".join(self.chord_name(degree, quality) for degree, quality in self.progression)


RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="midnight",
        name="Midnight Notebook",
        bpm=72.0,
        key_root=9,  # A minor
        progression=((0, "min9"), (8, "maj7"), (3, "maj9"), (10, "dom7")),
        swing=0.62,
        melody_density=0.42,
        cutoff_hz=5400.0,
        crackle=0.7,
        description="slow minor-key loops for 3am work",
    ),
    Recipe(
        id="rainy",
        name="Rain On The Fire Escape",
        bpm=80.0,
        key_root=2,  # D minor
        progression=((0, "min11"), (5, "min9"), (8, "maj7"), (7, "dom7sus4")),
        swing=0.58,
        melody_density=0.35,
        cutoff_hz=4800.0,
        crackle=0.85,
        echo_mix=0.3,
        description="muffled, wet and mostly drums",
    ),
    Recipe(
        id="cafe",
        name="Cafe Window Seat",
        bpm=88.0,
        key_root=5,  # F major
        progression=((2, "min9"), (7, "dom9"), (0, "maj9"), (9, "min7")),
        swing=0.64,
        melody_density=0.68,
        cutoff_hz=7000.0,
        crackle=0.35,
        description="the classic ii-V-I, bright and busy",
    ),
    Recipe(
        id="dusty",
        name="Dusty Cassette",
        bpm=76.0,
        key_root=7,  # G major
        progression=((0, "maj7"), (9, "min7"), (2, "min7"), (7, "dom7")),
        swing=0.60,
        melody_density=0.5,
        cutoff_hz=4200.0,
        saturation=1.7,
        crackle=0.95,
        echo_mix=0.32,
        description="worn tape: heavy wow, dark filter",
    ),
    Recipe(
        id="nocturne",
        name="Neon Nocturne",
        bpm=84.0,
        key_root=1,  # C♯ minor
        progression=((0, "min9"), (4, "min7b5"), (8, "maj7"), (11, "dim7")),
        swing=0.56,
        melody_density=0.48,
        cutoff_hz=5800.0,
        crackle=0.5,
        description="late-city chords with a dim7 turn",
    ),
)

RECIPE_BY_ID: dict[str, Recipe] = {recipe.id: recipe for recipe in RECIPES}
DEFAULT_RECIPE_ID = "midnight"

#: Track titles are generated from these so the now-playing card says something
#: nicer than "generative-3.wav". Deterministic per seed.
TITLE_HEADS = (
    "Half Asleep", "Paper Lanterns", "Static Bloom", "Two Stops Late", "Kettle Song",
    "Low Tide", "Neon Puddle", "Slow Commute", "Attic Window", "Cassette Sun",
    "Rain Check", "Muted Horns", "Faded Polaroid", "Night Bus", "Warm Static",
    "Loose Pages", "Distant Sirens", "Old Elevator", "Tea For One", "Worn Grooves",
)
TITLE_TAILS = (
    "at 3am", "in the rain", "(reprise)", "for a Tuesday", "under fluorescent light",
    "on the last train", "with the window open", "in an empty cafe", "and a broken fan",
    "for late studying", "in mono", "on loop", "(interlude)", "at golden hour",
    "with vinyl dust", "for nobody in particular", "before sunrise", "in the attic",
)


def recipe_for(reference: Any) -> Recipe:
    """Resolve a recipe by id, name or station URL; unknown values fall back.

    A station keeps its mood in its URL as ``generative:rainy`` (that is how one
    registry entry can have several moods), so that form is accepted here too -
    otherwise ``recipe_for(station.url)`` silently returns the default mood.
    """
    key = str(reference or "").strip().lower()
    for prefix in ("generative:", "studio:", "studio-", "mood:"):
        if key.startswith(prefix):
            key = key[len(prefix):].strip()
            break
    if key in RECIPE_BY_ID:
        return RECIPE_BY_ID[key]
    for recipe in RECIPES:
        if recipe.name.lower() == key or recipe.id == key.replace(" ", "-"):
            return recipe
    return RECIPE_BY_ID[DEFAULT_RECIPE_ID]


def track_title(recipe: Recipe, seed: int) -> str:
    """A deterministic, printable title for one rendered track."""
    rng = random.Random(f"{recipe.id}:{seed}")
    return f"{rng.choice(TITLE_HEADS)} {rng.choice(TITLE_TAILS)}"


def recipe_names() -> list[dict[str, str]]:
    """Recipe metadata for the dashboard's picker."""
    return [
        {
            "id": recipe.id,
            "name": recipe.name,
            "description": recipe.description,
            "bpm": f"{recipe.bpm:.0f}",
            "progression": recipe.progression_text(),
        }
        for recipe in RECIPES
    ]


# --------------------------------------------------------------------------- #
# DSP primitives
# --------------------------------------------------------------------------- #
def _midi_to_freq(midi: float) -> float:
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


def _oscillator(
    count: int,
    frequency: float,
    *,
    wow_depth: float = 0.0,
    wow_rate: float = 5.2,
    phase: float = 0.0,
    vibrato_depth: float = 0.0,
    vibrato_rate: float = 5.0,
) -> "np.ndarray":
    """A sine whose *instantaneous* frequency wobbles.

    Building the phase as a cumulative sum of a modulated frequency (rather
    than ``sin(2*pi*f*t)``) is what produces tape wow and vibrato: the pitch
    drifts smoothly without ever discontinuing the waveform, which is what
    makes it sound like a machine and not like a glitch.
    """
    t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    rate = np.full(count, frequency, dtype=np.float64)
    if wow_depth:
        rate = rate * (1.0 + wow_depth * np.sin(2 * np.pi * wow_rate * t + phase))
    if vibrato_depth:
        rate = rate * (1.0 + vibrato_depth * np.sin(2 * np.pi * vibrato_rate * t))
    return np.sin(2 * np.pi * np.cumsum(rate) / SAMPLE_RATE + phase)


def _envelope(
    count: int, *, attack: float, decay: float, sustain: float = 0.0, release: float = 0.0
) -> "np.ndarray":
    """Percussive envelope: fast attack, exponential decay, optional release.

    ``sustain`` is the level the decay settles at, and ``release`` fades that
    tail out at the end of the note - which is what keeps a repeated chord from
    clicking where one instance stops and the next starts.
    """
    if count <= 0:
        return np.zeros(0)
    t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    attack_samples = max(1, int(attack * SAMPLE_RATE))
    ramp = np.ones(count, dtype=np.float64)
    ramp[:attack_samples] = np.linspace(0.0, 1.0, attack_samples) ** 1.6
    # e^-3 by the nominal decay time: audible tail, gone before the next bar.
    decay_rate = 3.0 / max(decay, 1e-3)
    body = np.exp(-decay_rate * t)
    envelope = ramp * (sustain + (1.0 - sustain) * body)
    if release > 0:
        release_samples = min(count, int(release * SAMPLE_RATE))
        envelope[-release_samples:] *= np.linspace(1.0, 0.0, release_samples) ** 1.4
    return envelope


def _noise(count: int, rng: "np.random.Generator") -> "np.ndarray":
    """White noise from the track's seeded numpy generator.

    numpy's generator (rather than ``random.uniform`` in a Python loop) because
    the vinyl floor needs two million samples and a per-sample Python call
    costs more than the rest of the render put together.
    """
    return rng.uniform(-1.0, 1.0, size=int(count))


def _filter(signal: "np.ndarray", curve) -> "np.ndarray":
    """Filter ``signal`` in the frequency domain.

    ``curve(frequencies_hz)`` returns a gain per bin. Filtering this way keeps
    everything vectorised - a recursive IIR would need a Python loop over two
    million samples per track. The input dtype is preserved and the gain curve
    is cast to match, because a float64 curve multiplied into a complex64
    spectrum silently promotes the whole transform to double width.
    """
    array = np.asarray(signal)
    if array.size == 0:
        return array
    if array.dtype != np.float32:
        array = array.astype(np.float64)
    spectrum = np.fft.rfft(array)
    frequencies = np.fft.rfftfreq(array.size, 1.0 / SAMPLE_RATE)
    gains = np.asarray(curve(frequencies), dtype=spectrum.real.dtype)
    return np.fft.irfft(spectrum * gains, n=array.size)


def _lowpass_tilt(cutoff: float):
    """A gentle low-pass with a slight high-frequency tilt.

    A brick-wall cutoff sounds like a broken speaker; this rolls off from a
    third of the cutoff upwards and leaves a little air below it, which is the
    "sampled from a cassette" character.
    """

    def curve(frequencies: "np.ndarray") -> "np.ndarray":
        gain = 1.0 / np.sqrt(1.0 + (frequencies / max(cutoff, 200.0)) ** 4.0)
        tilt = 1.0 - 0.18 * np.clip(frequencies / 12000.0, 0.0, 1.0)
        return gain * tilt

    return curve


def _bandpass(low: float, high: float):
    def curve(frequencies: "np.ndarray") -> "np.ndarray":
        return (frequencies >= low) * (frequencies <= high) + 1e-9

    return curve


def _highpass(cutoff: float):
    def curve(frequencies: "np.ndarray") -> "np.ndarray":
        return 1.0 / np.sqrt(1.0 + (max(cutoff, 20.0) / np.maximum(frequencies, 1e-6)) ** 4.0)

    return curve


def _add(buffer: "np.ndarray", start: int, signal: "np.ndarray") -> None:
    """Mix ``signal`` into ``buffer`` at ``start``, clipping to the buffer end."""
    if signal.size == 0:
        return
    begin = max(0, int(start))
    if begin >= buffer.shape[0]:
        return
    end = min(buffer.shape[0], begin + signal.shape[0])
    buffer[begin:end] += signal[: end - begin]


# --------------------------------------------------------------------------- #
# Voices
# --------------------------------------------------------------------------- #
def _voice_electric_piano(
    frequency: float,
    duration: float,
    amplitude: float,
    rng: "random.Random",
    *,
    wow: float,
) -> "np.ndarray":
    """A Rhodes-ish tone: additive partials, a bell attack and slow tremolo."""
    count = int(duration * SAMPLE_RATE)
    partials = ((1.0, 1.00), (2.01, 0.38), (3.02, 0.14), (4.05, 0.06), (5.4, 0.03))
    signal = np.zeros(count, dtype=np.float64)
    for index, (ratio, gain) in enumerate(partials):
        phase = rng.uniform(0, 2 * math.pi)
        partial = _oscillator(
            count,
            frequency * ratio,
            wow_depth=wow * (1.0 + 0.35 * index),
            wow_rate=rng.uniform(0.35, 0.85),
            phase=phase,
        )
        # Higher partials die faster, which is the " struck tine " decay.
        partial *= np.exp(-(2.2 + 1.5 * index) * np.arange(count) / SAMPLE_RATE / max(duration, 0.2))
        signal += gain * partial
    tremolo = 1.0 + 0.05 * np.sin(2 * np.pi * rng.uniform(4.1, 5.4) * np.arange(count) / SAMPLE_RATE)
    envelope = _envelope(count, attack=0.006, decay=duration * 0.55, sustain=0.32, release=0.12)
    return signal * envelope * tremolo * amplitude


def _voice_bass(
    frequency: float, duration: float, amplitude: float, rng: "random.Random", *, wow: float
) -> "np.ndarray":
    """A soft upright-ish bass: sine plus a little second harmonic."""
    count = int(duration * SAMPLE_RATE)
    signal = _oscillator(count, frequency, wow_depth=wow * 0.6, wow_rate=0.4, phase=rng.uniform(0, 6.28))
    signal += 0.22 * _oscillator(count, frequency * 2, wow_depth=wow * 0.6, phase=rng.uniform(0, 6.28))
    envelope = _envelope(count, attack=0.012, decay=duration * 0.5, sustain=0.45, release=0.09)
    return _filter(signal * envelope, _lowpass_tilt(900.0)) * amplitude


def _voice_kick(duration: float, amplitude: float) -> "np.ndarray":
    """A kick built from a pitch sweep, plus a 4 ms click for the beater."""
    count = int(duration * SAMPLE_RATE)
    t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    sweep = 148.0 * np.exp(-9.0 * t) + 46.0
    body = np.sin(2 * np.pi * np.cumsum(sweep) / SAMPLE_RATE)
    body *= np.exp(-6.5 * t)
    click = np.zeros(count, dtype=np.float64)
    click_len = min(count, int(0.004 * SAMPLE_RATE))
    click[:click_len] = np.linspace(0.5, -0.4, click_len) * np.exp(-90 * np.arange(click_len) / SAMPLE_RATE)
    return (body * 0.92 + click * 0.3) * amplitude


def _voice_snare(duration: float, amplitude: float, rng: "np.random.Generator") -> "np.ndarray":
    """Filtered noise with a tonal body - a rim shot more than a snare."""
    count = int(duration * SAMPLE_RATE)
    noise = _filter(_noise(count, rng), _bandpass(1100.0, 7200.0))
    noise *= np.exp(-26.0 * np.arange(count) / SAMPLE_RATE)
    body = _oscillator(count, 187.0, phase=rng.uniform(0, 6.28))
    body *= np.exp(-30.0 * np.arange(count) / SAMPLE_RATE)
    return (noise * 0.8 + body * 0.35) * amplitude


def _voice_hat(duration: float, amplitude: float, rng: "np.random.Generator") -> "np.ndarray":
    """A short high-passed noise burst."""
    count = max(1, int(duration * SAMPLE_RATE))
    noise = _filter(_noise(count, rng), _highpass(6200.0))
    noise *= np.exp(-46.0 * np.arange(count) / SAMPLE_RATE)
    return noise * amplitude


def _vinyl_tile(count: int, rng: "np.random.Generator", intensity: float) -> "np.ndarray":
    """One short block of hiss plus crackle, at full length scale."""
    hiss = _filter(
        _noise(count, rng),
        lambda f: _highpass(90.0)(f) / np.sqrt(1.0 + (f / 6000.0) ** 3.0),
    )
    peak = float(np.max(np.abs(hiss))) or 1.0
    hiss = hiss / peak * 0.014 * intensity
    crackle = np.zeros(count, dtype=np.float64)
    pops = max(1, int(count / SAMPLE_RATE * 26 * intensity))
    positions = rng.integers(0, max(1, count - 120), size=pops)
    lengths = rng.integers(18, 90, size=pops)
    levels = rng.uniform(0.02, 0.09, size=pops) * intensity
    for position, length, level in zip(positions, lengths, levels):
        length = int(min(length, count - position))
        if length <= 0:
            continue
        burst = _noise(length, rng) * np.exp(-140.0 * np.arange(length) / SAMPLE_RATE)
        crackle[position : position + length] += burst * level
    return hiss + crackle


def _vinyl_texture(
    count: int,
    rng: "np.random.Generator",
    intensity: float,
    tile_seconds: float = 4.0,
) -> "np.ndarray":
    """Vinyl hiss and crackle for a whole track, tiled from a short block.

    Hiss is pink-ish (white noise rolled off above 6 kHz, high-passed below
    90 Hz) and crackle is sparse impulses - a few per second at random, which
    is what the ear reads as dust rather than as a rhythm.

    Tiling is not just an optimisation, though it is a good one: filtering the
    full 45 seconds of noise costs two transforms of two million samples, and
    on a busy machine that is most of the render. Four seconds filtered once
    and repeated is inaudibly different, especially with the per-tile gain
    jitter below, which stops the repetition from being periodic enough to
    hear as a loop.
    """
    intensity = max(0.0, float(intensity))
    if intensity <= 0 or count <= 0:
        return np.zeros(count, dtype=np.float64)
    tile_length = max(1024, min(count, int(tile_seconds * SAMPLE_RATE)))
    tile = _vinyl_tile(tile_length, rng, intensity)
    tiles = int(np.ceil(count / tile_length))
    texture = np.tile(tile, tiles)[:count]
    # Per-tile gain jitter: +-18%, enough to break the loop, too small to pump.
    gains = rng.uniform(0.82, 1.18, size=tiles)
    shaped = np.repeat(gains, tile_length)[:count]
    return texture * shaped


# --------------------------------------------------------------------------- #
# Arrangement
# --------------------------------------------------------------------------- #
def _voicing(recipe: Recipe, degree: int, quality: str) -> tuple[int, list[int]]:
    """Return ``(bass_midi, chord_midis)`` for one bar's chord.

    Chord tones are stacked from the intervals and then folded into a fixed
    register, which keeps voicings close together (voice leading) instead of
    leaping octaves every bar - the reason a four-chord loop sounds like a
    pianist rather than an arpeggiator.
    """
    root = recipe.key_root + degree
    bass_midi = 36 + (root % 12)
    if bass_midi > 43:
        bass_midi -= 12
    intervals = CHORD_QUALITIES.get(quality, CHORD_QUALITIES["min7"])
    base = 58 + (root % 12)
    notes: list[int] = []
    for interval in intervals:
        note = base + interval
        while note > 76:
            note -= 12
        while note < 55:
            note += 12
        notes.append(note)
    # Drop the root from the upper voicing when the chord has five or more
    # tones: the bass already has it, and keeping it muddies the middle.
    if len(notes) >= 5:
        notes = [note for note in notes if (note - base) % 12 != 0] or notes
    return bass_midi, sorted(set(notes))


def _drum_pattern(rng: "random.Random", bars: int) -> dict[str, list[tuple[int, float]]]:
    """Swung kick/snare/hat hits as ``(sixteenth-step, velocity)`` per bar.

    Steps are counted in sixteenths so the swing can move odd eighth notes by a
    fraction of a step rather than quantising them to a grid.
    """
    kick: list[tuple[int, float]] = []
    snare: list[tuple[int, float]] = []
    hats: list[tuple[int, float]] = []
    for bar in range(bars):
        base = bar * 16
        kick_slots = [0, 6, 10]
        if rng.random() < 0.5:
            kick_slots.append(rng.choice([3, 7, 14]))
        for slot in kick_slots:
            kick.append((base + slot, rng.uniform(0.82, 1.0) if slot == 0 else rng.uniform(0.6, 0.85)))
        snare.append((base + 4, rng.uniform(0.72, 0.9)))
        snare.append((base + 12, rng.uniform(0.6, 0.8)))
        if rng.random() < 0.35:
            snare.append((base + rng.choice([11, 13, 15]), rng.uniform(0.24, 0.42)))
        for step in range(16):
            if step % 2 == 1 and rng.random() < 0.18:
                continue  # dropped 16ths keep the hats from sounding like a metronome
            accent = 0.5 if step % 4 == 0 else 0.3
            hats.append((base + step, accent * rng.uniform(0.72, 1.05)))
        if rng.random() < 0.3:
            hats.append((base + 15, rng.uniform(0.4, 0.6)))
    return {"kick": kick, "snare": snare, "hat": hats}


def _melody_notes(recipe: Recipe, rng: "random.Random", bars: int) -> list[tuple[float, int, float]]:
    """Sparse pentatonic melody as ``(beat_position, midi, length_beats)``.

    The scale is the minor pentatonic of the key, with the major third allowed
    when the progression is major - one scale over four chords is exactly why
    lofi melodies sit so far back in the mix.
    """
    minor = recipe.progression[0][1].startswith("min") or recipe.progression[0][1] == "dim7"
    degrees = (0, 3, 5, 7, 10, 12) if minor else (0, 2, 4, 7, 9, 12)
    notes: list[tuple[float, int, float]] = []
    for bar in range(bars):
        if rng.random() > recipe.melody_density:
            continue
        count = rng.choice([1, 1, 2, 2, 3])
        beat = float(bar * 4)
        for _ in range(count):
            if beat >= (bar + 1) * 4:
                break
            length = rng.choice([0.5, 0.5, 0.75, 1.0, 1.5])
            midi = recipe.key_root + 60 + rng.choice(degrees)
            if rng.random() < 0.12:
                midi += 12
            notes.append((beat + rng.uniform(0.0, 0.06), midi, length))
            beat += length + rng.choice([0.0, 0.25, 0.5])
    return notes


def _stable_seed(text: str) -> int:
    """A 64-bit seed derived from a string, stable across runs and machines."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def render_audio(recipe: Recipe, seed: int = 0, bars: int = BARS_PER_TRACK) -> "np.ndarray":
    """Render one track and return an ``(n, 2)`` float array in ``[-1, 1]``.

    Everything random comes from two seeded generators - one Python
    (:class:`random.Random`) for musical decisions, one numpy for audio noise -
    so the same ``(recipe, seed)`` always renders the same audio. That is what
    makes the on-disk cache correct and what makes the dashboard's "now
    playing" title match the audio actually being heard.
    """
    _require_numpy()
    rng = random.Random(f"lofi/{recipe.id}/{seed}")
    noise_rng = np.random.default_rng(_stable_seed(f"lofi-noise/{recipe.id}/{seed}"))

    beat = recipe.seconds_per_beat
    total_seconds = bars * 4 * beat + 1.6  # a tail, so the echo can ring out
    count = int(total_seconds * SAMPLE_RATE)
    left = np.zeros(count, dtype=np.float64)
    right = np.zeros(count, dtype=np.float64)
    wow_depth = 0.0011 if recipe.id == "dusty" else 0.00055

    def place(
        signal: "np.ndarray", position_seconds: float, *, pan: float = 0.0, width: float = 0.0
    ) -> None:
        """Mix one mono voice into the stereo bus at a time offset.

        ``pan`` is a linear balance in ``[-1, 1]`` (negative is left). ``width``
        delays the right channel by a fraction of a millisecond and keeps a
        quieter copy at the original position - the Haas effect, which is the
        cheapest way to make a mono synth sound like it was recorded in a room.
        """
        start = int(position_seconds * SAMPLE_RATE)
        gain_left = 1.0 - 0.5 * pan
        gain_right = 1.0 + 0.5 * pan
        _add(left, start, signal * gain_left)
        if width > 0:
            delay = max(1, int(width * SAMPLE_RATE))
            _add(right, start, signal * (gain_right * 0.35))
            _add(right, start + delay, signal * gain_right)
        else:
            _add(right, start, signal * gain_right)

    # --- harmony ---------------------------------------------------------- #
    for bar in range(bars):
        degree, quality = recipe.progression[bar % len(recipe.progression)]
        bass_midi, chord = _voicing(recipe, degree, quality)
        bar_start = bar * 4 * beat
        chord_duration = 4 * beat * 0.98
        for index, midi_note in enumerate(chord):
            tone = _voice_electric_piano(
                _midi_to_freq(midi_note),
                chord_duration,
                0.19 if index % 2 == 0 else 0.155,
                rng,
                wow=wow_depth,
            )
            place(
                tone,
                bar_start + rng.uniform(0.0, 0.006),
                pan=-0.18 + 0.09 * index,
                width=0.0004 + 0.00012 * index,
            )
        # Bass on 1 and on the "and" of 3: the two notes a lofi bassist plays.
        for offset, length, gain in ((0.0, beat * 1.5, 0.42), (beat * 2.5, beat * 1.1, 0.30)):
            place(
                _voice_bass(_midi_to_freq(bass_midi), length, gain, rng, wow=wow_depth),
                bar_start + offset,
            )

    # --- drums ------------------------------------------------------------ #
    pattern = _drum_pattern(rng, bars)
    step_seconds = beat / 4.0
    swing_offset = (recipe.swing - 0.5) * 2 * step_seconds
    level = recipe.drums
    for slot, velocity in pattern["kick"]:
        position = slot * step_seconds + (swing_offset if slot % 2 == 1 else 0.0)
        place(_voice_kick(0.42, 0.78 * velocity * level), position)
    for slot, velocity in pattern["snare"]:
        position = slot * step_seconds + (swing_offset if slot % 2 == 1 else 0.0)
        place(_voice_snare(0.28, 0.42 * velocity * level, noise_rng), position, pan=0.06, width=0.0005)
    for slot, velocity in pattern["hat"]:
        position = slot * step_seconds + (swing_offset if slot % 2 == 1 else 0.0)
        open_hat = slot % 8 == 6 and rng.random() < 0.4
        place(
            _voice_hat(0.22 if open_hat else 0.06, 0.20 * velocity * level, noise_rng),
            position,
            pan=-0.12,
            width=0.0003,
        )

    # --- melody, through a tape echo -------------------------------------- #
    dry = np.zeros(count, dtype=np.float64)
    for beat_position, midi_note, length in _melody_notes(recipe, rng, bars):
        tone = _voice_electric_piano(
            _midi_to_freq(midi_note), length * beat * 1.5, 0.15, rng, wow=wow_depth * 1.6
        )
        tone = _filter(tone, _lowpass_tilt(recipe.cutoff_hz * 1.25))
        _add(dry, int(beat_position * beat * SAMPLE_RATE), tone)

    tap = max(1, int(beat * 0.75 * SAMPLE_RATE))  # a dotted eighth
    wet = np.zeros(count, dtype=np.float64)
    for index, gain in enumerate((0.40, 0.24, 0.13), start=1):
        offset = tap * index
        if offset < count:
            wet[offset:] += dry[: count - offset] * gain
    melody = dry + wet * (recipe.echo_mix / 0.30)
    left += melody * 1.02
    shift = int(0.0006 * SAMPLE_RATE)
    widened = np.zeros(count, dtype=np.float64)
    widened[shift:] = melody[: count - shift]
    right += widened * 0.98

    # --- texture ---------------------------------------------------------- #
    left += _vinyl_texture(count, noise_rng, recipe.crackle)
    right += _vinyl_texture(count, noise_rng, recipe.crackle)

    # --- master bus ------------------------------------------------------- #
    # The two channels get marginally different cutoffs: a real tape machine
    # never filters left and right identically, and the difference is what
    # keeps the image from collapsing to the centre. Single precision here is
    # deliberate - these are the only two transforms over the full track, and
    # halving their working set is worth more than the 24-bit mantissa nobody
    # can hear under vinyl noise.
    del melody, dry, wet, widened
    stereo = np.stack([left.astype(np.float32), right.astype(np.float32)])
    del left, right
    bus = np.stack(
        [
            _filter(stereo[0], _lowpass_tilt(recipe.cutoff_hz)),
            _filter(stereo[1], _lowpass_tilt(recipe.cutoff_hz * 0.985)),
        ],
        axis=1,
    )
    del stereo
    drive = recipe.saturation
    bus = (np.tanh(bus * np.float32(drive)) / math.tanh(drive)).astype(np.float32)
    peak = float(np.max(np.abs(bus))) or 1.0
    bus = bus * np.float32(TARGET_PEAK / peak)
    fade = max(1, int(0.05 * SAMPLE_RATE))
    bus[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
    bus[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
    return np.clip(bus, -1.0, 1.0)


def write_wav(target: Path, audio: "np.ndarray") -> Path:
    """Write an ``(n, 2)`` float array as a 16-bit stereo WAV."""
    _require_numpy()
    target.parent.mkdir(parents=True, exist_ok=True)
    interleaved = np.clip(np.asarray(audio), -1.0, 1.0)
    pcm = (interleaved * np.asarray(32767.0, dtype=interleaved.dtype)).astype("<i2")
    tmp = target.with_suffix(target.suffix + ".part")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    tmp.replace(target)
    return target


def cache_key(recipe: Recipe, seed: int, bars: int) -> str:
    """A stable filename for one render, derived from its parameters."""
    digest = hashlib.sha256(
        f"{recipe.id}|{seed}|{bars}|{recipe.bpm}|{recipe.progression}|{recipe.cutoff_hz}".encode()
    ).hexdigest()[:16]
    return f"gen-{recipe.id}-{seed}-{digest}.wav"


def cached_path(recipe: Recipe, seed: int, bars: int = BARS_PER_TRACK) -> Path:
    """Where a render lives (or will live) in the cache directory."""
    return paths.cache_dir() / cache_key(recipe, seed, bars)


def render_track(
    recipe_reference: Any = None,
    seed: int = 0,
    *,
    bars: int = BARS_PER_TRACK,
    force: bool = False,
) -> dict[str, Any]:
    """Render (or reuse) one track and describe it.

    Returns ``{"path", "title", "duration", "recipe", "seed", "cached"}`` - the
    shape :mod:`sources` needs to build a playable track, plus the title the
    dashboard shows. Rendering happens here rather than in the caller so the
    cache is the only place that knows how files are named.
    """
    _require_numpy()
    recipe = recipe_for(recipe_reference)
    target = cached_path(recipe, seed, bars)
    was_cached = target.is_file() and not force
    if not was_cached:
        logger.info("Rendering %s (seed %s) -> %s", recipe.name, seed, target.name)
        audio = render_audio(recipe, seed=seed, bars=bars)
        write_wav(target, audio)
    duration = bars * 4 * recipe.seconds_per_beat + 1.6
    return {
        "path": str(target),
        "title": track_title(recipe, seed),
        "duration": duration,
        "recipe": recipe.id,
        "recipe_name": recipe.name,
        "seed": int(seed),
        "cached": was_cached,
    }


def prune_cache(keep: int = 40) -> int:
    """Delete the oldest rendered tracks beyond ``keep``. Returns how many went.

    The cache only ever grows as seeds advance, so a bot left running for
    weeks would otherwise fill a disk with audio nobody will hear again.
    """
    directory = paths.CACHE_DIR
    if not directory.is_dir():
        return 0
    files = sorted(directory.glob("gen-*.wav"), key=lambda item: item.stat().st_mtime)
    excess = len(files) - max(0, keep)
    removed = 0
    for stale in files[: max(0, excess)]:
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:  # pragma: no cover - concurrent pruning
            logger.debug("Could not remove cached render %s: %s", stale, exc)
    return removed


__all__ = [
    "BARS_PER_TRACK",
    "DEFAULT_RECIPE_ID",
    "GenerativeUnavailable",
    "RECIPES",
    "RECIPE_BY_ID",
    "SAMPLE_RATE",
    "TRACK_SECONDS",
    "available",
    "cache_key",
    "cached_path",
    "prune_cache",
    "recipe_for",
    "recipe_names",
    "render_audio",
    "render_track",
    "track_title",
    "write_wav",
]
