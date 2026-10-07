#!/usr/bin/env bash
# Install OS packages on a CI runner without letting a flaky mirror win.
#
#   sudo bash scripts/ci_apt_install.sh ffmpeg libopus0 [dpkg-dev ...]
#
# The runner's package mirror (azure.archive.ubuntu.com on the 22.04/24.04
# images) breaks in ways that are neither our code's fault nor the same twice.
# In this repository it has, across one afternoon:
#
#   - sat inside a bare `apt-get update` for over half an hour with no output,
#     while a pull request's checks stayed pending and nothing said why;
#   - stalled past a 240s bound, twice, on two different runner images;
#   - and been unreachable for three bounded attempts in a row, failing a job
#     that had nothing to do with packages.
#
# So the rule is: never let apt be the reason a release or a check dies, and
# never make a human guess which of those happened. Every attempt is bounded,
# the cheapest option is tried first, a second mirror is tried before giving
# up, and the failure carries the apt output as annotations.
#
# Order of attempts:
#   1. install straight away - the images ship populated package lists, so a
#      warm runner often needs no update at all, just the .debs;
#   2. update, then install - for stale or missing lists;
#   3. the same again, after pointing apt at the public archive mirror if the
#      azure one is what the image uses. The switch is a bonus, not a
#      precondition: arm64 images use ports.ubuntu.com and have nothing to
#      switch, and a runner where the rewrite is not possible still deserves
#      the third attempt. Three bounded attempts happen either way.
#
# Deliberately not wrapped in sudo here: the caller decides (in CI it is
# `sudo bash scripts/ci_apt_install.sh ...`), and the tests run it as an
# ordinary user with a fake apt-get on PATH.
set -uo pipefail

PACKAGES=("$@")
if [ "${#PACKAGES[@]}" -eq 0 ]; then
  echo "usage: $0 <package> [package ...]" >&2
  exit 2
fi

LOG="${TMPDIR:-/tmp}/ci-apt-install.log"
ATTEMPT_TIMEOUT="${CI_APT_TIMEOUT:-240}"
ATTEMPTS=3

# Bound every connection as well as the attempt as a whole: a route that goes
# nowhere (typically IPv6 to a mirror that is only reachable over IPv4) would
# otherwise wait out its own default timeout on every single index file.
APT_UPDATE_OPTS=(
  -o Acquire::ForceIPv4=true
  -o Acquire::Retries=2
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
)

export DEBIAN_FRONTEND=noninteractive
: > "$LOG"

run_bounded() {
  echo "--- timeout ${ATTEMPT_TIMEOUT}s: $* ---" >> "$LOG"
  timeout "$ATTEMPT_TIMEOUT" "$@" >> "$LOG" 2>&1
}

try_install() {
  run_bounded apt-get install -y --no-install-recommends "${PACKAGES[@]}"
}

try_update_then_install() {
  run_bounded apt-get "${APT_UPDATE_OPTS[@]}" update || return 1
  try_install
}

switch_to_public_mirror() {
  # Root only, and only worth trying on images that actually use the azure
  # mirror. Anything unexpected here is not fatal - the attempt that follows
  # fails the same way it would have and is reported the same way.
  [ "$(id -u)" = "0" ] || return 1
  local files
  files=$(grep -rl "azure\.archive\.ubuntu\.com" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true)
  [ -n "$files" ] || return 1
  # shellcheck disable=SC2086
  sed -i 's|azure\.archive\.ubuntu\.com|archive.ubuntu.com|g' $files || return 1
  echo "sources now point at archive.ubuntu.com:" >> "$LOG"
  # shellcheck disable=SC2086
  cat $files >> "$LOG" 2>/dev/null || true
}

report_failure() {
  echo "::group::apt output (last 60 lines)"
  tail -n 60 "$LOG" 2>/dev/null || true
  echo "::endgroup::"
  grep -E "^(E:|Err:|W:|WARNING:|.*[Cc]ould not|.*[Tt]emporary failure|.*[Tt]imed out|.*[Tt]imeout)" "$LOG" \
    | tail -n 5 | cut -c1-240 | sed 's/^/::error::/' || true
  echo "::error::apt could not install ${PACKAGES[*]} in ${ATTEMPTS} attempts, on either mirror - the package mirror is unreachable from this runner"
}

echo "Installing: ${PACKAGES[*]}"

if try_install; then
  echo "Installed ${PACKAGES[*]} directly - the package lists were already usable."
  exit 0
fi
echo "::warning::installing directly failed (stale or missing package lists) - updating first"

if try_update_then_install; then
  echo "Installed ${PACKAGES[*]} after an update."
  exit 0
fi
if switch_to_public_mirror; then
  echo "::warning::the default mirror could not be reached - retrying against archive.ubuntu.com"
else
  echo "::warning::the mirror could not be reached, and there was nothing to switch - one more attempt"
fi

if try_update_then_install; then
  echo "Installed ${PACKAGES[*]} on the third attempt."
  exit 0
fi

report_failure
exit 1
