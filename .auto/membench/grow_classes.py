#!/usr/bin/env python3
"""Grow the two membench classes that had measurement headroom.

expired_facts and reference_noise sat at three queries each, which is too few to
tell a ranking change from one unlucky query. This appends seven verified items
per class per split: for expired_facts a current row (gold) plus an already
expired row saying something different (the distractor the retriever must
suppress), for reference_noise a new reference row whose fact competes with the
existing reference rows.

Run once; re-running is safe because it refuses to duplicate a query.
"""
from __future__ import annotations

import json
from pathlib import Path

REF_TAGS = {"heldout": ("hl7,docs,reference_only", "fhir,docs,reference_only"),
            "dev": ("k8s,docs,reference_only",)}

# (gold row that stays valid, row that already expired, query)
EXPIRED = {
 "heldout": [
  ("The transfusion log is countersigned by Dr. Anselm on the first Monday of each month.",
   "The transfusion log was countersigned by Dr. Rufe every Tuesday.",
   "who countersigns the transfusion log these days?", "Dr. Anselm"),
  ("The pathology courier collects specimens at 07:15 from the side entrance.",
   "The pathology courier collected specimens at 09:00 from the main doors.",
   "when does the pathology courier collect specimens now?", "07:15 from the side entrance"),
  ("This season the flu vaccination clinic runs in the community room.",
   "Last season the flu vaccination clinic ran in the old annex hall.",
   "where is the flu vaccination clinic held this season?", "community room"),
  ("The autoclave service contract sits with Vaporeon Technical.",
   "The autoclave service contract sat with Kestrel Autoclave Services.",
   "which company services the autoclave now?", "Vaporeon Technical"),
  ("Consultation interpreters are booked through the LinguaVia desk.",
   "Consultation interpreters were booked through the hospital switchboard.",
   "how do I book an interpreter for a consultation now?", "LinguaVia desk"),
  ("The clinical waste manifest is filed every Tuesday.",
   "The clinical waste manifest was filed at the end of each month.",
   "how often is the clinical waste manifest filed now?", "filed every Tuesday"),
  ("Overnight veterinary cover comes from the coastal night rota.",
   "Overnight veterinary cover came from the northern cooperative rota.",
   "who covers the clinic overnight now?", "coastal night rota"),
 ],
 "dev": [
  ("The controlled-drug count is witnessed by Pharmacist Okonjo every Friday.",
   "The controlled-drug count was witnessed by Pharmacist Aldridge every Wednesday.",
   "who witnesses the controlled-drug count now?", "Pharmacist Okonjo"),
  ("The ward refrigerator is read twice daily at 08:00 and 16:00.",
   "The ward refrigerator was read once daily at noon.",
   "how often is the ward refrigerator temperature read now?", "twice daily at 08:00 and 16:00"),
  ("Discharge prescriptions are dispensed from the ground floor bay.",
   "Discharge prescriptions were dispensed from the basement dispensary.",
   "where are discharge prescriptions dispensed now?", "ground floor bay"),
  ("The methotrexate counselling slot is run by the rheumatology pharmacy team.",
   "The methotrexate counselling slot was run by the ward nurses.",
   "who runs the methotrexate counselling slot now?", "rheumatology pharmacy team"),
  ("Ampoule sharps go in the purple-lid bin on the scullery shelf.",
   "Ampoule sharps went in the yellow bin in the treatment room.",
   "which bin do ampoule sharps go in now?", "purple-lid bin"),
  ("The supplier invoice cutoff is the 24th of each month.",
   "The supplier invoice cutoff was the last working day of the month.",
   "when is the supplier invoice cutoff now?", "24th of each month"),
  ("Drug queries outside opening hours go to the pharmacy duty line.",
   "Drug queries outside opening hours went to the site switchboard.",
   "where do I send drug queries outside opening hours now?", "pharmacy duty line"),
 ],
}

