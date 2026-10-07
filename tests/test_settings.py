"""Per-guild settings: flattening, clamping and validation."""

from __future__ import annotations

import pytest

import settings


GUILD_ID = 400000000000000001


def entry(config, **updates) -> dict:
    """A config with one guild entry."""
    config["guilds"] = {str(GUILD_ID): updates}
    return config


def test_defaults_apply_to_an_unconfigured_guild(config):
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert resolved["station_id"] == config["default_station"]
    assert resolved["volume"] == config["default_volume"]
    assert resolved["idle_minutes"] == config["idle_disconnect_minutes"]
    assert resolved["autostart"] is False
    assert resolved["always_on"] is False
    assert resolved["announce"] is True
    assert resolved["channel_status"] is True
    assert resolved["loop_mode"] == "off"


def test_every_documented_key_is_always_present(config):
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert set(resolved) == set(settings.GUILD_SETTING_KEYS)


def test_guild_values_override_the_globals(config):
    entry(config, volume=25, station_id="chillhop", idle_minutes=0, autostart=True)
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert resolved["volume"] == 25
    assert resolved["station_id"] == "chillhop"
    assert resolved["idle_minutes"] == 0
    assert resolved["autostart"] is True


def test_global_defaults_are_used_for_keys_the_guild_does_not_set(config):
    config["default_volume"] = 33
    entry(config, station_id="chillhop")
    assert settings.get_guild_settings(config, GUILD_ID)["volume"] == 33


def test_snowflake_keys_are_accepted_as_int_or_string(config):
    config["guilds"] = {str(GUILD_ID): {"volume": 40}}
    assert settings.get_guild_settings(config, GUILD_ID)["volume"] == 40
    assert settings.get_guild_settings(config, str(GUILD_ID))["volume"] == 40


def test_a_garbage_guilds_map_is_ignored(config):
    config["guilds"] = "not a dict"
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert resolved["volume"] == settings.DEFAULT_VOLUME


def test_a_garbage_guild_entry_is_ignored(config):
    config["guilds"] = {str(GUILD_ID): ["volume"]}
    assert settings.get_guild_settings(config, GUILD_ID)["volume"] == settings.DEFAULT_VOLUME


@pytest.mark.parametrize("value,expected", [(None, settings.DEFAULT_VOLUME), ("", settings.DEFAULT_VOLUME)])
def test_missing_ids_stay_missing(config, value, expected):
    entry(config, voice_channel_id=value, dj_role_id=value)
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert resolved["voice_channel_id"] is None
    assert resolved["dj_role_id"] is None


def test_ids_are_parsed_from_strings(config):
    entry(config, voice_channel_id="123", text_channel_id="456", dj_role_id="789")
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert resolved["voice_channel_id"] == 123
    assert resolved["text_channel_id"] == 456
    assert resolved["dj_role_id"] == 789


def test_a_non_numeric_id_is_dropped_rather_than_raising(config):
    entry(config, voice_channel_id="not-a-number")
    assert settings.get_guild_settings(config, GUILD_ID)["voice_channel_id"] is None


def test_the_legacy_top_level_now_playing_channel_is_used(config):
    config["now_playing_channel_id"] = 4242
    resolved = settings.get_guild_settings(config, GUILD_ID)
    assert resolved["text_channel_id"] == 4242


def test_a_guild_text_channel_wins_over_the_legacy_key(config):
    config["now_playing_channel_id"] = 4242
    entry(config, text_channel_id=99)
    assert settings.get_guild_settings(config, GUILD_ID)["text_channel_id"] == 99


def test_stay_connected_is_the_global_default_for_always_on(config):
    config["stay_connected"] = True
    assert settings.get_guild_settings(config, GUILD_ID)["always_on"] is True


# --------------------------------------------------------------------------- #
# Clamping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [(-40, settings.MIN_VOLUME), (0, 0), (60, 60), (150, 150), (900, settings.MAX_VOLUME)],
)
def test_volume_is_clamped(value, expected):
    assert settings.clamp_volume(value) == expected


@pytest.mark.parametrize("value", ["loud", None, "", [], object()])
def test_nonsense_volume_falls_back(value):
    assert settings.clamp_volume(value, fallback=55) == 55


def test_volume_rounds_rather_than_truncating():
    assert settings.clamp_volume(59.6) == 60


@pytest.mark.parametrize("value,expected", [(-5, 0), (0, 0), (10, 10), (99999, settings.MAX_IDLE_MINUTES)])
def test_idle_minutes_are_clamped(value, expected):
    assert settings.clamp_idle_minutes(value) == expected


def test_nonsense_idle_minutes_fall_back():
    assert settings.clamp_idle_minutes("soon", fallback=7) == 7


@pytest.mark.parametrize("value,expected", [
    ("off", "off"),
    ("station", "station"),
    ("queue", "queue"),
    ("track", "station"),
    ("one", "station"),
    ("all", "queue"),
    ("playlist", "queue"),
    ("STATION", "station"),
    ("  queue  ", "queue"),
    ("nonsense", "off"),
    (None, "off"),
])
def test_loop_modes_are_normalised(value, expected):
    assert settings.normalise_loop_mode(value) == expected


