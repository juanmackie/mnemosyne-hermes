"""Grading of one probe's recall output against MnemoSim's exact labels.

Two independent surfaces are graded for every probe:

* the **tool** surface — what ``mnemosyne_recall`` returns (the agent's explicit
  lookup), and
* the **prefetch** surface — what the provider injects automatically.

Grading is pure: it takes the rows a surface returned and the probe's labels and
returns a fixed set of counts. No thresholds live here; thresholds belong to the
scorecard so that changing one cannot silently change a label.
"""

from __future__ import annotations

import re
from typing import Any

_NORMALIZE = re.compile(r"\s+")


PROVENANCE_PREFIX = "learned under "
# Non-greedy up to the next labelled segment, so a model id containing a
# comma is read whole instead of truncated.
_MODEL_IN_LINE = re.compile(r"learned under model (.+?)(?:, (?:engine|hermes) |, \d{4}-)")


def normalize(text: str) -> str:
    return _NORMALIZE.sub(" ", str(text or "")).strip().lower()


def _contains(text: str, needle: str) -> bool:
    """Whole-term match on normalized text.

    Plain substring matching is wrong for this corpus and was a real bug: the
    value "Vim" is a substring of "Neovim", so a row containing only the
    correction was graded as also containing the stale value (and vice versa).
    The boundary check keeps "Sunday 02:00 UTC" matching inside a longer
    sentence while refusing matches that run into another word character.
    """
    needle = normalize(needle)
    haystack = normalize(text)
    if not needle:
        return False
    start = haystack.find(needle)
    while start != -1:
        before = haystack[start - 1] if start else " "
        after_index = start + len(needle)
        after = haystack[after_index] if after_index < len(haystack) else " "
        # An alphanumeric character touching either edge means this is part of
        # a larger token ("vim" inside "neovim"), not the value itself.
        if not before.isalnum() and not after.isalnum():
            return True
        start = haystack.find(needle, start + 1)
    return False


def rank_of_first(texts: list[str], needle: str) -> int | None:
    """1-based rank of the first row containing `needle`, or None."""
    for index, text in enumerate(texts, start=1):
        if _contains(text, needle):
            return index
    return None


