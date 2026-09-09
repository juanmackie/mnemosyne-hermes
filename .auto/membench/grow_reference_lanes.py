#!/usr/bin/env python3
"""Put the documentation queries in the lane that can answer them.

`memory_type = reference` with the `reference_only` tag is bulk documentation, and
the default recall lane drops it on purpose (RecallScope::Memory in
src/utils/retrieval.rs) so vendor docs cannot crowd out facts. Every item in the
old reference_noise class asked for a fact *inside* such a document, so the class
scored correct suppression as failure - it read 0.17 at n=10 for that reason.

Two classes now:
  reference_docs   - documentation questions, queried with --scope reference.
  reference_noise  - personal reference facts (memory_type=reference, untagged)
                     asked while a same-vocabulary document exists. This is what
                     "noise" always meant: the doc must not crowd out the fact.
"""
from __future__ import annotations

import json
from pathlib import Path

# (personal fact, competing documentation row, query, gold phrase, distractor phrase)
PERSONAL = {
 "heldout": [
  ("The clinic's HL7 field mappings are kept in mapping-clinical.csv inside the integrations folder.",
   "ADT A01 event signals a patient admission",
   "which file holds the clinic's HL7 field mappings?", "mapping-clinical.csv", "ADT A01"),
  ("Laboratory results arrive through the Dalbert interface and its credentials live at secret/clinic/dalbert.",
   "observation result message carries segments",
   "where do the laboratory interface credentials live?", "secret/clinic/dalbert", "observation result message"),
  ("The clinic's FHIR endpoint is https://clinic.example/fhir and it is reachable only over the VPN.",
   "Observation resource links a subject",
   "what is our FHIR endpoint address?", "https://clinic.example/fhir", "Observation resource links"),
 ],
 "dev": [
  ("The settlement worker image is built by the release workflow in .github/workflows/release.yaml.",
   "RollingUpdate strategy",
   "which workflow builds the settlement worker image?", "workflows/release.yaml", "RollingUpdate strategy"),
  ("The payments cluster kubeconfig is stored in Bitwarden under Kubernetes/payments-cluster.",
   "kubelet evicts by priority class",
   "where is the payments cluster kubeconfig stored?", "Kubernetes/payments-cluster", "kubelet evicts"),
  ("Our staging namespace is named payments-stage and shares the second node pool.",
   "node selector",
   "what is our staging namespace called?", "payments-stage", "node selector"),
 ],
}


def dump(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def main() -> int:
    for split in ("heldout", "dev"):
        cpath = Path(f".auto/membench/corpus_{split}.jsonl")
        qpath = Path(f".auto/membench/queries_{split}.jsonl")
        corpus = [json.loads(l) for l in cpath.read_text().splitlines() if l.strip()]
        queries = [json.loads(l) for l in qpath.read_text().splitlines() if l.strip()]
        contents = [r["content"] for r in corpus]
        seen = {q["query"] for q in queries}

        moved = 0
        for item in queries:
            if item.get("category") == "reference_noise" and "scope" not in item:
                item["category"] = "reference_docs"
                item["scope"] = "reference"
                moved += 1

        added = 0
        for i, (fact, doc, query, gold, distractor) in enumerate(PERSONAL[split]):
            if query in seen:
                continue
            assert fact not in contents, f"duplicate row {fact!r}"
            assert any(doc.lower() in c.lower() for c in contents), f"no doc row matches {doc!r}"
            corpus.append({"content": fact, "importance": 6, "memory_type": "reference",
                           "tags": "personal,endpoint", "age_days": 12 + i})
            queries.append({"query": query, "category": "reference_noise",
                            "relevant": [gold], "distractor": [distractor]})
            seen.add(query)
            added += 1

        dump(cpath, corpus)
        dump(qpath, queries)
        print(f"{split}: {moved} items moved to the reference lane, +{added} crowding items, "
              f"{len(corpus)} rows, {len(queries)} queries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
