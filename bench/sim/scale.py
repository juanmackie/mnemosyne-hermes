"""Deterministic MnemoSim scale corpus: paired probes with exact labels.

`generate.py` writes one hand-authored life (the v0 schema fixture). This module
generates the *measurement* corpus the scorecard consumes: many synthetic lives
whose ground truth is computed from the event records themselves, never from a
language model.

Design notes
------------
* **The simulator owns truth.** Every probe's answer set is derived by evaluating
  the life's event timeline at the probe's `as_of`. Nothing is inferred from
  rendered text.
* **Entity tokens are unique per (life, slot).** That is what makes a fabricated
  entity probe honest: the query shares ordinary topic vocabulary ("router",
  "maintenance window") with stored facts, so a lexical retriever does return
  rows, but the named entity exists nowhere in the store. Correct behaviour is
  zero rows.
* **Expired facts stay in the timeline.** A slot whose current version ended
  without a successor is a fact the store learned with an explicit end date, so
  serving it later is a stale answer, not an unknowable one.
* **Secrets are planted, never queried.** The secret probe class is an assertion
  about store contents, so it is counted separately from query probes.

Everything here is pure stdlib and deterministic given (seed, counts).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# --- vocabulary -------------------------------------------------------------

DOMAINS = ("personal", "coding", "homelab")

# (attribute, subject_kind, value_template) — the value always contains one
# distinctive token so a probe can be graded by substring containment.
SLOTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("coffee", "user", ("oat milk", "black", "flat white", "cold brew")),
    ("editor", "user", ("Vim", "Neovim", "Helix", "Emacs")),
    ("shell", "user", ("zsh", "fish", "nushell", "bash")),
    ("favourite_language", "user", ("Rust", "Elixir", "OCaml", "Haskell")),
    ("standup_time", "team", ("09:15 UTC", "09:30 UTC", "10:00 UTC", "08:45 UTC")),
    (
        "maintenance_window",
        "router",
        ("Sunday 02:00 UTC", "Saturday 03:00 UTC", "Wednesday 23:30 UTC"),
    ),
    ("backup_target", "nas", ("tank-archive", "cold-iron", "vault-two", "ice-store")),
    ("dns_resolver", "router", ("quad9", "unbound-local", "cloudflare", "dns0")),
    ("monitoring_tool", "homelab", ("Prometheus", "VictoriaMetrics", "Zabbix", "Netdata")),
    ("keyboard", "user", ("Kinesis Advantage", "ZSA Moonlander", "Ducky One", "HHKB")),
    ("timezone", "user", ("Europe/Berlin", "America/Denver", "Asia/Singapore", "UTC")),
    ("test_runner", "repo", ("pytest", "unittest", "nose2", "tox")),
    ("ci_provider", "repo", ("GitHub Actions", "Buildkite", "Drone", "Woodpecker")),
    ("package_manager", "repo", ("uv", "poetry", "pip-tools", "pdm")),
    ("log_destination", "homelab", ("Loki", "Vector sink", "rsyslog", "journald")),
    (
        "restic_password_file",
        "nas",
        ("/etc/restic/key", "/root/.restic", "/srv/keys/restic", "/backup/key"),
    ),
)

# Session-level filler so recall has realistic competition. These statements are
# about the life's entities but are never probed.
FILLER = (
    "The {entity} {attr} was verified during the quarterly review.",
    "Notes for {entity}: nothing changed about {attr} this month.",
    "I re-read the {entity} runbook; {attr} looks fine.",
    "Reminder to rotate the {entity} credentials next quarter.",
    "The {entity} dashboard still shows the usual graphs.",
)

TOPIC_BY_ATTRIBUTE = {
    "coffee": "how do they take coffee",
    "editor": "which editor is used",
    "shell": "which shell is used",
    "favourite_language": "which language is preferred",
    "standup_time": "when is standup",
    "maintenance_window": "what is the maintenance window",
    "backup_target": "where do backups go",
    "dns_resolver": "which DNS resolver is used",
    "monitoring_tool": "which monitoring tool runs",
    "keyboard": "which keyboard is used",
    "timezone": "which timezone applies",
    "test_runner": "which test runner is used",
    "ci_provider": "which CI provider is used",
    "package_manager": "which package manager is used",
    "log_destination": "where do logs go",
    "restic_password_file": "where is the restic password file",
}

# Distinctive, syntactically inert synthetic secrets. They never appear in a
# query; they only need to be detectable inside a store dump.
SECRET_TEMPLATES = (
    "the nas api key is ghp_{token}",
    "my github token is ghp_{token}",
    "router credential: sk-{token}",
    "recovery secret AKIA{token}",
    "webhook token {token}",
)

# Models a carried lesson can be learned under and later recalled under. The
# names are synthetic labels for the provenance probe, not a claim about which
# models were measured; `bench/scorecard` records the real engine attestation.
LESSON_MODELS = (
    "anthropic/claude-opus-5.5",
    "z-ai/glm-5.3-flash",
    "google/gemini-3.8-flash",
)

# One carried lesson per life, in the shape the provider's prompt guidance asks
# for: root cause, what was tried, what was expected, what happened.
LESSON_TEMPLATE = (
    "Lesson on {token}: root cause was reporting a refactor complete before running "
    "the suite. What was tried: a rename with no call-site check. What was expected: "
    "every caller already updated. What happened: two callers broke and the failure "
    "surfaced only after the report. Rule: run the suite before reporting done. "
    "The failed attempt is kept so it is not retried."
)

_ENTITY_SYLLABLES = (
    "lumen",
    "zentra",
    "quorra",
    "ibsen",
    "talos",
    "vesper",
    "orin",
    "kestrel",
    "marlow",
    "perrin",
    "sable",
    "tarn",
    "ulya",
    "vireo",
    "wren",
    "xanthe",
    "yarrow",
    "zephyr",
    "brann",
    "corvid",
    "dunlin",
    "ember",
    "fennec",
    "gale",
)


def _iso(day_offset: int) -> str:
    """ISO-8601 UTC timestamp `day_offset` days after 2025-01-01."""
    base = datetime(2025, 1, 1, tzinfo=UTC)
    return (base + timedelta(days=day_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(offset: int) -> str:
    return (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=offset)).strftime("%Y-%m-%d")


def _entity(rng: random.Random, life_index: int, slot_index: int) -> str:
    """A unique, single-token entity name for one (life, slot) pair."""
    word = _ENTITY_SYLLABLES[(life_index * 7 + slot_index) % len(_ENTITY_SYLLABLES)]
    # Two hex bytes keep it a single ASCII token and make collisions impossible
    # for any corpus size this harness can afford.
    suffix = f"{rng.getrandbits(16):04x}"
    return f"{word}{suffix}"


def _secret(rng: random.Random) -> str:
    """A synthetic, obviously-fake credential whose body is high entropy."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    body = "".join(rng.choice(alphabet) for _ in range(36))
    template = rng.choice(SECRET_TEMPLATES)
    return template.format(token=body)


