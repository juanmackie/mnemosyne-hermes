# Verification SELECT Query (P0 / P1 / P2)

The sqlite3 5-column SELECT criteria `[S1]`–`[S5]`:

```sql
SELECT
  (SELECT COUNT(*) FROM memory_events) AS S1_memory_events,
  (SELECT COUNT(*) FROM mutation_journal) AS S2_mutation_journal,
  (SELECT COUNT(*) FROM memory_integrity_runs) AS S3_integrity_runs,
  (SELECT COUNT(*) FROM memory_evidence) AS S4_memory_evidence,
  (SELECT COUNT(*) FROM audit_trail) AS S5_audit_trail;
```

Claim verified ONLY when:
- SELECT `[S1]` > 0
- `.auto/verify-claim.py` exit = 0
- Evidence file has `sha256`, `evidence_class`, `exit=0`
