"""Who may drive playback, and what the informational embeds say.

The guards are pure functions of a member and a config, which is why they are
worth testing exhaustively: they are the difference between "anyone can stop the
music" and "a stranger in another channel can".
"""

from __future__ import annotations

import pytest

import music
import settings as settings_module
import stations
from conftest import FakeChannel, FakeGuild, FakeMember, FakePermissions, FakeRole


GUILD_ID = 400000000000000001
VOICE_ID = 500000000000000001
OTHER_VOICE_ID = 500000000000000002
DJ_ROLE_ID = 800000000000000001


@pytest.fixture
def guild_config(config):
    config["guilds"] = {str(GUILD_ID): {}}
    return config


def member(*, roles=None, **flags) -> FakeMember:
    """A plain member with the given guild permissions (and optional roles)."""
    return FakeMember(10, "someone", roles=roles or [], permissions=FakePermissions(**flags))


def in_voice(member: FakeMember, channel_id) -> FakeMember:
    member.voice.channel = FakeChannel(channel_id, "study-room", kind="voice")
    return member


def control(member, config, bot_channel_id=None):
    return music.can_control(member, config, GUILD_ID, bot_channel_id)


# --------------------------------------------------------------------------- #
# can_control
# --------------------------------------------------------------------------- #
def test_an_administrator_always_may_control(guild_config):
    assert control(member(administrator=True), guild_config) is None


def test_manage_server_may_control(guild_config):
    assert control(member(manage_guild=True), guild_config) is None


def test_manage_server_may_control_from_another_channel(guild_config):
    """An operator moderating from a text channel should not have to join voice."""
    assert control(member(manage_guild=True), guild_config, VOICE_ID) is None


def test_a_plain_member_in_a_voice_channel_may_start_playback(guild_config):
    assert control(in_voice(member(), VOICE_ID), guild_config, None) is None


def test_a_plain_member_not_in_voice_may_not_start_playback(guild_config):
    message = control(member(), guild_config, None)
    assert message is not None
    assert "Join a voice channel" in message


def test_a_plain_member_in_the_bot_s_channel_may_control_it(guild_config):
    assert control(in_voice(member(), VOICE_ID), guild_config, VOICE_ID) is None


def test_a_plain_member_in_a_different_channel_may_not(guild_config):
    """Otherwise anyone in the server can skip or stop someone else's music."""
    message = control(in_voice(member(), OTHER_VOICE_ID), guild_config, VOICE_ID)
    assert message is not None
    assert "same voice channel" in message
    assert "dj_role" in message


def test_a_plain_member_out_of_voice_may_not_control_a_playing_bot(guild_config):
    assert control(member(), guild_config, VOICE_ID) is not None


def test_the_dj_role_may_control_from_anywhere(guild_config):
    guild_config["guilds"][str(GUILD_ID)]["dj_role_id"] = DJ_ROLE_ID
    dj = member(roles=[FakeRole(DJ_ROLE_ID, "DJ")])
    assert control(dj, guild_config, VOICE_ID) is None
    assert control(dj, guild_config, None) is None


def test_the_dj_role_may_control_even_when_not_in_voice(guild_config):
    guild_config["guilds"][str(GUILD_ID)]["dj_role_id"] = DJ_ROLE_ID
    assert control(member(roles=[FakeRole(DJ_ROLE_ID, "DJ")]), guild_config, VOICE_ID) is None


def test_without_the_dj_role_the_message_names_it(guild_config):
    guild_config["guilds"][str(GUILD_ID)]["dj_role_id"] = DJ_ROLE_ID
    dj_role = FakeRole(DJ_ROLE_ID, "Late Night DJs")
    guild = FakeGuild(GUILD_ID, roles=[dj_role])
    stranger = member(roles=[FakeRole(999, "everyone")])
    stranger.guild = guild
    message = control(stranger, guild_config, VOICE_ID)
    assert "Late Night DJs" in message


def test_a_dj_role_the_cache_has_lost_still_says_something_sane(guild_config):
    guild_config["guilds"][str(GUILD_ID)]["dj_role_id"] = DJ_ROLE_ID
    message = control(member(), guild_config, VOICE_ID)
    assert "the DJ role" in message


