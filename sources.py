"""Turning a station into something ffmpeg can actually play.

:mod:`stations` knows *what* can be played; this module works out *how*, and
produces a :class:`Track`: a concrete location on disk or on the network, plus
the metadata the player and the dashboard display.

Four kinds, four strategies:

``library``
    Pick the next file in the local folder. No network, no resolution, and the
    only kind guaranteed to work on a machine with no internet at all.
``generative``
    Render a track with :mod:`generative`. Also offline; the cost is CPU, so
    rendering runs in a thread executor and the player pre-renders the next
    track while the current one is still playing.
``stream``
    A plain HTTP(S) feed. Nothing to resolve: ffmpeg opens it directly, with
    reconnect flags so a dropped icecast connection recovers by itself.
``youtube``
    Resolve the watch/live URL to a direct media URL with yt-dlp, then hand
    *that* to ffmpeg. Running ffmpeg on the resolved URL (instead of piping
    yt-dlp's stdout into it) is what lets ffmpeg do its own reconnecting and
    keeps the audio path free of an extra process. Resolution happens in an
    executor because yt-dlp is blocking, and it is retried through the
    station's ``search`` query when the video id has gone stale.

Every failure is converted into a :class:`SourceError` carrying a message
written for a person: the same text ends up in a Discord interaction response,
a log line and a dashboard toast, and "HTTP Error 403: Forbidden" is not a
fix anybody can act on.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import paths
import stations
from stations import KIND_GENERATIVE, KIND_LIBRARY, KIND_STREAM, KIND_YOUTUBE, Station


logger = logging.getLogger("lofi.sources")

#: ffmpeg options per source kind. The reconnect flags matter most for live
#: radio: without them a single dropped TCP connection ends the stream and the
#: bot goes silent until somebody notices.
FFMPEG_OPTIONS: dict[str, str] = {
    KIND_STREAM: (
        "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 "
        "-reconnect_delay_max 5 -reconnect_max_retries 5 -timeout 15000000"
    ),
    KIND_YOUTUBE: (
        "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 "
        "-reconnect_delay_max 5 -timeout 15000000"
    ),
    KIND_LIBRARY: "-nostdin",
    KIND_GENERATIVE: "-nostdin",
}
#: Network sources get ffmpeg's own retry flags *before* ``-i``, so a dropped
#: TCP connection on a 24/7 stream is reconnected by ffmpeg instead of ending
#: the track. Local files do not need them (ffmpeg accepts and ignores them,
#: but a command line that only lists options that apply is easier to debug).
NETWORK_BEFORE_OPTIONS: tuple[str, ...] = (
    "-reconnect 1",
    "-reconnect_streamed 1",
    "-reconnect_at_eof 1",
    "-reconnect_delay_max 5",
    "-nostdin",
)


def ffmpeg_before_options(track: "Track") -> str:
    """The ``before_options`` string discord.py should pass for this track."""
    if track.kind in {KIND_STREAM, KIND_YOUTUBE}:
        return " ".join(NETWORK_BEFORE_OPTIONS)
    return "-nostdin"

#: How many recent library files to remember so a shuffle does not repeat.
RECENT_LIBRARY_MEMORY = 24
MAX_LIBRARY_FILES = 5000
#: A resolved YouTube URL is used once per track; yt-dlp reports expiry but
#: this is the age at which we stop trusting a cached resolution.
RESOLUTION_TTL_SECONDS = 60 * 45

_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TITLE_FROM_NAME = re.compile(r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+)$")


class SourceError(RuntimeError):
    """A station could not be turned into playable audio. Message is user-facing."""


@dataclass
class Track:
    """One playable item: what to open, and what to tell people about it."""

    title: str
    station_id: str
    station_name: str
    kind: str
    location: str
    is_live: bool = False
    duration: Optional[float] = None
    requested_by: Optional[int] = None
    art: str = "🎧"
    origin: str = ""
    artwork_url: Optional[str] = None
    uploader: Optional[str] = None
    station_url: str = ""
    seed: Optional[int] = None
    recipe: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON view for the dashboard. Snowflakes become strings."""
        return {
            "title": self.title,
            "stationId": self.station_id,
            "stationName": self.station_name,
            "kind": self.kind,
            "isLive": self.is_live,
            "duration": self.duration,
            "requestedBy": str(self.requested_by) if self.requested_by else None,
            "art": self.art,
            "origin": self.origin,
            "artworkUrl": self.artwork_url,
            "uploader": self.uploader,
            "seed": self.seed,
            "recipe": self.recipe,
        }

    @property
    def ffmpeg_options(self) -> str:
        return FFMPEG_OPTIONS.get(self.kind, "")

    @property
    def display_title(self) -> str:
        return self.title or self.station_name or "Unknown track"


