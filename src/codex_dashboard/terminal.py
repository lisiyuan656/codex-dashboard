from __future__ import annotations

import re


_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b[@-_]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")


def sanitize_terminal_output(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "")
    cleaned = _OSC_RE.sub("", cleaned)
    cleaned = _CSI_RE.sub("", cleaned)
    cleaned = _ESC_RE.sub("", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    return cleaned
