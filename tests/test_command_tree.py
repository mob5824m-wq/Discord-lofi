"""The command surface: names, signatures, permission tiers and documentation.

The tree is built without a token - discord.py needs nothing from Discord to
describe a command - which is what lets these tests assert that every command a
user can see is documented, tiered and guarded.
"""

from __future__ import annotations

import pytest
from discord import app_commands

import command_tree
import music


@pytest.fixture(scope="module")
def tree():
    return music.build_tree(command_tree._NullBot())


@pytest.fixture(scope="module")
def described(tree):
    return command_tree.describe(tree)


def commands(tree):
    return [command for command in tree.walk_commands() if not isinstance(command, app_commands.Group)]


def by_name(described):
    return {row["qualifiedName"]: row for row in described}


# --------------------------------------------------------------------------- #
# The tree itself
# --------------------------------------------------------------------------- #
def test_the_top_level_command_is_lofi(tree):
    assert tree.name == "lofi"
    assert "voice channel" in tree.description


def test_the_tree_is_guild_only(tree):
    """Discord ignores guild_only on subcommands, so it has to be on the groups;
    otherwise /lofi is offered in a DM where nothing can work."""
    assert tree.guild_only is True
    for group in tree.walk_commands():
        if isinstance(group, app_commands.Group):
            assert group.guild_only is True


def test_every_documented_command_count(tree, described):
    assert len(described) == len(commands(tree)) == 30


def test_command_names_are_unique(described):
    names = [row["qualifiedName"] for row in described]
    assert len(names) == len(set(names))


def test_the_expected_surface_exists(described):
    names = set(by_name(described))
    assert {
        "lofi play", "lofi pause", "lofi resume", "lofi skip", "lofi stop", "lofi leave",
        "lofi join", "lofi volume", "lofi loop", "lofi queue", "lofi studio", "lofi now",
        "lofi stations", "lofi upnext", "lofi status", "lofi library",
        "lofi setup station", "lofi setup voice_channel", "lofi setup text_channel",
        "lofi setup volume", "lofi setup idle", "lofi setup autostart",
        "lofi setup always_on", "lofi setup dj_role", "lofi setup announce",
        "lofi setup status_line", "lofi setup reset",
        "lofi station add", "lofi station remove", "lofi station default",
    } == names


def test_there_are_two_subcommand_groups(described):
    assert command_tree.groups(described) == ["lofi setup", "lofi station"]


def test_groups_themselves_are_not_listed_as_commands(described):
    assert "lofi setup" not in by_name(described)
    assert "lofi station" not in by_name(described)


def test_every_command_has_a_description(described):
    missing = [row["qualifiedName"] for row in described if not row["description"].strip()]
    assert missing == []


def test_descriptions_are_short_enough_for_discord(described):
    """Discord truncates at 100 characters; a longer one is a silent bug."""
    too_long = [row["qualifiedName"] for row in described if len(row["description"]) > 100]
    assert too_long == []


def test_descriptions_do_not_start_with_a_capital_only_to_look_official(described):
    for row in described:
        assert row["description"].strip(), row["qualifiedName"]


def test_every_command_is_json_serialisable(described):
    import json

    json.dumps(described)


def test_options_exclude_self_and_interaction(described):
    for row in described:
        names = [option["name"] for option in row["options"]]
        assert "self" not in names and "interaction" not in names


def test_option_descriptions_exist(described):
    empty = [
        (row["qualifiedName"], option["name"])
        for row in described
        for option in row["options"]
        if not option["description"].strip()
    ]
    assert empty == []


# --------------------------------------------------------------------------- #
# Signatures
# --------------------------------------------------------------------------- #
def test_a_command_with_no_options_has_a_bare_signature(described):
    assert by_name(described)["lofi pause"]["signature"] == "/lofi pause"


def test_optional_options_are_bracketed(described):
    assert by_name(described)["lofi play"]["signature"] == "/lofi play [station] [channel]"


def test_required_options_are_not_bracketed(described):
    assert by_name(described)["lofi volume"]["signature"] == "/lofi volume percent"
    assert by_name(described)["lofi station add"]["signature"] == "/lofi station add name url"


def test_choices_are_rendered_into_the_signature(described):
    """The signature doubles as documentation: mode:off/station/queue needs no README."""
    assert by_name(described)["lofi loop"]["signature"] == "/lofi loop mode:off/station/queue"


def test_a_long_choice_list_is_truncated_in_the_signature():
    class Choice:
        def __init__(self, value):
            self.name = value
            self.value = value

    class Command:
        qualified_name = "lofi example"

        class Parameter:
            name = "many"
            required = True
            choices = [Choice(str(index)) for index in range(9)]

        parameters = [Parameter()]

    signature = command_tree.signature(Command())
    assert signature.endswith("…")
    assert len(signature) < 80


def test_the_setup_signatures_read_like_the_real_thing(described):
    rows = by_name(described)
    assert rows["lofi setup station"]["signature"] == "/lofi setup station station"
    assert rows["lofi setup voice_channel"]["signature"] == "/lofi setup voice_channel channel"
    assert rows["lofi setup dj_role"]["signature"] == "/lofi setup dj_role [role]"
    assert rows["lofi setup reset"]["signature"] == "/lofi setup reset"


def test_signature_of_a_command_with_no_parameters_object():
    class Bare:
        qualified_name = "lofi bare"
        parameters = None

    assert command_tree.signature(Bare()) == "/lofi bare"


