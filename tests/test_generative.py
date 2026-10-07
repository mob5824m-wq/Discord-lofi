"""The offline renderer: determinism, level, cache behaviour and WAV output.

Everything here is pure computation on this machine, so it runs in the same
sandbox as the bot with no network and no ffmpeg - which is exactly the point of
the station.
"""

from __future__ import annotations

import math
import wave

import pytest

import generative
import paths


numpy = pytest.importorskip("numpy")


@pytest.fixture
def cache(tmp_state, monkeypatch):
    """Point the render cache at the test's own directory."""
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_state / "cache")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_state / "cache")
    (tmp_state / "cache").mkdir(exist_ok=True)
    return tmp_state / "cache"


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #
def test_every_recipe_is_complete():
    for recipe in generative.RECIPES:
        assert recipe.id and recipe.name and recipe.description
        assert 40 < recipe.bpm < 200
        assert 0 <= recipe.key_root < 12
        assert recipe.progression
        assert 0 < recipe.crackle <= 1
        assert recipe.cutoff_hz > 200


def test_recipe_ids_are_unique():
    ids = [recipe.id for recipe in generative.RECIPES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("reference", ["midnight", "rainy", "cafe", "dusty", "nocturne"])
def test_every_recipe_can_be_named(reference):
    assert generative.recipe_for(reference).id == reference


def test_recipe_for_is_case_insensitive_and_trims():
    assert generative.recipe_for("  CAFE ").id == "cafe"


def test_recipe_for_accepts_a_generative_url():
    assert generative.recipe_for("generative:rainy").id == "rainy"


def test_recipe_for_accepts_a_recipe_instance():
    recipe = generative.recipe_for("midnight")
    assert generative.recipe_for(recipe) is recipe


@pytest.mark.parametrize("reference", [None, "", "not-a-mood", 12345])
def test_an_unknown_reference_falls_back_to_the_default_recipe(reference):
    assert generative.recipe_for(reference).id == generative.DEFAULT_RECIPE_ID


def test_recipe_names_are_dashboard_ready():
    names = generative.recipe_names()
    assert len(names) == len(generative.RECIPES)
    assert {"id", "name", "description"} <= set(names[0])


def test_seconds_per_beat_matches_the_tempo():
    recipe = generative.recipe_for("cafe")
    assert recipe.seconds_per_beat == pytest.approx(60.0 / recipe.bpm)


def test_chord_names_are_readable():
    recipe = generative.recipe_for("midnight")
    text = recipe.progression_text()
    assert "–" in text
    assert any(char in text for char in "m7")


# --------------------------------------------------------------------------- #
# Titles
# --------------------------------------------------------------------------- #
def test_a_title_is_stable_for_a_seed():
    recipe = generative.recipe_for("midnight")
    assert generative.track_title(recipe, 4242) == generative.track_title(recipe, 4242)


def test_titles_differ_between_seeds():
    recipe = generative.recipe_for("midnight")
    titles = {generative.track_title(recipe, seed) for seed in range(40)}
    assert len(titles) > 5


def test_a_title_mentions_the_mood_or_stays_short():
    recipe = generative.recipe_for("cafe")
    title = generative.track_title(recipe, 7)
    assert title and len(title) < 80


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_the_same_seed_renders_the_same_audio():
    """Determinism is what makes the cache safe: two renders of one seed are
    byte-identical, so a cached file is the same track the bot would have made."""
    recipe = generative.recipe_for("cafe")
    first = generative.render_audio(recipe, seed=11, bars=2)
    second = generative.render_audio(recipe, seed=11, bars=2)
    assert numpy.array_equal(first, second)


def test_different_seeds_render_different_audio():
    recipe = generative.recipe_for("cafe")
    first = generative.render_audio(recipe, seed=11, bars=2)
    second = generative.render_audio(recipe, seed=12, bars=2)
    assert not numpy.array_equal(first, second)


def test_different_recipes_render_different_audio():
    first = generative.render_audio(generative.recipe_for("midnight"), seed=5, bars=2)
    second = generative.render_audio(generative.recipe_for("cafe"), seed=5, bars=2)
    assert not numpy.array_equal(first, second)


def test_rendered_audio_is_stereo_and_finite():
    audio = generative.render_audio(generative.recipe_for("rainy"), seed=3, bars=2)
    assert audio.ndim == 2 and audio.shape[1] == 2
    assert numpy.isfinite(audio).all()


def test_rendered_audio_is_loud_enough_but_not_clipping():
    """Normalised to TARGET_PEAK: quiet enough to leave headroom for Discord's
    encoder, loud enough not to sound broken next to a YouTube stream."""
    audio = generative.render_audio(generative.recipe_for("dusty"), seed=9, bars=2)
    peak = float(numpy.abs(audio).max())
    assert peak == pytest.approx(generative.TARGET_PEAK, abs=0.02)
    assert peak <= 1.0


def test_rendered_audio_is_not_silent():
    audio = generative.render_audio(generative.recipe_for("nocturne"), seed=9, bars=2)
    rms = float(numpy.sqrt(numpy.mean(numpy.square(audio))))
    assert rms > 0.02


def test_the_track_length_matches_the_requested_bars():
    recipe = generative.recipe_for("cafe")
    audio = generative.render_audio(recipe, seed=1, bars=4)
    expected = 4 * 4 * recipe.seconds_per_beat + 1.6  # four beats per bar plus the tail
    assert audio.shape[0] / generative.SAMPLE_RATE == pytest.approx(expected, abs=0.05)


def test_a_full_track_is_about_a_minute_long():
    assert generative.TRACK_SECONDS == pytest.approx(
        generative.BARS_PER_TRACK * 4 * generative.recipe_for("midnight").seconds_per_beat + 1.6,
        abs=3.0,
    )


def test_render_track_writes_a_readable_wav(cache):
    result = generative.render_track("cafe", seed=17, bars=2)
    with wave.open(result["path"], "rb") as handle:
        assert handle.getnchannels() == 2
        assert handle.getsampwidth() == 2  # 16-bit
        assert handle.getframerate() == generative.SAMPLE_RATE
        assert handle.getnframes() > 0


def test_render_track_describes_what_it_made(cache):
    result = generative.render_track("rainy", seed=21, bars=2)
    assert set(result) >= {"path", "title", "duration", "recipe", "seed", "cached"}
    assert result["recipe"] == "rainy"
    assert result["recipe_name"] == generative.recipe_for("rainy").name
    assert result["seed"] == 21
    assert result["cached"] is False
    assert result["duration"] > 0


def test_a_second_render_comes_from_the_cache(cache):
    first = generative.render_track("cafe", seed=31, bars=2)
    second = generative.render_track("cafe", seed=31, bars=2)
    assert first["path"] == second["path"]
    assert second["cached"] is True


def test_force_rerenders_even_when_cached(cache):
    generative.render_track("cafe", seed=33, bars=2)
    assert generative.render_track("cafe", seed=33, bars=2, force=True)["cached"] is False


def test_a_different_seed_is_a_different_file(cache):
    first = generative.render_track("cafe", seed=41, bars=2)
    second = generative.render_track("cafe", seed=42, bars=2)
    assert first["path"] != second["path"]


def test_the_cache_key_is_stable_and_seed_sensitive():
    recipe = generative.recipe_for("cafe")
    assert generative.cache_key(recipe, 5, 16) == generative.cache_key(recipe, 5, 16)
    assert generative.cache_key(recipe, 5, 16) != generative.cache_key(recipe, 6, 16)
    assert generative.cache_key(recipe, 5, 16) != generative.cache_key(recipe, 5, 8)


def test_the_cache_key_names_the_recipe():
    assert "cafe" in generative.cache_key(generative.recipe_for("cafe"), 5, 16)


def test_cached_path_lives_in_the_cache_directory(cache):
    target = generative.cached_path(generative.recipe_for("cafe"), 5, 16)
    assert target.parent == cache
    assert target.suffix == ".wav"


def test_render_track_defaults_to_the_default_recipe(cache):
    assert generative.render_track(None, seed=1, bars=2)["recipe"] == generative.DEFAULT_RECIPE_ID


def test_prune_cache_keeps_the_newest_files(cache):
    for seed in range(6):
        generative.render_track("cafe", seed=seed, bars=1)
    assert len(list(cache.glob("*.wav"))) == 6
    removed = generative.prune_cache(keep=2)
    assert removed == 4
    assert len(list(cache.glob("*.wav"))) == 2


def test_prune_cache_leaves_a_small_cache_alone(cache):
    generative.render_track("cafe", seed=1, bars=1)
    assert generative.prune_cache(keep=40) == 0


def test_prune_cache_ignores_other_files(cache):
    (cache / "keep-me.txt").write_text("not audio")
    generative.prune_cache(keep=0)
    assert (cache / "keep-me.txt").exists()


def test_write_wav_creates_missing_directories(tmp_state):
    target = tmp_state / "deep" / "nested" / "track.wav"
    audio = generative.render_audio(generative.recipe_for("cafe"), seed=1, bars=1)
    generative.write_wav(target, audio)
    assert target.is_file()


def test_midi_to_frequency_matches_concert_pitch():
    assert generative._midi_to_freq(69) == pytest.approx(440.0, abs=0.01)
    assert generative._midi_to_freq(60) == pytest.approx(261.63, abs=0.05)


def test_midi_to_frequency_doubles_every_twelve_semitones():
    assert generative._midi_to_freq(81) == pytest.approx(2 * generative._midi_to_freq(69), abs=0.01)


def test_the_envelope_starts_and_ends_at_zero():
    """A hard cutoff at the end of a note is an audible click; the release ramp
    is what stops a rendered track from sounding like it is full of ticks."""
    envelope = generative._envelope(4410, attack=0.01, decay=0.05, sustain=0.5, release=0.05)
    assert envelope[0] == pytest.approx(0.0, abs=1e-6)
    assert envelope[-1] == pytest.approx(0.0, abs=1e-6)
    assert 0.0 < envelope.max() <= 1.0


def test_the_release_ramp_is_what_brings_the_tail_down():
    with_release = generative._envelope(4410, attack=0.01, decay=0.2, sustain=0.6, release=0.05)
    without = generative._envelope(4410, attack=0.01, decay=0.2, sustain=0.6)
    assert without[-1] > with_release[-1]
    assert with_release[-1] == pytest.approx(0.0, abs=1e-9)


def test_an_empty_envelope_is_empty():
    assert generative._envelope(0, attack=0.01, decay=0.05).size == 0


def test_a_drum_pattern_covers_the_requested_bars():
    import random

    pattern = generative._drum_pattern(random.Random(3), bars=4)
    assert set(pattern) == {"kick", "snare", "hat"}
    # Steps are sixteenths, so four bars is 64 steps and no hit may fall past it.
    for hits in pattern.values():
        assert hits
        assert all(0 <= step < 4 * 16 for step, _ in hits)


def test_a_drum_pattern_is_swung_and_accented():
    import random

    pattern = generative._drum_pattern(random.Random(5), bars=8)
    velocities = [velocity for _, velocity in pattern["kick"]]
    assert max(velocities) > min(velocities)  # a metronome would be flat
    assert len(pattern["hat"]) > len(pattern["kick"])


def test_a_stable_seed_maps_text_to_the_same_number():
    assert generative._stable_seed("abc") == generative._stable_seed("abc")
    assert generative._stable_seed("abc") != generative._stable_seed("abd")


def test_available_reports_true_when_numpy_is_importable():
    assert generative.available() is True
