# Installing Lofi

Getting the software onto a machine. What to do *after* it starts — creating the Discord
application, inviting the bot, picking a station — is in [SETUP.md](SETUP.md). When something goes
wrong, [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

- [What you need](#what-you-need)
- [Debian / Ubuntu](#debian--ubuntu)
- [macOS](#macos)
- [Windows](#windows)
- [arm64 — Raspberry Pi, Apple Silicon, Graviton](#arm64--raspberry-pi-apple-silicon-graviton)
- [Getting the code](#getting-the-code)
- [Verifying the install](#verifying-the-install)
- [Where files live](#where-files-live)
- [Running it as a service](#running-it-as-a-service)
- [Docker](#docker)
- [Running it alongside another bot](#running-it-alongside-another-bot)
- [Updating](#updating)
- [Removing it](#removing-it)

---

## What you need

| Piece | Required? | Why |
| --- | --- | --- |
| Python **3.9+** | yes | the bot itself (3.11 or 3.12 recommended) |
| **ffmpeg** | yes | decodes everything into the PCM Discord needs |
| **libopus** | yes | encodes that PCM into Opus for voice |
| `yt-dlp` | no | YouTube stations and resolving links. Without it only streams, your library and Studio Lofi work |
| `numpy` | no | Studio Lofi, the offline renderer. Without it that station is disabled and the dashboard says so |
| A Discord **bot token** | yes, to go online | `--demo` runs the whole dashboard with simulated servers and no token |

ffmpeg and libopus are the two that trip people up, because neither is a Python package: discord.py
shells out to an `ffmpeg` binary and loads `libopus` as a shared library at runtime. Both are
available everywhere, and there is a pip fallback for ffmpeg (`imageio-ffmpeg`) that the bot will
find on its own.

---

## Debian / Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg libopus0
ffmpeg -version | head -1        # sanity check
```

`libopus0` is the runtime library. On a machine that will also *build* things against opus you
would want `libopus-dev`, but the bot only dlopens the shared object, so `libopus0` is enough.

Raspberry Pi OS / other Debian derivatives: identical. On arm64 the same package names apply.

## macOS

```bash
brew install python ffmpeg opus
```

discord.py finds Homebrew's opus at `/opt/homebrew/lib/libopus.0.dylib` (Apple Silicon) or
`/usr/local/lib/libopus.0.dylib` (Intel). If it does not — an unusual prefix, a MacPorts install —
point at it explicitly:

```bash
export LOFI_HOME="$HOME/Library/Application Support/Lofi"
python3 bot.py --check     # reports which opus it found
```

## Windows

Native Windows works, and discord.py bundles an opus DLL there, so you usually only need ffmpeg:

```powershell
winget install Gyan.FFmpeg      # or: choco install ffmpeg
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py --check
```

If `--check` reports libopus missing, download `libopus-0.dll` and put it next to `bot.py`; the bot
looks in its own directory before searching the system.

WSL2 is the better option if you plan to keep this running: treat it as the Linux case above, and
remember that audio is produced *inside* the VM and streamed to Discord over the network — your
Windows sound card is not involved at all.

### arm64 — Raspberry Pi, Apple Silicon, Graviton

Supported with nothing to change and no compiler needed:

- **No compiled code here.** The renderer is numpy; everything else is Python.
- **Every native dependency publishes aarch64 wheels** — `PyNaCl`, `numpy`, `aiohttp`, `davey`
  (pulled in by `discord.py[voice]`) and `audioop-lts` on Python 3.13 all ship
  `manylinux*_aarch64` builds. `discord.py` and `yt-dlp` are pure Python.
- **ffmpeg and libopus come from the distro.** Raspberry Pi OS is Debian: `sudo apt install ffmpeg
  libopus0`, the same command as on x86. The pip fallback works too — `imageio-ffmpeg` publishes a
  `manylinux2014_aarch64` wheel with a bundled binary that `--check` finds on its own.
- **Docker**: `python:3.12-slim` is multi-arch, so the Dockerfile below builds unchanged on arm64 —
  natively on the Pi, or with `docker buildx build --platform linux/arm64`.
- **Apple Silicon**: `brew install python ffmpeg opus`; discord.py finds opus at
  `/opt/homebrew/lib/libopus.0.dylib`.

What arm64 does change is headroom. Each connected voice channel costs one ffmpeg process and one
Opus encoder thread, and Studio Lofi's render is CPU-bound — about 3 s per track on the x86 machine
this was built on, longer on a Pi 3. The render is cached by content hash, so that cost is paid once
per mood per machine; on a small board, prefer the library and stream stations, or play a mood once
and let the cache do the rest.

---

## Getting the code

```bash
git clone https://github.com/mob5824m-wq/Discord-lofi.git
cd Discord-lofi

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt      # runtime
pip install -r requirements-dev.txt  # optional: pytest + pytest-asyncio
```

`requirements.txt`:

```
discord.py[voice]>=2.4.0
aiohttp>=3.9
yt-dlp>=2024.10.7
numpy>=1.24
```

`discord.py[voice]` (the extra, not the bare package) is what pulls in PyNaCl for voice encryption.
`imageio-ffmpeg` is commented out in that file — install it only if your platform has no ffmpeg
package, and the bot will find its bundled binary on its own.

Then the config:

```bash
cp config.example.json config.json
```

`config.json` is **gitignored** on purpose — it is the file a bot token and a dashboard key end up
in, and a token committed to git history is a token you have to rotate. On a fresh clone with no
`config.json`, the bot reads `config.example.json` and writes its own config into the data
directory, so it starts before you copy anything; it just will not have a token yet.

---

## Verifying the install

```bash
python3 bot.py --check
```

This is the fastest way to find out what is missing. It prints one row per dependency with a state,
a detail and — when something is wrong — the exact command to fix it:

| Row | Checks |
| --- | --- |
| `Python` | version against the 3.9 minimum |
| `discord.py` | importable, and which version |
| `ffmpeg` | a binary on `PATH`, `LOFI_FFMPEG`, common install locations, or the `imageio-ffmpeg` bundle |
| `libopus` | whether discord.py can load it, or where a candidate library was found |
| `yt-dlp` | installed version, or that YouTube stations will not resolve |
| `numpy` | installed version, or that Studio Lofi is disabled |
| `music folder` | whether `music/` exists and how many playable files are in it |
| `bot token` | present (and how long), from `config.json` or `LOFI_TOKEN` |
| `config file` | where config is read from and where writes will go, and whether that is writable |
| `data directory` | exists and is writable |
| `dashboard` | the host:port it will bind, plus a warning if that is reachable off this machine |
| `stations` | how many stations are available and how many work with no network at all |

`--check` exits non-zero if any of **Python, ffmpeg, libopus or the bot token** is missing — the
four things without which there is no audio. Everything else is a warning or "optional".

Nothing in `--check` connects to Discord, so it is safe to run on a machine with no internet and
safe to run repeatedly.

---

## Where files live

One directory holds all mutable state, so a backup is one folder and a migration is `scp`:

```
<data dir>/
├── config.json     # bot token, dashboard key, defaults, per-server settings
├── lofi.db         # SQLite: play sessions, track plays, event log
├── lofi.log        # only when started with --logfile
└── cache/
    └── gen-*.wav   # rendered Studio Lofi tracks, content-hashed, pruned to the newest 40
```

The data directory is, in order of precedence:

1. `LOFI_HOME` — set this in a service unit, a container or a test.
2. The platform state directory, if it can be created and written:
   - Linux/BSD: `$XDG_STATE_HOME/lofi`, else `~/.local/state/lofi`
   - macOS: `~/Library/Application Support/Lofi`
   - Windows: `%APPDATA%\Lofi`
3. `./data` next to the sources — the fallback for a read-only home or a sandbox.

`python3 bot.py --check` prints the path it resolved, so you never have to guess.

Two directories are deliberately *not* under the data directory:

- **`config.example.json`** stays with the code (it is the committed template, and it is read but
  never written).
- **`music/`** stays next to `bot.py`, because that is a folder people already have opinions about
  and may want to point at an existing collection. `LOFI_MUSIC=/path/to/music` overrides it, and
  `<data dir>/music` is checked as a second location.

The data directory is chmod'ed `0700` and `config.json` is written `0600`, best effort — a bot
token is a credential that can control your bot user.

---

## Running it as a service

**systemd** (the usual answer on a VPS):

```ini
# /etc/systemd/system/lofi.service
[Unit]
Description=Lofi Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lofi
WorkingDirectory=/opt/lofi
Environment=LOFI_HOME=/var/lib/lofi
ExecStart=/opt/lofi/.venv/bin/python /opt/lofi/bot.py
Restart=always
RestartSec=10
ReadWritePaths=/opt/lofi /var/lib/lofi
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd --system --home /opt/lofi --shell /usr/sbin/nologin lofi
sudo install -d -o lofi -g lofi /var/lib/lofi
sudo git clone https://github.com/mob5824m-wq/Discord-lofi.git /opt/lofi   # or copy your checkout
sudo -u lofi /opt/lofi/.venv/bin/pip install -r /opt/lofi/requirements.txt
sudo systemctl daemon-reload && sudo systemctl enable --now lofi
journalctl -u lofi -f
```

Pin `LOFI_HOME` rather than letting it default to the service user's home: a system service's
`$HOME` is often `/` or unset, and `paths.data_dir()` would then fall back to `./data` inside
`/opt/lofi`, mixing state with code on the next `git pull`.

Keep the token out of the unit file if the unit is world-readable — `EnvironmentFile=/etc/lofi.env`
with `chmod 600` on that file, containing `LOFI_TOKEN=...`, is cleaner than `Environment=`.

**A plain shell** is fine for trying it out:

```bash
source .venv/bin/activate
python3 bot.py                     # Ctrl-C stops it cleanly (players disconnect, sessions close)
python3 bot.py --log-level DEBUG --logfile
tail -f "$(python3 -c 'import paths; print(paths.LOG_PATH)')"
```

---

## Docker

The image needs ffmpeg and libopus inside it, and the data directory as a volume:

```dockerfile
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV LOFI_HOME=/data
VOLUME /data
EXPOSE 8790
CMD ["python", "bot.py"]
```

```bash
docker build -t lofi .
docker run -d --name lofi --restart unless-stopped \
  -e LOFI_TOKEN="$LOFI_TOKEN" \
  -e LOFI_DASHBOARD_TOKEN="$LOFI_DASHBOARD_TOKEN" \
  -p 127.0.0.1:8790:8790 \
  -v lofi-data:/data \
  -v "$PWD/music:/app/music:ro" \
  lofi
```

Notes that matter in a container:

- **Bind the dashboard to `127.0.0.1` inside the container** and publish it on the host loopback, as
  above, or put a reverse proxy in front. The dashboard's Host allowlist treats loopback as always
  allowed and everything else as opt-in; `-p 8790:8790` without the `127.0.0.1:` prefix exposes it to
  the network with the login key crossing it in clear text.
- **Pass the token and the dashboard key as environment variables**, not in a baked-in
  `config.json`. `LOFI_TOKEN` and `LOFI_DASHBOARD_TOKEN` both override the file.
- **Mount `music/`** if you use the library station. `:ro` is fine — the bot only reads it.
- **The volume holds your history.** Deleting the container without the volume loses `lofi.db`.
- If the container is behind a proxy that terminates TLS, set `dashboard_public_url` to the
  `https://` URL visitors use, so the dashboard marks its session cookie `Secure` and builds correct
  links. See [SETUP.md](SETUP.md#reaching-the-dashboard-from-another-machine).

---

## Running it alongside another bot

Lofi's dashboard follows the same design as
[Sentinel](https://github.com/mob5824m-wq/Sentinel)'s, and the two are meant to be able to share a
machine. Everything they touch is namespaced:

| | Sentinel | Lofi |
| --- | --- | --- |
| dashboard port | `8765` | **`8790`** |
| session cookie | `sentinel_dashboard_session` | `lofi_dashboard_session` |
| state directory | `~/.local/state/sentinel` | `~/.local/state/lofi` |
| database | `punishments.db` | `lofi.db` |
| log file | `bot.log` | `lofi.log` |
| environment variables | `SENTINEL_*` | `LOFI_*` |
| slash commands | `/manage …` | `/lofi …` |

So a default install of both comes up together: Sentinel on <http://127.0.0.1:8765>, Lofi on
<http://127.0.0.1:8790>, each with its own state directory, database, log and login key. Nothing has
to be configured first, and `tests/test_coexistence.py` fails if any row of that table drifts.

Two details worth understanding rather than memorising:

- **Cookies ignore ports.** A browser keeps one cookie jar per host, so both dashboards on
  `127.0.0.1` see each other's cookies. That is harmless *because* the names differ —
  `lofi_dashboard_session` is never read by Sentinel and vice versa. If you ever rename one, rename
  it to something unique.
- **Ports are the only real contention.** If 8790 is taken by something else on your machine, set
  `LOFI_DASHBOARD_PORT` or `dashboard_port`; nothing else needs to change.

**systemd** — one unit each, one user each, one state directory each:

```bash
sudo install -d -o lofi -g lofi /var/lib/lofi          # lofi.service uses LOFI_HOME=/var/lib/lofi
sudo install -d -o sentinel -g sentinel /var/lib/sentinel
systemctl enable --now sentinel lofi                    # either order; neither waits for the other
ss -ltnp | grep -E '8765|8790'                          # both listening, one process each
```

Do not share a system user between the two: the config files hold different credentials, and
`0600` only means something if the owners differ.

**Docker** — separate container, volume and published port:

```bash
docker run -d --name sentinel -p 127.0.0.1:8765:8765 -v sentinel-data:/data sentinel
docker run -d --name lofi     -p 127.0.0.1:8790:8790 -v lofi-data:/data     lofi
```

Each image keeps its own default port internally, so the published ports do not have to match the
container ports — but leaving them equal makes the proxy config easier to read.

**One reverse proxy, two dashboards.** They both serve their UI at `/`, so they need separate
`server` blocks — subdomains are the clean answer:

```nginx
server {                                  # sentinel.example.com -> 127.0.0.1:8765
    listen 443 ssl http2;
    server_name sentinel.example.com;
    # … certificates …
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
server {                                  # lofi.example.com -> 127.0.0.1:8790
    listen 443 ssl http2;
    server_name lofi.example.com;
    # … certificates …
    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then each bot's config names its own host, and each dashboard's Host allowlist stays meaningful:

```json
{ "dashboard_public_url": "https://lofi.example.com",
  "dashboard_allowed_hosts": ["lofi.example.com"],
  "dashboard_trusted_proxies": ["127.0.0.1"],
  "dashboard_secure_cookie": true }
```

Path prefixes (`/lofi/…`) do not work without patching the UI: the HTML requests its API at absolute
paths like `/api/overview`, so a prefix would send them to the other dashboard. Subdomains or
separate ports.

**Two copies of Lofi** on one machine (a test bot and a live one, say) need both a port and a home:

```bash
LOFI_HOME=/var/lib/lofi-staging LOFI_DASHBOARD_PORT=8791 LOFI_TOKEN=… python3 bot.py
```

Same code, separate state, separate login key, separate database. Slash commands are per Discord
application, so the two copies must be two applications — one token cannot be in two places.

## Updating

```bash
cd Discord-lofi
git pull
source .venv/bin/activate
pip install -U -r requirements.txt
python3 bot.py --check
sudo systemctl restart lofi        # or however you run it
```

`yt-dlp` is the dependency that most often needs updating on its own — YouTube changes its pages and
an old extractor starts failing with HTTP 403:

```bash
pip install -U yt-dlp
```

Your config and history survive an update: they are in the data directory, not in the checkout. The
SQLite schema carries a `schema_version` row and migrates forward on open, so a database written by
an older version is upgraded in place. If you ever want to start the history over, stop the bot and
move `lofi.db` aside — it is recreated on the next start.

---

## Removing it

```bash
sudo systemctl disable --now lofi && sudo rm /etc/systemd/system/lofi.service
rm -rf /opt/lofi                       # the code
rm -rf ~/.local/state/lofi             # the state: config.json, lofi.db, cache/
sudo userdel lofi
```

Then **revoke the token** in the Discord developer portal (Bot → Reset Token) if the machine is
being handed to someone else or destroyed: a token in a deleted directory is still a valid token
until it is reset.
