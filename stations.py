"""The station registry: everything Lofi knows how to play.

A *station* is a named source of audio, which is the unit people actually think
in ("put on Lofi Girl"), rather than a URL. Each one carries the information
needed to play it and to recover when playing it fails:

* ``url`` - what to hand to ffmpeg, or a YouTube watch/live URL to resolve
  first;
* ``kind`` - ``youtube``, ``stream`` (a plain HTTP(S)/icecast feed ffmpeg can
  open directly), ``library`` (files under ``./music``) or ``generative``
  (rendered locally by :mod:`generative`, no network at all);
* ``search`` - a YouTube search query tried when ``url`` cannot be resolved.
  Live stream ids change when a channel restarts its broadcast, and the usual
  failure is a 404 on a video that used to exist; falling back to
  ``ytsearch1:<query>`` turns that into "the station moved" instead of
  "the bot is broken".

Built-in stations live in :data:`BUILT_IN_STATIONS` and are always available.
Operator-added stations are stored in ``config.json`` under ``stations`` and are
indistinguishable to the player apart from ``builtin=False``, so a server can
replace the defaults entirely with its own.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit


logger = logging.getLogger("lofi.stations")

KIND_YOUTUBE = "youtube"
KIND_STREAM = "stream"
KIND_LIBRARY = "library"
KIND_GENERATIVE = "generative"
KINDS = (KIND_YOUTUBE, KIND_STREAM, KIND_LIBRARY, KIND_GENERATIVE)

MAX_STATION_NAME = 60
MAX_CUSTOM_STATIONS = 50
MAX_STATION_ID = 40
#: Local addresses are refused for remote URLs: a station pointing at
#: ``http://169.254.169.254/`` or ``http://127.0.0.1:6379`` would turn the bot
#: into a blind probe of whatever network it runs on.
PRIVATE_HOST_PATTERN = re.compile(
    r"^(localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?|\[?f[cd][0-9a-f]{2}:)",
    re.IGNORECASE,
)
SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Station:
    """One playable source of lofi audio."""

    id: str
    name: str
    kind: str
    url: str = ""
    description: str = ""
    search: str = ""
    art: str = "🎧"
    builtin: bool = False
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view, camelCase for the dashboard."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "description": self.description,
            "search": self.search,
            "art": self.art,
            "builtin": self.builtin,
            "tags": list(self.tags),
        }

    @property
    def needs_network(self) -> bool:
        return self.kind in {KIND_YOUTUBE, KIND_STREAM}

    @property
    def is_live(self) -> bool:
        """A station with no natural end: it is restarted, not advanced."""
        return self.kind in {KIND_YOUTUBE, KIND_STREAM}


def slugify(value: str, fallback: str = "station") -> str:
    """A station id that is safe in a URL path and stable across restarts."""
    normalised = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = SLUG_PATTERN.sub("-", ascii_only).strip("-")
    return slug[:MAX_STATION_ID] or fallback


def classify_url(url: str) -> Optional[str]:
    """``youtube`` / ``stream`` / ``library`` for a user-supplied URL.

    ``library`` covers plain filesystem paths, which is how a server with no
    internet (or a licensing preference) points the bot at a folder of its own
    tracks. Anything unrecognised returns ``None`` so the caller can say why.
    """
    candidate = str(url or "").strip()
    if not candidate:
        return None
    lowered = candidate.lower()

    if lowered.startswith(("library:", "folder:", "local:")):
        return KIND_LIBRARY
    if lowered in {"generative", "synth", "builtin"}:
        return KIND_GENERATIVE

    if "://" not in candidate and not lowered.startswith(("www.", "youtu.be")):
        # A bare path: /srv/music, ./music, C:\lofi\tracks
        if any(sep in candidate for sep in ("/", "\\")) or lowered.endswith(tuple((".",))):
            return KIND_LIBRARY
        return None

    if any(
        host in lowered
        for host in (
            "youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
            "m.youtube.com",
            "music.youtube.com",
        )
    ):
        return KIND_YOUTUBE

    scheme = urlsplit(candidate).scheme.lower()
    if scheme in {"http", "https"}:
        return KIND_STREAM
    if scheme in {"file"}:
        return KIND_LIBRARY
    # icecast/shoutcast feeds are sometimes written as host:port/mount with no
    # scheme; ffmpeg handles those, but only over http(s), so refuse them here
    # rather than passing something surprising to a subprocess.
    return None


def is_private_url(url: str) -> bool:
    """True for URLs that resolve to this machine or a private network."""
    try:
        host = (urlsplit(str(url)).hostname or "").strip().lower()
    except ValueError:
        return True
    if not host:
        return True
    return bool(PRIVATE_HOST_PATTERN.match(host))


def validate_station(
    *, name: str, url: str, kind: Optional[str] = None, allow_private: bool = False
) -> tuple[str, str, str]:
    """Normalise and check an operator-supplied station.

    Returns ``(station_id, name, kind)``. Raises :class:`ValueError` with a
    message meant to be shown to a human - the same text reaches a Discord
    interaction response and a dashboard error toast, so it says what to
    change, not just that something was wrong.
    """
    clean_name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not clean_name:
        raise ValueError("Give the station a name.")
    if len(clean_name) > MAX_STATION_NAME:
        raise ValueError(f"Station names are limited to {MAX_STATION_NAME} characters.")

    clean_url = str(url or "").strip()
    if not clean_url:
        raise ValueError("Give the station a URL, a folder path, or 'generative'.")

    resolved_kind = kind or classify_url(clean_url)
    if resolved_kind not in KINDS:
        raise ValueError(
            "That does not look like something Lofi can play. Use an http(s) stream, "
            "a YouTube link, a folder path prefixed with 'library:', or 'generative'."
        )
    if resolved_kind in {KIND_YOUTUBE, KIND_STREAM} and not allow_private and is_private_url(clean_url):
        raise ValueError(
            "That address points at this machine or a private network, so it was refused."
        )
    if len(clean_url) > 900:
        raise ValueError("That URL is too long (900 characters max).")

    station_id = slugify(clean_name)
    return station_id, clean_name, resolved_kind


# --------------------------------------------------------------------------- #
# Built-in stations
# --------------------------------------------------------------------------- #
#: Shipped with the bot. YouTube live ids are the long-running 24/7 broadcasts;
#: when one changes, ``search`` gets the bot back on air and the station can be
#: corrected from the dashboard without touching this file.
BUILT_IN_STATIONS: tuple[Station, ...] = (
    Station(
        id="lofi-girl",
        name="Lofi Girl",
        kind=KIND_YOUTUBE,
        url="https://www.youtube.com/watch?v=jfKfPfyJRdk",
        search="lofi hip hop radio beats to relax study to",
        description="beats to relax / study to - the 24/7 lofi hip hop broadcast",
        art="📚",
        builtin=True,
        tags=("lofi", "study", "24/7"),
    ),
    Station(
        id="synthwave-boy",
        name="Synthwave Boy",
        kind=KIND_YOUTUBE,
        url="https://www.youtube.com/watch?v=4xDzrJKXOOY",
        search="synthwave boy radio beats to chill game to",
        description="beats to chill / game to - neon-flavoured synthwave",
        art="🌆",
        builtin=True,
        tags=("synthwave", "gaming", "24/7"),
    ),
    Station(
        id="chillhop",
        name="Chillhop Radio",
        kind=KIND_YOUTUBE,
        url="https://www.youtube.com/watch?v=5yx6BWlEVcY",
        search="chillhop radio jazzy lofi hip hop beats",
        description="jazzy & lofi hip hop beats, streamed around the clock",
        art="☕",
        builtin=True,
        tags=("jazzy", "chill", "24/7"),
    ),
    Station(
        id="sleepy-lofi",
        name="Sleepy Lofi",
        kind=KIND_YOUTUBE,
        url="",
        search="lofi sleep radio relaxing beats to sleep study",
        description="slow, rain-soaked beats for late nights - found by search",
        art="🌙",
        builtin=True,
        tags=("sleep", "rain"),
    ),
    Station(
        id="groove-salad",
        name="Groove Salad",
        kind=KIND_STREAM,
        url="https://ice2.somafm.com/groovesalad-128-mp3",
        description="a nicely chilled plate of ambient / downtempo (SomaFM)",
        art="🥗",
        builtin=True,
        tags=("ambient", "downtempo"),
    ),
    Station(
        id="secret-agent",
        name="Secret Agent",
        kind=KIND_STREAM,
        url="https://ice2.somafm.com/secretagent-128-mp3",
        description="spy-lounge downtempo and trip-hop (SomaFM)",
        art="🕶️",
        builtin=True,
        tags=("downtempo", "trip-hop"),
    ),
    Station(
        id="library",
        name="My Library",
        kind=KIND_LIBRARY,
        url="library:music",
        description="files in the bot's ./music folder, shuffled",
        art="💿",
        builtin=True,
        tags=("local", "offline"),
    ),
    Station(
        id="generative",
        name="Studio Lofi",
        kind=KIND_GENERATIVE,
        url="generative",
        description="beats rendered on this machine - no network, no copyright",
        art="🎹",
        builtin=True,
        tags=("local", "offline", "generated"),
    ),
)

BUILT_IN_BY_ID: dict[str, Station] = {station.id: station for station in BUILT_IN_STATIONS}
DEFAULT_STATION_ID = "lofi-girl"


def station_from_dict(raw: Any) -> Optional[Station]:
    """Build a :class:`Station` from a config entry, or ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url", "") or "").strip()
    kind = str(raw.get("kind", "") or "").strip().lower()
    if kind not in KINDS:
        kind = classify_url(url) or KIND_STREAM
    name = re.sub(r"\s+", " ", str(raw.get("name", "") or "")).strip()
    if not name and url:
        name = url
    if not name:
        return None
    station_id = str(raw.get("id", "") or "").strip() or slugify(name)
    art = str(raw.get("art", "") or "").strip()
    return Station(
        id=station_id[:MAX_STATION_ID],
        name=name[:MAX_STATION_NAME],
        kind=kind,
        url=url,
        description=str(raw.get("description", "") or "").strip(),
        search=str(raw.get("search", "") or "").strip(),
        art=art[:8] or "🎧",
        builtin=False,
        tags=tuple(str(tag) for tag in (raw.get("tags") or []) if str(tag).strip())[:5],
    )


