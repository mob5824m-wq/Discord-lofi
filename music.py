"""The slash-command surface: one tree, ``/lofi``.

Everything the bot does from Discord lives under a single group so the whole
command list is discoverable by typing ``/lofi`` and reading - the same idea
Sentinel uses with ``/manage``.

Two permission tiers, both re-checked at run time rather than left to Discord's
command gating:

**transport** - ``play``, ``pause``, ``resume``, ``skip``, ``stop``, ``volume``,
``loop``, ``queue``, ``studio``, ``join``, ``leave``. Allowed for:
administrators, members with *Manage Server*, members holding the configured
**DJ role** when one is set, and otherwise anyone sitting in the same voice
channel as the bot. That last rule is the important one: without it, somebody
in a different channel could skip the music for a room they are not in.

**configuration** - ``setup`` and ``station add/remove/default``. Requires
*Manage Server* (or Administrator), checked on every invocation, so a stale
command registration cannot widen it.

Discord's own ``default_permissions`` is also set on the configuration commands
so the client greys them out for members who cannot use them - but the
run-time check is what actually decides, because a role can change between the
moment the menu is drawn and the moment it is clicked.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands

import generative
import paths
import permissions
import settings
import sources
import stations
import store
from player import PlayerError, PlayerManager
from stations import Station


logger = logging.getLogger("lofi.commands")

MAX_AUTOCOMPLETE = 25
EMBED_COLOUR = discord.Colour(0xB18CFF)
ERROR_COLOUR = discord.Colour(0xFB7185)


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def _is_admin(member: Any) -> bool:
    return bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))


def _can_manage_guild(member: Any) -> bool:
    return bool(getattr(getattr(member, "guild_permissions", None), "manage_guild", False))


def can_control(member: Any, guild_config: dict, guild_id: int, player_channel_id: Optional[int]) -> Optional[str]:
    """``None`` when the member may drive playback, else the refusal message.

    A pure function of the member, the config and where the bot currently is,
    which is why it lives at module level rather than on the cog: it is the one
    rule worth testing exhaustively.
    """
    if member is None:
        return "That command has to be used inside a server."
    if _is_admin(member) or _can_manage_guild(member):
        return None
    dj_role_id = settings.get_guild_settings(guild_config, guild_id)["dj_role_id"]
    if dj_role_id:
        role_ids = {getattr(role, "id", None) for role in getattr(member, "roles", [])}
        if int(dj_role_id) in role_ids:
            return None
        role = None
        guild = getattr(member, "guild", None)
        if guild is not None:
            role = guild.get_role(int(dj_role_id))
        name = role.name if role is not None else "the DJ role"
        return f"Only members with **{name}** (or Manage Server) can control playback here."
    if player_channel_id is None:
        # Nothing is playing yet: whoever is in a voice channel gets to start it.
        if getattr(getattr(member, "voice", None), "channel", None) is not None:
            return None
        return "Join a voice channel first, then run that command again."
    member_channel = getattr(getattr(member, "voice", None), "channel", None)
    if member_channel is not None and member_channel.id == int(player_channel_id):
        return None
    return (
        "You have to be in the same voice channel as the bot to control what it plays. "
        f"Set a DJ role with `/lofi setup dj_role:` to allow controlling it from anywhere."
    )


def can_configure(member: Any) -> Optional[str]:
    """``None`` when the member may change settings, else the refusal message."""
    if member is None:
        return "That command has to be used inside a server."
    if _is_admin(member) or _can_manage_guild(member):
        return None
    return "That needs **Manage Server** (or Administrator)."


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #
def station_list_embed(config: dict, guild_id: Optional[int] = None) -> discord.Embed:
    """The station list, marking the guild's current default."""
    current = settings.get_guild_settings(config, guild_id)["station_id"] if guild_id else ""
    embed = discord.Embed(title="Stations", colour=EMBED_COLOUR)
    lines: list[str] = []
    for station in stations.get_stations(config):
        marker = " ▶" if station.id == current else ""
        kind = {"youtube": "YouTube", "stream": "radio", "library": "files", "generative": "generated"}.get(
            station.kind, station.kind
        )
        lines.append(f"{station.art} **{station.name}**{marker}  \n`{station.id}` · {kind}")
    embed.description = "\n".join(lines)[:4000] or "No stations configured."
    embed.set_footer(
        text="Play one with /lofi play station:<id> · add your own with /lofi station add"
    )
    return embed


