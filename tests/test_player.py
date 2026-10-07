"""Playback in one guild: connecting, advancing, the watchdog and reporting.

Discord voice cannot be exercised here (no token, no route out), so the voice
client, the audio source and the channel are fakes from ``conftest``. What is
real is the player's own logic, its database writes and every message it shows.
"""

from __future__ import annotations

import asyncio
import time

import discord
import pytest

import paths
import player as player_module
import settings as settings_module
import sources
import stations
import store
from conftest import FakeChannel, FakeGuild, FakeMember, FakePermissions, FakeVoiceClient
from player import GuildPlayer, PlayerError
from sources import SourceError, Track
from stations import Station


GUILD_ID = 400000000000000001
CHANNEL_ID = 500000000000000001
TEXT_ID = 600000000000000001
USER_ID = 700000000000000001


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeAudioSource(discord.AudioSource):
    """Stands in for ``MonitoredAudio``: records the ffmpeg arguments, reads nothing.

    It has to be a real :class:`discord.AudioSource`: ``PCMVolumeTransformer``
    type-checks what it is handed, and a plain object would fail inside the
    player's own try/except and look like a bug in volume handling.
    """

    instances: list["FakeAudioSource"] = []

    def __init__(self, location, *, executable=None, before_options="", options=""):
        super().__init__()
        self.location = location
        self.executable = executable
        self.before_options = before_options
        self.options = options
        self.cleaned_up = False
        self.bytes_read = 480_000
        self._last_audio = time.monotonic()
        FakeAudioSource.instances.append(self)

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        return b""

    def cleanup(self) -> None:
        self.cleaned_up = True

    @property
    def seconds_since_audio(self) -> float:
        return time.monotonic() - self._last_audio

    def stall_for(self, seconds: float) -> None:
        """Pretend the stream stopped delivering audio this long ago."""
        self._last_audio = time.monotonic() - seconds


class FakeBot:
    def __init__(self, guilds=None):
        self._guilds = {guild.id: guild for guild in (guilds or [])}
        self.closed = False

    def get_guild(self, guild_id):
        return self._guilds.get(int(guild_id))

    async def close(self) -> None:
        self.closed = True


class FakeManager:
    """The three things a :class:`GuildPlayer` uses its manager for."""

    def __init__(self, bot, config, loop):
        self.bot = bot
        self._config = config
        self._loop = loop
        self.saved: list[dict] = []

    @property
    def loop(self):
        return self._loop

    @property
    def config(self):
        return self._config

    def save_config(self, config: dict) -> None:
        self._config = config
        self.saved.append(config)

    async def run_blocking(self, func, *args):
        return func(*args)


