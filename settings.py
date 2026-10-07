"""Per-guild playback settings, shared by the bot, the cog and the dashboard.

These are pure functions over the config dict: nothing here touches Discord or
the database, which is what makes them trivial to test and impossible to get
into an import cycle with :mod:`player` (which imports them) or
:mod:`dashboard` (which is handed them).

Two shapes have to keep working:

* the **global defaults** at the top level of ``config.json``
  (``default_station``, ``default_volume``, ...), which apply to every server;
* the **per-guild overrides** in ``guilds["<id>"]``, which win for that server
  and are what ``/lofi setup`` and the dashboard write.

:func:`get_guild_settings` flattens both into one view, so no caller has to
know which of the two a value came from. Guild ids are strings in JSON (a
64-bit snowflake cannot be a JSON object key otherwise) and ints everywhere
else; the helpers accept either.
"""

from __future__ import annotations

import logging
from typing import Any, Optional


logger = logging.getLogger("lofi.settings")

#: Hard limits, enforced here so the cog, the dashboard and a hand-edited
#: config file all agree on what is legal.
MIN_VOLUME = 0
MAX_VOLUME = 150
MIN_IDLE_MINUTES = 0
MAX_IDLE_MINUTES = 24 * 60
#: Anything above this is almost certainly a typo for a percentage; Discord's
#: own client caps at 200%.
DEFAULT_VOLUME = 60
DEFAULT_IDLE_MINUTES = 10

#: The keys a guild entry may hold. Anything else in a saved entry is preserved
#: on read (so an older or newer build's data is not destroyed) but ignored.
GUILD_SETTING_KEYS = (
    "station_id",
    "voice_channel_id",
    "text_channel_id",
    "autostart",
    "always_on",
    "volume",
    "idle_minutes",
    "dj_role_id",
    "announce",
    "loop_mode",
    "channel_status",
)

LOOP_MODES = ("off", "station", "queue")


def _guild_key(guild_id: Any) -> str:
    return str(int(guild_id))


def _as_int(value: Any) -> Optional[int]:
    """Parse a snowflake-ish value that may be ``None``, ``""`` or a string."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clamp_volume(value: Any, fallback: int = DEFAULT_VOLUME) -> int:
    """Volume as an integer percent in ``[MIN_VOLUME, MAX_VOLUME]``."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return int(fallback)
    return max(MIN_VOLUME, min(MAX_VOLUME, number))


