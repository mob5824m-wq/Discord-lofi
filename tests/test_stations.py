"""The station registry: kinds, validation, custom stations, studio moods."""

from __future__ import annotations

import pytest

import paths
import stations
from stations import Station


def custom(**fields) -> Station:
    """A non-built-in station with sane defaults for the field under test."""
    base = dict(id="mine", name="Mine", kind=stations.KIND_STREAM, url="https://example.com/a.mp3")
    base.update(fields)
    return Station(**base)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    ("https://youtu.be/jfKfPfyJRdk", "youtube"),
    ("https://www.youtube.com/watch?v=jfKfPfyJRdk", "youtube"),
    ("https://music.youtube.com/watch?v=abc", "youtube"),
    ("http://ice1.somafm.com/groovesalad-128-mp3", "stream"),
    ("https://play.lofi.girl/lofigirl/v3/aac", "stream"),
    ("library:music", "library"),
    ("folder:/srv/lofi", "library"),
    ("local:./music", "library"),
    ("file:///srv/lofi/x.mp3", "library"),
    ("/srv/lofi", "library"),
    ("./music", "library"),
    ("generative", "generative"),
])
def test_classify_url(url, expected):
    assert stations.classify_url(url) == expected


@pytest.mark.parametrize("url", ["", "   ", None, "not a url", "spotify:track:abc", "ftp://example.com/x.mp3"])
def test_classify_url_returns_none_for_things_it_cannot_play(url):
    """Nothing raises here: the caller decides how to explain it."""
    assert stations.classify_url(url) is None


@pytest.mark.parametrize("url", [
    "http://localhost:8080/stream",
    "http://127.0.0.1/x.mp3",
    "http://192.168.1.5/x.mp3",
    "http://10.0.0.1/x.mp3",
    "http://172.16.0.1/x.mp3",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/x.mp3",
    "http://0.0.0.0/x.mp3",
])
def test_local_network_addresses_are_flagged(url):
    """A server should not be pointed at a private address: SSRF, and it would not play anyway."""
    assert stations.is_private_url(url) is True


@pytest.mark.parametrize("url", ["http://example.com/x.mp3", "https://play.lofi.girl/aac", "https://ice2.somafm.com/g"])
def test_public_addresses_are_not_flagged(url):
    assert stations.is_private_url(url) is False


def test_an_address_that_cannot_be_parsed_counts_as_private():
    assert stations.is_private_url("http://") is True


# --------------------------------------------------------------------------- #
# validate_station: the text that reaches a Discord reply and a dashboard toast
# --------------------------------------------------------------------------- #
def test_validate_derives_an_id_and_classifies_the_url():
    station_id, name, kind = stations.validate_station(
        name="Radio Paradise", url="https://example.com/stream.mp3"
    )
    assert station_id == "radio-paradise"
    assert name == "Radio Paradise"
    assert kind == "stream"


def test_validate_honours_an_explicit_kind():
    assert stations.validate_station(name="x", url="https://example.com/a", kind="library")[2] == "library"


def test_validate_requires_a_name():
    with pytest.raises(ValueError, match="Give the station a name"):
        stations.validate_station(name="   ", url="https://example.com/a.mp3")


def test_validate_requires_a_url():
    with pytest.raises(ValueError, match="Give the station a URL"):
        stations.validate_station(name="No URL", url="")


def test_validate_collapses_whitespace_in_the_name():
    assert stations.validate_station(name="  Rainy   Cafe \n", url="https://example.com/a.mp3")[1] == "Rainy Cafe"


def test_validate_rejects_a_name_that_is_too_long():
    with pytest.raises(ValueError, match=f"{stations.MAX_STATION_NAME} characters"):
        stations.validate_station(name="x" * (stations.MAX_STATION_NAME + 1), url="https://example.com/a.mp3")


def test_validate_rejects_a_url_it_cannot_play():
    with pytest.raises(ValueError, match="does not look like something Lofi can play"):
        stations.validate_station(name="x", url="not a url")


def test_validate_rejects_a_url_that_is_too_long():
    with pytest.raises(ValueError, match="too long"):
        stations.validate_station(name="x", url="https://example.com/" + "a" * 900)


def test_validate_refuses_a_private_address_for_a_stream():
    with pytest.raises(ValueError, match="private network"):
        stations.validate_station(name="lan", url="http://192.168.1.5/x.mp3")


