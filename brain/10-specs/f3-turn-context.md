---
type: spec
status: draft        # draft → approved (HUMAN ONLY) → in-progress → implemented
created: 2026-09-19
upstreamable: yes
---
# Spec: F3 — per-turn context (date, time, time zone, user)

## Problem
The model never knows today's date, the time, the user's time zone, or who it is speaking with. Agent Q's
prompt has to tell it "you do not know today's date — always ask", which costs an extra exchange on
every date-dependent request. A wrong date began the 15 Sep 2026 production incident (a record posted
with a date nobody gave).

## Goals
- Every session turn, the model receives the current date (ISO + weekday), local time, the time zone
  those are expressed in, and the invoking user's full name.
- The information is always current — including on later turns and on resume after a pause.

## Non-goals
- Roles, company, permissions or any other user data. (Separate spec if wanted.)
- Code-only `Agent` runs without a session (`flow/lib/agent.py` `_build_initial_messages`).
- Changing how or when agent instructions are stored (see [[00-inbox/instructions-frozen-at-first-turn]]).
- Any change to the write-confirmation path.
- Changing Agent Q's prompt. That happens only after this is deployed to production.

## Current behaviour
- `flow_session.py:209` `_persist_turn` stores the agent's `instructions` as the system message on the
  first turn only.
- `flow_session.py:319` `_build_prompt_messages` rebuilds what the model is sent each turn and appends
  the memory block to the system message ephemerally (`:354`–`:360`); it inserts a system message if none exists.
- `flow_session.py:302` — `resume` also builds messages through `_build_prompt_messages` (read 19 Sep 2026).
- Nothing anywhere adds the date, time, time zone or user name.

## Proposed behaviour
In `_build_prompt_messages`, alongside the memory block and using the same pattern, append a short
context block to the system message (or insert one if absent). Never stored.

## Model-facing impact
The model sees, e.g.: "Current context: today is Friday, 2026-09-19. Local time is 10:42
(Europe/Berlin). You are speaking with Thivs Gobinath." It must not name any software, framework,
database, vendor or model.

## Acceptance criteria
1. With time frozen to a known instant, the system message sent to the model on a session turn contains
   that date in ISO format and its weekday name.
2. Two builds in the same session, time frozen to day 1 then to day 2, show day 1 then day 2.
3. The context block is never stored: after a turn, no stored session message contains the frozen date.
4. A session whose agent has no instructions and no memory still sends a system message with the context.
5. The time zone used is the user's own when set, otherwise the system time zone, and its name appears
   in the block; the local time shown is correct for that zone.
6. The block contains the invoking user's full name, and none of "frappe", "flow", "erpnext"
   (case-insensitive).
7. Upstream's existing suite stays green unmodified (in particular `TestAgentConfirmation`).

## Risks
- Time-zone conversion wrong around DST or midnight → wrong date sent confidently. Criterion 5 + QA.
- Token cost: roughly 30 tokens per turn.
- Prompt-injection surface: the user's full name is user-editable text placed in the system role. Quote it
  plainly; security-auditor to review.

## Open questions
- Which Frappe v16.31.0 functions give system tz, user tz and conversion? (Plan must cite file:line.)
- Which time-freezing utility exists in this Frappe version for tests?

## Links
[[40-architecture/engine-overview]] · [[20-adr/ADR-001-fork-flow]]

---
## Plan
<!-- Filled by /loop-plan. Max 8 tasks. -->
| # | Task | Files | Test | Satisfies AC |
|---|---|---|---|---|

**DO NOT CHANGE:** `flow/lib/agent.py` confirmation functions (`_invoke`, `_resolve_confirmation`,
`_confirmation_question`, `_has_denial`); `_persist_turn`; any doctype JSON.

**Rollback plan:**
