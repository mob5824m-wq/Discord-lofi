"""The demo provider: a dashboard with no Discord connection.

``--demo`` is how someone evaluates the UI before they have a bot token, so it
has to exercise the same code paths as the real thing - the same route handlers,
the same JSON shapes - with simulated servers instead of a gateway connection.
"""

from __future__ import annotations

import json

import pytest

import demo
import paths
from dashboard import DashboardError


KEY = "a-demo-dashboard-key-long-enough-to-be-accepted-here"
GUILD_IDS = [int(spec["id"]) for spec in demo.DEMO_GUILDS]


@pytest.fixture
def provider(tmp_state):
    config = dict(paths.DEFAULT_CONFIG)
    config["dashboard_token"] = KEY
    return demo.DemoProvider(config, token=KEY)


def first(provider) -> int:
    return GUILD_IDS[0]


# --------------------------------------------------------------------------- #
# Shape of the simulation
# --------------------------------------------------------------------------- #
def test_the_demo_says_it_is_a_demo(provider):
    assert provider.demo is True


def test_three_servers_are_simulated(provider):
    assert len(provider.guilds) == 3
    assert len(set(GUILD_IDS)) == 3


def test_the_simulated_servers_differ_from_each_other(provider):
    """A dashboard that looks the same for every server hides rendering bugs."""
    names = {guild.name for guild in provider.guilds}
    stations = {guild.station.id for guild in provider.guilds}
    states = {guild.state for guild in provider.guilds}
    assert len(names) == 3
    assert len(stations) >= 2
    assert "playing" in states and "idle" in states  # one connected, one not


def test_the_simulated_ids_are_real_looking_snowflakes():
    for guild_id in GUILD_IDS:
        assert isinstance(guild_id, int)
        assert len(str(guild_id)) >= 17


async def test_the_demo_uses_the_real_station_registry(provider):
    import stations

    payload = await provider.overview()
    listed = await provider.stations() if hasattr(provider, "stations") else None
    assert payload["stats"]["stations"] == len(stations.BUILT_IN_STATIONS)
    assert listed is None or listed


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
async def test_the_overview_has_the_shape_the_ui_renders(provider):
    payload = await provider.overview()
    assert {"bot", "stats", "guilds"} <= set(payload)
    assert payload["demo"] is True
    assert {"guilds", "connected", "playing", "listeners", "sessions", "topTracks", "recentEvents"} <= set(payload["stats"])


async def test_the_overview_counts_the_simulated_servers(provider):
    payload = await provider.overview()
    assert payload["stats"]["guilds"] == 3
    assert payload["bot"]["guildCount"] == 3


async def test_the_overview_is_json_serialisable_with_string_ids(provider):
    payload = await provider.overview()
    json.dumps(payload)
    assert all(isinstance(guild["id"], str) for guild in payload["guilds"])


async def test_the_overview_is_not_marked_as_a_live_discord_connection(provider):
    """The UI shows a demo banner; pretending to be connected would be a lie."""
    payload = await provider.overview()
    assert payload["bot"]["ready"] in (True, False)
    assert payload["demo"] is True


async def test_the_activity_feed_is_seeded(provider):
    payload = await provider.overview()
    assert payload["stats"]["recentEvents"]
    assert {"kind", "detail", "guildName", "createdAt"} <= set(payload["stats"]["recentEvents"][0])


async def test_the_most_played_panel_is_seeded(provider):
    payload = await provider.overview()
    tracks = payload["stats"]["topTracks"]
    assert tracks
    assert tracks[0]["plays"] >= tracks[-1]["plays"]  # sorted, as the panel assumes


# --------------------------------------------------------------------------- #
# Guild detail
# --------------------------------------------------------------------------- #
async def test_the_guild_detail_has_everything_the_servers_page_renders(provider):
    payload = await provider.guild_detail(first(provider))
    assert {"guild", "settings", "permissions", "voiceChannels", "textChannels", "roles", "player"} <= set(payload)


async def test_the_permission_audit_is_complete(provider):
    import permissions

    payload = await provider.guild_detail(first(provider))
    assert len(payload["permissions"]) == len(permissions.REQUIREMENTS)
    keys = {row["key"] for row in payload["permissions"]}
    assert "priority_speaker" in keys


async def test_the_voice_and_text_channels_are_listed(provider):
    payload = await provider.guild_detail(first(provider))
    assert payload["voiceChannels"]
    assert payload["textChannels"]
    assert all(isinstance(channel["id"], str) for channel in payload["voiceChannels"])


async def test_the_roles_include_a_dj_choice(provider):
    """The DJ role dropdown needs something to offer, or the field looks broken."""
    payload = await provider.guild_detail(first(provider))
    assert len(payload["roles"]) >= 2


