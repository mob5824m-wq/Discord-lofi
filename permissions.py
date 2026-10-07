"""Permission checks and the audit shown in ``/lofi status`` and the dashboard.

Discord answers "can the bot do X" per channel, not per server: a role can have
*Speak* everywhere and still be denied it in the one voice channel anyone
actually uses. So every check here resolves through
``channel.permissions_for(guild.me)`` when a channel is known, and falls back
to the guild-wide permissions when it is not.

The audit exists because the failure mode of a missing voice permission is
silent. A bot without *Speak* joins the channel, appears connected, plays
nothing, and logs nothing - which reads exactly like a broken ffmpeg install.
Naming the missing permission, and saying which one to tick, turns that into a
ten-second fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Requirement:
    """One permission the bot needs, and why."""

    key: str
    label: str
    required: bool
    reason: str


#: Everything Lofi asks for. ``priority_speaker`` is listed as *not* required
#: with an explanation, because people grant it expecting the music to duck
#: other voices - and it cannot: priority speaker is a desktop push-to-talk
#: keybind, there is no bot API for it, and a bot has no keybind to hold.
REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("view_channel", "View Channel", True, "needed to see the voice channel at all"),
    Requirement("connect", "Connect", True, "needed to join the voice channel"),
    Requirement("speak", "Speak", True, "without this the bot joins but is silent"),
    Requirement(
        "set_voice_channel_status",
        "Set Voice Channel Status",
        False,
        "lets the bot show the current track under the channel name",
    ),
    Requirement(
        "priority_speaker",
        "Priority Speaker",
        False,
        "has no effect for a bot - it is a desktop push-to-talk keybind, not an API",
    ),
    Requirement(
        "manage_channels",
        "Manage Channels",
        False,
        "only needed if you want the bot to move itself between channels",
    ),
)


def _permissions_for(bot_member: Any, channel: Any) -> Any:
    """Channel-aware permissions, falling back to guild-wide."""
    if channel is not None:
        try:
            return channel.permissions_for(bot_member)
        except Exception:  # a partial channel, or one the cache has lost
            pass
    guild = getattr(bot_member, "guild", None)
    return getattr(guild, "me", bot_member).guild_permissions if guild else None


def check(bot_member: Any, key: str, channel: Any = None) -> bool:
    """True when the bot holds ``key`` in ``channel`` (or guild-wide)."""
    permissions = _permissions_for(bot_member, channel)
    if permissions is None:
        return False
    return bool(getattr(permissions, key, False))


def missing(bot_member: Any, channel: Any = None) -> list[Requirement]:
    """The *required* permissions the bot does not have here."""
    return [item for item in REQUIREMENTS if item.required and not check(bot_member, item.key, channel)]


def can_play(bot_member: Any, channel: Any = None) -> Optional[str]:
    """``None`` when playback would work, otherwise a message naming the fix.

    Returned as a string rather than raised because the same answer is needed
    in three places with three different deliveries: a slash-command reply, a
    dashboard toast, and a startup log line.
    """
    if bot_member is None:
        return "The bot is not in that server yet."
    absent = missing(bot_member, channel)
    if not absent:
        return None
    names = ", ".join(item.label for item in absent)
    where = f"in #{getattr(channel, 'name', 'that channel')}" if channel is not None else "in that server"
    return (
        f"The bot is missing {names} {where}. Grant "
        f"{' and '.join(item.label for item in absent)} to its role "
        f"(server-wide or as a channel override) and try again."
    )


def audit(bot_member: Any, channel: Any = None) -> list[dict[str, Any]]:
    """Every requirement with its current state, for display."""
    rows: list[dict[str, Any]] = []
    for item in REQUIREMENTS:
        granted = check(bot_member, item.key, channel)
        rows.append(
            {
                "key": item.key,
                "label": item.label,
                "required": item.required,
                "granted": granted,
                "reason": item.reason,
                # A granted-but-useless permission is worth saying so, and so
                # is a missing optional one - both otherwise look like bugs.
                "state": "ok" if granted and item.key != "priority_speaker" else (
                    "no-effect" if granted and item.key == "priority_speaker" else (
                        "missing" if item.required else "optional"
                    )
                ),
            }
        )
    return rows


def summarise(bot_member: Any, channel: Any = None) -> str:
    """One-line human summary: ``Speak ✓ · Connect ✓ · Status ✓``."""
    parts = []
    for item in REQUIREMENTS[:4]:
        mark = "✓" if check(bot_member, item.key, channel) else "✗"
        parts.append(f"{item.label} {mark}")
    return " · ".join(parts)


__all__ = [
    "REQUIREMENTS",
    "Requirement",
    "audit",
    "can_play",
    "check",
    "missing",
    "summarise",
]