@pytest.fixture
def harness(tmp_state, monkeypatch):
    """A guild with a voice channel, a text channel and a fully-permissioned bot."""
    FakeAudioSource.instances.clear()
    monkeypatch.setattr(player_module, "MonitoredAudio", FakeAudioSource)
    monkeypatch.setattr(player_module.paths, "ffmpeg_executable", lambda name="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(player_module.paths, "opus_library", lambda: "/usr/lib/libopus.so")

    bot_member = FakeMember(2, "Lofi", bot=True, permissions=FakePermissions(
        view_channel=True, connect=True, speak=True,
        set_voice_channel_status=True, manage_channels=True,
    ))
    listener = FakeMember(USER_ID, "listener", guild=None)
    voice_channel = FakeChannel(CHANNEL_ID, "study-room", kind="voice", members=[bot_member, listener])
    text_channel = FakeChannel(TEXT_ID, "music", kind="text")
    guild = FakeGuild(
        GUILD_ID, "Test Server",
        channels=[voice_channel, text_channel],
        members=[bot_member, listener],
        me=bot_member,
    )
    bot_member.guild = guild
    listener.guild = guild
    bot = FakeBot([guild])

    config = dict(paths.DEFAULT_CONFIG)
    config["guilds"] = {str(GUILD_ID): {
        "station_id": "library",
        "voice_channel_id": CHANNEL_ID,
        "text_channel_id": TEXT_ID,
        "volume": 70,
        "announce": True,
        "channel_status": True,
    }}

    state = {
        "bot": bot,
        "guild": guild,
        "voice_channel": voice_channel,
        "text_channel": text_channel,
        "bot_member": bot_member,
        "listener": listener,
        "config": config,
    }

    def make(loop) -> GuildPlayer:
        voice_channel.connect_impl = lambda **kwargs: _install_voice_client(voice_channel, guild)
        return GuildPlayer(FakeManager(bot, config, loop), GUILD_ID)

    state["make"] = make
    return state


def _install_voice_client(channel: FakeChannel, guild: FakeGuild) -> FakeVoiceClient:
    client = FakeVoiceClient(channel, guild)
    guild.voice_client = client
    return client


def stub_track(**fields) -> Track:
    """A track that needs no network and no real file."""
    base = dict(
        title="Ambient Set",
        station_id="library",
        station_name="My Library",
        kind="library",
        location="/tmp/does-not-matter.mp3",
        duration=180.0,
        art="💿",
    )
    base.update(fields)
    return Track(**base)


def stub_source(monkeypatch, tracks) -> list:
    """Replace resolution with a queue of canned tracks (or exceptions)."""
    served: list[Station] = []

    async def fake_build_track(station, **kwargs):
        served.append(station)
        item = tracks.pop(0) if isinstance(tracks, list) else tracks
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(station, kwargs)
        return item

    monkeypatch.setattr(player_module.sources, "build_track", fake_build_track)
    return served


async def settle(times: int = 6, delay: float = 0.0) -> None:
    """Let scheduled tasks (``_after_started``) run without sleeping for real."""
    for _ in range(times):
        await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# Connecting
# --------------------------------------------------------------------------- #
async def test_connect_joins_the_channel(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    assert guild_player.is_connected
    assert guild_player.channel is harness["voice_channel"]
    assert harness["voice_channel"].connect_calls == 1


async def test_connecting_deafens_the_bot(harness):
    """A music bot that hears itself is a bot that echoes; self_deaf is not optional."""
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    assert harness["voice_channel"].connect_kwargs["self_deaf"] is True
    assert harness["voice_channel"].connect_kwargs["reconnect"] is True


async def test_connect_applies_the_configured_volume_and_loop_mode(harness):
    harness["config"]["guilds"][str(GUILD_ID)]["loop_mode"] = "station"
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    assert guild_player.volume == 70
    assert guild_player.loop_mode == "station"


async def test_connect_without_a_channel_is_refused(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError, match="Pick a voice channel"):
        await guild_player.connect(None)


async def test_connect_to_a_server_the_bot_is_not_in(harness):
    guild_player = GuildPlayer(FakeManager(FakeBot([]), harness["config"], asyncio.get_running_loop()), GUILD_ID)
    with pytest.raises(PlayerError, match="not in that server"):
        await guild_player.connect(harness["voice_channel"])


async def test_connect_without_speak_explains_the_fix(harness):
    channel = harness["voice_channel"]
    channel.set_permissions_for(harness["bot_member"].id, FakePermissions(
        view_channel=True, connect=True, speak=False,
    ))
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError) as error:
        await guild_player.connect(channel)
    assert "Speak" in str(error.value)


class TextOnlyChannel:
    """A text channel double: no ``connect()``, which is how discord.py differs."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.type = "text"


async def test_connect_to_a_text_channel_is_refused(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError, match="not a voice channel"):
        await guild_player.connect(TextOnlyChannel(TEXT_ID + 1, "music"))


async def test_connecting_twice_to_the_same_channel_does_nothing(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    await guild_player.connect(harness["voice_channel"])
    assert harness["voice_channel"].connect_calls == 1


async def test_connecting_to_another_channel_moves_rather_than_rejoining(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    other = FakeChannel(CHANNEL_ID + 1, "lounge", kind="voice", members=[harness["bot_member"]])
    guild_player.voice = _install_voice_client(harness["voice_channel"], harness["guild"])
    await guild_player.connect(other)
    assert guild_player.voice.moves == [other]
    assert harness["voice_channel"].connect_calls == 1  # moved, did not rejoin


async def test_a_connect_error_about_opus_is_translated(harness):
    import discord

    harness["voice_channel"].connect_error = discord.ClientException("Could not find opus library")
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError, match="libopus"):
        await guild_player.connect(harness["voice_channel"])


async def test_a_connect_timeout_mentions_the_udp_ports(harness):
    harness["voice_channel"].connect_error = asyncio.TimeoutError("timed out")
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError, match="UDP"):
        await guild_player.connect(harness["voice_channel"])


async def test_an_already_connected_error_is_recovered_from(harness):
    import discord

    existing = FakeVoiceClient(harness["voice_channel"], harness["guild"])
    harness["guild"].voice_client = existing
    harness["voice_channel"].connect_error = discord.ClientException("Already connected")
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    assert guild_player.voice is existing


# --------------------------------------------------------------------------- #
# Playing
# --------------------------------------------------------------------------- #
async def test_play_starts_a_track_and_hands_it_to_the_voice_client(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    track = await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert track.title == "Ambient Set"
    assert guild_player.current is track
    assert guild_player.is_playing
    assert guild_player.voice.source is not None


async def test_play_opens_a_session_in_the_database(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert guild_player.session_id is not None
    assert store.session_count(GUILD_ID) == 1


async def test_play_records_a_track_play(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="Counted")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert [row["title"] for row in store.top_tracks(GUILD_ID)] == ["Counted"]


async def test_play_uses_the_configured_station_when_none_is_named(harness, monkeypatch):
    served = stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play(channel=harness["voice_channel"])
    assert served[0].id == "library"


async def test_play_accepts_a_station_object(harness, monkeypatch):
    """``/lofi studio`` hands the player a station that is not in the registry."""
    served = stub_source(monkeypatch, [stub_track(kind="generative", station_id="studio-cafe")])
    guild_player = harness["make"](asyncio.get_running_loop())
    mood = stations.studio_station("studio-cafe")
    await guild_player.play(mood, channel=harness["voice_channel"])
    assert served[0] is mood


async def test_play_with_an_unknown_station_says_so(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError) as error:
        await guild_player.play("nope-not-real", channel=harness["voice_channel"])
    assert "/lofi stations" in str(error.value)


async def test_play_without_any_channel_to_use_says_what_to_do(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)].pop("voice_channel_id")
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError) as error:
        await guild_player.play("library")
    assert "voice channel" in str(error.value)


async def test_play_finds_the_requester_s_own_channel(harness, monkeypatch):
    """``/lofi play`` with no channel argument uses where you are sitting."""
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)].pop("voice_channel_id")
    harness["listener"].voice.channel = harness["voice_channel"]
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", requested_by=USER_ID)
    assert guild_player.channel is harness["voice_channel"]


async def test_a_source_error_becomes_a_player_error_with_the_same_advice(harness, monkeypatch):
    stub_source(monkeypatch, [SourceError("There is no audio in /srv/lofi. Drop some .mp3 files in there.")])
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError, match="Drop some .mp3"):
        await guild_player.play("library", channel=harness["voice_channel"])


async def test_a_stale_youtube_resolution_is_retried_once(harness, monkeypatch):
    """A 403 usually means a cached URL expired; one retry saves a support ticket."""
    attempts = []

    async def flaky(station, **kwargs):
        attempts.append(station.url)
        if len(attempts) == 1:
            raise SourceError("HTTP Error 403: Forbidden")
        return stub_track(kind="youtube", station_id=station.id)

    monkeypatch.setattr(player_module.sources, "build_track", flaky)
    cleared = []
    monkeypatch.setattr(player_module.sources, "clear_resolution_cache", lambda: cleared.append(True))
    guild_player = harness["make"](asyncio.get_running_loop())
    track = await guild_player.play("lofi-girl", channel=harness["voice_channel"])
    assert len(attempts) == 2
    assert cleared == [True]
    assert track.kind == "youtube"


async def test_a_non_403_source_error_is_not_retried(harness, monkeypatch):
    attempts = []

    async def always(station, **kwargs):
        attempts.append(station.id)
        raise SourceError("There is no audio in the music folder.")

    monkeypatch.setattr(player_module.sources, "build_track", always)
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError):
        await guild_player.play("library", channel=harness["voice_channel"])
    assert len(attempts) == 1


async def test_the_ffmpeg_arguments_reach_the_audio_source(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(kind="stream", location="https://ice2.somafm.com/g")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("groove-salad", channel=harness["voice_channel"])
    await settle()
    created = FakeAudioSource.instances[-1]
    assert created.executable == "/usr/bin/ffmpeg"
    assert "-reconnect" in created.before_options  # network source: retry flags
    assert created.options == sources.FFMPEG_OPTIONS["stream"]


async def test_a_local_track_gets_no_network_flags(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert FakeAudioSource.instances[-1].before_options == "-nostdin"


async def test_missing_ffmpeg_is_reported_before_anything_is_started(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    monkeypatch.setattr(player_module.paths, "ffmpeg_executable", lambda name="ffmpeg": None)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.connect(harness["voice_channel"])
    with pytest.raises(PlayerError, match="ffmpeg was not found"):
        await guild_player._start(stub_track())


async def test_the_volume_is_applied_to_the_transformer(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)]["volume"] = 25
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert guild_player.transformer.volume == pytest.approx(0.25)


async def test_a_second_play_reuses_the_open_session(harness, monkeypatch):
    """One session per visit: restarting a station must not double-count listening time."""
    stub_source(monkeypatch, [stub_track(title="First"), stub_track(title="Second")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    first_session = guild_player.session_id
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert guild_player.session_id == first_session
    assert store.session_count(GUILD_ID) == 1


async def test_a_played_track_is_announced_once(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="Announced")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    embeds = [embed for embed in harness["text_channel"].sent if embed is not None]
    assert embeds, "the now-playing embed was not sent to the configured text channel"
    serialised = str(embeds[-1].to_dict())
    assert "Announced" in serialised


async def test_announcements_can_be_turned_off(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)]["announce"] = False
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    assert harness["text_channel"].sent == []


async def test_the_library_shuffle_remembers_recent_files(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(location="/tmp/a.mp3"), stub_track(location="/tmp/b.mp3")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert "/tmp/a.mp3" in guild_player._recent_library


# --------------------------------------------------------------------------- #
# Advancing: the station must not end
# --------------------------------------------------------------------------- #
async def test_when_a_track_ends_the_next_one_starts(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="One"), stub_track(title="Two")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.voice.fire_after()  # the audio thread finished the track
    await settle(times=10)
    assert guild_player.current.title == "Two"


async def test_a_queue_is_drained_before_the_station_provides_more(harness, monkeypatch):
    served = stub_source(monkeypatch, [stub_track(title="One"), stub_track(title="Queued"), stub_track(title="Station")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    queued = await guild_player.queue_track("library")
    assert queued.title == "Queued"
    guild_player.voice.fire_after()
    await settle(times=10)
    assert guild_player.current.title == "Queued"


async def test_the_queue_is_bounded(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()] * 60)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    for _ in range(player_module.MAX_QUEUE):
        await guild_player.queue_track("library")
    with pytest.raises(PlayerError, match="queue is full"):
        await guild_player.queue_track("library")


async def test_clear_queue_reports_how_many_were_dropped(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()] * 5)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    await guild_player.queue_track("library")
    await guild_player.queue_track("library")
    assert guild_player.clear_queue() == 2
    assert not guild_player.queue


async def test_a_generated_station_moves_to_a_new_seed(harness, monkeypatch):
    """Studio Lofi has no playlist: the next track is a new seed of the same mood."""
    seeds = []

    async def capture(station, *, seed=None, **kwargs):
        seeds.append(seed)
        return stub_track(kind="generative", station_id="studio-cafe", seed=seed, title=f"Seed {seed}")

    monkeypatch.setattr(player_module.sources, "build_track", capture)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play(stations.studio_station("studio-cafe"), channel=harness["voice_channel"])
    await settle()
    guild_player.voice.fire_after()
    await settle(times=10)
    assert seeds[0] is None or seeds[0] != seeds[-1]
    assert guild_player.current.seed is not None


async def test_a_live_station_is_restarted_not_advanced(harness, monkeypatch):
    served = stub_source(monkeypatch, [stub_track(kind="stream", is_live=True), stub_track(kind="stream", is_live=True)])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("groove-salad", channel=harness["voice_channel"])
    await settle()
    guild_player.voice.fire_after()
    await settle(times=10)
    assert len(served) == 2
    assert guild_player.is_playing


async def test_a_playback_error_retries_with_a_backoff(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()] * 3)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.voice.fire_after(RuntimeError("ffmpeg died"))
    await settle(times=10)
    assert guild_player.restart_attempts >= 1
    assert "ffmpeg died" in guild_player.last_error


async def test_repeated_failure_gives_up_audibly(harness, monkeypatch):
    """Sitting silent in a voice channel is worse than leaving and saying why."""
    stub_source(monkeypatch, [stub_track()] * 40)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.restart_attempts = player_module.MAX_RESTART_ATTEMPTS
    guild_player.voice.fire_after(RuntimeError("still broken"))
    await settle(times=20)
    assert guild_player.voice is None or not guild_player.is_connected
    assert store.guild_events(GUILD_ID, limit=20)


async def test_a_permanent_source_failure_stops_with_the_reason(harness, monkeypatch):
    async def always(station, **kwargs):
        raise SourceError("There is no audio in the music folder.")

    monkeypatch.setattr(player_module.sources, "build_track", always)
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError):
        await guild_player.play("library", channel=harness["voice_channel"])
    await guild_player.connect(harness["voice_channel"])
    guild_player.station = stations.BUILT_IN_BY_ID["library"]
    await guild_player._advance("source-error")
    await settle(times=10)
    assert not guild_player.is_connected


async def test_skip_moves_to_the_next_track(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="One"), stub_track(title="Two")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    skipped = await guild_player.skip()
    await settle(times=10)
    assert guild_player.current.title == "Two"
    assert skipped.title == "Two"


async def test_skip_without_anything_playing_is_refused(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    with pytest.raises(PlayerError, match="Nothing is playing"):
        await guild_player.skip()


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
async def test_pause_and_resume(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert await guild_player.pause() is True
    assert guild_player.paused is True
    assert guild_player.voice.paused is True
    assert await guild_player.resume() is True
    assert guild_player.paused is False


async def test_pause_when_nothing_is_playing_reports_that(harness, monkeypatch):
    guild_player = harness["make"](asyncio.get_running_loop())
    assert await guild_player.pause() is False


async def test_resume_when_not_paused_reports_that(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert await guild_player.resume() is False


async def test_toggle_pause_flips(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert await guild_player.toggle_pause() is True  # now paused
    assert await guild_player.toggle_pause() is False  # now playing


async def test_set_volume_changes_the_transformer_and_the_settings(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert await guild_player.set_volume(35) == 35
    assert guild_player.transformer.volume == pytest.approx(0.35)
    assert guild_player.volume == 35


async def test_a_volume_nudge_does_not_rewrite_the_server_config(harness, monkeypatch):
    """Anyone in the channel may turn it down; that is not a settings change.
    Persistent volume belongs to /lofi setup volume:, which needs Manage Server."""
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    await guild_player.set_volume(35)
    assert guild_player.manager.saved == []


async def test_volume_is_clamped(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert await guild_player.set_volume(500) == settings_module.MAX_VOLUME
    assert await guild_player.set_volume(-20) == settings_module.MIN_VOLUME


async def test_set_loop_mode_normalises_what_it_is_given(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    assert await guild_player.set_loop_mode("STATION") == "station"
    assert guild_player.loop_mode == "station"
    assert await guild_player.set_loop_mode("nonsense") == "off"


async def test_a_loop_mode_change_is_not_a_config_change(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.set_loop_mode("queue")
    assert guild_player.manager.saved == []


async def test_stop_disconnects_and_closes_the_session(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    session_id = guild_player.session_id
    await guild_player.stop()
    await settle(times=10)
    assert not guild_player.is_connected
    assert guild_player.current is None
    row = store.db_fetchone("SELECT ended_reason AS r FROM play_sessions WHERE id = ?", (session_id,))
    assert row is not None and row["r"]


async def test_stop_can_leave_the_bot_in_the_channel(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    await guild_player.stop(disconnect=False)
    await settle(times=10)
    assert guild_player.is_connected
    assert guild_player.current is None


async def test_stop_without_a_session_writes_nothing(harness, monkeypatch):
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.stop()
    assert store.session_count(GUILD_ID) == 0


async def test_a_stopped_session_records_the_peak_listener_count(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.peak_listeners = 4
    session_id = guild_player.session_id
    await guild_player.stop()
    await settle(times=10)
    assert store.db_fetchone("SELECT peak_listeners AS p FROM play_sessions WHERE id = ?", (session_id,))["p"] == 4


# --------------------------------------------------------------------------- #
# Listeners and the watchdog
# --------------------------------------------------------------------------- #
async def test_listeners_counts_humans_not_bots(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert guild_player.listeners() == 1  # the bot is in the channel too


async def test_listeners_is_zero_when_not_connected(harness):
    guild_player = harness["make"](asyncio.get_running_loop())
    assert guild_player.listeners() == 0


async def test_the_watchdog_leaves_an_empty_channel_after_the_idle_limit(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)]["idle_minutes"] = 1
    harness["voice_channel"].members = [harness["bot_member"]]  # nobody listening
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player._empty_since = time.monotonic() - 120
    await guild_player._watchdog_tick()
    await settle(times=10)
    assert not guild_player.is_connected


async def test_the_watchdog_stays_when_people_are_listening(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)]["idle_minutes"] = 1
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player._empty_since = time.monotonic() - 600
    await guild_player._watchdog_tick()
    await settle()
    assert guild_player.is_connected


async def test_always_on_never_idles_out(harness, monkeypatch):
    """A 24/7 radio channel is the point of the bot for some servers."""
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)].update({"always_on": True, "idle_minutes": 1})
    harness["voice_channel"].members = [harness["bot_member"]]
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player._empty_since = time.monotonic() - 3600
    await guild_player._watchdog_tick()
    await settle(times=10)
    assert guild_player.is_connected


async def test_idle_zero_means_never_disconnect(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)]["idle_minutes"] = 0
    harness["voice_channel"].members = [harness["bot_member"]]
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player._empty_since = time.monotonic() - 7200
    await guild_player._watchdog_tick()
    assert guild_player.is_connected


async def test_the_watchdog_notices_a_dropped_voice_connection(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.voice.connected = False
    await guild_player._watchdog_tick()
    await settle(times=10)
    assert guild_player.voice is None


async def test_the_watchdog_restarts_a_stalled_stream(harness, monkeypatch):
    """A 24/7 feed that stops sending audio without closing is the classic failure."""
    stub_source(monkeypatch, [stub_track(kind="stream", is_live=True)] * 2)
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("groove-salad", channel=harness["voice_channel"])
    await settle()
    guild_player.audio.stall_for(player_module.STALL_SECONDS + 5)
    await guild_player._watchdog_tick()
    await settle(times=20)
    assert any(event["kind"] == "stall" for event in store.guild_events(GUILD_ID, limit=20))


async def test_a_paused_stream_is_not_treated_as_stalled(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(kind="stream", is_live=True)])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("groove-salad", channel=harness["voice_channel"])
    await settle()
    await guild_player.pause()
    guild_player.audio.stall_for(player_module.STALL_SECONDS + 5)
    await guild_player._watchdog_tick()
    await settle()
    assert guild_player.paused is True
    assert guild_player.current is not None


async def test_the_watchdog_tracks_the_peak_listener_count(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    harness["voice_channel"].members.append(FakeMember(999, "late"))
    await guild_player._watchdog_tick()
    assert guild_player.peak_listeners == 2


async def test_the_watchdog_writes_the_channel_status(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="Status Track")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    guild_player._last_status_write = 0.0
    await guild_player._watchdog_tick()
    await settle(times=10)
    assert any("Status Track" in str(edit.get("status", "")) for edit in harness["voice_channel"].edits)


# --------------------------------------------------------------------------- #
# Channel status
# --------------------------------------------------------------------------- #
async def test_the_status_text_shows_the_art_and_title(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="Rainy Tape", art="🌧️")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    assert guild_player.status_payload() == "🌧️ Rainy Tape"


async def test_the_status_text_says_paused(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="Rainy Tape")])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    await guild_player.pause()
    assert guild_player.status_payload().endswith("(paused)")


async def test_the_status_text_is_truncated_to_discord_s_limit(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(title="x" * 400)])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    payload = guild_player.status_payload()
    assert len(payload) <= player_module.STATUS_MAX_LENGTH
    assert payload.endswith("…")


async def test_the_status_can_be_turned_off_per_guild(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    harness["config"]["guilds"][str(GUILD_ID)]["channel_status"] = False
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    assert guild_player.status_payload() is None
    assert harness["voice_channel"].edits == []


async def test_turning_the_status_off_clears_what_was_there(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    assert harness["voice_channel"].edits
    harness["config"]["guilds"][str(GUILD_ID)]["channel_status"] = False
    assert await guild_player.update_channel_status(clear=True, force=True) is True
    assert harness["voice_channel"].edits[-1]["status"] is None


async def test_a_refused_status_is_recorded_rather_than_retried_forever(harness, monkeypatch):
    """The permission is optional; the operator still deserves to know why it is blank."""
    import discord

    class StubResponse:
        """discord.HTTPException reads status, reason and json() off the response."""
        status = 403
        reason = "Forbidden"

        @staticmethod
        def json():
            return {"message": "Missing Permissions", "code": 50013}

    stub_source(monkeypatch, [stub_track()])

    async def refuse(**kwargs):
        raise discord.Forbidden(StubResponse(), "Missing Permissions")

    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    guild_player.channel.edit = refuse
    guild_player._last_status_write = 0.0
    await guild_player.update_channel_status(force=True)
    await settle(times=5)
    assert guild_player.status_supported is False


async def test_the_status_is_cleared_when_playback_stops(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    await guild_player.stop()
    await settle(times=10)
    assert any(edit.get("status") is None for edit in harness["voice_channel"].edits)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
async def test_state_is_json_safe(harness, monkeypatch):
    import json

    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    snapshot = guild_player.state()
    json.dumps(snapshot)
    assert snapshot["guildId"] == str(GUILD_ID)
    assert snapshot["connected"] is True
    assert snapshot["listeners"] == 1
    assert snapshot["volume"] == 70
    assert snapshot["track"]["title"] == "Ambient Set"
    assert snapshot["station"]["id"] == "library"


async def test_state_of_an_idle_player_is_still_complete(harness):
    import json

    guild_player = harness["make"](asyncio.get_running_loop())
    snapshot = guild_player.state()
    json.dumps(snapshot)
    assert snapshot["connected"] is False
    assert snapshot["track"] is None
    assert snapshot["station"] is None
    assert snapshot["queue"] == []


async def test_state_reports_progress_for_a_known_duration(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(duration=100.0)])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.track_started_at = time.time() - 25
    snapshot = guild_player.state()
    assert snapshot["progress"] == pytest.approx(0.25, abs=0.02)
    assert snapshot["duration"] == 100.0


async def test_a_live_track_has_no_progress(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track(kind="stream", is_live=True)])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("groove-salad", channel=harness["voice_channel"])
    await settle()
    snapshot = guild_player.state()
    assert snapshot["duration"] is None
    assert snapshot["progress"] is None


async def test_state_carries_the_last_error(harness, monkeypatch):
    stub_source(monkeypatch, [stub_track()])
    guild_player = harness["make"](asyncio.get_running_loop())
    await guild_player.play("library", channel=harness["voice_channel"])
    await settle()
    guild_player.last_error = "ffmpeg died"
    assert guild_player.state()["lastError"] == "ffmpeg died"


def test_format_seconds_reads_like_a_clock():
    assert player_module._format_seconds(None) == "unknown"
    assert player_module._format_seconds(0) == "unknown"
    assert player_module._format_seconds(65) == "1:05"
    assert player_module._format_seconds(3725) == "1:02:05"


def test_short_errors_are_truncated_for_a_discord_embed():
    assert len(player_module._short_error(RuntimeError("x" * 5000))) <= 280


def test_connect_errors_mention_the_fix_not_the_traceback():
    assert "libopus" in player_module._explain_connect_error(RuntimeError("opus library not found"))
    assert "ffmpeg" in player_module._explain_connect_error(RuntimeError("ffmpeg not found"))
    assert "UDP" in player_module._explain_connect_error(RuntimeError("timed out"))


def test_stop_reasons_read_like_sentences():
    for reason, text in player_module.STOP_REASONS.items():
        assert text and text[0].islower() or text[0].isupper()
        assert len(text) < 120


# --------------------------------------------------------------------------- #
# A real end-to-end play, with a real file and no network
# --------------------------------------------------------------------------- #
async def test_playing_a_real_local_file_end_to_end(tmp_state, monkeypatch, harness):
    """No stubs: a WAV on disk goes through resolution, ffmpeg args and state."""
    import generative

    folder = tmp_state / "music"
    folder.mkdir(exist_ok=True)
    generative.write_wav(folder / "Real Track.wav", generative.render_audio(generative.recipe_for("cafe"), seed=3, bars=2))
    monkeypatch.setattr(paths, "music_dir", lambda: folder)

    guild_player = harness["make"](asyncio.get_running_loop())
    track = await guild_player.play("library", channel=harness["voice_channel"])
    await settle(times=10)
    assert track.location.endswith("Real Track.wav")
    assert track.title == "Real Track"
    assert guild_player.state()["connected"] is True
    created = FakeAudioSource.instances[-1]
    assert created.location.endswith("Real Track.wav")
    assert created.executable == "/usr/bin/ffmpeg"
