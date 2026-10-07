"""Voice playback: one :class:`GuildPlayer` per server, kept alive by a watchdog.

A lofi station is not a queue of songs the user assembled - it is a stream that
should be playing whenever anybody is listening. That single assumption shapes
this module:

* when a track ends, the **station continues** (next file, next generated seed,
  or a reconnect to the same broadcast). Anything the user queued by hand plays
  first, then the station resumes. ``/lofi stop`` is the only thing that ends it.
* failures are **retried with a backoff**, not surfaced as a crash. YouTube
  links go stale, icecast hosts drop connections, ffmpeg dies on a corrupt
  file; the listener should hear a gap of a few seconds, not silence forever.
* a **watchdog task** owns everything that happens without an event: idle
  disconnects, stalled streams (a live feed that stops sending audio without
  closing the socket - the ``after`` callback never fires for those, so the
  player measures the time since ffmpeg last produced bytes), dropped voice
  connections, and refreshing the voice-channel status.

The watchdog is also what makes the dashboard honest: :meth:`GuildPlayer.state`
reports what the player is doing *now*, including "reconnecting (attempt 3)",
rather than what the last command claimed.

Voice-channel status uses ``PUT /channels/{id}/voice-status`` (discord.py's
``VoiceChannel.edit(status=...)``) and needs the *Set Voice Channel Status*
permission. It is best effort: if the permission is missing or Discord refuses,
the player notes it once, stops trying, and keeps playing music.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Callable, Optional

import discord

import generative
import paths
import permissions
import settings
import sources
import stations
import store
from sources import SourceError, Track
from stations import KIND_GENERATIVE, KIND_LIBRARY, KIND_STREAM, KIND_YOUTUBE, Station


logger = logging.getLogger("lofi.player")

#: How often the watchdog looks at every player.
WATCHDOG_INTERVAL = 5.0
#: A live stream that has produced no bytes for this long is treated as dead.
#: Longer than a TCP stall, shorter than anyone will tolerate silence.
STALL_SECONDS = 20.0
MAX_RESTART_ATTEMPTS = 6
RESTART_BACKOFF_CAP = 45.0
MAX_QUEUE = 50
#: Voice channel status accepts 500 characters; anything past ~90 is noise in
#: the channel list, so it is cut here with an ellipsis.
STATUS_MAX_LENGTH = 90
#: Minimum seconds between two status writes, so a station that skips through
#: short tracks cannot turn into a rate-limit conversation.
STATUS_MIN_INTERVAL = 8.0
#: How long an empty channel is tolerated before the bot leaves. ``0`` (from
#: ``idle_minutes``) means never.
DEFAULT_IDLE_MINUTES = 10

STOP_REASONS = {
    "stop": "stopped by a command",
    "idle": "left because the channel was empty",
    "disconnect": "disconnected",
    "moved": "moved to another channel",
    "kicked": "removed from the channel",
    "error": "playback failed repeatedly",
    "shutdown": "the bot is shutting down",
    "reload": "the station was replaced",
}


class PlayerError(RuntimeError):
    """Playback could not start or continue. The message is user-facing."""


class MonitoredAudio(discord.FFmpegPCMAudio):
    """``FFmpegPCMAudio`` that remembers when it last produced audio.

    discord.py only tells us when a source *ends* or raises. A 24/7 broadcast
    whose host stops sending data keeps the ffmpeg process alive and the pipe
    open, so the ``after`` callback never fires and the bot sits in a voice
    channel playing nothing. Measuring the gap between reads is what lets the
    watchdog notice.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_data_at = time.monotonic()
        self.bytes_read = 0
        self.reads = 0
        self.eof_at: Optional[float] = None

    def read(self) -> bytes:
        data = super().read()
        self.reads += 1
        if data:
            self.last_data_at = time.monotonic()
            self.bytes_read += len(data)
        elif self.eof_at is None:
            self.eof_at = time.monotonic()
        return data

    @property
    def seconds_since_audio(self) -> float:
        return time.monotonic() - self.last_data_at


