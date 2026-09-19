---
type: spec
status: implemented   # prototype built; NOT for merge
approved-by: owner pre-approval for unattended run 3 2026-09-19 — REVIEW BEFORE MERGE
created: 2026-09-19
kind: spike
adr: [[20-adr/ADR-002-specialist-approval-routing]]
---
# O2 — SPIKE: hand a specialist's approval request up to the person

> **Evidence only. Not a feature and not for merge.** This prototype exists so that ADR-002 can be
> decided against measurements instead of predictions. ADR-002 stays `status: proposed`.

## Why
The O1 spike ([[40-architecture/agent-handoff-findings]]) established that when a specialist pauses
for a write approval, the caller receives an empty string, tells the person the work is under way,
and the pending write is parked where nothing links it to the conversation. The person believes a
write happened; it did not, and nobody will ever be asked.

ADR-002 proposes that **a pause propagates**. This spike builds the smallest thing that makes that
true and tests seven invariants against it. If one cannot be met, the invariant is NOT bent — the
failure is recorded, because that is the most valuable output this task can produce.

## Invariants (the acceptance criteria)
- **I1** A specialist's pause raises exactly ONE approval question in the person's own conversation,
  naming the specialist and showing the exact arguments.
- **I2** Only the exact `"Approve"` executes. `"Deny"` and any free text do not execute, at any
  depth. The specialist's tool records zero calls before approval.
- **I3** The arguments executed are byte-for-byte the arguments shown — compared by digest. A
  specialist whose stored arguments change after the question was asked must NOT execute.
- **I4** Parent run and child run are linked in the stored records both ways.
- **I5** While anything is pending, the caller never tells the person the work is done or under way.
- **I6** Resume/approve runs as the owner of the parent run only; `assert_run_owner` holds across the
  hop and any other user is rejected.
- **I7** Depth is exactly one. A specialist that tries to delegate is refused.

## Proposed behaviour (the prototype)
1. `Flow Run` gains a nullable `parent_run` link (ADR-002 item 1), set when a run is created from
   inside another run's tool.
2. A hand-up helper (`flow/lib/handup.py`) runs the specialist in its own session. If that run
   completes, its output is returned to the caller's tool as today. If it **pauses**, the helper
   returns a `Question` instead of a string.
3. The agent loop already treats a `Question` returned by a tool as a pause (`_invoke`'s docstring
   says so, and `_loop` stamps `key = call.id`). **No change to `_invoke`,
   `_resolve_confirmation`, `_confirmation_question` or `_has_denial`.** The only change in
   `flow/lib/agent.py` is one new optional field on the `Question` dataclass carrying the hand-up
   record (child run, specialist label, argument digest).
4. On resume, the session routes each answer whose question carries a hand-up record DOWN to the
   child run first, verifying the argument digest before anything executes, and then lets the
   caller's own turn continue with the specialist's result. `"Deny"` is passed through unchanged so
   the existing denial halt still stops the caller.

## Files this work may touch
- `flow/lib/handup.py` (new)
- `flow/lib/agent.py` — the `Question` dataclass ONLY
- `flow/flow/doctype/flow_run/flow_run.json`, `flow_run.py` — the `parent_run` link
- `flow/flow/doctype/flow_session/flow_session.py` — `chat(parent_run=…)` and hand-up routing in
  `resume()`
- `flow/tests/test_handup_prototype.py` (new)
- `brain/40-architecture/approval-handup-prototype.md` (new), `brain/20-adr/ADR-002-…` (one line),
  `brain/MOC.md`, `brain/changelog.md`, this spec and its features file

## DO NOT CHANGE
- `_invoke`, `_resolve_confirmation`, `_confirmation_question`, `_has_denial`
- Options stay exactly `["Approve", "Deny"]`; `allow_other` unchanged
- Any existing test file. `TestAgentConfirmation` stays green and unmodified
- Any frontend file
- ADR-002's `status:` — it stays `proposed`

## Open questions for the owner
Recorded in [[40-architecture/approval-handup-prototype]] once measured.