def status_embed(
    player: Any,
    *,
    config: dict,
    guild: Any,
    ffmpeg: Optional[str],
    opus_ok: bool,
    version: str,
) -> discord.Embed:
    """Diagnostics: what is playing, what is configured, what would fail."""
    state = player.state()
    embed = discord.Embed(title="Lofi status", colour=EMBED_COLOUR)

    if state["connected"]:
        track = state["track"] or {}
        embed.description = (
            f"**{track.get('title', 'unknown')}**  \n"
            f"{state['channelName'] or 'voice'} · {state['listeners']} listener(s) · "
            f"volume {state['volume']}%"
            + (" · paused" if state["paused"] else "")
        )
    else:
        embed.description = "Not connected to a voice channel right now."

    if guild is not None:
        guild_settings = settings.get_guild_settings(config, guild.id)
        embed.add_field(
            name="This server",
            value=(
                f"station: `{guild_settings.get('station_id') or 'default'}`\n"
                f"voice channel: {guild_settings.get('voice_channel_id') or 'not set'}\n"
                f"autostart: {'yes' if guild_settings.get('autostart') else 'no'} · "
                f"24/7: {'yes' if guild_settings.get('always_on') else 'no'}\n"
                f"idle leave: {guild_settings.get('idle_minutes')} min · "
                f"status line: {'on' if guild_settings.get('channel_status') else 'off'}"
            ),
            inline=False,
        )

    audit = permissions.audit(getattr(guild, "me", None), player.channel) if guild is not None else []
    if audit:
        marks = {"ok": "✓", "missing": "✗", "optional": "○", "no-effect": "–"}
        embed.add_field(
            name="Voice permissions",
            value="\n".join(
                f"{marks.get(row['state'], '?')} {row['label']}"
                + ("" if row["state"] == "ok" else f" — {row['reason']}")
                for row in audit
            )[:1024],
            inline=False,
        )

    engine = f"ffmpeg: {'found' if ffmpeg else 'MISSING'} · opus: {'ok' if opus_ok else 'MISSING'}"
    embed.add_field(name="Audio engine", value=engine, inline=False)
    if state.get("lastError"):
        embed.add_field(name="Last problem", value=str(state["lastError"])[:1024], inline=False)
    embed.set_footer(text=f"Lofi {version} · /lofi stations for the list")
    return embed


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
class SetupGroup(app_commands.Group):
    """``/lofi setup …`` - per-server configuration, Manage Server only."""

    def __init__(self, bot: Any) -> None:
        super().__init__(name="setup", description="Configure Lofi for this server", guild_only=True)
        self.bot = bot

    async def _guard(self, interaction: discord.Interaction) -> Optional[str]:
        problem = can_configure(interaction.user)
        if problem:
            await _refuse(interaction, problem)
            return problem
        return None

    async def _save(self, interaction: discord.Interaction, updates: dict[str, Any], note: str) -> None:
        bot = self.bot
        config = settings.set_guild_settings(bot.config, interaction.guild_id, updates)
        bot.apply_config(config)
        store.log_event("settings", note, guild_id=interaction.guild_id, actor_id=interaction.user.id)
        player = bot.players.get(interaction.guild_id, create=False)
        if player is not None:
            if "volume" in updates:
                await player.set_volume(int(updates["volume"]))
            if "loop_mode" in updates:
                await player.set_loop_mode(str(updates["loop_mode"]))
            if updates.get("channel_status") is False:
                await player.update_channel_status(clear=True, force=True)
            elif updates.get("channel_status") is True:
                await player.update_channel_status(force=True)
        await _confirm(interaction, note)

    @app_commands.command(name="station", description="Set the default station for this server")
    @app_commands.describe(station="Station id or name (see /lofi stations)")
    @app_commands.default_permissions(manage_guild=True)
    async def station(self, interaction: discord.Interaction, station: str) -> None:
        if await self._guard(interaction):
            return
        found = stations.find_station(self.bot.config, station)
        if found is None:
            await _refuse(interaction, f"No station matches {station[:60]!r}. Try /lofi stations.")
            return
        await self._save(interaction, {"station_id": found.id}, f"default station set to {found.name}")

    @app_commands.command(name="voice_channel", description="Set the channel Lofi joins on autostart")
    @app_commands.describe(channel="The voice channel to use")
    @app_commands.default_permissions(manage_guild=True)
    async def voice_channel(self, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        if await self._guard(interaction):
            return
        await self._save(
            interaction, {"voice_channel_id": channel.id}, f"voice channel set to #{channel.name}"
        )

    @app_commands.command(name="text_channel", description="Where now-playing messages are posted")
    @app_commands.describe(channel="A text channel, or nothing to clear it")
    @app_commands.default_permissions(manage_guild=True)
    async def text_channel(
        self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ) -> None:
        if await self._guard(interaction):
            return
        await self._save(
            interaction,
            {"text_channel_id": channel.id if channel else None},
            f"now-playing channel set to #{channel.name}" if channel else "now-playing channel cleared",
        )

    @app_commands.command(name="volume", description="Set the default volume")
    @app_commands.describe(percent="0-150")
    @app_commands.default_permissions(manage_guild=True)
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]) -> None:
        if await self._guard(interaction):
            return
        await self._save(interaction, {"volume": percent}, f"default volume set to {percent}%")

    @app_commands.command(name="idle", description="Minutes of an empty channel before Lofi leaves")
    @app_commands.describe(minutes="0 to never leave on its own")
    @app_commands.default_permissions(manage_guild=True)
    async def idle(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 0, 1440]) -> None:
        if await self._guard(interaction):
            return
        note = "idle disconnect disabled" if minutes == 0 else f"idle disconnect set to {minutes} minute(s)"
        await self._save(interaction, {"idle_minutes": minutes}, note)

    @app_commands.command(name="autostart", description="Start playing automatically when the bot boots")
    @app_commands.describe(enabled="True to turn on")
    @app_commands.default_permissions(manage_guild=True)
    async def autostart(self, interaction: discord.Interaction, enabled: bool) -> None:
        if await self._guard(interaction):
            return
        current = settings.get_guild_settings(self.bot.config, interaction.guild_id)
        if enabled and not current["voice_channel_id"]:
            await _refuse(
                interaction,
                "Autostart needs a channel to join. Run `/lofi setup voice_channel:` first.",
            )
            return
        await self._save(interaction, {"autostart": enabled}, f"autostart {'enabled' if enabled else 'disabled'}")

    @app_commands.command(name="always_on", description="Stay connected even when the channel is empty")
    @app_commands.describe(enabled="True for 24/7 mode")
    @app_commands.default_permissions(manage_guild=True)
    async def always_on(self, interaction: discord.Interaction, enabled: bool) -> None:
        if await self._guard(interaction):
            return
        await self._save(
            interaction,
            {"always_on": enabled, **({"idle_minutes": 0} if enabled else {})},
            "24/7 mode enabled - the bot stays connected" if enabled else "24/7 mode disabled",
        )

    @app_commands.command(name="dj_role", description="Restrict playback control to one role")
    @app_commands.describe(role="The DJ role, or nothing to clear it")
    @app_commands.default_permissions(manage_guild=True)
    async def dj_role(self, interaction: discord.Interaction, role: Optional[discord.Role] = None) -> None:
        if await self._guard(interaction):
            return
        await self._save(
            interaction,
            {"dj_role_id": role.id if role else None},
            f"DJ role set to @{role.name}" if role else "DJ role cleared",
        )

    @app_commands.command(name="announce", description="Post a now-playing message when the station changes")
    @app_commands.describe(enabled="True to post messages")
    @app_commands.default_permissions(manage_guild=True)
    async def announce(self, interaction: discord.Interaction, enabled: bool) -> None:
        if await self._guard(interaction):
            return
        await self._save(interaction, {"announce": enabled}, f"announcements {'enabled' if enabled else 'disabled'}")

    @app_commands.command(name="status_line", description="Show the current track as the voice channel's status")
    @app_commands.describe(enabled="True to show it")
    @app_commands.default_permissions(manage_guild=True)
    async def status_line(self, interaction: discord.Interaction, enabled: bool) -> None:
        if await self._guard(interaction):
            return
        await self._save(
            interaction,
            {"channel_status": enabled},
            "voice channel status enabled" if enabled else "voice channel status disabled",
        )

    @app_commands.command(name="reset", description="Clear this server's overrides back to the defaults")
    @app_commands.default_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction) -> None:
        if await self._guard(interaction):
            return
        config = dict(self.bot.config)
        guilds = dict(config.get("guilds") or {})
        guilds.pop(str(interaction.guild_id), None)
        config["guilds"] = guilds
        self.bot.apply_config(config)
        store.log_event("settings", "server settings reset to defaults", guild_id=interaction.guild_id)
        await _confirm(interaction, "This server is back to the global defaults.")


