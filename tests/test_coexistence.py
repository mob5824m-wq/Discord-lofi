"""Two bots, one machine.

Lofi's dashboard is deliberately close to Sentinel's — same architecture, same
config vocabulary, same single-file UI — and running a moderation bot and a
music bot on the same box is a normal thing to want. This file pins every
namespace the two would otherwise share, so the second bot to start does not
fail, and so nothing crosses between them.

The values compared against were read out of the Sentinel repository:

===========================  ==========================================
Sentinel                     Lofi
===========================  ==========================================
``dashboard_port`` 8765      ``dashboard.DEFAULT_PORT`` 8790
``sentinel_dashboard_session`` ``lofi_dashboard_session``
``~/.local/state/sentinel``  ``~/.local/state/lofi``
``punishments.db``           ``lofi.db``
``bot.log``                  ``lofi.log``
``SENTINEL_*`` env           ``LOFI_*`` env
``/manage`` commands         ``/lofi`` commands
===========================  ==========================================

Every test names the collision it prevents.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import command_tree
import dashboard
import demo
import paths
import store

REPO = Path(__file__).resolve().parent.parent
SOURCES = sorted(REPO.glob("*.py"))

#: What the sibling bot binds and names things. Changing these here should be a
#: deliberate act, not a side effect of a refactor.
SENTINEL_PORT = 8765
SENTINEL_COOKIE = "sentinel_dashboard_session"
SENTINEL_APP_NAME = "sentinel"
SENTINEL_DB = "punishments.db"
SENTINEL_LOG = "bot.log"
SENTINEL_ENV_PREFIX = "SENTINEL_"
SENTINEL_COMMAND_GROUP = "manage"

KEY_A = "coexistence-key-one-long-enough-to-be-accepted"
KEY_B = "coexistence-key-two-long-enough-to-be-accepted"


# --------------------------------------------------------------------------- #
# The one thing that actually collided: the port
# --------------------------------------------------------------------------- #
def test_the_default_port_is_not_the_one_the_sibling_bot_binds():
    """Two dashboards on 8765 means the second one cannot start at all.

    The failure is also easy to misread: the bot stays up and plays music, and
    only a log line says the dashboard could not bind.
    """
    assert dashboard.DEFAULT_PORT == 8790
    assert dashboard.DEFAULT_PORT != SENTINEL_PORT


def test_every_shipped_default_agrees_on_the_port():
    """The code fallback, the config template and the UI must not drift apart.

    A template that says one port while the code falls back to another produces
    a dashboard nobody can find at the address they were told to open.
    """
    assert paths.DEFAULT_CONFIG["dashboard_port"] == dashboard.DEFAULT_PORT

    template = json.loads((REPO / "config.example.json").read_text(encoding="utf-8"))
    assert template["dashboard_port"] == dashboard.DEFAULT_PORT

    ui = (REPO / "dashboard.html").read_text(encoding="utf-8")
    assert f'value="{dashboard.DEFAULT_PORT}"' in ui
    assert f"data.port || {dashboard.DEFAULT_PORT}" in ui


def test_a_server_with_no_configuration_resolves_to_the_default_port(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_PORT", raising=False)
    server = dashboard.DashboardServer(demo.DemoProvider(dict(paths.DEFAULT_CONFIG), token=KEY_A))
    server._apply_settings(server.config)
    assert server._port == dashboard.DEFAULT_PORT


def test_the_environment_override_still_wins(tmp_state, monkeypatch):
    """Portability matters more than the default: a third bot on the box, or a
    port that is already taken by something else entirely."""
    monkeypatch.setenv("LOFI_DASHBOARD_PORT", "9311")
    server = dashboard.DashboardServer(demo.DemoProvider(dict(paths.DEFAULT_CONFIG), token=KEY_A))
    server._apply_settings(server.config)
    assert server._port == 9311


async def test_two_dashboards_run_side_by_side_in_one_process(tmp_state):
    """No module-level state that would make a second instance misbehave."""
    clients = []
    try:
        for key in (KEY_A, KEY_B):
            config = dict(paths.DEFAULT_CONFIG)
            config["dashboard_token"] = key
            provider = demo.DemoProvider(config, token=key)
            server = dashboard.DashboardServer(provider, save_config=provider.save_config)
            client = TestClient(TestServer(server._build_app()))
            await client.start_server()
            clients.append((client, key))

        for client, key in clients:
            assert (await client.get("/healthz")).status == 200
            login = await client.post("/api/login", json={"token": key})
            assert login.status == 200
            overview = await client.get("/api/overview")
            assert overview.status == 200
            assert (await overview.json())["bot"]
    finally:
        for client, _ in clients:
            await client.close()


# --------------------------------------------------------------------------- #
# Cookies: ports do not separate them, names do
# --------------------------------------------------------------------------- #
def test_the_session_cookie_is_namespaced():
    """Browsers scope cookies by host and *ignore the port*, so two dashboards on
    127.0.0.1 share one cookie jar. Distinct names are the only thing that keeps
    one bot's session from being offered to the other.
    """
    assert dashboard.SESSION_COOKIE == "lofi_dashboard_session"
    assert dashboard.SESSION_COOKIE != SENTINEL_COOKIE
    assert dashboard.SESSION_COOKIE.startswith(f"{paths.APP_NAME}_")


async def test_a_session_from_one_dashboard_does_not_authenticate_another(tmp_state):
    """Even with the cookie delivered by hand, the other bot must refuse it."""
    config_a = dict(paths.DEFAULT_CONFIG, dashboard_token=KEY_A)
    config_b = dict(paths.DEFAULT_CONFIG, dashboard_token=KEY_B)
    provider_a = demo.DemoProvider(config_a, token=KEY_A)
    provider_b = demo.DemoProvider(config_b, token=KEY_B)
    client_a = TestClient(TestServer(dashboard.DashboardServer(provider_a)._build_app()))
    client_b = TestClient(TestServer(dashboard.DashboardServer(provider_b)._build_app()))
    await client_a.start_server()
    await client_b.start_server()
    try:
        assert (await client_a.post("/api/login", json={"token": KEY_A})).status == 200
        stolen = next(
            cookie.value
            for cookie in client_a.session.cookie_jar
            if cookie.key == dashboard.SESSION_COOKIE
        )
        assert stolen

        response = await client_b.get(
            "/api/overview", headers={"Cookie": f"{dashboard.SESSION_COOKIE}={stolen}"}
        )
        assert response.status == 401
        assert (await response.json())["error"] == "Authentication required."
    finally:
        await client_a.close()
        await client_b.close()


# --------------------------------------------------------------------------- #
# Files on disk
# --------------------------------------------------------------------------- #
def test_the_application_name_is_its_own():
    """The app name is what the platform state directory is built from, so this
    is the difference between ``~/.local/state/lofi`` and a shared folder."""
    assert paths.APP_NAME == "lofi"
    assert paths.APP_TITLE == "Lofi"
    assert paths.APP_NAME != SENTINEL_APP_NAME


def test_the_database_filename_is_derived_from_the_data_directory(tmp_state, monkeypatch):
    """Derived from the data directory, so a sibling bot writing ``punishments.db``
    into its own state dir can never meet this one."""
    monkeypatch.delenv("LOFI_DB", raising=False)
    derived = Path(paths._db_path())
    assert derived == tmp_state / "lofi.db"
    assert derived.name != SENTINEL_DB


def test_the_filenames_a_fresh_install_actually_gets(tmp_path):
    """Checked in a subprocess: ``LOG_PATH`` and the default database name are
    worked out at import time, and the fixtures in this suite redirect both.

    These are the two files a person sees next to a sibling bot's ``bot.log``,
    so the names are worth pinning exactly.
    """
    env = dict(os.environ)
    env["LOFI_HOME"] = str(tmp_path)
    env.pop("LOFI_DB", None)
    env.pop("LOFI_CONFIG", None)
    result = subprocess.run(
        [sys.executable, "-c", "import paths; print(paths.LOG_PATH); print(paths._db_path())"],
        cwd=REPO, env=env, capture_output=True, text=True, check=True,
    )
    log_path, db_path = (Path(line) for line in result.stdout.splitlines()[:2])
    assert log_path.name == "lofi.log"
    assert log_path.name != SENTINEL_LOG
    assert db_path.name == "lofi.db"
    assert db_path == tmp_path / "lofi.db"
    assert log_path.parent == tmp_path


def test_the_database_path_is_reread_rather_than_captured(tmp_state, monkeypatch):
    """``LOFI_DB`` wins when it is set, and is noticed when it stops being set -
    which is what lets one process serve a demo database and a real one."""
    monkeypatch.setenv("LOFI_DB", str(tmp_state / "elsewhere.db"))
    assert Path(paths._db_path()).name == "elsewhere.db"
    monkeypatch.delenv("LOFI_DB")
    assert Path(paths._db_path()).name == "lofi.db"


def test_the_demo_database_is_not_the_real_one():
    """``--demo`` invents servers; its history must not land in the file the
    real bot's stats are read from."""
    source = (REPO / "bot.py").read_text(encoding="utf-8")
    assert 'demo.db' in source
    assert "store.init_db()" in source


