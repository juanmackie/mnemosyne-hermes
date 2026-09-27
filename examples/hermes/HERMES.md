---
project: hermes-personal-agent
description: "Personal agent workspace: preferences, decisions, and task memory for a Hermes-powered assistant"
---

# Hermes Personal Agent

Memory workspace for a personal agent running a local Hermes model.

## Conventions

- Durable user preferences go in the `global` namespace.
- Project-specific facts go in per-project namespaces (auto-detected from
  `HERMES.md` / `AGENTS.md` / `CLAUDE.md` at the repo root).
- Run `mnemosyne-lite maintenance --auto-apply` periodically to remove exact
  duplicate memories (it previews the groups and asks first).
