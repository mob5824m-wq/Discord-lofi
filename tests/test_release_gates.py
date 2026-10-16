"""The two scripts that gate a release, run the way a Windows runner runs them.

Five releases were lost — v1.1.12 through v1.1.16, nothing published at all —
because the release workflow's "Verify the artifact is the real thing" step ended
in exit code 1 on both Windows jobs with no annotation and no message. The cause
was one ``print``: the step found the PyNaCl row of ``lofi --check`` and echoed it
back, the row starts with ``✓``, and Python on a Windows runner encodes a
*redirected* stream as cp1252, so the print raised ``UnicodeEncodeError`` and
killed the step. Linux and macOS runners are UTF-8 and passed, which is why it
looked like a Windows packaging problem rather than a printing one.

The tests here run those scripts as subprocesses with
``PYTHONIOENCODING=cp1252``, which reproduces the Windows behaviour exactly on
any platform: same codec, same redirected pipe, same exit code. What they pin is
that a diagnostic can never again die on its own output — and that when it does
fail, it fails by saying what is wrong.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ci_stdio  # noqa: E402
import verify_voice  # noqa: E402

#: What Python on a Windows GitHub runner uses for a redirected stream.
CP1252 = "cp1252"

#: A faithful `lofi --check` report: the row layout bot.py's run_check() prints,
#: with the host tools a build runner may or may not have.
CHECK_LOG = """
Lofi install check
--------------------------------------------------------------
 \u2713 Python          3.11.9
 \u2713 discord.py      2.4.0
 \u2713 PyNaCl          1.5.0 (voice encryption)
 \u2713 ffmpeg          C:\\ProgramData\\chocolatey\\bin\\ffmpeg.exe
 \u2717 libopus         not found
 \u2713 numpy           1.26.4
 \u2713 music folder    D:\\a\\_temp\\lofi-home\\music (0 files)
 \u2713 bot token       LOFI_TOKEN
 \u2713 config file     D:\\a\\_temp\\lofi-home\\config.json
 \u2713 data directory  D:\\a\\_temp\\lofi-home (writable)
 \u2713 dashboard       http://127.0.0.1:8787 (token in config.json)
 \u2713 stations        14 available (6 work offline)
--------------------------------------------------------------
To fix:
  \u00b7 Install libopus: sudo apt install libopus0 / brew install opus

Audio will not work until the \u2717 items above are fixed.
"""

#: What the same report looks like when the frozen binary cannot import PyNaCl.
NO_VOICE_LOG = CHECK_LOG.replace(
    " \u2713 PyNaCl          1.5.0 (voice encryption)",
    " \u2717 PyNaCl          not importable (Discord voice will not be supported)",
)

#: What it looked like before bot.py forced its own stdout to utf-8 (v1.1.12):
#: the report never got printed at all.
CRASHED_LOG = """Traceback (most recent call last):
  File "bot.py", line 893, in <module>
    sys.exit(main())
  File "bot.py", line 646, in run_check
    print(f" {marks.get(state, '?')} {label.ljust(width)}  {detail}")
