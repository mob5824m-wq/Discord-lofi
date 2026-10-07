"""Turning a station into something ffmpeg can open.

Network sources are stubbed: the sandbox has no route to YouTube or to an
icecast host, and a test that depends on the internet is a test that fails for
the wrong reason. What *is* exercised for real is the local library, the
generated station, the ffmpeg argument handling and every error message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import paths
import sources
import stations
from sources import SourceError, Track
from stations import Station


@pytest.fixture
def sync_runner():
    """A ``runner`` that calls blocking work inline instead of using a thread."""

    async def runner(func, *args):
        return func(*args)

    return runner


RESOLVED = {
    "url": "https://video.example/stream.m3u8",
    "title": "lofi hip hop radio - beats to relax/study to",
    "is_live": True,
    "duration": None,
    "webpage_url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
    "thumbnail": "https://img.example/thumb.jpg",
    "uploader": "Lofi Girl",
}


@pytest.fixture
def fake_resolution(monkeypatch):
    """Replace yt-dlp with a canned result, and count how often it is called.

    Both seams are patched: ``build_track`` reaches ``resolve_youtube_blocking``
    through the runner, while ``resolve_youtube`` reaches the cached wrapper in a
    worker thread. Patching only one sends a test to the real network.
    """
    calls: list[str] = []

    def fake(candidate: str) -> dict:
        calls.append(candidate)
        return dict(RESOLVED)

    monkeypatch.setattr(sources, "resolve_youtube_blocking", fake)
    monkeypatch.setattr(sources, "_resolve_youtube_blocking", fake)
    sources.clear_resolution_cache()
    yield calls
    sources.clear_resolution_cache()


# --------------------------------------------------------------------------- #
# Local library
# --------------------------------------------------------------------------- #
def test_scan_library_finds_audio_files(music_dir):
    found = sources.scan_library(music_dir)
    names = {Path(item.path).name for item in found}
    assert names == {"Artist One - Slow Tape.mp3", "kettle.wav", "03 - Third Track.flac"}


def test_scan_library_ignores_non_audio_files(music_dir):
    assert all(not item.path.endswith(".txt") for item in sources.scan_library(music_dir))


def test_scan_library_recurses_into_subfolders(music_dir):
    found = sources.scan_library(music_dir)
    assert any("mixes" in item.path for item in found)


def test_scan_library_records_the_depth(music_dir):
    found = {Path(item.path).name: item for item in sources.scan_library(music_dir)}
    assert found["kettle.wav"].depth == 0
    assert found["03 - Third Track.flac"].depth == 1


def test_scan_library_splits_artist_and_title(music_dir):
    found = {Path(item.path).name: item for item in sources.scan_library(music_dir)}
    assert found["Artist One - Slow Tape.mp3"].artist == "Artist One"
    assert found["Artist One - Slow Tape.mp3"].title == "Slow Tape"


def test_scan_library_strips_a_leading_track_number(music_dir):
    found = {Path(item.path).name: item for item in sources.scan_library(music_dir)}
    assert found["03 - Third Track.flac"].title == "Third Track"


def test_scan_library_of_a_missing_folder_is_empty(tmp_state):
    assert sources.scan_library(tmp_state / "nowhere") == []


def test_scan_library_of_nothing_at_all_is_empty(monkeypatch):
    monkeypatch.setattr(paths, "music_dir", lambda: None)
    assert sources.scan_library() == []


def test_scan_library_respects_the_limit(music_dir):
    assert len(sources.scan_library(music_dir, limit=2)) == 2


def test_library_summary_describes_the_folder(music_dir):
    summary = sources.library_summary(music_dir)
    assert summary["exists"] is True
    assert summary["count"] == 3
    assert summary["totalBytes"] > 0
    assert summary["path"] == str(music_dir)
    assert len(summary["files"]) == 3
    assert summary["truncated"] is False


def test_library_summary_reports_a_missing_folder(tmp_state):
    summary = sources.library_summary(tmp_state / "nowhere")
    assert summary["exists"] is False
    assert summary["count"] == 0
    assert summary["files"] == []


def test_library_summary_reports_no_folder_at_all(monkeypatch):
    monkeypatch.setattr(paths, "music_dir", lambda: None)
    summary = sources.library_summary()
    assert summary["exists"] is False
    assert summary["path"] is None


def test_library_summary_caps_the_file_list(music_dir):
    summary = sources.library_summary(music_dir)
    assert len(summary["files"]) <= 60


def test_library_file_to_dict_is_json_safe(music_dir):
    payload = sources.scan_library(music_dir)[0].to_dict()
    assert set(payload) == {"path", "title", "artist", "size", "modified"}


@pytest.mark.parametrize("stem,artist,title", [
    ("Artist One - Slow Tape", "Artist One", "Slow Tape"),
    ("03 - Third Track", None, "Third Track"),
    ("kettle", None, "kettle"),
    ("Some - Artist - Name", "Some", "Artist - Name"),
])
def test_pretty_title(stem, artist, title):
    assert sources._pretty_title(stem) == (artist, title)


def test_pick_library_file_returns_a_file(music_dir):
    chosen = sources.pick_library_file(root=music_dir)
    assert chosen is not None
    assert Path(chosen.path).exists()


def test_pick_library_file_avoids_what_was_played_recently(music_dir):
    all_files = sources.scan_library(music_dir)
    recent = [item.path for item in all_files[:2]]
    for _ in range(12):
        chosen = sources.pick_library_file(recent, root=music_dir)
        assert chosen.path not in recent


def test_pick_library_file_falls_back_when_everything_is_recent(music_dir):
    """With three files and a long memory, refusing to repeat would mean silence."""
    all_files = sources.scan_library(music_dir)
    chosen = sources.pick_library_file([item.path for item in all_files], root=music_dir)
    assert chosen is not None


def test_pick_library_file_is_deterministic_for_a_seed(music_dir):
    import random

    first = sources.pick_library_file(root=music_dir, rng=random.Random(7))
    second = sources.pick_library_file(root=music_dir, rng=random.Random(7))
    assert first.path == second.path


def test_pick_library_file_of_an_empty_folder_is_none(tmp_state):
    assert sources.pick_library_file(root=tmp_state / "nowhere") is None


def test_the_recent_memory_is_bounded():
    assert sources.RECENT_LIBRARY_MEMORY > 0


# --------------------------------------------------------------------------- #
# build_track: library
# --------------------------------------------------------------------------- #
def test_a_library_station_becomes_a_local_file_track(music_dir, sync_runner):
    station = Station(id="library", name="My Library", kind="library", url="library:music")
    track = asyncio.run(sources.build_track(station, requested_by=9, runner=sync_runner))
    assert track.kind == "library"
    assert Path(track.location).is_file()
    assert track.is_live is False
    assert track.requested_by == 9
    assert track.station_id == "library"
    assert track.art == station.art  # the station's icon, not a hardcoded one


def test_a_library_track_title_includes_the_artist(music_dir, sync_runner):
    station = Station(id="library", name="My Library", kind="library", url="library:music")
    titles = {asyncio.run(sources.build_track(station, runner=sync_runner)).title for _ in range(20)}
    assert "Artist One – Slow Tape" in titles


def test_a_library_station_can_point_at_its_own_folder(tmp_state, sync_runner):
    folder = tmp_state / "tapes"
    folder.mkdir()
    (folder / "Only Track.mp3").write_bytes(b"id3fake")
    station = Station(id="mine", name="Mine", kind="library", url=f"library:{folder}")
    track = asyncio.run(sources.build_track(station, runner=sync_runner))
    assert track.location.endswith("Only Track.mp3")
    assert track.origin == "Only Track.mp3"  # relative to the library root


def test_a_library_station_pointing_at_a_missing_folder_explains_itself(tmp_state, sync_runner):
    station = Station(id="mine", name="Mine", kind="library", url=f"library:{tmp_state / 'nowhere'}")
    with pytest.raises(SourceError) as error:
        asyncio.run(sources.build_track(station, runner=sync_runner))
    assert "nowhere" in str(error.value)


def test_an_empty_library_says_what_to_do(tmp_state, monkeypatch, sync_runner):
    empty = tmp_state / "empty"
    empty.mkdir()
    monkeypatch.setattr(paths, "music_dir", lambda: empty)
    station = Station(id="library", name="My Library", kind="library", url="library:music")
    with pytest.raises(SourceError) as error:
        asyncio.run(sources.build_track(station, runner=sync_runner))
    message = str(error.value)
    assert ".mp3" in message and "Studio Lofi" in message


def test_a_library_track_without_a_folder_says_what_to_do(tmp_state, monkeypatch, sync_runner):
    monkeypatch.setattr(paths, "music_dir", lambda: None)
    station = Station(id="library", name="My Library", kind="library", url="library:music")
    with pytest.raises(SourceError, match="music folder"):
        asyncio.run(sources.build_track(station, runner=sync_runner))


def test_probe_duration_of_a_real_wav(tmp_state):
    import generative

    target = tmp_state / "clip.wav"
    generative.write_wav(target, generative.render_audio(generative.recipe_for("cafe"), seed=1, bars=1))
    duration = sources.probe_duration(str(target))
    assert duration is None or duration > 0  # None when ffprobe is not installed


def test_probe_duration_of_a_missing_file_is_none(tmp_state):
    assert sources.probe_duration(str(tmp_state / "ghost.wav")) is None


# --------------------------------------------------------------------------- #
# build_track: generative
# --------------------------------------------------------------------------- #
def test_a_generative_station_renders_a_local_file(tmp_state, monkeypatch, sync_runner):
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_state / "cache")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_state / "cache")
    station = stations.studio_station("studio-cafe")
    track = asyncio.run(sources.build_track(station, seed=1234, runner=sync_runner))
    assert track.kind == "generative"
    assert Path(track.location).is_file()
    assert track.is_live is False
    assert track.seed == 1234
    assert track.recipe == "cafe"
    assert track.duration > 10
    assert "Studio" in track.station_name


def test_a_generative_track_has_a_generated_title(tmp_state, monkeypatch, sync_runner):
    import generative

    monkeypatch.setattr(paths, "CACHE_DIR", tmp_state / "cache")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_state / "cache")
    station = stations.studio_station("studio-rainy")
    track = asyncio.run(sources.build_track(station, seed=77, runner=sync_runner))
    assert track.title == generative.track_title(generative.recipe_for("rainy"), 77)


def test_a_generative_station_without_a_seed_still_renders(tmp_state, monkeypatch, sync_runner):
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_state / "cache")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_state / "cache")
    track = asyncio.run(sources.build_track(stations.BUILT_IN_BY_ID["generative"], runner=sync_runner))
    assert Path(track.location).is_file()
    assert track.seed is not None


def test_the_recipe_comes_from_the_station_url(tmp_state, monkeypatch, sync_runner):
    """One registry entry, several moods: the URL carries which one."""
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_state / "cache")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_state / "cache")
    station = Station(id="gen", name="Gen", kind="generative", url="generative:dusty")
    assert sources._recipe_reference(station) == "dusty"
    track = asyncio.run(sources.build_track(station, seed=3, runner=sync_runner))
    assert track.recipe == "dusty"


# --------------------------------------------------------------------------- #
# build_track: stream
# --------------------------------------------------------------------------- #
def test_a_stream_station_needs_no_resolution(sync_runner):
    station = Station(id="soma", name="Groove Salad", kind="stream", url="https://ice2.somafm.com/groovesalad-128-mp3")
    track = asyncio.run(sources.build_track(station, runner=sync_runner))
    assert track.kind == "stream"
    assert track.location == station.url
    assert track.is_live is True
    assert track.title == "Groove Salad"


def test_a_stream_station_without_a_url_is_refused(sync_runner):
    station = Station(id="broken", name="Broken", kind="stream", url="")
    with pytest.raises(SourceError, match="no stream URL"):
        asyncio.run(sources.build_track(station, runner=sync_runner))


def test_a_stream_station_pointing_at_a_private_address_is_refused(sync_runner):
    station = Station(id="lan", name="LAN", kind="stream", url="http://192.168.1.5/x.mp3")
    with pytest.raises(SourceError, match="private network"):
        asyncio.run(sources.build_track(station, runner=sync_runner))


# --------------------------------------------------------------------------- #
# build_track: youtube
# --------------------------------------------------------------------------- #
def test_a_youtube_station_resolves_to_a_stream_url(fake_resolution, sync_runner):
    station = stations.BUILT_IN_BY_ID["lofi-girl"]
    track = asyncio.run(sources.build_track(station, runner=sync_runner))
    assert track.kind == "youtube"
    assert track.location == "https://video.example/stream.m3u8"
    assert track.is_live is True
    assert track.uploader == "Lofi Girl"
    assert track.artwork_url == "https://img.example/thumb.jpg"
    assert track.origin == "https://www.youtube.com/watch?v=jfKfPfyJRdk"
    assert fake_resolution == [station.url]


def test_a_station_with_only_a_search_query_is_searched(fake_resolution, sync_runner):
    station = stations.BUILT_IN_BY_ID["sleepy-lofi"]
    track = asyncio.run(sources.build_track(station, runner=sync_runner))
    assert fake_resolution == [f"ytsearch1:{station.search}"]
    assert track.kind == "youtube"


def test_a_failed_link_falls_back_to_the_search_query(monkeypatch, sync_runner):
    """A 24/7 broadcast changes its video id; the search query gets it back on air."""
    attempts: list[str] = []

    def flaky(candidate: str) -> dict:
        attempts.append(candidate)
        if candidate.startswith("ytsearch"):
            return {"url": "https://video.example/found", "title": "Found by search",
                    "is_live": True, "duration": None, "webpage_url": candidate}
        raise SourceError("That video is no longer available.")

    monkeypatch.setattr(sources, "resolve_youtube_blocking", flaky)
    station = stations.BUILT_IN_BY_ID["lofi-girl"]
    track = asyncio.run(sources.build_track(station, runner=sync_runner))
    assert track.location == "https://video.example/found"
    assert len(attempts) == 2


def test_a_station_with_nothing_to_resolve_is_refused(sync_runner):
    station = Station(id="empty", name="Empty", kind="youtube", url="", search="")
    with pytest.raises(SourceError, match="no URL to resolve"):
        asyncio.run(sources.build_track(station, runner=sync_runner))


def test_a_station_that_fails_every_way_reports_the_last_reason(monkeypatch, sync_runner):
    def always(candidate: str) -> dict:
        raise SourceError("YouTube is asking this connection to sign in.")

    monkeypatch.setattr(sources, "resolve_youtube_blocking", always)
    sources.clear_resolution_cache()
    with pytest.raises(SourceError, match="sign in"):
        asyncio.run(sources.build_track(stations.BUILT_IN_BY_ID["lofi-girl"], runner=sync_runner))


def test_an_unknown_station_kind_is_refused(sync_runner):
    station = Station(id="weird", name="Weird", kind="spotify", url="x")
    with pytest.raises(SourceError, match="Unknown station kind"):
        asyncio.run(sources.build_track(station, runner=sync_runner))


def test_requested_by_is_carried_onto_the_track(fake_resolution, sync_runner):
    track = asyncio.run(sources.build_track(stations.BUILT_IN_BY_ID["lofi-girl"], requested_by=42, runner=sync_runner))
    assert track.requested_by == 42


# --------------------------------------------------------------------------- #
# The resolution cache
# --------------------------------------------------------------------------- #
def test_a_resolution_is_cached_for_a_while(fake_resolution, sync_runner):
    station = stations.BUILT_IN_BY_ID["lofi-girl"]
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    assert len(fake_resolution) == 1


def test_the_cache_can_be_bypassed(fake_resolution, sync_runner):
    station = stations.BUILT_IN_BY_ID["lofi-girl"]
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    asyncio.run(sources.resolve_youtube(station.url, use_cache=False))
    assert len(fake_resolution) == 2


def test_clearing_the_cache_forces_a_fresh_resolution(fake_resolution, sync_runner):
    station = stations.BUILT_IN_BY_ID["lofi-girl"]
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    sources.clear_resolution_cache()
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    assert len(fake_resolution) == 2


def test_a_stale_resolution_is_not_reused(fake_resolution, monkeypatch, sync_runner):
    """yt-dlp hands out URLs that expire; a 45-minute-old one must be re-fetched."""
    station = stations.BUILT_IN_BY_ID["lofi-girl"]
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    entries = list(sources._RESOLUTION_CACHE.items())
    sources._RESOLUTION_CACHE.clear()
    for key, (stamp, payload) in entries:
        sources._RESOLUTION_CACHE[key] = (stamp - sources.RESOLUTION_TTL_SECONDS - 60, payload)
    asyncio.run(sources.resolve_youtube(station.url, use_cache=True))
    assert len(fake_resolution) == 2


def test_a_failed_resolution_is_not_cached(monkeypatch, sync_runner):
    calls = []

    def always(candidate: str) -> dict:
        calls.append(candidate)
        raise SourceError("nope")

    monkeypatch.setattr(sources, "_resolve_youtube_blocking", always)
    sources.clear_resolution_cache()
    for _ in range(2):
        with pytest.raises(SourceError):
            asyncio.run(sources.resolve_youtube("https://youtu.be/abc"))
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# ffmpeg arguments
# --------------------------------------------------------------------------- #
def test_network_tracks_get_ffmpeg_reconnect_flags_before_the_input():
    """Placed before ``-i`` these survive a dropped TCP connection on a 24/7
    stream; after it, ffmpeg ignores them and the station goes silent."""
    stream = asyncio.run(sources.build_track(
        Station(id="s", name="S", kind="stream", url="https://ice2.somafm.com/groovesalad-128-mp3")
    ))
    before = sources.ffmpeg_before_options(stream)
    assert before.startswith("-reconnect 1")
    assert "-reconnect_streamed 1" in before
    assert "-reconnect_at_eof 1" in before


def test_youtube_tracks_also_get_reconnect_flags():
    track = Track(title="t", station_id="s", station_name="n", kind="youtube", location="https://x/y")
    assert "-reconnect" in sources.ffmpeg_before_options(track)


def test_local_tracks_get_no_network_flags():
    track = Track(title="t", station_id="s", station_name="n", kind="library", location="/tmp/a.mp3")
    assert sources.ffmpeg_before_options(track) == "-nostdin"
    generated = Track(title="t", station_id="s", station_name="n", kind="generative", location="/tmp/a.wav")
    assert sources.ffmpeg_before_options(generated) == "-nostdin"


@pytest.mark.parametrize("kind", ["stream", "youtube", "library", "generative"])
def test_every_kind_has_after_input_options(kind):
    track = Track(title="t", station_id="s", station_name="n", kind=kind, location="/tmp/a.mp3")
    assert track.ffmpeg_options == sources.FFMPEG_OPTIONS[kind]


def test_an_unknown_kind_gets_no_extra_options():
    track = Track(title="t", station_id="s", station_name="n", kind="weird", location="/tmp/a.mp3")
    assert track.ffmpeg_options == ""


def test_the_ffmpeg_command_is_reproducible_for_a_support_conversation():
    track = Track(title="t", station_id="s", station_name="n", kind="stream", location="https://ice2.somafm.com/g")
    command = sources.ffmpeg_command(track)
    assert command[0] == (paths.ffmpeg_executable() or "ffmpeg")
    assert "-i" in command
    assert command[command.index("-i") + 1] == track.location
    assert command[-1] == "pipe:1"
    assert "48000" in command  # Discord wants 48 kHz stereo s16le


def test_the_ffmpeg_command_puts_reconnect_flags_before_the_input():
    track = Track(title="t", station_id="s", station_name="n", kind="stream", location="https://example.com/s")
    command = sources.ffmpeg_command(track)
    assert command.index("-reconnect") < command.index("-i")


def test_the_ffmpeg_command_for_a_local_file_has_no_network_flags():
    track = Track(title="t", station_id="s", station_name="n", kind="library", location="/tmp/a.mp3")
    command = sources.ffmpeg_command(track)
    assert "-reconnect" not in command
    assert "-nostdin" in command


# --------------------------------------------------------------------------- #
# Error text
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("error_text,expected_fragment", [
    ("Sign in to confirm your age", "sign in"),
    ("Please sign in to verify", "sign in"),
    ("HTTP Error 403: Forbidden", "yt-dlp"),
    ("Video unavailable: this video is private", "no longer available"),
    ("This video has been removed", "no longer available"),
    ("Unable to download webpage: timed out", "could not be reached"),
    ("[SSL: TLSV1_ALERT] something", "could not be reached"),
    ("No module named yt_dlp", "not installed"),
    ("something entirely new", "something entirely new"),
])
def test_youtube_errors_are_translated_into_advice(error_text, expected_fragment):
    message = sources._explain_youtube_error("https://youtu.be/x", error_text, {})
    assert expected_fragment.lower() in message.lower()


def test_an_unexplained_error_is_truncated():
    message = sources._explain_youtube_error("x", "y" * 5000, {})
    assert len(message) < 300


def test_an_empty_error_still_produces_a_message():
    assert sources._explain_youtube_error("x", "", {}).strip()


# --------------------------------------------------------------------------- #
# Track
# --------------------------------------------------------------------------- #
def test_track_to_dict_uses_camel_case_and_string_snowflakes():
    track = Track(
        title="T", station_id="s", station_name="S", kind="youtube",
        location="https://x", requested_by=400000000000000001,
    )
    payload = track.to_dict()
    assert payload["requestedBy"] == "400000000000000001"
    assert payload["stationId"] == "s"
    assert payload["isLive"] is False
    import json

    json.dumps(payload)


def test_a_track_without_a_requested_by_says_none():
    assert Track(title="T", station_id="s", station_name="S", kind="library", location="/x").to_dict()["requestedBy"] is None


def test_display_title_falls_back_to_the_station_name():
    assert Track(title="", station_id="s", station_name="Groove Salad", kind="stream", location="x").display_title == "Groove Salad"
    assert Track(title="", station_id="s", station_name="", kind="stream", location="x").display_title == "Unknown track"
