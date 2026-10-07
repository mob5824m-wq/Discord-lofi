"""Authenticated web dashboard for Lofi, sharing the bot's event loop.

The dashboard is deliberately bound to loopback by default and protected by a
single high-privilege key: from it you can start and stop audio in every server
the bot is in, which is the same reach as the bot token itself. Exposing it
needs the same three things Sentinel's dashboard does, and the config keys that
provide them:

* ``dashboard_host`` / ``dashboard_port`` - what to listen on. Keep loopback and
  let a reverse proxy on the same machine reach it, or bind ``0.0.0.0`` and
  forward the port.
* ``dashboard_allowed_hosts`` - the public name(s) the dashboard answers for.
  The ``Host`` header is checked against this list to block DNS-rebinding
  attacks; a name that is not listed gets ``400 Unrecognized Host header``,
  which is the allowlist working, not a network failure. ``"*"`` disables the
  check and is only appropriate for a throwaway local preview.
* ``dashboard_tls_cert`` / ``dashboard_tls_key`` - serve HTTPS directly, or
  terminate TLS in a proxy and set ``dashboard_secure_cookie`` plus
  ``dashboard_public_url``.

``dashboard_trusted_proxies`` is the bridge to a reverse proxy: the addresses
whose ``X-Forwarded-For`` may be believed. Without it every visitor looks like
the proxy, and the login throttle becomes one bucket for the whole internet.

Everything the dashboard *knows* comes from a provider - :class:`BotProvider`
against a live bot, or ``demo.DemoProvider`` against simulated servers. That
split is what lets the UI be developed, screenshotted and tested without a
Discord token, and it keeps this module free of ``discord`` calls.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import ssl
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlsplit

from aiohttp import web

import command_tree
import paths
import settings as settings_module
import sources
import stations
import store


logger = logging.getLogger("lofi.dashboard")

SESSION_COOKIE = "lofi_dashboard_session"
#: Where the dashboard listens unless the config or ``LOFI_DASHBOARD_PORT`` says
#: otherwise. Not 8765: that is the default of Sentinel's dashboard, and running
#: a moderation bot and a music bot on one machine should not require anybody to
#: edit a config file first. Every other namespace here is already prefixed
#: (``lofi_dashboard_session``, ``LOFI_*``, ``<state dir>/lofi``, ``lofi.db``),
#: so the port was the only thing two bots would fight over.
DEFAULT_PORT = 8790
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 5 * 60
MAX_HISTORY_ROWS = 200
#: Request bodies are settings and short actions, never uploads.
MAX_BODY_BYTES = 64 * 1024


def _is_loopback_host(host: str) -> bool:
    """True for 127.0.0.1 / ::1 / localhost / another loopback address."""
    name = str(host or "").strip().lower()
    if name in {"", "localhost"}:
        return True
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _snowflake(value: Any) -> Optional[str]:
    """Serialise a Discord id as a string.

    Snowflakes exceed JavaScript's ``Number.MAX_SAFE_INTEGER``, so sending one
    as a JSON number makes the browser silently round it; the rounded id then
    comes back on the next request and matches no guild.
    """
    if value is None or value == "":
        return None
    return str(value)


def ensure_dashboard_token(config: dict, save_config: Callable[[dict], None]) -> str:
    """Return the dashboard credential, generating and storing one if needed.

    ``LOFI_DASHBOARD_TOKEN`` overrides the stored value, which is what a
    container deployment wants (the key never lands on disk).
    """
    env_token = os.environ.get("LOFI_DASHBOARD_TOKEN", "").strip()
    configured = env_token or config.get("dashboard_token")
    if isinstance(configured, str) and configured:
        if len(configured) < 32:
            raise ValueError("dashboard_token must contain at least 32 characters")
        return configured

    token = secrets.token_urlsafe(36)
    updated = dict(config)
    updated["dashboard_token"] = token
    save_config(updated)
    if config is not updated:
        config.clear()
        config.update(updated)
    return token


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
class Provider(Protocol):
    """What the dashboard needs from whatever is behind it."""

    config: dict
    demo: bool

    async def overview(self) -> dict[str, Any]: ...
    async def guild_detail(self, guild_id: int) -> dict[str, Any]: ...
    async def player_state(self, guild_id: int) -> dict[str, Any]: ...
    async def player_action(self, guild_id: int, action: str, payload: dict) -> dict[str, Any]: ...
    async def save_settings(self, guild_id: int, updates: dict) -> dict[str, Any]: ...
    async def history(self, guild_id: Optional[int], limit: int) -> dict[str, Any]: ...
    async def sync_commands(self, guild_id: Optional[int]) -> dict[str, Any]: ...


class DashboardError(Exception):
    """A provider refused. The message is shown to the operator as-is."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class BotProvider:
    """The real thing: reads live player state and drives the bot."""

    demo = False

    def __init__(self, bot: Any, players: Any) -> None:
        self.bot = bot
        self.players = players

    @property
    def config(self) -> dict:
        return self.bot.config

    # --- helpers ------------------------------------------------------- #
    def _guild(self, guild_id: int) -> Any:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            raise DashboardError("The bot is not connected to that server.", status=404)
        return guild

    def _player(self, guild_id: int) -> Any:
        return self.players.get(int(guild_id))

    # --- endpoints ----------------------------------------------------- #
    async def overview(self) -> dict[str, Any]:
        guilds: list[dict[str, Any]] = []
        for guild in self.bot.guilds:
            player = self.players.get(guild.id, create=False)
            state = player.state() if player is not None else {}
            guilds.append(
                {
                    "id": _snowflake(guild.id),
                    "name": guild.name,
                    "icon": guild.icon.url if guild.icon else None,
                    "memberCount": guild.member_count,
                    "connected": bool(state.get("connected")),
                    "playing": bool(state.get("playing")),
                    "paused": bool(state.get("paused")),
                    "listeners": int(state.get("listeners") or 0),
                    "station": (state.get("station") or {}).get("name"),
                    "track": (state.get("track") or {}).get("title"),
                }
            )
        guilds.sort(key=lambda row: (not row["playing"], row["name"].lower()))
        stats = self.players.overview()
        stats["topTracks"] = store.top_tracks(limit=6)
        stats["recentEvents"] = _event_rows(store.guild_events(None, limit=12), self.bot)
        return {
            "bot": {
                "ready": bool(self.bot.is_ready()),
                "name": str(self.bot.user) if self.bot.user else "Connecting",
                "avatar": self.bot.user.display_avatar.url if self.bot.user else None,
                "guildCount": len(self.bot.guilds),
                "uptimeSeconds": round(self.bot.uptime_seconds, 1),
                "voiceReady": bool(self.bot.voice_ready),
                "version": paths.app_version(),
            },
            "stats": stats,
            "guilds": guilds,
            "demo": self.demo,
        }

    async def guild_detail(self, guild_id: int) -> dict[str, Any]:
        guild = self._guild(guild_id)
        config = self.config
        guild_settings = settings_module.get_guild_settings(config, guild.id)
        player = self._player(guild.id)
        voice_channels = [
            {
                "id": _snowflake(channel.id),
                "name": channel.name,
                "category": channel.category.name if channel.category else None,
                "members": len([m for m in channel.members if not m.bot]),
            }
            for channel in guild.voice_channels
        ]
        text_channels = [
            {
                "id": _snowflake(channel.id),
                "name": channel.name,
                "category": channel.category.name if channel.category else None,
            }
            for channel in guild.text_channels
        ]
        roles = [
            {"id": _snowflake(role.id), "name": role.name, "position": role.position}
            for role in sorted(guild.roles, key=lambda item: item.position, reverse=True)
            if not role.is_default() and not role.managed
        ]
        current_channel = player.channel if player is not None else None
        audit = _permission_rows(getattr(guild, "me", None), current_channel)
        return {
            "guild": {
                "id": _snowflake(guild.id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
                "ownerId": _snowflake(guild.owner_id),
                "memberCount": guild.member_count,
            },
            "settings": {
                "stationId": guild_settings["station_id"],
                "voiceChannelId": _snowflake(guild_settings["voice_channel_id"]),
                "textChannelId": _snowflake(guild_settings["text_channel_id"]),
                "autostart": guild_settings["autostart"],
                "alwaysOn": guild_settings["always_on"],
                "volume": guild_settings["volume"],
                "idleMinutes": guild_settings["idle_minutes"],
                "djRoleId": _snowflake(guild_settings["dj_role_id"]),
                "announce": guild_settings["announce"],
                "loopMode": guild_settings["loop_mode"],
                "channelStatus": guild_settings["channel_status"],
            },
            "player": player.state() if player is not None else {},
            "voiceChannels": voice_channels,
            "textChannels": text_channels,
            "roles": roles,
            "permissions": audit,
            "stats": {
                "sessions": store.session_count(guild.id),
                "listeningMinutes": round(store.listening_minutes(guild.id, days=7), 1),
                "topTracks": store.top_tracks(guild.id, limit=6),
            },
            "demo": self.demo,
        }

    async def player_state(self, guild_id: int) -> dict[str, Any]:
        self._guild(guild_id)
        player = self._player(guild_id)
        state = player.state() if player is not None else {}
        state["demo"] = self.demo
        return state

    async def player_action(self, guild_id: int, action: str, payload: dict) -> dict[str, Any]:
        from player import PlayerError  # local import: avoids a cycle at module load

        guild = self._guild(guild_id)
        player = self._player(guild_id)
        action_name = str(action or "").strip().lower()
        try:
            if action_name == "play":
                reference = payload.get("station") or None
                channel_id = payload.get("channelId")
                channel = guild.get_channel(int(channel_id)) if channel_id else None
                if channel is None:
                    channel = _first_voice_channel(guild)
                if channel is None:
                    raise DashboardError("That server has no voice channel to join.")
                track = await player.play(reference, channel=channel, requested_by=None)
                return {"ok": True, "message": f"Playing {track.display_title}", "player": player.state()}
            if action_name == "pause":
                changed = await player.pause()
                return {"ok": changed, "message": "Paused" if changed else "Nothing was playing", "player": player.state()}
            if action_name == "resume":
                changed = await player.resume()
                return {"ok": changed, "message": "Resumed" if changed else "Playback was not paused", "player": player.state()}
            if action_name == "skip":
                track = await player.skip()
                return {"ok": True, "message": f"Skipped to {track.display_title}", "player": player.state()}
            if action_name == "stop":
                await player.stop(reason="stop")
                return {"ok": True, "message": "Stopped and left the channel", "player": player.state()}
            if action_name == "leave":
                await player.stop(reason="disconnect")
                return {"ok": True, "message": "Left the voice channel", "player": player.state()}
            if action_name == "volume":
                applied = await player.set_volume(int(payload.get("percent", player.volume)))
                return {"ok": True, "message": f"Volume {applied}%", "player": player.state()}
            if action_name == "loop":
                applied = await player.set_loop_mode(str(payload.get("mode", "off")))
                return {"ok": True, "message": f"Loop mode: {applied}", "player": player.state()}
            if action_name == "queue":
                track = await player.queue_track(str(payload.get("item", "")))
                return {"ok": True, "message": f"Queued {track.display_title}", "player": player.state()}
            if action_name == "clear-queue":
                count = player.clear_queue()
                return {"ok": True, "message": f"Cleared {count} queued item(s)", "player": player.state()}
            if action_name == "announce":
                if player.current is None:
                    raise DashboardError("Nothing is playing to announce.")
                posted = await player.announce(player.current)
                return {
                    "ok": posted,
                    "message": "Posted the now-playing card" if posted else "No channel is configured for announcements",
                    "player": player.state(),
                }
            raise DashboardError(f"Unknown action: {action_name or '(none)'}")
        except PlayerError as exc:
            raise DashboardError(str(exc)) from exc

    async def save_settings(self, guild_id: int, updates: dict) -> dict[str, Any]:
        guild = self._guild(guild_id)
        snake = {
            "station_id": updates.get("stationId"),
            "voice_channel_id": updates.get("voiceChannelId"),
            "text_channel_id": updates.get("textChannelId"),
            "autostart": updates.get("autostart"),
            "always_on": updates.get("alwaysOn"),
            "volume": updates.get("volume"),
            "idle_minutes": updates.get("idleMinutes"),
            "dj_role_id": updates.get("djRoleId"),
            "announce": updates.get("announce"),
            "loop_mode": updates.get("loopMode"),
            "channel_status": updates.get("channelStatus"),
        }
        apply_now = {key: value for key, value in snake.items() if value is not None}
        if not apply_now:
            raise DashboardError("Nothing to save.")
        try:
            config = settings_module.set_guild_settings(self.config, guild.id, apply_now)
        except ValueError as exc:
            raise DashboardError(str(exc)) from exc
        self.bot.save_config(config)
        store.log_event(
            "settings",
            "dashboard saved: " + ", ".join(sorted(apply_now)),
            guild_id=guild.id,
        )
        player = self._player(guild.id)
        if player is not None:
            if "volume" in apply_now:
                await player.set_volume(int(apply_now["volume"]))
            if "loop_mode" in apply_now:
                await player.set_loop_mode(str(apply_now["loop_mode"]))
            if apply_now.get("channel_status") is False:
                await player.update_channel_status(clear=True, force=True)
            elif apply_now.get("channel_status") is True:
                await player.update_channel_status(force=True)
        return {
            "ok": True,
            "message": "Settings saved",
            "settings": (await self.guild_detail(guild.id))["settings"],
            "player": player.state() if player is not None else {},
        }

    async def history(self, guild_id: Optional[int], limit: int) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), MAX_HISTORY_ROWS))
        sessions = store.guild_history(guild_id, limit=limit)
        events = store.guild_events(guild_id, limit=limit)
        return {
            "sessions": [_session_row(row, self.bot) for row in sessions],
            "events": _event_rows(events, self.bot),
            "topTracks": store.top_tracks(guild_id, limit=8),
            "listeningMinutes": round(store.listening_minutes(guild_id, days=7), 1),
            "demo": self.demo,
        }

    async def sync_commands(self, guild_id: Optional[int]) -> dict[str, Any]:
        result = await self.bot.sync_commands(guild_id)
        store.log_event("sync", f"slash commands synced ({result})", guild_id=guild_id)
        return {"ok": True, "message": "Slash commands published", "result": result}


