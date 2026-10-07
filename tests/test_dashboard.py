"""The dashboard's HTTP surface: authentication, CSRF, host rules and the API.

Driven through a real aiohttp application against the demo provider, so the
middleware runs exactly as it does in production. The security properties are
the point of this file: a single token guards a page that can stop music in
someone's server and rewrite their configuration.
"""

from __future__ import annotations

import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dashboard
import demo
import paths
from dashboard import DashboardError, DashboardServer, ensure_dashboard_token


KEY = "test-dashboard-key-that-is-long-enough-to-be-accepted"
GUILD_ID = "111111111111111111"


def make_config(**overrides) -> dict:
    config = dict(paths.DEFAULT_CONFIG)
    config["dashboard_token"] = KEY
    config.update(overrides)
    return config


@pytest.fixture
async def client(tmp_state):
    """A running dashboard with the demo provider, and a logged-out client."""
    config = make_config()
    provider = demo.DemoProvider(config, token=KEY)
    server = DashboardServer(provider, save_config=provider.save_config)
    test_client = TestClient(TestServer(server._build_app()))
    await test_client.start_server()
    try:
        yield test_client, server, provider, config
    finally:
        await test_client.close()


async def login(client, key: str = KEY):
    """Log in and return the CSRF token (the browser keeps the cookie itself)."""
    response = await client.post("/api/login", json={"token": key})
    payload = await response.json()
    return response, payload


async def authed(client):
    _, payload = await login(client)
    return payload["csrfToken"]


# --------------------------------------------------------------------------- #
# Liveness and the UI
# --------------------------------------------------------------------------- #
async def test_healthz_answers_without_a_session(client):
    """An uptime check must not need the credential it is monitoring."""
    test_client = client[0]
    response = await test_client.get("/healthz")
    assert response.status == 200
    assert await response.json() == {"ok": True}


async def test_healthz_reveals_nothing_about_the_installation(client):
    test_client = client[0]
    body = await (await test_client.get("/healthz")).text()
    for secret in (KEY, "guild", "version", "token"):
        assert secret not in body.lower() or secret == "token"


async def test_the_ui_is_served_at_the_root(client):
    test_client = client[0]
    response = await test_client.get("/")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/html")
    body = await response.text()
    assert "<title>Lofi" in body
    assert KEY not in body  # the credential is never rendered into the page


async def test_the_ui_is_not_cached(client):
    test_client = client[0]
    response = await test_client.get("/")
    assert response.headers["Cache-Control"] == "no-store"


async def test_the_ui_ships_no_external_assets(client):
    """A single self-contained file: no CDN to break, no third party to leak to."""
    test_client = client[0]
    body = await (await test_client.get("/")).text()
    assert "http://cdn" not in body and "https://cdn" not in body
    assert "<link rel=\"stylesheet\" href=\"http" not in body


async def test_an_unknown_path_is_a_404(client):
    test_client = client[0]
    response = await test_client.get("/nope")
    assert response.status == 404


async def test_an_unknown_api_path_is_json(client):
    test_client = client[0]
    await authed(test_client)
    response = await test_client.get("/api/nope")
    assert response.status == 404
    assert (await response.json())["error"]


async def test_an_unknown_api_path_is_refused_before_it_is_revealed(client):
    """401 rather than 404 for an unauthenticated caller: the route list is not
    public information."""
    test_client = client[0]
    assert (await test_client.get("/api/nope")).status == 401


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
async def test_the_api_refuses_an_unauthenticated_call(client):
    test_client = client[0]
    response = await test_client.get("/api/overview")
    assert response.status == 401
    assert "Authentication required" in (await response.json())["error"]


@pytest.mark.parametrize("path", [
    "/api/overview", "/api/commands", "/api/stations", "/api/library", "/api/generative",
    "/api/history", "/api/settings", f"/api/guilds/{GUILD_ID}",
    f"/api/guilds/{GUILD_ID}/player", f"/api/guilds/{GUILD_ID}/history", "/api/session",
])
async def test_every_api_route_needs_a_session(client, path):
    test_client = client[0]
    assert (await test_client.get(path)).status == 401


