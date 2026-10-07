"""Config/data path resolution, and finding the binaries audio depends on."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

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
