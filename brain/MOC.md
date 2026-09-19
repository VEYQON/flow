---
type: moc
---
# VEYQON Flow — Map of Content

The hub. If a note isn't linked from here, it's lost.

## Start here
- [[30-loops/dev-loops|The 7 development loops]] — how work gets done
- [[40-architecture/engine-overview|Engine overview]] — how a turn flows through the engine
- [[70-runbooks/local-setup|Local setup]] — the bench, and how to rebuild it
- [[20-adr/ADR-001-fork-flow|ADR-001 Fork Flow]] — why this repo exists and its branch model

## Work in flight
- **Specs** → `10-specs/`
  - [[10-specs/f3-turn-context|F3 — per-turn context]] (draft)
  - [[10-specs/e5-confirm-prompt-field-v2|E5 v2 — an approval question a caller cannot steer]] (implemented 2026-09-19, awaiting review)
  - [[10-specs/e5-confirm-prompt-field|E5 v1]] (superseded by v2 — a template engine in the approval path)
- **Inbox** → `00-inbox/`
  - [[00-inbox/instructions-frozen-at-first-turn]]
  - [[00-inbox/worded-refusal-reproposes]]
  - [[00-inbox/stream-disconnect-loses-output]]

## Decisions
- **ADRs** → `20-adr/` — immutable. Supersede, never edit.

## Operations
- [[changelog]] · **Daily** → `80-daily/` · **Retros** → `90-retro/`
