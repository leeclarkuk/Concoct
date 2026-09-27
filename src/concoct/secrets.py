"""Secret registration, redaction and scanning.

Every credential Concoct handles is registered here so that log output and
error messages can be scrubbed, and so that generated files can be rejected
if they ever contain a live credential or something that looks like one.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

REDACTED = "***REDACTED***"

# Patterns for well-known credential formats. Deliberately conservative: false
# positives only block a commit (and trigger a repair), false negatives leak.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_api_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    "openai_api_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
}

_MIN_SECRET_LENGTH = 8


class SecretRegistry:
    """Thread-safe set of literal secret values that must never be emitted."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SECRET_LENGTH:
            with self._lock:
                self._values.add(value)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def values(self) -> list[str]:
        with self._lock:
            # Longest first so overlapping secrets are fully redacted.
            return sorted(self._values, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self.values():
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern in SECRET_PATTERNS.values():
            text = pattern.sub(REDACTED, text)
        return text


registry = SecretRegistry()


def redact(text: str) -> str:
    """Redact registered secrets and known credential patterns from ``text``."""
    return registry.redact(text)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    kind: str


def scan_text(path: str, text: str) -> list[SecretFinding]:
    """Return secret-like findings in ``text`` (registered values and patterns)."""
    findings: list[SecretFinding] = []
    literal_values = registry.values()
    for lineno, line in enumerate(text.splitlines(), start=1):
        for value in literal_values:
            if value in line:
                findings.append(SecretFinding(path, lineno, "configured_secret"))
        for kind, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                findings.append(SecretFinding(path, lineno, kind))
    return findings
