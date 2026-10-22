"""Keep personal and business details out of public CI logs.

The repository is public, and on a public repository every Actions run log is
readable by anyone. The sync's own log lines carry Google Photos share links
(viewable by anyone holding the link), OneDrive URLs, employee names in event
folder and file names, vehicle VINs used as asset tags, and email addresses.

When running under GitHub Actions, every log record — message and traceback
alike — passes through ``redact`` before it is written. Local runs keep full
detail for debugging. ``REDACT_LOGS=true|false`` overrides either default.
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, Tuple

_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Any web address: share links, CDN images, OneDrive/SharePoint, Snipe-IT.
    (re.compile(r"(?:https?:)?//[^\s\"'<>]+"), "[link]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[email]"),
    # Vehicle asset tags are VINs: 17 characters, never I, O or Q.
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "[VIN]"),
    # The person in sync-generated names, which always put a date after them:
    #   "Check Out - Alex Rivera - 2026-07-17 09-01"
    #   "Chevrolet Silverado 2007 - Alex Rivera - 2026-07-17 09-01-45.jpg"
    # Stops at "/" so in a path only the last segment is considered.
    (re.compile(r" - [^/\n|]+? - (?=\d{4}-\d{2}-\d{2})"), " - [person] - "),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def should_redact() -> bool:
    override = os.environ.get("REDACT_LOGS", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


class RedactingFormatter(logging.Formatter):
    """Formats as usual, then redacts the finished line, traceback included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install() -> bool:
    """Wrap every root handler's formatter. Returns whether redaction is on."""
    if not should_redact():
        return False
    for handler in logging.getLogger().handlers:
        current = handler.formatter or logging.Formatter()
        handler.setFormatter(RedactingFormatter(current._fmt, current.datefmt))
    return True
