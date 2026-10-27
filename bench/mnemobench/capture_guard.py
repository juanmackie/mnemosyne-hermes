"""Standalone deterministic secret capture guard (M4 v0; not provider-wired)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|secret|token)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{16,})", re.IGNORECASE
    ),
)


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    kind: str


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in (value.count(char) for char in set(value))
    )


def find_secrets(
    text: str, *, entropy_min_length: int = 32, entropy_threshold: float = 4.0
) -> list[Finding]:
    """Return sorted non-overlapping detector spans; caller decides persistence policy."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if entropy_min_length < 1 or not math.isfinite(entropy_threshold) or entropy_threshold < 0:
        raise ValueError("entropy limits must be positive and finite")
    found: list[Finding] = []
    for index, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            found.append(Finding(match.start(), match.end(), f"pattern_{index}"))
    token = re.compile(
        r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{" + str(entropy_min_length) + r",}(?![A-Za-z0-9])"
    )
    for match in token.finditer(text):
        if shannon_entropy(match.group()) >= entropy_threshold:
            found.append(Finding(match.start(), match.end(), "high_entropy"))
    found.sort(key=lambda item: (item.start, -(item.end - item.start)))
    result: list[Finding] = []
    end = -1
    for finding in found:
        if finding.start >= end:
            result.append(finding)
            end = finding.end
    return result


def redact(text: str, replacement: str = "[REDACTED]") -> tuple[str, int]:
    if not isinstance(replacement, str) or not replacement:
        raise ValueError("replacement must be non-empty text")
    spans = find_secrets(text)
    for finding in reversed(spans):
        text = text[: finding.start] + replacement + text[finding.end :]
    return text, len(spans)
