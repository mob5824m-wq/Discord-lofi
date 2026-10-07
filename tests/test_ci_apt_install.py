"""`scripts/ci_apt_install.sh` — the CI package installer, against a fake apt.

The installer exists because the runner's package mirror broke three different
ways in one afternoon, and each way had to stop costing a job:

* a bare ``apt-get update`` that filled no output for half an hour while a PR's
  checks stayed pending;
* a stall that outlived a 240s bound;
* a mirror that was unreachable for every bounded attempt.

What the script promises, and what is checked here: the cheap path needs no
update, a stale-lists failure recovers through an update, a mirror that never
answers is retried on the public mirror instead, and a mirror that is genuinely
gone fails *fast*, with annotations that say what happened.

`apt-get` is faked on PATH, so these run as an ordinary user: the script's
mirror rewrite is root-only (it edits /etc/apt) and correctly declines here.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_apt_install.sh"

#: A stand-in for apt-get. Records every call, and fails according to the
#: scenario's environment variables: APT_INSTALL_FAILS / APT_UPDATE_FAILS are
#: "always"/"never"/"first" (fail only the first call of that kind).
FAKE_APT_GET = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    echo "$*" >> "$APT_CALLS"
    kind=other
    case "$1" in
      install) kind=install ;;
      update) kind=update ;;
    esac
    if [ "$kind" != "other" ]; then
      n=0
      [ -f "$APT_COUNT" ] && n=$(cat "$APT_COUNT")
      n=$((n + 1))
      echo "$n" > "$APT_COUNT"
      var="APT_${kind^^}_FAILS"
      mode="${!var:-never}"
      case "$mode" in
        always) echo "E: Failed to fetch package list (fake)" >&2; exit 100 ;;
        first) [ "$n" = "1" ] && { echo "E: Failed to fetch package list (fake)" >&2; exit 100; } ;;
      esac
    fi
    exit 0
    """
)


def _run(tmp_path: Path, *, install_fails: str = "never", update_fails: str = "never"):
    """Run the installer with a fake apt-get, and report calls + output."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "apt-get"
    fake.write_text(FAKE_APT_GET)
    fake.chmod(0o755)

    calls = tmp_path / "calls"
    count = tmp_path / "count"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "APT_CALLS": str(calls),
            "APT_COUNT": str(count),
            "APT_INSTALL_FAILS": install_fails,
            "APT_UPDATE_FAILS": update_fails,
            "TMPDIR": str(tmp_path),
            # Keep the bounds irrelevant to the test's runtime; the fake
            # returns immediately and every failure is a fast non-zero exit.
            "CI_APT_TIMEOUT": "30",
        }
    )
    completed = subprocess.run(
        ["bash", str(SCRIPT), "ffmpeg", "libopus0"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return completed, calls.read_text().splitlines() if calls.exists() else []


def test_the_cheap_path_needs_no_update(tmp_path):
    """Runner images ship usable package lists; don't pay for an update."""
    completed, calls = _run(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert calls == ["install -y --no-install-recommends ffmpeg libopus0"]
    assert "directly" in completed.stdout


def test_a_direct_install_failure_recovers_through_an_update(tmp_path):
    """Stale lists are the normal reason a direct install fails."""
    completed, calls = _run(tmp_path, install_fails="first")

    assert completed.returncode == 0, completed.stderr
    assert calls[0].startswith("install ")
    assert calls[1].startswith("-o Acquire::ForceIPv4=true")
    assert calls[1].endswith("update"), calls
    assert calls[2].startswith("install ")
    assert "after an update" in completed.stdout


def test_update_attempts_are_bounded_and_connection_bounded(tmp_path):
    """The exact failure that started this: an update with no bounds anywhere."""
    completed, calls = _run(tmp_path, install_fails="first")

    update = next(call for call in calls if call.endswith("update"))
    assert "Acquire::http::Timeout=20" in update
    assert "Acquire::https::Timeout=20" in update
    assert "Acquire::ForceIPv4=true" in update
    assert "Acquire::Retries=2" in update


def test_a_dead_mirror_fails_fast_with_annotations(tmp_path):
    """Giving up is fine; giving up silently, or slowly, is not."""
    completed, calls = _run(tmp_path, install_fails="always", update_fails="always")

    assert completed.returncode == 1
    # Exactly three bounded attempts, whatever the mirror switch managed:
    # install, update+install, update+install. Never a fourth.
    assert len(calls) == 5, calls
    assert sum(1 for call in calls if call.startswith("install")) == 3
    assert sum(1 for call in calls if call.endswith("update")) == 2
    assert "::error::" in completed.stdout
    assert "unreachable from this runner" in completed.stdout
    # The apt output itself is attached, so the cause is readable from the check.
    assert "::group::apt output" in completed.stdout
    assert "Failed to fetch package list" in completed.stdout


def test_a_switched_or_unswitchable_mirror_does_not_change_the_number_of_attempts(tmp_path):
    """Three attempts happen either way.

    The third attempt used to be skipped when the mirror could not be switched
    - root-only, and pointless on the arm64 images, which use ports.ubuntu.com
    and have nothing to switch. A runner that cannot reach its mirror then got
    two attempts where every other runner got three.
    """
    assert os.getuid() != 0, "this test assumes an unprivileged runner"
    completed, calls = _run(tmp_path, install_fails="always", update_fails="always")

    assert completed.returncode == 1
    assert sum(1 for call in calls if call.endswith("update")) == 2, (
        "the third attempt must happen even when the sources cannot be rewritten"
    )


def test_it_declines_to_rewrite_sources_as_a_non_root_user(tmp_path):
    """The mirror switch edits /etc/apt, so it must be a no-op off-root.

    These tests run unprivileged; if the script tried anyway, this would touch
    the machine running the suite.
    """
    assert os.getuid() != 0, "this test assumes an unprivileged runner"
    completed, _ = _run(tmp_path, install_fails="always", update_fails="always")

    assert completed.returncode == 1
    assert "there was nothing to switch" in completed.stdout


def test_it_refuses_to_run_without_packages(tmp_path):
    completed = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)}, timeout=30,
    )
    assert completed.returncode == 2
    assert "usage:" in completed.stderr


@pytest.mark.parametrize("package", ["ffmpeg", "libopus0"])
def test_the_packages_each_job_needs_are_the_ones_asked_for(tmp_path, package):
    """A guard on the call sites in the workflows: they pass these by name."""
    completed, calls = _run(tmp_path)
    assert package in calls[0]
