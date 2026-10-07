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


def _shell_of(job_block: str) -> str:
    """The text of every ``run:`` in a job — the commands, not the prose.

    A job block also carries prose that *mentions* commands: the release body
    tells users to run ``apt-get install -f`` after installing the .deb, and a
    check for "this job calls apt" must not read that as a call.
    """
    lines = job_block.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if not match:
            index += 1
            continue
        indent, rest = match.group(1), match.group(2)
        if rest and rest not in ("|", "|-", ">", ">-"):
            out.append(rest)
            index += 1
            continue
        index += 1
        while index < len(lines):
            following = lines[index]
            if following.strip() and not following.startswith(indent + "  "):
                break
            out.append(following)
            index += 1
    return "\n".join(out)

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


#: Where the OS packages each job needs get installed, and with what.
PACKAGE_INSTALL_CALLS = {
    "build.yml": {
        "tests": ["ffmpeg", "libopus0"],
        "ui": ["ffmpeg", "libopus0"],
    },
    "release.yml": {
        "build": ["ffmpeg", "libopus0", "dpkg-dev"],
    },
}


def test_no_workflow_calls_apt_directly():
    """apt is flaky in three separate ways; it is handled in exactly one place.

    Not a style rule. A bare `apt-get update` in a job is what produced a step
    that ran for half an hour with no output, then two more failures on two
    other runners, and each fix had to be repeated in three places to hold.
    The installer script is that place; a job that goes around it loses the
    bounds, the retries and the mirror fallback.
    """
    for workflow, jobs in WORKFLOWS.items():
        for job, block in jobs.items():
            for command in re.findall(r"^.*apt-get.*$", _join_continuations(_shell_of(block)), re.MULTILINE):
                if command.lstrip().startswith("echo "):
                    continue  # a message *about* apt, not a call to it
                pytest.fail(
                    f"{workflow}:{job} calls apt-get directly; use "
                    f"scripts/ci_apt_install.sh instead: {command.strip()}"
                )


def test_every_job_that_needs_system_packages_uses_the_installer():
    for workflow, jobs in PACKAGE_INSTALL_CALLS.items():
        for job, packages in jobs.items():
            assert "scripts/ci_apt_install.sh" in WORKFLOWS[workflow][job], (
                f"{workflow}:{job} installs system packages without the installer"
            )
            for package in packages:
                assert package in WORKFLOWS[workflow][job], (
                    f"{workflow}:{job} no longer installs {package}"
                )


def test_the_installer_bounds_every_attempt_and_every_connection():
    """The installer's own contract: nothing unbounded, nothing hand-rolled."""
    source = (WORKFLOWS_DIR.parent.parent / "scripts" / "ci_apt_install.sh").read_text(encoding="utf-8")

    assert 'timeout "$ATTEMPT_TIMEOUT"' in source, "attempts are not bounded as a whole"
    for option in (
        "Acquire::ForceIPv4=true",
        "Acquire::Retries=2",
        "Acquire::http::Timeout=20",
        "Acquire::https::Timeout=20",
    ):
        assert option in source, f"the installer no longer sets {option}"

    # Every apt-get call is routed through the helper that applies the bound.
    for line in source.splitlines():
        stripped = line.strip()
        if "apt-get" not in stripped or stripped.startswith("#") or stripped.startswith("echo"):
            continue
        assert stripped.startswith("run_bounded apt-get"), (
            f"apt-get outside run_bounded - one stalled attempt would run "
            f"unbounded: {stripped}"
        )
