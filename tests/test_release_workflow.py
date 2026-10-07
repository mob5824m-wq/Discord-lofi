"""Invariants of the release workflow — the ones whose failure is silent.

Every merge to main is supposed to end in a published release: ``prepare`` bumps
VERSION and pushes the tag, ``build`` produces the 6 native installers,
``release`` publishes them. On v1.1.9 that chain broke in the one place nothing
was watching. ``prepare`` enabled ``actions/setup-python``'s pip cache although
the job never runs pip, and the action's *post* step (cache-save,
``post-if: success()``) failed with::

    Cache folder path is retrieved for pip but doesn't exist on disk:
    /home/runner/.cache/pip

*after* the bump commit and its tag had already been pushed. The job went red
anyway, and because ``build`` required ``needs.prepare.result == 'success'``,
build and release were skipped: main moved, the tag existed, no release was
published.

The properties below are what that failure turned on, so they are pinned here.
The workflows are read as text on purpose — PyYAML is not in
requirements-dev.txt, and only whole-job questions are being asked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def _job_blocks(text: str) -> dict[str, str]:
    """Split the ``jobs:`` mapping of a workflow into ``{job name: text}``.

    A job starts at a two-space-indented ``name:`` line with nothing after the
    colon; everything up to the next such line (or the next top-level key)
    belongs to it. ``run: |`` bodies never match: they carry something after
    the colon.

    Whole-line comments are dropped, so that a comment *about* a command cannot
    satisfy a check for the command: "this job never runs pip install" must not
    read as a job that runs it, and a note explaining why `apt-get update` needs
    bounding must not read as an unbounded one.
    """
    blocks: dict[str, str] = {}
    current: str | None = None
    in_jobs = False
    for line in text.splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line[:1] not in ("", " ", "\t") and not line.startswith("#"):
            break  # a new top-level key: jobs is over
        match = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = ""
            continue
        if current is not None and not line.lstrip().startswith("#"):
            blocks[current] += line + "\n"
    return blocks


#: {workflow file name: {job name: job text}}
WORKFLOWS: dict[str, dict[str, str]] = {
    path.name: _job_blocks(path.read_text(encoding="utf-8"))
    for path in sorted(WORKFLOWS_DIR.glob("*.yml"))
}

RELEASE = WORKFLOWS["release.yml"]

#: `cache: pip` / `cache: 'pip'` / `cache: "pip"` as a whole line.
CACHE_PIP = re.compile(r"^\s*cache:\s*['\"]?pip['\"]?\s*$", re.MULTILINE)
PIP_INSTALL = re.compile(r"\bpip install\b")


def _join_continuations(text: str) -> str:
    """Fold shell line continuations, so one command is one line.

    A command split with a trailing backslash is still a single command, and
    the flags that make it safe may well sit on the second line.
    """
    return re.sub(r"\\\n\s*", " ", text)

#: The 6 installers a release must carry, spelled as the release job spells them.
SIX_INSTALLERS = [
    "lofi-${VER}-linux-amd64.deb",
    "lofi-${VER}-linux-arm64.deb",
    "lofi-${VER}-macos-amd64.dmg",
    "lofi-${VER}-macos-arm64.dmg",
    "lofi-${VER}-windows-amd64-setup.exe",
    "lofi-${VER}-windows-arm64-setup.exe",
]


# --------------------------------------------------------------------------- #
# the parser itself, so a green run means something
# --------------------------------------------------------------------------- #

def test_workflows_are_discovered_and_parsed():
    assert {"build.yml", "release.yml"} <= set(WORKFLOWS)
    assert {"prepare", "build", "release"} <= set(RELEASE)
    # Every job block has content; an empty one would make the checks vacuous.
    for name, jobs in WORKFLOWS.items():
        for job, block in jobs.items():
            assert block.strip(), f"{name}:{job} parsed as empty"


# --------------------------------------------------------------------------- #
# the v1.1.9 regression: a pip cache on a job that never runs pip
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_pip_cache_is_only_enabled_where_pip_runs(workflow):
    """`cache: pip` on a job that installs nothing fails the job in its post step.

    setup-python's cache-save post step refuses to save a cache directory that
    was never created — and an absent ``~/.cache/pip`` is the normal state of a
    job that never runs pip. The failure lands *after* the steps that mattered
    have already run, which is the worst possible place for it: the bump commit
    and tag are pushed, and the job still goes red.
    """
    for job, block in WORKFLOWS[workflow].items():
        if CACHE_PIP.search(block):
            assert PIP_INSTALL.search(block), (
                f"{workflow}:{job} enables the pip cache but never runs pip; "
                "setup-python's post step will fail with 'Cache folder path is "
                "retrieved for pip but doesn't exist on disk' and take the "
                "rest of the release with it"
            )


def test_release_prepare_does_not_cache_pip():
    """The v1.1.9 regression, pinned by name so it cannot quietly come back."""
    assert not CACHE_PIP.search(RELEASE["prepare"])


# --------------------------------------------------------------------------- #
# the blast radius: one late failure must not swallow a whole release
# --------------------------------------------------------------------------- #

def test_build_runs_when_prepare_died_after_publishing_the_bump():
    """`prepare.outputs.sha` means the bump commit and its tag reached main.

    build must not skip that case. The release is the entire point of this
    workflow, and a failure *after* the push (a setup action's post step, say)
    must not turn into a version that exists as a tag and nowhere else.
    """
    assert "needs.prepare.outputs.sha" in RELEASE["build"]


def test_release_never_publishes_without_a_successful_build():
    """Otherwise a failed prepare yields an empty release and a broken page."""
    assert "needs.build.result == 'success'" in RELEASE["release"]


def test_release_requires_all_six_installers():
    """Five of six installers is a download page with a dead row on it."""
    for name in SIX_INSTALLERS:
        assert name in RELEASE["release"], f"{name} is not required by the release job"


# --------------------------------------------------------------------------- #
# nothing may hang forever: a stalled mirror has to fail, not sit there
# --------------------------------------------------------------------------- #

def test_every_job_has_a_timeout():
    """A job with no timeout can sit for six hours producing nothing.

    Not hypothetical: the jsdom job on PR #7 spent half an hour inside
    ``apt-get update`` on a stalled mirror while the run's checks stayed
    pending, and there was no output anywhere to say why. GitHub's default job
    timeout is six hours, which is long enough for "slow" and "dead" to look
    identical.
    """
    for workflow, jobs in WORKFLOWS.items():
        for job, block in jobs.items():
            assert re.search(r"^    timeout-minutes:\s*\d+\s*$", block, re.MULTILINE), (
                f"{workflow}:{job} has no job-level timeout-minutes"
            )


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_apt_updates_bound_their_connections(workflow):
    """`apt-get update` with no bounds is a step that can hang on its own.

    A blackholed route to the mirror — the usual one is IPv6 to
    archive.ubuntu.com — waits out its own timeout per attempt, and with none
    configured the step simply never returns. ForceIPv4 and the Acquire
    timeouts bound every connection; `timeout N apt-get` bounds the attempt as
    a whole, so a mirror that accepts connections but never delivers cannot
    hold the step either.
    """
    for job, block in WORKFLOWS[workflow].items():
        for command in re.findall(r"^.*apt-get.*update.*$", _join_continuations(block), re.MULTILINE):
            if command.lstrip().startswith("echo "):
                continue  # a message *about* apt-get, not a call to it
            assert "Acquire::http::Timeout" in command, (
                f"{workflow}:{job}: apt-get update without Acquire::http::Timeout "
                f"- a stalled mirror would hang it: {command.strip()}"
            )
            assert re.search(r"\btimeout\s+\d+\s+apt-get", command), (
                f"{workflow}:{job}: apt-get update is not wrapped in `timeout N` "
                f"- one stalled attempt would run to the step timeout: {command.strip()}"
            )
