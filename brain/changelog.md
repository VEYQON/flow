---
type: changelog
---
# Changelog
- 2026-09-19 — Fork created at upstream 4a3189b; `.gitattributes` pins LF; harness installed. [[20-adr/ADR-001-fork-flow]]
- 2026-09-19 — F3: the model is told the date, time, zone and who it is speaking with, every turn; and a run now persists only the messages it produced. [[10-specs/f3-turn-context]]
- 2026-09-19 — E2: a linked agent's instructions are rebuilt every turn, so editing an agent reaches conversations already open. [[10-specs/e2-instructions-per-turn]]
- 2026-09-19 — E5: approval questions can be written in plain language on the tool record, and name the tool the way a person would. [[10-specs/e5-confirm-prompt-field]]
- 2026-09-19 — E5 v2: the approval question is substituted, not evaluated, and the exact arguments are always shown beneath it. Replaces the template engine v1 put in the approval path. [[10-specs/e5-confirm-prompt-field-v2]]
- 2026-09-20 — S14: a denial in a group of approvals executes nothing; refusing one of several actions shown together now refuses all of them. [[10-specs/s14-deny-stops-batch]]
- 2026-09-20 — S15: a resume that cannot honour an approval says so instead of echoing it, and never executes on an answer to a question nobody was asked. [[10-specs/s15-resume-fails-closed]]
