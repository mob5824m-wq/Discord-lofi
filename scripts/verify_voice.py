#!/usr/bin/env python3
"""Fail the release when the packaged binary cannot do Discord voice.

``lofi --check`` is the diagnostic users get; this reads the log it produced and
gates the release on the one row that packaging — not the host — is responsible
for. ffmpeg and libopus are system packages and may legitimately be missing on a
build runner, but **PyNaCl** has to be *inside* the frozen binary: discord.py
imports ``nacl.bindings`` lazily, and when it is absent it silently drops voice
support, so the bot connects, joins the channel and plays nothing.

The row is checked in the release workflow's "Verify the artifact is the real
thing" step, right after ``scripts/verify_packages.py`` has established that the
installer is a real .deb/.dmg/.exe:

    "$BIN" --check > check.log 2>&1 || echo "--check exited non-zero (...)"
    python scripts/verify_voice.py check.log

It used to be an inline ``python - <<'PY'`` heredoc in the workflow. That is why
it is a script now: the heredoc printed the row it had found, the row starts with
``✓``, a Windows runner's Python encodes a redirected stream as cp1252, and the
print raised UnicodeEncodeError — exit code 1, no annotation, no message, on both
Windows jobs of every release from v1.1.12 to v1.1.16. A script can be run under
``PYTHONIOENCODING=cp1252`` in a test and prove it survives; a heredoc cannot.
See scripts/ci_stdio.py and tests/test_release_gates.py.

Exit code 0 = the row is there and PyNaCl imports. 1 = anything else, with a
``::error::`` annotation that carries the reason and the tail of the log, so the
failure is readable without opening the Actions log.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_stdio import ascii_safe, force_utf8_stdio, safe_print  # noqa: E402

#: The row that has to be there. bot.py prints it as `` ✓ PyNaCl  1.5.0 (…)``.
DEFAULT_LABEL = "PyNaCl"

#: What bot.py prints in that row's detail when the import failed.
NOT_IMPORTABLE = "not importable"

#: bot.py's state marks. A row carrying the missing mark has failed whatever its
#: wording says, so a reworded detail cannot sneak a broken binary past the gate.
MISSING_MARK = "\u2717"  # ✗

TRACEBACK = "Traceback (most recent call last)"

#: Why this one row gates a release, for the message that fails on it. Other
#: rows (ffmpeg, libopus) are host tools a build runner may legitimately lack,
#: so they get no hint — and `--label` on them is a debugging aid, not a gate.
PYNACL_HINT = (
    "Discord voice would be disabled: discord.py imports nacl.bindings lazily "
    "and silently drops voice when it is absent, so the bot joins the channel "
    "and plays nothing. lofi.spec must bundle nacl, nacl.bindings, nacl._sodium "
    "and _cffi_backend. "
)


def find_row(text: str, label: str = DEFAULT_LABEL) -> str | None:
    """The ``lofi --check`` line for ``label``, or None when there is none.

    Tolerates a mangled state mark: the mark is the first field and the label the
    second, but a mark that could not be encoded arrives as ``\\u2713`` (one field)
    or, on a terminal that dropped it, not at all — so the label is accepted in
    either position.
    """
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        if label in (fields[0], fields[1]):
            return line.rstrip()
    return None


def row_is_ok(row: str) -> bool:
    """True when the row says the dependency is really importable."""
    if NOT_IMPORTABLE in row:
        return False
    stripped = row.lstrip()
    return not stripped.startswith(MISSING_MARK)


def tail(text: str, lines: int = 12) -> str:
    """The last ``lines`` of the log — what a reader needs to see the cause."""
    kept = [line for line in text.splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


#: GitHub caps an annotation message well below a whole --check report, and a
#: message truncated by them loses its ending silently. Cut at a line boundary
#: instead, and say where the rest is.
ANNOTATION_LIMIT = 900
TRUNCATED = "(truncated - the job log has the whole report)"


def annotate(message: str, limit: int = ANNOTATION_LIMIT) -> None:
    """Print ``message`` as a GitHub Actions error annotation.

    One line, ASCII, percent-escaped: workflow commands are parsed from stdout,
    a newline ends the command, and a character the runner's stdout cannot
    encode would lose the very message this exists to deliver.
    """
    safe = ascii_safe(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if len(safe) > limit:
        budget = limit - len(TRUNCATED) - 3
        cut = safe.rfind("%0A", 0, budget)
        safe = (safe[:cut] if cut > 0 else safe[:budget]) + "%0A" + TRUNCATED
    print(f"::error::{safe}")


def evaluate(text: str | None, label: str = DEFAULT_LABEL, log_name: str = "check.log",
             tail_lines: int = 12) -> tuple[bool, str]:
    """``(ok, message)`` for the contents of a ``lofi --check`` log."""
    if text is None:
        return False, (
            f"{log_name} was never written, so the packaged binary produced no "
            f"--check output at all; it did not run"
        )
    if not text.strip():
        return False, (
            f"{log_name} is empty: the packaged binary printed nothing and "
            f"exited (a frozen binary that dies on startup does exactly this)"
        )

    row = find_row(text, label)
    if row is None:
        reason = (
            f"--check did not report the {label} runtime row"
            if TRACEBACK not in text
            else f"the packaged binary crashed during --check, so there is no {label} row"
        )
        return False, f"{reason}; the end of {log_name} says:\n{tail(text, tail_lines)}"

    if not row_is_ok(row):
        hint = PYNACL_HINT if label == DEFAULT_LABEL else ""
        return False, (
            f"the packaged binary cannot import {label}. {hint}"
            f"Row: {row.strip()}\n"
            f"The end of {log_name} says:\n{tail(text, tail_lines)}"
        )

    return True, f"{label} is bundled and importable: {row.strip()}"


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("log", help="file holding the output of `lofi --check` (`-` for stdin)")
    ap.add_argument("--label", default=DEFAULT_LABEL, help=f"row to require (default: {DEFAULT_LABEL})")
    ap.add_argument("--tail", type=int, default=12, help="log lines to quote when it fails")
    args = ap.parse_args(argv)

    name = args.log
    try:
        if name == "-":
            text = sys.stdin.read()
            name = "stdin"
        else:
            path = Path(name)
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError as exc:
        text = None
        name = f"{name} (unreadable: {exc})"

    ok, message = evaluate(text, label=args.label, log_name=name, tail_lines=args.tail)
    if ok:
        # Real glyphs, not escapes: this line lands in a release log a person
        # reads. safe_print only falls back when the stream cannot take them.
        safe_print(message)
        return 0

    annotate(message)
    # The annotation is the machine-readable half; this is the human one, on
    # stderr, for somebody running the script at a terminal.
    safe_print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