class StationGroup(app_commands.Group):
    """``/lofi station …`` - the custom station list, Manage Server only."""

    def __init__(self, bot: Any) -> None:
        super().__init__(name="station", description="Manage custom stations", guild_only=True)
        self.bot = bot

    @app_commands.command(name="add", description="Add a custom station (URL, folder or 'generative')")
    @app_commands.describe(name="What to call it", url="A stream URL, YouTube link, library: path, or generative")
    @app_commands.default_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, name: str, url: str) -> None:
        problem = can_configure(interaction.user)
        if problem:
            await _refuse(interaction, problem)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            station_id, clean_name, kind = stations.validate_station(name=name, url=url)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        station = Station(
            id=station_id, name=clean_name, kind=kind, url=url.strip(), art="🔗", description="added from Discord"
        )
        # Prove it resolves before saving it: a station that cannot play is
        # worse than an error now.
        if station.kind in {stations.KIND_YOUTUBE, stations.KIND_LIBRARY}:
            try:
                await sources.build_track(station, runner=self.bot.players.run_blocking)
            except sources.SourceError as exc:
                await interaction.followup.send(
                    f"That station was not saved - it cannot be played yet: {exc}", ephemeral=True
                )
                return
        config = stations.save_custom_station(self.bot.config, station)
        self.bot.apply_config(config)
        store.log_event("station", f"added station {clean_name} ({kind})", guild_id=interaction.guild_id)
        await interaction.followup.send(
            f"Saved **{clean_name}** (`{station_id}`). Play it with `/lofi play station:{station_id}`.",
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Remove a custom station (or restore a built-in one)")
    @app_commands.describe(station="Station id")
    @app_commands.default_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, station: str) -> None:
        problem = can_configure(interaction.user)
        if problem:
            await _refuse(interaction, problem)
            return
        config, removed = stations.remove_custom_station(self.bot.config, station.strip().lower())
        if not removed:
            await _refuse(interaction, f"No custom station with the id `{station[:40]}`.")
            return
        self.bot.apply_config(config)
        store.log_event("station", f"removed station {station}", guild_id=interaction.guild_id)
        built_in = station.strip().lower() in stations.BUILT_IN_BY_ID
        suffix = " The built-in version is back." if built_in else ""
        await _confirm(interaction, f"Removed `{station[:40]}`.{suffix}")

    @app_commands.command(name="default", description="Set the station used when /lofi play has no argument")
    @app_commands.describe(station="Station id or name")
    @app_commands.default_permissions(manage_guild=True)
    async def default(self, interaction: discord.Interaction, station: str) -> None:
        problem = can_configure(interaction.user)
        if problem:
            await _refuse(interaction, problem)
            return
        found = stations.find_station(self.bot.config, station)
        if found is None:
            await _refuse(interaction, f"No station matches {station[:60]!r}.")
            return
        config = dict(self.bot.config)
        config["default_station"] = found.id
        self.bot.apply_config(config)
        await _confirm(interaction, f"The global default station is now **{found.name}**.")


