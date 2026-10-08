"""Startup, the install check and the command line.

The intents assertion is the important one: asking for a privileged intent makes
the bot fail to log in for anyone who has not ticked boxes in the developer
portal, and the failure arrives as an exception at connect time rather than at
install time.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

import discord
import pytest

import bot as bot_module
import paths
import store
from conftest import FakeChannel, FakeGuild, FakeMember, FakePermissions


# --------------------------------------------------------------------------- #
# Intents
# --------------------------------------------------------------------------- #
def test_the_bot_requests_no_privileged_intents(tmp_state):
    """Members, Presences and Message Content all need enabling in the portal."""
    instance = bot_module.LofiBot(start_dashboard=False)
    intents = instance.intents
    assert intents.members is False
    assert intents.presences is False
    assert intents.message_content is False


def test_the_bot_requests_what_playback_needs(tmp_state):
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance.intents.voice_states is True  # notices being moved or kicked
    assert instance.intents.guilds is True        # resolves channels and members


def test_the_intents_are_the_library_default_plus_voice(tmp_state):
    """Nothing exotic: default() and an explicit voice_states flag."""
    default = discord.Intents.default()
    used = bot_module.LofiBot(start_dashboard=False).intents
    assert used.value == default.value


def test_no_prefix_commands_are_registered(tmp_state):
    """The whole surface is slash commands; a prefix would need message content."""
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance.help_command is None
    assert instance.command_prefix is not None


# --------------------------------------------------------------------------- #
# The bot object
# --------------------------------------------------------------------------- #
def test_a_new_bot_starts_with_the_default_config(tmp_state):
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance.config["default_station"] == paths.DEFAULT_CONFIG["default_station"]
    assert instance.players is not None
    assert instance.dashboard is None


def test_a_supplied_config_is_merged_over_the_defaults(tmp_state):
    instance = bot_module.LofiBot({"default_volume": 22}, start_dashboard=False)
    assert instance.config["default_volume"] == 22
    assert instance.config["dashboard_port"] == paths.DEFAULT_CONFIG["dashboard_port"]


def test_save_config_writes_and_adopts(tmp_state):
    instance = bot_module.LofiBot(start_dashboard=False)
    instance.save_config(dict(instance.config, default_volume=44))
    assert instance.config["default_volume"] == 44
    assert paths.load_config()["default_volume"] == 44


def test_apply_config_survives_a_read_only_disk(tmp_state, monkeypatch):
    """A settings change should still take effect for this run."""
    instance = bot_module.LofiBot(start_dashboard=False)

    def refuse(updated):
        raise OSError("read-only file system")

    monkeypatch.setattr(paths, "write_config", refuse)
    instance.apply_config(dict(instance.config, default_volume=55))
    assert instance.config["default_volume"] == 55


def test_the_dashboard_token_is_generated_once_and_reused(tmp_state):
    instance = bot_module.LofiBot(start_dashboard=False)
    first = instance.dashboard_token()
    assert len(first) >= 32
    assert instance.dashboard_token() == first


def test_uptime_starts_at_construction(tmp_state):
    instance = bot_module.LofiBot(start_dashboard=False)
    assert 0 <= instance.uptime_seconds < 60  # a property, not a method


def test_the_version_matches_the_version_file():
    assert bot_module.VERSION == paths.app_version()


# --------------------------------------------------------------------------- #
# The install check
# --------------------------------------------------------------------------- #
def test_the_check_covers_everything_audio_needs():
    labels = {row[0] for row in bot_module._check_rows()}
    for expected in ("Python", "discord.py", "PyNaCl", "ffmpeg", "libopus", "bot token", "data directory"):
        assert expected in labels, expected


def test_check_rows_have_a_state_and_fixes():
    for label, state, detail, fixes in bot_module._check_rows():
        assert state in {"ok", "missing", "warn", "optional"}, (label, state)
        assert isinstance(detail, str)
        assert isinstance(fixes, list)


def test_a_missing_row_comes_with_a_fix():
    """The point of the report is the fix, not the diagnosis."""
    for label, state, detail, fixes in bot_module._check_rows():
        if state == "missing":
            assert fixes, label


def test_missing_pynacl_has_platform_appropriate_fixes(monkeypatch):
    monkeypatch.setattr(bot_module, "_pynacl_version", lambda: None)
    row = next(row for row in bot_module._check_rows() if row[0] == "PyNaCl")
    assert row[1] == "missing"
    assert "will not be supported" in row[2]
    assert any("requirements.txt" in fix for fix in row[3])
    assert any("requirements-winarm.txt" in fix for fix in row[3])


def test_ffmpeg_fixes_name_the_install_command(monkeypatch):
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": None)
    row = next(row for row in bot_module._check_rows() if row[0] == "ffmpeg")
    assert row[1] == "missing"
    assert any("apt install ffmpeg" in fix for fix in row[3])
    assert any("imageio-ffmpeg" in fix for fix in row[3])


def test_a_found_ffmpeg_is_reported_with_its_path(monkeypatch):
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/opt/bin/ffmpeg")
    row = next(row for row in bot_module._check_rows() if row[0] == "ffmpeg")
    assert row[1] == "ok"
    assert row[2] == "/opt/bin/ffmpeg"


def test_a_missing_token_is_reported_but_does_not_break_the_check(monkeypatch):
    monkeypatch.delenv("LOFI_TOKEN", raising=False)
    row = next(row for row in bot_module._check_rows() if row[0] == "bot token")
    assert row[1] == "missing"
    assert any("developer portal" in fix or "config.json" in fix for fix in row[3])


def test_a_token_in_the_environment_counts(monkeypatch):
    monkeypatch.setenv("LOFI_TOKEN", "a-token-from-the-environment")
    row = next(row for row in bot_module._check_rows() if row[0] == "bot token")
    assert row[1] == "ok"
    assert "a-token" not in row[2]  # never echo a credential into a report


def test_run_check_prints_a_report_and_succeeds(tmp_state, monkeypatch, capsys):
    monkeypatch.setenv("LOFI_TOKEN", "a-token-from-the-environment")
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(paths, "opus_library", lambda: "/usr/lib/libopus.so")
    code = bot_module.run_check()
    output = capsys.readouterr().out
    assert code == 0
    assert "Lofi install check" in output
    assert "✓" in output
    assert "Everything needed for audio is in place" in output


def test_run_check_fails_when_audio_cannot_work(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": None)
    monkeypatch.setattr(paths, "opus_library", lambda: None)
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)
    code = bot_module.run_check()
    output = capsys.readouterr().out
    assert code == 1
    assert "✗" in output
    assert "Audio will not work" in output


def test_run_check_fails_when_pynacl_is_missing(tmp_state, monkeypatch, capsys):
    monkeypatch.setenv("LOFI_TOKEN", "a-token-from-the-environment")
    monkeypatch.setattr(bot_module, "_pynacl_version", lambda: None)
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(paths, "opus_library", lambda: "/usr/lib/libopus.so")
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)

    code = bot_module.run_check()
    output = capsys.readouterr().out
    assert code == 1
    assert "PyNaCl" in output
    assert "Discord voice will not be supported" in output
    assert "requirements.txt" in output


def test_run_check_deduplicates_the_fixes(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": None)
    monkeypatch.setattr(paths, "opus_library", lambda: None)
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)
    bot_module.run_check()
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if "imageio-ffmpeg" in line]
    assert len(lines) == 1


def test_the_check_says_where_the_dashboard_will_listen(tmp_state, monkeypatch, capsys):
    monkeypatch.setenv("LOFI_TOKEN", "a-token-from-the-environment")
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    bot_module.run_check()
    output = capsys.readouterr().out
    assert "dashboard" in output.lower()
    assert "will listen on" in output


def test_the_check_warns_when_the_dashboard_will_be_exposed(tmp_state, monkeypatch):
    """Binding 0.0.0.0 over plain HTTP is the configuration people ship by accident."""
    monkeypatch.setenv("LOFI_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("LOFI_TOKEN", "a-token-from-the-environment")
    row = next(row for row in bot_module._check_rows() if row[0] == "dashboard")
    assert "0.0.0.0" in row[2]
    assert any("HTTPS" in fix for fix in row[3])


def test_the_check_reports_a_disabled_dashboard(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    (tmp_state / "config.json").write_text('{"dashboard_enabled": false}', encoding="utf-8")
    row = next(row for row in bot_module._check_rows() if row[0] == "dashboard")
    assert row[1] == "optional"
    assert "disabled" in row[2]


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def test_the_parser_accepts_every_documented_flag():
    args = bot_module.build_parser().parse_args([
        "--check", "--demo", "--dashboard-token", "--no-dashboard",
        "--dashboard-host", "0.0.0.0", "--dashboard-port", "9000",
        "--dashboard-allowed-hosts", "a.example,b.example",
        "--sync-commands", "--log-level", "DEBUG", "--logfile", "/tmp/x.log", "--version",
    ])
    assert args.check and args.demo and args.version
    assert args.dashboard_host == "0.0.0.0"
    assert args.dashboard_port == 9000
    assert args.logfile == "/tmp/x.log"


def test_the_parser_defaults_to_a_plain_run():
    args = bot_module.build_parser().parse_args([])
    assert not args.check and not args.demo and not args.no_dashboard
    assert args.dashboard_host is None
    assert args.log_level == "INFO"


def test_cli_overrides_beat_the_config(config):
    args = bot_module.build_parser().parse_args([
        "--dashboard-host", "0.0.0.0", "--dashboard-port", "9001",
        "--dashboard-allowed-hosts", "lofi.example.com, other.example.com",
    ])
    updated = bot_module._apply_cli_overrides(config, args)
    assert updated["dashboard_host"] == "0.0.0.0"
    assert updated["dashboard_port"] == 9001
    assert updated["dashboard_allowed_hosts"] == ["lofi.example.com", "other.example.com"]


def test_no_dashboard_disables_the_server(config):
    args = bot_module.build_parser().parse_args(["--no-dashboard"])
    assert bot_module._apply_cli_overrides(config, args)["dashboard_enabled"] is False


def test_the_config_is_untouched_by_the_overrides(config):
    args = bot_module.build_parser().parse_args(["--dashboard-port", "9002"])
    bot_module._apply_cli_overrides(config, args)
    assert config["dashboard_port"] == paths.DEFAULT_CONFIG["dashboard_port"]


def test_version_exits_cleanly(tmp_state, capsys):
    output = io.StringIO()
    with redirect_stdout(output):
        assert bot_module.main(["--version"]) == 0
    assert bot_module.VERSION in output.getvalue()


def test_check_exits_with_the_report_code(tmp_state, monkeypatch):
    monkeypatch.setattr(bot_module, "run_check", lambda: 1)
    assert bot_module.main(["--check"]) == 1


def test_no_token_exits_with_instructions(tmp_state, monkeypatch, capsys):
    monkeypatch.delenv("LOFI_TOKEN", raising=False)
    monkeypatch.setattr(paths, "write_config", lambda updated: paths.config_write_path())
    error = io.StringIO()
    with redirect_stderr(error):
        assert bot_module.main([]) == 2
    text = error.getvalue()
    assert "bot_token" in text
    assert "--demo" in text  # the escape hatch for someone without a token yet
    assert "developers/applications" in text


def test_the_dashboard_token_flag_prints_the_key(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(paths, "write_config", lambda updated: paths.config_write_path())
    output = io.StringIO()
    with redirect_stdout(output):
        assert bot_module.main(["--dashboard-token"]) == 0
    assert len(output.getvalue().strip()) >= 32


def test_a_too_short_stored_token_exits_with_an_explanation(tmp_state, monkeypatch, capsys):
    """A weak key in config.json must stop the bot rather than serve a guessable UI.

    Read from stdout, not caplog: main() calls setup_logging(), which replaces
    the root handlers the caplog fixture installs.
    """
    (tmp_state / "config.json").write_text('{"dashboard_token": "short"}', encoding="utf-8")
    monkeypatch.setattr(paths, "write_config", lambda updated: paths.config_write_path())
    assert bot_module.main([]) == 2
    output = capsys.readouterr().out
    assert "at least 32 characters" in output


# --------------------------------------------------------------------------- #
# First run: the one question
# --------------------------------------------------------------------------- #
class _FakeTerminal(io.StringIO):
    """stdin the way a terminal sees it: a tty with the answer already typed."""

    def isatty(self) -> bool:
        return True


def test_a_first_run_asks_for_the_token_and_saves_it(tmp_state, monkeypatch, capsys):
    monkeypatch.delenv("LOFI_TOKEN", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeTerminal("MTk4NzY1NDMy.MTEyMjMzNDQ1.NjY3ODg5\n"))
    config = dict(paths.DEFAULT_CONFIG)

    token = bot_module._prompt_for_token(config)

    assert token == "MTk4NzY1NDMy.MTEyMjMzNDQ1.NjY3ODg5"
    assert config["bot_token"] == token                       # adopted for this run
    assert paths.load_config()["bot_token"] == token           # and remembered for the next
    assert "Bot token:" in capsys.readouterr().out


def test_the_prompt_says_where_a_token_comes_from(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeTerminal("a.b.c\n"))
    bot_module._prompt_for_token(dict(paths.DEFAULT_CONFIG))
    assert "developers/applications" in capsys.readouterr().out


def test_the_saved_config_is_private(tmp_state, monkeypatch):
    """A token on disk must not be readable by every other user on the box."""
    monkeypatch.setattr(sys, "stdin", _FakeTerminal("a.b.c\n"))
    bot_module._prompt_for_token(dict(paths.DEFAULT_CONFIG))
    if os.name != "nt":
        assert oct(paths.config_write_path().stat().st_mode & 0o777) == "0o600"


def test_an_empty_answer_is_not_an_error(tmp_state, monkeypatch):
    """Pressing Enter falls through to the instructions, not to a crash."""
    monkeypatch.setattr(sys, "stdin", _FakeTerminal("\n"))
    assert bot_module._prompt_for_token(dict(paths.DEFAULT_CONFIG)) == ""
    assert not (tmp_state / "config.json").exists()          # nothing was written


def test_nothing_is_asked_when_nobody_can_answer(tmp_state, monkeypatch):
    """systemd, Docker and pipes have no keyboard: a read there would hang."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("a-token-from-a-pipe\n"))
    assert bot_module._prompt_for_token(dict(paths.DEFAULT_CONFIG)) == ""
    assert not (tmp_state / "config.json").exists()


