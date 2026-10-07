"""Simulated servers, so the dashboard can be seen before the bot is connected.

``python3 bot.py --demo`` starts the dashboard against :class:`DemoProvider`
instead of :class:`dashboard.BotProvider`. Nothing here talks to Discord: the
guilds, listeners and play history are invented, and they move - the generated
station advances to a new title every so often, listener counts drift, and the
transport buttons really do change what the UI shows.

Two reasons this exists beyond screenshots:

* **setup order.** A new operator can look at the dashboard, decide what they
  want, and read the station list before creating a Discord application,
  installing ffmpeg and inviting a bot anywhere.
* **tests.** ``tests/test_dashboard.py`` drives every endpoint against this
  provider, so the API contract is covered without a token, a network or a
  voice connection.

The provider is honest about being fake: every payload carries ``demo: true``,
and the UI shows a banner saying the servers are simulated. The station list is
the *real* registry, and the generated titles come from the real
:mod:`generative` title function, so what you see is what the bot would show.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Optional

import command_tree
import generative
import paths
import settings as settings_module
import sources
import stations
import store
from dashboard import DashboardError, ensure_dashboard_token


#: How often a generated station moves to its next track, in seconds.
DEMO_TRACK_SECONDS = 45.0
DEMO_LISTENER_DRIFT = 25.0

DEMO_GUILDS: tuple[dict[str, Any], ...] = (
    {
        "id": 111111111111111111,
        "name": "Night Owl Study Hall",
        "members": 1284,
        "station": "lofi-girl",
        "state": "playing",
        "listeners": 7,
        "channel": "study room",
    },
    {
        "id": 222222222222222222,
        "name": "Pixel & Chill",
        "members": 462,
        "station": "studio-midnight",
        "state": "playing",
        "listeners": 3,
        "channel": "late night",
    },
    {
        "id": 333333333333333333,
        "name": "Rainy Cafe",
        "members": 88,
        "station": "groove-salad",
        "state": "idle",
        "listeners": 0,
        "channel": "the cafe",
    },
)

#: Titles used for stations whose real content is a live broadcast - the demo
#: never invents a track list for somebody else's stream.
LIVE_LABEL = "Live broadcast"
LIBRARY_DEMO_TITLES = (
    "Midnight Tape – Side A",
    "Kettle On, Rain Outside",
    "Bus Window (demo mix)",
    "Two Blocks From Home",
    "Cassette Warmth",
)


class DemoGuild:
    """One simulated server and its playback state."""

    def __init__(self, spec: dict[str, Any], config: dict) -> None:
        self.id = int(spec["id"])
        self.name = str(spec["name"])
        self.member_count = int(spec["members"])
        self.channel_name = str(spec["channel"])
        self.channel_id = self.id + 1
        self.text_channel_id = self.id + 2
        self.volume = int(settings_module.get_guild_settings(config, self.id)["volume"] or 60)
        self.loop_mode = "off"
        self.state = str(spec["state"])  # playing | paused | idle
        self.listeners = int(spec["listeners"])
        self.station = stations.find_station(config, spec["station"]) or stations.default_station(config)
        self.started_at = time.time() - random.uniform(600, 5400)
        self.track_started_at = time.time() - random.uniform(10, DEMO_TRACK_SECONDS)
        self.seed = random.randrange(1, 10_000)
        self.queue: list[dict[str, Any]] = []
        self.peak_listeners = max(self.listeners, int(spec["listeners"]))
        self.last_error = ""
        self.rng = random.Random(self.id)

    # --- state --------------------------------------------------------- #
    @property
    def connected(self) -> bool:
        return self.state != "idle"

    @property
    def playing(self) -> bool:
        return self.state == "playing"

    def tick(self) -> None:
        """Advance time: new tracks, drifting listener counts."""
        now = time.time()
        if not self.connected:
            return
        if (
            self.station.kind == stations.KIND_GENERATIVE
            and now - self.track_started_at > DEMO_TRACK_SECONDS
        ):
            self.seed += 1
            self.track_started_at = now
        if now % DEMO_LISTENER_DRIFT < 5:
            drift = self.rng.choice([-1, 0, 0, 0, 1])
            self.listeners = max(0, min(self.member_count, self.listeners + drift))
            self.peak_listeners = max(self.peak_listeners, self.listeners)

    def track(self) -> dict[str, Any]:
        """The current track, as the dashboard expects it."""
        station = self.station
        if station.kind == stations.KIND_GENERATIVE:
            recipe = generative.recipe_for(_recipe_of(station))
            title = generative.track_title(recipe, self.seed)
            duration = 16 * 4 * recipe.seconds_per_beat + 1.6
            is_live = False
            origin = f"{recipe.name} · seed {self.seed}"
        elif station.kind == stations.KIND_LIBRARY:
            title = LIBRARY_DEMO_TITLES[self.seed % len(LIBRARY_DEMO_TITLES)]
            duration = 214.0
            is_live = False
            origin = "music/ (demo)"
        else:
            title = LIVE_LABEL
            duration = None
            is_live = True
            origin = station.url or station.description
        elapsed = time.time() - self.track_started_at
        return {
            "title": title,
            "stationId": station.id,
            "stationName": station.name,
            "kind": station.kind,
            "isLive": is_live,
            "duration": round(duration, 1) if duration else None,
            "requestedBy": None,
            "art": station.art,
            "origin": origin,
            "artworkUrl": None,
            "uploader": "Lofi Girl" if station.id == "lofi-girl" else None,
            "seed": self.seed if station.kind == stations.KIND_GENERATIVE else None,
            "recipe": _recipe_of(station) if station.kind == stations.KIND_GENERATIVE else None,
            "elapsed": round(elapsed, 1),
            "progress": (
                None if not duration else max(0.0, min(1.0, round(elapsed / duration, 4)))
            ),
        }

    def state_payload(self) -> dict[str, Any]:
        track = self.track() if self.connected else None
        return {
            "guildId": str(self.id),
            "connected": self.connected,
            "playing": self.playing,
            "paused": self.state == "paused",
            "channelId": str(self.channel_id) if self.connected else None,
            "channelName": self.channel_name if self.connected else None,
            "listeners": self.listeners if self.connected else 0,
            "peakListeners": self.peak_listeners,
            "volume": self.volume,
            "loopMode": self.loop_mode,
            "station": self.station.to_dict(),
            "track": track,
            "elapsed": track["elapsed"] if track else 0.0,
            "duration": track["duration"] if track else None,
            "progress": track["progress"] if track else None,
            "queue": list(self.queue),
            "queueLength": len(self.queue),
            "startedAt": self.started_at if self.connected else None,
            "restartAttempts": 0,
            "lastError": self.last_error,
            "lastErrorAt": None,
            # Mirrors GuildPlayer.status_payload(), including the paused
            # marker, so the demo shows what a real server would show.
            "statusText": (
                f"{self.station.art} {track['title']}"
                + (" (paused)" if self.state == "paused" else "")
                if track and self.connected
                else ""
            ),
            "statusSupported": True,
            "bitrateKbps": 128.0 if self.connected else None,
            "demo": True,
        }

    # --- actions ------------------------------------------------------- #
    def apply(self, action: str, payload: dict[str, Any], config: dict) -> str:
        action_name = action.strip().lower()
        if action_name == "play":
            reference = payload.get("station") or self.station.id
            found = _demo_station(config, reference)
            channel_id = payload.get("channelId")
            self.station = found
            self.state = "playing"
            self.track_started_at = time.time()
            self.started_at = time.time()
            self.last_error = ""
            if channel_id:
                self.channel_id = int(channel_id)
            if self.listeners == 0:
                self.listeners = self.rng.randint(1, 5)
            return f"Playing {found.name} in #{self.channel_name}"
        if action_name == "pause":
            if not self.connected:
                raise DashboardError("The bot is not connected in that server.")
            self.state = "paused"
            return "Paused"
        if action_name == "resume":
            if self.state != "paused":
                raise DashboardError("Playback was not paused.")
            self.state = "playing"
            return "Playing again"
        if action_name == "skip":
            if not self.connected:
                raise DashboardError("Nothing is playing to skip.")
            if self.queue:
                item = self.queue.pop(0)
                self.station = _demo_station(config, item.get("stationId") or self.station.id)
            self.seed += 1
            self.track_started_at = time.time()
            return f"Skipped to {self.track()['title']}"
        if action_name in {"stop", "leave"}:
            self.state = "idle"
            self.listeners = 0
            self.queue.clear()
            return "Stopped and left the channel"
        if action_name == "volume":
            self.volume = settings_module.clamp_volume(payload.get("percent", self.volume))
            return f"Volume {self.volume}%"
        if action_name == "loop":
            self.loop_mode = settings_module.normalise_loop_mode(payload.get("mode", "off"))
            return f"Loop mode: {self.loop_mode}"
        if action_name == "queue":
            found = _demo_station(config, payload.get("item") or self.station.id)
            if len(self.queue) >= 20:
                raise DashboardError("The queue is full.")
            self.queue.append(
                {"title": LIVE_LABEL if found.is_live else found.name, "stationId": found.id,
                 "stationName": found.name, "kind": found.kind, "art": found.art}
            )
            return f"Queued {found.name}"
        if action_name == "clear-queue":
            count = len(self.queue)
            self.queue.clear()
            return f"Cleared {count} queued item(s)"
        if action_name == "announce":
            if not self.connected:
                raise DashboardError("Nothing is playing to announce.")
            return f"Posted the now-playing card to #lofi-updates (simulated)"
        raise DashboardError(f"Unknown action: {action_name or '(none)'}")


def _recipe_of(station: Any) -> str:
    raw = str(getattr(station, "url", "") or "")
    if station.id.startswith("studio-"):
        return station.id.split("studio-", 1)[-1]
    return raw.split(":", 1)[1].strip() if ":" in raw else "midnight"


def _demo_station(config: dict, reference: Any) -> Any:
    """Resolve a station reference, including the demo's ``studio-*`` moods."""
    text = str(reference or "").strip()
    if text.startswith("studio-"):
        recipe = generative.recipe_for(text.split("studio-", 1)[-1])
        return stations.Station(
            id=f"studio-{recipe.id}",
            name=f"Studio · {recipe.name}",
            kind=stations.KIND_GENERATIVE,
            url=f"generative:{recipe.id}",
            art="🎹",
            description=recipe.description,
        )
    found = stations.find_station(config, text)
    if found is None:
        raise DashboardError(f"No station matches {text[:60]!r}.", status=404)
    return found