# --------------------------------------------------------------------------- #
# Local library
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LibraryFile:
    """One audio file found under the music directory."""

    path: str
    title: str
    artist: Optional[str]
    size: int
    modified: float
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "size": self.size,
            "modified": self.modified,
        }


def _pretty_title(stem: str) -> tuple[Optional[str], str]:
    """Split ``Artist - Title`` filenames, falling back to the whole stem."""
    cleaned = re.sub(r"\s+", " ", stem).strip()
    match = _TITLE_FROM_NAME.match(cleaned)
    if match:
        artist = match.group("artist").strip()
        title = match.group("title").strip()
        # A leading track number ("03 - Song") is not an artist.
        if re.fullmatch(r"\d{1,3}", artist):
            return None, title
        return artist or None, title or cleaned
    return None, cleaned or stem


def scan_library(root: Optional[Path] = None, limit: int = MAX_LIBRARY_FILES) -> list[LibraryFile]:
    """Every audio file under the library directory, in a stable order.

    Symlinks are followed (people keep their music on another volume) but the
    walk keeps a visited-set so a symlink loop cannot hang the scan. The limit
    exists because the result is serialised to the dashboard, and a 50k-file
    collection would make that response enormous for no benefit.
    """
    directory = root if root is not None else paths.music_dir()
    if directory is None or not directory.is_dir():
        return []
    found: list[LibraryFile] = []
    visited: set[Path] = set()

    def walk(current: Path, depth: int) -> None:
        if len(found) >= limit:
            return
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved in visited:
            return
        visited.add(resolved)
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            logger.debug("Could not list %s: %s", current, exc)
            return
        for entry in entries:
            if len(found) >= limit:
                return
            try:
                if entry.is_symlink() and not entry.exists():
                    continue
                if entry.is_dir():
                    if depth < 6:
                        walk(entry, depth + 1)
                    continue
                if entry.suffix.lower() not in paths.AUDIO_EXTENSIONS:
                    continue
                info = entry.stat()
            except OSError:
                continue
            artist, title = _pretty_title(entry.stem)
            found.append(
                LibraryFile(
                    path=str(entry),
                    title=title,
                    artist=artist,
                    size=int(info.st_size),
                    modified=float(info.st_mtime),
                    depth=depth,
                )
            )

    walk(Path(directory), 0)
    return found


def library_summary(root: Optional[Path] = None) -> dict[str, Any]:
    """A small description of the library for the dashboard's Stations page."""
    directory = root if root is not None else paths.music_dir()
    files = scan_library(directory)
    return {
        "path": str(directory) if directory else None,
        "exists": bool(directory and directory.is_dir()),
        "count": len(files),
        "totalBytes": sum(item.size for item in files),
        "files": [item.to_dict() for item in files[:60]],
        "truncated": len(files) > 60,
    }


def pick_library_file(
    recent: Optional[Iterable[str]] = None,
    *,
    rng: Optional[random.Random] = None,
    root: Optional[Path] = None,
) -> Optional[LibraryFile]:
    """The next file to play, avoiding anything in ``recent``.

    A shuffle that can repeat the same three tracks is the most common
    complaint about simple players, so the caller passes the last few paths it
    played and they are excluded until the library has been through.
    """
    files = scan_library(root)
    if not files:
        return None
    chooser = rng or random
    excluded = set(recent or ())
    candidates = [item for item in files if item.path not in excluded]
    if not candidates:
        candidates = files
    return chooser.choice(candidates)


_DURATION_CACHE: dict[str, tuple[float, float]] = {}


def probe_duration(path: str, timeout: float = 8.0) -> Optional[float]:
    """Seconds of audio in ``path``, read from ffmpeg's banner. Cached.

    ffprobe is not always installed alongside ffmpeg (the bundled binaries ship
    ffmpeg alone), so this parses the ``Duration:`` line ffmpeg prints for
    ``-i`` with no output - one subprocess per file, once, then remembered.
    """
    executable = paths.ffmpeg_executable()
    if not executable:
        return None
    cached = _DURATION_CACHE.get(path)
    if cached is not None:
        return cached[1]
    try:
        completed = subprocess.run(
            [executable, "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not probe %s: %s", path, exc)
        return None
    match = _DURATION_PATTERN.search(completed.stderr or "")
    duration: Optional[float] = None
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if duration <= 0:
            duration = None
    try:
        modified = Path(path).stat().st_mtime
    except OSError:
        modified = 0.0
    _DURATION_CACHE[path] = (modified, duration if duration is not None else -1.0)
    return duration


# --------------------------------------------------------------------------- #
# YouTube resolution
# --------------------------------------------------------------------------- #
def _ytdlp_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        # A live broadcast has no "best quality" in the usual sense; asking
        # yt-dlp to prefer a stable audio-only format avoids the video-only
        # variants that would make ffmpeg decode frames nobody can see.
        "live_from_start": False,
        "socket_timeout": 20,
        "retries": 2,
        "extract_flat": False,
        "noprogress": True,
    }


