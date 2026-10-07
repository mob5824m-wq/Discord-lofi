# UI tests

`dashboard.ui.js` loads the real `dashboard.html` into [jsdom](https://github.com/jsdom/jsdom),
points it at a **live dashboard server**, and drives it like a person would: log in, click through
every page, change a setting, press a transport button, add and test a station, read the history
tables, log out. It asserts on the text the DOM ends up showing, not on CSS, so markup that drops
information fails the run. It also fails on any `console.error` or uncaught JS error.

## Run it

```bash
cd tests/ui && npm install && cd -

# 1. a server, with a token you choose (demo mode needs no Discord token):
python bot.py --demo --dashboard-token ui-test-key --dashboard-port 8790

# 2. in another shell:
node tests/ui/dashboard.ui.js ui-test-key
node tests/ui/dashboard.ui.js ui-test-key http://127.0.0.1:9000   # another instance
```

64 assertions. Exit code 0 means every one passed.

## Notes

- The server must be reachable and must accept the key: the login endpoint rate-limits after five
  failures per IP for five minutes, so a typo'd key can lock the harness out briefly.
- Use `--demo`. The assertions expect a populated overview (servers, listeners, tracks, events);
  against a real bot that has never connected to a voice channel the empty states are shown
  instead, and the harness reports that as a failure rather than silently passing.
- `LOFI_DASHBOARD_URL` is an alternative to the second argument.