def test_the_data_directory_override_is_its_own_variable():
    source = (REPO / "paths.py").read_text(encoding="utf-8")
    assert '"LOFI_HOME"' in source
    assert "SENTINEL_HOME" not in source


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
#: Variables that belong to the platform rather than to either bot.
PLATFORM_ENV = {"XDG_STATE_HOME", "XDG_DATA_HOME", "APPDATA", "PROGRAMDATA", "HOME", "PATH"}

ENV_USE = re.compile(
    r"(?:environ\.get\(|environ\[|getenv\(|_env_path\()\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
)


def test_every_environment_variable_it_reads_is_prefixed():
    """An unprefixed variable is one a sibling bot may also read — and one that
    a shell profile written for the other bot will silently set.
    """
    found: set[str] = set()
    for source in SOURCES:
        found.update(ENV_USE.findall(source.read_text(encoding="utf-8")))

    ours = found - PLATFORM_ENV
    assert ours, "the scan found nothing, so the regex has drifted from the code"
    assert all(name.startswith("LOFI_") for name in ours), sorted(ours)


def test_no_sibling_bot_identifier_is_baked_into_the_code():
    for source in SOURCES:
        text = source.read_text(encoding="utf-8")
        assert SENTINEL_ENV_PREFIX not in text, source.name
        assert SENTINEL_COOKIE not in text, source.name


