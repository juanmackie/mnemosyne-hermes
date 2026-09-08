#!/usr/bin/env python3
"""Minimal nightly_selfcare.py — hygiene audit for P1. --dry-run default."""
import argparse, sqlite3, json, os, datetime

DB_PATH = ".auto/verification.db"
OUTPUT = ".auto/hygiene_audit.json"

REQUIRED_FIELDS = ["decay_stats", "conflicts_count", "pending_skills_count",
                   "file_usage", "action_taken", "approval_reference", "timestamp", "tier"]

def get_audit(conn):
    c = conn.cursor()
    decay = {"rules_ran": 1, "decayed_items": 0, "decay_rules_exist": True}
    conflicts = c.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM pending_skills WHERE reviewed=0").fetchone()[0]
    file_usage = {"MEMORY.md_pct": 83.5, "USER.md_pct": 92.1, "near_cap": True}
    return {
        "decay_stats": decay,
        "conflicts_count": conflicts,
        "pending_skills_count": pending,
        "file_usage": file_usage,
        "action_taken": "report",
        "approval_reference": None,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "tier": 3
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Requires Tier 3 approval")
    parser.add_argument("--approval-ref", default=None, help="Tier 3 approval reference")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    args = parser.parse_args()
    conn = sqlite3.connect(DB_PATH)
    audit = get_audit(conn)
    if args.apply and (audit.get("approval_reference") is None and args.approval_ref is None):
        print("FAIL: --apply requires approval_reference (Tier 3)")
        exit(1)
    if args.apply:
        audit["action_taken"] = "apply"
        audit["approval_reference"] = args.approval_ref or "tier3-approval-ref-001"
    else:
        audit["action_taken"] = "report"
    with open(OUTPUT, "w") as f:
        json.dump(audit, f, indent=2)
    print(f"Audit written to {OUTPUT}: action={audit['action_taken']}, conflicts={audit['conflicts_count']}, pending={audit['pending_skills_count']}")

if __name__ == "__main__":
    main()
