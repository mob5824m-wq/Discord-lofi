"""Lofi - a Discord bot that joins a voice channel and plays lofi, plus a dashboard.

Run it with::

    python3 bot.py                  # connect to Discord and start the dashboard
    python3 bot.py --check          # diagnose the install without connecting
    python3 bot.py --demo           # dashboard only, with simulated servers
    python3 bot.py --dashboard-token  # print the dashboard login key

The bot needs a Discord token with the *no privileged intents* defaults
(``guilds`` and ``voice_states`` are enough), **PyNaCl** for voice encryption,
an **ffmpeg** binary and **libopus**. ``--check`` tests these and prints what
to install for whatever is missing, because a voice bot that cannot play audio
otherwise fails silently - it joins the channel, says nothing, and logs nothing.

A first run has no token to read, so it asks for one and stores it in
``config.json``: that single question is the whole first-run setup, and no
later run asks again. The prompt is skipped when stdin is not a terminal -
systemd, Docker and pipes have nobody to answer it, and those runs are told
what to set instead of blocking on a read that never comes.

Everything else is optional and degrades on its own:

* **yt-dlp** - only needed for YouTube stations. Without it, the internet-radio
  and offline stations still work.
* **numpy** - only needed for Studio Lofi, the beats rendered on this machine.
* **a music folder** - ``./music``, for the local library station.

Configuration lives in ``config.json`` (see :mod:`paths` for where that is read
from and written to) and holds the bot token, the global defaults, per-server
overrides and the dashboard's network settings. Play history lives in SQLite in
the data directory, so a config rewrite cannot lose it and a database reset
cannot lose the settings.

The dashboard (:mod:`dashboard`) shares this process and its event loop, which
is why it can show live player state and drive playback rather than polling a
second copy of the truth. It is bound to loopback by default and needs a
login key; see ``docs/DASHBOARD.md`` for exposing it safely.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

import command_tree
import dashboard
import generative
import music
import paths
import permissions
import player as player_module
import settings
import sources
import stations
import store
from player import PlayerError, PlayerManager


logger = logging.getLogger("lofi.bot")

# Read from the VERSION file (bundled into the binary by lofi.spec) so an
# installed build reports the version it was actually released as. A hardcoded
# string here drifts the moment scripts/bump_version.py runs.
VERSION = paths.app_version()
MIN_PYTHON = (3, 9)
#: Rotating log: 5 MB x 3, in the data directory next to the database.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

DEFAULT_CONFIG: dict[str, Any] = dict(paths.DEFAULT_CONFIG, version=VERSION)


class LofiBot(commands.Bot):
    """The bot: config, players, commands and the dashboard, in one loop."""

    def __init__(self, config: Optional[dict] = None, *, start_dashboard: bool = True) -> None:
        intents = discord.Intents.default()
        # voice_states is what lets the bot notice it was moved or kicked;
        # default() already includes it and requests no privileged intents.
        intents.voice_states = True
        intents.guilds = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.config: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.players = PlayerManager(self)
        self.dashboard: Optional[dashboard.DashboardServer] = None
        self._start_dashboard = start_dashboard
        self._ready_at: Optional[float] = None
        self.voice_ready = False
        self.started_at = time.time()

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    def save_config(self, updated: dict) -> None:
        """Persist and adopt a new config in one step.

        Adopting it in memory as well as on disk matters: the players read
        ``bot.config`` live, so a dashboard change to volume or a station must
        take effect without a restart.
        """
        paths.write_config(updated)
        self.config = updated

    def apply_config(self, updated: dict) -> None:
        """Persist a config change, logging (but not raising) on a read-only disk."""
        try:
            self.save_config(updated)
        except OSError as exc:
            logger.error("Settings changed in memory but could not be saved: %s", exc)
            self.config = updated

    def dashboard_token(self) -> str:
        return dashboard.ensure_dashboard_token(self.config, self.save_config)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def setup_hook(self) -> None:
        """Prepare everything that must exist before the gateway connects."""
        store.init_db()
        closed = store.close_dangling_sessions("the bot restarted")
        if closed:
            logger.info("Closed %d play session(s) left open by a previous run.", closed)

        self.voice_ready = self._probe_voice()
        if not self.voice_ready:
            logger.warning(
                "Voice support is not ready: one or more of PyNaCl, libopus or ffmpeg is "
                "missing. Run 'python3 bot.py --check' for the fix. The dashboard still works."
            )

        self.tree.add_command(command_tree.build(self))
        self.tree.on_error = self._on_command_error  # type: ignore[method-assign]

        provider = dashboard.BotProvider(self, self.players)
        self.dashboard = dashboard.DashboardServer(provider, save_config=self.save_config)
        if self._start_dashboard:
            try:
                await self.dashboard.start()
            except ValueError as exc:
                logger.error("Dashboard configuration is invalid: %s", exc)
            except OSError as exc:
                logger.error(
                    "The dashboard could not bind %s:%s (%s). Another copy may already be "
                    "running; use --dashboard-port to pick a different one.",
                    self.config.get("dashboard_host"), self.config.get("dashboard_port"), exc,
                )

    def _probe_voice(self) -> bool:
        """Can this process actually send audio? Checked once, up front."""
        # discord.py logs its own warning when `nacl` cannot be imported, but
        # does not stop the client from starting. Check it here as well so the
        # app does not advertise voice playback as ready when encryption cannot
        # work.
        pynacl_ok = _pynacl_version() is not None
        opus_ok = False
        try:
            opus_ok = discord.opus.is_loaded()
        except Exception:
            opus_ok = False
        if not opus_ok:
            library = paths.opus_library()
            if library and library != "loaded":
                try:
                    discord.opus.load_opus(library)
                    opus_ok = True
                except Exception as exc:
                    logger.debug("Could not load opus from %s: %s", library, exc)
        ffmpeg_ok = bool(paths.ffmpeg_executable())
        return bool(pynacl_ok and opus_ok and ffmpeg_ok)

    async def on_ready(self) -> None:
        self._ready_at = time.time()
        logger.info(
            "Connected as %s (id %s) in %d server(s).",
            self.user, getattr(self.user, "id", "?"), len(self.guilds),
        )
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening, name="lofi · /lofi play"
                )
            )
        except discord.DiscordException as exc:  # pragma: no cover - cosmetic
            logger.debug("Could not set the presence: %s", exc)

        if self.config.get("sync_commands_on_start", True):
            await self.sync_commands()

        report = await self.players.autostart()
        for line in report:
            logger.info("Autostart: %s", line)

        if self.dashboard is not None and self.dashboard.running:
            for url in self.dashboard.urls():
                logger.info("Dashboard: %s", url)
            logger.info("Dashboard key: run 'python3 bot.py --dashboard-token' to print it.")
        store.log_event("ready", f"connected as {self.user} in {len(self.guilds)} server(s)")

    async def sync_commands(self, guild_id: Optional[int] = None) -> dict[str, Any]:
        """Publish the slash commands, globally and to any configured server.

        Per-guild syncs apply instantly; the global sync can take up to an hour
        to propagate. Doing both is why a fresh install gets working commands
        in the server it was invited to right away.
        """
        result: dict[str, Any] = {"global": 0, "guilds": {}}
        try:
            synced = await self.tree.sync()
            result["global"] = len(synced)
        except discord.HTTPException as exc:
            logger.error("Global command sync failed: %s", exc)
            result["error"] = str(exc)
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                result["guilds"][str(guild.id)] = len(synced)
            except discord.HTTPException as exc:
                logger.warning("Command sync failed for %s: %s", guild.name, exc)
        return result

    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Turn a command failure into a message a person can act on."""
        message = str(error)
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original
            if isinstance(original, PlayerError):
                message = str(original)
            elif isinstance(original, discord.Forbidden):
                message = "The bot is missing a permission needed for that. Run /lofi status."
            elif isinstance(original, discord.NotFound):
                message = "That channel or role no longer exists."
            else:
                logger.exception("Unhandled command error", exc_info=original)
                message = "Something went wrong running that command; it has been logged."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down - try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use that command here."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.DiscordException:  # pragma: no cover - interaction expired
            pass

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await self.players.handle_voice_state(member, before, after)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Log the join and say hello where the bot is allowed to."""
        logger.info("Joined %s (%d members)", guild.name, guild.member_count)
        store.log_event("guild-join", f"joined {guild.name}", guild_id=guild.id)
        channel = _first_speakable_channel(guild)
        if channel is None:
            return
        embed = discord.Embed(
            title="Lofi is here",
            description=(
                "Join a voice channel and run `/lofi play` to start a station.\n"
                "`/lofi stations` lists them, `/lofi setup` configures this server, and "
                "`/lofi status` checks the bot's permissions."
            ),
            colour=music.EMBED_COLOUR,
        )
        embed.set_footer(text="Studio Lofi renders beats locally - it works with no internet at all")
        with contextlib.suppress(discord.DiscordException):
            await channel.send(embed=embed)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info("Removed from %s", guild.name)
        store.log_event("guild-remove", f"removed from {guild.name}", guild_id=guild.id)
        player = self.players.get(guild.id, create=False)
        if player is not None:
            await player.stop(reason="disconnect")

    async def close(self) -> None:
        """Shut everything down in order: players, dashboard, then the gateway."""
        with contextlib.suppress(Exception):
            await self.players.shutdown()
        if self.dashboard is not None:
            with contextlib.suppress(Exception):
                await self.dashboard.close()
        await super().close()

    @property
    def uptime_seconds(self) -> float:
        base = self._ready_at or self.started_at
        return max(0.0, time.time() - base)


def _first_speakable_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """A channel the bot may post in, preferring the system channel."""
    candidate = getattr(guild, "system_channel", None)
    if candidate is not None and permissions.check(guild.me, "send_messages", candidate):
        return candidate
    for channel in guild.text_channels:
        if permissions.check(guild.me, "send_messages", channel) and not channel.is_nsfw():
            return channel
    return None


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class _SafeStreamHandler(logging.StreamHandler):
    """A StreamHandler that never raises UnicodeEncodeError on emoji.

    In containers or locales with LANG=C the stdout encoding can be latin-1,
    and a log line containing 📚 or 🎧 would otherwise crash the logging
    thread with ``UnicodeEncodeError: 'latin-1' codec can't encode character``.
    The handler falls back to ``backslashreplace`` so the message is still
    visible and logging keeps working.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Do not call super().emit() because StreamHandler.emit() catches
        # *all* exceptions and turns them into a "Logging error" traceback
        # on stderr, which is exactly the spam the user reported (repeated
        # 31 times). Instead, re-implement the write with an explicit
        # UnicodeEncodeError fallback.
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                # Fallback: backslashreplace makes the emoji visible as
                # \U0001f4da and is always encodable in latin-1.
                enc = getattr(stream, "encoding", None) or "utf-8"
                fallback = msg.encode(enc, errors="backslashreplace").decode(enc, errors="replace")
                try:
                    stream.write(fallback + self.terminator)
                except UnicodeEncodeError:
                    # Last resort: replace any remaining unencodable chars.
                    safer = fallback.encode(enc, errors="replace").decode(enc, errors="replace")
                    stream.write(safer + self.terminator)
            self.flush()
        except RecursionError:  # See logging.StreamHandler
            raise
        except Exception:
            self.handleError(record)


