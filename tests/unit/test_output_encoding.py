"""Output-encoding hardening for non-UTF-8 consoles (Windows cp1251 etc.).

On Windows the console streams inherit a legacy code page, so printing any
non-ASCII character — our own ``→ — …`` notes, or arbitrary Unicode echoed
from the user's source files — used to die with ``UnicodeEncodeError``.
``_force_utf8_io`` reconfigures stdout/stderr to UTF-8 to prevent that.
"""
from __future__ import annotations

import io
import sys

from ast_outline.cli import _force_utf8_io


class _RecordingStream:
    """Minimal stream stand-in that remembers a ``reconfigure`` call."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self.reconfigured_to: str | None = None

    def reconfigure(self, *, encoding: str) -> None:
        self.reconfigured_to = encoding
        self.encoding = encoding


def test_reconfigures_non_utf8_stream(monkeypatch):
    """A cp1251-backed TextIOWrapper is switched to UTF-8 in place."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1251")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _force_utf8_io()

    assert out.encoding == "utf-8"
    assert err.encoding == "utf-8"


def test_leaves_utf8_stream_untouched(monkeypatch):
    """An already-UTF-8 stream is skipped — no needless reconfigure."""
    out = _RecordingStream("utf-8")
    err = _RecordingStream("UTF-8")  # case/hyphen variants normalise the same
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _force_utf8_io()

    assert out.reconfigured_to is None
    assert err.reconfigured_to is None


def test_skips_stream_without_reconfigure(monkeypatch):
    """A stream lacking ``reconfigure`` (like pytest's capture) is skipped
    silently — the helper must never raise on it."""
    out = io.StringIO()  # no .reconfigure attribute
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", out)

    _force_utf8_io()  # must not raise


def test_emoji_survives_cp1251_console(monkeypatch):
    """End-to-end: the exact crash path — an emoji that cp1251 cannot
    encode — now prints as UTF-8 bytes instead of raising."""
    buf = io.BytesIO()
    # newline="" so the wrapper does not translate "\n" to os.linesep —
    # on Windows that would add a "\r" and hide what this asserts.
    out = io.TextIOWrapper(buf, encoding="cp1251", newline="")
    monkeypatch.setattr(sys, "stdout", out)

    _force_utf8_io()
    print("🔁 → — …")  # U+1F501 is unencodable in cp1251
    sys.stdout.flush()

    assert buf.getvalue().decode("utf-8") == "🔁 → — …\n"
