"""Config/data path resolution, and finding the binaries audio depends on."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Optional

import pytest

import paths


def test_default_config_has_every_key_the_code_reads():
    """A key missing here becomes a KeyError in a code path nobody tested."""
    for key in (
        "bot_token",
        "guilds",
        "stations",
        "default_station",
        "default_volume",
        "idle_disconnect_minutes",
        "stay_connected",
        "announce_now_playing",
        "channel_status",
        "dashboard_enabled",
        "dashboard_host",
        "dashboard_port",
        "dashboard_token",
        "dashboard_allowed_hosts",
        "dashboard_public_url",
        "dashboard_secure_cookie",
        "dashboard_trusted_proxies",
    ):
        assert key in paths.DEFAULT_CONFIG, key


def test_load_config_merges_defaults_over_a_partial_file(tmp_state):
    target = tmp_state / "config.json"
    target.write_text(json.dumps({"bot_token": "abc", "default_volume": 42}), encoding="utf-8")
    loaded = paths.load_config()
    assert loaded["bot_token"] == "abc"
    assert loaded["default_volume"] == 42
    assert loaded["dashboard_port"] == paths.DEFAULT_CONFIG["dashboard_port"]


def test_load_config_falls_back_to_defaults_when_the_file_is_absent(tmp_state):
    loaded = paths.load_config()
    assert loaded == paths.DEFAULT_CONFIG


def test_load_config_tolerates_an_empty_file(tmp_state):
    (tmp_state / "config.json").write_text("", encoding="utf-8")
    assert paths.load_config()["default_station"] == paths.DEFAULT_CONFIG["default_station"]


def test_load_config_rejects_malformed_json_with_the_filename(tmp_state):
    (tmp_state / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        paths.load_config()
    assert "config.json" in str(error.value)
    assert "not valid JSON" in str(error.value)


def test_load_config_rejects_a_json_array(tmp_state):
    (tmp_state / "config.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        paths.load_config()


def test_write_config_round_trips(tmp_state):
    written = paths.write_config({"bot_token": "secret", "guilds": {"1": {"volume": 30}}})
    assert Path(written).is_file()
    loaded = json.loads(Path(written).read_text(encoding="utf-8"))
    assert loaded["bot_token"] == "secret"
    assert loaded["guilds"]["1"]["volume"] == 30


def test_write_config_is_private(tmp_state):
    if os.name == "nt":
        pytest.skip("POSIX permission bits only")
    written = paths.write_config({"bot_token": "secret"})
    mode = stat.S_IMODE(Path(written).stat().st_mode)
    assert mode & stat.S_IRWXG == 0
    assert mode & stat.S_IRWXO == 0


def test_write_config_leaves_no_temp_files_behind(tmp_state):
    paths.write_config({"a": 1})
    assert list(tmp_state.glob(".config-*")) == []


def test_config_candidates_prefers_lofi_config(tmp_state, monkeypatch):
    explicit = tmp_state / "custom.json"
    explicit.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LOFI_CONFIG", str(explicit))
    assert paths.config_candidates()[0] == explicit
    assert paths.config_path() == explicit


def test_config_write_path_follows_lofi_config(tmp_state, monkeypatch):
    explicit = tmp_state / "custom.json"
    monkeypatch.setenv("LOFI_CONFIG", str(explicit))
    assert paths.config_write_path() == explicit


def test_config_write_path_is_the_data_dir_by_default(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_CONFIG", raising=False)
    assert paths.config_write_path() == paths.DATA_DIR / "config.json"


def test_the_committed_template_is_a_read_candidate():
    """config.json is gitignored because a bot token ends up in it; the template
    is what a fresh clone reads until somebody copies it."""
    assert any(candidate.name == "config.example.json" for candidate in paths.config_candidates())


def test_the_template_is_never_a_write_target(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_CONFIG", raising=False)
    assert paths.config_write_path().name == "config.json"
    assert "example" not in paths.config_write_path().name


def test_a_real_config_wins_over_the_template(tmp_state, monkeypatch):
    (tmp_state / "config.json").write_text(json.dumps({"default_volume": 11}), encoding="utf-8")
    assert paths.config_path().name == "config.json"
    assert paths.load_config()["default_volume"] == 11


# --------------------------------------------------------------------------- #
# libopus discovery
#
# Shared libraries do not live on PATH: a correctly installed libopus on Debian
# is /usr/lib/x86_64-linux-gnu/libopus.so.0. Searching with shutil.which() -
# which is what this used to do - reports "not found" on every working Linux
# install, and CI caught it on the first run against a real runner.
# --------------------------------------------------------------------------- #
LINUX = sys.platform.startswith("linux")


def _a_real_library() -> Optional[Path]:
    """Something on this machine that can actually be dlopened.

    Stands in for libopus, which the test environment does not have. libc is
    already loaded in this process, so opening it again is a refcount bump
    rather than a real load.
    """
    for directory in (
        "/usr/lib/x86_64-linux-gnu",
        "/lib/x86_64-linux-gnu",
        "/usr/lib/aarch64-linux-gnu",
        "/lib/aarch64-linux-gnu",
        "/usr/lib64",
        "/lib64",
        "/usr/lib",
        "/lib",
    ):
        for name in ("libc.so.6", "libc.so"):
            candidate = Path(directory) / name
            if candidate.is_file():
                return candidate
    return None


@pytest.fixture
def opus_not_loaded(monkeypatch):
    """The normal state at --check time: nothing has loaded opus yet."""
    import discord.opus

    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)
    monkeypatch.delenv("LOFI_OPUS", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    return discord.opus


@pytest.fixture
def fake_libopus(tmp_path, monkeypatch, opus_not_loaded) -> Path:
    """A loadable file named the way libopus is named, in a directory that is
    not on PATH - the exact shape of a real libopus0 install."""
    if not LINUX:
        pytest.skip("the multiarch library layout is a Linux thing")
    real = _a_real_library()
    if real is None:
        pytest.skip("no loadable system library to stand in for libopus")
    directory = tmp_path / "x86_64-linux-gnu"
    directory.mkdir()
    target = directory / "libopus.so.0"
    try:
        target.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - odd filesystems
        target.write_bytes(real.read_bytes())
    monkeypatch.setenv("LD_LIBRARY_PATH", str(directory))
    return target


@pytest.fixture
def no_other_libopus(monkeypatch):
    """Pretend nothing else on this machine can satisfy the search.

    CI installs ``libopus0`` so that the ``--check`` step has something to find,
    which means on a runner a system directory holds a real library *and* the
    last resort (``ctypes.util.find_library``) resolves. A test about rejection
    would pass on a laptop with no libopus and fail there, for reasons that have
    nothing to do with the code under test.
    """
    import ctypes.util

    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)


def test_opus_is_found_in_a_library_directory_that_is_not_on_path(fake_libopus):
    """The regression: `which libopus.so.0` finds nothing on a working install."""
    assert shutil.which("libopus.so.0") is None  # the search that used to be the only one
    assert paths.opus_library() == str(fake_libopus)


def test_the_scan_covers_the_multiarch_directories(monkeypatch, opus_not_loaded):
    """Debian, Ubuntu and Fedora all put it somewhere else again."""
    if LINUX:
        found = {str(directory) for directory in paths._library_dirs()}
        assert "/usr/lib" in found and "/lib" in found
        multiarch = [item for item in found if "linux-gnu" in item]
        if Path("/usr/lib/x86_64-linux-gnu").is_dir() or Path("/usr/lib/aarch64-linux-gnu").is_dir():
            assert multiarch, "the multiarch triple directories were not scanned"


def test_ld_library_path_is_searched_first(monkeypatch, opus_not_loaded):
    """So a custom build can shadow a system one."""
    directories = paths._library_dirs()
    assert directories, "no directories to search at all"
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/custom/lib")
    assert str(paths._library_dirs()[0]) == "/opt/custom/lib"


def test_a_file_with_the_right_name_that_cannot_be_loaded_is_rejected(
    tmp_path, monkeypatch, opus_not_loaded, no_other_libopus
):
    """A wrong-architecture library or a dangling symlink must not be reported
    as a working install: --check should never say "fine" and then have
    discord.py fail at connect time."""
    if not LINUX:
        pytest.skip("the multiarch library layout is a Linux thing")
    directory = tmp_path / "x86_64-linux-gnu"
    directory.mkdir()
    (directory / "libopus.so.0").write_text("this is not an ELF object")
    # Only the directory holding the bad file: the multiarch layout itself is
    # what test_the_scan_covers_the_multiarch_directories pins down.
    monkeypatch.setattr(paths, "_library_dirs", lambda: [directory])
    assert paths.opus_library() is None


def test_lofi_opus_points_straight_at_the_library(fake_libopus, monkeypatch):
    """The escape hatch for an unusual prefix, mirroring LOFI_FFMPEG."""
    monkeypatch.setenv("LOFI_OPUS", str(fake_libopus))
    assert paths.opus_library() == str(fake_libopus)


def test_a_lofi_opus_that_cannot_be_opened_is_not_pretended_to_work(
    tmp_path, monkeypatch, opus_not_loaded
):
    broken = tmp_path / "libopus.so.0"
    broken.write_text("nope")
    monkeypatch.setenv("LOFI_OPUS", str(broken))
    assert paths.opus_library() is None


def test_nothing_is_reported_when_there_is_nothing_to_find(
    monkeypatch, opus_not_loaded, no_other_libopus
):
    monkeypatch.setattr(paths, "_library_dirs", lambda: [])
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    monkeypatch.setenv("LD_LIBRARY_PATH", "")
    assert paths.opus_library() is None


def test_an_already_loaded_library_short_circuits_the_search(monkeypatch, fake_libopus):
    import discord.opus

    monkeypatch.setattr(discord.opus, "is_loaded", lambda: True)
    assert paths.opus_library() == "loaded"


def test_music_dir_is_none_when_there_is_no_folder(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_MUSIC", raising=False)
    monkeypatch.setattr(paths, "source_dir", lambda: tmp_state / "nowhere")
    assert paths.music_dir() is None


def test_music_dir_honours_the_override(tmp_state, monkeypatch):
    folder = tmp_state / "tapes"
    folder.mkdir()
    monkeypatch.setenv("LOFI_MUSIC", str(folder))
    assert paths.music_dir() == folder


def test_music_dir_override_pointing_nowhere_is_none(tmp_state, monkeypatch):
    monkeypatch.setenv("LOFI_MUSIC", str(tmp_state / "missing"))
    assert paths.music_dir() is None


def test_cache_dir_is_created(tmp_state):
    created = paths.cache_dir()
    assert created.is_dir()
    assert created == tmp_state / "cache"


def _realistic_which(name: str):
    """A ``shutil.which`` stand-in that behaves like the real one for paths.

    The production code passes a full path to ``which`` when ``LOFI_FFMPEG``
    names something that does not exist, and the real ``which`` returns ``None``
    for that - a stub that invents a result would hide the fallback.
    """
    if os.path.sep in str(name):
        return str(name) if Path(name).is_file() and os.access(name, os.X_OK) else None
    return f"/usr/bin/{name}"


def test_ffmpeg_prefers_the_environment_override(tmp_state, monkeypatch):
    fake = tmp_state / "ffmpeg"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("LOFI_FFMPEG", str(fake))
    paths._FFMPEG_CACHE.clear()
    assert paths.ffmpeg_executable() == str(fake)


def test_ffmpeg_falls_back_to_path_lookup(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_FFMPEG", raising=False)
    paths._FFMPEG_CACHE.clear()
    monkeypatch.setattr(paths.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert paths.ffmpeg_executable() == "/usr/bin/ffmpeg"


def test_ffmpeg_warns_about_a_broken_override(tmp_state, monkeypatch, caplog):
    monkeypatch.setenv("LOFI_FFMPEG", str(tmp_state / "does-not-exist"))
    paths._FFMPEG_CACHE.clear()
    monkeypatch.setattr(paths.shutil, "which", _realistic_which)
    with caplog.at_level("WARNING"):
        assert paths.ffmpeg_executable() == "/usr/bin/ffmpeg"
    assert "does not exist" in caplog.text


def test_ffmpeg_result_is_cached(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_FFMPEG", raising=False)
    paths._FFMPEG_CACHE.clear()
    calls = []

    def counting_which(name):
        calls.append(name)
        return _realistic_which(name)

    monkeypatch.setattr(paths.shutil, "which", counting_which)
    paths.ffmpeg_executable()
    paths.ffmpeg_executable()
    assert calls == ["ffmpeg"]


def test_ffprobe_is_resolved_separately(tmp_state, monkeypatch):
    paths._FFMPEG_CACHE.clear()
    monkeypatch.setattr(paths.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert paths.ffmpeg_executable("ffprobe") == "/usr/bin/ffprobe"


def test_ffmpeg_returns_none_when_nothing_is_found(monkeypatch):
    monkeypatch.delenv("LOFI_FFMPEG", raising=False)
    paths._FFMPEG_CACHE.clear()
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    monkeypatch.setattr(paths, "_bundled_ffmpeg", lambda: None)
    monkeypatch.setattr(paths, "_common_ffmpeg_locations", lambda: [])
    assert paths.ffmpeg_executable() is None


def test_bundled_ffmpeg_is_used_when_path_lookup_fails(monkeypatch):
    monkeypatch.delenv("LOFI_FFMPEG", raising=False)
    paths._FFMPEG_CACHE.clear()
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    monkeypatch.setattr(paths, "_bundled_ffmpeg", lambda: "/opt/pkg/ffmpeg")
    assert paths.ffmpeg_executable() == "/opt/pkg/ffmpeg"


def test_app_version_reads_the_version_file():
    assert paths.app_version() == Path(paths.resource_path("VERSION")).read_text().strip()


def test_resource_path_finds_the_dashboard():
    found = paths.resource_path("dashboard.html")
    assert found is not None and found.is_file()
    assert "<title>Lofi" in found.read_text(encoding="utf-8")


def test_resource_path_returns_none_when_absent():
    assert paths.resource_path("no-such-file.html") is None


def test_audio_extensions_cover_the_common_formats():
    for extension in (".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a"):
        assert extension in paths.AUDIO_EXTENSIONS


def test_data_dir_honours_lofi_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOFI_HOME", str(tmp_path / "elsewhere"))
    assert paths.data_dir() == tmp_path / "elsewhere"


def test_platform_state_dir_is_used_without_an_override(monkeypatch, tmp_path):
    monkeypatch.delenv("LOFI_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    chosen = paths.data_dir()
    assert str(chosen).startswith(str(tmp_path / "state"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only directory rules")
def test_tighten_private_dir_removes_group_and_other_bits(tmp_path):
    target = tmp_path / "private"
    target.mkdir(mode=0o755)
    paths._tighten_private_dir(target)
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & stat.S_IRWXG == 0
    assert mode & stat.S_IRWXO == 0


def test_tighten_private_dir_survives_a_missing_directory(tmp_path):
    paths._tighten_private_dir(tmp_path / "does-not-exist")  # must not raise