def get_stations(config: Optional[dict] = None) -> list[Station]:
    """Built-ins plus every custom station, in a stable display order.

    A custom station may reuse a built-in's id, which replaces it - that is how
    an operator fixes a stale YouTube link from the dashboard without editing
    code.
    """
    custom: dict[str, Station] = {}
    if isinstance(config, dict):
        raw_list = config.get("stations")
        if isinstance(raw_list, dict):  # tolerate {"id": {...}} shapes
            raw_list = [dict(value, id=key) for key, value in raw_list.items()]
        if isinstance(raw_list, Iterable):
            for entry in raw_list:
                station = station_from_dict(entry)
                if station is not None:
                    custom[station.id] = station
    merged: dict[str, Station] = {}
    for station in BUILT_IN_STATIONS:
        merged[station.id] = custom.pop(station.id, None) or station
    merged.update(custom)
    return list(merged.values())


def studio_station(reference: str) -> Optional[Station]:
    """A generated-mood station for ``studio-<recipe>``, or ``None``.

    ``/lofi studio mood:cafe`` and the dashboard's mood picker both refer to a
    recipe that is *not* in the registry: five moods would be five more entries
    in every list for something that is one station with a parameter. Synthesising
    it here means the player, the status line and the history all see a normal
    :class:`Station` with a distinct id, so "Studio · Cafe Window Seat" reads
    correctly everywhere without being saved anywhere.
    """
    text = str(reference or "").strip()
    prefix = "studio-"
    if not text.lower().startswith(prefix):
        return None
    try:
        import generative  # local import: numpy is optional
    except Exception:  # pragma: no cover - numpy missing entirely
        return None
    # Slice by length, not by splitting on a lowercase literal: the reference
    # itself may be "STUDIO-Cafe" and split() would then find nothing.
    recipe = generative.recipe_for(text[len(prefix):])
    return Station(
        id=f"studio-{recipe.id}",
        name=f"Studio · {recipe.name}",
        kind=KIND_GENERATIVE,
        url=f"generative:{recipe.id}",
        description=recipe.description,
        art="🎹",
        tags=("generated", recipe.id),
    )


