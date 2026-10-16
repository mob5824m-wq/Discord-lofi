#!/usr/bin/env python3
"""One place that makes a CI script's own output encoding-independent.

Why this exists
---------------
Python picks the encoding of a *redirected* stream from the platform locale,
and on a Windows GitHub runner that locale is cp1252. So the first character
outside Latin-1 that a script prints — an em dash in an error message, the ``✓``
that ``lofi --check`` puts in front of every row — raises::

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u2713'

and the script dies with exit code 1 *after* swallowing whatever it was about to
say. That is the worst shape a CI failure can take: the step that was supposed
to explain the release turns into "Process completed with exit code 1" with no
annotation, because the line that would have become the annotation is the line
that could not be encoded.

It cost five releases. Both Windows build jobs failed at "Verify the artifact is
the real thing" on every push to main from v1.1.12 to v1.1.16 while Linux and
macOS passed, so nothing was published and the only visible clue was the exit
code. ``bot.py`` had the same bug and fixed it for itself in ``main()`` and
``setup_logging()``; the release workflow's own inline Python still had it.

Every script that CI runs calls :func:`force_utf8_stdio` first. The workflows
also export ``PYTHONIOENCODING=utf-8`` (release.yml, on the build job) because
that covers interpreters this file is not imported into — but the guard here is
the half that still works when somebody runs the script by hand on Windows.
"""

from __future__ import annotations

import sys

__all__ = ["ascii_safe", "force_utf8_stdio", "safe_print"]


def force_utf8_stdio() -> None:
    """Point stdout/stderr at UTF-8 with an error handler that cannot raise.

    ``backslashreplace`` rather than ``strict``: a diagnostic that cannot be
    encoded must still be *printed*, as ``\\u2713`` if need be. Losing the glyph
    is recoverable; losing the message is what hid this for five releases.

    Never raises. A stream that cannot be reconfigured (already detached, an
    embedded interpreter, stdout replaced by a test harness) is left alone —
    the caller still gets its output through, and :func:`ascii_safe` is there
    for the lines that must survive any encoding at all.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # ValueError, OSError, io.UnsupportedOperation, ...
            continue


def ascii_safe(text: str) -> str:
    """``text`` with every non-ASCII character escaped, for messages that must
    survive a stdout this module could not reconfigure."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def safe_print(text: str, *, file=None) -> None:
    """``print`` that degrades to escaped ASCII instead of raising.

    :func:`force_utf8_stdio` makes this unnecessary in practice; it is here
    because the alternative — a diagnostic that dies on its own message — is the
    failure that cost five releases, and a stream that cannot be reconfigured
    (detached, replaced by a caller) is not hypothetical. Real glyphs when they
    can be encoded, ``\\u2713`` when they cannot, a printed line either way.
    """
    try:
        print(text, file=file)
    except UnicodeEncodeError:
        print(ascii_safe(text), file=file)
