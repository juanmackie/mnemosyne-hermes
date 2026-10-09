"""Capped, cited result cards for the lite surface.

Leviathan-inspired presentation over the existing FTS5/BM25 recall:
one call returns a handful of ranked rows, each with an id, its
namespace, importance, and a `match:` snippet from the text that matched.
A header reports `shown N of M` so agents can tell "no match" from
"no data". Truncation is always marked with `…`, never silent.

Pure functions only: no DB access, no I/O. The CLI (`--format cards`)
and the MCP `text` field share this renderer.

`max_chars` caps each memory content body. Optional context metadata has its
own 160-character line cap. Context is free-form descriptive metadata, not a
verified citation. Created dates are rendered as UTC calendar dates.
"""

import re
from datetime import UTC, datetime

# A character the FTS5 unicode61 tokenizer keeps: a letter or a digit.
_TOKEN_RE = re.compile(r"[^\W_]", re.UNICODE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

DEFAULT_CARD_CHARS = 500
DEFAULT_SNIPPET_CHARS = 160
DEFAULT_METADATA_CHARS = 160


def query_terms(query: str) -> list[str]:
    """Lowercased search terms, longest first, deduplicated.

    Longest first so the snippet search prefers the most specific term.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for word in _WORD_RE.findall(query.lower()):
        if word not in seen:
            seen.add(word)
            terms.append(word)
    terms.sort(key=len, reverse=True)
    return terms


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ").replace("\t", " ")).strip()


def truncate(text: str, limit: int) -> str:
    """Cap `text` at `limit` chars, marking the cut with `…`."""
    if limit < 1:
        limit = 1
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_snippet(content: str, query: str, width: int = DEFAULT_SNIPPET_CHARS) -> str:
    """Return a `width`-capped window around the first query-term hit.

    The window is marked with `…` wherever it was cut. When no term occurs
    literally (stemming matched a variant), the leading window is returned.
    """
    flat = _single_line(content)
    if not flat:
        return ""
    if width < 1:
        width = 1
    lowered = flat.lower()
    best = -1
    for term in query_terms(query):
        at = lowered.find(term)
        if at != -1 and (best == -1 or at < best):
            best = at
            if best == 0:
                break
    if best == -1:
        return truncate(flat, width)
    half = width // 3
    start = max(0, best - half)
    end = min(len(flat), start + width)
    if end - start < width:
        start = max(0, end - width)
    window = flat[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    # If the window itself was cut short of `width` only by stripping,
    # still mark an outer cut so callers never mistake it for full text.
    if prefix or suffix:
        return (prefix + window + suffix).strip() or window
    return truncate(window, width)


def _card_body(index: int, row: dict, query: str, max_chars: int) -> list[str]:
    row_id = row.get("id", "-")
    namespace = row.get("namespace", "-")
    importance = row.get("importance", "-")
    lines = [f"[{index}] {row_id} · {namespace} · importance {importance}"]
    body = truncate(_single_line(str(row.get("content", ""))), max_chars)
    lines.append(f"  {body}" if body else "  -")
    if query:
        lines.append(f"  match: {build_snippet(str(row.get('content', '')), query)}")
    context = _single_line(str(row.get("context") or ""))
    if context:
        lines.append(
            truncate(
                "  context (metadata): " + context,
                DEFAULT_METADATA_CHARS,
            )
        )
    created = _utc_date(row.get("created_at"))
    if created:
        lines.append(f"  created (UTC): {created}")
    return lines


def _utc_date(value: object) -> str | None:
    """Format a SQLite Unix timestamp as a deterministic UTC calendar date."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def render_recall_cards(
    rows: list[dict],
    query: str,
    total: int,
    indexed_total: int,
    max_chars: int = DEFAULT_CARD_CHARS,
    *,
    total_is_bounded: bool = False,
) -> str:
    """Render recall rows; `max_chars` caps each content body only."""
    shown = len(rows)
    if total_is_bounded:
        count_label = f"shown {shown} · bounded keyword fallback returned {total} candidates"
    else:
        count_label = f"shown {shown} of {total}"
    header = f'mnemosyne-lite recall · query "{query}" · {count_label} · {indexed_total} memories indexed'
    if not rows:
        return header + "\n(no matches; try fewer or different words)"
    out = [header]
    for i, row in enumerate(rows, start=1):
        out.extend(_card_body(i, row, query, max_chars))
    return "\n".join(out)


def render_list_cards(
    rows: list[dict],
    indexed_total: int,
    max_chars: int = DEFAULT_CARD_CHARS,
) -> str:
    """Render list rows as cards (no query, so no `match:` line)."""
    header = f"mnemosyne-lite list · shown {len(rows)} · {indexed_total} memories indexed"
    if not rows:
        return header + "\n(no memories found)"
    out = [header]
    for i, row in enumerate(rows, start=1):
        out.extend(_card_body(i, row, "", max_chars))
    return "\n".join(out)