def test_no_member_at_all_is_refused(guild_config):
    """A DM has no guild permissions object to read."""
    assert control(None, guild_config) == "That command has to be used inside a server."


def test_a_member_with_no_permissions_object_is_refused(guild_config):
    broken = FakeMember(11, "broken")
    broken.guild_permissions = None
    assert control(broken, guild_config, VOICE_ID) is not None


def test_the_bot_s_channel_id_is_compared_as_an_int(guild_config):
    """Discord ids arrive as strings from the dashboard and as ints from the cache."""
    assert control(in_voice(member(), VOICE_ID), guild_config, str(VOICE_ID)) is None


# --------------------------------------------------------------------------- #
# can_configure
# --------------------------------------------------------------------------- #
def test_configure_needs_manage_server():
    assert music.can_configure(member(manage_guild=True)) is None
    assert music.can_configure(member(administrator=True)) is None


def test_configure_refuses_a_dj(guild_config):
    guild_config["guilds"][str(GUILD_ID)]["dj_role_id"] = DJ_ROLE_ID
    message = music.can_configure(member(roles=[FakeRole(DJ_ROLE_ID, "DJ")]))
    assert message is not None
    assert "Manage Server" in message


def test_configure_refuses_a_plain_member():
    assert "Manage Server" in music.can_configure(member())


def test_configure_refuses_a_dm():
    assert music.can_configure(None) == "That command has to be used inside a server."


# --------------------------------------------------------------------------- #
# The station list
# --------------------------------------------------------------------------- #
def test_the_station_list_names_every_station(guild_config):
    embed = music.station_list_embed(guild_config)
    for station in stations.BUILT_IN_STATIONS:
        assert station.name in embed.description


def test_the_station_list_marks_the_server_s_default(guild_config):
    import stations

    guild_config["guilds"][str(GUILD_ID)]["station_id"] = "groove-salad"
    embed = music.station_list_embed(guild_config, GUILD_ID)
    assert "▶" in embed.description
    marked = [line for line in embed.description.splitlines() if "▶" in line]
    assert "Groove Salad" in marked[0]


def test_the_station_list_explains_how_to_play_one(guild_config):
    embed = music.station_list_embed(guild_config)
    assert "/lofi play" in (embed.footer.text or "")


def test_the_station_list_says_what_kind_each_station_is(guild_config):
    description = music.station_list_embed(guild_config).description
    assert "YouTube" in description
    assert "generated" in description
    assert "radio" in description


def test_the_station_list_is_within_discord_s_embed_limit(guild_config):
    guild_config["stations"] = [
        {"id": f"station-{index}", "name": f"Station {index} with a fairly long name",
         "url": "https://example.com/a.mp3", "kind": "stream"}
        for index in range(50)
    ]
    assert len(music.station_list_embed(guild_config).description) <= 4000


def test_the_station_list_without_a_guild_marks_nothing(guild_config):
    assert "▶" not in music.station_list_embed(guild_config).description


# --------------------------------------------------------------------------- #
# The status embed
# --------------------------------------------------------------------------- #
class FakePlayer:
    """Only the two things ``status_embed`` reads."""

    def __init__(self, state, channel=None):
        self._state = state
        self.channel = channel

    def state(self):
        return self._state


def idle_state(**overrides) -> dict:
    state = {
        "connected": False, "playing": False, "paused": False, "track": None,
        "station": None, "channelName": None, "listeners": 0, "volume": 70,
        "queue": [], "queueLength": 0, "lastError": "", "statusText": "",
        "restartAttempts": 0, "elapsed": 0.0, "duration": None, "progress": None,
    }
    state.update(overrides)
    return state


def playing_state(**overrides) -> dict:
    state = idle_state(
        connected=True, playing=True, channelName="study-room", listeners=3,
        track={"title": "Ambient Set", "stationName": "My Library", "art": "💿",
               "kind": "library", "duration": 180.0, "isLive": False},
        station={"id": "library", "name": "My Library", "kind": "library"},
    )
    state.update(overrides)
    return state