class LofiGroup(app_commands.Group):
    """``/lofi`` - the whole command surface."""

    #: Discord ignores ``guild_only`` on subcommands ("due to a Discord
    #: limitation"), so it is set on the groups themselves: every command here
    #: needs a guild, and this stops Discord offering ``/lofi`` in a DM where
    #: none of them could ever work. The per-command decorators stay as
    #: documentation of intent.
    def __init__(self, bot: Any) -> None:
        super().__init__(name="lofi", description="Play lofi in a voice channel", guild_only=True)
        self.bot = bot
        # Nested groups are added as commands: discord.py treats a Group inside
        # a Group as a subcommand group, giving /lofi setup ... and
        # /lofi station ...
        self.add_command(SetupGroup(bot))
        self.add_command(StationGroup(bot))

    # --- helpers ------------------------------------------------------- #
    def _player(self, interaction: discord.Interaction):
        return self.bot.players.get(interaction.guild_id)

    async def _control_guard(self, interaction: discord.Interaction) -> bool:
        """True when the caller may drive playback (refuses and returns False otherwise)."""
        player = self._player(interaction)
        channel_id = getattr(getattr(player, "channel", None), "id", None)
        problem = can_control(interaction.user, self.bot.config, interaction.guild_id, channel_id)
        if problem:
            await _refuse(interaction, problem)
            return False
        return True

    async def _station_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = (current or "").strip().lower()
        matches: list[app_commands.Choice[str]] = []
        for station in stations.get_stations(self.bot.config):
            haystack = f"{station.id} {station.name} {' '.join(station.tags)}".lower()
            if not needle or needle in haystack:
                matches.append(app_commands.Choice(name=f"{station.art} {station.name}", value=station.id))
            if len(matches) >= MAX_AUTOCOMPLETE:
                break
        return matches

    # --- transport ----------------------------------------------------- #
    @app_commands.command(name="play", description="Join your voice channel and start a lofi station")
    @app_commands.describe(station="Station id, name or a URL (blank uses this server's default)", channel="Voice channel to join")
    @app_commands.autocomplete(station=_station_autocomplete)
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        station: Optional[str] = None,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        await interaction.response.defer(ephemeral=False)
        started = time.monotonic()
        try:
            track = await player.play(station, requested_by=interaction.user.id, channel=channel)
        except PlayerError as exc:
            await interaction.followup.send(embed=_error_embed(str(exc)), ephemeral=True)
            return
        took = time.monotonic() - started
        embed = player.now_playing_embed(track, requested_by=interaction.user.id)
        if took > 3.5:
            embed.set_footer(text=f"Took {took:.0f}s to resolve · /lofi for controls")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        if await player.pause():
            await _confirm(interaction, "Paused. `/lofi resume` when you want it back.", ephemeral=True)
        else:
            await _refuse(interaction, "Nothing is playing right now.")

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        if await player.resume():
            await _confirm(interaction, "Playing again.", ephemeral=True)
        else:
            await _refuse(interaction, "Playback was not paused.")

    @app_commands.command(name="skip", description="Skip to the next track")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        try:
            track = await player.skip(requested_by=interaction.user.id)
        except PlayerError as exc:
            await _refuse(interaction, str(exc))
            return
        await interaction.response.send_message(
            embed=player.now_playing_embed(track, requested_by=interaction.user.id)
        )

    @app_commands.command(name="stop", description="Stop playback and leave the channel")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        if not player.is_connected and player.current is None:
            await _refuse(interaction, "The bot is not playing in this server.")
            return
        await player.stop(reason="stop")
        await _confirm(interaction, "Stopped, and left the channel.", ephemeral=True)

    @app_commands.command(name="leave", description="Leave the voice channel without changing settings")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        if not player.is_connected:
            await _refuse(interaction, "The bot is not in a voice channel here.")
            return
        await player.stop(reason="disconnect")
        await _confirm(interaction, "Left the channel.", ephemeral=True)

    @app_commands.command(name="join", description="Join a voice channel without starting playback")
    @app_commands.describe(channel="The channel to join (defaults to yours)")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        target = channel or getattr(getattr(interaction.user, "voice", None), "channel", None)
        if target is None:
            await _refuse(interaction, "Join a voice channel first, or pass one with `channel:`.")
            return
        try:
            await player.connect(target, requested_by=interaction.user.id)
        except PlayerError as exc:
            await _refuse(interaction, str(exc))
            return
        await _confirm(interaction, f"Joined **#{target.name}**. Start the music with `/lofi play`.")

    @app_commands.command(name="volume", description="Set playback volume")
    @app_commands.describe(percent="0-150")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        applied = await player.set_volume(percent)
        await _confirm(interaction, f"Volume set to **{applied}%**.", ephemeral=True)

    @app_commands.command(name="loop", description="What happens when a track ends")
    @app_commands.describe(mode="off = station decides, station = restart it, queue = keep going through the queue")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="station (restart the same broadcast)", value="station"),
            app_commands.Choice(name="queue (keep playing through the queue)", value="queue"),
        ]
    )
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: str) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        applied = await player.set_loop_mode(mode)
        await _confirm(interaction, f"Loop mode: **{applied}**.", ephemeral=True)

    @app_commands.command(name="queue", description="Add a one-off station or link after the current track")
    @app_commands.describe(item="A station id, name or URL")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, item: str) -> None:
        if not await self._control_guard(interaction):
            return
        player = self._player(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            track = await player.queue_track(item, requested_by=interaction.user.id)
        except PlayerError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Queued **{track.display_title}** ({len(player.queue)} in the queue).", ephemeral=True
        )

    @app_commands.command(name="studio", description="Play a beat rendered on this machine - no network needed")
    @app_commands.describe(mood="Which generated mood to play")
    @app_commands.choices(
        mood=[app_commands.Choice(name=recipe.name, value=recipe.id) for recipe in generative.RECIPES]
    )
    @app_commands.guild_only()
    async def studio(self, interaction: discord.Interaction, mood: str) -> None:
        if not await self._control_guard(interaction):
            return
        if not generative.available():
            await _refuse(interaction, generative.NUMPY_IMPORT_ERROR or "Studio Lofi needs numpy.")
            return
        recipe = generative.recipe_for(mood)
        player = self._player(interaction)
        await interaction.response.defer()
        try:
            # find_station() turns "studio-<recipe>" into the station, so the
            # player, the status line and the history all agree on its name.
            track = await player.play(f"studio-{recipe.id}", requested_by=interaction.user.id)
        except PlayerError as exc:
            await interaction.followup.send(embed=_error_embed(str(exc)), ephemeral=True)
            return
        embed = player.now_playing_embed(track, requested_by=interaction.user.id)
        embed.add_field(name="Mood", value=f"{recipe.name} · {recipe.bpm:.0f} BPM · {recipe.progression_text()}", inline=False)
        await interaction.followup.send(embed=embed)

    # --- information --------------------------------------------------- #
    @app_commands.command(name="now", description="What is playing right now")
    @app_commands.guild_only()
    async def now(self, interaction: discord.Interaction) -> None:
        player = self._player(interaction)
        if player.current is None:
            await _refuse(interaction, "Nothing is playing. Start something with `/lofi play`.")
            return
        await interaction.response.send_message(
            embed=player.now_playing_embed(player.current, requested_by=player.requested_by)
        )

    @app_commands.command(name="stations", description="List every station this bot can play")
    @app_commands.guild_only()
    async def stations_command(self, interaction: discord.Interaction) -> None:
        embed = station_list_embed(self.bot.config, interaction.guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="upnext", description="Show the queue and what comes next")
    @app_commands.guild_only()
    async def upnext(self, interaction: discord.Interaction) -> None:
        player = self._player(interaction)
        items = list(player.queue)
        embed = discord.Embed(title="Up next", colour=EMBED_COLOUR)
        if player.current is not None:
            embed.description = f"**Now:** {player.current.display_title}\n"
        if not items:
            station = player.station
            follow = "the same broadcast" if station and station.is_live else "the next track from the station"
            embed.description = (embed.description or "") + f"_The queue is empty - {follow} will play._"
        else:
            lines = [f"{index}. {track.display_title}" for index, track in enumerate(items[:20], start=1)]
            embed.description = (embed.description or "") + "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Diagnostics: playback, settings and permissions")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        player = self._player(interaction)
        embed = status_embed(
            player,
            config=self.bot.config,
            guild=interaction.guild,
            ffmpeg=paths.ffmpeg_executable(),
            opus_ok=bool(self.bot.voice_ready),
            version=paths.app_version(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="library", description="Show what is in the bot's local music folder")
    @app_commands.guild_only()
    async def library(self, interaction: discord.Interaction) -> None:
        summary = sources.library_summary()
        embed = discord.Embed(title="Local library", colour=EMBED_COLOUR)
        if not summary["exists"]:
            embed.description = (
                "There is no music folder yet. Create `music/` next to the bot and drop audio files in it - "
                "then `/lofi play station:library`."
            )
        else:
            embed.description = (
                f"`{summary['path']}`\n**{summary['count']}** track(s), "
                f"{summary['totalBytes'] / (1024 * 1024):.1f} MB"
            )
            names = [f"· {item['title']}" for item in summary["files"][:15]]
            if names:
                embed.add_field(name="Files", value="\n".join(names)[:1024], inline=False)
            if summary["truncated"]:
                embed.set_footer(text="Only the first 60 files are listed")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="That did not work", description=message[:1500], colour=ERROR_COLOUR)


async def _refuse(interaction: discord.Interaction, message: str, *, ephemeral: bool = True) -> None:
    """Send a refusal, whether or not the interaction has been deferred."""
    embed = _error_embed(message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.DiscordException:  # pragma: no cover - the interaction expired
        logger.debug("Could not deliver a refusal for %s", getattr(interaction, "id", "?"))


async def _confirm(
    interaction: discord.Interaction, message: str, *, ephemeral: bool = False
) -> None:
    """Send a short confirmation, whether or not the interaction was deferred."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
    except discord.DiscordException:  # pragma: no cover
        logger.debug("Could not deliver a confirmation for %s", getattr(interaction, "id", "?"))


def build_tree(bot: Any) -> LofiGroup:
    """Construct the command group for a bot instance."""
    return LofiGroup(bot)


__all__ = [
    "EMBED_COLOUR",
    "ERROR_COLOUR",
    "LofiGroup",
    "SetupGroup",
    "StationGroup",
    "build_tree",
    "can_configure",
    "can_control",
    "station_list_embed",
    "status_embed",
]