# --------------------------------------------------------------------------- #
# Permission tiers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,tier", [
    ("lofi play", command_tree.TIER_TRANSPORT),
    ("lofi pause", command_tree.TIER_TRANSPORT),
    ("lofi skip", command_tree.TIER_TRANSPORT),
    ("lofi stop", command_tree.TIER_TRANSPORT),
    ("lofi volume", command_tree.TIER_TRANSPORT),
    ("lofi loop", command_tree.TIER_TRANSPORT),
    ("lofi queue", command_tree.TIER_TRANSPORT),
    ("lofi studio", command_tree.TIER_TRANSPORT),
    ("lofi join", command_tree.TIER_TRANSPORT),
    ("lofi leave", command_tree.TIER_TRANSPORT),
    ("lofi now", command_tree.TIER_INFO),
    ("lofi stations", command_tree.TIER_INFO),
    ("lofi status", command_tree.TIER_INFO),
    ("lofi library", command_tree.TIER_INFO),
    ("lofi upnext", command_tree.TIER_INFO),
    ("lofi setup station", command_tree.TIER_CONFIGURATION),
    ("lofi setup reset", command_tree.TIER_CONFIGURATION),
    ("lofi station add", command_tree.TIER_CONFIGURATION),
    ("lofi station remove", command_tree.TIER_CONFIGURATION),
])
def test_the_tier_of_a_command(name, tier):
    assert command_tree.tier(name) == tier


def test_a_nested_command_inherits_its_group_s_tier():
    """A new /lofi setup option must not silently become information-only."""
    assert command_tree.tier("lofi setup something_new") == command_tree.TIER_CONFIGURATION
    assert command_tree.tier("lofi station something_new") == command_tree.TIER_CONFIGURATION


def test_an_unknown_command_is_information_only():
    """The safe default: a new command is readable by everyone until tiered."""
    assert command_tree.tier("lofi brand_new") == command_tree.TIER_INFO


def test_tier_lookup_ignores_case_and_padding():
    assert command_tree.tier("  LOFI Play ") == command_tree.TIER_TRANSPORT


def test_every_tier_has_a_human_label():
    for tier in (command_tree.TIER_TRANSPORT, command_tree.TIER_CONFIGURATION, command_tree.TIER_INFO):
        assert command_tree.TIER_LABELS[tier]


def test_configuration_commands_are_guarded_by_discord_too(tree):
    """``default_permissions(manage_guild=True)` hides them from people who
    cannot use them; the runtime check in music.py is the enforcement."""
    for command in commands(tree):
        if command_tree.tier(command.qualified_name) == command_tree.TIER_CONFIGURATION:
            assert command.default_permissions is not None, command.qualified_name
            assert command.default_permissions.manage_guild is True, command.qualified_name


def test_transport_commands_are_not_locked_behind_manage_server(tree):
    """Anyone in the voice channel should be able to skip a track."""
    for command in commands(tree):
        if command_tree.tier(command.qualified_name) == command_tree.TIER_TRANSPORT:
            assert command.default_permissions is None, command.qualified_name


def test_configuration_tiers_match_the_runtime_guard(described, tree):
    """The tier table and the code must agree, or the docs lie."""
    for command in commands(tree):
        tier = command_tree.tier(command.qualified_name)
        callback = command.callback
        source_names = getattr(callback, "__qualname__", "")
        if tier == command_tree.TIER_CONFIGURATION:
            assert "SetupGroup" in source_names or "StationGroup" in source_names, source_names


# --------------------------------------------------------------------------- #
# Documentation output
# --------------------------------------------------------------------------- #
def test_the_reference_table_covers_every_command(described):
    table = command_tree.reference_markdown(described)
    for row in described:
        assert row["signature"].strip("`") in table


def test_the_reference_table_has_a_header(described):
    table = command_tree.reference_markdown(described)
    lines = table.splitlines()
    assert lines[0] == "| Command | What it does | Who can use it |"
    assert lines[1] == "| --- | --- | --- |"
    assert len(lines) == 2 + len(described)


def test_the_reference_table_says_who_may_use_a_command(described):
    table = command_tree.reference_markdown(described)
    setup_line = next(line for line in table.splitlines() if "`/lofi setup station" in line)
    assert "Manage Server" in setup_line


def test_a_pipe_in_a_description_cannot_break_the_table():
    described = [{"qualifiedName": "lofi odd", "signature": "/lofi odd", "description": "a | b",
                  "tier": command_tree.TIER_INFO, "options": []}]
    table = command_tree.reference_markdown(described)
    assert table.count("| a / b |") == 1


def test_command_names_follow_the_table(described):
    assert command_tree.command_names(described) == [row["qualifiedName"] for row in described]


def test_command_names_can_be_computed_from_nothing():
    assert "lofi play" in command_tree.command_names()


def test_describe_works_with_no_bot_at_all():
    """Documentation and tests must not need a token."""
    assert len(command_tree.describe()) == 30


def test_build_returns_the_same_tree_the_bot_installs(tree):
    assert isinstance(tree, music.LofiGroup)
    assert isinstance(command_tree.build(command_tree._NullBot()), music.LofiGroup)


def test_the_null_bot_exposes_what_the_groups_read():
    """If a group reads an attribute the null bot lacks, describe() would fail."""
    null_bot = command_tree._NullBot()
    assert isinstance(null_bot.config, dict)
    assert null_bot.voice_ready is False
    assert null_bot.players.get(1) is None