def clamp_idle_minutes(value: Any, fallback: int = DEFAULT_IDLE_MINUTES) -> int:
    """Idle timeout in minutes; ``0`` means "never leave on its own"."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return int(fallback)
    return max(MIN_IDLE_MINUTES, min(MAX_IDLE_MINUTES, number))


def normalise_loop_mode(value: Any, fallback: str = "off") -> str:
    """One of :data:`LOOP_MODES`, falling back for typos and old values."""
    candidate = str(value or "").strip().lower()
    if candidate in LOOP_MODES:
        return candidate
    # "track"/"one" are the words people reach for when they mean the station
    # should be restarted rather than advanced.
    if candidate in {"track", "one", "single", "restart"}:
        return "station"
    if candidate in {"all", "playlist"}:
        return "queue"
    return fallback if fallback in LOOP_MODES else "off"


def get_guild_entry(config: dict, guild_id: Any) -> dict:
    """The raw saved entry for a guild (``{}`` when there is none)."""
    guilds = config.get("guilds")
    if not isinstance(guilds, dict):
        return {}
    entry = guilds.get(_guild_key(guild_id))
    return dict(entry) if isinstance(entry, dict) else {}


def get_guild_settings(config: dict, guild_id: Any) -> dict[str, Any]:
    """Effective settings for a guild: per-guild overrides on global defaults.

    Every key in :data:`GUILD_SETTING_KEYS` is always present in the result, so
    callers can read ``settings["volume"]`` without a ``.get`` dance and a
    half-applied default.
    """
    entry = get_guild_entry(config, guild_id)
    settings: dict[str, Any] = {
        "station_id": str(
            entry.get("station_id") or config.get("default_station") or ""
        ).strip(),
        "voice_channel_id": _as_int(entry.get("voice_channel_id")),
        "text_channel_id": _as_int(
            entry.get("text_channel_id") if "text_channel_id" in entry else config.get("now_playing_channel_id")
        ),
        "autostart": bool(entry.get("autostart", False)),
        "always_on": bool(entry.get("always_on", config.get("stay_connected", False))),
        "volume": clamp_volume(entry.get("volume", config.get("default_volume")), DEFAULT_VOLUME),
        "idle_minutes": clamp_idle_minutes(
            entry.get("idle_minutes", config.get("idle_disconnect_minutes")),
            DEFAULT_IDLE_MINUTES,
        ),
        "dj_role_id": _as_int(entry.get("dj_role_id")),
        "announce": bool(entry.get("announce", config.get("announce_now_playing", True))),
        "loop_mode": normalise_loop_mode(entry.get("loop_mode"), "off"),
        # The voice channel status line ("🎧 Now playing ...") needs the
        # optional Set Voice Channel Status permission; the toggle lets a
        # server turn the feature off even where the permission exists.
        "channel_status": bool(entry.get("channel_status", config.get("channel_status", True))),
    }
    return settings


def set_guild_settings(config: dict, guild_id: Any, updates: dict[str, Any]) -> dict:
    """Return a *new* config with ``updates`` applied to one guild's entry.

    Unknown keys are rejected rather than stored: a typo in a dashboard request
    should fail loudly instead of becoming a setting nobody reads. Values are
    normalised through the same clamps the readers use, so what is written is
    what will be read back.
    """
    unknown = sorted(set(updates) - set(GUILD_SETTING_KEYS))
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")

    updated = dict(config)
    guilds = dict(updated.get("guilds") or {})
    key = _guild_key(guild_id)
    entry = dict(guilds.get(key) or {})

    for name, value in updates.items():
        if name in {"volume"}:
            entry[name] = clamp_volume(value, DEFAULT_VOLUME)
        elif name == "idle_minutes":
            entry[name] = clamp_idle_minutes(value, DEFAULT_IDLE_MINUTES)
        elif name == "loop_mode":
            entry[name] = normalise_loop_mode(value)
        elif name in {"voice_channel_id", "text_channel_id", "dj_role_id"}:
            entry[name] = _as_int(value)
        elif name in {"autostart", "always_on", "announce", "channel_status"}:
            entry[name] = bool(value)
        elif name == "station_id":
            # An explicit empty string clears the override back to the global
            # default; None means "leave it alone" and never reaches here.
            entry[name] = str(value or "").strip()
        else:  # pragma: no cover - GUILD_SETTING_KEYS is checked above
            entry[name] = value

    guilds[key] = entry
    updated["guilds"] = guilds
    return updated


def configured_guild_ids(config: dict) -> list[int]:
    """Every guild with a saved entry, as ints, in insertion order."""
    guilds = config.get("guilds")
    if not isinstance(guilds, dict):
        return []
    ids: list[int] = []
    for key in guilds:
        value = _as_int(key)
        if value is not None:
            ids.append(value)
    return ids


def autostart_guilds(config: dict) -> list[int]:
    """Guilds that should start playing by themselves when the bot boots."""
    return [
        guild_id
        for guild_id in configured_guild_ids(config)
        if get_guild_settings(config, guild_id)["autostart"]
        and get_guild_settings(config, guild_id)["voice_channel_id"]
    ]


__all__ = [
    "DEFAULT_IDLE_MINUTES",
    "DEFAULT_VOLUME",
    "GUILD_SETTING_KEYS",
    "LOOP_MODES",
    "MAX_IDLE_MINUTES",
    "MAX_VOLUME",
    "MIN_VOLUME",
    "autostart_guilds",
    "clamp_idle_minutes",
    "clamp_volume",
    "configured_guild_ids",
    "get_guild_entry",
    "get_guild_settings",
    "normalise_loop_mode",
    "set_guild_settings",
]
