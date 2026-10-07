"""Where Lofi keeps its files, and where it finds the ones it does not own.

Everything the bot reads or writes goes through this module so the rest of the
code never guesses a location. Three kinds of path live here:

* **the application directory** - the checkout (or frozen bundle) holding
  ``bot.py``, ``dashboard.html`` and the shipped ``config.example.json``
  template. It may be read-only.
* **the data directory** - state the bot owns: the SQLite database, rendered
  generative-audio cache, and the writable copy of ``config.json``. Chosen once
  at import time from ``LOFI_HOME`` or a platform state directory, falling back
  to ``./data`` next to the sources.
* **the music library** - audio files the *user* owns, ``./music`` by default
  (``LOFI_MUSIC`` overrides). Kept out of the data directory on purpose: people
  sync that folder from elsewhere, and a reinstall must not delete their
  tracks.

``ffmpeg_executable()`` is here too, because "which ffmpeg do we shell out to"
is the same question as "which file do we read": discord.py's voice support
needs an ffmpeg binary, and on a desktop machine it is usually installed
somewhere that is not on ``PATH`` (a Homebrew prefix, ``C:\\ffmpeg\\bin``, a
Python package that bundles one).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("lofi.paths")

APP_NAME = "lofi"
APP_TITLE = "Lofi"
#: Audio the local library is allowed to contain. Extensions only - the real
#: check is "can ffmpeg open it", which is what :mod:`sources` does at play
#: time; this list only decides what the scanner shows you.
AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
    ".webm",
    ".mka",
    ".aiff",
    ".aif",
}


def is_frozen() -> bool:
    """True when running from a PyInstaller-style bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Optional[Path]:
    """Directory of the frozen bundle, when there is one."""
    if not is_frozen():
        return None
    candidate = getattr(sys, "_MEIPASS", None)
    if candidate:
        return Path(candidate)
    executable = getattr(sys, "executable", None)
    return Path(executable).resolve().parent if executable else None


def app_dir() -> Path:
    """The directory holding the code (and the shipped config template)."""
    bundled = bundle_dir()
    if bundled is not None:
        return bundled
    return Path(__file__).resolve().parent