# (reference row, query, gold phrase unique to that row)
REFERENCE = {
 "heldout": [
  ("HL7 reference: a DFT financial transaction message posts a charge code against the patient visit with the billing status.",
   "which HL7 message posts a charge against a patient visit?", "posts a charge code"),
  ("HL7 reference: an MDM medical document report carries a dictated report as an encoded attachment with a document type code.",
   "which HL7 message carries a dictated report attachment?", "dictated report as an encoded attachment"),
  ("HL7 reference: an ORM order message sends the requested procedure code and the ordering clinician to the laboratory.",
   "which HL7 message places an order with the laboratory?", "requested procedure code"),
  ("HL7 reference: a BAR add billing account record opens the account for an encounter with account class and priority.",
   "which HL7 message opens a billing account for an encounter?", "opens the account for an encounter"),
  ("FHIR reference: a ServiceRequest resource records the procedure a clinician wants with intent, category and requester.",
   "which FHIR resource records a procedure the clinician has asked for?", "intent, category and requester"),
  ("FHIR reference: a Specimen resource records the collection time, specimen type and the container it arrived in.",
   "which FHIR resource says how a sample was collected and contained?", "the container it arrived in"),
  ("FHIR reference: an Encounter resource records the period, the location and the participants for one patient visit.",
   "which FHIR resource lists who took part in a patient visit?", "the participants for one patient visit"),
 ],
 "dev": [
  ("Kubernetes reference: a HorizontalPodAutoscaler scales a Deployment by watching a CPU utilisation target and a replica range.",
   "which Kubernetes object scales a deployment on a CPU target?", "CPU utilisation target"),
  ("Kubernetes reference: a PodDisruptionBudget protects a minimum available count so voluntary drains cannot go below it.",
   "which Kubernetes object stops a drain taking pods below a minimum?", "minimum available count"),
  ("Kubernetes reference: a NetworkPolicy selects ingress and egress peers by pod selector and namespace label.",
   "which Kubernetes object restricts which pods may talk to each other?", "ingress and egress peers"),
  ("Kubernetes reference: a PersistentVolumeClaim requests a storage class, an access mode and a capacity.",
   "which Kubernetes object asks for storage of a given class?", "storage class, an access mode and a capacity"),
  ("Kubernetes reference: a ConfigMap injects non-secret configuration into pods as environment variables or mounted files.",
   "where does a pod get non-secret configuration from?", "environment variables or mounted files"),
  ("Kubernetes reference: a Job runs a pod to completion with backoffLimit retries and an active deadline.",
   "which Kubernetes object runs a pod until it finishes, with retries?", "backoffLimit retries"),
  ("Kubernetes reference: a Service routes traffic to pods by matching a label selector and allocating a cluster IP.",
   "which Kubernetes object gives a set of pods a stable cluster IP?", "allocating a cluster IP"),
 ],
}


def main() -> int:
    for split in ("heldout", "dev"):
        cpath = Path(f".auto/membench/corpus_{split}.jsonl")
        qpath = Path(f".auto/membench/queries_{split}.jsonl")
        corpus = [json.loads(l) for l in cpath.read_text().splitlines() if l.strip()]
        queries = [json.loads(l) for l in qpath.read_text().splitlines() if l.strip()]
        seen = {q["query"] for q in queries}
        contents = [r["content"] for r in corpus]

        added_rows = added_queries = 0
        for i, (current, expired, query, gold) in enumerate(EXPIRED[split]):
            if query in seen:
                continue
            for text, row in ((current, {"content": current, "importance": 6, "memory_type": "fact",
                                         "tags": "current,cover", "age_days": 4}),
                              (expired, {"content": expired, "importance": 6, "memory_type": "task",
                                         "tags": "expired,cover", "age_days": 6,
                                         "expires_in_days": -(2 + i)})):
                assert text not in contents, f"duplicate row {text!r}"
                corpus.append(row)
            span = expired.rstrip(".")
            hits = [c for c in contents + [current, expired] if span.lower() in c.lower()]
            assert len(set(hits)) == 1, f"distractor label {span!r} is not unique to one row"
            queries.append({"query": query, "category": "expired_facts",
                            "relevant": [gold], "distractor": [span]})
            seen.add(query)
            added_rows += 2
            added_queries += 1

        for i, (row_text, query, gold) in enumerate(REFERENCE[split]):
            if query in seen:
                continue
            tags = REF_TAGS[split][i % len(REF_TAGS[split])]
            assert row_text not in contents, f"duplicate row {row_text!r}"
            corpus.append({"content": row_text, "importance": 4, "memory_type": "reference",
                           "tags": tags, "age_days": 30 + i})
            competitor = REFERENCE[split][(i + 1) % len(REFERENCE[split])][2]
            queries.append({"query": query, "category": "reference_noise",
                            "relevant": [gold], "distractor": [competitor]})
            seen.add(query)
            added_rows += 1
            added_queries += 1

        cpath.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in corpus) + "\n")
        qpath.write_text("\n".join(json.dumps(q, ensure_ascii=False) for q in queries) + "\n")
        print(f"{split}: +{added_rows} corpus rows, +{added_queries} queries "
              f"(now {len(corpus)} rows, {len(queries)} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