def find_station(config: Optional[dict], station_ref: Any) -> Optional[Station]:
    """Look a station up by id, name, studio mood, or (for a URL) by classifying it.

    Returning a synthesised station for an unrecognised-but-valid URL is what
    makes ``/lofi play https://...`` work without registering anything first.
    """
    reference = str(station_ref or "").strip()
    if not reference:
        return None
    studio = studio_station(reference)
    if studio is not None:
        return studio
    stations = get_stations(config)
    lowered = reference.lower()
    # "/lofi stations" prints "🥗 Groove Salad", so people paste the emoji back
    # in. Strip any leading decoration before comparing against the plain name.
    decoration = "".join(ch for ch in lowered if not (ch.isalnum() or ch.isspace()))
    bare = lowered.lstrip(decoration).strip()
    for station in stations:
        if station.id == lowered:
            return station
    for station in stations:
        if station.name.lower() in (lowered, bare):
            return station
    if classify_url(reference) is not None:
        kind = classify_url(reference)
        return Station(
            id=slugify(reference, fallback="custom"),
            name=_short_name_for_url(reference),
            kind=kind or KIND_STREAM,
            url=reference,
            description="played from a link",
            art="🔗",
        )
    for station in stations:
        if lowered in station.name.lower():
            return station
    return None


