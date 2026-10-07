"""The ``/lofi`` command tree, documented and introspectable.

Everything the bot does from Discord hangs off one group::

    /lofi play        join a voice channel and start a station
    /lofi pause       /lofi resume       /lofi skip
    /lofi stop        /lofi leave        /lofi join
    /lofi volume      /lofi loop         /lofi queue
    /lofi studio      play a beat rendered on this machine (no network)
    /lofi now         /lofi upnext       /lofi stations
    /lofi status      diagnostics: playback, settings, voice permissions
    /lofi library     what is in the bot's local music folder
    /lofi setup …     this server's settings      (Manage Server)
    /lofi station …   add / remove / default      (Manage Server)

The group object itself lives in :mod:`music`, next to the guards and embeds it
uses. This module exists because three other places need to *describe* the tree
rather than run it:

* the dashboard's Commands page, which renders the same list a moderator would
  see in Discord, so the docs cannot drift from the code;
* the test suite, which asserts the tree still builds, that no command name
  collides, and that every configuration command re-checks permissions at run
  time;
* :file:`README.md`, whose command table is generated from :func:`reference_markdown`.

Permissions come in two tiers, and both are enforced in :mod:`music` when the
command runs rather than by Discord's ``default_member_permissions`` alone -
see that module's docstring for why.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from discord import app_commands

import music


TIER_TRANSPORT = "transport"
TIER_CONFIGURATION = "configuration"
TIER_INFO = "information"

#: Which tier each command belongs to, by qualified name. Anything not listed
#: is information-only (safe for everybody).
TIERS: dict[str, str] = {
    "lofi play": TIER_TRANSPORT,
    "lofi pause": TIER_TRANSPORT,
    "lofi resume": TIER_TRANSPORT,
    "lofi skip": TIER_TRANSPORT,
    "lofi stop": TIER_TRANSPORT,
    "lofi leave": TIER_TRANSPORT,
    "lofi join": TIER_TRANSPORT,
    "lofi volume": TIER_TRANSPORT,
    "lofi loop": TIER_TRANSPORT,
    "lofi queue": TIER_TRANSPORT,
    "lofi studio": TIER_TRANSPORT,
    "lofi setup": TIER_CONFIGURATION,
    "lofi station": TIER_CONFIGURATION,
}

TIER_LABELS = {
    TIER_TRANSPORT: "Anyone in the same voice channel (or the DJ role / Manage Server)",
    TIER_CONFIGURATION: "Manage Server or Administrator",
    TIER_INFO: "Everyone",
}


def build(bot: Any) -> music.LofiGroup:
    """Construct the tree for a bot instance."""
    return music.build_tree(bot)


def tier(qualified_name: str) -> str:
    """The permission tier of a command, matching on the name or its parent."""
    name = qualified_name.strip().lower()
    if name in TIERS:
        return TIERS[name]
    for known, value in TIERS.items():
        if name.startswith(known + " "):
            return value
    return TIER_INFO


def signature(command: Any) -> str:
    """``/lofi play [station] [channel]`` - the shape a user types.

    Required options are shown bare, optional ones in brackets, and a choice
    option lists its values so the signature doubles as documentation.
    """
    parts: list[str] = []
    for parameter in getattr(command, "parameters", []) or []:
        if parameter.name in {"self", "interaction"}:
            continue
        choices = getattr(parameter, "choices", None)
        if choices:
            values = "/".join(str(choice.value) for choice in choices[:4])
            if len(choices) > 4:
                values += "/…"
            rendered = f"{parameter.name}:{values}"
        else:
            rendered = parameter.name
        parts.append(rendered if parameter.required else f"[{rendered}]")
    return f"/{command.qualified_name}" + (f" {' '.join(parts)}" if parts else "")


def describe(group: Optional[Any] = None, bot: Any = None) -> list[dict[str, Any]]:
    """Every command in the tree as plain data, for the dashboard and tests."""
    root = group if group is not None else music.build_tree(bot if bot is not None else _NullBot())
    rows: list[dict[str, Any]] = []
    for command in root.walk_commands():
        if isinstance(command, app_commands.Group):
            continue
        rows.append(
            {
                "name": command.name,
                "qualifiedName": command.qualified_name,
                "description": command.description or "",
                "signature": signature(command),
                "tier": tier(command.qualified_name),
                "options": [
                    {
                        "name": parameter.name,
                        "required": bool(parameter.required),
                        "description": parameter.description or "",
                        "choices": [
                            {"name": choice.name, "value": choice.value}
                            for choice in (getattr(parameter, "choices", None) or [])
                        ],
                    }
                    for parameter in (getattr(command, "parameters", []) or [])
                    if parameter.name not in {"self", "interaction"}
                ],
            }
        )
    return rows


def groups(described: Optional[Iterable[dict[str, Any]]] = None) -> list[str]:
    """The subcommand groups in the tree (``lofi setup``, ``lofi station``)."""
    rows = list(described) if described is not None else describe()
    seen: list[str] = []
    for row in rows:
        parts = row["qualifiedName"].split(" ")
        if len(parts) > 2:
            parent = " ".join(parts[:2])
            if parent not in seen:
                seen.append(parent)
    return seen


def reference_markdown(described: Optional[Iterable[dict[str, Any]]] = None) -> str:
    """A markdown table of the tree, used to keep the README honest."""
    rows = list(described) if described is not None else describe()
    lines = [
        "| Command | What it does | Who can use it |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        who = TIER_LABELS[row["tier"]]
        description = row["description"].replace("|", "/")
        lines.append(f"| `{row['signature']}` | {description} | {who} |")
    return "\n".join(lines)


def command_names(described: Optional[Iterable[dict[str, Any]]] = None) -> list[str]:
    """Every qualified command name, in tree order."""
    rows = list(described) if described is not None else describe()
    return [row["qualifiedName"] for row in rows]


class _NullBot:
    """A stand-in bot so :func:`describe` works with nothing connected.

    Building the tree does not touch Discord, so documentation and tests can
    ask for it without a token - but the group constructor wants an object to
    read ``config`` and ``players`` from later, when a command actually runs.
    """

    config: dict = {}
    voice_ready = False

    class players:  # noqa: N801 - mirrors the real attribute name
        @staticmethod
        def get(guild_id: Any, create: bool = True) -> None:
            return None

        @staticmethod
        async def run_blocking(func: Any, *args: Any) -> Any:
            return func(*args)


__all__ = [
    "TIER_CONFIGURATION",
    "TIER_INFO",
    "TIER_LABELS",
    "TIER_TRANSPORT",
    "TIERS",
    "build",
    "command_names",
    "describe",
    "groups",
    "reference_markdown",
    "signature",
    "tier",
]