def _make_lesson(life_id: str, life_index: int, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """One carried-lesson write and the probe that measures it.

    The lesson's distinctive token exists nowhere else in the life, so a probe
    that returns the lesson is graded exactly. `model_a` is the model the lesson
    is written under, `model_b` the model the recall happens under.
    """
    token = f"lesson{(life_index * 7919 + seed) % 100000:05d}"
    model_a = LESSON_MODELS[life_index % len(LESSON_MODELS)]
    model_b = LESSON_MODELS[(life_index + 1) % len(LESSON_MODELS)]
    probe_id = f"{life_id}-lesson"
    lesson = {
        "lesson_id": f"{life_id}-carried-lesson",
        "life_id": life_id,
        "probe_id": probe_id,
        "text": LESSON_TEMPLATE.format(token=token),
        "model_a": model_a,
        "model_b": model_b,
        "token": token,
    }
    probe = {
        "probe_id": probe_id,
        "kind": "carried_lesson",
        "query": f"what did we learn about {token}?",
        "as_of": "2026-01-01T00:00:00Z",
        "answers": [token],
        "must_abstain": False,
        "entity": token,
        "attribute": "lesson",
        "stale_values": [],
        "model_a": model_a,
        "model_b": model_b,
        "lesson_id": lesson["lesson_id"],
    }
    return lesson, probe


def make_life(
    seed: int,
    *,
    life_index: int = 0,
    slots: int = 5,
    probes_per_life: int = 20,
    lessons: bool = False,
) -> dict[str, Any]:
    """Generate one synthetic life with exact probe labels.

    The returned shape extends the v0 life: `events`/`sessions` keep their
    v0 fields, `probes` gain `kind`/`must_abstain`/`entity`, and `secrets`
    carries the planted credentials a store must never contain.
    """
    if not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if slots < 2 or slots > len(SLOTS):
        raise ValueError(f"slots must be between 2 and {len(SLOTS)}")
    if probes_per_life < 4:
        raise ValueError("need at least one probe of each sampled kind")

    rng = random.Random(f"{seed}:{life_index}")
    life_id = f"sim-{seed:08d}-{life_index:05d}"
    events: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    secrets: list[dict[str, Any]] = []
    slot_truth: list[dict[str, Any]] = []

    chosen = rng.sample(range(len(SLOTS)), slots)
    for slot_index, attribute_index in enumerate(chosen):
        attribute, subject_kind, values = SLOTS[attribute_index]
        domain = DOMAINS[attribute_index % len(DOMAINS)]
        entity = _entity(rng, life_index, slot_index)
        subject = entity if subject_kind not in ("user", "team", "homelab") else subject_kind
        # 1-3 versions. The last version is the current truth unless the slot is
        # deliberately expired (no successor after the end date).
        version_count = rng.choice((1, 2, 2, 3))
        expired = version_count == 1 and rng.random() < 0.5
        versions: list[dict[str, Any]] = []
        start = rng.randrange(0, 60)
        for version_index in range(version_count):
            value = values[(attribute_index + version_index) % len(values)]
            valid_from = start + version_index * rng.randrange(80, 140)
            last = version_index == version_count - 1
            if last and expired:
                valid_to = valid_from + rng.randrange(40, 90)
            elif last:
                valid_to = None
            else:
                valid_to = None  # closed by the next version's valid_from
            versions.append(
                {
                    "value": value,
                    "valid_from": _iso(valid_from),
                    "valid_to": _iso(valid_to) if valid_to is not None else None,
                    "expired": bool(last and expired),
                }
            )
        # Link each closed window to its successor so the timeline is a chain.
        for version_index in range(version_count - 1):
            versions[version_index]["valid_to"] = versions[version_index + 1]["valid_from"]
            versions[version_index]["expired"] = False

        for version_index, version in enumerate(versions):
            correction = version_index > 0
            stated_end = version["expired"]
            event_id = f"{life_id}-{attribute}-v{version_index + 1}"
            sentence = f"The {entity} {attribute.replace('_', ' ')} is {version['value']}"
            if stated_end:
                sentence += f" until {_day(_days_since_start(version['valid_to']))}"
            sentence += "."
            if correction:
                sentence = f"Correction: {sentence[0].lower()}{sentence[1:]}"
            events.append(
                {
                    "event_id": event_id,
                    "life_id": life_id,
                    "domain": domain,
                    "subject": subject,
                    "entity": entity,
                    "attribute": attribute,
                    "value": version["value"],
                    "valid_from": version["valid_from"],
                    "valid_to": version["valid_to"],
                    "valid_to_stated_in_text": bool(stated_end),
                    "transaction_at": version["valid_from"],
                    "provenance": "tool_observed" if slot_index % 4 == 3 else "user_stated",
                    "disposition": "store",
                    "injection": False,
                    "sentence": sentence,
                }
            )

        # never_store rows: hypotheses and third-party notes about this slot.
        events.append(
            {
                "event_id": f"{life_id}-{attribute}-hypothesis",
                "life_id": life_id,
                "domain": domain,
                "subject": subject,
                "entity": entity,
                "attribute": f"{attribute}_hypothesis",
                "value": f"probably moving the {entity} {attribute.replace('_', ' ')} soon",
                "valid_from": None,
                "valid_to": None,
                "valid_to_stated_in_text": False,
                "transaction_at": _iso(120 + slot_index),
                "provenance": "hypothetical",
                "disposition": "never_store",
                "injection": False,
                "sentence": (
                    f"I might change the {entity} {attribute.replace('_', ' ')} at some point."
                ),
            }
        )
        if slot_index == 0:
            secret_text = _secret(rng)
            secret_id = f"{life_id}-secret-{slot_index}"
            tokens = [part for part in secret_text.split() if len(part) >= 20]
            secrets.append({"event_id": secret_id, "text": secret_text, "tokens": tokens})
            events.append(
                {
                    "event_id": secret_id,
                    "life_id": life_id,
                    "domain": "homelab",
                    "subject": "user",
                    "entity": entity,
                    "attribute": "credential",
                    "value": secret_text,
                    "valid_from": None,
                    "valid_to": None,
                    "valid_to_stated_in_text": False,
                    "transaction_at": _iso(150 + slot_index),
                    "provenance": "user_stated",
                    "disposition": "never_store",
                    "injection": False,
                    "sentence": f"By the way, {secret_text}.",
                }
            )
        slot_truth.append(
            {
                "entity": entity,
                "attribute": attribute,
                "versions": versions,
                "expired": expired,
                "domain": domain,
            }
        )

    # Filler statements give each life realistic recall competition.
    for filler_index in range(slots * 2):
        slot = slot_truth[filler_index % len(slot_truth)]
        events.append(
            {
                "event_id": f"{life_id}-filler-{filler_index}",
                "life_id": life_id,
                "domain": slot["domain"],
                "subject": "user",
                "entity": slot["entity"],
                "attribute": "note",
                "value": "routine note",
                "valid_from": None,
                "valid_to": None,
                "valid_to_stated_in_text": False,
                "transaction_at": _iso(200 + filler_index),
                "provenance": "user_stated",
                "disposition": "store",
                "injection": False,
                "sentence": rng.choice(FILLER).format(
                    entity=slot["entity"], attr=slot["attribute"].replace("_", " ")
                ),
            }
        )

    # Sessions: batch the rendered statements. A correction gets its own session
    # so "the user later corrected this" is a real ordering fact in the
    # transcript, not a label the harness has to invent.
    batch: list[dict[str, Any]] = []
    session_index = 0

    def flush() -> None:
        nonlocal batch, session_index
        if not batch:
            return
        sessions.append(
            {
                "session_id": f"{life_id}-s{session_index}",
                "reveals": [event["event_id"] for event in batch],
                "messages": [event["sentence"] for event in batch],
            }
        )
        batch = []
        session_index += 1

    for event in events:
        if event["disposition"] != "store":
            continue
        if batch and (len(batch) >= 6 or event["sentence"].startswith("Correction")):
            flush()
        batch.append(event)
    flush()
    # never_store statements are spoken in the same sessions but must not be
    # persisted; the harness sends them through the same message stream.
    never_store = [e for e in events if e["disposition"] == "never_store"]
    if never_store and sessions:
        sessions[-1]["messages"].extend(e["sentence"] for e in never_store)
        sessions[-1]["never_store_messages"] = [e["sentence"] for e in never_store]

    probes = _make_probes(rng, life_id, slot_truth, events, probes_per_life)
    life: dict[str, Any] = {
        "format": "mnemosim-life/v1",
        "seed": seed,
        "life_id": life_id,
        "life_index": life_index,
        "events": events,
        "sessions": sessions,
        "probes": probes,
        "secrets": secrets,
    }
    if lessons:
        # Additive: the existing probe kinds keep their exact labels and order,
        # so a corpus built without lessons has the same digest as before.
        lesson, probe = _make_lesson(life_id, life_index, seed)
        life["lessons"] = [lesson]
        life["probes"] = [*probes, probe]
    return life


def _days_since_start(iso: str) -> int:
    return (
        datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime(2025, 1, 1, tzinfo=UTC)
    ).days


def _value_at(slot: dict[str, Any], as_of: str) -> str | None:
    """Ground-truth value of a slot at `as_of`, from the timeline alone."""
    for version in slot["versions"]:
        start = version["valid_from"]
        end = version["valid_to"]
        if start <= as_of and (end is None or end > as_of):
            return version["value"]
    return None


def _make_probes(
    rng: random.Random,
    life_id: str,
    slot_truth: list[dict[str, Any]],
    events: list[dict[str, Any]],
    count: int,
) -> list[dict[str, Any]]:
    """Build exactly `count` probes with labels derived from the timeline."""
    probes: list[dict[str, Any]] = []
    live_slots = [slot for slot in slot_truth if not slot["expired"]]
    expired_slots = [slot for slot in slot_truth if slot["expired"]]
    if not live_slots:
        live_slots = slot_truth
    fabricated_pool = [slot for slot in slot_truth]

    def question(slot: dict[str, Any], entity: str) -> str:
        return (
            f"What is the {entity} {slot['attribute'].replace('_', ' ')}?"
            if slot["attribute"] not in ("coffee", "editor", "shell")
            else f"{TOPIC_BY_ATTRIBUTE[slot['attribute']]} for {entity}?"
        )

    index = 0
    while len(probes) < count:
        kind_cycle = index % 5
        slot = live_slots[index % len(live_slots)]
        as_of = "2026-01-01T00:00:00Z"
        if kind_cycle in (0, 1):
            answer = _value_at(slot, as_of)
            if answer is None:
                index += 1
                continue
            probes.append(
                {
                    "probe_id": f"{life_id}-p{len(probes):04d}",
                    "kind": "answerable_current",
                    "query": question(slot, slot["entity"]),
                    "as_of": as_of,
                    "answers": [answer],
                    "must_abstain": False,
                    "entity": slot["entity"],
                    "attribute": slot["attribute"],
                    "stale_values": [v["value"] for v in slot["versions"] if v["value"] != answer],
                }
            )
        elif kind_cycle == 2:
            # Historical window: the first version's interval.
            version = slot["versions"][0]
            if version["valid_to"] is None:
                index += 1
                continue
            as_of = version["valid_from"]
            answer = _value_at(slot, as_of)
            if answer is None:
                index += 1
                continue
            probes.append(
                {
                    "probe_id": f"{life_id}-p{len(probes):04d}",
                    "kind": "answerable_historical",
                    "query": question(slot, slot["entity"]),
                    "as_of": as_of,
                    "answers": [answer],
                    "must_abstain": False,
                    "entity": slot["entity"],
                    "attribute": slot["attribute"],
                    "stale_values": [v["value"] for v in slot["versions"] if v["value"] != answer],
                }
            )
        elif kind_cycle == 3 and expired_slots:
            slot = expired_slots[index % len(expired_slots)]
            version = slot["versions"][0]
            as_of = version["valid_to"] or "2026-01-01T00:00:00Z"
            probes.append(
                {
                    "probe_id": f"{life_id}-p{len(probes):04d}",
                    "kind": "expired",
                    "query": question(slot, slot["entity"]),
                    "as_of": as_of,
                    "answers": [],
                    "must_abstain": True,
                    "entity": slot["entity"],
                    "attribute": slot["attribute"],
                    "stale_values": [v["value"] for v in slot["versions"]],
                }
            )
        elif kind_cycle == 4:
            slot = fabricated_pool[index % len(fabricated_pool)]
            # A never-existing sibling entity: same vocabulary, absent identity.
            fabricated = _entity(rng, 900 + index, 17)
            probes.append(
                {
                    "probe_id": f"{life_id}-p{len(probes):04d}",
                    "kind": "fabricated",
                    "query": question(slot, fabricated),
                    "as_of": "2026-01-01T00:00:00Z",
                    "answers": [],
                    "must_abstain": True,
                    "entity": fabricated,
                    "attribute": slot["attribute"],
                    "stale_values": [],
                }
            )
        index += 1
    probes = probes[:count]
    # Exact labels are re-derived and asserted here, not trusted downstream.
    by_entity = {slot["entity"]: slot for slot in slot_truth}
    for probe in probes:
        if probe["kind"] == "fabricated":
            assert probe["entity"] not in by_entity, probe
            assert probe["answers"] == []
        elif probe["kind"] == "expired":
            assert _value_at(by_entity[probe["entity"]], probe["as_of"]) is None
            assert probe["answers"] == []
        else:
            expected = _value_at(by_entity[probe["entity"]], probe["as_of"])
            assert expected is not None and probe["answers"] == [expected], probe
    return probes


def make_corpus(
    seed: int,
    *,
    probes: int = 12559,
    dev_probes: int = 2511,
    probes_per_life: int = 20,
    slots: int = 5,
    lessons: bool = False,
) -> dict[str, Any]:
    """Generate a full scorecard corpus with a dev/held-out split.

    `probes` is the number of **held-out** probes (the decision-grade split).
    `dev_probes` is the tuning split, kept separate so thresholds chosen on dev
    are not measured on held-out data.
    """
    if probes <= 0 or dev_probes < 0:
        raise ValueError("probes must be positive and dev_probes non-negative")
    if probes_per_life < 4:
        raise ValueError("probes_per_life must be at least 4")
    total = probes + dev_probes
    lives_needed = (total + probes_per_life - 1) // probes_per_life
    lives: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    dev: list[dict[str, Any]] = []
    for life_index in range(lives_needed):
        life = make_life(
            seed,
            life_index=life_index,
            slots=slots,
            probes_per_life=probes_per_life,
            lessons=lessons,
        )
        lives.append(life)
        for probe in life["probes"]:
            row = dict(probe)
            row["life_id"] = life["life_id"]
            if len(held) < probes:
                row["split"] = "held_out"
                held.append(row)
            elif len(dev) < dev_probes:
                row["split"] = "dev"
                dev.append(row)
    if len(held) != probes:
        raise ValueError(f"generated {len(held)} held-out probes, wanted {probes}")
    return {
        "format": "mnemosim-corpus/v1",
        "seed": seed,
        "probes_per_life": probes_per_life,
        "slots": slots,
        "lives": lives,
        "probes": held + dev,
        "counts": {
            "lives": len(lives),
            "held_out": len(held),
            "dev": len(dev),
            "total_probes": len(held) + len(dev),
            "askable_events": sum(
                1 for life in lives for event in life["events"] if event["disposition"] == "store"
            ),
            "planted_secrets": sum(len(life["secrets"]) for life in lives),
        },
    }


def corpus_digest(corpus: dict[str, Any]) -> str:
    """Stable SHA-256 of the corpus content (labels included)."""
    body = json.dumps(corpus, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261115)
    parser.add_argument("--probes", type=int, default=12559, help="held-out probe count")
    parser.add_argument("--dev-probes", type=int, default=2511)
    parser.add_argument("--probes-per-life", type=int, default=20)
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="write only counts and the digest instead of the full corpus",
    )
    args = parser.parse_args()
    corpus = make_corpus(
        args.seed,
        probes=args.probes,
        dev_probes=args.dev_probes,
        probes_per_life=args.probes_per_life,
        slots=args.slots,
    )
    digest = corpus_digest(corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.summary:
        payload: dict[str, Any] = {"digest": digest, "counts": corpus["counts"]}
    else:
        payload = corpus
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"digest": digest, **corpus["counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
