---
type: architecture
measured: 2026-09-19 against veyqon (upstream 4a3189b + harness), Frappe v16.31.0
spike: [[10-specs/o1-agent-handoff-spike]]
---
# Can one agent hand work to another? — findings

Every finding is marked **VERIFIED** (a named test in `flow/tests/test_spike_agent_handoff.py`
establishes it), **INFERRED** (read from code, not executed) or **UNKNOWN**. Ten tests, all green
at `GATE=GREEN`, `MIN_TESTS=596`.

## The short answer
Handoff already works, and it works better than expected in the two places that would have been
expensive to fix — permissions and the approval gate itself. It fails in exactly one place, and
that place is bad: **when the specialist needs a write approved, the person is never asked.**

## (a) A tool inside agent A's run can start a run on agent B and return B's answer

**VERIFIED** — `TestHandoffToACodeAgent.test_a_tool_can_run_a_code_agent_and_return_its_answer`
and `TestHandoffToARecordDefinedAgent.test_a_tool_can_run_a_record_defined_agent_by_name`.

Nothing needs building for the happy path. A tool calls `new_session(B).chat(question)` — the same
entry `start_run` uses — and returns `run.output`. That string becomes A's tool result and A
answers from it. Works identically for a code-defined `Agent` object and for a `Flow Agent` record
passed by name.

- B gets its OWN instructions, not A's. **VERIFIED** (the second test asserts on what B's model was
  sent).
- B runs on its own `Flow Session`, distinct from A's. **VERIFIED**.
- A record-defined B's session carries the `agent` link; a code-defined B's does not. **VERIFIED**.
- A code agent's session cannot rebuild its own runtime, so resuming one requires passing the same
  `Agent` object back in. **VERIFIED** (used in
  `TestTheParkedApprovalCanStillBeAnswered.test_approving_the_orphaned_run_by_id_executes_the_write`).

## (b) When B pauses for a write approval — THE FINDING

**VERIFIED** — `TestWhatHappensWhenTheSpecialistNeedsApproval` (two tests).

What actually happens, in order:
1. B's run pauses correctly. Its `Flow Run` row is `Paused` and carries the question.
   **The write does NOT execute.** The approval gate is not weakened by being nested.
2. **A's tool receives an empty string.** A paused run's `output` is `None`, so a tool written the
   obvious way returns `""`.
3. **A never learns a pause happened.** A's run completes normally and A answers the user — in the
   test, "I have asked." The user is told the work is under way when it is parked.
4. **A's run carries no question**, so no approval is ever put in front of anyone. **VERIFIED**:
   `questions` on the outer run is `None`.
5. **B's paused run is orphaned.** It is a real, owned, resumable row, but nothing links it to the
   conversation the person is in: listing paused runs for A's session returns `[]`. **VERIFIED**.

So the failure is silent, and it is the worst shape a failure can take around writes: the person
believes something was done, and a pending write sits where only a database query can find it.

**Can the user's approval ever reach it through A? Not today — but only routing is missing.**
**VERIFIED** — `TestTheParkedApprovalCanStillBeAnswered`. Handed B's run id, the documented resume
path works: "Approve" executes the write and B finishes; "Deny" stops it without executing. The
approval machinery survives the handoff intact. Nothing about `_resolve_confirmation` needs to
change. What is missing is that nobody is ever shown the question.

## (c) Whose permissions B's tools run with

**VERIFIED** — `TestWhoseIdentityTheSpecialistRunsAs` (two tests).

The invoking user's, throughout. There is no identity switch anywhere in a handoff: `frappe.session.user`
inside B's tool equals the user who invoked A, and both sessions and both runs are owned by that
user. No elevation, and no drop either.

This is the good news that shapes the design: **a fix that surfaces B's question through A does not
have to solve a permissions problem.** The owner checks that guard resume (`assert_run_owner`) already
pass for the invoking user — **VERIFIED**, the spike calls `assert_run_owner` on the orphaned run and
it does not raise.

Contrast, and the one case that DOES switch identity: a trigger-sourced run switches to the trigger's
`run_as` for the whole run (`flow/triggers/triggers.py:74-75`). **INFERRED** — not exercised here;
see [[00-inbox/trigger-runs-name-scheduler-as-the-person]].

## (d) Do the two Flow Run rows link to each other?

**VERIFIED** — `TestHandoffToACodeAgent.test_neither_run_records_the_other`. **No.**

`Flow Run`'s only outward pointers are `session`, `trigger`, and `reference_doctype`/`reference_name`.
After a handoff, `trigger` is `None` on both, `reference_doctype` is `None` on both, and the two
sessions differ. There is no parent-run field and nothing sets the reference pair to the other run.
Any design that needs to find "the run my run is waiting on" has to add that link.

## A defect found on the way, unrelated to approvals

**VERIFIED** — `TestTheNestedRunClobbersTheOuterRunsMemoryStamp`.

The flag identifying which run is writing memory is process-global. A turn sets it on entry and
clears it to `None` in a `finally`. A nested turn therefore clears it **while the outer turn is still
running**: on entering the handoff tool the flag names the outer run; on returning from it the flag
is `None`. Anything the outer run saves to memory after its tool returns records no source run.
Predicted from reading the code, then confirmed by test rather than asserted. **Not fixed here** —
out of the spike's scope. Should become an inbox item if handoff is pursued.

## UNKNOWN — what this spike did not establish

- **Locking under real conditions.** `chat()` commits before the long model call only when not in a
  test, so the test transaction never exercises production row locking on `Flow Session`. Whether a
  nested run can deadlock against its own caller in production is **UNKNOWN-IN-TEST**.
- **Streaming.** Every test here is the synchronous path. What a nested run does inside
  `stream_with_persistence` — which persists only on `Done` and marks the run failed on a client
  disconnect — is **UNKNOWN**. Interacts with [[00-inbox/stream-disconnect-loses-output]].
- **Depth and cost.** Nothing stops B from calling C, or A from calling B in a loop. No depth limit,
  budget or cycle check was looked for. **UNKNOWN**.
- **`auto_approve`.** A trigger-sourced A runs with `auto_approve=True`, but B is assembled without
  it, so B would still pause inside an unattended run. Read from `assemble` (`flow_agent.py:99-105`),
  **INFERRED**, not tested.

## Links
[[10-specs/o1-agent-handoff-spike]] · [[20-adr/ADR-002-specialist-approval-routing]] ·
[[40-architecture/engine-overview]] · [[MOC]]