def grade(probe: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    """Grade one probe.

    `texts` are the returned rows in rank order. The returned dict is the unit
    the scorecard aggregates and bootstraps, so every field is per-probe.
    """
    kind = probe["kind"]
    answers = probe["answers"]
    stale = probe.get("stale_values") or []
    out: dict[str, Any] = {
        "probe_id": probe["probe_id"],
        "kind": kind,
        "returned_rows": len(texts),
        "answer_hit": False,
        "answer_rank": None,
        "fabricated_rows": 0,
        "expired_served": 0,
        "stale_ranked_above_current": 0,
        "answered_fabricated": False,
    }

    if kind == "fabricated":
        # Correct behaviour: nothing. Any row is a false answer for an entity
        # that exists nowhere in the store.
        out["fabricated_rows"] = len(texts)
        out["answered_fabricated"] = len(texts) > 0
        return out

    if kind == "expired":
        # The fact's validity window ended before `as_of`, and the store was
        # told the end date in the utterance. Serving any version of it is
        # serving an expired fact as current.
        #
        # A row only counts if it is about *this* probe's entity. Values are
        # drawn from a small per-attribute vocabulary, so another entity's
        # current fact can legitimately share the value ("tox" is one life's
        # test runner while another life's pytest row is also "tox"). Matching on
        # the value alone reported those unrelated rows as expired-fact leaks.
        entity = probe.get("entity") or ""
        served = sum(
            1
            for row in texts
            if (not entity or _contains(row, entity))
            and any(_contains(row, value) for value in stale)
        )
        out["expired_served"] = served
        return out

    # answerable_current / answerable_historical
    for answer in answers:
        rank = rank_of_first(texts, answer)
        if rank is not None:
            out["answer_hit"] = True
            out["answer_rank"] = (
                rank if out["answer_rank"] is None else min(out["answer_rank"], rank)
            )
    if kind == "answerable_current" and out["answer_rank"] is not None:
        # Current-truth probe: a superseded value must not outrank the current
        # one. Same entity gate as `expired`: another entity's row carrying the
        # same value is not this probe's stale fact.
        entity = probe.get("entity") or ""
        for value in stale:
            stale_rank = None
            for index, row in enumerate(texts, start=1):
                if (not entity or _contains(row, entity)) and _contains(row, value):
                    stale_rank = index
                    break
            if stale_rank is not None and stale_rank <= out["answer_rank"]:
                out["stale_ranked_above_current"] = 1
                break
    return out


def grade_prefetch(probe: dict[str, Any], context: str) -> dict[str, Any]:
    """Grade the injected prefetch block.

    Prefetch is a single string, so ranks are meaningless and the tool surface's
    fields do not all apply. The block is therefore graded on the facts it puts
    in front of the model, and the record carries the same canonical field names
    as the tool surface so both can be aggregated identically:

    * ``answer_hit`` / ``answer_present`` — the right fact reached the block;
    * ``answered_fabricated`` / ``fabricated_present`` — the block injected
      content for an entity that exists nowhere (any injected recall content is
      a false premise, since the entity cannot be in the store);
    * ``expired_served`` / ``expired_present`` — an ended validity window was
      injected;
    * ``stale_ranked_above_current`` / ``stale_present`` — for a current-truth
      probe, a superseded value was injected without the current one.
    """
    kind = probe["kind"]
    out: dict[str, Any] = {
        "probe_id": probe["probe_id"],
        "kind": kind,
        "chars": len(context or ""),
        "answer_present": False,
        "fabricated_present": False,
        "expired_present": False,
        "stale_present": False,
        # canonical fields shared with the tool surface
        "answer_hit": False,
        "answer_rank": None,
        "answered_fabricated": False,
        "fabricated_rows": 0,
        "expired_served": 0,
        "stale_ranked_above_current": 0,
        "returned_rows": 0,
    }
    if not context:
        return out
    out["returned_rows"] = context.count("\n- ") + context.count("\n[")
    if kind == "fabricated":
        # The block must not present anything as answering this entity. Any
        # injected recall content does, because the entity is uninventable.
        out["fabricated_present"] = bool(context.strip())
        out["answered_fabricated"] = out["fabricated_present"]
        out["fabricated_rows"] = out["returned_rows"] or 1
        return out
    answers = probe["answers"]
    out["answer_present"] = any(_contains(context, answer) for answer in answers)
    out["answer_hit"] = out["answer_present"]
    stale = probe.get("stale_values") or []
    entity = probe.get("entity") or ""
    entity_present = not entity or _contains(context, entity)
    if kind == "expired":
        out["expired_present"] = entity_present and any(
            _contains(context, value) for value in stale
        )
        out["expired_served"] = int(out["expired_present"])
    elif kind == "answerable_current":
        stale_injected = any(_contains(context, value) for value in stale)
        out["stale_present"] = stale_injected and not out["answer_present"]
        out["stale_ranked_above_current"] = int(stale_injected and not out["answer_present"])
    return out


def prefetch_entries(context: str) -> list[tuple[str, str]]:
    """Pair each injected row with the provenance line that follows it.

    The injected format puts a row line (`  [timestamp] …`) and, when the row
    carries provenance, one indented `learned under …` line directly after it.
    Pairing is what lets the lesson probe check *its own* row's provenance
    instead of any line in the block.
    """
    entries: list[tuple[str, str]] = []
    pending: tuple[str, str] | None = None
    for line in (context or "").splitlines():
        if line.startswith("  ["):
            if pending is not None:
                entries.append(pending)
            pending = (line, "")
        elif line.strip().startswith(PROVENANCE_PREFIX) and pending is not None:
            entries.append((pending[0], line.strip()))
            pending = None
        elif pending is not None:
            entries.append(pending)
            pending = None
    if pending is not None:
        entries.append(pending)
    return entries


def grade_provenance(probe: dict[str, Any], entries: list[tuple[str, str]]) -> dict[str, Any]:
    """Provenance fields for one carried-lesson probe.

    `entries` are (row text, provenance line or "") pairs. A lesson is
    `provenance_present` when the row that answers the probe carries a line, and
    `wrong_model` counts lines that name a model other than the one the lesson
    was written under. With one lesson per life, every provenance line in the
    store names that lesson's `model_a`, so a differing name is a real violation
    rather than another row's legitimate origin.
    """
    answer = (probe.get("answers") or [""])[0]
    model_a = str(probe.get("model_a") or "")
    lines = [line for _, line in entries if line]
    lesson_lines = [line for text, line in entries if line and _contains(text, answer)]
    wrong_model = 0
    for line in lines:
        match = _MODEL_IN_LINE.search(line)
        if match and model_a and match.group(1).strip() != model_a:
            wrong_model += 1
    return {
        "provenance_present": bool(lesson_lines),
        "provenance_lines": len(lines),
        "wrong_model": wrong_model,
    }
