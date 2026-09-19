---
type: spec
status: implemented     # draft → approved (HUMAN ONLY) → in-progress → implemented
implemented: 2026-09-19
approved-by: owner pre-approval for unattended run 2026-09-19 — REVIEW BEFORE MERGE
created: 2026-09-19
upstreamable: no
---
# Spec: O1 — spike: can one agent hand work to another?

## Problem
Agent Q is one agent with one growing tool list and one set of instructions. The obvious next shape is
a generalist that hands a piece of work to a specialist and gets an answer back. Before any of that is
designed, four things have to stop being guesses: whether a tool inside a run can start another agent's
run at all, what a *write approval* inside the specialist does to the caller, whose permissions the
specialist's tools run with, and whether the two runs are linked in the record at all.

This is a **research spike**. Its deliverable is evidence and a draft decision, not a feature. No
production behaviour changes. No bug found here is fixed here.

## Goals
- Answer (a)-(d) below as facts, each backed by a named test that passes on this branch, or explicitly
  marked INFERRED / UNKNOWN when a test could not establish it.
- Leave behind a findings note and a DRAFT ADR proposing how a specialist's approval request could
  reach the person talking to Agent Q.

## Non-goals
- Shipping a handoff tool, a `Flow Agent` field, or any doctype change.
- FIXING anything the spike discovers. Findings become inbox items, not commits.
- Calling a real model. The spike uses the suite's existing `FakeModel`.
- Any change to the write-confirmation path (`flow/lib/agent.py`), to `flow_run.py`, or to
  `flow_session.py`. The spike is **test-only**.

## Current behaviour
<!-- Every claim below was read on 19 Sep 2026 at the commit this branch was cut from. -->
- `flow/api/api.py:21` `start_run(input, agent=None, session=None, ...)` is the whitelisted entry
  point; it calls `new_session(agent, model=model)` or `load_session(...)` and then `convo.chat(...)`.
- `flow/lib/session.py:18` `new_session(agent=...)` accepts a code `Agent`, a `Flow Agent` doc, an
  agent NAME, or `None`; `_resolve_new_agent` (:58) returns the runtime for each. A code agent leaves
  `Flow Session.agent` empty (:63).
- `flow/lib/agent.py:_invoke` returns a `Question` for a `requires_confirmation` tool unless
  `auto_approve`; `_loop` then returns `RunResult(paused=True, questions=[...])`.
- `flow/flow/doctype/flow_run/flow_run.py:_status_from_result` maps `paused` → status "Paused".
- `flow/api/api.py:resume_run` (:42) resumes by **run name**, after `assert_run_owner`
  (`flow/lib/session.py:106`), and refuses a run that is not Paused.
- `Flow Run` has fields `session, source, trigger, reference_doctype, reference_name, status,
  iterations, input, output, tool_calls, questions, usage, config_snapshot, error, feedback_*`.
  There is **no** parent-run or caller-run link field.
- `flow/triggers/triggers.py:74-75` switches identity with `frappe.set_user(t.run_as or t.owner)` for
  the length of a trigger run.

## Proposed behaviour
None. This spike adds ONE new test file, `flow/tests/test_spike_agent_handoff.py`, whose tests are
written as *characterisation* tests: each asserts what the engine does today, so the file doubles as a
tripwire if the answer ever changes.

## Model-facing impact
None. No model-facing string is added or changed. The spike's own tool descriptions exist only inside
the test file and are never shipped; they still avoid naming any platform, vendor or model, so a later
copy-paste cannot leak one.

## Acceptance criteria
1. A test proves a tool executing inside agent A's run can start a run on a **code-defined** `Agent` B
   (through the same path `start_run` uses) and return B's final output as A's tool result, where A
   then uses it in its answer.
2. A test proves the same for a **doctype `Flow Agent`** B, started by name through that same path.
3. A test establishes what A receives, and what A's run does, when B's run PAUSES for a write
   approval: the value returned to A, A's own run status, the `Flow Run` row B's pause created and its
   status, and whether `resume_run` on B's run is reachable by the invoking user.
4. A test establishes whose identity B's tools execute under — the user who invoked A, or another.
5. A test establishes whether any field on either `Flow Run` row links A's run to B's run.
6. `brain/40-architecture/agent-handoff-findings.md` exists, and every finding in it is marked
   VERIFIED (naming the test method that establishes it), INFERRED (naming the code read), or UNKNOWN.
7. A DRAFT ADR exists in `brain/20-adr/` with `status: proposed`, proposing how a specialist's approval
   request reaches the person talking to Agent Q. It is not accepted; the owner decides.
8. `scripts/run-tests.sh` prints `GATE=GREEN` with `MIN_TESTS=586`, and no file outside
   `flow/tests/test_spike_agent_handoff.py`, `brain/`, and this spec's own features file is changed.

## Risks
- **A spike that quietly becomes a feature.** Mitigated by the non-goals and by AC8's file list.
- **A characterisation test that pins a BUG as correct.** Mitigated by naming, in the test docstring,
  which assertions describe behaviour the spike considers wrong.
- **The nested run reusing the outer run's state.** `_set_active_run` (`flow_session.py`) is a
  process-global flag that the outer `chat()` sets and clears in a `finally`. A nested run inside a
  tool will set it to B's run and clear it to `None` — so the OUTER run's memory writes after the tool
  returns may lose their `source_run` stamp. The spike must look for this and report it, not fix it.
- Test isolation: a nested `chat()` writes rows inside the test transaction. Use the suite's own
  helpers so rollback still works.

## Open questions
- Does a nested run inside a tool deadlock on the `Flow Session` row lock? `chat()` commits before the
  model call only when `not frappe.flags.in_test`, so a test may not exercise the production locking.
  If so, record it as UNKNOWN-IN-TEST rather than claiming safety.

## Links
[[40-architecture/engine-overview]] · [[20-adr/ADR-001-fork-flow]] · [[MOC]]

---
## Outcome
Ten tests in `flow/tests/test_spike_agent_handoff.py`, all green at `GATE=GREEN`, `MIN_TESTS=596`.
Findings: [[40-architecture/agent-handoff-findings]]. Draft decision:
[[20-adr/ADR-002-specialist-approval-routing]] — `status: proposed`, NOT accepted.

**The answer in one line:** handoff already works, runs as the invoking user throughout, and does
not weaken the approval gate — but when the specialist needs a write approved, nobody is ever asked,
the caller tells the person the work is under way, and the pending write is left parked where
nothing links it to the conversation.

**Nothing was fixed.** Three things this uncovered are recorded, not solved: the un-asked approval
(the ADR's subject), the nested run clearing the outer run's memory-source flag
(`TestTheNestedRunClobbersTheOuterRunsMemoryStamp`), and the absence of any link between the two
`Flow Run` rows.