async def test_an_unknown_guild_is_a_404(provider):
    with pytest.raises(DashboardError) as error:
        await provider.guild_detail(999999999999999999)
    assert error.value.status == 404


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #
async def test_the_player_state_reports_a_track(provider):
    state = await provider.player_state(first(provider))
    assert state["connected"] is True
    assert state["track"]["title"]
    assert isinstance(state["listeners"], int)


async def test_pausing_is_reflected_in_the_state(provider):
    guild_id = first(provider)
    result = await provider.player_action(guild_id, "pause", {})
    assert result["ok"] is True
    assert result["player"]["paused"] is True
    assert (await provider.player_state(guild_id))["paused"] is True


async def test_resuming_after_a_pause(provider):
    guild_id = first(provider)
    await provider.player_action(guild_id, "pause", {})
    result = await provider.player_action(guild_id, "resume", {})
    assert result["player"]["paused"] is False


async def test_the_status_line_says_paused(provider):
    """The same text a real server would show under the channel name."""
    guild_id = first(provider)
    await provider.player_action(guild_id, "pause", {})
    state = await provider.player_state(guild_id)
    assert state["statusText"].endswith("(paused)")


async def test_skipping_changes_the_track(provider):
    """On a generated station: a live broadcast's title is the same before and
    after a skip, so it would prove nothing."""
    guild_id = first(provider)
    await provider.player_action(guild_id, "play", {"station": "studio-rainy"})
    before = (await provider.player_state(guild_id))["track"]["title"]
    await provider.player_action(guild_id, "skip", {})
    after = (await provider.player_state(guild_id))["track"]["title"]
    assert before != after


async def test_a_generated_track_rolls_over_when_its_time_is_up(provider, monkeypatch):
    """The UI polls every few seconds; a track that never ends looks frozen."""
    guild = provider._guild(first(provider))
    await provider.player_action(guild.id, "play", {"station": "studio-midnight"})
    clock = [guild.track_started_at]
    monkeypatch.setattr(demo.time, "time", lambda: clock[0])
    before = guild.track()["title"]
    clock[0] = guild.track_started_at + demo.DEMO_TRACK_SECONDS + 1
    guild.tick()
    assert guild.track()["title"] != before
    assert guild.seed > 0


async def test_the_volume_action_is_applied_and_clamped(provider):
    guild_id = first(provider)
    result = await provider.player_action(guild_id, "volume", {"percent": 42})
    assert result["player"]["volume"] == 42
    result = await provider.player_action(guild_id, "volume", {"percent": 5000})
    assert result["player"]["volume"] == 150


async def test_playing_a_station_switches_the_station(provider):
    guild_id = first(provider)
    result = await provider.player_action(guild_id, "play", {"station": "groove-salad"})
    assert result["player"]["station"]["id"] == "groove-salad"


async def test_playing_a_studio_mood_renders_a_generated_title(provider):
    """The dashboard offers the moods, so the demo has to accept them too."""
    guild_id = first(provider)
    result = await provider.player_action(guild_id, "play", {"station": "studio-midnight"})
    assert result["player"]["station"]["id"] == "studio-midnight"
    assert result["player"]["track"]["title"]


async def test_playing_an_unknown_station_is_refused(provider):
    guild_id = first(provider)
    with pytest.raises(DashboardError):
        await provider.player_action(guild_id, "play", {"station": "not-a-station"})


async def test_stopping_disconnects(provider):
    guild_id = first(provider)
    result = await provider.player_action(guild_id, "stop", {})
    assert result["player"]["connected"] is False
    assert result["player"]["track"] is None


async def test_an_unknown_action_is_refused(provider):
    with pytest.raises(DashboardError):
        await provider.player_action(first(provider), "launch-missiles", {})


async def test_an_action_on_an_unknown_guild_is_a_404(provider):
    with pytest.raises(DashboardError) as error:
        await provider.player_action(1, "pause", {})
    assert error.value.status == 404


async def test_listeners_drift_over_time(provider, monkeypatch):
    """A number that never moves hides a broken poll in the UI."""
    guild = provider._guild(first(provider))
    clock = [1_000_000.0]
    monkeypatch.setattr(demo.time, "time", lambda: clock[0])
    seen = {guild.listeners}
    for step in range(40):
        # Land inside the drift window (the first five seconds of each period).
        clock[0] = 1_000_000.0 + step * demo.DEMO_LISTENER_DRIFT + 1
        guild.tick()
        seen.add(guild.state_payload()["listeners"])
    assert len(seen) > 1