def _permission_rows(bot_member: Any, channel: Any) -> list[dict[str, Any]]:
    """The voice-permission audit, with a fallback when discord is not importable."""
    import permissions

    return permissions.audit(bot_member, channel)


def _first_voice_channel(guild: Any) -> Any:
    channels = list(getattr(guild, "voice_channels", []) or [])
    if not channels:
        return None
    return channels[0]


def _session_row(row: dict[str, Any], bot: Any) -> dict[str, Any]:
    guild = bot.get_guild(int(row["guild_id"])) if bot is not None else None
    started = float(row.get("started_at") or 0.0)
    ended = row.get("ended_at")
    return {
        "id": int(row.get("id") or 0),
        "guildId": _snowflake(row.get("guild_id")),
        "guildName": guild.name if guild is not None else f"guild {row.get('guild_id')}",
        "stationId": row.get("station_id"),
        "stationName": row.get("station_name"),
        "trackTitle": row.get("track_title"),
        "requestedBy": _snowflake(row.get("requested_by")),
        "startedAt": started,
        "endedAt": float(ended) if ended else None,
        "running": ended is None,
        "endedReason": row.get("ended_reason"),
        "peakListeners": int(row.get("peak_listeners") or 0),
        "secondsPlayed": round(float(row.get("seconds_played") or 0.0), 1),
    }