def _resolve_youtube_blocking(reference: str) -> dict[str, Any]:
    """Blocking yt-dlp resolution. Always called through an executor."""
    try:
        import yt_dlp  # noqa: WPS433 - optional dependency, imported on use
    except Exception as exc:  # pragma: no cover - depends on the environment
        raise SourceError(
            "yt-dlp is not installed, so YouTube stations cannot be resolved. "
            "Install it with 'pip install -U yt-dlp'."
        ) from exc

    def extract(target: str) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(_ytdlp_options()) as downloader:
            return downloader.extract_info(target, download=False) or {}

    info: dict[str, Any] = {}
    error_text = ""
    try:
        info = extract(reference)
    except Exception as exc:  # yt_dlp.utils.DownloadError and friends
        error_text = str(exc)
    if not info.get("url"):
        # A playlist/CHANNEL page resolves to entries rather than a stream.
        entries = info.get("entries") or []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("url"):
                info = entry
                break
    if not info.get("url"):
        raise SourceError(_explain_youtube_error(reference, error_text, info))

    duration = info.get("duration")
    is_live = bool(info.get("is_live")) or info.get("duration") is None
    return {
        "url": str(info["url"]),
        "title": str(info.get("title") or "").strip(),
        "is_live": is_live,
        "duration": float(duration) if isinstance(duration, (int, float)) and duration else None,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "webpage_url": info.get("webpage_url") or reference,
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }


def _explain_youtube_error(reference: str, error_text: str, info: dict[str, Any]) -> str:
    """A message that says what to do, not just that YouTube said no."""
    text = (error_text or "").lower()
    if "sign in" in text or "confirm you" in text or "bot" in text:
        return (
            "YouTube is asking this connection to sign in, so the stream could not be "
            "resolved. Try another station, or add your own stream URL from the dashboard."
        )
    if "403" in text or "forbidden" in text:
        return (
            "YouTube refused the request (HTTP 403). This usually means yt-dlp is out of "
            "date - run 'pip install -U yt-dlp' on the bot host and try again."
        )
    if "404" in text or "not available" in text or "removed" in text or "private" in text:
        return (
            "That video is no longer available, so the station's link has gone stale. "
            "Update the station URL from the dashboard, or pick another station."
        )
    if "unable to download" in text or "tls" in text or "timed out" in text or "network" in text:
        return (
            "YouTube could not be reached from this machine. Check the bot's network "
            "connection, or use the offline Studio Lofi / My Library stations."
        )
    if "yt-dlp" in text or "no module" in text:
        return "yt-dlp is not installed. Run 'pip install -U yt-dlp' on the bot host."
    detail = " ".join((error_text or "").split())[:220]
    return f"YouTube could not resolve that link. {detail}".strip()


_RESOLUTION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


async def resolve_youtube(
    reference: str, *, loop: Optional[asyncio.AbstractEventLoop] = None, use_cache: bool = True
) -> dict[str, Any]:
    """Resolve a YouTube link to a direct media URL, off the event loop.

    Results are cached briefly: a server that replays the same station after a
    reconnect should not pay for another extraction, but a cache that outlives
    the URL's own expiry would hand ffmpeg a dead link.
    """
    runner = loop or asyncio.get_running_loop()
    cached = _RESOLUTION_CACHE.get(reference)
    if use_cache and cached and (cached[0] + RESOLUTION_TTL_SECONDS) > time.monotonic():
        return cached[1]
    resolved = await runner.run_in_executor(None, _resolve_youtube_blocking_cached, reference, use_cache)
    return resolved