def test_validate_can_be_told_to_allow_a_private_address():
    """A LAN-only install is a legitimate choice; the operator has to opt in."""
    station_id, _, kind = stations.validate_station(
        name="lan", url="http://192.168.1.5/x.mp3", allow_private=True
    )
    assert station_id == "lan" and kind == "stream"


def test_a_local_path_is_never_treated_as_a_private_address():
    """library: sources are filesystem paths, so the SSRF guard must not apply."""
    assert stations.validate_station(name="files", url="library:/srv/lofi")[2] == "library"


def test_the_id_is_truncated_to_the_station_id_limit():
    # The name has to be legal first: a 200-character name is rejected outright.
    station_id, _, _ = stations.validate_station(name="x" * stations.MAX_STATION_NAME, url="https://example.com/a.mp3")
    assert len(station_id) <= stations.MAX_STATION_ID


# --------------------------------------------------------------------------- #
# slugify
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    ("Radio Paradise", "radio-paradise"),
    ("  Spaced   Out  ", "spaced-out"),
    ("Ünïcödé ✓", "unicode"),
    ("!!!", "station"),
    ("", "station"),
    (None, "station"),
])
def test_slugify(value, expected):
    assert stations.slugify(value) == expected


def test_slugify_truncates():
    assert len(stations.slugify("a" * 200)) == stations.MAX_STATION_ID


def test_slugify_honours_a_custom_fallback():
    assert stations.slugify("***", fallback="custom") == "custom"


# --------------------------------------------------------------------------- #
# Built-in registry
# --------------------------------------------------------------------------- #
def test_built_in_ids_are_unique():
    ids = [station.id for station in stations.BUILT_IN_STATIONS]
    assert len(ids) == len(set(ids))


def test_every_built_in_is_well_formed():
    for station in stations.BUILT_IN_STATIONS:
        assert station.id and station.name and station.art
        assert station.kind in stations.KINDS
        # A station needs something to play: a URL, or a search query to find one.
        assert station.url or station.search, station.id


def test_every_built_in_marks_itself_builtin():
    assert all(station.builtin for station in stations.BUILT_IN_STATIONS)


def test_a_youtube_station_without_a_link_is_found_by_search():
    sleepy = stations.BUILT_IN_BY_ID["sleepy-lofi"]
    assert sleepy.url == ""
    assert sleepy.search


def test_live_stations_report_that_they_never_end():
    for station_id in ("lofi-girl", "groove-salad"):
        station = stations.BUILT_IN_BY_ID[station_id]
        assert station.is_live is True
        assert station.needs_network is True
    for station_id in ("library", "generative"):
        station = stations.BUILT_IN_BY_ID[station_id]
        assert station.is_live is False
        assert station.needs_network is False


def test_get_stations_returns_the_built_ins_with_no_config():
    assert [station.id for station in stations.get_stations(None)] == [
        station.id for station in stations.BUILT_IN_STATIONS
    ]


def test_a_custom_station_is_appended_after_the_built_ins(config):
    config["stations"] = [custom().to_dict()]
    listed = stations.get_stations(config)
    assert listed[-1].id == "mine"
    assert listed[0].id == stations.BUILT_IN_STATIONS[0].id


def test_a_custom_station_can_replace_a_built_in(config):
    """How an operator fixes a stale YouTube link without editing code."""
    config["stations"] = [custom(id="lofi-girl", name="My Lofi", url="https://example.com/a.mp3").to_dict()]
    listed = stations.get_stations(config)
    assert len([station for station in listed if station.id == "lofi-girl"]) == 1
    assert stations.find_station(config, "lofi-girl").name == "My Lofi"


def test_a_replaced_station_keeps_its_position_in_the_list(config):
    config["stations"] = [custom(id="groove-salad", name="Renamed").to_dict()]
    ids = [station.id for station in stations.get_stations(config)]
    assert ids.index("groove-salad") == [station.id for station in stations.BUILT_IN_STATIONS].index("groove-salad")


def test_a_dictionary_shaped_stations_map_is_accepted(config):
    """Tolerate ``{"id": {...}}``: hand-editing a config makes that shape easily."""
    config["stations"] = {"mine": {"name": "Mine", "url": "https://example.com/a.mp3", "kind": "stream"}}
    assert stations.find_station(config, "mine") is not None


def test_a_broken_stations_list_is_ignored(config):
    config["stations"] = "not a list"
    assert len(stations.get_stations(config)) == len(stations.BUILT_IN_STATIONS)