def _event_rows(rows: list[dict[str, Any]], bot: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        guild = bot.get_guild(int(row["guild_id"])) if bot is not None and row.get("guild_id") else None
        out.append(
            {
                "id": int(row.get("id") or 0),
                "kind": row.get("kind"),
                "detail": row.get("detail"),
                "guildId": _snowflake(row.get("guild_id")),
                "guildName": guild.name if guild is not None else None,
                "createdAt": float(row.get("created_at") or 0.0),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class DashboardServer:
    """aiohttp dashboard: auth, host allowlist, CSRF and the JSON API."""

    def __init__(self, provider: Provider, *, save_config: Optional[Callable[[dict], None]] = None) -> None:
        self.provider = provider
        self._save_config = save_config or (lambda updated: None)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._login_failures: dict[str, tuple[int, float]] = {}
        self._token: Optional[str] = None
        self._host = "127.0.0.1"
        self._port = DEFAULT_PORT
        self._secure_cookie = False
        self._tls_context: Optional[ssl.SSLContext] = None
        self._trusted_proxies: list[str] = []
        self._public_url = ""
        self._allow_any_host = False
        self._warnings: list[str] = []
        self._settings_applied = False

    # --- configuration ------------------------------------------------- #
    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def config(self) -> dict:
        return self.provider.config

    def _apply_settings(self, config: dict) -> None:
        """Resolve network settings from config + environment.

        Split out of :meth:`start` so the configuration can be validated - and
        tested - without binding a socket.
        """
        self._host = os.environ.get(
            "LOFI_DASHBOARD_HOST", str(config.get("dashboard_host") or "127.0.0.1")
        )
        raw_port = os.environ.get(
            "LOFI_DASHBOARD_PORT", str(config.get("dashboard_port", DEFAULT_PORT))
        )
        try:
            self._port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("dashboard_port must be an integer") from exc
        if not 0 < self._port < 65536:
            raise ValueError("dashboard_port must be between 1 and 65535")

        self._public_url = str(
            os.environ.get("LOFI_DASHBOARD_PUBLIC_URL") or config.get("dashboard_public_url") or ""
        ).strip()

        raw_proxies = config.get("dashboard_trusted_proxies", [])
        if isinstance(raw_proxies, str):
            raw_proxies = [raw_proxies]
        self._trusted_proxies = (
            [str(item).strip() for item in raw_proxies if str(item).strip()]
            if isinstance(raw_proxies, (list, tuple))
            else []
        )

        raw_hosts = config.get("dashboard_allowed_hosts", [])
        if isinstance(raw_hosts, str):
            raw_hosts = [raw_hosts]
        allowed = (
            [str(item).strip() for item in raw_hosts if str(item).strip()]
            if isinstance(raw_hosts, (list, tuple))
            else []
        )
        self._allow_any_host = any(item == "*" for item in allowed)
        config["dashboard_allowed_hosts"] = [item for item in allowed if item != "*"]

        cert = str(
            os.environ.get("LOFI_DASHBOARD_TLS_CERT") or config.get("dashboard_tls_cert") or ""
        ).strip()
        key = str(
            os.environ.get("LOFI_DASHBOARD_TLS_KEY") or config.get("dashboard_tls_key") or ""
        ).strip()
        if bool(cert) != bool(key):
            raise ValueError(
                "dashboard_tls_cert and dashboard_tls_key must be set together "
                "(the fullchain certificate and its private key)."
            )
        self._tls_context = self._load_tls_context(cert, key) if cert else None
        # A TLS listener always gets a secure cookie; anything else would send
        # the session over plain HTTP.
        self._secure_cookie = bool(self._tls_context) or bool(config.get("dashboard_secure_cookie", False))
        # The credential is resolved here, with the rest of the configuration, so
        # that every entry point - start(), or an embedder that builds the app
        # itself - gets a server that can actually authenticate. It also means a
        # too-short token in config.json fails loudly at configuration time.
        self._token = ensure_dashboard_token(config, self._save_config)
        self._warnings = self.remote_access_warnings()
        self._settings_applied = True

    def _load_tls_context(self, cert_path: str, key_path: str) -> ssl.SSLContext:
        for path in (cert_path, key_path):
            if not Path(path).is_file():
                raise ValueError(f"dashboard TLS file not found: {path}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        except (ssl.SSLError, OSError) as exc:
            raise ValueError(f"could not load the dashboard TLS certificate: {exc}") from exc
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):  # pragma: no cover - older OpenSSL
            pass
        return context

    @property
    def scheme(self) -> str:
        return "https" if self._tls_context is not None else "http"

    @property
    def browser_scheme(self) -> str:
        """What the *browser* uses - which is what the cookie rules apply to.

        TLS is often terminated in a reverse proxy, in which case
        ``dashboard_public_url`` is the only thing that says so.
        """
        if self._tls_context is not None:
            return "https"
        if self._public_url.lower().startswith("https://"):
            return "https"
        return "http"

    def urls(self) -> list[str]:
        urls: list[str] = []
        if self._public_url:
            urls.append(self._public_url.rstrip("/") + "/")
        host = "127.0.0.1" if self._host in {"0.0.0.0", "::", ""} else self._host
        urls.append(f"{self.scheme}://{host}:{self._port}/")
        if self._host in {"0.0.0.0", "::"}:
            urls.append(f"{self.scheme}://<this machine's address>:{self._port}/")
        return urls

    def remote_access_warnings(self) -> list[str]:
        """The ways this listener would fail for a remote visitor - as fixes."""
        warnings: list[str] = []
        exposed = not _is_loopback_host(self._host)
        secure_browser = self.browser_scheme == "https"

        if self._allow_any_host:
            warnings.append(
                'dashboard_allowed_hosts contains "*", so any Host header is accepted. That '
                "disables DNS-rebinding protection and is only appropriate for a throwaway "
                "local preview."
            )
        if self._secure_cookie and not secure_browser:
            warnings.append(
                "dashboard_secure_cookie is on, but the browser reaches this dashboard over "
                "plain HTTP, so it will refuse to store the session cookie and every login "
                "will appear to succeed and then bounce back. Serve HTTPS, or turn "
                "dashboard_secure_cookie off."
            )
        if exposed and not secure_browser:
            warnings.append(
                f"The dashboard is listening on {self._host} over plain HTTP: the login key "
                "crosses the network in clear text. Put it behind HTTPS and firewall the port."
            )
        if exposed and not self._allow_any_host and not self._allowed_hosts_configured():
            warnings.append(
                f"dashboard_host is {self._host} (reachable off this machine) but "
                "'dashboard_allowed_hosts' is empty, so any Host header that is not a bare IP "
                'is answered with 400. Add the public name, e.g. "yourname.duckdns.org".'
            )
        if self._public_url and not self._public_url.lower().startswith(("http://", "https://")):
            warnings.append(f"dashboard_public_url should include the scheme; got {self._public_url!r}.")
        return warnings

    def _allowed_hosts_configured(self) -> bool:
        configured = self.config.get("dashboard_allowed_hosts", [])
        if isinstance(configured, str):
            configured = [configured]
        return bool(configured)

    # --- lifecycle ----------------------------------------------------- #
    async def start(self) -> bool:
        """Start listening. Returns True when the server is up."""
        if self.running:
            return True
        config = self.config
        if not config.get("dashboard_enabled", True):
            logger.info("Web dashboard disabled by config.")
            return False

        self._apply_settings(config)  # resolves the token too

        app = self._build_app()
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await runner.setup()
        site = web.TCPSite(runner, host=self._host, port=self._port, ssl_context=self._tls_context)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site
        logger.info(
            "Dashboard listening at %s://%s:%d%s",
            self.scheme, self._host, self._port, " (TLS)" if self._tls_context else "",
        )
        for warning in self._warnings:
            logger.warning("Dashboard: %s", warning)
        return True

    async def close(self) -> None:
        if self._runner is not None:
            runner, self._runner = self._runner, None
            self._site = None
            await runner.cleanup()
        self._sessions.clear()

    def _build_app(self) -> web.Application:
        server = self
        if not self._settings_applied:
            # Every request path goes through this resolution, so an entry point
            # that forgets _apply_settings cannot silently serve the defaults.
            self._apply_settings(self.config)

        @web.middleware
        async def auth_and_security(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
            if not server._allowed_host(request.host):
                return server._security_headers(
                    web.json_response({"error": "Unrecognized Host header."}, status=400)
                )
            if request.path.startswith("/api/") and request.path != "/api/login":
                session = server._session_for_request(request)
                if session is None:
                    return server._security_headers(
                        web.json_response({"error": "Authentication required."}, status=401)
                    )
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    csrf = request.headers.get("X-CSRF-Token", "")
                    if not hmac.compare_digest(csrf, session["csrf"]):
                        return server._security_headers(
                            web.json_response({"error": "Invalid CSRF token."}, status=403)
                        )
            try:
                response = await handler(request)
            except DashboardError as exc:
                response = web.json_response({"error": exc.message}, status=exc.status)
            except web.HTTPException as exc:
                if request.path.startswith("/api/"):
                    response = web.json_response({"error": exc.text or exc.reason}, status=exc.status)
                else:
                    response = exc
            except Exception:
                logger.exception("Unhandled dashboard request %s %s", request.method, request.path)
                if request.path.startswith("/api/"):
                    response = web.json_response(
                        {"error": "The dashboard could not complete that request."}, status=500
                    )
                else:
                    response = web.Response(text="Dashboard error", status=500)
            return server._security_headers(response)

        app = web.Application(middlewares=[auth_and_security], client_max_size=MAX_BODY_BYTES)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/healthz", self.healthz),
                web.get("/api/session", self.session_status),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/overview", self.overview),
                web.get("/api/commands", self.commands),
                web.get("/api/stations", self.list_stations),
                web.post("/api/stations", self.add_station),
                web.delete("/api/stations/{station_id}", self.remove_station),
                web.post("/api/stations/{station_id}/default", self.set_default_station),
                web.post("/api/stations/{station_id}/test", self.test_station),
                web.get("/api/library", self.library),
                web.get("/api/generative", self.generative),
                web.get("/api/history", self.global_history),
                web.post("/api/sync", self.sync_commands),
                web.get("/api/settings", self.dashboard_settings),
                web.post("/api/settings", self.save_dashboard_settings),
                web.get("/api/guilds/{guild_id}", self.guild_detail),
                web.get("/api/guilds/{guild_id}/player", self.player_state),
                web.post("/api/guilds/{guild_id}/player", self.player_action),
                web.post("/api/guilds/{guild_id}/settings", self.save_guild_settings),
                web.get("/api/guilds/{guild_id}/history", self.guild_history),
            ]
        )
        return app

    # --- security ------------------------------------------------------ #
    @staticmethod
    def _security_headers(response: web.StreamResponse) -> web.StreamResponse:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data: https://cdn.discordapp.com "
            "https://i.ytimg.com; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        if str(response.headers.get("Content-Type", "")).startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _allowed_host(self, raw_host: str) -> bool:
        """Reject DNS-rebinding Host headers.

        A rebinding attack points an attacker-controlled name at this server's
        address so the browser sends that name in the Host header with the
        victim's cookies attached. Checking Host against a list is what stops
        it, which is why a public name must be listed before the dashboard
        answers for it. Loopback names are always allowed: a page cannot forge
        the Host header of a request it is not allowed to make.
        """
        host = self._normalize_host(raw_host)
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        if self._allow_any_host:
            return True

        allowed = set()
        configured = self.config.get("dashboard_allowed_hosts", [])
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, (list, tuple, set)):
            allowed = {self._normalize_host(str(item)) for item in configured if str(item).strip()}
        if self._public_url:
            allowed.add(self._normalize_host(urlsplit(self._public_url).netloc))
        if allowed:
            return host in allowed
        if self._host not in {"0.0.0.0", "::", ""}:
            return host == self._normalize_host(self._host)
        return False

    @staticmethod
    def _normalize_host(raw_host: str) -> str:
        value = str(raw_host or "").strip().lower()
        if value.startswith("["):
            return (urlsplit("//" + value).hostname or "").rstrip(".")
        if value.count(":") > 1:
            return value.rstrip(".")  # bare IPv6 literal
        if ":" in value:
            value = value.split(":", 1)[0]
        return value.rstrip(".")

    def _client_ip(self, request: web.Request) -> str:
        """The visitor's address, believing X-Forwarded-For only when trusted."""
        peer = request.remote or "unknown"
        if not self._trusted_proxies or not self._is_trusted_proxy(peer):
            return peer
        forwarded = request.headers.get("X-Forwarded-For", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        for hop in reversed(hops):
            if not self._is_trusted_proxy(hop):
                return hop
        return hops[0] if hops else peer

    def _is_trusted_proxy(self, value: str) -> bool:
        """Entries may be addresses (``127.0.0.1``) or CIDR ranges."""
        candidate = self._normalize_host(value)
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return candidate in {entry.strip().lower() for entry in self._trusted_proxies}
        for entry in self._trusted_proxies:
            try:
                network = ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError:
                continue
            if address.version == network.version and address in network:
                return True
        return False

    def _session_for_request(self, request: web.Request) -> Optional[dict[str, Any]]:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = time.time()
        if session["expires_at"] <= now:
            self._sessions.pop(session_id, None)
            return None
        session["expires_at"] = now + SESSION_TTL_SECONDS  # sliding, like Sentinel's
        return session

    # --- handlers ------------------------------------------------------ #
    async def index(self, _request: web.Request) -> web.Response:
        resource = paths.resource_path("dashboard.html")
        if resource is None:
            return web.Response(text="Dashboard UI is missing (dashboard.html).", status=500)
        try:
            html = resource.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Could not read dashboard.html from %s", resource)
            return web.Response(text="Dashboard UI is unavailable.", status=500)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def healthz(self, _request: web.Request) -> web.Response:
        """Unauthenticated liveness probe for a systemd unit or an uptime check.

        Reveals nothing but "the process is up": no guild list, no version, no
        player state.
        """
        return web.json_response({"ok": True})

    async def login(self, request: web.Request) -> web.Response:
        peer = self._client_ip(request)
        now = time.monotonic()
        count, reset_at = self._login_failures.get(peer, (0, now + LOGIN_WINDOW_SECONDS))
        if reset_at <= now:
            count, reset_at = 0, now + LOGIN_WINDOW_SECONDS
        if count >= MAX_LOGIN_FAILURES:
            return web.json_response(
                {"error": "Too many failed attempts. Wait a few minutes and try again."}, status=429
            )
        try:
            data = await request.json()
        except (ValueError, TypeError):
            data = {}
        submitted = data.get("token", "") if isinstance(data, dict) else ""
        expected = self._token or ""
        if not isinstance(submitted, str) or not expected or not hmac.compare_digest(submitted, expected):
            self._login_failures[peer] = (count + 1, reset_at)
            logger.warning("Rejected dashboard login from %s", peer)
            return web.json_response({"error": "Invalid dashboard key."}, status=401)

        self._login_failures.pop(peer, None)
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[session_id] = {"csrf": csrf, "expires_at": time.time() + SESSION_TTL_SECONDS}
        response = web.json_response(
            {"authenticated": True, "csrfToken": csrf, "demo": bool(self.provider.demo)}
        )
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=self._secure_cookie,
            samesite="Strict",
            path="/",
        )
        logger.info("Dashboard login accepted from %s", peer)
        return response

    async def session_status(self, request: web.Request) -> web.Response:
        session = self._session_for_request(request)
        if session is None:
            raise web.HTTPUnauthorized(text="Authentication required.")
        return web.json_response(
            {
                "authenticated": True,
                "csrfToken": session["csrf"],
                "demo": bool(self.provider.demo),
                "warnings": self._warnings,
                "urls": self.urls(),
            }
        )

    async def logout(self, request: web.Request) -> web.Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            self._sessions.pop(session_id, None)
        response = web.json_response({"authenticated": False})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def overview(self, _request: web.Request) -> web.Response:
        return web.json_response(await self.provider.overview())

    async def guild_detail(self, request: web.Request) -> web.Response:
        guild_id = _guild_id(request)
        return web.json_response(await self.provider.guild_detail(guild_id))

    async def player_state(self, request: web.Request) -> web.Response:
        guild_id = _guild_id(request)
        return web.json_response(await self.provider.player_state(guild_id))

    async def player_action(self, request: web.Request) -> web.Response:
        guild_id = _guild_id(request)
        payload = await _json(request)
        action = str(payload.get("action", "")).strip()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        result = await self.provider.player_action(guild_id, action, data)
        return web.json_response(result)

    async def save_guild_settings(self, request: web.Request) -> web.Response:
        guild_id = _guild_id(request)
        payload = await _json(request)
        return web.json_response(await self.provider.save_settings(guild_id, payload))

    async def guild_history(self, request: web.Request) -> web.Response:
        guild_id = _guild_id(request)
        return web.json_response(await self.provider.history(guild_id, _query_limit(request)))

    async def global_history(self, request: web.Request) -> web.Response:
        return web.json_response(await self.provider.history(None, _query_limit(request)))

    async def sync_commands(self, request: web.Request) -> web.Response:
        payload = await _json(request)
        return web.json_response(await self.provider.sync_commands(_query_guild_id(payload)))

    async def commands(self, _request: web.Request) -> web.Response:
        described = command_tree.describe()
        return web.json_response(
            {
                "commands": described,
                "groups": command_tree.groups(described),
                "tiers": command_tree.TIER_LABELS,
            }
        )

    async def list_stations(self, _request: web.Request) -> web.Response:
        config = self.config
        default_id = str(config.get("default_station") or "")
        return web.json_response(
            {
                "stations": [station.to_dict() for station in stations.get_stations(config)],
                "defaultStationId": default_id,
                "library": sources.library_summary(),
                "generativeAvailable": _generative_available(),
            }
        )

    async def add_station(self, request: web.Request) -> web.Response:
        payload = await _json(request)
        try:
            station_id, name, kind = stations.validate_station(
                name=str(payload.get("name", "")),
                url=str(payload.get("url", "")),
                kind=payload.get("kind") or None,
            )
        except ValueError as exc:
            raise DashboardError(str(exc)) from exc
        station = stations.Station(
            id=station_id,
            name=name,
            kind=kind,
            url=str(payload.get("url", "")).strip(),
            description=str(payload.get("description", "")).strip()[:200],
            art=str(payload.get("art", "")).strip()[:8] or "🔗",
        )
        try:
            config = stations.save_custom_station(self.config, station)
        except ValueError as exc:
            raise DashboardError(str(exc)) from exc
        self._save_config(config)
        store.log_event("station", f"dashboard added station {name} ({kind})")
        return web.json_response(
            {"ok": True, "message": f"Saved {name}", "station": station.to_dict()}
        )

    async def remove_station(self, request: web.Request) -> web.Response:
        station_id = request.match_info.get("station_id", "")
        config, removed = stations.remove_custom_station(self.config, station_id)
        if not removed:
            raise DashboardError(f"No custom station with the id '{station_id}'.", status=404)
        self._save_config(config)
        store.log_event("station", f"dashboard removed station {station_id}")
        restored = station_id in stations.BUILT_IN_BY_ID
        return web.json_response(
            {
                "ok": True,
                "message": f"Removed {station_id}" + (" (built-in version restored)" if restored else ""),
            }
        )

    async def set_default_station(self, request: web.Request) -> web.Response:
        station_id = request.match_info.get("station_id", "")
        found = stations.find_station(self.config, station_id)
        if found is None:
            raise DashboardError(f"No station with the id '{station_id}'.", status=404)
        config = dict(self.config)
        config["default_station"] = found.id
        self._save_config(config)
        store.log_event("station", f"default station set to {found.name}")
        return web.json_response({"ok": True, "message": f"Default station is now {found.name}"})

    async def test_station(self, request: web.Request) -> web.Response:
        """Resolve a station without playing it - the "does this link work" button."""
        station_id = request.match_info.get("station_id", "")
        found = stations.find_station(self.config, station_id)
        if found is None:
            raise DashboardError(f"No station with the id '{station_id}'.", status=404)
        started = time.monotonic()
        runner = getattr(self.provider, "players", None)
        run_blocking = getattr(runner, "run_blocking", None)
        try:
            track = await sources.build_track(found, runner=run_blocking)
        except sources.SourceError as exc:
            raise DashboardError(str(exc), status=422) from exc
        took = time.monotonic() - started
        return web.json_response(
            {
                "ok": True,
                "message": f"Resolved in {took:.1f}s",
                "track": track.to_dict(),
                "seconds": round(took, 2),
            }
        )

    async def library(self, _request: web.Request) -> web.Response:
        summary = sources.library_summary()
        summary["expectedPath"] = str(paths.source_dir() / "music")
        summary["extensions"] = sorted(paths.AUDIO_EXTENSIONS)
        return web.json_response(summary)

    async def generative(self, _request: web.Request) -> web.Response:
        import generative

        cached = []
        try:
            cached = sorted(
                (
                    {
                        "name": item.name,
                        "bytes": item.stat().st_size,
                        "modified": item.stat().st_mtime,
                    }
                    for item in paths.CACHE_DIR.glob("gen-*.wav")
                ),
                key=lambda row: -row["modified"],
            )[:20]
        except OSError:  # pragma: no cover - an unreadable cache dir
            cached = []
        return web.json_response(
            {
                "available": generative.available(),
                "reason": generative.NUMPY_IMPORT_ERROR,
                "recipes": generative.recipe_names(),
                "cacheDir": str(paths.CACHE_DIR),
                "cached": cached,
            }
        )

    async def dashboard_settings(self, _request: web.Request) -> web.Response:
        config = self.config
        return web.json_response(
            {
                "host": self._host,
                "port": self._port,
                "publicUrl": self._public_url,
                "allowedHosts": list(config.get("dashboard_allowed_hosts") or []),
                "allowAnyHost": self._allow_any_host,
                "secureCookie": self._secure_cookie,
                "trustedProxies": list(self._trusted_proxies),
                "tls": bool(self._tls_context),
                "tlsCert": str(config.get("dashboard_tls_cert") or ""),
                "tlsKey": str(config.get("dashboard_tls_key") or ""),
                "enabled": bool(config.get("dashboard_enabled", True)),
                "warnings": self._warnings,
                "urls": self.urls(),
                "tokenLength": len(self._token or ""),
                "dataDir": str(paths.DATA_DIR),
                "configPath": str(paths.config_path()),
                "dbPath": str(paths.DB_PATH),
                "ffmpeg": paths.ffmpeg_executable(),
                "opus": paths.opus_library(),
                "version": paths.app_version(),
            }
        )

    async def save_dashboard_settings(self, request: web.Request) -> web.Response:
        payload = await _json(request)
        config = dict(self.config)
        changed: list[str] = []

        if "host" in payload:
            config["dashboard_host"] = str(payload["host"]).strip() or "127.0.0.1"
            changed.append("host")
        if "port" in payload:
            try:
                port = int(payload["port"])
            except (TypeError, ValueError) as exc:
                raise DashboardError("The port must be a number.") from exc
            if not 0 < port < 65536:
                raise DashboardError("The port must be between 1 and 65535.")
            config["dashboard_port"] = port
            changed.append("port")
        if "publicUrl" in payload:
            config["dashboard_public_url"] = str(payload["publicUrl"]).strip()
            changed.append("public URL")
        if "allowedHosts" in payload:
            raw = payload["allowedHosts"]
            items = raw if isinstance(raw, list) else str(raw).replace(",", "\n").split("\n")
            config["dashboard_allowed_hosts"] = [str(item).strip() for item in items if str(item).strip()]
            changed.append("allowed hosts")
        if "trustedProxies" in payload:
            raw = payload["trustedProxies"]
            items = raw if isinstance(raw, list) else str(raw).replace(",", "\n").split("\n")
            config["dashboard_trusted_proxies"] = [str(item).strip() for item in items if str(item).strip()]
            changed.append("trusted proxies")
        if "secureCookie" in payload:
            config["dashboard_secure_cookie"] = bool(payload["secureCookie"])
            changed.append("secure cookie")
        if "enabled" in payload:
            config["dashboard_enabled"] = bool(payload["enabled"])
            changed.append("enabled")
        if "regenerateToken" in payload and payload["regenerateToken"]:
            config["dashboard_token"] = secrets.token_urlsafe(36)
            changed.append("a new login key")
            self._sessions.clear()

        if not changed:
            raise DashboardError("Nothing to save.")
        self._save_config(config)
        # Re-read the network settings so the next request is judged by them.
        # The listener itself keeps its old binding until a restart, which the
        # response says plainly rather than pretending otherwise.
        self._apply_settings(config)
        store.log_event("settings", "dashboard settings changed: " + ", ".join(changed))
        needs_restart = any(name in {"host", "port", "enabled"} for name in changed)
        return web.json_response(
            {
                "ok": True,
                "message": "Saved " + ", ".join(changed) + (" (restart the bot to rebind the port)" if needs_restart else ""),
                "warnings": self._warnings,
                "urls": self.urls(),
                "token": config.get("dashboard_token") if "a new login key" in changed else None,
            }
        )


def _generative_available() -> bool:
    try:
        import generative

        return generative.available()
    except Exception:  # pragma: no cover - numpy import failure
        return False


def _guild_id(request: web.Request) -> int:
    raw = request.match_info.get("guild_id", "")
    if not raw.isdigit():
        raise DashboardError("That is not a server id.", status=404)
    return int(raw)


def _query_limit(request: web.Request, default: int = 50) -> int:
    """``?limit=`` as a safe, bounded integer.

    A query string is attacker-controlled text, so ``int(raw)`` on it would turn
    ``?limit=abc`` into a 500 and a traceback in the operator's log. Anything
    unparseable falls back to the default, and everything is capped so one
    request cannot ask for the whole history table.
    """
    raw = str(request.query.get("limit", "") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_HISTORY_ROWS))


def _query_guild_id(payload: dict) -> Optional[int]:
    """An optional guild id from a JSON body, or ``None`` for "every server"."""
    raw = str(payload.get("guildId", "") or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise DashboardError("That is not a server id.", status=404)
    return int(raw)


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "BotProvider",
    "DashboardError",
    "DashboardServer",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "MAX_HISTORY_ROWS",
    "Provider",
    "SESSION_COOKIE",
    "ensure_dashboard_token",
]