def test_a_token_that_cannot_be_saved_still_works_for_this_run(tmp_state, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeTerminal("a.b.c\n"))
    monkeypatch.setattr(paths, "write_config", lambda updated: (_ for _ in ()).throw(OSError("read-only")))
    assert bot_module._prompt_for_token(dict(paths.DEFAULT_CONFIG)) == "a.b.c"


def test_a_first_run_goes_from_zero_to_connecting_in_one_step(tmp_state, monkeypatch, capsys):
    """The point of the prompt: no editing a file between installing and running."""
    monkeypatch.delenv("LOFI_TOKEN", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeTerminal("MTk4.NjY1.NDMy\n"))
    built = []

    class FakeBot:
        def __init__(self, config, start_dashboard=True):
            built.append(config["bot_token"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def start(self, token):
            built.append(token)

    monkeypatch.setattr(bot_module, "LofiBot", FakeBot)

    def fake_run(coro):
        coro.close()          # never awaited: there is no Discord to talk to here
        return 0

    monkeypatch.setattr(bot_module.asyncio, "run", fake_run)
    assert bot_module.main([]) == 0
    assert built == ["MTk4.NjY1.NDMy"]


# --------------------------------------------------------------------------- #
# Logging and helpers
# --------------------------------------------------------------------------- #
def test_setup_logging_writes_to_a_file(tmp_state):
    target = tmp_state / "bot.log"
    bot_module.setup_logging("DEBUG", str(target))
    logging.getLogger("lofi.test").info("hello from the log")
    for handler in list(logging.getLogger().handlers):
        handler.flush()
    assert "hello from the log" in target.read_text(encoding="utf-8")


def test_setup_logging_survives_an_unwritable_path(tmp_state):
    bot_module.setup_logging("INFO", "/proc/definitely/not/writable.log")  # must not raise


def test_the_first_speakable_channel_skips_ones_the_bot_cannot_post_in():
    bot_member = FakeMember(2, "Lofi", bot=True, permissions=FakePermissions(send_messages=True))
    denied = FakeChannel(11, "staff", kind="text")
    denied.set_permissions_for(2, FakePermissions(send_messages=False))
    allowed = FakeChannel(12, "music", kind="text")
    allowed.set_permissions_for(2, FakePermissions(send_messages=True))
    guild = FakeGuild(5, channels=[denied, allowed], me=bot_member)
    bot_member.guild = guild
    assert bot_module._first_speakable_channel(guild).id == 12


def test_the_first_speakable_channel_is_none_when_there_is_nowhere_to_post():
    bot_member = FakeMember(2, "Lofi", bot=True)
    denied = FakeChannel(11, "staff", kind="text")
    denied.set_permissions_for(2, FakePermissions(send_messages=False))
    guild = FakeGuild(5, channels=[denied], me=bot_member)
    bot_member.guild = guild
    assert bot_module._first_speakable_channel(guild) is None


def test_the_database_is_initialised_and_dangling_sessions_closed(tmp_state):
    """What setup_hook does before connecting: a crashed run must not look busy."""
    store.init_db()
    store.start_session(400000000000000001, station_id="library", station_name="My Library",
                        track_title="Ghost", channel_id=None, requested_by=None)
    assert store.close_dangling_sessions() == 1


def test_probe_voice_reports_the_audio_dependencies(tmp_state, monkeypatch):
    """A bot that cannot play audio should say so before it joins a channel."""
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": None)
    monkeypatch.setattr(paths, "opus_library", lambda: None)
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance._probe_voice() is False
    assert instance.voice_ready is False


def test_probe_voice_is_true_when_all_audio_dependencies_are_present(tmp_state, monkeypatch):
    monkeypatch.setattr(bot_module, "_pynacl_version", lambda: "1.5.0")
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: True)
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance._probe_voice() is True


def test_probe_voice_requires_pynacl_even_with_opus_and_ffmpeg(tmp_state, monkeypatch):
    monkeypatch.setattr(bot_module, "_pynacl_version", lambda: None)
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: True)
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance._probe_voice() is False


