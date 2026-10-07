# Architecture

How Lofi is put together, and — more usefully — *why*. The README says what it does; this says what
shapes the code. If you are changing something, the sections here are the ones to read first: most of
the odd-looking decisions are load-bearing.

- [The shape of it](#the-shape-of-it)
- [One process, one event loop](#one-process-one-event-loop)
- [The provider split](#the-provider-split)
- [Settings are pure functions](#settings-are-pure-functions)
- [Stations are data](#stations-are-data)
- [sources.py: turning a station into bytes](#sourcespy-turning-a-station-into-bytes)
- [player.py: a stream, not a queue](#playerpy-a-stream-not-a-queue)
- [generative.py: the source nobody can take down](#generativepy-the-source-nobody-can-take-down)
- [store.py: history, not configuration](#storepy-history-not-configuration)
- [paths.py: three kinds of directory](#pathspy-three-kinds-of-directory)
- [dashboard.html: no build step](#dashboardhtml-no-build-step)
- [The security model](#the-security-model)
- [command_tree.py: one description, three readers](#command_treepy-one-description-three-readers)
- [Failure philosophy](#failure-philosophy)
- [How it is tested](#how-it-is-tested)
- [Limits and trade-offs](#limits-and-trade-offs)
- [Extending it](#extending-it)

---

## The shape of it

```
                    Discord gateway (websocket)          Discord voice (UDP)
                              │                                   ▲
                              ▼                                   │ opus
        ┌───────────────────────────────────────────┐            │
        │ LofiBot (bot.py)  ── one asyncio loop ────┼────────────┘
        │                                           │
        │   app_commands tree (music.py)            │      ┌──────────────────┐
        │        │                                  │      │ DashboardServer  │
        │        ▼                                  │      │  (dashboard.py)  │
        │   PlayerManager ──► GuildPlayer (one per guild)   │  aiohttp on the  │
        │        │                 │                        │  *same* loop     │
        │        │                 ├── watchdog task        └────────┬─────────┘
        │        │                 ├── MonitoredAudio ─► ffmpeg ─► PCM        │
        │        │                 └── channel status writes          dashboard.html
        │        ▼                                                            (one file)
        │   sources.py ── yt-dlp (in a thread) / library scan / generative
        │        │
        │   settings.py  stations.py  permissions.py     (pure, no I/O)
        └────────┴──────────────────────────────────────────────────┘
                 │
                 ▼
   paths.py ─► config.json        store.py ─► lofi.db (SQLite, WAL)
               (what the operator decided)   (what the bot learned)
```

Two things this diagram is trying to make obvious:

1. **The dashboard is not a separate service.** It is an `aiohttp` app running on the bot's own event
   loop, reading the same in-memory player objects the commands do. There is no IPC, no second
   config, no cache to invalidate.
2. **Everything that blocks runs somewhere else.** yt-dlp, ffmpeg subprocess setup, WAV rendering and
   every SQLite statement are the four things that could stall the loop, and each is handled
   explicitly (thread executor, subprocess, thread executor, short-lived connection).

## One process, one event loop

The dashboard shares the bot's loop via `web.AppRunner` + `web.TCPSite`, started from
`LofiBot.setup_hook()` and closed on shutdown. This is the same arrangement Sentinel uses, and the
reasons are practical rather than aesthetic:

- **State is live.** The Player page shows what a player is doing *now* — including
  "reconnecting (attempt 3)" — because it reads the object, not a snapshot another process published.
  A separate web app would need the bot to push state, and then you have two things to keep honest.
- **Actions are direct.** "Skip" from the browser calls the same code path as `/lofi skip`. One set of
  permission checks, one set of failure modes, one set of tests.
- **One credential, one config, one deployment.** No second port to firewall, no service-to-service
  token, no "the dashboard is up but the bot is not".
- **It costs you discipline.** Anything that blocks in a request handler stalls *audio*, not just the
  UI. That is why SQLite connections are opened per statement and closed immediately, why yt-dlp
  resolution goes through `run_in_executor`, and why the dashboard never shells out itself.

The failure direction matters too: **the dashboard failing must not take the music down.** If the
port cannot be bound (another copy running, a permission problem), the bot logs
`The dashboard could not bind …` and carries on playing. `--no-dashboard` and
`"dashboard_enabled": false` turn it off entirely for operators who do not want a listener at all.

## The provider split

`DashboardServer` knows nothing about `discord`. It talks to a **Provider** — a seven-method protocol
plus a `config` property and a `demo` flag:

```python
class Provider(Protocol):
    demo: bool
    config: dict
    async def overview(self) -> dict: ...
    async def guild_detail(self, guild_id: int) -> dict: ...
    async def player_state(self, guild_id: int) -> dict: ...
    async def player_action(self, guild_id: int, action: str, payload: dict) -> dict: ...
    async def save_settings(self, guild_id: int, updates: dict) -> dict: ...
    async def history(self, guild_id: Optional[int], limit: int) -> dict: ...
    async def sync_commands(self, guild_id: Optional[int]) -> dict: ...
```

Two implementations:

| | `dashboard.BotProvider` | `demo.DemoProvider` |
| --- | --- | --- |
| Data | a live `LofiBot` + `PlayerManager` | three invented servers that move |
| Used by | `python bot.py` | `python bot.py --demo`, `tests/test_dashboard.py` |
| `demo` | `false` | `true` (the UI shows a banner; every payload carries it) |

This one seam buys three things: the UI can be developed and screenshotted with no Discord token;
`tests/test_dashboard.py` covers all 140 assertions of the API contract with no network; and a new
operator can look at the real dashboard *before* deciding to install anything. The demo provider uses
the **real** station registry and the **real** generative title function, so what you see is what the
bot would show — only the servers are fake.

The dashboard also owns all serialisation. Every Discord id leaves as a **string**, because a 19-digit
snowflake exceeds JavaScript's safe-integer range and a JSON number would be silently rounded in the
browser. `_snowflake()` is a serialiser, not a validator: it will happily stringify anything.

## Settings are pure functions

`settings.py` is pure: functions from a config dict to values, no Discord, no database, no I/O. The
payoff is disproportionate:

- No import cycle. `player` imports settings, `dashboard` is handed settings, `music` imports both.
  A module that needed a bot object would tie all three together.
- Both callers agree by construction. `/lofi setup volume:` and the dashboard's Servers page write the
  same key through the same clamp, so a value set in one is read correctly by the other.
- Testing is table-driven. 59 tests cover every key's coercion, default, clamp and precedence with no
  mocks at all.

Resolution order, highest first: **live player change → `guilds["<id>"]` → top-level default →
built-in default.** Values are *clamped, not rejected*: `volume: 4000` becomes 150, a negative idle
becomes 0, an unknown station id falls back to the default. A dashboard that returned `400` for a
slider dragged too far would be a dashboard people stop trusting.

Two deliberate asymmetries:

- **Volume set from Discord is not persisted.** `/lofi volume 90` means "turn it up for this session".
  A bot that remembered 150% across restarts is a bot someone files a bug about. `/lofi setup volume:`
  is the persistent one, and it is a different command on purpose.
- **Guild ids are strings in JSON and ints everywhere else.** A 64-bit snowflake cannot be a JSON
  object key otherwise. The helpers accept either.

## Stations are data

A station is the unit people think in ("put on Lofi Girl"), not a URL. `stations.py` is a registry of
records, merged in a fixed order: **built-ins first, then operator stations from `config.json`, with
the same id overriding**. A server can therefore replace `lofi-girl` with its own broadcast without
patching code, and `/lofi station remove lofi-girl` restores the built-in by deleting the override.

Four kinds, because they fail differently and recover differently:

| kind | what it is | failure mode | recovery |
| --- | --- | --- | --- |
| `youtube` | a watch/live URL, resolved by yt-dlp | extraction blocked, video removed, stale id | fall back to `search`, re-resolve after the cache TTL |
| `stream` | an HTTP(S)/icecast feed ffmpeg opens directly | encoder dies upstream, TLS interception | ffmpeg's own reconnect flags; the watchdog measures silence |
| `library` | files under `music/` | folder missing, corrupt file, empty folder | skip the file; two distinct messages for "no folder" vs "empty folder" |
| `generative` | rendered locally by `generative.py` | numpy missing, cache dir unwritable | the one kind that cannot fail for network reasons |

The `search` field deserves a note: **live stream ids change when a channel restarts its
broadcast**, and the common failure is a 404 on a video that used to exist. Falling back to
`ytsearch1:<query>` turns "the bot is broken" into "the station moved" — which is the truth, and
self-heals without an operator.

Validation is a keyword-only function returning `(station, error, warnings)`, so the dashboard's
**Test** button and `/lofi station add` share one implementation: a station that cannot be played is
refused *before* it is saved, with the reason.

## sources.py: turning a station into bytes

Three decisions worth defending:

**Resolve, then hand the URL to ffmpeg — do not pipe yt-dlp's stdout into it.** Piping is the common
pattern and it is worse here: with a pipe, ffmpeg cannot seek or reconnect, a hiccup in the extractor
kills playback, and the bot inherits yt-dlp's buffering. Resolving to a direct media URL and letting
ffmpeg open it means ffmpeg does its own reconnecting, its own buffer management, and reports errors
in a form the player can act on. The cost is that resolved URLs expire — hence the next point.

**Cache resolutions for 45 minutes** (`RESOLUTION_TTL_SECONDS = 2700`). Long enough that a reconnect
after a dropped voice channel does not pay for another extraction; short enough that the cache cannot
outlive the URL's own expiry and hand ffmpeg a dead link. `use_cache=False` bypasses both layers (the
async wrapper and the blocking one) — an earlier version honoured it only in the wrapper, so a forced
re-resolve could still be served from cache.

**Nothing blocking touches the loop.** `_blocking()` runs a callable in the default executor, and
library scans and generative renders go through it too. The generative path also *pre-renders the next
track while the current one plays*, so a ~3 s render never becomes an audible gap.

A `Track` is the output: a concrete location (file path or URL) plus the metadata the player and the
dashboard display — title, station, kind, `is_live`, duration, artwork, uploader, seed, recipe. Live
tracks carry no duration and no progress, and the UI hides the progress bar for them rather than
showing a bar that never moves.

## player.py: a stream, not a queue

The load-bearing assumption: **a lofi station is a stream that should be playing whenever anybody is
listening**, not a playlist someone assembled. Everything else follows.

- **When a track ends, the station continues** — next file, next generated seed, or a reconnect to the
  same broadcast. Hand-queued items play first, then the station resumes. `/lofi stop` is the only
  thing that ends it.
- **Failures are retried with a backoff**, not surfaced as a crash: `min(45, 1.5 · 2^(attempt−1))`
  seconds, capped at `MAX_RESTART_ATTEMPTS = 6`, after which the player gives up with reason `error`
  and records it. The counter resets on any successful track, so a station that works for six hours
  and then fails twice is not "about to give up".
- **A watchdog task owns everything that happens without an event**, every 5 s: dead voice
  connections, an empty channel for `idle_minutes`, a stalled stream, and refreshing the channel
  status. This is also what makes the dashboard honest — `state()` reports what the player is doing
  now, not what the last command claimed.

The stall detector is the non-obvious part. discord.py tells you when an audio source *ends* or
*raises*; a 24/7 broadcast whose upstream encoder died does neither — it holds the socket open and
sends nothing, forever. So `MonitoredAudio` (a `FFmpegPCMAudio` subclass) timestamps every read, and
the watchdog restarts the track after `STALL_SECONDS = 20` of silence. Without it, the failure looks
like "the bot is connected and playing" to everyone, including the dashboard.

The voice-channel status line has two constraints that shaped the code: it is **write-only** (no API
returns the current status, so the player remembers what it last wrote and skips identical writes) and
it is **rate-limited** (`STATUS_MIN_INTERVAL = 8`, truncated to 90 of the allowed 500 characters).
Clearing on stop is *exempt* from the throttle — a stale "now playing" under a channel for hours is
worse than one extra request. An earlier version throttled the clear and shipped that bug.

Sessions and events are written through `store.py` as the player goes, including peak listeners, so
the History page can answer "why did it stop at 3 a.m." (`endedReason`) without logs.

## generative.py: the source nobody can take down

Every other station depends on someone else: a broadcast that can be taken down, an icecast host that
can move, a `music/` folder that starts out empty. This module is the one source the bot controls end
to end — rendered on the machine it runs on, no network, nothing to license. It is what makes a fresh
install playable in ten seconds, and what `--demo` listens to.

Implementation notes that matter if you touch it:

- **numpy, float32, 44100 Hz**, rendered in one pass per track (`TRACK_SECONDS = 52`). No per-sample
  Python: every envelope, LFO and pattern is a vector operation. A per-sample `random.uniform` in a
  loop is how this becomes a 40-second render instead of a 3-second one.
- **Tiled texture.** Vinyl crackle and the noise floor are generated as a ~4 s tile and repeated with
  per-tile gain jitter (±18%) — enough to break the loop, too small to pump. Generating 52 s of
  independent noise per render is wasted work for something inaudible.
- **`TARGET_PEAK = 0.84`** and envelopes that intentionally peak below 1.0, so raising the volume to
  150% clips less. Loudness is a volume problem, not a render problem.
- **The "lofi" is DSP, not samples**: a low-pass/tilt curve in the frequency domain, wow and flutter
  as an LFO on instantaneous frequency, `tanh` tape saturation, a Haas-widened stereo image.
- **Content-hashed cache.** The filename is a SHA-256 of the recipe + seed + engine parameters, so a
  re-render of the same thing is a file lookup, and a change to the engine invalidates old files
  automatically. The cache is pruned to the newest 40.
- The stereo bus must not be L/R-averaged for the tilt filter — an earlier version did, collapsing the
  image the Haas widening had just created.

Titles come from a word list seeded by the same hash, which is why they read like *Cassette Sun in the
attic* rather than *generative-3*. The demo provider uses the same function, so demo titles are real
titles.

## store.py: history, not configuration

The split is the point: `config.json` holds **what the operator decided** (small, hand-editable,
survives a database reset); `lofi.db` holds **what the bot learned** (grows forever, must survive a
config rewrite). Neither should ever be written because of the other.

- **One connection per statement**, wrapped in `closing()`, rows as `sqlite3.Row`. A long-lived
  connection held across an `await` is how you get "database is locked" when the dashboard writes
  while a track ends. Voice playback shares the loop, so the connection lifetime is deliberately
  shorter than any await.
- **WAL** keeps readers (dashboard polling history) from blocking the writer (a session ending
  mid-request).
- **The path is read from `paths` on every call**, not captured at import time, so a test that
  redirects `LOFI_HOME` is honoured and `LOFI_DB` can point at another volume.
- **The schema self-heals.** Tables are created at startup, and any `no such table` error re-runs the
  schema script once and retries — the database file can be deleted or replaced underneath a
  long-running bot, and a dashboard request should not fail when the fix is one `executescript` away.
- **Dangling sessions are closed on boot** with reason "bot restarted", so a crash cannot leave
  sessions running forever in the stats.
- `SCHEMA_VERSION` is a row, not a filename convention: migrations go forward in place.

## paths.py: three kinds of directory

| | Contains | Writable? | Override |
| --- | --- | --- | --- |
| **application dir** | `bot.py`, `dashboard.html`, `config.example.json`, `music/` | maybe not (a packaged or read-only install) | — |
| **data dir** | `config.json`, `lofi.db`, `lofi.log`, `cache/` | must be | `LOFI_HOME`, else the platform state dir, else `./data` |
| **music dir** | the operator's own audio files | read-only is fine | `LOFI_MUSIC` |

Keeping `music/` out of the data directory is deliberate: people sync that folder from elsewhere, and
a reinstall or a state reset must not delete their tracks. `music_dir()` returns `None` rather than
creating an empty folder, because "you have no library folder" and "your library is empty" are
different problems with different fixes and the UI says both.

`ffmpeg_executable()` lives here because "which ffmpeg do we shell out to" is the same question as
"which file do we read": on a desktop machine it is usually installed somewhere that is not on `PATH`
(a Homebrew prefix, `C:\ffmpeg\bin`, a Python package that bundles one). The search order is
`LOFI_FFMPEG`/`LOFI_FFPROBE` → `PATH` → common install locations → the `imageio-ffmpeg` bundle.

Writes go to `LOFI_CONFIG` if set, else `<data dir>/config.json` — **never** to
`config.example.json`, which is a read candidate only. That asymmetry is the whole reason the
template has a different filename: `config.json` is gitignored because it is where a token lands, and
a token in git history is a token you have to rotate. The data directory is chmod'ed `0700` and the
config written `0600`, best effort.

`is_frozen()` / `bundle_dir()` support a PyInstaller-style bundle reading resources from alongside the
executable, which is why resource lookups go through `resource_path()` instead of `__file__`.

## dashboard.html: no build step

One file: inline CSS, inline JS, no bundler, no CDN, no external requests. It is served from disk on
every request (so an edit needs no restart), and the CSP reflects the constraint —
`script-src 'self' 'unsafe-inline'` is what a single-file UI costs, and it is why there are no
third-party assets to compromise. Images are allowed from `data:` and `cdn.discordapp.com` (station
and avatar art); `connect-src 'self'`; `base-uri 'none'`.

What that buys: it works from a machine that can reach the bot and nothing else. No `npm install`, no
asset hashes, no "the dashboard is blank because a CDN is down", no build artefacts to commit. The
cost is a large file — which is why the jsdom harness asserts on **rendered DOM text** rather than on
markup or CSS: information loss is the failure mode that matters, and styling churn should not break
tests.

The UI is a plain `fetch` layer over the JSON API plus a page router. Sessions are server-side, so the
client holds no state beyond the cookie and the CSRF token; every mutating call sends
`X-CSRF-Token`, and errors are surfaced through one toast function. The demo banner is driven by the
`demo` flag in the payload, not by a URL parameter, so it cannot be turned off by editing a query
string.

## The security model

The dashboard can stop music in every server the bot is in and rewrite configuration — the same reach
as the bot token. It is treated as an admin panel, and the controls are layered so that a mistake in
one does not open everything:

| Control | Implementation | What it stops |
| --- | --- | --- |
| One high-entropy key | `secrets.token_urlsafe(36)`, generated once, stored `0600`, never logged; `<32` chars refused; `LOFI_DASHBOARD_TOKEN` overrides | guessing; a key sitting in a log or a backup of a log |
| Server-side sessions | 8 h sliding TTL, `HttpOnly` + `SameSite=Strict` + `Path=/`, `Secure` under TLS | XSS reading a token; CSRF via cross-site posts |
| CSRF tokens | per session, `hmac.compare_digest` on POST/PUT/PATCH/DELETE for `/api/*` except login | a page you visit driving your authenticated session |
| Login throttling | 5 failures per IP per 5 min → `429` | brute force. `dashboard_trusted_proxies` makes the IP the *real* one behind a proxy |
| Host allowlist | loopback always allowed; else `dashboard_allowed_hosts` or the hostname of `dashboard_public_url`; `*` opts out and warns | **DNS rebinding** — a page in your browser pointing a name at `127.0.0.1` and reading the API with your cookie |
| Body cap | 64 KiB → `413` | a request that exhausts memory |
| Headers on every response | CSP, `nosniff`, `no-referrer`, `Permissions-Policy` | injection, MIME confusion, referrer leaks, device access |
| Error handling | understood errors keep status + message; unexpected ones become a generic JSON `500` | internals leaking through a stack trace |

Bound to `127.0.0.1` by default. Listening off-loopback over plain HTTP logs a warning that states
the *fix*, not just the risk — every warning in `remote_access_warnings()` does, because a warning
that says "this is dangerous" without saying what to do is a warning people learn to ignore.

## command_tree.py: one description, three readers

The command *implementations* live in `music.py`. `command_tree.py` exists because three other places
need to **describe** the tree rather than run it: the dashboard's Commands page, the test suite, and
the README's command table (generated from `reference_markdown()`). One source of truth means the docs
cannot drift from the code, and a renamed command fails a test instead of silently invalidating a
paragraph.

Permissions are declared as tiers (`TIER_TRANSPORT`, `TIER_CONFIGURATION`, `TIER_INFO`) and enforced
**at run time in `music.py`**, not left to Discord's `default_member_permissions`. Two reasons:
command registration is cached for up to an hour, so a stale registration cannot be the thing that
widens access; and the transport rule ("are you in the same voice channel as the bot") is not
expressible as a Discord permission at all. `default_permissions` is still set, so the client greys
commands out for people who cannot use them — that is UX, not enforcement.

`guild_only=True` goes on the **Group** constructors. Putting `@app_commands.guild_only()` on a
subcommand does nothing, which is the kind of bug a test catches and a manual check does not.
`_NullBot` lets `describe()` run with no token and no connection, which is how `--check`, the docs and
the tests all read the tree.

## Failure philosophy

Four rules the code follows consistently, worth keeping if you add to it:

1. **Cosmetic failures never take audio down.** A missing status permission, a failed now-playing
   post, a dashboard that cannot bind — each logs once and playback continues.
2. **Every user-facing message says what to do.** Not "YouTube error 403" but "YouTube refused the
   request (HTTP 403). This usually means yt-dlp is out of date — run `pip install -U yt-dlp`".
   `_explain_youtube_error()` maps five failure families to five different fixes.
3. **Report honestly, even when the answer is "that does nothing".** The permission audit marks a
   granted **Priority Speaker** as `no-effect` with the reason, rather than a green tick for a
   permission no bot can use. A dashboard that flatters you is a dashboard you cannot debug with.
4. **Distinguish "absent" from "empty".** `music_dir()` returns `None` vs an empty list; the library
   page says "you have no library folder" vs "the folder is there but empty"; a station with no
   network requirement is marked as one. Different problems, different fixes, different messages.

## How it is tested

831 pytest tests, no network, no Discord token, ~50 s. The strategy is **seams and fakes**, because
the untestable part (a real voice connection) is the smallest part:

- **Real aiohttp.** `tests/test_dashboard.py` builds the actual application through
  `DashboardServer._build_app()` and drives it with a test client — auth, throttling, CSRF, the Host
  allowlist, body limits, header presence, and every payload shape. 140 tests. This is the file to
  read before changing anything about authentication.
- **Fake voice clients.** `test_player.py` covers advancing, backoff, the watchdog, idle disconnect,
  stall recovery and session bookkeeping against stubs. `MonitoredAudio` must subclass
  `discord.AudioSource` in the fakes, or `PCMVolumeTransformer.__del__` crashes pytest in a way that
  looks nothing like the actual mistake.
- **Real DSP, asserted numerically.** `test_generative.py` checks peak level, envelope shape, chord
  spelling, drum grids and determinism — a renderer is testable without listening to it.
- **Table-driven purity.** `test_settings.py` and `test_stations.py` need no mocks at all, which is
  the direct payoff of those modules being pure.
- **Blocking-call guards.** `test_sources.py` patches both the async and the blocking resolution paths,
  because patching one still lets a test reach the network through the other.
- **A jsdom harness** (`tests/ui/`) drives the real `dashboard.html` against a live `--demo` server:
  64 assertions on rendered text, failing on any `console.error`.
- **A coexistence file.** `test_coexistence.py` exists because this dashboard is modelled on
  Sentinel's and people run both on one machine. It pins every namespace the two would otherwise
  share — the port (8790 here, 8765 there), the cookie name, the state directory, the database and
  log filenames, the `LOFI_*` env prefix, the `/lofi` command group — and proves functionally that a
  session lifted from one dashboard is refused by the other. Cookies ignore ports, so two dashboards
  on `127.0.0.1` share one jar: distinct names are the only thing separating them, which is exactly
  the kind of fact that erodes silently in a refactor.

Deliberately **not** tested: real Discord voice, real YouTube extraction, and anything requiring an
outbound connection. Those are covered by `--check` at install time and by `/lofi status` at run time
instead — a diagnostic that names the missing permission is worth more than a mock that asserts the
call was made.

The suite has found real bugs, which is the only argument for it that matters: opus failures swallowed
on connect; a rate-limited status clear leaving a stale track name; an announcement-channel lookup
that excluded threads and news channels; a status embed rendering "This server" with no guild;
`?limit=abc` and a non-numeric `guildId` returning HTTP 500; `use_cache=False` ignored by the cached
resolver.

## Limits and trade-offs

- **No sharding.** One process, one gateway connection. Correct below ~2500 servers; beyond that
  discord.py's `AutoShardedClient` is the change, and the player manager is already keyed by guild id
  so it would not need to move.
- **Single machine per bot.** Voice is UDP to Discord from wherever the process runs; there is no
  distributing players across hosts. Scaling out means multiple bot applications, not multiple
  workers.
- **CPU and upstream bandwidth are the real ceiling.** One ffmpeg subprocess and one Opus encoder
  thread per connected guild, ~100 kbit/s upstream per connection, plus whatever the source costs.
  YouTube stations from one IP get rate-limited long before the CPU does.
- **The queue is not persisted.** It is a `deque` capped at `MAX_QUEUE = 50`, and it is empty after a
  restart. Stations are; queues are not. Deliberate: a queue that survives a restart plays music
  nobody asked for, hours later.
- **No per-user volume.** Discord mixes client-side, so this would only be a bot-side gain change
  affecting everybody. Volume is per guild.
- **Priority Speaker is unusable by a bot.** Not a missing feature — no API, desktop-only, requires a
  human's push-to-talk keybind. Documented in
  [SETUP.md](SETUP.md#4-about-priority-speaker) and reported as `no-effect` by the audit.
- **One file of HTML** is a real constraint at some size. The current page count is comfortable; a
  dozen more pages would want components and a build step, and the CSP would want `script-src 'self'`
  without `unsafe-inline`.

## Extending it

**A new station kind.** Add it to `stations.KINDS`, teach `sources.build_track()` to produce a `Track`
for it (off the loop if it blocks), add a validation rule, and it appears in `/lofi stations`, the
dashboard and the tests automatically — the registry is data, so nothing else needs to know.

**A new command.** Implement it in `music.py` under the right tier, add its metadata to
`command_tree.py`, and the README table, the Commands page and the tree tests all pick it up. Then
`python bot.py --sync-commands` to publish without a restart.

**A new dashboard page.** Add a route, a provider method if it needs data the provider does not
already expose, a nav button and a `load*()` function in `dashboard.html`, and an assertion in
`tests/ui/`. Anything mutating needs CSRF, which the UI already sends.

One ordering rule: aiohttp matches routes in registration order, so a literal path that shares a
prefix with a dynamic one must come first. Every literal route is registered above the
`{guild_id}`/`{station_id}` block for that reason — add `/api/guilds/summary` *below*
`/api/guilds/{guild_id}` and `summary` arrives as a guild id, to be answered with
`404 "That is not a server id."` instead of your data.

**A new setting.** One entry in `settings.py` (default, coercion, clamp) and it is readable by the
player, writable by `/lofi setup`, editable from the Servers page, and covered by the table tests.