def _resolve_youtube_blocking_cached(reference: str, use_cache: bool = True) -> dict[str, Any]:
    """Resolve inside the worker thread, updating the TTL cache there.

    The cache is written from whichever thread resolves, so two guilds asking
    for the same station at the same moment cannot both pay for an extraction.
    ``use_cache`` has to be honoured here as well as in the async wrapper, or a
    caller asking for a fresh extraction (the dashboard's "Test" button after a
    yt-dlp upgrade) would still be handed the stale one.
    """
    if use_cache:
        cached = _RESOLUTION_CACHE.get(reference)
        if cached and (cached[0] + RESOLUTION_TTL_SECONDS) > time.monotonic():
            return cached[1]
    resolved = _resolve_youtube_blocking(reference)
    _RESOLUTION_CACHE[reference] = (time.monotonic(), resolved)
    return resolved


def clear_resolution_cache() -> None:
    """Drop cached YouTube resolutions (after a yt-dlp upgrade, or on error)."""
    _RESOLUTION_CACHE.clear()


# --------------------------------------------------------------------------- #
# Track construction
# --------------------------------------------------------------------------- #
async def build_track(
    station: Station,
    *,
    requested_by: Optional[int] = None,
    seed: Optional[int] = None,
    recent: Optional[Iterable[str]] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    runner: Optional[Callable[..., Any]] = None,
) -> Track:
    """Resolve ``station`` into a :class:`Track` ready for the player.

    ``runner`` is an optional ``await``-able callable used for blocking work;
    tests pass one that never touches a real executor, and the player passes
    the bot's loop.
    """
    if station.kind == KIND_LIBRARY:
        return _library_track(station, requested_by=requested_by, recent=recent)
    if station.kind == KIND_GENERATIVE:
        return await _generative_track(station, requested_by=requested_by, seed=seed, runner=runner)
    if station.kind == KIND_YOUTUBE:
        return await _youtube_track(station, requested_by=requested_by, runner=runner)
    if station.kind == KIND_STREAM:
        return _stream_track(station, requested_by=requested_by)
    raise SourceError(f"Unknown station kind: {station.kind!r}")


def _library_track(
    station: Station, *, requested_by: Optional[int], recent: Optional[Iterable[str]]
) -> Track:
    """A track from the local folder, or a :class:`SourceError` explaining why not."""
    root = _library_root(station)
    explicit = _requested_library_path(station)
    if root is None and explicit:
        # Say which folder is missing instead of quietly falling back to ./music:
        # a station pointed at /srv/lofi that plays ./music is a bug nobody sees.
        raise SourceError(
            f"The library folder {explicit} does not exist on the machine running the bot. "
            "Create it and put audio files in it, or point the station at another folder."
        )
    chosen = pick_library_file(recent, root=root)
    if chosen is None:
        where = str(root) if root else "the music folder"
        raise SourceError(
            f"There is no audio in {where}. Drop some .mp3/.flac/.ogg files in there, "
            "or play the offline Studio Lofi station instead."
        )
    duration = probe_duration(chosen.path)
    title = f"{chosen.artist} – {chosen.title}" if chosen.artist else chosen.title
    return Track(
        title=title,
        station_id=station.id,
        station_name=station.name,
        kind=KIND_LIBRARY,
        location=chosen.path,
        duration=duration,
        requested_by=requested_by,
        art=station.art or "💿",
        origin=str(Path(chosen.path).relative_to(root)) if root else chosen.path,
        station_url=station.url,
    )


def _requested_library_path(station: Station) -> Optional[str]:
    """The folder a station explicitly names, or ``None`` for the default one."""
    raw = str(station.url or "").strip()
    if raw.lower().startswith(("library:", "folder:", "local:")):
        raw = raw.split(":", 1)[1].strip()
    if raw and raw.lower() not in {"music", "."}:
        return raw
    return None


def _library_root(station: Station) -> Optional[Path]:
    """Where a library station points: its URL, or the default music folder."""
    explicit = _requested_library_path(station)
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_dir() else None
    return paths.music_dir()


async def _generative_track(
    station: Station,
    *,
    requested_by: Optional[int],
    seed: Optional[int],
    runner: Optional[Callable[..., Any]],
) -> Track:
    """Render (or reuse) a generated track.

    The recipe comes from the station description convention ``generative:<id>``
    in its URL, so a server can have several generated stations with different
    moods without new code.
    """
    import generative  # local import: numpy is optional

    if not generative.available():
        raise SourceError(generative.NUMPY_IMPORT_ERROR or "Studio Lofi needs numpy.")
    recipe = generative.recipe_for(_recipe_reference(station))
    chosen_seed = int(seed) if seed is not None else random.randrange(1, 10_000_000)
    render = _blocking(runner, generative.render_track, recipe.id, chosen_seed)
    info = await render
    path = Path(str(info["path"]))
    if not path.is_file():  # pragma: no cover - render_track would have raised
        raise SourceError("The generated track could not be written to disk.")
    return Track(
        title=str(info["title"]),
        station_id=station.id,
        station_name=station.name,
        kind=KIND_GENERATIVE,
        location=str(path),
        duration=float(info["duration"]),
        requested_by=requested_by,
        art=station.art or "🎹",
        origin=f"{recipe.name} · seed {chosen_seed}",
        station_url=station.url,
        seed=chosen_seed,
        recipe=recipe.id,
    )


