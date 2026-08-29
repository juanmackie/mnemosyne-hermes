# Project-context bootstrap

`mnemosyne.bootstrap` is the sanctioned read path for building a bounded
startup package for a personal agent. It assembles existing Mnemosyne records;
it is not a second memory store and it never writes or promotes a candidate.

## CLI

```bash
mnemosyne bootstrap \
  --project /path/to/repository \
  --task "review the authentication changes" \
  --agent hermes \
  --capability security-review \
  --budget-tokens 3500 \
  --min-confidence 0.5
```

When `--namespace` is omitted, the repository project namespace is detected.
The same selection logic is available through the `mnemosyne.bootstrap` MCP
tool and the Hermes alias `mnemosyne_bootstrap`.

## Response channels

The JSON response is versioned as `bootstrap.v1` and keeps these channels
separate:

- `constraints`: active constraint/constitution memories. Existing manually
  created constraints remain compatible; extracted constraints must carry
  `constraint_status:approved` before they are returned.
- `facts`: confident project knowledge, excluding constraints and reasoning
  lessons.
- `guardrails`: failure-derived reasoning lessons, returned as fallible
  guidance rather than facts.
- `policies`: eligible interaction policies from the existing policy channel.
- `skills`: relevant project-local skill metadata. Skill content is not copied
  into bootstrap output; the agent may load the reported relative path.
- `abstentions`: explicit reasons a channel was empty or budget-limited.

Every memory item includes its source ID, namespace, confidence, and
provenance IDs when available. Ordering is deterministic and the approximate
token usage never exceeds `budget_tokens`.

## Constraint lifecycle

Create proposals from existing, evidence-bearing memories. The routed owner is
the only identity allowed to decide them:

```bash
mnemosyne constraint propose \
  --namespace project:myapp \
  --text "Do not modify production volumes" \
  --scope deployment \
  --priority 10 \
  --source-memory SOURCE_UUID \
  --evidence "Reviewer found production volumes are protected" \
  --proposer hermes \
  --owner alice

mnemosyne constraint list --namespace project:myapp --status proposed
mnemosyne constraint approve PROPOSAL_UUID --reviewer alice
mnemosyne constraint reject PROPOSAL_UUID --reviewer alice
mnemosyne constraint supersede APPROVED_UUID --reviewer alice
mnemosyne constraint export --namespace project:myapp --output .mnemosyne/CONSTRAINTS.md
```

Approval changes lifecycle state only; it does not mutate factual memory rows.
Only approved, unexpired proposals enter bootstrap. The Markdown file is a
reviewable projection and is never treated as a second source of truth.

## Safety contract

Bootstrap is read-only. It does not:

- create or update memories;
- turn extracted conversational text into active constraints;
- mix interaction policies into factual results;
- read global context when a project namespace was requested;
- load arbitrary files outside the explicitly supplied project root.
