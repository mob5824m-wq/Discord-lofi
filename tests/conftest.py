"""Shared fixtures: an isolated data directory per test.

Every path the bot touches is redirected through environment variables and the
:mod:`paths` constants, so a test can never write to a real install's config or
database - and two tests running in the same session cannot see each other's
state.

``LOFI_DB`` and ``LOFI_CONFIG`` are read on every call (not cached at import),
which is what makes this work without reloading modules.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402  (path setup has to happen first)
import store  # noqa: E402


@pytest.fixture
def tmp_state(tmp_path, monkeypatch) -> Path:
    """An empty, writable home for one test."""
    monkeypatch.setenv("LOFI_HOME", str(tmp_path))
    monkeypatch.setenv("LOFI_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LOFI_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("LOFI_TOKEN", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_PORT", raising=False)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(paths, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(paths, "LOG_PATH", str(tmp_path / "test.log"))
    store.init_db()
    return tmp_path


@pytest.fixture
def music_dir(tmp_state, monkeypatch) -> Path:
    """A library folder with a few files in it."""
    directory = tmp_state / "music"
    directory.mkdir()
    (directory / "Artist One - Slow Tape.mp3").write_bytes(b"id3-fake-audio")
    (directory / "kettle.wav").write_bytes(b"RIFFfake")
    (directory / "notes.txt").write_text("not audio")
    nested = directory / "mixes"
    nested.mkdir()
    (nested / "03 - Third Track.flac").write_bytes(b"fLaCfake")
    monkeypatch.setattr(paths, "music_dir", lambda: directory)
    import sources

    monkeypatch.setattr(sources.paths, "music_dir", lambda: directory)
    return directory


@pytest.fixture
def config() -> dict[str, Any]:
    """A config with defaults plus one configured guild."""
    return json.loads(json.dumps(paths.DEFAULT_CONFIG))


@pytest.fixture
def seeded_random() -> Callable[[int], random.Random]:
    """A deterministic RNG factory, so shuffles are reproducible in tests."""

    def make(seed: int) -> random.Random:
        return random.Random(seed)

    return make


class FakePermissions:
    """A stand-in for :class:`discord.Permissions` with settable flags."""

    def __init__(self, **flags: bool) -> None:
        defaults = {
            "administrator": False,
            "manage_guild": False,
            "view_channel": True,
            "connect": True,
            "speak": True,
            "set_voice_channel_status": True,
            "priority_speaker": False,
            "manage_channels": False,
            "send_messages": True,
        }
        defaults.update(flags)
        for key, value in defaults.items():
            setattr(self, key, value)


class FakeRole:
    def __init__(self, role_id: int, name: str, position: int = 1) -> None:
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = False

    def is_default(self) -> bool:
        return False


class FakeVoiceState:
    def __init__(self, channel: Any = None) -> None:
        self.channel = channel


class FakeMember:
    def __init__(
        self,
        member_id: int,
        name: str = "member",
        *,
        bot: bool = False,
        roles: Optional[list] = None,
        permissions: Optional[FakePermissions] = None,
        channel: Any = None,
        guild: Any = None,
    ) -> None:
        self.id = member_id
        self.name = name
        self.bot = bot
        self.display_name = name
        self.roles = roles or []
        self.guild_permissions = permissions or FakePermissions()
        self.voice = FakeVoiceState(channel)
        self.guild = guild
        self.mention = f"<@{member_id}>"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeMember {self.name}>"


class FakeChannel:
    """A voice-or-text channel double that records what was sent or edited."""

    def __init__(
        self,
        channel_id: int,
        name: str = "study-room",
        *,
        kind: str = "voice",
        members: Optional[list] = None,
        category: Any = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.type = kind
        self.members = members if members is not None else []
        self.category = category
        self.sent: list[Any] = []
        self.edits: list[dict] = []
        self._permissions: dict[int, FakePermissions] = {}
        self.connect_calls = 0
        self.connect_kwargs: dict = {}
        #: Set by a test: what ``connect()`` returns (a :class:`FakeVoiceClient`).
        self.connect_impl: Optional[Callable[..., Any]] = None
        self.connect_error: Optional[Exception] = None

    def is_nsfw(self) -> bool:
        """discord.py exposes this as a method on TextChannel."""
        return False

    def permissions_for(self, member: Any) -> FakePermissions:
        return self._permissions.get(getattr(member, "id", 0), FakePermissions())

    def set_permissions_for(self, member_id: int, permissions: FakePermissions) -> None:
        self._permissions[member_id] = permissions

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append(kwargs.get("embed") or (args[0] if args else None))
        return object()

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    async def connect(self, **kwargs: Any) -> Any:
        """Records the kwargs (self_deaf matters) and hands back a fake client."""
        self.connect_calls += 1
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error
        if self.connect_impl is not None:
            return self.connect_impl(**kwargs)
        raise AssertionError("FakeChannel.connect needs connect_impl set by the test")


class FakeGuild:
    def __init__(
        self,
        guild_id: int,
        name: str = "Test Server",
        *,
        channels: Optional[list] = None,
        members: Optional[list] = None,
        roles: Optional[list] = None,
        me: Optional[Any] = None,
    ) -> None:
        self.id = guild_id
        self.name = name
        self.member_count = len(members or []) or 100
        self.icon = None
        self.owner_id = guild_id + 1
        self.channels = channels or []
        self.members = members or []
        self.roles = roles or []
        self.me = me
        self.voice_client = None

    @property
    def voice_channels(self) -> list:
        return [channel for channel in self.channels if channel.type == "voice"]

    @property
    def text_channels(self) -> list:
        return [channel for channel in self.channels if channel.type == "text"]

    @property
    def categories(self) -> list:
        return [channel for channel in self.channels if channel.type == "category"]

    def get_channel(self, channel_id: int) -> Any:
        for channel in self.channels:
            if channel.id == int(channel_id):
                return channel
        return None

    def get_member(self, member_id: int) -> Any:
        for member in self.members:
            if member.id == int(member_id):
                return member
        return None

    def get_role(self, role_id: int) -> Any:
        for role in self.roles:
            if role.id == int(role_id):
                return role
        return None


class FakeVoiceClient:
    """Records transport calls and lets a test fire the ``after`` callback."""

    def __init__(self, channel: Any, guild: Any) -> None:
        self.channel = channel
        self.guild = guild
        self.source: Any = None
        self.after: Optional[Callable] = None
        self.connected = True
        self.paused = False
        self.playing = False
        self.disconnects = 0
        self.moves: list[Any] = []

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing and not self.paused

    def is_paused(self) -> bool:
        return self.paused

    def play(self, source: Any, *, after: Optional[Callable] = None) -> None:
        self.source = source
        self.after = after
        self.playing = True
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def stop(self) -> None:
        self.playing = False
        self.paused = False
        self.source = None

    async def disconnect(self, *, force: bool = False) -> None:
        self.connected = False
        self.playing = False
        self.disconnects += 1

    async def move_to(self, channel: Any) -> None:
        self.moves.append(channel)
        self.channel = channel

    def fire_after(self, error: Optional[Exception] = None) -> None:
        """Simulate the audio thread finishing the current source."""
        callback, self.after = self.after, None
        self.playing = False
        if callback is not None:
            callback(error)