UnicodeEncodeError: 'charmap' codec can't encode character '\\u2713' in position 1
"""


def run_script(script: "str | Path", *args: str, encoding: str = CP1252,
               cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """Run a CI script the way a Windows runner would: stdout to a pipe, cp1252.

    ``script`` is a name under scripts/ or a path to any other file.
    """
    path = Path(script)
    target = path if path.is_absolute() or path.exists() else SCRIPTS / path
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    env.pop("PYTHONUTF8", None)
    return subprocess.run(
        [sys.executable, str(target), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=env, timeout=120,
    )


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# the encoding itself, so a green suite means the reproduction is real
# --------------------------------------------------------------------------- #

def test_an_unguarded_print_of_the_check_mark_dies_on_cp1252(tmp_path):
    """The control: this is what the release workflow's heredoc did.

    If this ever stops failing, the tests below have stopped reproducing the
    Windows runner and are no longer evidence of anything.
    """
    script = write(tmp_path, "unguarded.py", "print('PyNaCl is bundled: \\u2713 PyNaCl 1.5.0')\n")
    proc = run_script(script, encoding=CP1252, cwd=tmp_path)
    assert proc.returncode == 1, proc.stdout
    assert "UnicodeEncodeError" in proc.stderr


def test_the_guard_makes_the_same_print_survive(tmp_path):
    script = write(
        tmp_path, "guarded.py",
        "import sys; sys.path.insert(0, %r)\n"
        "from ci_stdio import force_utf8_stdio\n"
        "force_utf8_stdio()\n"
        "print('PyNaCl is bundled: \\u2713 PyNaCl 1.5.0')\n" % str(SCRIPTS),
    )
    proc = run_script(script, encoding=CP1252, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "\u2713 PyNaCl 1.5.0" in proc.stdout


class _UnreconfigurableCp1252Stream:
    """A stdout that cannot be reconfigured and cannot encode the check mark.

    This is the case force_utf8_stdio gives up on: a caller that replaced
    sys.stdout, an embedded interpreter. safe_print is what keeps a diagnostic
    alive anyway.
    """

    encoding = "cp1252"

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> int:
        encoded = text.encode("cp1252")  # raises on \u2713, as the real one does
        self.written.append(encoded.decode("cp1252"))
        return len(text)

    def flush(self) -> None:
        pass


def test_safe_print_degrades_when_the_stream_cannot_be_reconfigured(monkeypatch):
    stream = _UnreconfigurableCp1252Stream()
    monkeypatch.setattr(sys, "stdout", stream)
    ci_stdio.force_utf8_stdio()  # nothing to reconfigure here, and it must not raise
    ci_stdio.safe_print(" \u2713 PyNaCl  1.5.0 (voice encryption)")
    assert "".join(stream.written) == " \\u2713 PyNaCl  1.5.0 (voice encryption)\n"


def test_the_bot_prints_its_own_check_report_on_a_cp1252_stdout(tmp_path):
    """The app-side half of the same bug — the one bot.py fixed for v1.1.15.

    Worth pinning because the release job now exports ``PYTHONIOENCODING=utf-8``,
    which would hide a regression here, and because a user's Windows console is
    not UTF-8 either: ``lofi --check`` has to guard its own stdout or it dies
    mid-report, which is what made the gate below find no PyNaCl row at all on
    the v1.1.12-v1.1.14 builds.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = CP1252
    env.pop("PYTHONUTF8", None)
    env["LOFI_HOME"] = str(tmp_path)
    env["LOFI_TOKEN"] = "ci-dummy-token-not-used-for-anything"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bot.py"), "--check"],
        capture_output=True, cwd=str(ROOT), env=env, timeout=300,
    )
    report = proc.stdout.decode("utf-8", errors="replace")
    errors = proc.stderr.decode("utf-8", errors="replace")
    # ffmpeg and libopus are host tools: a runner without them exits 1, and that
    # is not what this test is about.
    assert proc.returncode in (0, 1), errors
    assert "UnicodeEncodeError" not in report + errors, errors
    assert "Lofi install check" in report, report + errors
    assert "\u2713" in report, "the report is UTF-8 even though the stream asked for cp1252"
    assert verify_voice.find_row(report) is not None, "no PyNaCl row for the release gate to read"


def test_every_script_ci_runs_guards_its_own_output():
    """One guard, three callers — and a fourth script that only prints ASCII is
    still a script somebody will add an em dash to."""
    for name in ("build.py", "verify_packages.py", "verify_voice.py"):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "force_utf8_stdio()" in source, f"scripts/{name} can die on its own output"


# --------------------------------------------------------------------------- #
# verify_voice.py — the gate that failed silently
# --------------------------------------------------------------------------- #

def test_the_pynacl_gate_passes_a_good_check_log_under_cp1252(tmp_path):
    """The regression, end to end: the row is found *and* reported.

    v1.1.12-v1.1.16 failed here, after finding the row, on the print of it.
    """
    log = write(tmp_path, "check.log", CHECK_LOG)
    proc = run_script("verify_voice.py", str(log))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PyNaCl is bundled and importable" in proc.stdout
    assert "1.5.0" in proc.stdout
    # Quoted back with its real mark, not an escape: it is force_utf8_stdio that
    # makes this line printable on a cp1252 runner, and a reader of a release log
    # should not have to decode \u2713 to see that voice works.
    assert "\u2713 PyNaCl" in proc.stdout
    assert "::error::" not in proc.stdout


def test_the_pynacl_gate_fails_when_voice_would_be_silently_disabled(tmp_path):
    """The failure the gate exists for: a binary that joins and plays nothing."""
    log = write(tmp_path, "check.log", NO_VOICE_LOG)
    proc = run_script("verify_voice.py", str(log))
    assert proc.returncode == 1
    assert "::error::" in proc.stdout, "a failed gate must be readable without the log"
    assert "cannot import PyNaCl" in proc.stdout + proc.stderr
    assert "nacl._sodium" in proc.stdout + proc.stderr, "the fix is a spec entry; say so"


def test_the_pynacl_gate_reports_a_binary_that_crashed(tmp_path):
    """No row at all is a crashed binary, and the traceback is the evidence."""
    log = write(tmp_path, "check.log", CRASHED_LOG)
    proc = run_script("verify_voice.py", str(log))
    assert proc.returncode == 1
    assert "crashed during --check" in proc.stdout + proc.stderr
    assert "UnicodeEncodeError" in proc.stdout + proc.stderr, "quote the log, do not summarise it"


def test_the_pynacl_gate_reports_a_log_that_was_never_written(tmp_path):
    proc = run_script("verify_voice.py", str(tmp_path / "absent.log"))
    assert proc.returncode == 1
    assert "never written" in proc.stdout + proc.stderr


def test_the_pynacl_gate_reports_an_empty_log(tmp_path):
    log = write(tmp_path, "check.log", "\n   \n")
    proc = run_script("verify_voice.py", str(log))
    assert proc.returncode == 1
    assert "empty" in proc.stdout + proc.stderr