def test_the_status_embed_says_when_nothing_is_playing(guild_config):
    embed = music.status_embed(FakePlayer(idle_state()), config=guild_config, guild=None,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    assert "Not connected" in embed.description


def test_the_status_embed_shows_the_current_track(guild_config):
    embed = music.status_embed(FakePlayer(playing_state()), config=guild_config, guild=None,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    assert "Ambient Set" in embed.description
    assert "3 listener(s)" in embed.description
    assert "paused" not in embed.description


def test_the_status_embed_says_when_paused(guild_config):
    embed = music.status_embed(FakePlayer(playing_state(paused=True)), config=guild_config, guild=None,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    assert "paused" in embed.description


def test_the_status_embed_lists_this_server_s_settings(guild_config):
    guild_config["guilds"][str(GUILD_ID)] = {
        "station_id": "groove-salad", "autostart": True, "idle_minutes": 5, "channel_status": False,
    }
    guild = FakeGuild(GUILD_ID, me=member(manage_guild=True))
    embed = music.status_embed(FakePlayer(idle_state()), config=guild_config, guild=guild,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    field = next(item for item in embed.fields if item.name == "This server")
    assert "groove-salad" in field.value
    assert "autostart: yes" in field.value
    assert "idle leave: 5 min" in field.value
    assert "status line: off" in field.value


def test_the_status_embed_audits_voice_permissions(guild_config):
    bot_member = member(view_channel=True, connect=True, speak=False)
    guild = FakeGuild(GUILD_ID, me=bot_member)
    bot_member.guild = guild
    channel = FakeChannel(VOICE_ID, "study-room", kind="voice")
    channel.set_permissions_for(bot_member.id, FakePermissions(view_channel=True, connect=True, speak=False))
    embed = music.status_embed(FakePlayer(idle_state(), channel=channel), config=guild_config, guild=guild,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    field = next(item for item in embed.fields if item.name == "Voice permissions")
    assert "✗ Speak" in field.value
    assert "silent" in field.value  # the reason, not just the name


def test_the_status_embed_marks_priority_speaker_as_having_no_effect(guild_config):
    bot_member = member(priority_speaker=True)
    guild = FakeGuild(GUILD_ID, me=bot_member)
    bot_member.guild = guild  # the audit reads guild.me.guild_permissions
    embed = music.status_embed(FakePlayer(idle_state()), config=guild_config, guild=guild,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    field = next(item for item in embed.fields if item.name == "Voice permissions")
    line = next(item for item in field.value.splitlines() if "Priority Speaker" in item)
    assert "–" in line
    assert "no effect" in line


def test_the_status_embed_reports_a_missing_ffmpeg(guild_config):
    embed = music.status_embed(FakePlayer(idle_state()), config=guild_config, guild=None,
                               ffmpeg=None, opus_ok=False, version="1.0.0")
    engine = next(item for item in embed.fields if item.name == "Audio engine")
    assert "MISSING" in engine.value


def test_the_status_embed_shows_the_last_problem(guild_config):
    embed = music.status_embed(FakePlayer(playing_state(lastError="ffmpeg died mid-track")),
                               config=guild_config, guild=None,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.0.0")
    problem = next(item for item in embed.fields if item.name == "Last problem")
    assert "ffmpeg died" in problem.value


def test_the_status_embed_footers_the_version(guild_config):
    embed = music.status_embed(FakePlayer(idle_state()), config=guild_config, guild=None,
                               ffmpeg="/usr/bin/ffmpeg", opus_ok=True, version="1.2.3")
    assert "1.2.3" in (embed.footer.text or "")


def test_the_status_embed_survives_no_guild(guild_config):
    """A DM has no server settings, so the block that would show them is skipped
    rather than rendered as a list of "not set" values."""
    embed = music.status_embed(FakePlayer(idle_state()), config=guild_config, guild=None,
                               ffmpeg=None, opus_ok=False, version="1.0.0")
    assert [item.name for item in embed.fields] == ["Audio engine"]


# --------------------------------------------------------------------------- #
# Error presentation
# --------------------------------------------------------------------------- #
def test_an_error_embed_is_visibly_an_error():
    embed = music._error_embed("Something went wrong")
    assert "Something went wrong" in (embed.description or embed.title or "")
    assert embed.colour is not None


def test_a_long_error_is_truncated_for_an_embed_field():
    embed = music._error_embed("x" * 5000)
    text = str(embed.to_dict())
    assert len(text) < 6000