def source_dir() -> Path:
    """Where the Python sources live - the checkout when running from source."""
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Optional[Path]:
    """Locate a read-only shipped resource (``dashboard.html`` and friends).

    Returns ``None`` when the file is absent rather than raising: a missing
    dashboard UI is a problem for the dashboard, not for the music, and the
    caller decides how loudly to complain.
    """
    roots: list[Path] = []
    bundled = bundle_dir()
    if bundled is not None:
        roots.append(bundled)
    roots.append(app_dir())
    if not is_frozen():
        roots.append(source_dir())
    if os.name != "nt":
        roots.extend(Path(p) for p in (f"/usr/share/{APP_NAME}", f"/usr/local/share/{APP_NAME}"))
    for root in roots:
        try:
            candidate = root.joinpath(*parts)
            if candidate.is_file():
                return candidate
        except (OSError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# Data directory
# --------------------------------------------------------------------------- #
def _env_path(name: str) -> Optional[Path]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except (OSError, RuntimeError):
        logger.warning("Ignoring unusable %s value: %r", name, raw)
        return None


def _platform_state_dir() -> Optional[Path]:
    """The conventional per-user state directory for this platform."""
    try:
        home: Optional[Path] = Path.home()
    except (RuntimeError, OSError):  # no HOME / passwd entry (odd service env)
        home = None
    if home is None:
        return None
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / APP_TITLE
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_TITLE
    base = os.environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """Where the bot's own state lives (database, cache, writable config).

    ``LOFI_HOME`` wins outright - it is what a systemd unit, a Docker volume or
    a test suite sets. Otherwise the platform state directory is used when it
    can be created, and ``./data`` next to the sources when it cannot (a
    read-only home, a sandbox without one).
    """
    override = _env_path("LOFI_HOME")
    if override is not None:
        return override
    state = _platform_state_dir()
    if state is not None:
        try:
            state.mkdir(parents=True, exist_ok=True)
            probe = tempfile.NamedTemporaryFile(dir=state, delete=True)
            probe.close()
            return state
        except OSError as exc:
            logger.debug("State directory %s is not usable (%s); falling back.", state, exc)
    return source_dir() / "data"


def _tighten_private_dir(path: Path) -> None:
    """Keep a directory holding a bot token private, best effort."""
    if os.name == "nt":
        return
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        wanted = current & ~ (stat.S_IRWXG | stat.S_IRWXO)
        if wanted != current:
            path.chmod(wanted)
    except OSError as exc:  # pragma: no cover - odd filesystems
        logger.debug("Could not tighten permissions on %s: %s", path, exc)


DATA_DIR = data_dir()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _tighten_private_dir(DATA_DIR)
except OSError as _exc:  # pragma: no cover - read-only filesystem
    logger.warning("Could not prepare the data directory %s: %s", DATA_DIR, _exc)

def _db_path() -> str:
    """Where SQLite lives: ``LOFI_DB`` if set, else ``<data dir>/lofi.db``.

    The override exists for two cases: a deployment that keeps the database on
    another volume, and ``bot.py --demo``, which points it at a throwaway file
    so simulated events never land in the real history.
    """
    override = _env_path("LOFI_DB")
    if override is not None:
        return str(override)
    return str(DATA_DIR / "lofi.db")


DB_PATH = _db_path()
LOG_PATH = str(DATA_DIR / "lofi.log")
CACHE_DIR = DATA_DIR / "cache"


def cache_dir() -> Path:
    """Scratch space for rendered audio and downloaded artwork.

    Everything in here is reproducible, so it is safe to delete - which is also
    why the generated filenames carry a content hash (see :mod:`generative`).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def music_dir() -> Optional[Path]:
    """The local music library, or ``None`` when it does not exist.

    ``None`` (rather than an empty directory that is silently created) means
    the UI can say "you have no library folder" instead of "your library is
    empty" - two different problems with two different fixes.
    """
    override = _env_path("LOFI_MUSIC")
    if override is not None:
        return override if override.is_dir() else None
    for candidate in (source_dir() / "music", DATA_DIR / "music"):
        if candidate.is_dir():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict[str, Any] = {
    "bot_token": "",
    "guilds": {},
    "stations": [],
    "default_station": "lofi-girl",
    "default_volume": 60,
    "idle_disconnect_minutes": 10,
    "stay_connected": False,
    "announce_now_playing": True,
    "channel_status": True,
    "sync_commands_on_start": True,
    "dashboard_enabled": True,
    "dashboard_host": "127.0.0.1",
    # Not 8765, which is what Sentinel's dashboard uses by default: two bots on
    # one machine should both come up without anybody editing a config first.
    "dashboard_port": 8790,
    "dashboard_token": "",
    "dashboard_allowed_hosts": [],
    "dashboard_public_url": "",
    "dashboard_secure_cookie": False,
    "dashboard_trusted_proxies": [],
    "dashboard_tls_cert": "",
    "dashboard_tls_key": "",
}


def config_candidates() -> list[Path]:
    """Every place a config is accepted from, in priority order.

    ``config.example.json`` is the committed template: ``config.json`` is
    gitignored because that is the file a bot token ends up in, and a token in
    git history is a token that has to be rotated. The template is still read,
    so a fresh clone works before anyone copies it - but it is never a write
    target (see :func:`config_write_path`).
    """
    candidates: list[Path] = []
    override = _env_path("LOFI_CONFIG")
    if override is not None:
        candidates.append(override)
    candidates.append(DATA_DIR / "config.json")
    candidates.append(source_dir() / "config.json")
    candidates.append(source_dir() / "config.example.json")
    bundled = bundle_dir()
    if bundled is not None:
        candidates.append(bundled / "config.json")
        candidates.append(bundled / "config.example.json")
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def config_path() -> Path:
    """The config file to *read*: the first candidate that exists."""
    for candidate in config_candidates():
        if candidate.is_file():
            return candidate
    return config_candidates()[-1]


def config_write_path() -> Path:
    """The config file to *write*.

    Deliberately not the shipped template when a data directory exists: a
    packaged install keeps its application directory read-only, and the config
    holds a bot token that should not sit in a world-readable program folder.
    Writing to ``config.example.json`` is never correct - it is the committed
    template - so it is not considered here even though it is read from.
    """
    override = _env_path("LOFI_CONFIG")
    if override is not None:
        return override
    return DATA_DIR / "config.json"


def load_config(default: Optional[dict] = None) -> dict[str, Any]:
    """Read config.json, merged over the defaults.

    A malformed file raises rather than being ignored: silently starting with
    default settings is how a bot ends up in the wrong channel with the wrong
    token, and the error message can say exactly which file to fix.
    """
    base = dict(default if default is not None else DEFAULT_CONFIG)
    target = config_path()
    if not target.is_file():
        return base
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read {target}: {exc}") from exc
    if not raw.strip():
        return base
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{target} must contain a JSON object.")
    merged = dict(base)
    merged.update(loaded)
    # Nested mappings get their defaults too, so a config written by an older
    # version still has every key the current code reads.
    for key, value in DEFAULT_CONFIG.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            filled = dict(value)
            filled.update(merged[key])
            merged[key] = filled
    return merged


def _write_json(target: Path, cfg: dict[str, Any]) -> None:
    """Write config.json atomically, keeping the old file if writing fails.

    The token lives in this file. A crash halfway through a plain ``open()``
    would leave a truncated config that no longer parses, and the next start
    would lose the token - so the new content is written to a sibling temp file
    and moved into place.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, target)
        tmp = None  # type: ignore[assignment]
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass


def write_config(cfg: dict[str, Any]) -> Path:
    """Persist config to the first writable candidate."""
    errors: list[str] = []
    for target in config_candidates():
        try:
            _write_json(target, cfg)
            return target
        except OSError as exc:
            errors.append(f"{target}: {exc}")
    raise OSError("Could not write config.json anywhere: " + "; ".join(errors))


# --------------------------------------------------------------------------- #
# ffmpeg / opus
# --------------------------------------------------------------------------- #
_FFMPEG_CACHE: dict[str, Optional[str]] = {}


def _bundled_ffmpeg() -> Optional[str]:
    """An ffmpeg shipped inside a Python package, if one is installed.

    Windows and macOS users routinely have no ffmpeg on ``PATH``. Two packages
    bundle a working binary, and either one turns "install ffmpeg yourself" into
    "pip install imageio-ffmpeg" - so both are probed before we give up.
    """
    for module, attr in (("imageio_ffmpeg", "get_ffmpeg_exe"), ("static_ffmpeg", "run")):
        try:
            imported = __import__(module)
        except Exception:
            continue
        try:
            if module == "imageio_ffmpeg":
                found = imported.get_ffmpeg_exe()
                if found and Path(found).exists():
                    return str(found)
            else:  # static_ffmpeg returns (ffmpeg, ffprobe) after downloading
                found, _probe = imported.run.get_or_fetch_platform_executables_else_raise()
                if found and Path(found).exists():
                    return str(found)
        except Exception as exc:
            logger.debug("%s did not provide an ffmpeg binary: %s", module, exc)
    return None


def _common_ffmpeg_locations() -> list[Path]:
    home = Path.home()
    locations = [
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/snap/bin/ffmpeg"),
        home / "scoop/shims/ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
    ]
    chocolatey = os.environ.get("ProgramData")
    if chocolatey:
        locations.append(Path(chocolatey) / "bin/ffmpeg.exe")
    return locations


def ffmpeg_executable(name: str = "ffmpeg") -> Optional[str]:
    """Resolve the ffmpeg (or ffprobe) binary to run.

    Order: ``LOFI_FFMPEG`` / ``LOFI_FFPROBE`` for the case where a machine has
    several and the user knows which one they want, then ``PATH``, then a
    bundled binary from an installed package, then the usual install prefixes.
    Cached per name, because every track that starts asks.
    """
    if name in _FFMPEG_CACHE:
        return _FFMPEG_CACHE[name]
    env_name = f"LOFI_{name.upper()}"
    override = os.environ.get(env_name, "").strip()
    resolved: Optional[str] = None
    if override:
        candidate = Path(override).expanduser()
        resolved = str(candidate) if candidate.exists() else shutil.which(override)
        if resolved is None:
            logger.warning("%s points at %r, which does not exist; searching anyway.", env_name, override)
    if resolved is None:
        resolved = shutil.which(name)
    if resolved is None and name == "ffmpeg":
        resolved = _bundled_ffmpeg()
    if resolved is None:
        for candidate in _common_ffmpeg_locations():
            if candidate.name == name or candidate.name.startswith(name):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    resolved = str(candidate)
                    break
    _FFMPEG_CACHE[name] = resolved
    return resolved


def opus_library() -> Optional[str]:
    """A libopus discord.py can load, if one is findable.

    discord.py raises at voice-connect time when opus is missing, which reads as
    "the bot cannot join". Reporting it up front - and naming the package to
    install - is the difference between a five-minute setup and a support
    request.
    """
    try:
        import discord.opus  # noqa: WPS433 - import is the probe

        if discord.opus.is_loaded():
            return "loaded"
    except Exception:
        return None
    search = {
        "darwin": ["libopus.0.dylib", "libopus.dylib", "/opt/homebrew/lib/libopus.0.dylib"],
        "win32": ["libopus-0.dll", "opus.dll"],
    }.get(sys.platform, ["libopus.so.0", "libopus.so"])
    for candidate in search:
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return str(path)
        found = shutil.which(candidate)
        if found:
            return found
    return None


def app_version() -> str:
    """The version string shown in the dashboard and by ``--version``."""
    version_file = resource_path("VERSION")
    if version_file is not None:
        try:
            return version_file.read_text(encoding="utf-8").strip() or "0.0.0"
        except OSError:  # pragma: no cover
            pass
    return "0.0.0"


__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "AUDIO_EXTENSIONS",
    "CACHE_DIR",
    "DATA_DIR",
    "DB_PATH",
    "DEFAULT_CONFIG",
    "LOG_PATH",
    "app_dir",
    "app_version",
    "bundle_dir",
    "cache_dir",
    "config_candidates",
    "config_path",
    "config_write_path",
    "ffmpeg_executable",
    "is_frozen",
    "load_config",
    "music_dir",
    "opus_library",
    "resource_path",
    "source_dir",
    "write_config",
]
