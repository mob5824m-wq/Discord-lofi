# Lofi — a Discord bot that plays lofi in your voice channel

A self-hosted Discord bot that joins a voice channel and plays lofi without stopping, plus a
token-authenticated web dashboard for running it. No music-service account, no API keys beyond a
bot token, no privileged Discord intents.

```
python3 bot.py --demo        # the dashboard, with simulated servers, no token needed
python3 bot.py --check       # is this machine actually able to play audio?
python3 bot.py               # the bot
```

Four ways to get sound, so it works in almost any situation:

| Source | What it is | Network |
| --- | --- | --- |
| **YouTube broadcasts** | the 24/7 lofi streams (Lofi Girl, Chillhop, Synthwave Boy) | required |
| **Radio streams** | SomaFM's ambient channels, over plain HTTP | required |
| **Your own files** | `music/` shuffled forever, nothing leaves the machine | none |
| **Studio Lofi** | beats rendered on your machine from chord progressions | none |

The last two are the reason this bot keeps playing on a flaky network, in a country where YouTube
is throttled, or on a box with no internet at all.

| Document | For |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | getting it onto a machine: Linux/macOS/Windows, `--check`, where files live, systemd, Docker, updating |
| [docs/SETUP.md](docs/SETUP.md) | the Discord side: application, token, intents, the invite and every permission, Priority Speaker, first run, the dashboard, remote access |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | symptom → cause → fix, quoting the messages the bot actually prints |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how it is built and why, for changing it |

---

## Downloads — 6 native binaries

No Python needed — each release ships **6 artifacts: 1 arm + 1 native for each
OS** (Debian/Linux, macOS, Windows), built with PyInstaller from [`lofi.spec`](lofi.spec):

| OS | amd64 (native) | arm64 |
|---|---|---|
| **Debian / Ubuntu** | `lofi-linux-amd64` · `.tar.gz` | `lofi-linux-arm64` · `.tar.gz` |
| **macOS** | `lofi-macos-amd64` (Intel) | `lofi-macos-arm64` (Apple Silicon) |
| **Windows** | `lofi-windows-amd64.exe` · `.zip` | `lofi-windows-arm64.exe` · `.zip` |

```bash
# Linux example
tar -xzf lofi-1.0.0-linux-amd64.tar.gz
sudo install -m 0755 lofi-linux-amd64 /usr/local/bin/lofi
LOFI_TOKEN=... lofi --check   # then lofi
# Windows: Expand-Archive lofi-*-windows-*.zip; .\lofi-windows-amd64.exe --check
```

System deps still required (`ffmpeg` + `libopus` — `apt`/`brew`/`winget` line
below, or `pip install imageio-ffmpeg`). Details, local builds and CI matrix:
[`docs/PACKAGING.md`](docs/PACKAGING.md) · [`scripts/build.py`](scripts/build.py) · `make artifacts`.

Sources still run with Python — see Quick start below.

## Quick start

**1. Dependencies** — Python 3.9+ (3.11+ recommended), `ffmpeg` and `libopus`:

```bash
# Debian / Ubuntu
sudo apt install python3-venv ffmpeg libopus0
# macOS
brew install python ffmpeg opus
# Windows: winget install Gyan.FFmpeg  (opus is bundled with discord.py on Windows)
```