def test_probe_voice_loads_opus_from_a_known_path(tmp_state, monkeypatch):
    """Some installs ship libopus somewhere the loader does not look by default."""
    loaded = []
    monkeypatch.setattr(bot_module, "_pynacl_version", lambda: "1.5.0")
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: False)
    monkeypatch.setattr(paths, "opus_library", lambda: "/usr/lib/libopus.so")
    monkeypatch.setattr(discord.opus, "load_opus", lambda path: loaded.append(path))
    instance = bot_module.LofiBot(start_dashboard=False)
    assert instance._probe_voice() is True
    assert loaded == ["/usr/lib/libopus.so"]


def test_probe_voice_is_false_when_ffmpeg_is_missing_even_with_opus(tmp_state, monkeypatch):
    monkeypatch.setattr(paths, "ffmpeg_executable", lambda name="ffmpeg": None)
    monkeypatch.setattr(discord.opus, "is_loaded", lambda: True)
    assert bot_module.LofiBot(start_dashboard=False)._probe_voice() is False


def test_a_module_version_is_reported_when_installed():
    assert bot_module._module_version("discord")


def test_a_module_version_is_none_when_missing():
    assert bot_module._module_version("definitely_not_installed_xyz") is None


def test_writable_detects_a_directory_that_can_be_written(tmp_state):
    assert bot_module._writable(tmp_state) is True


def test_writable_creates_missing_parent_directories(tmp_state):
    """The data directory may not exist yet on a first run."""
    target = tmp_state / "does" / "not" / "exist" / "config.json"
    assert bot_module._writable(target) is True
    assert (tmp_state / "does" / "not" / "exist").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only path")
def test_writable_detects_a_path_that_cannot_be_written(tmp_state):
    assert bot_module._writable("/proc/1/cannot/write/here.json") is False