def test_the_annotation_is_one_ascii_line(tmp_path):
    """Workflow commands end at a newline, and a runner's stdout is not ours to
    pick: the annotation survives both."""
    log = write(tmp_path, "check.log", NO_VOICE_LOG)
    proc = run_script("verify_voice.py", str(log))
    annotations = [line for line in proc.stdout.splitlines() if line.startswith("::error::")]
    assert len(annotations) == 1
    assert annotations[0].isascii(), annotations[0]
    assert "\n" not in annotations[0] and "%0A" in annotations[0], "multi-line, escaped"


def test_a_long_report_is_cut_at_a_line_boundary_and_says_so(tmp_path):
    """GitHub truncates an over-long annotation message of its own accord and
    says nothing about it, so the script cuts first and points at the job log."""
    rows = "\n".join(" \u2713 stations        " + "x" * 180 for _ in range(12))
    log = write(tmp_path, "check.log", NO_VOICE_LOG + rows + "\n")
    proc = run_script("verify_voice.py", str(log))
    assert proc.returncode == 1
    annotation = next(line for line in proc.stdout.splitlines() if line.startswith("::error::"))
    assert len(annotation) <= 900 + len("::error::"), len(annotation)
    assert annotation.endswith(verify_voice.TRUNCATED)
    # The human half still gets everything: the cap is GitHub's, not ours.
    assert "x" * 180 in proc.stderr


def test_a_host_dependency_is_not_blamed_on_the_spec(tmp_path):
    """``--label`` on ffmpeg or libopus is a debugging aid: those rows describe
    the runner, not the bundle, and the message must not imply otherwise."""
    log = write(tmp_path, "check.log", CHECK_LOG)
    proc = run_script("verify_voice.py", str(log), "--label", "libopus")
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "cannot import libopus" in combined
    assert "lofi.spec" not in combined, "a missing host tool is not a packaging bug"


def test_the_gate_reads_a_log_from_stdin(tmp_path):
    """So it can sit at the end of a pipe without a file in between."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "verify_voice.py"), "-"],
        input=CHECK_LOG, capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "importable" in proc.stdout


def test_the_gate_can_require_another_row(tmp_path):
    """ffmpeg is a host tool and must not gate a release; the mechanism is the
    same one, so it takes a label rather than a second script."""
    log = write(tmp_path, "check.log", CHECK_LOG)
    assert run_script("verify_voice.py", str(log), "--label", "ffmpeg").returncode == 0
    assert run_script("verify_voice.py", str(log), "--label", "libopus").returncode == 1


@pytest.mark.parametrize("text,expected", [
    (CHECK_LOG, " \u2713 PyNaCl          1.5.0 (voice encryption)"),
    (NO_VOICE_LOG, " \u2717 PyNaCl          not importable (Discord voice will not be supported)"),
    ("PyNaCl 1.5.0 (voice encryption)", "PyNaCl 1.5.0 (voice encryption)"),  # mark lost
    ("", None),
    ("nothing to see here", None),
])
def test_row_lookup_tolerates_a_mangled_mark(text, expected):
    """A mark that could not be encoded arrives as ``\\u2713``, or not at all;
    the label is what identifies the row."""
    assert verify_voice.find_row(text) == expected


def test_a_missing_mark_still_fails_on_the_wording():
    assert not verify_voice.row_is_ok("PyNaCl  not importable (Discord voice will not be supported)")
    assert not verify_voice.row_is_ok(" \u2717 PyNaCl  something else entirely")
    assert verify_voice.row_is_ok(" \u2713 PyNaCl  1.5.0 (voice encryption)")


# --------------------------------------------------------------------------- #
# verify_packages.py — the same landmine, in the failure path
# --------------------------------------------------------------------------- #

def test_the_package_verifier_names_a_bad_package_under_cp1252(tmp_path):
    """Its failure messages carry em dashes, so a *failing* verify used to be a
    UnicodeEncodeError too: exit 1, no artifact named, nothing to act on.

    The passing path is all ASCII, which is why this only ever bit on Windows
    when something else had already gone wrong.
    """
    import zipfile

    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "lofi-1.2.3-windows-amd64-setup.exe", "w") as zf:
        zf.writestr("EMULATED.txt", "placeholder")

    proc = run_script("verify_packages.py", str(dist))
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "FAIL" in combined, combined
    assert "EMULATED.txt" in combined, "say which artifact and why"
    assert "UnicodeEncodeError" not in combined, "the verifier died on its own message"


def test_the_package_verifier_still_passes_a_real_pe_under_cp1252(tmp_path):
    """The guard must not turn a good artifact into a failure."""
    import struct

    dist = tmp_path / "dist"
    dist.mkdir()
    installer = dist / "lofi-1.2.3-windows-amd64-setup.exe"
    e_lfanew = 0x80
    blob = bytearray(e_lfanew)
    blob[0:2] = b"MZ"
    blob[0x3C:0x40] = struct.pack("<I", e_lfanew)
    blob += b"PE\0\0" + struct.pack("<H", 0x014C) + struct.pack("<H", 1) + b"\0" * 16
    blob += struct.pack("<H", 0)
    installer.write_bytes(bytes(blob))

    proc = run_script("verify_packages.py", str(dist), "--expect", installer.name)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "verified as real" in proc.stdout