async def test_a_wrong_key_is_refused(client):
    test_client = client[0]
    response, payload = await login(test_client, "the-wrong-key-but-long-enough-to-be-polite")
    assert response.status == 401
    assert payload["error"] == "Invalid dashboard key."


async def test_an_empty_key_is_refused(client):
    test_client = client[0]
    response, _ = await login(test_client, "")
    assert response.status == 401


async def test_a_non_string_key_is_refused(client):
    test_client = client[0]
    response = await test_client.post("/api/login", json={"token": 12345})
    assert response.status == 401


async def test_a_malformed_login_body_is_refused_not_crashed(client):
    test_client = client[0]
    response = await test_client.post("/api/login", data="not json at all",
                                      headers={"Content-Type": "application/json"})
    assert response.status == 401


async def test_the_right_key_authenticates(client):
    test_client = client[0]
    response, payload = await login(test_client)
    assert response.status == 200
    assert payload["authenticated"] is True
    assert payload["csrfToken"]
    assert payload["demo"] is True
    assert KEY not in json.dumps(payload)


async def test_the_session_cookie_is_not_readable_by_scripts(client):
    test_client = client[0]
    response, _ = await login(test_client)
    cookie = response.headers["Set-Cookie"]
    assert dashboard.SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie


async def test_the_session_cookie_is_not_marked_secure_over_plain_http(client):
    """A secure cookie over HTTP would make every login silently bounce."""
    test_client = client[0]
    response, _ = await login(test_client)
    assert "Secure" not in response.headers["Set-Cookie"]


async def test_a_session_survives_the_next_request(client):
    test_client = client[0]
    await authed(test_client)
    assert (await test_client.get("/api/overview")).status == 200


async def test_session_status_reports_the_csrf_token(client):
    test_client = client[0]
    token = await authed(test_client)
    payload = await (await test_client.get("/api/session")).json()
    assert payload["authenticated"] is True
    assert payload["csrfToken"] == token


async def test_logout_ends_the_session(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post("/api/logout", json={}, headers={"X-CSRF-Token": token})
    assert response.status == 200
    assert (await response.json())["authenticated"] is False
    assert (await test_client.get("/api/overview")).status == 401


async def test_logout_needs_the_csrf_token_like_any_other_mutation(client):
    test_client = client[0]
    await authed(test_client)
    assert (await test_client.post("/api/logout", json={})).status == 403


async def test_a_forged_cookie_is_refused(client):
    test_client = client[0]
    response = await test_client.get("/api/overview", cookies={dashboard.SESSION_COOKIE: "forged-session"})
    assert response.status == 401


async def test_an_expired_session_is_refused(client):
    test_client, server, _, _ = client
    token = await authed(test_client)
    session_id = test_client.session.cookie_jar.filter_cookies(test_client.make_url("/"))[dashboard.SESSION_COOKIE].value
    server._sessions[session_id]["expires_at"] = time.time() - 1
    assert (await test_client.get("/api/overview")).status == 401
    assert token


async def test_sessions_do_not_last_forever(client):
    _, server, _, _ = client
    assert dashboard.SESSION_TTL_SECONDS == 8 * 3600


async def test_failed_logins_are_throttled_per_address(client):
    test_client, server, _, _ = client
    for _ in range(dashboard.MAX_LOGIN_FAILURES):
        assert (await login(test_client, "wrong-key-but-long-enough-to-be-polite"))[0].status == 401
    response, payload = await login(test_client, KEY)
    assert response.status == 429
    assert "Too many failed attempts" in payload["error"]


async def test_a_successful_login_clears_the_failure_count(client):
    test_client, server, _, _ = client
    await login(test_client, "wrong-key-but-long-enough-to-be-polite")
    assert server._login_failures
    await login(test_client)
    assert server._login_failures == {}


async def test_the_failure_window_resets(client):
    test_client, server, _, _ = client
    peer = "127.0.0.1"
    server._login_failures[peer] = (dashboard.MAX_LOGIN_FAILURES, time.monotonic() - 1)
    response, _ = await login(test_client)
    assert response.status == 200


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method,path,payload", [
    ("post", f"/api/guilds/{GUILD_ID}/player", {"action": "pause"}),
    ("post", f"/api/guilds/{GUILD_ID}/settings", {"volume": 40}),
    ("post", "/api/stations", {"name": "x", "url": "https://example.com/a.mp3"}),
    ("delete", "/api/stations/lofi-girl", {}),
    ("post", "/api/settings", {"port": 8790}),
    ("post", "/api/sync", {}),
])
async def test_a_mutation_without_a_csrf_token_is_refused(client, method, path, payload):
    test_client = client[0]
    await authed(test_client)
    response = await getattr(test_client, method)(path, json=payload)
    assert response.status == 403
    assert "CSRF" in (await response.json())["error"]