def default_station(config: Optional[dict]) -> Station:
    """The station to fall back on, guaranteed to exist."""
    configured = str((config or {}).get("default_station") or "").strip()
    if configured:
        found = find_station(config, configured)
        if found is not None:
            return found
    return BUILT_IN_BY_ID[DEFAULT_STATION_ID]


def _short_name_for_url(url: str) -> str:
    """A readable label for a bare link: the host, or the video id."""
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - classify_url already rejected these
        return "Custom link"
    host = (parts.hostname or "").replace("www.", "")
    if "youtube" in host or "youtu.be" in host:
        video_id = parts.query.split("v=")[-1].split("&")[0] if "v=" in parts.query else ""
        if not video_id and parts.path.startswith("/"):
            video_id = parts.path.strip("/").split("/")[-1]
        if video_id and len(video_id) <= 20:
            return f"YouTube · {video_id}"
    return host or "Custom link"


def save_custom_station(config: dict, station: Station) -> dict:
    """Return a new config with ``station`` added or replaced."""
    updated = dict(config)
    entries = list(updated.get("stations") or [])
    entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("id") != station.id]
    entries.append(
        {
            "id": station.id,
            "name": station.name,
            "kind": station.kind,
            "url": station.url,
            "description": station.description,
            "search": station.search,
            "art": station.art,
            "tags": list(station.tags),
        }
    )
    if len(entries) > MAX_CUSTOM_STATIONS:
        raise ValueError(
            f"A maximum of {MAX_CUSTOM_STATIONS} custom stations can be saved; remove one first."
        )
    updated["stations"] = entries
    return updated


def remove_custom_station(config: dict, station_id: str) -> tuple[dict, bool]:
    """Return ``(new_config, removed)``.

    Removing a *built-in* id drops the override that was hiding it, which is how
    an operator restores a station they had replaced.
    """
    updated = dict(config)
    entries = list(updated.get("stations") or [])
    kept = [
        entry
        for entry in entries
        if not (isinstance(entry, dict) and str(entry.get("id", "")) == str(station_id))
    ]
    removed = len(kept) != len(entries)
    updated["stations"] = kept
    return updated, removed


__all__ = [
    "BUILT_IN_BY_ID",
    "BUILT_IN_STATIONS",
    "DEFAULT_STATION_ID",
    "KINDS",
    "KIND_GENERATIVE",
    "KIND_LIBRARY",
    "KIND_STREAM",
    "KIND_YOUTUBE",
    "MAX_CUSTOM_STATIONS",
    "MAX_STATION_NAME",
    "Station",
    "classify_url",
    "default_station",
    "find_station",
    "get_stations",
    "is_private_url",
    "remove_custom_station",
    "save_custom_station",
    "slugify",
    "station_from_dict",
    "studio_station",
    "validate_station",
]
