"""The permission audit, including the permissions that look useful and are not."""

from __future__ import annotations

import pytest

import permissions
from conftest import FakeChannel, FakeGuild, FakeMember, FakePermissions


def bot_member(**flags) -> FakeMember:
    """The bot's own member object, with the flags under test set.

    ``guild`` is wired up because the guild-wide fallback reads ``guild.me``;
    a fake without one would report every permission as missing and the tests
    would pass for the wrong reason.
    """
    member = FakeMember(1, "Lofi", bot=True, permissions=FakePermissions(**flags))
    member.guild = FakeGuild(5, me=member)
    return member


def channel_with(member: FakeMember, **flags) -> FakeChannel:
    """A voice channel with a per-channel override for the bot."""
    channel = FakeChannel(10, "study-room")
    channel.set_permissions_for(member.id, FakePermissions(**flags))
    return channel


def test_every_requirement_has_a_reason():
    """The audit is only useful if it says what a permission is for."""
    for item in permissions.REQUIREMENTS:
        assert item.key and item.label and item.reason
        assert isinstance(item.required, bool)


def test_the_required_set_is_exactly_what_audio_needs():
    assert {item.key for item in permissions.REQUIREMENTS if item.required} == {
        "view_channel",
        "connect",
        "speak",
    }


def test_speak_is_required_because_a_silent_bot_looks_broken():
    speak = next(item for item in permissions.REQUIREMENTS if item.key == "speak")
    assert speak.required is True
    assert "silent" in speak.reason


def test_priority_speaker_is_listed_but_not_required():
    """Granting it to a bot does nothing: it is a desktop push-to-talk keybind."""
    item = next(item for item in permissions.REQUIREMENTS if item.key == "priority_speaker")
    assert item.required is False
    assert "no effect" in item.reason


def test_set_voice_channel_status_is_optional_and_cosmetic():
    item = next(item for item in permissions.REQUIREMENTS if item.key == "set_voice_channel_status")
    assert item.required is False
    assert "track" in item.reason


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def test_check_reads_the_channel_override_when_a_channel_is_known():
    """A role can have Speak everywhere and still be denied it in one channel."""
    member = bot_member(speak=True, connect=True, view_channel=True)
    channel = channel_with(member, speak=False, connect=True, view_channel=True)
    assert permissions.check(member, "speak", channel) is False
    assert permissions.check(member, "connect", channel) is True


def test_check_falls_back_to_guild_permissions_without_a_channel():
    member = bot_member(speak=True)
    guild = FakeGuild(5, me=member)
    member.guild = guild
    assert permissions.check(member, "speak") is True
    assert permissions.check(member, "priority_speaker") is False


def test_check_uses_guild_me_rather_than_the_passed_member():
    """``guild.me`` carries the bot's real roles; a caller may pass anything."""
    real = bot_member(speak=False)
    impostor = bot_member(speak=True)
    impostor.guild = FakeGuild(5, me=real)
    assert permissions.check(impostor, "speak") is False


def test_check_survives_a_channel_that_cannot_answer():
    class BrokenChannel:
        def permissions_for(self, member):
            raise RuntimeError("partial channel")

    member = bot_member(speak=True)
    member.guild = FakeGuild(5, me=member)
    assert permissions.check(member, "speak", BrokenChannel()) is True


def test_check_returns_false_when_there_is_nothing_to_read():
    member = FakeMember(1, "Lofi")
    member.guild = None
    member.guild_permissions = None
    assert permissions.check(member, "speak") is False


def test_check_of_an_unknown_permission_is_false():
    assert permissions.check(bot_member(), "launch_missiles") is False


# --------------------------------------------------------------------------- #
# missing / can_play
# --------------------------------------------------------------------------- #
def test_nothing_is_missing_when_the_bot_can_play():
    member = bot_member(view_channel=True, connect=True, speak=True)
    assert permissions.missing(member) == []
    assert permissions.can_play(member) is None


def test_missing_speak_is_reported():
    member = bot_member(view_channel=True, connect=True, speak=False)
    assert [item.key for item in permissions.missing(member)] == ["speak"]


def test_can_play_names_the_permission_and_the_fix():
    member = bot_member(view_channel=True, connect=True, speak=False)
    message = permissions.can_play(member)
    assert "Speak" in message
    assert "role" in message