async def test_a_mutation_with_the_wrong_csrf_token_is_refused(client):
    test_client = client[0]
    await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/player", json={"action": "pause"},
        headers={"X-CSRF-Token": "not-the-token"},
    )
    assert response.status == 403


async def test_a_mutation_with_the_right_csrf_token_is_accepted(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/player", json={"action": "pause"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status == 200


async def test_a_get_does_not_need_a_csrf_token(client):
    test_client = client[0]
    await authed(test_client)
    assert (await test_client.get("/api/overview")).status == 200


async def test_login_itself_does_not_need_a_csrf_token(client):
    """It has no session yet; the throttle is what protects it instead."""
    test_client = client[0]
    assert (await login(test_client))[0].status == 200


# --------------------------------------------------------------------------- #
# Host allowlist (DNS rebinding)
# --------------------------------------------------------------------------- #
async def test_a_loopback_host_is_always_allowed(client):
    test_client = client[0]
    for host in ("127.0.0.1", "localhost", "[::1]"):
        response = await test_client.get("/api/overview", headers={"Host": host})
        assert response.status in (200, 401), host


async def test_an_unknown_host_is_refused(client):
    """Without this, a page in the operator's browser can read the API by
    pointing a DNS name at 127.0.0.1 - the session cookie would be sent along."""
    test_client, server, _, _ = client
    await authed(test_client)
    server._allow_any_host = False
    allow_hosts(server, [])
    response = await test_client.get("/api/overview", headers={"Host": "evil.example.com"})
    assert response.status == 400
    assert "Host" in (await response.json())["error"]


def allow_hosts(server, hosts):
    """Set the allowlist the way the settings endpoint does: in the live config.

    The middleware reads ``server.config`` per request, so a change made through
    the API takes effect without a restart - which is what these tests assert.
    """
    server.config["dashboard_allowed_hosts"] = list(hosts)


async def test_a_listed_host_is_allowed(client):
    test_client, server, _, _ = client
    await authed(test_client)
    allow_hosts(server, ["lofi.example.com"])
    response = await test_client.get("/api/overview", headers={"Host": "lofi.example.com"})
    assert response.status == 200


async def test_a_listed_host_matches_with_a_port(client):
    test_client, server, _, _ = client
    await authed(test_client)
    allow_hosts(server, ["lofi.example.com"])
    # a port that is deliberately not the one being served: the allowlist
    # matches on host, never on port
    response = await test_client.get("/api/overview", headers={"Host": "lofi.example.com:9999"})
    assert response.status == 200


async def test_a_host_that_is_not_listed_is_refused(client):
    test_client, server, _, _ = client
    await authed(test_client)
    allow_hosts(server, ["lofi.example.com"])
    response = await test_client.get("/api/overview", headers={"Host": "other.example.com"})
    assert response.status == 400


async def test_the_public_url_s_hostname_is_allowed_without_listing_it(client):
    test_client, server, _, _ = client
    await authed(test_client)
    # What the settings endpoint does: write the config, then re-read it.
    server.config["dashboard_public_url"] = "https://lofi.duckdns.org/"
    server.config["dashboard_allowed_hosts"] = []
    server._apply_settings(server.config)
    response = await test_client.get("/api/overview", headers={"Host": "lofi.duckdns.org"})
    assert response.status == 200


async def test_a_wildcard_allows_any_host_but_says_so(client):
    test_client, server, _, _ = client
    await authed(test_client)
    server._apply_settings(make_config(dashboard_allowed_hosts=["*"]))
    response = await test_client.get("/api/overview", headers={"Host": "anything.example"})
    assert response.status == 200
    assert any('"*"' in warning for warning in server.remote_access_warnings())
    assert server._allow_any_host is True


async def test_the_wildcard_is_not_stored_in_the_config(client):
    """A "*" that survives into config.json would re-enable itself on restart."""
    _, server, _, config = client
    server._apply_settings(make_config(dashboard_allowed_hosts=["*"]))
    assert config["dashboard_allowed_hosts"] == []


# --------------------------------------------------------------------------- #
# Security headers and body limits
# --------------------------------------------------------------------------- #
async def test_every_response_carries_the_security_headers(client):
    test_client = client[0]
    for path in ("/", "/healthz", "/api/overview"):
        response = await test_client.get(path)
        assert response.headers["X-Content-Type-Options"] == "nosniff", path
        assert response.headers["Referrer-Policy"] == "no-referrer", path
        assert "microphone=()" in response.headers["Permissions-Policy"], path
        assert response.headers["Content-Security-Policy"].startswith("default-src 'self'"), path


async def test_the_csp_allows_only_discord_s_cdn_for_images(client):
    test_client = client[0]
    policy = (await test_client.get("/")).headers["Content-Security-Policy"]
    assert "img-src 'self' data: https://cdn.discordapp.com" in policy
    assert "base-uri 'none'" in policy


async def test_an_oversized_body_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/settings",
        json={"announce": "x" * (dashboard.MAX_BODY_BYTES + 10)},
        headers={"X-CSRF-Token": token},
    )
    assert response.status == 413