def test_loop_mode_fallback_is_validated():
    assert settings.normalise_loop_mode("nope", fallback="also-nope") == "off"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_set_guild_settings_creates_an_entry(config):
    updated = settings.set_guild_settings(config, GUILD_ID, {"volume": 42})
    assert updated["guilds"][str(GUILD_ID)]["volume"] == 42
    # The caller's dict is not mutated in place: the bot swaps configs wholesale.
    assert "guilds" not in config or config["guilds"] == {}


def test_set_guild_settings_preserves_other_guilds(config):
    config["guilds"] = {"1": {"volume": 10}, str(GUILD_ID): {"volume": 20}}
    updated = settings.set_guild_settings(config, GUILD_ID, {"volume": 30})
    assert updated["guilds"]["1"]["volume"] == 10
    assert updated["guilds"][str(GUILD_ID)]["volume"] == 30


def test_set_guild_settings_preserves_unknown_keys_from_another_version(config):
    config["guilds"] = {str(GUILD_ID): {"volume": 10, "future_setting": "keep me"}}
    updated = settings.set_guild_settings(config, GUILD_ID, {"volume": 20})
    assert updated["guilds"][str(GUILD_ID)]["future_setting"] == "keep me"


def test_set_guild_settings_rejects_unknown_keys(config):
    with pytest.raises(ValueError) as error:
        settings.set_guild_settings(config, GUILD_ID, {"volumne": 40})
    assert "volumne" in str(error.value)


def test_set_guild_settings_clamps_what_it_writes(config):
    updated = settings.set_guild_settings(config, GUILD_ID, {"volume": 4000, "idle_minutes": -3})
    assert updated["guilds"][str(GUILD_ID)]["volume"] == settings.MAX_VOLUME
    assert updated["guilds"][str(GUILD_ID)]["idle_minutes"] == 0


def test_set_guild_settings_normalises_booleans(config):
    updated = settings.set_guild_settings(config, GUILD_ID, {"autostart": "yes", "always_on": 0, "announce": None})
    stored = updated["guilds"][str(GUILD_ID)]
    assert stored["autostart"] is True
    assert stored["always_on"] is False
    assert stored["announce"] is False


def test_set_guild_settings_normalises_the_loop_mode(config):
    updated = settings.set_guild_settings(config, GUILD_ID, {"loop_mode": "track"})
    assert updated["guilds"][str(GUILD_ID)]["loop_mode"] == "station"


def test_an_empty_station_id_clears_the_override_back_to_the_default(config):
    entry(config, station_id="chillhop")
    updated = settings.set_guild_settings(config, GUILD_ID, {"station_id": ""})
    assert settings.get_guild_settings(updated, GUILD_ID)["station_id"] == config["default_station"]


def test_station_ids_are_trimmed(config):
    updated = settings.set_guild_settings(config, GUILD_ID, {"station_id": "  chillhop  "})
    assert updated["guilds"][str(GUILD_ID)]["station_id"] == "chillhop"


def test_an_explicit_none_clears_an_id(config):
    entry(config, voice_channel_id=5)
    updated = settings.set_guild_settings(config, GUILD_ID, {"voice_channel_id": None})
    assert settings.get_guild_settings(updated, GUILD_ID)["voice_channel_id"] is None


def test_channel_status_can_be_turned_off_per_guild(config):
    updated = settings.set_guild_settings(config, GUILD_ID, {"channel_status": False})
    assert settings.get_guild_settings(updated, GUILD_ID)["channel_status"] is False


def test_global_channel_status_default_is_honoured(config):
    config["channel_status"] = False
    assert settings.get_guild_settings(config, GUILD_ID)["channel_status"] is False


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_configured_guild_ids_returns_ints_in_order(config):
    config["guilds"] = {"11": {}, "22": {}, "not-an-id": {}}
    assert settings.configured_guild_ids(config) == [11, 22]


def test_configured_guild_ids_handles_a_broken_map(config):
    config["guilds"] = None
    assert settings.configured_guild_ids(config) == []


def test_autostart_guilds_needs_both_the_flag_and_a_channel(config):
    config["guilds"] = {
        "11": {"autostart": True, "voice_channel_id": 5},
        "22": {"autostart": True},
        "33": {"autostart": False, "voice_channel_id": 5},
    }
    assert settings.autostart_guilds(config) == [11]


def test_autostart_is_empty_by_default(config):
    assert settings.autostart_guilds(config) == []


def test_get_guild_entry_returns_a_copy(config):
    config["guilds"] = {str(GUILD_ID): {"volume": 10}}
    fetched = settings.get_guild_entry(config, GUILD_ID)
    fetched["volume"] = 99
    assert config["guilds"][str(GUILD_ID)]["volume"] == 10