def test_can_play_names_every_missing_permission():
    member = bot_member(view_channel=False, connect=False, speak=False)
    message = permissions.can_play(member)
    for label in ("View Channel", "Connect", "Speak"):
        assert label in message


def test_can_play_mentions_the_channel_it_checked():
    member = bot_member(view_channel=True, connect=True, speak=True)
    channel = channel_with(member, speak=False)
    message = permissions.can_play(member, channel)
    assert "#study-room" in message


def test_can_play_says_server_when_no_channel_is_known():
    member = bot_member(speak=False)
    assert "in that server" in permissions.can_play(member)


def test_can_play_handles_a_bot_that_is_not_in_the_server():
    assert permissions.can_play(None) == "The bot is not in that server yet."


def test_optional_permissions_never_block_playback():
    """No Status, no Priority Speaker, no Manage Channels: still plays."""
    member = bot_member(
        view_channel=True, connect=True, speak=True,
        set_voice_channel_status=False, priority_speaker=False, manage_channels=False,
    )
    assert permissions.can_play(member) is None


def test_missing_only_lists_required_permissions():
    member = bot_member(view_channel=True, connect=True, speak=True, set_voice_channel_status=False)
    assert permissions.missing(member) == []


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def test_the_audit_covers_every_requirement():
    assert len(permissions.audit(bot_member())) == len(permissions.REQUIREMENTS)


def test_audit_rows_are_display_ready():
    row = permissions.audit(bot_member())[0]
    assert set(row) == {"key", "label", "required", "granted", "reason", "state"}


def test_a_granted_required_permission_is_ok():
    member = bot_member(view_channel=True, connect=True, speak=True)
    states = {row["key"]: row["state"] for row in permissions.audit(member)}
    assert states["speak"] == "ok"
    assert states["connect"] == "ok"


def test_a_missing_required_permission_is_missing():
    member = bot_member(speak=False)
    states = {row["key"]: row["state"] for row in permissions.audit(member)}
    assert states["speak"] == "missing"


def test_a_missing_optional_permission_is_optional_not_missing():
    member = bot_member(view_channel=True, connect=True, speak=True, manage_channels=False)
    states = {row["key"]: row["state"] for row in permissions.audit(member)}
    assert states["manage_channels"] == "optional"


def test_granted_priority_speaker_is_reported_as_having_no_effect():
    """The one permission people grant expecting ducking. Saying "ok" here would
    leave them believing it works; saying "missing" would tell them to grant it."""
    member = bot_member(view_channel=True, connect=True, speak=True, priority_speaker=True)
    row = next(item for item in permissions.audit(member) if item["key"] == "priority_speaker")
    assert row["granted"] is True
    assert row["state"] == "no-effect"


def test_ungranted_priority_speaker_is_not_flagged_as_a_problem():
    member = bot_member(view_channel=True, connect=True, speak=True, priority_speaker=False)
    row = next(item for item in permissions.audit(member) if item["key"] == "priority_speaker")
    assert row["state"] == "optional"


def test_granted_status_permission_is_ok():
    member = bot_member(view_channel=True, connect=True, speak=True, set_voice_channel_status=True)
    row = next(item for item in permissions.audit(member) if item["key"] == "set_voice_channel_status")
    assert row["state"] == "ok"


def test_the_audit_reflects_a_channel_override():
    member = bot_member(view_channel=True, connect=True, speak=True)
    channel = channel_with(member, speak=False)
    row = next(item for item in permissions.audit(member, channel) if item["key"] == "speak")
    assert row["state"] == "missing"


# --------------------------------------------------------------------------- #
# summarise
# --------------------------------------------------------------------------- #
def test_the_summary_shows_ticks_and_crosses():
    member = bot_member(view_channel=True, connect=True, speak=False)
    summary = permissions.summarise(member)
    assert "View Channel ✓" in summary
    assert "Speak ✗" in summary
    assert "·" in summary


def test_the_summary_covers_the_permissions_that_matter():
    summary = permissions.summarise(bot_member())
    for label in ("View Channel", "Connect", "Speak"):
        assert label in summary


def test_the_summary_is_short_enough_for_one_line():
    assert len(permissions.summarise(bot_member())) < 120