If your platform has no ffmpeg package, `pip install imageio-ffmpeg` ships a binary the bot will
find on its own. **arm64 works with no changes** — Raspberry Pi OS, Apple Silicon, Graviton: every
native dependency publishes aarch64 wheels and there is no compiled code here
([details](docs/INSTALL.md#arm64--raspberry-pi-apple-silicon-graviton)).

**2. Install and create a bot token**

```bash
git clone https://github.com/mob5824m-wq/Discord-lofi
cd Discord-lofi
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # optional: pytest + pytest-asyncio
```

Create the application at <https://discord.com/developers/applications> → *Bot* → *Reset Token*,
then invite it with **both** the `bot` and `applications.commands` scopes — the bot publishes its own
slash commands on start, and it can only do that in a server that granted the scope:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot%20applications.commands&permissions=281474979941376
```

That permission integer asks for: View Channels, Connect, Speak, Set Voice Channel Status, Send
Messages, Embed Links, Read Message History. Nothing privileged — no Members, no Presences, no
Message Content, so there is nothing to enable in the developer portal for the bot to log in.

Step by step, including what each permission is for and what breaks without it:
[docs/SETUP.md](docs/SETUP.md).

**3. Configure**

```bash
cp config.example.json config.json   # config.json is gitignored: it holds your token
nano config.json                     # set "bot_token"
```

or `export LOFI_TOKEN=...` if you would rather keep the token out of a file entirely. On a fresh
clone the bot reads `config.example.json` and writes its own config to the data directory, so it
runs before you copy anything.

**4. Check, then run**

```bash
python3 bot.py --check     # ✓ ffmpeg, ✓ libopus, ✓ token, ✓ data dir …
python3 bot.py
```

A live run logs the URL but **not** the key — a login key in a journald stream is a key in every
backup of that stream:

```
Dashboard listening at http://127.0.0.1:8790
Connected as Lofi (id 123456789012345678) in 3 server(s).
Dashboard: http://127.0.0.1:8790/
Dashboard key: run 'python3 bot.py --dashboard-token' to print it.
```

So print it when you want it:

```bash
python3 bot.py --dashboard-token
```

`--demo` is the exception and prints the key inline, because there is no bot and nothing to protect:

```
Demo dashboard - no Discord connection, simulated servers.
  open:  http://127.0.0.1:8790/
  key:   REZSbNBb6rqJc8_xBoy6fgdSfwoOTx1twuWTa-v5Vi69UE1n
  (also printable with 'python3 bot.py --dashboard-token')
```

**5. Play**

Join a voice channel and run `/lofi play`. Or, without YouTube, `/lofi studio mood:cafe`.

---

## The dashboard

One self-contained HTML file served by the bot itself, sharing the bot's event loop and its SQLite
state. No build step, no CDN, no external requests — it works from a machine that can reach the bot
and nothing else.

| Page | What you can do there |
| --- | --- |
| **Overview** | servers, listeners, listening minutes, most-played tracks, activity feed |
| **Player** | transport (play / pause / skip / stop), volume, queue, the channel status line as Discord shows it, and the player's health: restarts, last error, stall detection |
| **Servers** | per-server settings, the voice-permission audit, that server's listening stats |
| **Stations** | add / test / default / remove stations; render a Studio Lofi mood; see the local library |
| **History** | finished sessions with duration, peak listeners and why each one ended |
| **Commands** | the live command tree with signatures and who may run each one |
| **Settings** | network binding, Host allowlist, trusted proxies, TLS, key rotation |

### Seeing it without a bot

```bash
python3 bot.py --demo
```

Serves the same dashboard against three simulated servers that move: tracks advance, listeners
drift, actions work, settings save. Every response is marked `demo: true` and the UI shows a banner,
so there is no way to mistake it for a live connection.

### Security

The dashboard can stop music and rewrite configuration on someone's server, so it is treated like
an admin panel:

- **One key**, `secrets.token_urlsafe(36)`, generated on first run and stored in `config.json`
  (mode `0600`). `LOFI_DASHBOARD_TOKEN` overrides it for containers, so the key never touches disk.
  Fewer than 32 characters is refused rather than used.
- **Sessions** are server-side, 8 hours, sliding; the cookie is `HttpOnly` + `SameSite=Strict`, and
  forced `Secure` whenever TLS is on.
- **CSRF tokens** are required on every mutating request and compared with `hmac.compare_digest`.
- **Login throttling**: five failures from one address in five minutes → `429`.
- **Host allowlist**: any `Host` header that is not loopback, not listed in
  `dashboard_allowed_hosts` and not the hostname of `dashboard_public_url` is answered `400`. That
  is what stops DNS rebinding — a page in your browser cannot read the API by pointing a name at
  `127.0.0.1`, even with your session cookie.
- **Headers** on every response: a CSP that allows nothing but this origin and Discord's image CDN,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`.
- **Bound to `127.0.0.1` by default.** Listening on `0.0.0.0` over plain HTTP produces a warning
  that says what to do about it, not just that it is risky.

### Reaching it from another machine

Put it behind a reverse proxy with TLS (Caddy, nginx, Traefik) and tell the dashboard the name
visitors will use:

```json
{
  "dashboard_host": "127.0.0.1",
  "dashboard_port": 8790,
  "dashboard_public_url": "https://lofi.example.com",
  "dashboard_allowed_hosts": ["lofi.example.com"],
  "dashboard_trusted_proxies": ["127.0.0.1"],
  "dashboard_secure_cookie": true
}
```

Or terminate TLS in the bot itself with `dashboard_tls_cert` / `dashboard_tls_key` (or
`LOFI_DASHBOARD_TLS_CERT` / `LOFI_DASHBOARD_TLS_KEY`). Both must be set together; the cookie
becomes `Secure` automatically.

`--dashboard-allowed-hosts '*'` accepts any `Host` header. It exists for a throwaway local preview
(the sandbox this project was built in, for example) and it logs a warning saying that it disables
DNS-rebinding protection.

### Running it next to another bot

This dashboard is modelled on [Sentinel](https://github.com/mob5824m-wq/Sentinel)'s, and running both
on one machine is a normal thing to want — moderation in one, music in the other. Every namespace is
separate, so the second one to start just works:

| | Sentinel | Lofi |
| --- | --- | --- |
| dashboard port | `8765` | **`8790`** |
| session cookie | `sentinel_dashboard_session` | `lofi_dashboard_session` |
| state directory | `~/.local/state/sentinel` | `~/.local/state/lofi` |
| database | `punishments.db` | `lofi.db` |
| log file | `bot.log` | `lofi.log` |
| environment variables | `SENTINEL_*` | `LOFI_*` |
| slash commands | `/manage …` | `/lofi …` |

Both bind `127.0.0.1` and that is fine, but note *why* the cookie names matter: **cookies ignore
ports**, so two dashboards on the same host share one cookie jar. `tests/test_coexistence.py` pins
each row of that table, including that a session lifted from one dashboard is refused by the other.

Two things are yours to separate, because they are per-machine rather than per-bot:

- **A reverse proxy.** Give each dashboard its own subdomain or port. Both serve their UI at `/`, so
  they cannot share one `server {}` block.
- **A second copy of Lofi.** Change `dashboard_port` (or `LOFI_DASHBOARD_PORT`) *and* `LOFI_HOME`, or
  the two copies will fight over the port, the config file and the database.

Deployment-level detail — unit names, containers, proxy config — is in
[docs/INSTALL.md](docs/INSTALL.md#running-it-alongside-another-bot).

---

## Stations

Built in:

| id | name | kind | what it is |
| --- | --- | --- | --- |
| `lofi-girl` | 📚 Lofi Girl | YouTube | beats to relax / study to — the 24/7 lofi hip hop broadcast |
| `synthwave-boy` | 🌆 Synthwave Boy | YouTube | beats to chill / game to — neon-flavoured synthwave |
| `chillhop` | ☕ Chillhop Radio | YouTube | jazzy & lofi hip hop beats, streamed around the clock |
| `sleepy-lofi` | 🌙 Sleepy Lofi | YouTube | slow, rain-soaked beats for late nights — found by search |
| `groove-salad` | 🥗 Groove Salad | stream | a nicely chilled plate of ambient / downtempo (SomaFM) |
| `secret-agent` | 🕶️ Secret Agent | stream | spy-lounge downtempo and trip-hop (SomaFM) |
| `library` | 💿 My Library | files | everything in `music/`, shuffled, never repeating until it must |
| `generative` | 🎹 Studio Lofi | rendered | beats made on this machine — no network, no copyright |

A YouTube broadcast changes its video id occasionally. When one does, the station falls back to its
search query and gets back on air by itself; you can also correct the link from the dashboard
without touching code.

### Adding your own

`/lofi station add name:"Radio Paradise" url:"https://example.com/stream.mp3"` — or the Stations
page in the dashboard, which lets you **test** a station before you play it: a stream is connected
to, a YouTube link is resolved, a generated mood is rendered, and a library station tells you
plainly that the folder has no audio in it.

Accepted URLs are `http(s)` streams, YouTube links, `library:/path/to/folder`, and `generative`.
An address that points at `localhost`, a private range (`10/8`, `172.16/12`, `192.168/16`,
`169.254/16`) or `file://` is refused — a bot should not be steerable into your own network.

### Studio Lofi

Five moods, each a chord progression, a tempo and a mix setting, rendered to a 52-second WAV in
about three seconds:

| mood | tempo | progression |
| --- | --- | --- |
| `midnight` — Midnight Notebook | 72 bpm | Am9 – Fmaj7 – Cmaj9 – G7 |
| `rainy` — Rain On The Fire Escape | 80 bpm | Dm11 – Gm9 – B♭maj7 – A7sus4 |
| `cafe` — Cafe Window Seat | 88 bpm | Gm9 – C9 – Fmaj9 – Dm7 |
| `dusty` — Dusty Cassette | 76 bpm | Gmaj7 – Em7 – Am7 – D7 |
| `nocturne` — Neon Nocturne | 84 bpm | C♯m9 – Fm7♭5 – Amaj7 – Cdim7 |

`/lofi studio mood:cafe` plays one; the next track is a new seed of the same mood, pre-rendered
while the current one plays so there is no gap. Rendered files are cached under the data directory
and pruned to the newest 40. It needs `numpy` (`pip install numpy`); without it the station reports
that it is unavailable instead of failing quietly.

Titles are generated too — *Cassette Sun in the attic*, *Slow Commute under fluorescent light* — so
the now-playing line is not the same string all night.

### Your own files

Drop audio into `music/` next to `bot.py` (or set `LOFI_MUSIC`). `.mp3 .flac .ogg .opus .wav .m4a
.aac .wma .aiff` are found recursively; `Artist - Title.mp3` and `03 - Title.flac` are parsed into a
title. The shuffle remembers the last 24 files so a three-track folder does not repeat itself
immediately, and falls back to repeating once there is nothing else to play.

---

## Commands

Everything is a slash command under `/lofi`. Three permission tiers:

- **Everyone** — read-only: `/lofi now`, `/lofi stations`, `/lofi upnext`, `/lofi status`, `/lofi library`
- **Transport** — anyone in the *same voice channel* as the bot, or the DJ role, or Manage Server
- **Configuration** — Manage Server or Administrator

| Command | What it does | Who can use it |
| --- | --- | --- |
| `/lofi play [station] [channel]` | Join your voice channel and start a lofi station | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi pause` | Pause playback | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi resume` | Resume playback | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi skip` | Skip to the next track | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi stop` | Stop playback and leave the channel | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi leave` | Leave the voice channel without changing settings | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi join [channel]` | Join a voice channel without starting playback | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi volume percent` | Set playback volume | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi loop mode:off/station/queue` | What happens when a track ends | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi queue item` | Add a one-off station or link after the current track | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi studio mood:midnight/rainy/cafe/dusty/…` | Play a beat rendered on this machine — no network needed | Anyone in the same voice channel (or the DJ role / Manage Server) |
| `/lofi now` | What is playing right now | Everyone |
| `/lofi stations` | List every station this bot can play | Everyone |
| `/lofi upnext` | Show the queue and what comes next | Everyone |
| `/lofi status` | Diagnostics: playback, settings and permissions | Everyone |
| `/lofi library` | Show what is in the bot's local music folder | Everyone |
| `/lofi setup station station` | Set the default station for this server | Manage Server or Administrator |
| `/lofi setup voice_channel channel` | Set the channel Lofi joins on autostart | Manage Server or Administrator |
| `/lofi setup text_channel [channel]` | Where now-playing messages are posted | Manage Server or Administrator |
| `/lofi setup volume percent` | Set the default volume | Manage Server or Administrator |
| `/lofi setup idle minutes` | Minutes of an empty channel before Lofi leaves | Manage Server or Administrator |
| `/lofi setup autostart enabled` | Start playing automatically when the bot boots | Manage Server or Administrator |
| `/lofi setup always_on enabled` | Stay connected even when the channel is empty | Manage Server or Administrator |
| `/lofi setup dj_role [role]` | Restrict playback control to one role | Manage Server or Administrator |
| `/lofi setup announce enabled` | Post a now-playing message when the station changes | Manage Server or Administrator |
| `/lofi setup status_line enabled` | Show the current track as the voice channel's status | Manage Server or Administrator |
| `/lofi setup reset` | Clear this server's overrides back to the defaults | Manage Server or Administrator |
| `/lofi station add name url` | Add a custom station (URL, folder or `generative`) | Manage Server or Administrator |
| `/lofi station remove station` | Remove a custom station (or restore a built-in one) | Manage Server or Administrator |
| `/lofi station default station` | Set the station used when `/lofi play` has no argument | Manage Server or Administrator |

`/lofi volume` and `/lofi loop` are deliberately **not** saved: anyone in the channel can nudge the
volume down for the night without changing the server's configuration. `/lofi setup volume` is the
persistent one.

---

## Discord permissions

Give the bot's role, **in the voice channel it will use**:

| Permission | Needed? | Why |
| --- | --- | --- |
| View Channel | **yes** | without it the bot cannot see the channel exists |
| Connect | **yes** | to join it |
| Speak | **yes** | without it the bot joins, looks connected, and is silent |
| Set Voice Channel Status | optional | shows the current track under the channel name |
| Manage Channels | optional | only if you want the bot to move itself between channels |
| Priority Speaker | granted, but **inert** | see below |

`/lofi status` and the dashboard's Servers page run this audit live, per channel, and name whatever
is missing. That matters because a missing *Speak* permission fails **silently** — the bot joins,
nothing plays, nothing is logged, and it looks exactly like a broken ffmpeg install.

### About Priority Speaker

Priority Speaker is granted to a role or per channel by someone with Manage Roles, and it **only
works with the desktop app's Push-to-Talk input mode**: the person speaking holds their own
"Push-to-Talk (Priority)" keybind, which ducks everyone else. A bot has no input mode and no
keybind, and there is no gateway field, HTTP route or discord.py method that activates it. So
granting it to this bot does nothing at all — the permission is real, the effect is not.

What you probably wanted instead:

- **Music quieter than voices** → `/lofi volume 40` (0–150, default 60). Discord mixes the two;
  there is no ducking API for bots.
- **Music that stops when people talk** → not available to bots; Priority Speaker is a client-side
  feature and cannot be triggered from the outside.
- **A human DJ who can duck the music** → give them the DJ role (`/lofi setup dj_role:`) so they can
  pause and lower it from anywhere in the server.

The audit reports Priority Speaker as `no-effect` rather than `ok` or `missing`, so nobody spends an
evening wondering why it is not doing anything.

### The voice channel status line

With *Set Voice Channel Status*, the channel shows `🎹 Cassette Sun in the attic` under its name,
updated as tracks change (rate-limited, and cleared when playback stops). Discord does not let a bot
read a channel's status back, so the bot tracks the last value it wrote — the dashboard shows that
value, labelled as what the bot last set.

---

## Configuration

Read from, in order: `LOFI_CONFIG`, `<data dir>/config.json`, `./config.json`,
`./config.example.json`. Written to: `LOFI_CONFIG` if set, otherwise `<data dir>/config.json` — the
data directory defaults to your platform state directory (`~/.local/state/lofi` on Linux,
`~/Library/Application Support/Lofi` on macOS, `%APPDATA%\Lofi` on Windows), and `LOFI_HOME` moves
it. The committed template is never a write target.

```json
{
  "bot_token": "",
  "default_station": "lofi-girl",
  "default_volume": 60,
  "idle_disconnect_minutes": 15,
  "stay_connected": false,
  "announce_now_playing": true,
  "channel_status": true,
  "sync_commands_on_start": true,
  "stations": [],
  "guilds": {
    "400000000000000001": {
      "station_id": "groove-salad",
      "voice_channel_id": "500000000000000001",
      "text_channel_id": "600000000000000001",
      "volume": 60,
      "autostart": true,
      "always_on": false,
      "idle_minutes": 15,
      "dj_role_id": null,
      "announce": true,
      "loop_mode": "off",
      "channel_status": true
    }
  },
  "dashboard_enabled": true,
  "dashboard_host": "127.0.0.1",
  "dashboard_port": 8790,
  "dashboard_token": "",
  "dashboard_allowed_hosts": [],
  "dashboard_public_url": "",
  "dashboard_secure_cookie": false,
  "dashboard_trusted_proxies": []
}
```

Top-level keys are the defaults; a `guilds` entry overrides them for one server. Anything set from
Discord (`/lofi setup …`) or the dashboard lands in that server's entry. Unknown keys are kept, so a
config written by a newer version survives a downgrade.

Environment variables: `LOFI_TOKEN`, `LOFI_HOME`, `LOFI_CONFIG`, `LOFI_DB`, `LOFI_MUSIC`,
`LOFI_FFMPEG`, `LOFI_FFPROBE`, `LOFI_OPUS`, `LOFI_LOG_LEVEL`, `LOFI_DASHBOARD_TOKEN`, `LOFI_DASHBOARD_HOST`,
`LOFI_DASHBOARD_PORT`, `LOFI_DASHBOARD_PUBLIC_URL`, `LOFI_DASHBOARD_TLS_CERT`,
`LOFI_DASHBOARD_TLS_KEY`.

### Command line

```
--check                     diagnose the install and exit (exit 1 if audio cannot work)
--demo                      dashboard only, with simulated servers, no token
--dashboard-token           print the login key and exit
--no-dashboard              do not start the web dashboard
--dashboard-host HOST       override dashboard_host
--dashboard-port PORT       override dashboard_port
--dashboard-allowed-hosts   comma-separated Host headers to accept ("*" for a local preview)
--sync-commands             publish the slash commands and exit
--log-level LEVEL           DEBUG | INFO | WARNING | ERROR
--logfile PATH              write logs here instead of the data directory
--version                   print the version and exit
```

---

## Running it for real

**systemd** (a unit that survives a reboot and restarts on a crash):

```ini
[Unit]
Description=Lofi Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lofi
WorkingDirectory=/opt/lofi
# Pin the state directory: config.json, lofi.db, the log and the rendered-track
# cache all live under it. Without this it would be /home/lofi/.local/state/lofi.
Environment=LOFI_HOME=/var/lib/lofi
ExecStart=/opt/lofi/.venv/bin/python /opt/lofi/bot.py
Restart=always
RestartSec=10
# /opt/lofi holds your music/ folder, which the bot reads and may rescan:
ReadWritePaths=/opt/lofi /var/lib/lofi
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

`sudo install -d -o lofi -g lofi /var/lib/lofi` before the first start, then
`systemctl daemon-reload && systemctl enable --now lofi`.

**Docker** needs ffmpeg and libopus in the image, and the data directory as a volume — it holds
`config.json`, the SQLite state and the generated-track cache.

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV LOFI_HOME=/data
VOLUME /data
EXPOSE 8790
CMD ["python", "bot.py"]
```

Keep the token in the environment (`LOFI_TOKEN`, `LOFI_DASHBOARD_TOKEN`) rather than in the image.

**Autostart and 24/7** — `/lofi setup voice_channel:` then `/lofi setup autostart:true` makes the
bot join and play whenever it starts. Add `/lofi setup always_on:true` to stay in an empty channel
(radio-station behaviour); without it, an empty channel for `idle_minutes` ends the session and the
bot leaves, which is what you want for a study room.

---

## What happens when a track ends

Nothing ends. That is the whole design:

- a **live broadcast or stream** is reconnected — the "next track" of a 24/7 stream is the same
  stream, so it is restarted rather than advanced;
- a **local file** moves to the next one in the shuffle;
- a **generated** track renders a new seed of the same mood, pre-rendered during the previous track
  so there is no gap;
- a **queue** item plays before any of that;
- a **failure** (ffmpeg died, the stream stalled, YouTube returned 403) is retried with an
  exponential backoff, up to six attempts, and then the bot leaves the channel and says why in the
  configured text channel — sitting silent in a voice channel is worse than admitting defeat.

A watchdog runs every five seconds: it notices a dropped voice connection, an empty channel past
the idle limit, and a stream that stopped sending audio without closing (the classic icecast
failure), and it keeps the channel status line current. Every session, track play and event is
written to SQLite, which is what the dashboard's History and Overview pages read.

---

## Troubleshooting

`python3 bot.py --check` answers most of it. The short version:

| Symptom | Cause |
| --- | --- |
| Joins the channel, no sound | missing **Speak** permission — `/lofi status` will name it |
| `Could not find opus library` | `apt install libopus0` / `brew install opus` |
| `ffmpeg was not found` | `apt install ffmpeg`, or `pip install imageio-ffmpeg` |
| YouTube: *Sign in to confirm…* | YouTube is blocking that IP; use a stream, your library, or Studio Lofi |
| YouTube: *HTTP 403* | yt-dlp is out of date — `pip install -U yt-dlp` |
| Dashboard loads, every login bounces | `dashboard_secure_cookie` on while the browser reaches it over HTTP |
| Dashboard `400 Unrecognized Host header` | add your public name to `dashboard_allowed_hosts` |
| Commands do not appear | `/lofi status`, then check the bot was invited with `applications.commands` |

Full detail, including voice-connection timeouts (usually outbound UDP 50000–65535 being filtered),
is in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## How it is put together

| File | Responsibility |
| --- | --- |
| `bot.py` | entry point, `LofiBot`, the install check, the CLI |
| `music.py` | the 30 slash commands and the permission guards behind them |
| `player.py` | one guild's playback: connect, advance, watchdog, sessions, status line |
| `sources.py` | station → something ffmpeg can open; library scanning; yt-dlp resolution |
| `generative.py` | the offline renderer: recipes, synthesis, WAV cache |
| `stations.py` | the station registry, validation, custom stations |
| `dashboard.py` | the aiohttp server: auth, CSRF, Host allowlist, the JSON API |
| `dashboard.html` | the UI — one file, inline CSS/JS, no external assets |
| `demo.py` | the simulated provider behind `--demo` |
| `store.py` | SQLite: sessions, track plays, events |
| `settings.py` | per-guild settings resolution, pure functions over the config |
| `permissions.py` | the voice-permission audit |
| `command_tree.py` | the command table the README, the dashboard and the tests all read |
| `paths.py` | config/data/cache locations, ffmpeg and opus discovery |

Design notes — why the dashboard shares the bot's loop, why settings are pure functions, why the
renderer is float32 and tiled — are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt   # or: pip install pytest pytest-asyncio
python3 -m pytest
```

**839 tests, no network, no Discord token, ~50 s.**

| file | tests | what it covers |
| --- | --- | --- |
| `test_paths.py` | 42 | XDG/Windows/macOS data dirs, config precedence, ffmpeg + opus discovery |
| `test_settings.py` | 59 | every config key: coercion, defaults, clamping, precedence |
| `test_stations.py` | 101 | validation, dedupe, merge order, live-detection heuristics, presets |
| `test_store.py` | 49 | schema, migrations, sessions, events, history aggregates, corruption recovery |
| `test_generative.py` | 50 | DSP: peak levels, envelopes, chord spelling, drum grids, render pipeline |
| `test_sources.py` | 76 | yt-dlp plumbing, blocking-call guard, cache TTL, error mapping |
| `test_permissions.py` | 32 | voice/text permission math, audit report, the Priority Speaker verdict |
| `test_player.py` | 83 | connect, queue, watchdog stall recovery, status throttle, failure modes |
| `test_music.py` | 37 | library scan, cache pruning, generative render cache, track rotation |
| `test_command_tree.py` | 55 | every slash command: signature, permission tier, description, response text |
| `test_dashboard.py` | 140 | auth, CSRF, sessions, rate limit, Host allowlist, headers, every route |
| `test_bot.py` | 48 | `--check` startup diagnostics, dashboard bootstrap, token handling |
| `test_demo.py` | 47 | the demo provider: simulated guilds, ticks, listener drift |
| `test_coexistence.py` | 20 | nothing shared with a sibling bot on the same machine: port, cookie, state dir, database, env prefix, command group |

`tests/test_dashboard.py` is the file to read before you change anything about authentication.

Both suites run in CI on every push and pull request
([.github/workflows/build.yml](.github/workflows/build.yml)): pytest across Python 3.9, 3.11 and
3.13 on a runner with real ffmpeg and libopus installed, plus the jsdom harness against a live
`--demo` server. No job needs a Discord token or network access to YouTube.

The UI is checked separately, in [tests/ui/](tests/ui/): a jsdom harness that loads the real
`dashboard.html`, points it at a live `--demo` server and drives it — login, every page, the
transport buttons, the settings form round-trip, station add/test/remove, the history tables, the
empty and error states, logout. 64 assertions on *rendered text*, not on CSS, so markup that loses
information fails the run, and any `console.error` fails it too.

```bash
cd tests/ui && npm install
python bot.py --demo --dashboard-token ui-test-key --dashboard-port 8790   # shell 1
node tests/ui/dashboard.ui.js ui-test-key                                  # shell 2
```

## Legal and licensing

The bot plays what you point it at. The built-in YouTube stations are public 24/7 broadcasts whose
owners publish them for exactly this; SomaFM streams are free to relay but are their work — if you
run this publicly, consider [supporting SomaFM](https://somafm.com/) and the artists behind the
broadcasts. Studio Lofi's output is generated here from chord progressions and contains nobody's
recording. Your own `music/` folder is your responsibility.

Code: MIT — see [LICENSE](LICENSE).