def _recipe_reference(station: Station) -> str:
    raw = str(station.url or "").strip()
    if ":" in raw:
        return raw.split(":", 1)[1].strip()
    return raw or "midnight"


async def _youtube_track(
    station: Station, *, requested_by: Optional[int], runner: Optional[Callable[..., Any]]
) -> Track:
    """Resolve the station's video, falling back to its search query."""
    candidates: list[str] = []
    if station.url:
        candidates.append(station.url)
    if station.search:
        candidates.append(f"ytsearch1:{station.search}")
    if not candidates:
        raise SourceError(f"Station {station.name!r} has no URL to resolve.")

    last_error: Optional[SourceError] = None
    for index, candidate in enumerate(candidates):
        try:
            resolved = await _blocking(runner, resolve_youtube_blocking, candidate)
        except SourceError as exc:
            last_error = exc
            logger.warning("Could not resolve %s (%s): %s", station.name, candidate, exc)
            continue
        if index > 0 and station.url:
            logger.info(
                "Station %s fell back to its search query (%s).", station.name, station.search
            )
        return Track(
            title=resolved["title"] or station.name,
            station_id=station.id,
            station_name=station.name,
            kind=KIND_YOUTUBE,
            location=resolved["url"],
            is_live=bool(resolved["is_live"]),
            duration=resolved["duration"],
            requested_by=requested_by,
            art=station.art or "📻",
            origin=resolved.get("webpage_url") or candidate,
            artwork_url=resolved.get("thumbnail"),
            uploader=resolved.get("uploader"),
            station_url=station.url or candidate,
        )
    raise last_error or SourceError(f"Could not resolve station {station.name!r}.")


def resolve_youtube_blocking(candidate: str) -> dict[str, Any]:
    """Sync wrapper used by the executor path (kept separate for tests)."""
    return _resolve_youtube_blocking(candidate)


def _stream_track(station: Station, *, requested_by: Optional[int]) -> Track:
    """An HTTP(S) feed: nothing to resolve, but the URL is sanity-checked."""
    url = str(station.url or "").strip()
    if not url:
        raise SourceError(f"Station {station.name!r} has no stream URL.")
    if stations.is_private_url(url):
        raise SourceError(
            "That stream address points at this machine or a private network, so it was refused."
        )
    return Track(
        title=station.name,
        station_id=station.id,
        station_name=station.name,
        kind=KIND_STREAM,
        location=url,
        is_live=True,
        requested_by=requested_by,
        art=station.art or "📡",
        origin=url,
        station_url=url,
    )


async def _blocking(runner: Optional[Callable[..., Any]], func: Callable[..., Any], *args: Any) -> Any:
    """Run ``func(*args)`` off the event loop.

    ``runner`` lets a caller (usually a test) supply its own executor policy;
    the default is the loop's thread pool.
    """
    if runner is not None:
        return await runner(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def ffmpeg_command(track: Track) -> list[str]:
    """The ffmpeg argv the player will effectively run, for ``--check`` output.

    discord.py builds this itself; exposing it means a support conversation can
    reproduce a failure by pasting one command into a shell.
    """
    executable = paths.ffmpeg_executable() or "ffmpeg"
    parts = [executable]
    before = ffmpeg_before_options(track)
    if before:
        parts.extend(shlex.split(before))
    parts.extend(
        [
            "-i",
            track.location,
            "-f",
            "s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-loglevel",
            "warning",
            "pipe:1",
        ]
    )
    return parts


__all__ = [
    "FFMPEG_OPTIONS",
    "NETWORK_BEFORE_OPTIONS",
    "LibraryFile",
    "RESOLUTION_TTL_SECONDS",
    "SourceError",
    "Track",
    "build_track",
    "clear_resolution_cache",
    "ffmpeg_before_options",
    "ffmpeg_command",
    "library_summary",
    "pick_library_file",
    "probe_duration",
    "resolve_youtube",
    "resolve_youtube_blocking",
    "scan_library",
]