class DemoProvider:
    """A :class:`dashboard.Provider` backed by simulated servers."""

    demo = True

    def __init__(self, config: Optional[dict] = None, *, token: Optional[str] = None) -> None:
        self.config: dict[str, Any] = dict(paths.DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.started_at = time.time()
        self.token = token or str(self.config.get("dashboard_token") or "") or _demo_token()
        self.guilds = [DemoGuild(spec, self.config) for spec in DEMO_GUILDS]
        self._history = _seed_history(self.guilds)
        self._events = _seed_events(self.guilds)

    def save_config(self, updated: dict) -> None:
        """Adopt a config the dashboard changed (stations, network settings).

        The provider holds its own copy, so a plain ``dict.update`` on the
        caller's copy would silently discard a station added from the UI.
        """
        self.config = dict(updated)

    # --- provider surface ---------------------------------------------- #
    def _guild(self, guild_id: int) -> DemoGuild:
        for guild in self.guilds:
            if guild.id == int(guild_id):
                guild.tick()
                return guild
        raise DashboardError("The bot is not connected to that server.", status=404)

    async def overview(self) -> dict[str, Any]:
        for guild in self.guilds:
            guild.tick()
        rows = []
        for guild in self.guilds:
            state = guild.state_payload()
            rows.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon": None,
                    "memberCount": guild.member_count,
                    "connected": state["connected"],
                    "playing": state["playing"],
                    "paused": state["paused"],
                    "listeners": state["listeners"],
                    "station": (state["station"] or {}).get("name"),
                    "track": (state["track"] or {}).get("title"),
                }
            )
        rows.sort(key=lambda row: (not row["playing"], row["name"].lower()))
        connected = [guild for guild in self.guilds if guild.connected]
        listening = sum(max(0.0, time.time() - guild.started_at) for guild in connected) / 60.0
        return {
            "bot": {
                "ready": True,
                "name": "Lofi (demo)",
                "avatar": None,
                "guildCount": len(self.guilds),
                "uptimeSeconds": round(time.time() - self.started_at, 1),
                "voiceReady": True,
                "version": paths.app_version(),
            },
            "stats": {
                "guilds": len(self.guilds),
                "connected": len(connected),
                "playing": len([guild for guild in connected if guild.playing]),
                "paused": len([guild for guild in self.guilds if guild.state == "paused"]),
                "listeners": sum(guild.listeners for guild in connected),
                "peakListeners": sum(guild.peak_listeners for guild in self.guilds),
                "sessions": len(self._history),
                "listeningMinutes": round(listening + 412.5, 1),
                "stations": len(stations.get_stations(self.config)),
                "libraryTracks": 0,
                "errors": 0,
                "uptimeSeconds": round(time.time() - self.started_at, 1),
                "topTracks": _demo_top_tracks(),
                "recentEvents": self._events[:12],
            },
            "guilds": rows,
            "demo": True,
        }

    async def guild_detail(self, guild_id: int) -> dict[str, Any]:
        guild = self._guild(guild_id)
        guild_settings = settings_module.get_guild_settings(self.config, guild.id)
        return {
            "guild": {
                "id": str(guild.id),
                "name": guild.name,
                "icon": None,
                "ownerId": str(guild.id + 9),
                "memberCount": guild.member_count,
            },
            "settings": {
                "stationId": guild_settings["station_id"] or guild.station.id,
                "voiceChannelId": str(guild.channel_id),
                "textChannelId": str(guild.text_channel_id),
                "autostart": guild_settings["autostart"],
                "alwaysOn": guild_settings["always_on"],
                "volume": guild.volume,
                "idleMinutes": guild_settings["idle_minutes"],
                "djRoleId": guild_settings["dj_role_id"],
                "announce": guild_settings["announce"],
                "loopMode": guild.loop_mode,
                "channelStatus": guild_settings["channel_status"],
            },
            "player": guild.state_payload(),
            "voiceChannels": [
                {"id": str(guild.channel_id), "name": guild.channel_name, "category": "Voice", "members": guild.listeners},
                {"id": str(guild.channel_id + 10), "name": "afk", "category": "Voice", "members": 0},
            ],
            "textChannels": [
                {"id": str(guild.text_channel_id), "name": "lofi-updates", "category": "Music"},
                {"id": str(guild.text_channel_id + 10), "name": "general", "category": None},
            ],
            "roles": [
                {"id": str(guild.id + 100), "name": "DJ", "position": 12},
                {"id": str(guild.id + 101), "name": "Moderator", "position": 20},
                {"id": str(guild.id + 102), "name": "Members", "position": 1},
            ],
            "permissions": _demo_permissions(),
            "stats": {
                "sessions": len([row for row in self._history if row["guildId"] == str(guild.id)]),
                "listeningMinutes": round(120.0 + guild.listeners * 7.5, 1),
                "topTracks": _demo_top_tracks(guild.id),
            },
            "demo": True,
        }

    async def player_state(self, guild_id: int) -> dict[str, Any]:
        return self._guild(guild_id).state_payload()

    async def player_action(self, guild_id: int, action: str, payload: dict) -> dict[str, Any]:
        guild = self._guild(guild_id)
        message = guild.apply(action, payload or {}, self.config)
        kind = "player"
        self._events.insert(
            0,
            {
                "id": len(self._events) + 1,
                "kind": kind,
                "detail": message,
                "guildId": str(guild.id),
                "guildName": guild.name,
                "createdAt": time.time(),
            },
        )
        return {"ok": True, "message": message, "player": guild.state_payload(), "demo": True}

    async def save_settings(self, guild_id: int, updates: dict) -> dict[str, Any]:
        guild = self._guild(guild_id)
        snake: dict[str, Any] = {}
        mapping = {
            "stationId": "station_id",
            "voiceChannelId": "voice_channel_id",
            "textChannelId": "text_channel_id",
            "autostart": "autostart",
            "alwaysOn": "always_on",
            "volume": "volume",
            "idleMinutes": "idle_minutes",
            "djRoleId": "dj_role_id",
            "announce": "announce",
            "loopMode": "loop_mode",
            "channelStatus": "channel_status",
        }
        for camel, key in mapping.items():
            if updates.get(camel) is not None:
                snake[key] = updates[camel]
        if not snake:
            raise DashboardError("Nothing to save.")
        try:
            self.config = settings_module.set_guild_settings(self.config, guild.id, snake)
        except ValueError as exc:
            raise DashboardError(str(exc)) from exc
        if "volume" in snake:
            guild.volume = settings_module.clamp_volume(snake["volume"])
        if "loop_mode" in snake:
            guild.loop_mode = settings_module.normalise_loop_mode(snake["loop_mode"])
        self._events.insert(
            0,
            {
                "id": len(self._events) + 1,
                "kind": "settings",
                "detail": "dashboard saved: " + ", ".join(sorted(snake)),
                "guildId": str(guild.id),
                "guildName": guild.name,
                "createdAt": time.time(),
            },
        )
        return {
            "ok": True,
            "message": "Settings saved (simulated)",
            "settings": (await self.guild_detail(guild.id))["settings"],
            "player": guild.state_payload(),
            "demo": True,
        }

    async def history(self, guild_id: Optional[int], limit: int) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), 200))
        wanted = str(guild_id) if guild_id else None
        sessions = [row for row in self._history if wanted is None or row["guildId"] == wanted][:limit]
        events = [row for row in self._events if wanted is None or row["guildId"] == wanted][:limit]
        return {
            "sessions": sessions,
            "events": events,
            "topTracks": _demo_top_tracks(int(guild_id) if guild_id else None),
            "listeningMinutes": 412.5,
            "demo": True,
        }

    async def sync_commands(self, guild_id: Optional[int]) -> dict[str, Any]:
        return {
            "ok": True,
            "message": "Slash commands published (simulated)",
            "result": {"global": len(command_tree.command_names()), "guilds": {}},
            "demo": True,
        }


