# Setting Lofi up

The software is installed ([INSTALL.md](INSTALL.md)). This is the part that happens in the Discord
developer portal and in your server: creating the application, inviting it with the right
permissions, and getting sound out of a voice channel.

- [1. Create the application](#1-create-the-application)
- [2. Intents: none of the privileged ones](#2-intents-none-of-the-privileged-ones)
- [3. Invite the bot](#3-invite-the-bot)
- [4. About Priority Speaker](#4-about-priority-speaker)
- [5. The voice channel status line](#5-the-voice-channel-status-line)
- [6. Start it and publish the commands](#6-start-it-and-publish-the-commands)
- [7. First five minutes in your server](#7-first-five-minutes-in-your-server)
- [8. Per-server settings and how they resolve](#8-per-server-settings-and-how-they-resolve)
- [9. The dashboard](#9-the-dashboard)
- [Reaching the dashboard from another machine](#reaching-the-dashboard-from-another-machine)
- [10. More than one server](#10-more-than-one-server)
- [Rotating credentials](#rotating-credentials)

---

## 1. Create the application

At <https://discord.com/developers/applications>:

1. **New Application** → give it a name ("Lofi" is fine; this is the name Discord shows).
2. **Bot** tab → **Reset Token** → copy it. This is the only time Discord shows it.
3. Leave **Public Bot** on if you want other people to be able to invite it, off if this is only
   for your servers.
4. You do **not** need OAuth2 redirect URIs, and you do not need to enable anything under
   "Privileged Gateway Intents" — see the next section.

Put the token where the bot will find it, in one of three ways:

```bash
# let the first run ask for it: one prompt, then it stores the answer and starts
lofi                       # "Bot token: " → paste → Enter

# a file (gitignored, written 0600)
cp config.example.json config.json
$EDITOR config.json        # "bot_token": "…"

# or the environment, which overrides the file and never touches disk
export LOFI_TOKEN="…"
```

The prompt is the whole first-run setup: it asks for the token and nothing else, because that is
the only thing Lofi cannot work out for itself. Everything else has a default, a fallback, or
`lofi --check` to name the fix. It is skipped when there is nobody to answer it - systemd, Docker
and pipes have no keyboard - and those runs are told what to set instead. The installers ask the
same question: the Debian package writes it to `/etc/lofi/lofi.env`, the macOS package to
`~/Library/Application Support/lofi/config.json`, and the Windows setup to `LOFI_TOKEN` in your
user environment.

A token is a credential that can act as your bot. Do not paste it into chat, an issue, a screenshot
or a commit. If it leaks: Bot → **Reset Token**, and every running copy stops working immediately.

## 2. Intents: none of the privileged ones

The bot asks for `discord.Intents.default()` plus `voice_states` and `guilds`. Both are
non-privileged, so **nothing needs enabling in the portal**, and the bot will not be flagged as
requiring verification at 100 servers.

`voice_states` is what lets it notice that it was moved to another channel, that the channel
emptied (idle disconnect), or that someone dragged it out. `guilds` is what makes the server list,
the channel pickers and the dashboard's per-server page work.

It deliberately does **not** ask for `members`, `presences` or `message_content`:

- No member list is needed — permissions are checked against the *calling* member, which arrives
  with the interaction.
- No message content is needed — there are no prefix commands. Everything is a slash command, and
  slash commands arrive as structured data.

If you turn one of those three on in the portal anyway, Discord starts rejecting the connection and
the bot prints why:

```
Discord asked for a privileged intent. Lofi needs none: use
discord.Intents.default() and disable Members/Presence/Message Content
in the developer portal.
```

The fix is to go back to the Bot tab and switch them off. The process exits with status 2.

## 3. Invite the bot

**OAuth2 → URL Generator**: scopes `bot` and `applications.commands`, then the permissions below.
Or use the integer directly:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=281474979941376&scope=bot%20applications.commands
```

Replace `YOUR_APP_ID` with the **Application ID** from the General Information tab (not the token).

| Permission | Needed for | What breaks without it |
| --- | --- | --- |
| **View Channels** | seeing the channels at all | the bot cannot join or list anything |
| **Connect** | joining a voice channel | `/lofi play` fails with a permission error before any audio |
| **Speak** | sending audio | it joins, sits silent, and `/lofi status` names this permission as missing |
| **Set Voice Channel Status** | the status line under the channel name | the now-playing text is skipped; audio is unaffected |
| **Send Messages** | replies to slash commands | ephemeral-only fallback; some responses cannot be shown |
| **Embed Links** | the now-playing embed | messages fall back to plain text |
| **Read Message History** | posting in a channel with a slow history requirement | now-playing announcements can fail in some channels |

`applications.commands` is the scope that makes slash commands appear. Inviting with `bot` alone
gives you a member that cannot be talked to.

A **voice-only** invite, if you would rather not let it post anything:

```
permissions=281474979857408     # View Channels, Connect, Speak, Set Voice Channel Status
```

Slash command *replies* are ephemeral for transport commands, so this still works — you just lose
the now-playing embeds. `/lofi setup announce:false` and `/lofi setup status_line:false` make that
combination tidy.

**Manage Server** is not required by the bot. It is what *your members* need to run the
configuration commands (`/lofi setup …`, `/lofi station …`); the transport commands
(`/lofi play`, `pause`, `skip`, …) are open to anyone in the same voice channel, or to a role you
name with `/lofi setup dj_role:`.

Permissions can be granted per channel instead of server-wide, which is the right thing to do if
you only want music in one study room: give the bot's role Connect + Speak in that channel and
nothing elsewhere.

## 4. About Priority Speaker

If you granted the bot **Priority Speaker** in your server — a reasonable thing to try, and this
project was built for a server where it is granted — it has **no effect**, and there is nothing the
bot could do about it:

- Priority Speaker is a **desktop-app feature**. It ducks *other people's* microphones while the
  priority speaker talks.
- It only activates with **Push-to-Talk** input mode, through the user's own "Push-to-Talk
  (Priority)" keybind. A bot has no input mode and no keybinds, so there is nothing to activate.
- There is **no field for it in the bot API**. `VoiceChannel.edit()` accepts `status` (the channel
  status line) but nothing that says "speak with priority"; the gateway's voice-state payload has
  `self_mute`, `self_deaf`, `suppress` and `request_to_speak_timestamp` — no priority flag.
- Discord deliberately does not let bots duck other users. A bot that could silence a room would be
  a griefing tool.

So the permission is inert. The bot's own permission audit says exactly that rather than reporting
a green tick for something that does nothing: when `/lofi status`, the dashboard's Servers page or
`/api/guilds/{id}` inspects a server where `priority_speaker` is granted, the entry comes back as
`granted: true, state: "no-effect"` with the reason above. You can leave it granted (harmless) or
remove it to avoid confusing the next person who reads the role's permissions.

**What actually works instead**, if the goal is "music should not fight with people talking":

1. **Volume.** `/lofi volume 35` (0–150, default 60). Discord mixes the two locally, and lofi at
   30–45% sits behind conversation without disappearing. This is the real answer for a study room.
2. **Per-server defaults.** `/lofi setup volume 35` makes it stick, so nobody has to remember.
3. **A DJ role.** `/lofi setup dj_role:@Music` restricts transport control to people who can be
   trusted to duck the music when someone starts talking — a human doing what Priority Speaker
   would have automated.
4. **Separate channels.** A "music" channel and a "talk" channel, with the bot only permitted in the
   first. Crude, effective, and the only way to guarantee voice activity is never ducked by music.

## 5. The voice channel status line

Discord lets a bot write up to 500 characters of text under a voice channel's name — the same slot a
human's "Playing …" activity fills. Lofi uses it for the current track:

```
🎧 Cassette Sun in the attic · Studio Lofi
```

To make it work:

- The bot needs **Set Voice Channel Status** (in the invite integer above, or granted per channel).
- `/lofi setup status_line:true` — on by default; turn it off if your server dislikes channel noise.
- It is a **write-only** field: Discord does not return the current status in any API response, so
  the bot remembers what it last wrote and will not rewrite an identical string. Writes are also
  throttled to one per 8 seconds minimum, because a queue that advances quickly would otherwise hit
  the rate limit and start dropping the *audio* tasks that share the connection.

Two behaviours worth knowing:

- When playback stops, the status is **cleared** — an explicit `None` write, not a stale track name
  left under the channel for hours. (Clearing used to be skipped by the throttle; it is not any
  more, because a stale "now playing" is worse than one extra request.)
- If the bot lacks the permission, it logs once and carries on. The status line is decoration;
  losing it must never stop the music.

## 6. Start it and publish the commands

```bash
source .venv/bin/activate
python3 bot.py
```

On a clean start:

```
Dashboard listening at http://127.0.0.1:8790
Connected as Lofi (id 123456789012345678) in 3 server(s).
Dashboard: http://127.0.0.1:8790/
Dashboard key: run 'python3 bot.py --dashboard-token' to print it.
Autostart: My Server: now playing in #Study Room
```

The dashboard line comes first because the server binds during `setup_hook`, before Discord fires
`ready` — so if the token is fine but the port is taken you see the bind failure before anything
else. The `Autostart:` line only appears for servers with `autostart` configured, and each one says
what happened (`now playing in #channel`, `configured voice channel no longer exists`, or the error).

If PyNaCl, libopus or ffmpeg is missing you get one warning instead of the voice lines:

```
Voice support is not ready: one or more of PyNaCl, libopus or ffmpeg is missing. Run
'python3 bot.py --check' for the fix. The dashboard still works.
```

and the bot still starts — the dashboard, the configuration commands and the offline diagnostics all
work without audio, which is more useful than refusing to boot. A voice bot that cannot play audio
otherwise fails in the worst possible way: it joins the channel, says nothing, and logs nothing.

**Slash commands are published on start** (`"sync_commands_on_start": true`), and published *twice*:
once globally, and once per server the bot is in. That matters because the two propagate at very
different speeds — a global registration can take **up to an hour** to show up in a client's cache
the first time, while a guild-scoped sync applies instantly. Doing both is why a fresh install has
working commands in your server right away.

To publish again without restarting:

```bash
python3 bot.py --sync-commands     # CLI: sync, print the result, exit
```

or the dashboard's **Commands** page → *Sync slash commands* (also in the top bar), which calls the
same code path and reports `{global: 30, guilds: {"…": 30}}`.

If commands never appear at all, the usual causes, in order:

1. The bot was invited **without** the `applications.commands` scope → re-invite with it (the bot
   stays in the server; re-running the invite URL just updates scopes and permissions).
2. Your client is showing a cached command list → reload Discord (Ctrl-R / Cmd-R).
3. `sync_commands_on_start` is `false` and nobody has run `--sync-commands` → the startup log has no
   sync line at all, which is how you tell this apart from a scope problem.
4. The sync failed and was logged: `Global command sync failed: …`. A 403 there means the
   application is not in that server or the scope is missing; a 429 means you have synced too often
   (Discord rate-limits command publishing).

## 7. First five minutes in your server

Join a voice channel, then:

```
/lofi status                      # what it sees: connection, permissions, settings, last error
/lofi stations                    # everything it can play, and which need the network
/lofi play                        # join your channel, start the default station (lofi-girl)
/lofi play station:groove-salad   # a SomaFM stream — no YouTube, no yt-dlp resolution
/lofi studio mood:rainy           # render a track locally — works with no internet at all
/lofi volume 40                   # 0–150, applied immediately, not persisted across restarts
/lofi now                         # the current track, with artwork when there is any
/lofi upnext                      # the queue
/lofi skip  ·  /lofi pause  ·  /lofi resume  ·  /lofi stop
```

`/lofi status` is the diagnostic to trust. It reports the permission audit for the channel the bot
is in — including the `no-effect` verdict on Priority Speaker — the resolved settings, whether opus
and ffmpeg are usable, the watchdog's restart count, and the last playback error. If sound is
missing, read that before reading logs. [TROUBLESHOOTING.md](TROUBLESHOOTING.md) is organised around
what it tells you.

Then make it behave the way your server wants:

```
/lofi setup station:groove-salad          # what /lofi play starts with no argument
/lofi setup voice_channel:#Study Room     # the channel autostart joins
/lofi setup text_channel:#music           # where now-playing embeds are posted
/lofi setup announce:true                 # post those embeds at all
/lofi setup volume:35                     # the persistent default
/lofi setup idle:15                       # minutes of an empty channel before it leaves
/lofi setup dj_role:@Music                # restrict transport control to a role
/lofi setup autostart:true                # join and play whenever the bot starts
/lofi setup always_on:true                # never leave an empty channel (radio behaviour)
/lofi setup reset                         # back to the defaults
```

`autostart` without `always_on` is the study-room setup: the bot is there when people arrive, and
leaves after `idle` minutes of silence. `always_on` is the 24/7 radio setup: it stays connected to
an empty channel and keeps playing. Both are off by default, so a fresh install never joins a
channel uninvited.

## 8. Per-server settings and how they resolve

Every setting resolves in a fixed order, highest first:

1. **A live player change** — `/lofi volume 90` while connected. Deliberately *not* persisted: it is
   "turn it up for this session", and a bot that remembered 150% forever is a bot someone will
   complain about.
2. **This server's entry** in `config.json` → `"guilds": { "<server id>": { … } }`.
3. **The top-level default** in `config.json` → `"default_volume"`, `"default_station"`,
   `"idle_disconnect_minutes"`, `"stay_connected"`, `"announce_now_playing"`, `"channel_status"`.
4. **The built-in default** — volume 60, station `lofi-girl`, idle 10 minutes, everything else off.

`/lofi setup …` writes level 2. The dashboard writes the same place. Editing `config.json` by hand
works too and is picked up on restart; a running bot re-reads the file only when *it* writes it, so
hand-edits while it is running are overwritten by the next settings change — stop the bot first, or
use the dashboard.

Values are clamped, not rejected: `volume: 4000` becomes 150, a negative idle becomes 0, an unknown
station id falls back to the default rather than failing to play.

## 9. The dashboard

It starts with the bot on `127.0.0.1:8790`. **Print your key:**

```bash
python3 bot.py --dashboard-token
```

It was generated once (`secrets.token_urlsafe(36)`, 48 characters), stored in `config.json` with
mode `0600`, and never printed to the log — a key in a journald stream is a key in every backup of
that stream. Override it with the `LOFI_DASHBOARD_TOKEN` environment variable (which is how a
container supplies it without the key touching disk), and rotate it from the Settings page — a
rotation invalidates every session, as it should. Keys shorter than 32 characters are refused rather
than used, so a weak value fails loudly at startup.

Open <http://127.0.0.1:8790>, paste the key, and you are in for 8 hours (sliding — activity extends
it). From there:

| Page | Set up |
| --- | --- |
| **Overview** | nothing; it is the health read. Servers, listeners, listening minutes, top tracks, activity feed |
| **Player** | volume and transport for the selected server, and the player's health: restarts, stall detection, last error |
| **Servers** | per-server settings (same keys as `/lofi setup`), the permission audit, that server's stats |
| **Stations** | add a custom station (a YouTube link, an HTTP/S stream, a folder, or `generative`), **test** it before saving, set the default, remove one. Shows the library folder path, whether it exists and how many playable files are in it |
| **History** | finished sessions with duration, peak listeners and why each ended |
| **Commands** | the live command tree — signatures and who may run each one |
| **Settings** | network binding, Host allowlist, trusted proxies, TLS, key rotation, and the warnings that say what to fix |

Two things to do on first login:

1. **Stations → Test** each built-in YouTube station. It resolves the link without playing it, so
   you find out on day one whether your IP is being asked to sign in, rather than during a study
   session.
2. **Settings → read the warnings.** They are generated from your actual binding and state the fix,
   not just the risk — "dashboard_host is 0.0.0.0 but dashboard_allowed_hosts is empty, so any Host
   header that is not a bare IP is answered with 400. Add the public name, e.g.
   yourname.duckdns.org."

### Trying the dashboard with no bot at all

```bash
python3 bot.py --demo
```

Three simulated servers that move: tracks advance, listeners drift, transport buttons work,
settings save. Every API response carries `demo: true` and the UI shows a banner, so it cannot be
mistaken for a live connection. It writes to a throwaway database, never to your real history.

## Reaching the dashboard from another machine

Bind loopback and put a TLS proxy in front. Do not expose `0.0.0.0:8790` over plain HTTP: the login
key crosses the network in clear text, and anyone who reads it once owns the bot's configuration.

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

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name lofi.example.com;
    ssl_certificate     /etc/letsencrypt/live/lofi.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lofi.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;          # must be the real name
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        client_max_body_size 64k;                           # the app caps bodies at 64 KiB anyway
    }
}
```

What each key does, and why:

- **`dashboard_allowed_hosts`** — every `Host` header that is *not* loopback and *not* listed is
  answered `400`. This is the DNS-rebinding guard: a page you visit in your browser cannot point a
  name at `127.0.0.1` and read the API using your session cookie. The hostname of
  `dashboard_public_url` is accepted implicitly, so setting only the public URL is enough.
- **`dashboard_trusted_proxies`** — the client IP is read from `X-Forwarded-For` **only** when the
  request comes from an address in this list. Without it the login throttle counts your proxy as one
  client (so one person's five typos lock out everybody), and with a wildcard trust anybody can
  forge the header to dodge the throttle. Put your proxy's address here and nowhere else.
- **`dashboard_secure_cookie`** — forced on automatically when the bot itself terminates TLS. Set it
  by hand when a proxy does. Getting this wrong is the classic "login succeeds and immediately
  bounces back": the browser refuses to store a `Secure` cookie over HTTP. The Settings page warns
  about exactly that combination.
- **`dashboard_public_url`** — used to build links and to decide whether the browser is on HTTPS
  (which is how `secure_cookie` gets forced). Include the scheme; a bare host produces a warning.
- **`dashboard_tls_cert` / `dashboard_tls_key`** — terminate TLS in the bot instead. Both required
  together; one without the other is a startup error, not a silent fallback to HTTP.

A tunnel is the low-effort version, and it is legitimate for one person:

```bash
ssh -N -L 8790:127.0.0.1:8790 you@your-server    # then browse http://127.0.0.1:8790
```

Loopback is always allowed by the Host check, so this needs no config change at all — and the key
travels inside SSH.

`--dashboard-allowed-hosts '*'` (or `"*"` in the config) accepts any Host header. It exists for a
throwaway local preview and logs a warning saying it disables DNS-rebinding protection. Do not leave
it on anything reachable.

### Logging in over SSH

The tunnel needs an SSH login on the server. A key is the better choice; a password works if you need one.

- **Key (recommended).** Add your public key to `~/.ssh/authorized_keys` on the server. `ssh-copy-id you@your-server` does this from Linux or macOS. Keys can't be brute-forced in practice, and you can remove one device's key without touching the others.
- **Password.** Use a long random password for that account, set with `passwd`. Password logins are brute-force targets on any port reachable from the internet, so if you keep them on, run fail2ban as well.

Put the server settings in a drop-in file, so changes to the main `sshd_config` can't overwrite them:

```
# /etc/ssh/sshd_config.d/10-lofi.conf
# Key login only. For a password login, set PasswordAuthentication yes.
PubkeyAuthentication yes
PasswordAuthentication no
PermitEmptyPasswords no
PermitRootLogin prohibit-password
AllowTcpForwarding yes
```

OpenSSH uses the first value it reads for each setting, so the `Include /etc/ssh/sshd_config.d/*.conf` line must sit above any conflicting setting in `/etc/ssh/sshd_config`. If that file has no Include line, put the settings in it directly. Then check and reload:

```
sudo sshd -t                         # no output means the syntax is fine
sudo sshd -T | grep -E '^(permitrootlogin|permitemptypasswords|passwordauthentication|allowtcpforwarding) '
sudo systemctl reload ssh            # the service is called "sshd" on RHEL-family systems
```

Keep your current session open until a new login works from a second terminal. A bad sshd config can lock you out, and that open session is your way back in.

What each line does, and why:

- **`PermitRootLogin prohibit-password`**: root can still log in with a key, but never with a password. Root is the first username most password attacks try.
- **`PermitEmptyPasswords no`**: an account with an empty password can't log in over SSH. Some images ship with this on, which lets anyone who can reach the port into any account that has no password set.
- **`AllowTcpForwarding yes`**: the tunnel needs it. Hardened images sometimes turn it off.
- **`PasswordAuthentication no`**: key login only. Set it to `yes` only for the password option above.

Using the tunnel:

```
ssh -N -o ServerAliveInterval=60 -L 8790:127.0.0.1:8790 you@your-server
```

Then browse to <http://127.0.0.1:8790> on your machine.

- Don't add `-g`. It makes the forwarded port reachable from your whole network, not just your machine.
- Get the dashboard key in a second SSH session with `python3 bot.py --dashboard-token`, run from the install directory (use the venv's Python if you made one).
- On Windows with PuTTY, the tunnel is under Connection → SSH → Tunnels: source port `8790`, destination `127.0.0.1:8790`.

## 10. More than one server

One process handles every server it is in, with a player per guild — independent stations, volumes,
queues, watchdogs and sessions. Nothing is global except the defaults in `config.json` and the
single dashboard key.

The dashboard's server picker (top right) switches which guild every page talks about; the Overview
aggregates across all of them. Per-server state lives under `"guilds": { "<id>": … }`, so a config
that survived three servers is still readable.

Scaling limits worth knowing: each connected guild holds one ffmpeg subprocess and one Opus encoder
thread. A handful of servers is nothing; a hundred simultaneous voice connections is a CPU and
upstream-bandwidth problem (roughly 100 kbit/s per connection, plus whatever the source stream
costs), and YouTube stations from one IP will start being rate-limited long before then.

## Rotating credentials

| Credential | How | Effect |
| --- | --- | --- |
| **Bot token** | Developer portal → Bot → Reset Token, then update `config.json` / `LOFI_TOKEN` and restart | every running copy disconnects immediately |
| **Dashboard key** | Settings page → regenerate, or set `LOFI_DASHBOARD_TOKEN` and restart | every session is invalidated; everybody logs in again |
| **Invite permissions** | re-run the invite URL with a new `permissions=` integer | updates in place, no re-join needed |