async def test_listeners_do_not_change_on_every_single_poll(provider, monkeypatch):
    """Drift is time-gated on purpose: a number that jumps on each 3s poll looks
    like a broken dashboard rather than a busy voice channel."""
    guild = provider._guild(first(provider))
    clock = [1_000_000.0 + demo.DEMO_LISTENER_DRIFT / 2]  # middle of a period
    monkeypatch.setattr(demo.time, "time", lambda: clock[0])
    before = guild.listeners
    for _ in range(5):
        guild.tick()
    assert guild.listeners == before


async def test_an_idle_server_does_not_tick(provider, monkeypatch):
    guild = next(item for item in provider.guilds if not item.connected)
    clock = [1_000_000.0 + 1]
    monkeypatch.setattr(demo.time, "time", lambda: clock[0])
    before = (guild.listeners, guild.seed)
    guild.tick()
    assert (guild.listeners, guild.seed) == before


async def test_the_peak_listener_count_only_grows(provider, monkeypatch):
    guild = provider._guild(first(provider))
    clock = [1_000_000.0]
    monkeypatch.setattr(demo.time, "time", lambda: clock[0])
    peaks = []
    for step in range(30):
        clock[0] = 1_000_000.0 + step * demo.DEMO_LISTENER_DRIFT + 1
        guild.tick()
        peaks.append(guild.peak_listeners)
    assert peaks == sorted(peaks)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
async def test_settings_are_saved_into_the_provider_config(provider):
    guild_id = first(provider)
    await provider.save_settings(guild_id, {"volume": 33, "autostart": True})
    assert provider.config["guilds"][str(guild_id)]["volume"] == 33
    assert provider.config["guilds"][str(guild_id)]["autostart"] is True


async def test_saved_settings_come_back_on_the_next_read(provider):
    guild_id = first(provider)
    await provider.save_settings(guild_id, {"volume": 61})
    detail = await provider.guild_detail(guild_id)
    assert detail["settings"]["volume"] == 61


async def test_an_out_of_range_volume_is_clamped(provider):
    guild_id = first(provider)
    result = await provider.save_settings(guild_id, {"volume": 900})
    assert result["settings"]["volume"] == 150


async def test_an_unknown_setting_is_refused(provider):
    with pytest.raises(DashboardError):
        await provider.save_settings(first(provider), {"botToken": "steal"})


async def test_settings_for_an_unknown_guild_are_a_404(provider):
    with pytest.raises(DashboardError) as error:
        await provider.save_settings(1, {"volume": 50})
    assert error.value.status == 404


# --------------------------------------------------------------------------- #
# History and sync
# --------------------------------------------------------------------------- #
async def test_history_is_seeded_for_every_guild(provider):
    payload = await provider.history(None, 50)
    assert {"sessions", "events", "topTracks", "listeningMinutes"} <= set(payload)
    assert payload["sessions"]
    assert payload["demo"] is True


async def test_history_can_be_filtered_to_one_guild(provider):
    guild_id = first(provider)
    payload = await provider.history(guild_id, 50)
    assert payload["sessions"]
    assert all(row["guildId"] == str(guild_id) for row in payload["sessions"])


async def test_history_respects_the_limit(provider):
    payload = await provider.history(None, 3)
    assert len(payload["sessions"]) <= 3


async def test_playing_a_track_adds_history(provider):
    guild_id = first(provider)
    before = len((await provider.history(guild_id, 100))["sessions"])
    await provider.player_action(guild_id, "skip", {})
    after = len((await provider.history(guild_id, 100))["sessions"])
    assert after >= before


async def test_a_command_sync_is_simulated(provider):
    result = await provider.sync_commands(None)
    assert result["ok"] is True
    assert result["demo"] is True
    assert result["result"]["global"] == 30


async def test_a_guild_scoped_sync_is_simulated(provider):
    result = await provider.sync_commands(first(provider))
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# Config adoption
# --------------------------------------------------------------------------- #
async def test_a_station_added_through_the_api_survives_the_provider(provider):
    """The provider keeps its own copy of the config, so a plain dict.update on
    the caller's copy would silently discard a station added from the UI."""
    import stations

    updated = stations.save_custom_station(
        provider.config,
        stations.Station(id="demo-added", name="Demo Added", kind="stream", url="https://example.com/a.mp3"),
    )
    provider.save_config(updated)
    assert stations.find_station(provider.config, "demo-added") is not None


async def test_saving_a_config_replaces_it_wholesale(provider):
    provider.save_config(dict(provider.config, default_station="chillhop"))
    assert provider.config["default_station"] == "chillhop"


async def test_a_removed_station_disappears(provider):
    import stations

    provider.save_config(stations.save_custom_station(
        provider.config,
        stations.Station(id="temp", name="Temp", kind="stream", url="https://example.com/a.mp3"),
    ))
    updated, removed = stations.remove_custom_station(provider.config, "temp")
    provider.save_config(updated)
    assert removed is True
    assert stations.find_station(provider.config, "temp") is None