def test_a_broken_entry_is_skipped_not_fatal(config):
    config["stations"] = [{"id": "bad"}, custom().to_dict()]
    listed = stations.get_stations(config)
    assert all(station.id != "bad" for station in listed)
    assert any(station.id == "mine" for station in listed)


def test_custom_stations_never_claim_to_be_builtin(config):
    config["stations"] = [custom().to_dict()]
    assert stations.find_station(config, "mine").builtin is False


# --------------------------------------------------------------------------- #
# find_station
# --------------------------------------------------------------------------- #
def test_find_by_id_name_and_emoji_prefixed_name(config):
    assert stations.find_station(config, "groove-salad").kind == "stream"
    assert stations.find_station(config, "Groove Salad").kind == "stream"
    assert stations.find_station(config, "🥗 Groove Salad").kind == "stream"


def test_find_is_case_insensitive(config):
    assert stations.find_station(config, "LOFI-GIRL") is not None


def test_find_falls_back_to_a_partial_name_match(config):
    assert stations.find_station(config, "groove").id == "groove-salad"


def test_find_turns_a_bare_link_into_a_station(config):
    """``/lofi play https://...`` works without registering anything first."""
    found = stations.find_station(config, "https://example.com/some-file.flac")
    assert found is not None
    assert found.url == "https://example.com/some-file.flac"
    assert found.art == "🔗"


def test_a_bare_youtube_link_is_named_after_its_video_id(config):
    found = stations.find_station(config, "https://www.youtube.com/watch?v=jfKfPfyJRdk")
    assert found.kind == "youtube"
    assert "jfKfPfyJRdk" in found.name


def test_a_bare_short_link_is_named_after_its_host(config):
    found = stations.find_station(config, "https://ice1.somafm.com/groovesalad-128-mp3")
    assert found.name == "ice1.somafm.com"


def test_find_returns_none_for_nonsense(config):
    assert stations.find_station(config, "zzz definitely not a station zzz") is None
    assert stations.find_station(config, "") is None
    assert stations.find_station(config, None) is None


def test_find_accepts_a_station_instance(config):
    station = stations.find_station(config, "library")
    assert stations.find_station(config, station) is not None


# --------------------------------------------------------------------------- #
# default_station
# --------------------------------------------------------------------------- #
def test_the_default_is_the_documented_one(config):
    assert stations.default_station(config).id == stations.DEFAULT_STATION_ID


def test_the_default_honours_the_config(config):
    config["default_station"] = "sleepy-lofi"
    assert stations.default_station(config).id == "sleepy-lofi"


def test_an_unknown_default_falls_back_rather_than_returning_none(config):
    config["default_station"] = "nope-not-real"
    assert stations.default_station(config).id == stations.DEFAULT_STATION_ID


def test_the_default_survives_an_empty_config():
    assert stations.default_station({}) is not None
    assert stations.default_station(None) is not None


def test_a_studio_mood_can_be_the_default(config):
    """Consistent with ``/lofi play studio-cafe``: one resolver, one meaning."""
    config["default_station"] = "studio-cafe"
    resolved = stations.default_station(config)
    assert resolved.id == "studio-cafe"
    assert resolved.kind == "generative"


# --------------------------------------------------------------------------- #
# Saving and removing custom stations
# --------------------------------------------------------------------------- #
def test_save_custom_station_round_trips(tmp_state):
    updated = stations.save_custom_station(paths.DEFAULT_CONFIG, custom(name="Radio Paradise", art="🎙"))
    assert updated["stations"][0]["id"] == "mine"
    assert stations.find_station(updated, "mine").art == "🎙"


def test_save_custom_station_does_not_mutate_the_input(tmp_state):
    original = dict(paths.DEFAULT_CONFIG)
    stations.save_custom_station(original, custom())
    assert original == paths.DEFAULT_CONFIG


def test_saving_again_replaces_rather_than_duplicates(tmp_state):
    once = stations.save_custom_station(paths.DEFAULT_CONFIG, custom())
    twice = stations.save_custom_station(once, custom(name="Mine Again", url="https://example.com/b.mp3"))
    assert len(twice["stations"]) == 1
    assert stations.find_station(twice, "mine").name == "Mine Again"


def test_saving_the_maximum_number_of_stations_is_allowed(tmp_state):
    config = dict(paths.DEFAULT_CONFIG)
    for index in range(stations.MAX_CUSTOM_STATIONS):
        config = stations.save_custom_station(config, custom(id=f"station-{index}", name=f"Station {index}"))
    assert len(config["stations"]) == stations.MAX_CUSTOM_STATIONS