def setup_logging(level: str = "INFO", logfile: Optional[str] = None) -> None:
    """Console plus a rotating file log in the data directory."""
    # Try to make stdout/stderr utf-8 so emoji like 📚 never raises. This is
    # best-effort: in some embedded interpreters reconfigure is unavailable.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    handlers: list[logging.Handler] = [_SafeStreamHandler(sys.stdout)]
    target = logfile or paths.LOG_PATH
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
            )
        )
    except OSError as exc:
        print(f"Could not open the log file {target}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("discord").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #
def _pynacl_version() -> Optional[str]:
    """Return PyNaCl's version only when its voice bindings really import."""
    try:
        module = importlib.import_module("nacl")
        # Importing the bindings also loads PyNaCl's native sodium extension;
        # a top-level `nacl` package alone is not enough for Discord voice.
        importlib.import_module("nacl.bindings")
    except Exception:
        return None
    version = getattr(module, "__version__", None)
    return version if isinstance(version, str) else "installed"


def _pynacl_fixes() -> list[str]:
    """Installation advice differs for source installs and frozen binaries."""
    if getattr(sys, "frozen", False):
        return [
            "This Lofi binary should bundle PyNaCl. Upgrade to a release with the packaging fix; "
            "if the warning persists, report `lofi --version` and your OS/architecture."
        ]
    return [
        "Use the same Python that runs Lofi: python -m pip install -r requirements.txt",
        "Windows on ARM64: python -m pip install -r requirements-winarm.txt",
    ]


def _check_rows() -> list[tuple[str, str, str, list[str]]]:
    """``(label, state, detail, fixes)`` for everything audio needs."""
    rows: list[tuple[str, str, str, list[str]]] = []

    python_ok = sys.version_info >= MIN_PYTHON
    rows.append(
        (
            "Python",
            "ok" if python_ok else "missing",
            sys.version.split()[0],
            [] if python_ok else [f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required."],
        )
    )

    discord_version = getattr(discord, "__version__", "unknown")
    rows.append(("discord.py", "ok", discord_version, []))

    pynacl_version = _pynacl_version()
    rows.append(
        (
            "PyNaCl",
            "ok" if pynacl_version else "missing",
            f"{pynacl_version} (voice encryption)"
            if pynacl_version
            else "not importable (Discord voice will not be supported)",
            [] if pynacl_version else _pynacl_fixes(),
        )
    )

    ffmpeg = paths.ffmpeg_executable()
    rows.append(
        (
            "ffmpeg",
            "ok" if ffmpeg else "missing",
            ffmpeg or "not found on PATH",
            []
            if ffmpeg
            else [
                "Linux: sudo apt install ffmpeg   ·   macOS: brew install ffmpeg",
                "Windows: winget install Gyan.FFmpeg (or choco install ffmpeg)",
                "Anywhere: pip install imageio-ffmpeg (a bundled binary Lofi will find)",
            ],
        )
    )

    opus_loaded = False
    with contextlib.suppress(Exception):
        opus_loaded = discord.opus.is_loaded()
    opus_hint = paths.opus_library()
    opus_override = os.environ.get("LOFI_OPUS", "").strip()
    if opus_loaded:
        opus_detail = "loaded"
    elif opus_hint:
        opus_detail = opus_hint
    elif opus_override:
        # Naming the bad override beats "not found": the library may well be
        # installed, and LOFI_OPUS is pointing somewhere else.
        opus_detail = f"LOFI_OPUS points at {opus_override}, which could not be opened"
    else:
        opus_detail = "not found"
    rows.append(
        (
            "libopus",
            "ok" if (opus_loaded or opus_hint) else "missing",
            opus_detail,
            []
            if (opus_loaded or opus_hint)
            else [
                "Linux: sudo apt install libopus0   ·   macOS: brew install opus",
                "Windows: download libopus-0.dll and put it next to bot.py",
            ],
        )
    )

    ytdlp = _module_version("yt_dlp")
    rows.append(
        (
            "yt-dlp",
            "ok" if ytdlp else "missing",
            ytdlp or "not installed (YouTube stations will not resolve)",
            [] if ytdlp else ["pip install -U yt-dlp"],
        )
    )

    rows.append(
        (
            "numpy",
            "ok" if generative.available() else "missing",
            _module_version("numpy") or "not installed (Studio Lofi is disabled)",
            [] if generative.available() else ["pip install numpy"],
        )
    )

    library = paths.music_dir()
    count = len(sources.scan_library()) if library else 0
    rows.append(
        (
            "music folder",
            "ok" if count else "optional",
            f"{library} ({count} track(s))" if library else "none - the library station has nothing to play",
            [] if count else [f"Create {paths.source_dir() / 'music'} and drop audio files in it."],
        )
    )

    config = paths.load_config(DEFAULT_CONFIG)
    token = str(config.get("bot_token") or os.environ.get("LOFI_TOKEN") or "").strip()
    rows.append(
        (
            "bot token",
            "ok" if token else "missing",
            f"set ({len(token)} characters)" if token else "empty",
            []
            if token
            else [
                "Put the token in config.json as \"bot_token\", or export LOFI_TOKEN.",
                "Create the application at https://discord.com/developers/applications",
                "Or run Lofi with no token set: the first run asks for it.",
            ],
        )
    )

    writable = paths.config_write_path()
    can_write = _writable(writable)
    rows.append(
        (
            "config file",
            "ok" if can_write else "warn",
            f"{paths.config_path()} (writes go to {writable})",
            [] if can_write else [f"Make {writable.parent} writable, or set LOFI_HOME."],
        )
    )

    rows.append(
        (
            "data directory",
            "ok" if os.access(str(paths.DATA_DIR), os.W_OK) else "warn",
            str(paths.DATA_DIR),
            [] if os.access(str(paths.DATA_DIR), os.W_OK) else ["Set LOFI_HOME to a writable folder."],
        )
    )

    if config.get("dashboard_enabled", True):
        host = str(os.environ.get("LOFI_DASHBOARD_HOST") or config.get("dashboard_host") or "127.0.0.1")
        port = os.environ.get("LOFI_DASHBOARD_PORT") or config.get(
            "dashboard_port", dashboard.DEFAULT_PORT
        )
        reachable_off_machine = not host.startswith("127.") and host not in {"localhost", "::1"}
        rows.append(
            (
                "dashboard",
                "ok",
                f"will listen on {host}:{port}",
                []
                if not reachable_off_machine
                else [
                    f"It will be reachable from other machines on {host}:{port}. Set "
                    "dashboard_allowed_hosts to the name visitors will use, and serve it "
                    "over HTTPS - the login key is sent in clear text otherwise.",
                ],
            )
        )
    else:
        rows.append(("dashboard", "optional", "disabled in config.json", []))

    stations_total = len(stations.get_stations(config))
    offline = len([s for s in stations.get_stations(config) if not s.needs_network])
    rows.append(
        (
            "stations",
            "ok",
            f"{stations_total} available ({offline} work offline)",
            [],
        )
    )
    return rows


def _module_version(name: str) -> Optional[str]:
    try:
        module = __import__(name)
    except Exception:
        return None
    for attribute in ("version", "__version__"):
        value = getattr(module, attribute, None)
        if isinstance(value, str):
            return value
        if isinstance(value, type(sys)) or hasattr(value, "__version__"):
            nested = getattr(value, "__version__", None)
            if isinstance(nested, str):
                return nested
    return "installed"


def _writable(target: Any) -> bool:
    try:
        directory = os.path.dirname(str(target)) or "."
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".lofi-write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False


def run_check() -> int:
    """Print the install report. Exit code 1 when audio could not work."""
    rows = _check_rows()
    marks = {"ok": "✓", "missing": "✗", "warn": "!", "optional": "○"}
    width = max(len(label) for label, *_ in rows)
    print("\nLofi install check\n" + "-" * 62)
    fixes: list[str] = []
    blocking = False
    for label, state, detail, suggestions in rows:
        print(f" {marks.get(state, '?')} {label.ljust(width)}  {detail}")
        if state == "missing" and label in {"PyNaCl", "ffmpeg", "libopus", "bot token", "Python"}:
            blocking = True
        for suggestion in suggestions:
            fixes.append(f"  · {suggestion}")
    print("-" * 62)
    if fixes:
        print("To fix:")
        print("\n".join(dict.fromkeys(fixes)))
    if blocking:
        print("\nAudio will not work until the ✗ items above are fixed.")
        return 1
    print("\nEverything needed for audio is in place.")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _stdin_is_interactive() -> bool:
    """True when there is a person at the keyboard to answer a prompt.

    ``sys.stdin`` is absent in some frozen contexts and raises when read from
    after being closed, so neither is assumed.
    """
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        return False


def _prompt_for_token(config: dict) -> str:
    """Ask for the bot token when there is none, save it, and return it.

    A first run is one question. The token is the only thing Lofi cannot work
    out for itself - ffmpeg, libopus, the music folder and the dashboard key
    each have a default, a fallback or a diagnostic that names the fix - so the
    prompt asks for exactly that, and the answer is written to ``config.json``
    so the next run starts without asking.

    An empty answer is not an error: it falls through to the instructions that
    say where the token goes, which is also what a non-interactive run gets.
    """
    if not _stdin_is_interactive():
        return ""
    print("First run - Lofi needs a Discord bot token to connect.")
    print("Create one at https://discord.com/developers/applications -> Bot -> Reset Token.")
    try:
        entered = input("Bot token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if not entered:
        return ""
    updated = dict(config)
    updated["bot_token"] = entered
    try:
        paths.write_config(updated)
    except OSError as exc:
        # The token is still good for this run; it just will not be remembered.
        logger.warning("Could not save the token to %s: %s", paths.config_write_path(), exc)
    else:
        config.clear()
        config.update(updated)
        print(f"Saved to {paths.config_write_path()} - you will not be asked again.\n")
    return entered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lofi",
        description="A Discord bot that plays lofi in a voice channel, with a web dashboard.",
    )
    parser.add_argument("--check", action="store_true", help="diagnose the install and exit")
    parser.add_argument("--demo", action="store_true", help="run the dashboard only, with simulated servers")
    parser.add_argument("--dashboard-token", action="store_true", help="print the dashboard login key and exit")
    parser.add_argument("--no-dashboard", action="store_true", help="do not start the web dashboard")
    parser.add_argument("--dashboard-host", help="override dashboard_host")
    parser.add_argument("--dashboard-port", type=int, help="override dashboard_port")
    parser.add_argument(
        "--dashboard-allowed-hosts",
        help='comma-separated Host headers to accept ("*" allows any, for a local preview)',
    )
    parser.add_argument("--sync-commands", action="store_true", help="sync slash commands and exit")
    parser.add_argument("--log-level", default=os.environ.get("LOFI_LOG_LEVEL", "INFO"))
    parser.add_argument("--logfile", help="write logs here instead of the data directory")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Command-line flags beat config.json, which beats the defaults."""
    updated = dict(config)
    if args.dashboard_host:
        updated["dashboard_host"] = args.dashboard_host
    if args.dashboard_port:
        updated["dashboard_port"] = int(args.dashboard_port)
    if args.dashboard_allowed_hosts:
        updated["dashboard_allowed_hosts"] = [
            item.strip() for item in args.dashboard_allowed_hosts.split(",") if item.strip()
        ]
    if args.no_dashboard:
        updated["dashboard_enabled"] = False
    return updated


async def _run_demo(args: argparse.Namespace) -> int:
    """Serve the dashboard against simulated servers, with no Discord login."""
    import demo

    config = _apply_cli_overrides(paths.load_config(DEFAULT_CONFIG), args)
    config["dashboard_enabled"] = True
    config.setdefault("dashboard_host", "127.0.0.1")
    # Simulated play history must not land in the real database.
    if not os.environ.get("LOFI_DB"):
        os.environ["LOFI_DB"] = str(paths.DATA_DIR / "demo.db")
    store.init_db()
    # The demo uses the same stored key as a real run, so `--dashboard-token`
    # and a running `--demo` always agree about what to type.
    token = dashboard.ensure_dashboard_token(config, paths.write_config)
    provider = demo.DemoProvider(config, token=token)
    server = dashboard.DashboardServer(provider, save_config=provider.save_config)
    await server.start()
    print("\nDemo dashboard - no Discord connection, simulated servers.")
    for url in server.urls():
        print(f"  open:  {url}")
    print(f"  key:   {token}")
    print("  (also printable with 'python3 bot.py --dashboard-token')")
    print("\nCtrl-C to stop.\n", flush=True)
    stop = asyncio.Event()

    def request_stop(*_args: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, request_stop)
    try:
        await stop.wait()
    finally:
        await server.close()
    return 0


async def _run_sync(bot: LofiBot, token: str) -> int:
    """Connect, publish commands, disconnect. Used by --sync-commands."""
    async with bot:
        await bot.login(token)
        result = await bot.sync_commands()
        print(f"Synced {result['global']} global command(s).")
        for guild_id, count in result["guilds"].items():
            print(f"  guild {guild_id}: {count} command(s)")
        if result.get("error"):
            print(f"Global sync error: {result['error']}")
            return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # stdout is a pipe under systemd, Docker and CI: without line buffering a
    # printed dashboard key or --check report can sit in the buffer unseen.
    # Also force utf-8 so emoji in logs (📚) never crashes with latin-1.
    with contextlib.suppress(AttributeError, ValueError, TypeError):
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"Lofi {VERSION}")
        return 0
    if args.check:
        return run_check()

    setup_logging(args.log_level, args.logfile)
    config = _apply_cli_overrides(paths.load_config(DEFAULT_CONFIG), args)

    if args.demo:
        return asyncio.run(_run_demo(args))

    try:
        token_value = dashboard.ensure_dashboard_token(config, paths.write_config)
    except ValueError as exc:
        logger.error("Dashboard token problem: %s", exc)
        return 2

    if args.dashboard_token:
        print(token_value)
        return 0

    token = str(config.get("bot_token") or os.environ.get("LOFI_TOKEN") or "").strip()
    if not token:
        token = _prompt_for_token(config)
    if not token:
        print(
            "No bot token found. Put it in config.json as \"bot_token\" (or export LOFI_TOKEN).\n"
            "Create one at https://discord.com/developers/applications, then run "
            "'python3 bot.py --check' to confirm the rest of the install.\n"
            "Want to see the dashboard first? Run 'python3 bot.py --demo'.",
            file=sys.stderr,
        )
        return 2

    if args.sync_commands:
        bot = LofiBot(config, start_dashboard=False)
        return asyncio.run(_run_sync(bot, token))

    bot = LofiBot(config, start_dashboard=bool(config.get("dashboard_enabled", True)))

    async def run() -> None:
        async with bot:
            await bot.start(token)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except discord.LoginFailure:
        print(
            "Discord rejected the bot token. Check config.json (or LOFI_TOKEN) - "
            "the token is on the Bot page of the developer portal.",
            file=sys.stderr,
        )
        return 2
    except discord.PrivilegedIntentsRequired:
        print(
            "Discord asked for a privileged intent. Lofi needs none: use "
            "discord.Intents.default() and disable Members/Presence/Message Content "
            "in the developer portal.",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        logger.error("Network problem: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