async def test_dashboard_settings_can_be_saved(client):
    test_client, server, provider, _ = client
    token = await authed(test_client)
    response = await test_client.post(
        "/api/settings",
        json={"publicUrl": "https://lofi.example.com/", "allowedHosts": ["lofi.example.com"], "secureCookie": False},
        headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    assert provider.config["dashboard_public_url"] == "https://lofi.example.com/"
    assert provider.config["dashboard_allowed_hosts"] == ["lofi.example.com"]


async def test_saving_nothing_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post("/api/settings", json={"nonsense": 1}, headers={"X-CSRF-Token": token})
    assert response.status == 400
    assert "Nothing to save" in (await response.json())["error"]


async def test_a_bad_port_is_refused_by_the_settings_endpoint(client):
    test_client = client[0]
    token = await authed(test_client)
    for port in (0, 70000, "abc"):
        response = await test_client.post("/api/settings", json={"port": port}, headers={"X-CSRF-Token": token})
        assert response.status == 400, port
        assert "port" in (await response.json())["error"].lower()


async def test_regenerating_the_key_ends_every_session(client):
    test_client, server, provider, _ = client
    token = await authed(test_client)
    response = await test_client.post(
        "/api/settings", json={"regenerateToken": True}, headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    assert provider.config["dashboard_token"] != KEY
    assert (await test_client.get("/api/overview")).status == 401


async def test_a_provider_error_becomes_json_not_a_traceback(client, monkeypatch):
    test_client, server, provider, _ = client
    token = await authed(test_client)

    async def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(provider, "overview", explode)
    response = await test_client.get("/api/overview")
    assert response.status == 500
    payload = await response.json()
    assert "boom" not in payload["error"]  # internals stay in the log


async def test_a_dashboard_error_keeps_its_status_and_message(client, monkeypatch):
    test_client, _, provider, _ = client
    await authed(test_client)

    async def refuse(guild_id):
        raise DashboardError("That server is not connected.", status=404)

    monkeypatch.setattr(provider, "guild_detail", refuse)
    response = await test_client.get(f"/api/guilds/{GUILD_ID}")
    assert response.status == 404
    assert (await response.json())["error"] == "That server is not connected."


# --------------------------------------------------------------------------- #
# Path parameters
# --------------------------------------------------------------------------- #
async def test_an_unknown_guild_is_a_404(client):
    test_client = client[0]
    await authed(test_client)
    response = await test_client.get("/api/guilds/999999999999999999")
    assert response.status == 404


async def test_a_non_numeric_guild_id_is_a_404_not_a_500(client):
    test_client = client[0]
    await authed(test_client)
    for value in ("abc", "..", "%2e%2e", "1;drop table"):
        response = await test_client.get(f"/api/guilds/{value}")
        assert response.status == 404, value


async def test_an_unknown_station_is_a_404(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.delete("/api/stations/not-a-station", headers={"X-CSRF-Token": token})
    assert response.status == 404


# --------------------------------------------------------------------------- #
# The API payload shapes
# --------------------------------------------------------------------------- #
async def test_the_overview_lists_the_demo_guilds(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/overview")).json()
    assert payload["demo"] is True
    assert len(payload["guilds"]) == 3
    assert {"guilds", "connected", "playing", "listeners"} <= set(payload["stats"])
    assert payload["bot"]["version"]
    assert payload["stats"]["topTracks"]


async def test_every_snowflake_is_serialised_as_a_string(client):
    """Discord ids exceed double precision; a number in JSON corrupts them in
    the browser before the page ever sees it."""
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/overview")).json()
    assert all(isinstance(guild["id"], str) for guild in payload["guilds"])
    assert all(isinstance(event["guildId"], str) for event in payload["stats"]["recentEvents"] if event["guildId"])
    detail = await (await test_client.get(f"/api/guilds/{GUILD_ID}")).json()
    assert isinstance(detail["guild"]["id"], str)
    assert isinstance(detail["settings"]["voiceChannelId"], str)
    state = await (await test_client.get(f"/api/guilds/{GUILD_ID}/player")).json()
    assert isinstance(state["channelId"], str)


async def test_the_guild_detail_carries_settings_and_permissions(client):
    test_client = client[0]
    await authed(test_client)
    detail = await (await test_client.get(f"/api/guilds/{GUILD_ID}")).json()
    assert {"guild", "settings", "permissions", "voiceChannels", "textChannels", "roles"} <= set(detail)
    assert detail["settings"]["volume"]
    assert set(detail["settings"]) >= {
        "stationId", "voiceChannelId", "textChannelId", "autostart", "alwaysOn",
        "volume", "idleMinutes", "djRoleId", "announce", "loopMode", "channelStatus",
    }


async def test_the_permission_audit_flags_priority_speaker(client):
    """Granted in the demo server, and still reported as having no effect."""
    test_client = client[0]
    await authed(test_client)
    detail = await (await test_client.get(f"/api/guilds/{GUILD_ID}")).json()
    row = next(item for item in detail["permissions"] if item["key"] == "priority_speaker")
    assert row["granted"] is True
    assert row["state"] == "no-effect"
    assert "no effect" in row["reason"]


async def test_the_player_endpoint_reports_a_track(client):
    test_client = client[0]
    await authed(test_client)
    state = await (await test_client.get(f"/api/guilds/{GUILD_ID}/player")).json()
    assert state["connected"] is True
    assert state["track"]["title"]
    assert isinstance(state["listeners"], int)


async def test_a_player_action_changes_the_state(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/player", json={"action": "pause"}, headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["ok"] is True
    assert payload["player"]["paused"] is True
    assert payload["message"]
    state = await (await test_client.get(f"/api/guilds/{GUILD_ID}/player")).json()
    assert state["paused"] is True


async def test_the_action_response_carries_the_new_volume(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/player", json={"action": "volume", "data": {"percent": 42}},
        headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    assert (await response.json())["player"]["volume"] == 42


async def test_an_unknown_player_action_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/player", json={"action": "launch"}, headers={"X-CSRF-Token": token},
    )
    assert response.status in (400, 404, 422)
    assert (await response.json())["error"]


async def test_settings_are_saved_through_the_api(client):
    test_client, _, provider, _ = client
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/settings",
        json={"volume": 42, "autostart": True, "stationId": "chillhop"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    detail = await (await test_client.get(f"/api/guilds/{GUILD_ID}")).json()
    assert detail["settings"]["volume"] == 42
    assert detail["settings"]["autostart"] is True
    assert provider.config["guilds"][GUILD_ID]["volume"] == 42


async def test_an_out_of_range_volume_is_clamped_like_the_slash_command(client):
    """Same rule as /lofi volume: 0-150. Refusing would leave the slider and the
    command disagreeing about what a valid number is."""
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/settings", json={"volume": 4000}, headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    assert (await response.json())["settings"]["volume"] == 150


async def test_an_unknown_setting_key_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        f"/api/guilds/{GUILD_ID}/settings", json={"botToken": "steal"}, headers={"X-CSRF-Token": token},
    )
    assert response.status in (400, 422)


async def test_the_command_reference_is_served(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/commands")).json()
    rows = payload["commands"] if isinstance(payload, dict) else payload
    assert len(rows) == 30
    assert {"signature", "tier", "description"} <= set(rows[0])


async def test_the_station_list_is_served(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/stations")).json()
    listed = payload["stations"] if isinstance(payload, dict) else payload
    assert len(listed) >= 8
    assert any(station["id"] == "generative" for station in listed)


async def test_a_station_can_be_added_and_removed_through_the_api(client):
    test_client = client[0]
    token = await authed(test_client)
    added = await test_client.post(
        "/api/stations",
        json={"name": "Radio Test", "url": "https://example.com/stream.mp3", "art": "🎙"},
        headers={"X-CSRF-Token": token},
    )
    assert added.status == 200
    listing = await (await test_client.get("/api/stations")).json()
    listed = listing["stations"] if isinstance(listing, dict) else listing
    assert any(station["name"] == "Radio Test" for station in listed)

    removed = await test_client.delete("/api/stations/radio-test", headers={"X-CSRF-Token": token})
    assert removed.status == 200
    listing = await (await test_client.get("/api/stations")).json()
    listed = listing["stations"] if isinstance(listing, dict) else listing
    assert not any(station["name"] == "Radio Test" for station in listed)


async def test_a_station_pointing_at_a_private_address_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    for url in ("http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/stream", "ftp://example.com/a"):
        response = await test_client.post(
            "/api/stations", json={"name": "Bad", "url": url}, headers={"X-CSRF-Token": token},
        )
        assert response.status in (400, 422), url
        assert (await response.json())["error"]


async def test_a_station_without_a_name_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        "/api/stations", json={"name": "", "url": "https://example.com/a.mp3"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status in (400, 422)
    assert "name" in (await response.json())["error"].lower()


async def test_a_default_station_can_be_set(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        "/api/stations/groove-salad/default", json={}, headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    listing = await (await test_client.get("/api/stations")).json()
    assert listing["defaultStationId"] == "groove-salad"


async def test_a_station_can_be_tested_before_it_is_played(client):
    """The library station has nothing to play in a fresh install, and the test
    endpoint has to say that instead of reporting a broken player later."""
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        "/api/stations/library/test", json={}, headers={"X-CSRF-Token": token},
    )
    payload = await response.json()
    assert response.status in (200, 422)
    if response.status == 422:
        assert "mp3" in payload["error"] or "audio" in payload["error"].lower()


async def test_a_generated_station_can_be_tested(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post(
        "/api/stations/generative/test", json={}, headers={"X-CSRF-Token": token},
    )
    assert response.status == 200
    payload = await response.json()
    assert payload["ok"] is True
    assert payload["track"]["title"]


async def test_the_library_endpoint_reports_the_folder(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/library")).json()
    assert {"exists", "count", "files"} <= set(payload)


async def test_the_generative_endpoint_lists_the_moods(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/generative")).json()
    assert payload["available"] is True
    assert len(payload["recipes"]) == 5
    assert {"id", "name", "description"} <= set(payload["recipes"][0])


async def test_history_is_capped(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/history?limit=5")).json()
    assert len(payload["sessions"]) <= 5
    assert {"sessions", "events", "topTracks", "listeningMinutes"} <= set(payload)
    assert isinstance(payload["sessions"][0]["guildId"], str)


async def test_an_absurd_history_limit_is_capped(client):
    """MAX_DASHBOARD_ROWS exists so one request cannot dump the whole database."""
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/history?limit=999999")).json()
    assert len(payload["sessions"]) <= dashboard.MAX_HISTORY_ROWS
    assert len(payload["events"]) <= dashboard.MAX_HISTORY_ROWS


@pytest.mark.parametrize("limit", ["abc", "", "-5", "1.5", "%00", "9" * 40])
async def test_a_garbage_history_limit_falls_back(client, limit):
    """A query string is attacker-controlled text: nothing here may 500."""
    test_client = client[0]
    await authed(test_client)
    response = await test_client.get(f"/api/history?limit={limit}")
    assert response.status == 200
    assert (await response.json())["sessions"] is not None


async def test_a_garbage_guild_id_in_a_sync_request_is_refused(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post("/api/sync", json={"guildId": "not-an-id"},
                                      headers={"X-CSRF-Token": token})
    assert response.status == 404


async def test_the_settings_endpoint_reports_the_network_configuration(client):
    test_client = client[0]
    await authed(test_client)
    payload = await (await test_client.get("/api/settings")).json()
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == dashboard.DEFAULT_PORT
    assert payload["enabled"] is True
    assert payload["urls"]
    assert payload["tokenLength"] >= 32  # how long it is, never what it is


async def test_the_settings_endpoint_never_returns_the_token(client):
    """The key is shown once on the console; the API must not hand it back."""
    test_client = client[0]
    await authed(test_client)
    payload = json.dumps(await (await test_client.get("/api/settings")).json())
    assert KEY not in payload


async def test_a_command_sync_is_reported(client):
    test_client = client[0]
    token = await authed(test_client)
    response = await test_client.post("/api/sync", json={}, headers={"X-CSRF-Token": token})
    assert response.status == 200
    payload = await response.json()
    assert payload["ok"] is True
    assert payload["result"]["global"] == 30


# --------------------------------------------------------------------------- #
# ensure_dashboard_token
# --------------------------------------------------------------------------- #
def test_a_token_is_generated_once_and_saved(tmp_state):
    config = make_config(dashboard_token="")
    saved: list[dict] = []
    token = ensure_dashboard_token(config, saved.append)
    assert len(token) >= 32
    assert saved and saved[0]["dashboard_token"] == token


def test_an_existing_token_is_kept(tmp_state):
    config = make_config()
    assert ensure_dashboard_token(config, lambda updated: None) == KEY


def test_the_caller_s_dict_is_updated_in_place(tmp_state):
    """The bot keeps reading the dict it passed in, so it has to see the token."""
    config = make_config(dashboard_token="")
    ensure_dashboard_token(config, lambda updated: None)
    assert config["dashboard_token"]


def test_the_environment_overrides_the_stored_token(tmp_state, monkeypatch):
    monkeypatch.setenv("LOFI_DASHBOARD_TOKEN", "an-environment-token-that-is-long-enough")
    assert ensure_dashboard_token(make_config(), lambda updated: None) == "an-environment-token-that-is-long-enough"


def test_a_short_token_is_refused_rather_than_used(tmp_state):
    with pytest.raises(ValueError, match="at least 32"):
        ensure_dashboard_token(make_config(dashboard_token="too-short"), lambda updated: None)


def test_a_generated_token_is_url_safe_and_random(tmp_state):
    tokens = {ensure_dashboard_token(make_config(dashboard_token=""), lambda updated: None) for _ in range(5)}
    assert len(tokens) == 5
    assert all(token.replace("-", "").replace("_", "").isalnum() for token in tokens)


# --------------------------------------------------------------------------- #
# _apply_settings and the warnings
# --------------------------------------------------------------------------- #
def test_settings_come_from_the_config(tmp_state):
    server = DashboardServer(demo.DemoProvider(make_config(dashboard_host="10.0.0.5", dashboard_port=9001), token=KEY))
    server._apply_settings(make_config(dashboard_host="10.0.0.5", dashboard_port=9001))
    assert server._host == "10.0.0.5"
    assert server._port == 9001


def test_the_environment_overrides_the_config(tmp_state, monkeypatch):
    monkeypatch.setenv("LOFI_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("LOFI_DASHBOARD_PORT", "9100")
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config())
    assert server._host == "0.0.0.0"
    assert server._port == 9100


@pytest.mark.parametrize("port", ["not-a-port", "0", "70000", "-1", ""])
def test_a_bad_port_is_refused(tmp_state, monkeypatch, port):
    monkeypatch.delenv("LOFI_DASHBOARD_PORT", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    with pytest.raises(ValueError, match="dashboard_port"):
        server._apply_settings(make_config(dashboard_port=port))


def test_tls_needs_both_a_certificate_and_a_key(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_TLS_CERT", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_TLS_KEY", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    with pytest.raises(ValueError, match="together"):
        server._apply_settings(make_config(dashboard_tls_cert="/tmp/cert.pem"))


def test_a_missing_tls_file_says_which_one(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_TLS_CERT", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_TLS_KEY", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    with pytest.raises(ValueError, match="cert.pem"):
        server._apply_settings(make_config(
            dashboard_tls_cert="/tmp/does-not-exist-cert.pem",
            dashboard_tls_key="/tmp/does-not-exist-key.pem",
        ))


def test_exposing_the_port_over_http_produces_a_warning_that_says_the_fix(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_PORT", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config(dashboard_host="0.0.0.0"))
    warnings = server.remote_access_warnings()
    assert any("HTTPS" in warning for warning in warnings)


def test_a_secure_cookie_over_http_is_warned_about(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config(dashboard_secure_cookie=True))
    assert any("bounce" in warning for warning in server.remote_access_warnings())


def test_a_loopback_listener_produces_no_warning(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_PORT", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_PUBLIC_URL", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config())
    assert server.remote_access_warnings() == []


def test_a_public_url_without_a_scheme_is_warned_about(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_PUBLIC_URL", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config(dashboard_public_url="lofi.example.com"))
    assert any("scheme" in warning for warning in server.remote_access_warnings())


def test_browser_scheme_accounts_for_a_proxy_that_terminates_tls(tmp_state, monkeypatch):
    """With a reverse proxy in front, the listener is HTTP but the browser is HTTPS."""
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config(dashboard_public_url="https://lofi.example.com/"))
    assert server.scheme == "http"
    assert server.browser_scheme == "https"


def test_urls_are_absolute(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("LOFI_DASHBOARD_PORT", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config())
    assert all(url.startswith("http") for url in server.urls())


def test_a_wildcard_host_still_warns_about_dns_rebinding(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DASHBOARD_HOST", raising=False)
    server = DashboardServer(demo.DemoProvider(make_config(), token=KEY))
    server._apply_settings(make_config(dashboard_allowed_hosts=["*"]))
    assert server._allow_any_host is True
    assert any("DNS-rebinding" in warning for warning in server.remote_access_warnings())


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (111111111111111111, "111111111111111111"),
    ("222222222222222222", "222222222222222222"),
    (None, None),
    ("", None),
])
def test_snowflake_serialisation(value, expected):
    assert dashboard._snowflake(value) == expected


def test_snowflake_serialisation_is_a_serialiser_not_a_validator():
    """Route parameters are validated by _guild_id; this only shapes the output,
    so a stray value is passed through as the string it already is."""
    assert dashboard._snowflake(0) == "0"
    assert dashboard._snowflake("abc") == "abc"


def test_a_snowflake_is_never_emitted_as_a_json_number():
    big = 111111111111111111
    assert json.loads(json.dumps({"id": dashboard._snowflake(big)}))["id"] == str(big)
    assert big != int(float(big))  # the rounding this avoids


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("[::1]", True), ("::1", True),
    ("127.0.0.53", True), ("LOCALHOST", True), (" 127.0.0.1 ", True),
    ("0.0.0.0", False), ("example.com", False), ("192.168.1.5", False),
])
def test_loopback_detection(host, expected):
    assert dashboard._is_loopback_host(host) is expected


def test_a_missing_host_header_counts_as_loopback():
    """An HTTP/1.0 client sends no Host at all; that is a local curl, not a
    browser - a browser cannot omit the header, so rebinding cannot use it."""
    assert dashboard._is_loopback_host("") is True


def test_the_session_cookie_name_is_this_bot_s(client):
    assert dashboard.SESSION_COOKIE == "lofi_dashboard_session"
