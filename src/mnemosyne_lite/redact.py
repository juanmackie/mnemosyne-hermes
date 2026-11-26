"""Capture-time secret redaction for the lite store.

Stdlib only. The shapes are the ones the pinned engine already applies at its
own capture boundary (``_redact_capture_secrets`` in
``integrations/engine-patches/core-beam.py.patch``), plus the private-key block
that the MnemoBench capture guard matches. Using the same shapes on both
surfaces means text the engine refuses to store as-is is also redacted before
it reaches the lite store.
"""

import math
import re

REDACTED = "[REDACTED]"

# Ordered cheapest-first; overlap between the patterns is resolved below by
# keeping the earliest, longest span, so a labelled assignment wins over the
# high-entropy token inside its value.
_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    re.compile(
        r"\b(?:gh[pous]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16,})\b"
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|secret|token|credential|password)\s*[:=]\s*['\"]?"
        r"[A-Za-z0-9_./+=-]{16,}",
        re.IGNORECASE,
    ),
)

# A bare token-shaped blob of 32+ characters. Entropy decides: an ordinary long
# identifier (`a_very_long_descriptive_function_name`) is not in the class,
# and a hex/base64 secret is.
_HIGH_ENTROPY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9])")
_ENTROPY_MIN = 4.0


def _shannon_entropy(value: str) -> float:
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in (value.count(char) for char in set(value))
    )


def redact_capture_secrets(content: str) -> str:
    """Replace credential-shaped spans with ``[REDACTED]``, leaving the rest as-is.

    Every replacement is shorter than the span it replaces, so a caller's
    empty/whitespace/length validation belongs on the text passed in here, not
    on the result. Redaction is idempotent: no pattern matches its own output.
    """
    spans = [
        (match.start(), match.end()) for pattern in _PATTERNS for match in pattern.finditer(content)
    ]
    for match in _HIGH_ENTROPY_TOKEN_RE.finditer(content):
        if _shannon_entropy(match.group()) >= _ENTROPY_MIN:
            spans.append((match.start(), match.end()))
    if not spans:
        return content

    selected: list[tuple[int, int]] = []
    end = -1
    for start, stop in sorted(spans, key=lambda span: (span[0], -(span[1] - span[0]))):
        if start >= end:
            selected.append((start, stop))
            end = stop
    for start, stop in reversed(selected):
        content = content[:start] + REDACTED + content[stop:]
    return content