def test_saving_one_too_many_is_refused_with_a_fix(tmp_state):
    config = dict(paths.DEFAULT_CONFIG, stations=[
        {"id": f"s{index}", "name": f"S{index}", "url": "https://example.com/a.mp3", "kind": "stream"}
        for index in range(stations.MAX_CUSTOM_STATIONS)
    ])
    with pytest.raises(ValueError, match="remove one first"):
        stations.save_custom_station(config, custom(id="one-too-many"))


def test_remove_custom_station_deletes_it(tmp_state):
    saved = stations.save_custom_station(paths.DEFAULT_CONFIG, custom())
    updated, removed = stations.remove_custom_station(saved, "mine")
    assert removed is True
    assert stations.find_station(updated, "mine") is None


def test_removing_a_replacement_restores_the_built_in(tmp_state):
    """``remove`` on a built-in id drops the override hiding it."""
    saved = stations.save_custom_station(paths.DEFAULT_CONFIG, custom(id="lofi-girl", name="My Lofi"))
    updated, removed = stations.remove_custom_station(saved, "lofi-girl")
    assert removed is True
    assert stations.find_station(updated, "lofi-girl").name == "Lofi Girl"


def test_removing_an_unknown_id_reports_that_nothing_happened(tmp_state):
    updated, removed = stations.remove_custom_station(dict(paths.DEFAULT_CONFIG), "ghost")
    assert removed is False
    assert len(stations.get_stations(updated)) == len(stations.BUILT_IN_STATIONS)


# --------------------------------------------------------------------------- #
# Studio moods
# --------------------------------------------------------------------------- #
def test_a_studio_reference_becomes_a_station():
    station = stations.studio_station("studio-cafe")
    assert station is not None
    assert station.id == "studio-cafe"
    assert station.kind == "generative"
    assert station.url == "generative:cafe"
    assert station.name.startswith("Studio · ")


def test_a_studio_reference_is_case_insensitive():
    assert stations.studio_station("STUDIO-Cafe").id == "studio-cafe"


def test_an_unknown_studio_reference_uses_the_default_recipe():
    station = stations.studio_station("studio-not-real")
    assert station is not None and station.id.startswith("studio-")


def test_a_non_studio_reference_is_not_a_studio_station():
    assert stations.studio_station("lofi-girl") is None
    assert stations.studio_station("") is None
    assert stations.studio_station(None) is None


def test_find_station_resolves_a_studio_mood(config):
    assert stations.find_station(config, "studio-midnight").id == "studio-midnight"


def test_a_studio_station_is_never_saved_into_the_registry(config):
    """Five moods would be five more entries in every list for one parameter."""
    stations.find_station(config, "studio-cafe")
    assert all(station.id != "studio-cafe" for station in stations.get_stations(config))


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def test_to_dict_is_json_safe_and_camel_free_for_the_dashboard():
    station = custom(tags=("a", "b"))
    payload = station.to_dict()
    assert payload["tags"] == ["a", "b"]  # a tuple would break JSON callers
    assert set(payload) == {"id", "name", "kind", "url", "description", "search", "art", "builtin", "tags"}


def test_to_dict_round_trips():
    station = custom(description="desc", search="query", art="🎙", tags=("a",))
    assert stations.station_from_dict(station.to_dict()) == station


@pytest.mark.parametrize("raw", [None, "nope", 42, [], {}])
def test_station_from_dict_ignores_junk(raw):
    assert stations.station_from_dict(raw) is None


def test_station_from_dict_fills_in_missing_fields():
    station = stations.station_from_dict({"id": "a", "url": "https://example.com/a.mp3"})
    assert station.name == "https://example.com/a.mp3"  # the url is the only label available
    assert station.kind == "stream"
    assert station.art == "🎧"


def test_station_from_dict_truncates_and_caps_tags():
    station = stations.station_from_dict({
        "name": "x" * 200, "url": "https://example.com/a.mp3",
        "art": "🎧🎧🎧🎧🎧", "tags": ["a", "b", "c", "d", "e", "f", "g"],
    })
    assert len(station.name) == stations.MAX_STATION_NAME
    assert len(station.art) <= 8
    assert len(station.tags) == 5


def test_station_from_dict_recovers_a_kind_the_registry_does_not_know():
    station = stations.station_from_dict({"name": "x", "url": "https://example.com/a.mp3", "kind": "spotify"})
    assert station.kind == "stream"


def test_a_station_from_config_is_not_builtin():
    assert stations.station_from_dict(custom().to_dict()).builtin is False
