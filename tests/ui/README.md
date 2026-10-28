# UI tests

`dashboard.ui.js` loads the real `dashboard.html` into [jsdom](https://github.com/jsdom/jsdom),
points it at a **live dashboard server**, and drives it like a person would: log in, click through
every page, change a setting, press a transport button, add and test a station, read the history
tables, log out. It asks `/api/session` which mode the server is in and, when the dashboard runs
without a login key, checks the keyless UI instead (no login screen, no sign-out button, the access
note, the login switch reading "off"). It asserts on the text the DOM ends up showing, not on CSS, so markup that drops
information fails the run. It also fails on any `console.error` or uncaught JS error.

## Run it

```bash
cd tests/ui && npm install && cd -

# 1. a server, with a token you choose (demo mode needs no Discord token).
#    LOFI_DASHBOARD_TOKEN is the way to choose it; keys must be >= 32 characters.
LOFI_DASHBOARD_TOKEN=ui-test-key-0123456789abcdef0123456789 \
  python bot.py --demo --dashboard-port 8790

# 2. in another shell:
node tests/ui/dashboard.ui.js ui-test-key-0123456789abcdef0123456789
node tests/ui/dashboard.ui.js ui-test-key-0123456789abcdef0123456789 http://127.0.0.1:9000

# or against a keyless server - no key argument, the login steps are skipped:
python bot.py --demo --no-dashboard-login --dashboard-port 8791
node tests/ui/dashboard.ui.js "" http://127.0.0.1:8791
```

Exit code 0 means every check passed.

## Notes

- A keyed server must accept the key: the login endpoint rate-limits after five failures per IP for
  five minutes, so a typo'd key can lock the harness out briefly. A keyless one accepts anything,
  including no argument at all.
- Use `--demo`. The assertions expect a populated overview (servers, listeners, tracks, events);
  against a real bot that has never connected to a voice channel the empty states are shown
  instead, and the harness reports that as a failure rather than silently passing.
- `LOFI_DASHBOARD_URL` is an alternative to the second argument.