# --------------------------------------------------------------------------- #
# Discord-side namespaces
# --------------------------------------------------------------------------- #
def test_the_command_group_does_not_collide():
    """Two bots registering ``/manage`` in one server is a confusing namespace;
    every command here hangs off ``/lofi``.
    """
    described = command_tree.describe()
    assert len(described) == 30
    qualified = {item["qualifiedName"] for item in described}
    assert all(name.split(" ")[0] == "lofi" for name in qualified), sorted(qualified)
    assert not any(name.startswith(SENTINEL_COMMAND_GROUP) for name in qualified)


def test_the_command_tree_builds_without_a_token():
    """So ``--check``, the docs and this test can all read it on a machine where
    the other bot holds the only Discord connection."""
    assert command_tree.describe(bot=command_tree._NullBot())


# --------------------------------------------------------------------------- #
# Telling the two apart in a browser
# --------------------------------------------------------------------------- #
def test_the_page_identifies_the_bot():
    """Two tabs, both on 127.0.0.1, both dark: the title and the icon are what
    you actually look at."""
    html = (REPO / "dashboard.html").read_text(encoding="utf-8")
    assert "<title>Lofi" in html
    assert 'rel="icon"' in html


def test_the_icon_is_inline_and_allowed_by_the_csp():
    """No asset pipeline and no external origin, so the icon has to be a data:
    URI — which only works while ``img-src`` keeps allowing ``data:``."""
    html = (REPO / "dashboard.html").read_text(encoding="utf-8")
    assert 'href="data:image/svg+xml,' in html

    response = web.Response(text="x")
    dashboard.DashboardServer._security_headers(response)
    policy = response.headers["Content-Security-Policy"]
    assert "img-src 'self' data:" in policy


def test_the_demo_is_labelled_as_the_demo():
    """With both bots running, a simulated server must not be mistaken for the
    other bot's real one."""
    provider = demo.DemoProvider(dict(paths.DEFAULT_CONFIG), token=KEY_A)
    assert provider.demo is True
