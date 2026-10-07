# Troubleshooting

Everything here was written against a real failure mode in this codebase, and the messages quoted are
the ones the bot actually prints — so you can search this page for the text you saw.

- [Read this first](#read-this-first)
- [No sound](#no-sound)
- [YouTube stations](#youtube-stations)
- [Streams (SomaFM and friends)](#streams-somafm-and-friends)
- [Your own files](#your-own-files)
- [Studio Lofi](#studio-lofi)
- [Stalls, restarts and the watchdog](#stalls-restarts-and-the-watchdog)
- [Voice connections](#voice-connections)
- [Volume](#volume)
- [Commands](#commands)
- [Now-playing messages and the channel status line](#now-playing-messages-and-the-channel-status-line)
- [The dashboard](#the-dashboard)
- [History and the database](#history-and-the-database)
- [Starting, stopping and exit codes](#starting-stopping-and-exit-codes)
- [Filing a bug report](#filing-a-bug-report)

---

## Read this first

Three tools, in the order that finds problems fastest:

```bash
python3 bot.py --check     # the install: python, ffmpeg, opus, yt-dlp, numpy, token, paths, binding
/lofi status               # one server: connection, the permission audit, resolved settings, last error
python3 bot.py --log-level DEBUG --logfile      # then: tail the file it names
```

`--check` never connects to Discord and is safe to run on a machine with no network. It exits **1**
if any of Python, ffmpeg, libopus or the bot token is missing, and prints the exact command to fix
each one.

`/lofi status` is the one to trust for "it joined but nothing happened", because it reports what the
bot can actually see *in that channel*, including permissions granted server-wide but denied by a
channel override — the single most common cause of a silent bot.

The dashboard's **Player** page shows the same information plus the watchdog's restart counter and
the stall detector, and its **Servers** page shows the permission audit with a reason for every row.

## No sound

**It joins the channel, sits silent, and `/lofi status` says a permission is missing.**
`Speak` is required to send audio. It can be granted server-wide and still be denied by a channel
override or by a role the bot has — Discord resolves the *effective* permission, and that is what
`permissions.check()` reads. Grant Speak in that specific channel, or remove the deny.

**It joins, and nothing at all happens — no error, no log line.**
This is what a missing `ffmpeg` or `libopus` looks like from inside a voice channel, which is why
`--check` exists. discord.py raises when you ask it to play audio without opus; the player catches
that, logs it, and reports it through `/lofi status` and the dashboard rather than dying:

```bash
sudo apt install ffmpeg libopus0     # Linux
brew install ffmpeg opus             # macOS
pip install imageio-ffmpeg           # anywhere: a bundled ffmpeg binary Lofi will find
```

The startup warning names it:

```
Voice support is not ready: opus or ffmpeg is missing. Run 'python3 bot.py --check' for
the fix. The dashboard still works.
```

**Sound works for one person and not another.** That is a Discord client setting, not the bot:
per-channel *user* volume (right-click the bot in the member list → move the slider), "Stream
Volume" attenuation, or an output device that is not the one you think. Ask them to check the
bot's volume slider first — it is reset to 100% by nothing and remembered per client.

**Sound worked and then stopped mid-session.** Jump to [stalls](#stalls-restarts-and-the-watchdog).

## YouTube stations

Every YouTube failure surfaces as a message written for a person, not a stack trace. If you see one
of these in `/lofi status`, the dashboard Player page, or the log:

| Message | What it means | Fix |
| --- | --- | --- |
| *YouTube is asking this connection to sign in, so the stream could not be resolved.* | YouTube has flagged this IP as a bot. Common on datacentre ranges and shared VPS IPs | Switch to a **stream** station (`groove-salad`, `secret-agent`), **My Library**, or **Studio Lofi** — none of them touch YouTube. Or run the bot from a residential connection |
| *YouTube refused the request (HTTP 403).* | The extractor is out of date; YouTube changed something | `pip install -U yt-dlp`, then `/lofi skip` |
| *That video is no longer available, so the station's link has gone stale.* | The 24/7 broadcast was taken down or made private | Dashboard → Stations → replace the URL, or `/lofi station add` a new one |
| *YouTube could not be reached from this machine.* | DNS, a firewall, or no outbound HTTPS | Check egress; the offline stations work regardless |
| *yt-dlp is not installed.* | Exactly that | `pip install -U yt-dlp` |

Two behaviours worth knowing:

- **Resolutions are cached for 45 minutes** (`RESOLUTION_TTL_SECONDS = 2700`). A cached direct URL
  is reused across reconnects so a flaky channel does not pay for a fresh extraction each time — but
  a cache that outlived the URL's own expiry would hand ffmpeg a dead link, hence the TTL. If you
  fixed something on YouTube's side and want it re-resolved now, restart the bot or `/lofi skip`
  twice.
- **Resolution happens off the event loop**, in a thread. If it hangs, the watchdog notices the
  silence and restarts the track rather than freezing the whole bot.

## Streams (SomaFM and friends)

`groove-salad` and `secret-agent` are plain HTTP streams, so they have no extractor and no sign-in
wall. When one fails:

- *Station … has no stream URL.* → the station record lost its `url`; re-add it from the dashboard.
- It connects, then the watchdog restarts it every ~20 s → the stream is up but sending nothing
  (an encoder that died upstream). Check the station's own page; there is nothing to fix locally.
- TLS errors on an `https://` stream behind a corporate proxy → the proxy is intercepting. Either
  trust its CA in the system store (ffmpeg uses the system store) or use an `http://` mount point.

Streams are **live**: they have no duration, no seeking and no meaningful "next track". The player
marks them `is_live`, the dashboard hides the progress bar for them, and `/lofi skip` on a live
station reconnects to the same broadcast rather than advancing — which is why the title does not
change when you skip one.

## Your own files

**"You have no library folder"** and **"the folder is there but empty"** are different messages
because they are different problems:

- *No folder* → `music/` does not exist next to `bot.py`. Create it, or point `LOFI_MUSIC` at an
  existing collection. The bot deliberately does **not** create it for you: silently inventing an
  empty folder makes "my music is missing" look like "my music is empty".
- *Folder exists, 0 tracks* → nothing in it has a playable extension. Recognised:
  `.mp3 .flac .ogg .opus .wav .m4a .aac .wma .aiff`. Subdirectories are scanned; hidden files and
  anything else (`.txt`, `.m3u`, `.cue`, partial downloads) are ignored.

The dashboard's Stations page shows the resolved path, whether it exists, the file count and total
size, so "which folder is it even reading" is answered without a shell.

A file that fails to decode (a corrupt download, an unusual container) is skipped and logged, not
fatal: the player advances to the next one. If *every* file fails, suspect ffmpeg rather than the
files — `ffmpeg -i your-file.mp3 -f null -` will tell you in one command.

## Studio Lofi

- *Studio Lofi needs numpy.* → `pip install numpy`. The dashboard's Stations page reports
  `generativeAvailable: false` with the reason, and `/lofi stations` marks it unavailable, so this is
  never a mystery at 2 a.m.
- **The first render of a mood takes ~3 s**, and every later one is instant: rendered WAVs are cached
  under `<data dir>/cache/gen-*.wav` with a content-hash filename, so the same recipe + seed is
  rendered once per machine. The cache is pruned to the newest 40 files.
- *The generated track could not be written to disk.* → the data directory is not writable. Check
  `LOFI_HOME` / the `config file` and `data directory` rows of `--check`.
- It sounds quiet or the peaks look wrong → they are not: the renderer targets a peak of 0.84 and the
  envelope intentionally peaks below 1.0 so nothing clips when the volume is raised to 150%. Compare
  against `/lofi volume`, not against a mastered track.

## Stalls, restarts and the watchdog

Each player runs a watchdog every 5 seconds (`WATCHDOG_INTERVAL`) that checks four things, in order:

1. **The voice connection died** (Discord dropped it, or someone moved/kicked the bot) → reconnect,
   or stop with reason `disconnect` / `kicked` / `moved`.
2. **The channel has been empty** for `idle_minutes` → stop with reason `idle`
   (*"left because the channel was empty"*), unless `always_on` is set.
3. **The stream stalled**: audio is "playing" but no PCM has arrived for 20 s (`STALL_SECONDS`).
   discord.py only tells you when a source *ends* or raises — a 24/7 broadcast whose encoder died
   keeps the connection open and sends nothing, forever. The watchdog logs
   `Stream stalled in guild … (no audio for Ns); restarting.`, records a `stall` event and advances.
4. **Nothing is playing and nothing is queued** → idle out or stop.

Restarts back off and are capped: after `MAX_RESTART_ATTEMPTS = 6` consecutive failures the player
gives up with reason `error` (*"playback failed repeatedly"*) rather than hammering the source and
Discord's rate limits forever. The counter resets on a successful track. `/lofi status`, the Player
page and the History page all show it, and every give-up is written to the event log with the
underlying message — so "it stopped an hour ago and I don't know why" is answerable from the
dashboard's History page without reading logs.

## Voice connections

**It never joins, and the log shows a timeout.** Voice needs **outbound UDP in the 50000–65535
range** to Discord's voice servers, in addition to outbound 443. Locked-down networks (corporate,
some VPS providers, dorm networks) filter exactly that. Symptoms: gateway connects fine, the bot
appears online, `/lofi play` hangs and then fails. Test from the bot's host:

```bash
nc -u -z -w 3 voice.discord.media 50000 || echo "UDP egress looks filtered"
```

**It joins and immediately leaves.** Two causes: `idle_minutes` with an empty channel (you were not
in it yet), or a `moved`/`kicked` event because a moderator dragged it. History shows which —
`endedReason` is one of `stop`, `idle`, `disconnect`, `moved`, `kicked`, `error`, `shutdown`,
`reload`, each with a human sentence.

**"Could not move to #channel (…); reconnecting."** Someone moved the bot while it was playing. It
reconnects to the new channel and resumes; this is logged as a warning because it is unusual, not
because it is broken.

**It works from your laptop but not the server.** Almost always the UDP range above, or an old
ffmpeg in the server's distribution that cannot open the source's container. `ffmpeg -version` on
both, and prefer a current build.

## Volume

- `/lofi volume` accepts **0–150** and applies immediately. It is deliberately **not persisted**: it
  means "turn it up for this session". Use `/lofi setup volume:` for a value that survives restarts.
- Values are clamped, not rejected: `999` becomes 150, `-5` becomes 0. The dashboard does the same,
  so a bad input cannot put the player into a state the command could not.
- Above ~130% quiet sources distort — the transformer multiplies PCM samples and clipping is
  clipping. If you need it louder than that, the source is too quiet; raise the *station* (a
  different broadcast) rather than the gain.
- Music too quiet relative to voices is normal and intended: Discord mixes them client-side. 30–45%
  is the study-room range. See [SETUP.md → About Priority Speaker](SETUP.md#4-about-priority-speaker)
  for why the Priority Speaker permission cannot do this for you.

## Commands

**Slash commands do not appear.**

1. The bot was invited without the `applications.commands` scope → re-run the invite URL with it
   (see [SETUP.md](SETUP.md#3-invite-the-bot)). The bot does not need to leave the server.
2. Client cache → Ctrl-R / Cmd-R.
3. Global propagation takes up to an hour the first time. The bot also syncs per-guild on start, so
   commands in servers it is already in should be instant; if the startup log has no sync line,
   `sync_commands_on_start` is off. Force it: `python3 bot.py --sync-commands`.
4. `Global command sync failed: …` in the log → a 403 means scope/permission, a 429 means you have
   published too often (Discord rate-limits command registration; wait).

**A command was refused.** These are this bot's own rules, checked on every invocation, and the
message tells you which one:

| Message | Rule | Fix |
| --- | --- | --- |
| *That command has to be used inside a server.* | used in a DM | run it in a server |
| *Join a voice channel first, then run that command again.* | nothing is playing, and you are not in a voice channel | join one, or pass `channel:` to `/lofi play` |
| *You have to be in the same voice channel as the bot to control what it plays.* | a transport command from another channel | join the bot's channel, or `/lofi leave` then `/lofi play channel:#yours` |
| *Only members with **DJ** (or Manage Server) can control playback here.* | a DJ role is configured and you do not have it | get the role, or have an admin clear it with `/lofi setup dj_role:` |
| *That needs **Manage Server** (or Administrator).* | a `/lofi setup …` or `/lofi station …` command | real Discord permission, granted per server |

The transport rule is not a Discord permission and cannot be granted in the invite: it exists so one
person sitting in another channel cannot stop a study room's music. Setting a DJ role is the escape
hatch — with one configured, that role may control playback from anywhere, which is what you want for
a moderator who is not in the room.

Checks are re-run on every invocation rather than cached, so removing a DJ role or someone leaving
the channel takes effect immediately.

**A command did nothing at all.** If no response arrives, the interaction timed out — Discord gives
3 s to acknowledge, and the commands that touch the network defer first, so this means the work
*after* the defer failed or hung. The log has the exception. The usual cause is a first-time YouTube
resolution on a slow link; retrying works because the result is then cached for 45 minutes. There are
no cooldowns in this bot, so "wait a moment and try again" is not a thing it will tell you.

## Now-playing messages and the channel status line

**No embed is posted when the track changes.**

- `/lofi setup announce:false` → turn it on.
- No `text_channel` configured → the bot looks for a channel it can send in; if it cannot find one it
  posts nothing rather than spamming a random channel. Set one: `/lofi setup text_channel:#music`.
- Missing **Send Messages** / **Embed Links** in that channel → the post fails and is logged as
  `Now-playing post failed in guild …`; audio is unaffected.
- Threads and announcement channels are valid targets — an earlier version skipped them, which is why
  a news channel silently got nothing.

**The status line under the channel name does not appear.**

- Needs the **Set Voice Channel Status** permission. Without it the bot logs once
  (`no 'Set Voice Channel Status' permission; track status will not be shown under the channel name`)
  and never tries again for that guild, so it cannot waste a request per track.
- `/lofi setup status_line:false` disables it.
- Writes are throttled to one per 8 s (`STATUS_MIN_INTERVAL`) and identical text is not rewritten at
  all — Discord does not let you read the current status back, so the bot remembers what it last
  wrote. A queue of very short tracks therefore updates the status less often than it changes tracks.
  This is deliberate: the status line shares a rate limit with things that matter more.
- It is truncated to 90 characters with an ellipsis. Discord allows 500, but a 500-character string
  under a channel name is unreadable in the channel list.
- **Clearing** on stop is exempt from the throttle. A stale "now playing" left under a channel for
  hours is worse than one extra request, so `/lofi stop` always clears it.
- If Discord refuses the write, you get `Discord refused the voice channel status in guild …` and a
  `warning` event; the bot marks the feature unsupported for that guild and moves on.

## The dashboard

**The port will not open.** `Address already in use` means something else already has 8790: a second
copy of Lofi, a `--demo` you started earlier and forgot, or an unrelated service. Find it with
`ss -ltnp | grep 8790`, then either stop that process or move this one with
`LOFI_DASHBOARD_PORT=8791` / `"dashboard_port": 8791`.

Note that a *sibling bot* is not the usual suspect: Sentinel's dashboard — the one this is modelled
on — defaults to 8765, and every other namespace the two touch is separate (see
[INSTALL.md](INSTALL.md#running-it-alongside-another-bot)). If both dashboards are on 8765, you are
running an older Lofi; the default moved to 8790 for exactly this reason.

Either way the bot logs
`The dashboard could not bind 0.0.0.0:8790 (…). Another copy may already be …` and keeps running:
losing the dashboard must not take the music down with it.

**`400 {"error":"Unrecognized Host header."}`** The Host allowlist is doing its job (DNS-rebinding
protection). Add the name you are using to `dashboard_allowed_hosts`, or set `dashboard_public_url`
whose hostname is accepted implicitly. Loopback is always allowed. `*` accepts anything and logs a
warning saying it disables that protection — do not leave it on.

**`401 {"error":"Invalid dashboard key."}`** Wrong key. Print it: `python3 bot.py --dashboard-token`.
Note that `LOFI_DASHBOARD_TOKEN` overrides the stored key, so a container can be using a different
one from the file you are reading.

**Login appears to succeed, then bounces straight back to the login screen.** The session cookie is
marked `Secure` while your browser reaches the dashboard over plain HTTP, so the browser refuses to
store it. Either serve HTTPS or set `dashboard_secure_cookie: false`. The Settings page warns about
exactly this combination before it bites:

> dashboard_secure_cookie is on, but the browser reaches this dashboard over plain HTTP, so it will
> refuse to store the session cookie and every login will appear to succeed and then bounce back.

**`429 {"error":"Too many failed attempts. Wait a few minutes and try again."}`** Five failed logins
from one IP inside five minutes. Wait it out. Behind a proxy where every visitor shares one IP, set
`dashboard_trusted_proxies` to the proxy's address so the real client IP is read from
`X-Forwarded-For` — otherwise one person's typos lock out everybody. (Do not trust that header from
anywhere else: a client that can forge it can dodge the throttle.)

**`403 {"error":"Invalid CSRF token."}`** Every mutating request must carry an `X-CSRF-Token` header
matching the session (compared with `hmac.compare_digest`). In the shipped UI this is automatic; if
you are calling the API from a script, `POST /api/login`, keep the cookie, read `csrfToken` from
`GET /api/session`, and send it on every `POST`/`PUT`/`PATCH`/`DELETE`. `GET` requests are exempt.
A session older than 8 hours (sliding, so only 8 hours of *inactivity*) answers `401
Authentication required.` instead — log in again.

**`413` on a POST** Bodies are capped at 64 KiB (`MAX_BODY_BYTES`). This is a settings API, not an
upload endpoint; something is sending far more than a settings payload.

**`404` for a server that exists** The bot is not in that server, or the id is not a snowflake:
`{"error":"The bot is not connected to that server."}` / `{"error":"That is not a server id."}`.
Discord ids are serialised as **strings** everywhere in this API, because a 19-digit number exceeds
JavaScript's safe integer range and a JSON number would be silently rounded.

**`500` with a generic message** An unexpected provider error is turned into a generic JSON 500 on
purpose — internals do not belong in a response body. The real exception is in the log. Errors the
bot understands (`DashboardError`) keep their own status and message, so a 400/404/422 tells you what
to fix and a 500 means "look at the log".

**TLS will not start** `dashboard_tls_cert` and `dashboard_tls_key` must both be set; one without the
other is a startup error, not a silent fallback to HTTP. Under TLS the session cookie becomes
`Secure` automatically. If a *proxy* terminates TLS instead, set `dashboard_public_url` to the
`https://` URL so the dashboard knows.

**Both dashboards are open and you cannot tell them apart.** They are both dark, both on
`127.0.0.1`. Three things distinguish them: the port in the address bar (8790 is Lofi), the tab title
(*Lofi · Studio Console*), and the tab icon — Lofi's is a violet record, and it is an inline data-URI
SVG so it works with no external assets and no network. Inside the UI, the sidebar shows the bot's
own name and version, and demo mode is banner-labelled.

**It works from the server, not from your laptop.** `dashboard_host` defaults to `127.0.0.1`. To
reach it from elsewhere either bind `0.0.0.0` **and** put it behind TLS, or — much better — leave it
on loopback and tunnel: `ssh -N -L 8790:127.0.0.1:8790 you@server`. Binding off-loopback over HTTP
logs a warning that says the fix.

## History and the database

**History is empty after a restart.** Sessions that were still open when the process died are closed
on the next start with reason *"bot restarted"*, so a crash cannot leave sessions running forever in
the stats. You see them in History with a short duration; you do not lose them.

**`no such table: …`** The schema is created at startup *and* recreated on demand: if the database
file was deleted or replaced under a running bot, the next read or write re-runs the schema script
once and retries. If you see this in a log, something removed `lofi.db` while the bot was running —
check for a second process, a container volume that did not mount, or a cleanup job.

**Two processes, one database.** WAL mode is on, so a dashboard read does not block a track ending
mid-request. Running two *bots* against one `lofi.db` is still a bad idea (they will fight over
sessions and the config file), but readers and a single writer are fine.

**Move or back up the history** by copying `<data dir>/lofi.db` (plus `-wal` and `-shm` if the bot is
running, or stop it first). Set `LOFI_DB` to point the database at another volume. `LOFI_HOME` moves
everything.

**Start over**: stop the bot, move `lofi.db` aside, start it. Config is separate and survives.

## Starting, stopping and exit codes

| Code | Meaning |
| --- | --- |
| `0` | clean start and clean stop, `--check` all clear, `--version`, `--dashboard-token` |
| `1` | `--check` found a blocking problem; a global command sync failed; a network `OSError` |
| `2` | the token is missing, Discord rejected it (`LoginFailure`), a privileged intent was demanded, or the dashboard key is unusable |

Under systemd, `Restart=always` + `RestartSec=10` is right for codes 1 and 2 as well as crashes:
`2` usually means a config problem a human will fix, and restarting is cheap and self-limiting. If
you would rather it not loop on a bad token, use `Restart=on-failure` and watch
`systemctl status lofi`.

**Ctrl-C stops cleanly**: players disconnect, sessions are closed with reason *"the bot is shutting
down"*, the dashboard stops listening, then the process exits 0. If a player fails to stop you get
`Shutdown: player for … did not stop cleanly: …` and the process still exits — a hung voice gateway
must not prevent shutdown.

**It exits immediately with a token message.** Read it; both cases say what to do:

```
Discord rejected the bot token. Check config.json (or LOFI_TOKEN) - the token is on the
Bot page of the developer portal.

No bot token found. Put it in config.json as "bot_token" (or export LOFI_TOKEN).
Create one at https://discord.com/developers/applications, then run 'python3 bot.py --check'
to confirm the rest of the install.
Want to see the dashboard first? Run 'python3 bot.py --demo'.
```

Note these two go to **stderr as plain prints**, not through the logger, because they happen before
logging is configured — a `journalctl -p warning` filter will still show them, but a log-parsing
script looking for the `Lofi:` prefix will not.

## Filing a bug report

Collect these four things and most issues are diagnosable in one round trip:

```bash
python3 bot.py --version
python3 bot.py --check
/lofi status                      # paste the response
python3 bot.py --log-level DEBUG --logfile   # then attach the tail of the file it names
```

Before you paste anything: **`config.json` contains your bot token and dashboard key.** Redact both,
and if you have already pushed a config with a token in it, reset the token in the developer portal —
a token in git history is compromised even after you delete the file.