# --------------------------------------------------------------------------- #
# Demo fixtures
# --------------------------------------------------------------------------- #
def _demo_token() -> str:
    """A throwaway key for the demo, printed on start."""
    import secrets

    return secrets.token_urlsafe(36)


def _demo_permissions() -> list[dict[str, Any]]:
    import permissions

    rows = []
    for item in permissions.REQUIREMENTS:
        granted = True
        rows.append(
            {
                "key": item.key,
                "label": item.label,
                "required": item.required,
                "granted": granted,
                "reason": item.reason,
                "state": "no-effect" if item.key == "priority_speaker" else ("ok" if granted else "missing"),
            }
        )
    return rows


def _demo_top_tracks(guild_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Plausible "most played" rows, labelled as generated titles."""
    rng = random.Random(4242 if guild_id is None else guild_id)
    titles = [
        generative.track_title(generative.recipe_for(recipe), rng.randrange(1, 500))
        for recipe in ("midnight", "cafe", "rainy", "dusty", "nocturne")
    ]
    rows = [
        {"title": title, "plays": rng.randint(4, 38), "peak_listeners": rng.randint(1, 9)}
        for title in titles
    ]
    rows.sort(key=lambda row: (-row["plays"], row["title"]))
    return rows


def _seed_history(guilds: list[DemoGuild]) -> list[dict[str, Any]]:
    """A few days of play sessions, newest first."""
    rng = random.Random(99)
    rows: list[dict[str, Any]] = []
    now = time.time()
    counter = 1
    for guild in guilds:
        for index in range(6):
            started = now - (index + 1) * rng.uniform(3600, 26000)
            seconds = rng.uniform(900, 14400)
            station = guild.station if index == 0 else rng.choice(list(stations.BUILT_IN_STATIONS))
            title = (
                generative.track_title(generative.recipe_for(_recipe_of(station)), rng.randrange(1, 999))
                if station.kind == stations.KIND_GENERATIVE
                else (LIVE_LABEL if station.is_live else rng.choice(LIBRARY_DEMO_TITLES))
            )
            rows.append(
                {
                    "id": counter,
                    "guildId": str(guild.id),
                    "guildName": guild.name,
                    "stationId": station.id,
                    "stationName": station.name,
                    "trackTitle": title,
                    "requestedBy": None,
                    "startedAt": started,
                    "endedAt": None if (index == 0 and guild.connected) else started + seconds,
                    "running": index == 0 and guild.connected,
                    "endedReason": None if (index == 0 and guild.connected) else rng.choice(
                        ["stopped by a command", "left because the channel was empty", "the bot restarted"]
                    ),
                    "peakListeners": rng.randint(1, 12),
                    "secondsPlayed": round(seconds if index else (now - started), 1),
                }
            )
            counter += 1
    rows.sort(key=lambda row: -row["startedAt"])
    return rows


def _seed_events(guilds: list[DemoGuild]) -> list[dict[str, Any]]:
    rng = random.Random(7)
    kinds = (
        ("connect", "joined #{channel}"),
        ("settings", "default station set to {station}"),
        ("skip", "skipped {title}"),
        ("stall", "the stream stopped sending audio; restarting"),
        ("station", "added station Late Night Jazz (stream)"),
        ("sync", "slash commands synced"),
        ("ready", "connected as Lofi#4821 in 3 server(s)"),
    )
    rows: list[dict[str, Any]] = []
    now = time.time()
    for index in range(14):
        guild = rng.choice(guilds)
        kind, template = rng.choice(kinds)
        detail = template.format(
            channel=guild.channel_name,
            station=guild.station.name,
            title=generative.track_title(generative.recipe_for("midnight"), rng.randrange(1, 999)),
        )
        rows.append(
            {
                "id": index + 1,
                "kind": kind,
                "detail": detail,
                "guildId": str(guild.id),
                "guildName": guild.name,
                "createdAt": now - index * rng.uniform(120, 3600),
            }
        )
    rows.sort(key=lambda row: -row["createdAt"])
    return rows


__all__ = [
    "DEMO_GUILDS",
    "DemoGuild",
    "DemoProvider",
    "LIVE_LABEL",
]