class GuildPlayer:
    """Everything about playback in one guild."""

    def __init__(self, manager: "PlayerManager", guild_id: int) -> None:
        self.manager = manager
        self.guild_id = int(guild_id)

        self.voice: Optional[discord.VoiceClient] = None
        self.station: Optional[Station] = None
        self.current: Optional[Track] = None
        self.queue: deque[Track] = deque()
        self.paused = False
        self.loop_mode = "off"
        self.volume: int = settings.DEFAULT_VOLUME

        self.started_at: Optional[float] = None
        self.track_started_at: Optional[float] = None
        self.session_id: Optional[int] = None
        self.peak_listeners = 0
        self.last_error = ""
        self.last_error_at: Optional[float] = None
        self.restart_attempts = 0
        self.status_text = ""
        self.status_supported: Optional[bool] = None
        self.requested_by: Optional[int] = None

        self._lock = asyncio.Lock()
        self._stopping = False
        self._advance_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._prerender_task: Optional[asyncio.Task] = None
        self._empty_since: Optional[float] = None
        self._last_status_write = 0.0
        self._last_status_text: Optional[str] = None
        self._recent_library: deque[str] = deque(maxlen=sources.RECENT_LIBRARY_MEMORY)
        self._seed = random.randrange(1, 10_000_000)
        self._announced_station: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #
    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.manager.bot.get_guild(self.guild_id)

    @property
    def channel(self) -> Optional[discord.abc.Connectable]:
        return self.voice.channel if self.voice is not None else None

    @property
    def settings(self) -> dict[str, Any]:
        return settings.get_guild_settings(self.manager.config, self.guild_id)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self.manager.loop

    @property
    def transformer(self) -> Optional[discord.PCMVolumeTransformer]:
        source = getattr(self.voice, "source", None) if self.voice else None
        return source if isinstance(source, discord.PCMVolumeTransformer) else None

    @property
    def audio(self) -> Optional[MonitoredAudio]:
        transformer = self.transformer
        source = getattr(transformer, "original", None) if transformer else None
        return source if isinstance(source, MonitoredAudio) else None

    @property
    def is_connected(self) -> bool:
        return bool(self.voice and self.voice.is_connected())

    @property
    def is_playing(self) -> bool:
        return bool(self.voice and self.voice.is_playing() and not self.paused)

    def listeners(self) -> int:
        """Humans in the voice channel - the number the dashboard shows."""
        channel = self.channel
        if channel is None:
            return 0
        try:
            members = list(channel.members)
        except Exception:  # pragma: no cover - a channel that vanished mid-read
            return 0
        return sum(1 for member in members if not getattr(member, "bot", False))

    def elapsed_seconds(self) -> float:
        if not self.track_started_at:
            return 0.0
        return max(0.0, time.time() - self.track_started_at)

    # ------------------------------------------------------------------ #
    # Connecting
    # ------------------------------------------------------------------ #
    async def connect(self, channel: Any, *, requested_by: Optional[int] = None) -> None:
        """Join ``channel``, moving if already connected somewhere else."""
        if channel is None:
            raise PlayerError("Pick a voice channel first.")
        guild = self.guild
        if guild is None:
            raise PlayerError("The bot is not in that server.")
        problem = permissions.can_play(guild.me, channel)
        if problem:
            raise PlayerError(problem)
        if not hasattr(channel, "connect"):
            raise PlayerError(f"#{getattr(channel, 'name', 'that channel')} is not a voice channel.")

        if self.voice is not None and self.voice.is_connected():
            if getattr(self.voice.channel, "id", None) == getattr(channel, "id", None):
                return
            try:
                await self.voice.move_to(channel)
                logger.info("Moved to #%s in %s", channel.name, guild.name)
                store.log_event("moved", f"moved to #{channel.name}", guild_id=self.guild_id)
                return
            except (discord.DiscordException, AttributeError) as exc:
                logger.warning("Could not move to #%s (%s); reconnecting.", channel.name, exc)
                await self._teardown_voice()

        try:
            self.voice = await channel.connect(timeout=60.0, reconnect=True, self_deaf=True)
        except discord.ClientException as exc:
            # "Already connected" - the cache disagrees with us; take it over.
            existing = guild.voice_client
            if existing is not None:
                self.voice = existing
                try:
                    await existing.move_to(channel)
                except discord.DiscordException:
                    pass
            else:
                # A missing opus library also arrives as ClientException, and it
                # is the single most common install failure: it has to come back
                # with its own instructions rather than a generic message.
                raise PlayerError(_explain_connect_error(exc)) from exc
        except (discord.DiscordException, asyncio.TimeoutError, OSError) as exc:
            raise PlayerError(_explain_connect_error(exc)) from exc

        self.volume = self.settings["volume"]
        self.loop_mode = self.settings["loop_mode"]
        self._empty_since = None if self.listeners() else time.monotonic()
        self._ensure_watchdog()
        logger.info("Joined #%s in %s", getattr(channel, "name", channel), guild.name)
        store.log_event(
            "connect", f"joined #{getattr(channel, 'name', 'a voice channel')}",
            guild_id=self.guild_id, actor_id=requested_by,
        )

    def _ensure_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.loop.create_task(self._watchdog())

    async def _teardown_voice(self) -> None:
        voice, self.voice = self.voice, None
        if voice is None:
            return
        try:
            if voice.is_connected():
                await voice.disconnect(force=True)
        except (discord.DiscordException, OSError) as exc:
            logger.debug("Disconnect was not clean in guild %s: %s", self.guild_id, exc)

    # ------------------------------------------------------------------ #
    # Playing
    # ------------------------------------------------------------------ #
    async def play(
        self,
        station_reference: Any = None,
        *,
        requested_by: Optional[int] = None,
        channel: Any = None,
        queue: bool = False,
    ) -> Track:
        """Start (or queue) playback of a station.

        ``station_reference`` may be a station id, a name, or a raw URL - the
        same three things ``/lofi play`` accepts. With nothing supplied, the
        guild's configured station (or the global default) is used.
        """
        guild = self.guild
        if guild is None:
            raise PlayerError("The bot is not in that server.")
        current_settings = self.settings
        station: Optional[Station] = None
        if isinstance(station_reference, Station):
            # A synthesised station (``/lofi studio`` moods) is not in the
            # registry, and does not need to be: the player only needs the
            # resolution fields.
            station = station_reference
        elif station_reference:
            station = stations.find_station(self.manager.config, station_reference)
        if station_reference and station is None:
            raise PlayerError(
                f"I could not find a station called {str(station_reference)[:60]!r}. "
                "Try /lofi stations to see the list."
            )
        if station is None:
            station = self.station or stations.find_station(
                self.manager.config, current_settings["station_id"]
            ) or stations.default_station(self.manager.config)

        target_channel = channel or self.channel or self._configured_channel() or self._requester_channel(requested_by)
        if target_channel is None:
            raise PlayerError(
                "Join a voice channel first, or tell me which one to use "
                "(/lofi play channel: or /lofi setup voice_channel:)."
            )
        await self.connect(target_channel, requested_by=requested_by)

        track = await self._build_track(station, requested_by=requested_by)
        if queue:
            if len(self.queue) >= MAX_QUEUE:
                raise PlayerError(f"The queue is full ({MAX_QUEUE} items).")
            self.queue.append(track)
            store.log_event("queued", f"queued {track.display_title}", guild_id=self.guild_id, actor_id=requested_by)
            return track

        async with self._lock:
            self.station = station
            self.requested_by = requested_by
            self.restart_attempts = 0
            self.last_error = ""
            await self._start(track, requested_by=requested_by, announce=True)
        return track

    def _configured_channel(self) -> Any:
        guild = self.guild
        channel_id = self.settings["voice_channel_id"]
        if guild is None or not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        return channel if channel is not None else None

    def _requester_channel(self, requested_by: Optional[int]) -> Any:
        """The voice channel the person who asked is sitting in."""
        guild = self.guild
        if guild is None or not requested_by:
            return None
        member = guild.get_member(int(requested_by))
        return getattr(member, "voice", None).channel if member else None

    async def _build_track(
        self, station: Station, *, requested_by: Optional[int] = None, seed: Optional[int] = None
    ) -> Track:
        """Resolve a station into a track, retrying the easy mistakes once."""
        try:
            return await sources.build_track(
                station,
                requested_by=requested_by,
                seed=seed,
                recent=self._recent_library,
                runner=self.manager.run_blocking,
            )
        except SourceError as exc:
            message = str(exc)
            # A stale cached resolution is the one failure worth an immediate
            # second opinion; everything else will fail the same way again.
            if station.kind == KIND_YOUTUBE and ("403" in message or "Forbidden" in message):
                sources.clear_resolution_cache()
                try:
                    return await sources.build_track(
                        station,
                        requested_by=requested_by,
                        seed=seed,
                        recent=self._recent_library,
                        runner=self.manager.run_blocking,
                    )
                except SourceError:
                    pass
            raise PlayerError(message) from exc

    async def _start(self, track: Track, *, requested_by: Optional[int] = None, announce: bool = False) -> None:
        """Hand a track to the voice client. Caller holds ``_lock``."""
        voice = self.voice
        if voice is None or not voice.is_connected():
            raise PlayerError("The bot is not connected to a voice channel.")
        if track.kind == KIND_LIBRARY:
            self._recent_library.append(track.location)

        executable = paths.ffmpeg_executable()
        if not executable:
            raise PlayerError(
                "ffmpeg was not found on this machine. Install it (or 'pip install imageio-ffmpeg') "
                "and restart the bot - run 'python3 bot.py --check' for details."
            )

        try:
            audio = MonitoredAudio(
                track.location,
                executable=executable,
                before_options=sources.ffmpeg_before_options(track),
                options=track.ffmpeg_options,
            )
        except Exception as exc:  # FileNotFoundError / spawn failure
            raise PlayerError(f"Could not start ffmpeg for that track: {exc}") from exc

        previous = self.transformer
        try:
            transformer = discord.PCMVolumeTransformer(audio, volume=max(0.0, self.volume / 100.0))
        except Exception as exc:  # pragma: no cover - defensive
            audio.cleanup()
            raise PlayerError(f"Could not prepare audio: {exc}") from exc

        self._stopping = False
        voice.play(transformer, after=lambda error: self._after_callback(error, previous))

        now = time.time()
        first_track = self.session_id is None
        if first_track:
            self.started_at = now
            self.session_id = store.start_session(
                self.guild_id,
                station_id=track.station_id,
                station_name=track.station_name,
                track_title=track.display_title,
                channel_id=getattr(self.channel, "id", None),
                requested_by=requested_by,
            )
        else:
            store.update_session(self.session_id, track_title=track.display_title)
        self.current = track
        self.track_started_at = now
        self.paused = False
        self.restart_attempts = 0
        store.record_track_play(
            self.guild_id,
            station_id=track.station_id,
            title=track.display_title,
            listeners=self.listeners(),
        )
        self.loop.create_task(self._after_started(track, requested_by=requested_by, announce=announce))
        logger.info(
            "Playing %r (%s) in guild %s for %d listener(s)",
            track.display_title, track.kind, self.guild_id, self.listeners(),
        )

    def _after_callback(self, error: Optional[Exception], previous: Any) -> None:
        """discord.py's ``after`` hook - runs on the audio thread.

        Nothing here may touch the event loop directly, and the previous
        transformer is cleaned up explicitly: discord.py only cleans up the
        source it was given, so replacing one mid-playback would otherwise leak
        an ffmpeg subprocess per track.
        """
        if previous is not None:
            try:
                previous.cleanup()
            except Exception:  # pragma: no cover - cleanup is best effort
                pass
        self.loop.call_soon_threadsafe(self._schedule_advance, error)

    def _schedule_advance(self, error: Optional[Exception]) -> None:
        if self._stopping:
            return
        if error is not None:
            self.last_error = _short_error(error)
            self.last_error_at = time.time()
            logger.warning("Playback error in guild %s: %s", self.guild_id, self.last_error)
        if self._advance_task is None or self._advance_task.done():
            self._advance_task = self.loop.create_task(self._advance("track-ended" if error is None else "error"))

    async def _advance(self, reason: str) -> None:
        """Move to whatever should play next, with a backoff after failures."""
        async with self._lock:
            if self._stopping or self.voice is None:
                return
            station = self.station
            if station is None:
                return

            delay = 0.0
            if reason == "error" or reason == "stalled":
                self.restart_attempts += 1
                if self.restart_attempts > MAX_RESTART_ATTEMPTS:
                    await self._fail_out(reason)
                    return
                delay = min(RESTART_BACKOFF_CAP, 1.5 * (2 ** (self.restart_attempts - 1)))
                logger.info(
                    "Restarting %s in guild %s in %.1fs (attempt %d/%d)",
                    station.name, self.guild_id, delay, self.restart_attempts, MAX_RESTART_ATTEMPTS,
                )
            else:
                self.restart_attempts = 0

            next_track: Optional[Track] = None
            if self.queue:
                next_track = self.queue.popleft()
            else:
                # A live station is restarted rather than advanced: the "next
                # track" of a 24/7 broadcast is the same broadcast.
                seed = None
                if station.kind == KIND_GENERATIVE:
                    self._seed += 1
                    seed = self._seed
                try:
                    next_track = await self._build_track(
                        station, requested_by=self.requested_by, seed=seed
                    )
                except PlayerError as exc:
                    self.last_error = str(exc)
                    self.last_error_at = time.time()
                    self.restart_attempts += 1
                    if self.restart_attempts > MAX_RESTART_ATTEMPTS:
                        await self._fail_out("source-error")
                        return
                    delay = max(delay, min(RESTART_BACKOFF_CAP, 2.0 * self.restart_attempts))
                    next_track = None

            if delay:
                await asyncio.sleep(delay)
                if self._stopping or self.voice is None:
                    return

            if next_track is None:
                # Retry the same station once the backoff has passed.
                try:
                    seed = self._seed + 1 if station.kind == KIND_GENERATIVE else None
                    if seed:
                        self._seed = seed
                    next_track = await self._build_track(
                        station, requested_by=self.requested_by, seed=seed
                    )
                except PlayerError as exc:
                    self.last_error = str(exc)
                    await self._fail_out("source-error")
                    return

            try:
                await self._start(next_track, requested_by=self.requested_by)
            except PlayerError as exc:
                self.last_error = str(exc)
                logger.error("Could not start the next track in guild %s: %s", self.guild_id, exc)
                await self._fail_out("start-error")

    async def _fail_out(self, reason: str) -> None:
        """Give up audibly rather than sitting silent in a voice channel."""
        message = self.last_error or "playback kept failing"
        logger.error("Giving up in guild %s (%s): %s", self.guild_id, reason, message)
        store.log_event("error", f"playback stopped: {message}"[:300], guild_id=self.guild_id)
        await self._announce_error(message)
        await self.stop(reason="error")

    async def _after_started(
        self, track: Track, *, requested_by: Optional[int], announce: bool
    ) -> None:
        """Post-start side effects: status, announcement, pre-rendering."""
        try:
            await self.update_channel_status(force=True)
        except Exception as exc:  # pragma: no cover - status is best effort
            logger.debug("Voice channel status failed: %s", exc)
        if announce and self.settings["announce"]:
            station_changed = self._announced_station != track.station_id
            self._announced_station = track.station_id
            if station_changed or not track.is_live:
                await self.announce(track, requested_by=requested_by)
        if track.kind == KIND_GENERATIVE:
            self._prerender_next(track)

    def _prerender_next(self, track: Track) -> None:
        """Render the next generated track while this one plays.

        Rendering costs a few seconds of CPU; doing it during playback is what
        keeps a generated station gapless instead of pausing between tracks.
        """
        if self._prerender_task is not None and not self._prerender_task.done():
            return
        recipe = track.recipe or "midnight"
        seed = (track.seed or self._seed) + 1

        async def render() -> None:
            try:
                await self.manager.run_blocking(generative.render_track, recipe, seed)
            except Exception as exc:
                logger.debug("Pre-render of %s seed %s failed: %s", recipe, seed, exc)

        self._prerender_task = self.loop.create_task(render())

    # ------------------------------------------------------------------ #
    # Transport controls
    # ------------------------------------------------------------------ #
    async def pause(self) -> bool:
        if self.voice is None or not self.voice.is_playing():
            return False
        self.voice.pause()
        self.paused = True
        await self.update_channel_status(force=True)
        return True

    async def resume(self) -> bool:
        if self.voice is None or not self.voice.is_paused():
            return False
        self.voice.resume()
        self.paused = False
        await self.update_channel_status(force=True)
        return True

    async def toggle_pause(self) -> bool:
        """Pause or resume, returning ``True`` when now paused."""
        if self.paused:
            await self.resume()
            return False
        await self.pause()
        return True

    async def skip(self, *, requested_by: Optional[int] = None) -> Track:
        """Stop the current track and play the next one immediately."""
        if self.voice is None or self.current is None:
            raise PlayerError("Nothing is playing to skip.")
        self._stopping = True
        try:
            self.voice.stop()
        finally:
            self._stopping = False
        store.log_event(
            "skip", f"skipped {self.current.display_title}", guild_id=self.guild_id, actor_id=requested_by
        )
        if self._advance_task is None or self._advance_task.done():
            self._advance_task = self.loop.create_task(self._advance("skipped"))
            await asyncio.shield(self._advance_task)
        return self.current

    async def set_volume(self, percent: int) -> int:
        """Set playback volume (0-150) and remember it for this guild."""
        percent = settings.clamp_volume(percent, self.volume)
        self.volume = percent
        transformer = self.transformer
        if transformer is not None:
            transformer.volume = max(0.0, percent / 100.0)
        return percent

    async def set_loop_mode(self, mode: str) -> str:
        self.loop_mode = settings.normalise_loop_mode(mode)
        return self.loop_mode

    async def queue_track(self, reference: Any, *, requested_by: Optional[int] = None) -> Track:
        """Add a one-off track (a station, a URL or a file) after the current one."""
        station = stations.find_station(self.manager.config, reference)
        if station is None:
            raise PlayerError(f"I could not find anything to play for {str(reference)[:60]!r}.")
        if len(self.queue) >= MAX_QUEUE:
            raise PlayerError(f"The queue is full ({MAX_QUEUE} items).")
        track = await self._build_track(station, requested_by=requested_by)
        self.queue.append(track)
        return track

    def clear_queue(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    async def stop(self, reason: str = "stop", *, disconnect: bool = True) -> None:
        """Stop playback, close the session, and (by default) leave the channel."""
        self._stopping = True
        try:
            if self.voice is not None:
                try:
                    self.voice.stop()
                except Exception:  # pragma: no cover - a voice client already gone
                    pass
            await self.update_channel_status(clear=True)
            self._close_session(reason)
            self.current = None
            self.queue.clear()
            self.paused = False
            self.station = None if reason in {"stop", "shutdown", "error", "kicked"} else self.station
            self._announced_station = None
            if disconnect:
                await self._teardown_voice()
            store.log_event(
                "stop",
                f"playback stopped ({STOP_REASONS.get(reason, reason)})",
                guild_id=self.guild_id,
            )
        finally:
            self._stopping = False

    def _close_session(self, reason: str) -> None:
        """Write the finished play session to the database."""
        session_id, self.session_id = self.session_id, None
        if not session_id:
            return
        seconds = 0.0
        if self.started_at:
            seconds = max(0.0, time.time() - self.started_at)
        store.end_session(
            session_id,
            reason=STOP_REASONS.get(reason, reason),
            peak_listeners=self.peak_listeners,
            seconds_played=seconds,
        )
        self.started_at = None
        self.peak_listeners = 0

    # ------------------------------------------------------------------ #
    # Voice channel status
    # ------------------------------------------------------------------ #
    def status_payload(self) -> Optional[str]:
        """The text to show under the channel name, or ``None`` to clear it."""
        if not self.settings.get("channel_status", True):
            return None
        track = self.current
        if track is None or self.voice is None:
            return None
        prefix = track.art or (self.station.art if self.station else "🎧")
        label = track.display_title
        if self.paused:
            label = f"{label} (paused)"
        text = f"{prefix} {label}".strip()
        if len(text) > STATUS_MAX_LENGTH:
            text = text[: STATUS_MAX_LENGTH - 1].rstrip() + "…"
        return text

    async def update_channel_status(self, *, force: bool = False, clear: bool = False) -> bool:
        """Write (or clear) the voice channel's status text.

        Best effort by design: the permission is optional, the feature is
        cosmetic, and a refusal must never interrupt playback. The first
        refusal is recorded so ``/lofi status`` and the dashboard can explain
        why the status is not showing, instead of the operator guessing.
        """
        channel = self.channel
        guild = self.guild
        if channel is None or guild is None:
            return False
        if not clear and not self.settings.get("channel_status", True):
            # The server turned the feature off: do not write anything at all.
            return False
        if self.status_supported is False and not clear:
            return False
        text = None if clear else self.status_payload()
        if text is None and not clear and not force:
            return False
        if not force and text == self._last_status_text:
            return False
        now = time.monotonic()
        # A clear bypasses the rate limit: "stop" happens right after a start more
        # often than not, and the point of the limit is to avoid rewriting the
        # same text - not to leave a stale track under the channel name.
        if not force and not clear and now - self._last_status_write < STATUS_MIN_INTERVAL:
            return False
        if not permissions.check(guild.me, "set_voice_channel_status", channel):
            if self.status_supported is None:
                self.status_supported = False
                logger.info(
                    "Guild %s: no 'Set Voice Channel Status' permission; track status will not "
                    "be shown under the channel name.", self.guild_id,
                )
            return False

        try:
            await channel.edit(status=text)
        except discord.Forbidden as exc:
            self.status_supported = False
            logger.warning(
                "Discord refused the voice channel status in guild %s: %s", self.guild_id, exc
            )
            store.log_event(
                "warning", "voice channel status was refused (missing permission)", guild_id=self.guild_id
            )
            return False
        except discord.HTTPException as exc:
            # Rate limited or a transient failure: leave the old text alone and
            # let the next watchdog pass try again.
            logger.debug("Voice channel status update failed: %s", exc)
            return False

        self._last_status_write = time.monotonic()
        self._last_status_text = text
        self.status_text = text or ""
        self.status_supported = True
        return True

    # ------------------------------------------------------------------ #
    # Announcements
    # ------------------------------------------------------------------ #
    def _announcement_channel(self) -> Optional[discord.TextChannel]:
        guild = self.guild
        if guild is None:
            return None
        channel_id = self.settings["text_channel_id"]
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            # Duck-typed rather than isinstance(TextChannel): threads, news
            # channels and stage text chats can all carry the embed, and
            # requiring one exact class silently drops announcements elsewhere.
            if channel is not None and hasattr(channel, "send") and getattr(channel, "type", "") not in (
                "voice", "stage_voice", "stage", "category",
            ):
                return channel
        voice_channel = self.channel
        if voice_channel is not None:
            category = getattr(voice_channel, "category", None)
            if category is not None:
                for channel in category.text_channels:
                    if permissions.check(guild.me, "send_messages", channel):
                        return channel
        return None

    def now_playing_embed(self, track: Track, *, requested_by: Optional[int] = None) -> discord.Embed:
        """The now-playing card, also rendered as HTML in the dashboard."""
        colour = discord.Colour(0xB18CFF)
        embed = discord.Embed(title=f"Now playing · {track.station_name}", colour=colour)
        embed.description = f"**{track.display_title}**"
        if track.uploader:
            embed.add_field(name="From", value=track.uploader[:120], inline=True)
        kind_labels = {
            KIND_LIBRARY: "Local library",
            KIND_GENERATIVE: "Rendered on the bot",
            KIND_STREAM: "Internet radio",
            KIND_YOUTUBE: "YouTube broadcast",
        }
        embed.add_field(name="Source", value=kind_labels.get(track.kind, track.kind), inline=True)
        embed.add_field(
            name="Length",
            value="live" if track.is_live else _format_seconds(track.duration) if track.duration else "unknown",
            inline=True,
        )
        listeners = self.listeners()
        embed.add_field(name="Listeners", value=str(listeners), inline=True)
        embed.add_field(name="Volume", value=f"{self.volume}%", inline=True)
        if requested_by:
            embed.set_footer(text=f"Requested by <@{requested_by}> · /lofi for controls")
        else:
            embed.set_footer(text="Auto-started · /lofi for controls")
        if track.artwork_url:
            embed.set_thumbnail(url=track.artwork_url)
        return embed

    async def announce(self, track: Track, *, requested_by: Optional[int] = None) -> bool:
        """Post the now-playing card, if an announcement channel can be found."""
        channel = self._announcement_channel()
        if channel is None:
            return False
        try:
            await channel.send(embed=self.now_playing_embed(track, requested_by=requested_by))
        except discord.Forbidden:
            logger.info("Guild %s: cannot send now-playing messages in #%s", self.guild_id, channel.name)
            store.log_event(
                "warning", f"no permission to post now-playing in #{channel.name}", guild_id=self.guild_id
            )
            return False
        except discord.HTTPException as exc:
            logger.warning("Now-playing post failed in guild %s: %s", self.guild_id, exc)
            return False
        return True

    async def _announce_error(self, message: str) -> None:
        channel = self._announcement_channel()
        if channel is None:
            return
        embed = discord.Embed(
            title="Playback stopped",
            description=message[:1500],
            colour=discord.Colour(0xFB7185),
        )
        embed.set_footer(text="Fix the station from the dashboard, or run /lofi stations")
        try:
            await channel.send(embed=embed)
        except discord.DiscordException:
            pass

    # ------------------------------------------------------------------ #
    # Watchdog
    # ------------------------------------------------------------------ #
    async def _watchdog(self) -> None:
        """The periodic maintenance loop for this guild."""
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                try:
                    await self._watchdog_tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - a tick must never kill the loop
                    logger.exception("Watchdog tick failed for guild %s", self.guild_id)
                if self.voice is None and self._watchdog_task is not None:
                    # Nothing left to watch; the next connect() starts a new one.
                    return
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise

    async def _watchdog_tick(self) -> None:
        current_settings = self.settings
        listeners = self.listeners()
        self.peak_listeners = max(self.peak_listeners, listeners)

        # 1. Voice connection dropped (a Discord blip, or somebody moved us).
        if self.voice is not None and not self.voice.is_connected():
            logger.warning("Voice connection lost in guild %s; leaving cleanly.", self.guild_id)
            store.log_event("disconnect", "the voice connection dropped", guild_id=self.guild_id)
            await self.stop(reason="disconnect")
            return
        if self.voice is None:
            return

        # 2. Idle disconnect: an empty channel is paying for audio nobody hears.
        idle_minutes = current_settings["idle_minutes"]
        if listeners == 0 and not current_settings["always_on"] and idle_minutes > 0:
            if self._empty_since is None:
                self._empty_since = time.monotonic()
            elif time.monotonic() - self._empty_since > idle_minutes * 60:
                logger.info(
                    "Leaving guild %s: channel empty for %d minute(s)", self.guild_id, idle_minutes
                )
                await self.stop(reason="idle")
                return
        else:
            self._empty_since = None

        # 3. A live stream that stopped sending audio without closing.
        audio = self.audio
        if (
            audio is not None
            and self.voice.is_playing()
            and not self.paused
            and audio.seconds_since_audio > STALL_SECONDS
        ):
            logger.warning(
                "Stream stalled in guild %s (no audio for %.0fs); restarting.",
                self.guild_id, audio.seconds_since_audio,
            )
            store.log_event("stall", "the stream stopped sending audio; restarting", guild_id=self.guild_id)
            self._stopping = True
            try:
                self.voice.stop()
            finally:
                self._stopping = False
            if self._advance_task is None or self._advance_task.done():
                self._advance_task = self.loop.create_task(self._advance("stalled"))
            return

        # 4. Keep the channel status in step (Discord can drop it, and a pause
        #    or a volume change is worth reflecting).
        await self.update_channel_status()

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def state(self) -> dict[str, Any]:
        """A JSON-safe snapshot, used by the dashboard and ``/lofi status``."""
        track = self.current
        station = self.station
        channel = self.channel
        audio = self.audio
        connected = self.is_connected
        duration = None
        progress = None
        if track is not None:
            if track.is_live:
                duration = None
            elif track.duration:
                duration = round(float(track.duration), 1)
                progress = min(1.0, round(self.elapsed_seconds() / max(track.duration, 1.0), 4))
        return {
            "guildId": str(self.guild_id),
            "connected": connected,
            "playing": self.is_playing,
            "paused": self.paused,
            "channelId": str(getattr(channel, "id", "")) if channel is not None else None,
            "channelName": getattr(channel, "name", None) if channel is not None else None,
            "listeners": self.listeners(),
            "peakListeners": self.peak_listeners,
            "volume": self.volume,
            "loopMode": self.loop_mode,
            "station": station.to_dict() if station is not None else None,
            "track": track.to_dict() if track is not None else None,
            "elapsed": round(self.elapsed_seconds(), 1) if self.track_started_at else 0.0,
            "duration": duration,
            "progress": progress,
            "queue": [item.to_dict() for item in list(self.queue)[:20]],
            "queueLength": len(self.queue),
            "startedAt": self.started_at,
            "restartAttempts": self.restart_attempts,
            "lastError": self.last_error,
            "lastErrorAt": self.last_error_at,
            "statusText": self.status_text,
            "statusSupported": self.status_supported,
            "bitrateKbps": round((audio.bytes_read * 8 / 1000) / max(self.elapsed_seconds(), 1.0), 1)
            if audio is not None and self.track_started_at
            else None,
        }


def _explain_connect_error(exc: Exception) -> str:
    """Translate a voice-connect failure into something actionable."""
    text = str(exc).lower()
    if "opus" in text:
        return (
            "The opus audio library is missing, so the bot cannot send voice. "
            "On Linux install 'libopus0' (Debian/Ubuntu) or 'opus' (Homebrew on macOS); "
            "on Windows the bundled build usually works. Run 'python3 bot.py --check'."
        )
    if "ffmpeg" in text or "no such file" in text:
        return (
            "ffmpeg was not found. Install it, or 'pip install imageio-ffmpeg', then run "
            "'python3 bot.py --check' to confirm the bot can see it."
        )
    if "timed out" in text or "timeout" in text:
        return (
            "The voice connection timed out. This is usually UDP being blocked: Discord voice "
            "needs outbound UDP on ports 50000-65535, which some networks and VPNs filter."
        )
    if "already connected" in text:
        return "The bot is already connected in that server; disconnect it first (/lofi leave)."
    if "permission" in text or "missing" in text:
        return f"Discord refused the voice connection: {exc}"
    return f"Could not join the voice channel: {exc}"


def _short_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    return text[:280] or error.__class__.__name__


def _format_seconds(value: Optional[float]) -> str:
    if not value:
        return "unknown"
    total = int(round(float(value)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class PlayerManager:
    """Owns every :class:`GuildPlayer`, and the config they read from."""

    def __init__(
        self,
        bot: Any,
        *,
        save_config: Optional[Callable[[dict], None]] = None,
        run_blocking: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.bot = bot
        self._save_config = save_config
        self._run_blocking = run_blocking
        self.players: dict[int, GuildPlayer] = {}
        self.started_at = time.time()

    # --- plumbing ------------------------------------------------------ #
    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        try:
            return self.bot.loop
        except AttributeError:  # a test double without a loop attribute
            return asyncio.get_event_loop()

    @property
    def config(self) -> dict:
        return self.bot.config

    def save_config(self, config: dict) -> None:
        if self._save_config is not None:
            self._save_config(config)

    async def run_blocking(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run blocking work (yt-dlp, ffmpeg, rendering) off the event loop."""
        if self._run_blocking is not None:
            return await self._run_blocking(func, *args)
        return await self.loop.run_in_executor(None, func, *args)

    def get(self, guild_id: Any, *, create: bool = True) -> Optional[GuildPlayer]:
        key = int(guild_id)
        player = self.players.get(key)
        if player is None and create:
            player = GuildPlayer(self, key)
            self.players[key] = player
        return player

    def active_players(self) -> list[GuildPlayer]:
        """Players that are connected or still hold a session."""
        return [player for player in self.players.values() if player.is_connected or player.session_id]

    # --- lifecycle ----------------------------------------------------- #
    async def autostart(self) -> list[str]:
        """Start every guild configured to play by itself. Returns a report.

        Run once the bot is ready. Failures are collected rather than raised:
        one server with a deleted voice channel must not stop the others from
        coming up, and the operator needs the list to fix anything.
        """
        report: list[str] = []
        for guild_id in settings.autostart_guilds(self.config):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                report.append(f"guild {guild_id}: the bot is not in that server")
                continue
            player = self.get(guild_id)
            channel_id = player.settings["voice_channel_id"]
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel is None:
                report.append(f"{guild.name}: configured voice channel no longer exists")
                store.log_event("warning", "autostart skipped: voice channel is gone", guild_id=guild_id)
                continue
            try:
                await player.play(requested_by=None, channel=channel)
                report.append(f"{guild.name}: now playing in #{channel.name}")
            except PlayerError as exc:
                report.append(f"{guild.name}: {exc}")
                store.log_event("error", f"autostart failed: {exc}"[:300], guild_id=guild_id)
        return report

    async def handle_voice_state(
        self, member: Any, before: Any, after: Any
    ) -> None:
        """React to the bot being moved, deafened or kicked from a channel."""
        guild = getattr(member, "guild", None)
        if guild is None or getattr(member, "id", None) != getattr(self.bot.user, "id", None):
            return
        player = self.get(guild.id, create=False)
        if player is None:
            return
        if after.channel is None and before.channel is not None:
            logger.info("Removed from the voice channel in %s", guild.name)
            store.log_event("kicked", "the bot was removed from the voice channel", guild_id=guild.id)
            await player.stop(reason="kicked")
        elif after.channel is not None and before.channel is not None and after.channel.id != before.channel.id:
            store.log_event(
                "moved", f"moved to #{after.channel.name}", guild_id=guild.id
            )
            player._empty_since = None if player.listeners() else time.monotonic()

    async def shutdown(self, reason: str = "shutdown") -> None:
        """Stop every player, closing sessions so history is not left open."""
        for player in list(self.players.values()):
            try:
                await player.stop(reason=reason)
            except Exception as exc:  # pragma: no cover - shutdown must finish
                logger.warning("Shutdown: player for %s did not stop cleanly: %s", player.guild_id, exc)
            if player._watchdog_task is not None:
                player._watchdog_task.cancel()
        self.players.clear()

    # --- reporting ----------------------------------------------------- #
    def overview(self) -> dict[str, Any]:
        """Aggregate numbers for the dashboard's Overview page."""
        players = list(self.players.values())
        connected = [player for player in players if player.is_connected]
        playing = [player for player in connected if player.is_playing]
        return {
            "guilds": len(self.bot.guilds),
            "connected": len(connected),
            "playing": len(playing),
            "paused": len([player for player in connected if player.paused]),
            "listeners": sum(player.listeners() for player in connected),
            "peakListeners": sum(player.peak_listeners for player in connected),
            "sessions": store.session_count(),
            "listeningMinutes": round(store.listening_minutes(days=7), 1),
            "stations": len(stations.get_stations(self.config)),
            "libraryTracks": len(sources.scan_library()),
            "errors": len([player for player in players if player.last_error]),
            "uptimeSeconds": round(time.time() - self.started_at, 1),
        }


__all__ = [
    "DEFAULT_IDLE_MINUTES",
    "GuildPlayer",
    "MAX_QUEUE",
    "MonitoredAudio",
    "PlayerError",
    "PlayerManager",
    "STALL_SECONDS",
    "STOP_REASONS",
    "WATCHDOG_INTERVAL",
]
