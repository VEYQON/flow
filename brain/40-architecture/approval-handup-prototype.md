---
type: architecture
measured: 2026-09-19 against loop/o2-approval-handup-spike (veyqon + the O1 spike), Frappe v16.31.0
spike: [[10-specs/o2-approval-handup-spike]]
adr: [[20-adr/ADR-002-specialist-approval-routing]]
---
# ADR-002 prototype — handing a specialist's approval up to the person

> **Evidence only. This branch is not for merge, and ADR-002 is still `status: proposed`.**
> The prototype exists so the decision can be taken against measurements. Every claim below is a
> named test in `flow/tests/test_handup_prototype.py`, green at `GATE=GREEN`, `TESTS_RUN=616`.

## The invariant table — AFTER a security review that broke four things

> **Read this table with the review section below it.** The first version of this note marked
> I1, I3, I5 and I7 VERIFIED. A security reviewer then executed three attacks that falsified them.
> The invariants were NOT bent to fit; the table was corrected, the prototype was fixed where the
> fix was small, and what is still not established says so.

| # | invariant | status | test |
|---|---|---|---|
| I1 | one approval question in the person's own conversation, naming the specialist, showing the exact arguments | **PARTLY VERIFIED** — true for the first pause; a *second* pause was not handed up at all (HIGH-2). The prototype now refuses to continue rather than hiding it, but the second question still does not reach the person | `test_i1_*`, `test_a_specialist_that_stops_again_does_not_let_the_caller_claim_it_is_done` |
| I2 | only the exact `"Approve"` executes, at any depth; zero tool calls before approval | **VERIFIED** — the reviewer attacked the answers dict in six shapes and could not execute anything without the exact string, except through HIGH-4, which is fixed | `test_i2_*` |
| I3 | what executes is byte-for-byte what was shown, compared by digest | **FAILED as worded** — the digest is over the *pending calls of the run that paused*, and that is not always the write. In the reviewer's depth-2 chain it covered the delegation call, so one "Approve" ran a write two levels down that the question did not name. The depth fix closes that chain; the general weakness stands (see "What is still not established") | `test_i3_*` |
| I4 | parent run and child run linked in the stored records both ways | **VERIFIED after a fix** — the *second* delegation of one turn used to be stored with no parent at all (HIGH-1) | `test_i4_*`, `test_a_second_delegation_in_one_turn_is_still_recorded_as_a_child` |
| I5 | nothing is claimed done or under way while anything is pending | **FAILED as first built, VERIFIED after the fixes** — the reviewer produced "parent Completed, output `done`, child still Paused" **twice** (HIGH-2, HIGH-3), and the two original I5 tests could not see it because both assert only on the moment *before* any answer | `test_i5_*` plus the three `TestTheSecurityReviewsFindings` tests |
| I6 | resume runs as the owner of the parent run only | **VERIFIED** | `test_i6_*` |
| I7 | depth is exactly one | **FAILED as first built, VERIFIED after the fix** — depth 2 was executed (HIGH-1); the fix also limits a turn to ONE delegation | `test_i7_*`, `test_a_second_delegation_in_one_turn_is_still_recorded_as_a_child` |

**The honest summary for the decision: of the seven, two were false when this note first claimed
them verified, one is false as worded and stays false, and the rest held.** ADR-002's design is not
refuted by that — every failure but I3's was a defect in this prototype, and each was small to fix.
I3's is the design's own sharpest risk, which ADR-002 already names ("Question keys must stay
unambiguous across two runs"), showing up in a form the ADR did not anticipate: the question and the
run the answer is routed into are not bound to each other at all.

## The security review, and what was done about it
A security reviewer (read-only, isolated, on commit `ca9be13`) executed four attacks in a console
session, each ending in a rollback. Three were confirmed end to end.

| sev | finding | fix |
|---|---|---|
| **HIGH-1** | **Depth guard bypassed, and I4 with it.** `delegate` reads `frappe.flags.flow_run` to learn which run it is inside. The nested turn *cleared* that flag on the way out, so a model that emits two delegations in one assistant message gave the second one `parent_run=None` — stored as a top-level run, and then free to delegate again. Depth 2 was reached; one "Approve" executed a write two levels down that the question did not name. | **FIXED by failing closed.** A delegation whose calling run cannot be identified is now REFUSED, so no run is ever stored without a parent. The first fix attempted was to make `chat`/`resume` restore the previously active run instead of clearing it — which is the better fix, and also closes the memory-stamp clobber O1 found — but it turns O1's own characterisation test red, and a test is never edited to make a change pass. **Recommendation for the owner: take the restore fix and update O1's characterisation test deliberately, in its own commit.** The cost of the shipped fix is that only one delegation per turn is possible. |
| **HIGH-2** | **The original failure, restored.** When the specialist paused a *second* time after its approved call, `route_answers_down` took `child_run.output or ""` from a still-Paused run, so the caller finished and said the work was done while a second write sat parked. | **FIXED.** A still-Paused child now refuses the caller's turn rather than completing it on nothing. Probe I → **RED**. Handing the *new* question up in turn is not built. |
| **HIGH-3** | **A resume whose answers omit the caller's own key completed the turn with no approval at all** — the delegating tool is not `requires_confirmation`, so `answers.get(key) → None → ""` became its result. Reachable through the public resume API by answering with the child's call id. | **FIXED.** A hand-up question with no answer in the dict refuses the resume. Probe H → **RED**. |
| **HIGH-4** | **`auto_approve` could survive on a registered specialist.** `_SPECIALISTS` holds a long-lived mutable `Agent`; `FlowSession.resume` never resets `auto_approve`, so a stale `True` made a second confirmation tool run unasked. Mechanism executed; reaching the `True` is plausible, not executed. | **FIXED.** The hand-up sets `auto_approve = False` on the specialist's runtime before resuming it. No test — the state needed is contrived; recorded here instead. |

The reviewer also found that `test_the_confirmation_functions_are_byte_identical_to_the_branch_point`
had a hole: a misspelled name compared `None` to `None` and passed. **Fixed** — each name must now
resolve on both sides, and a deliberately absent name is asserted to resolve to nothing.

**What the reviewer attacked and could NOT break** (evidence, not the absence of it): CLAUDE.md
rule 4 — it independently hashed the AST source segments of `_invoke`, `_resolve_confirmation`,
`_confirmation_question`, `_has_denial` and four more, all identical to `veyqon`; the E5
forged-second-question attack does not reproduce through `_hand_up`, because `json.dumps`'
`ensure_ascii=True` escapes `\n`, U+2028, U+2029, U+0085 and U+202E, and the body is joined rather
than interpolated; rule 3 — it printed the actual strings reaching the model and found no platform,
vendor or model name; the digest cannot be made to disagree between `pending_calls_of` and the
agent's own `_transcript_calls`; six answer-dict shapes execute nothing; a re-resume fails closed;
another user is rejected before the session is opened. No new whitelisted endpoint, no
`allow_guest`, no `frappe.db.sql`, no `ignore_permissions` in the new engine code.

## What the prototype is

Four moving parts, and deliberately no more.

1. **`Flow Run.parent_run`** — a nullable link, set when a run is created from inside another run's
   tool (`create_run(parent_run=…)`, `FlowSession.chat(parent_run=…)`). ADR-002 item 1, unchanged.
2. **`flow/lib/handup.py`** — `delegate(specialist, question, label=…)`. It starts the specialist's
   own session and run. If that run **completes**, its output is returned to the calling tool as a
   string, exactly as today. If it **pauses**, `delegate` returns a `Question` instead.
3. **No change to the agent loop.** `Agent._invoke`'s own docstring already says a `Question`
   "returned or synthesized" signals a pause, and `_loop` already stamps `key = call.id` on it. So
   a tool returning a `Question` pauses the caller's turn, with no edit to `_invoke`,
   `_resolve_confirmation`, `_confirmation_question` or `_has_denial`. A test asserts those four
   are byte-identical to `veyqon` by comparing their AST source segments
   (`test_the_confirmation_functions_are_byte_identical_to_the_branch_point`), and upstream's
   `TestAgentConfirmation` is green and unmodified.
   The one change in `flow/lib/agent.py` is a new optional `handup` field on the `Question`
   dataclass, which nothing on the approval path reads.
4. **`route_answers_down()`**, called at the top of `FlowSession.resume`. For each answer whose
   question carries a hand-up record it checks the owner, checks the digest, resumes the
   specialist's parked run with that answer, and replaces the answer with what the specialist
   produced — which then becomes the delegating tool's result. `"Deny"` is passed through
   **unchanged**, so the engine's existing denial halt still stops the caller's turn as well.

## Why this shape answers the O1 failure
O1's failure was that the caller received `""`, said the work was under way, and left the write
parked. Here the caller receives no tool result at all — its turn pauses — so there is no second
model turn in which to claim anything. `test_i5_the_caller_never_says_the_work_is_done_or_under_way`
asserts on the scripted model directly: the caller's "I have removed them." response is still
sitting unconsumed in the script, and the model was called exactly once.

## Probes — every invariant was proved able to go red
Each mutation was applied to the implementation, the module re-run, and the implementation restored
from a byte backup and proved by `sha256sum -c` (OK on all four files, every time).

| probe | mutation | result |
|---|---|---|
| A | drop the digest check in `route_answers_down` | **RED** (1 failure) — I3 |
| B | drop the `"Deny"` pass-through, substituting the child's output instead | **RED** (1 failure) — I2 |
| C | make the depth guard return unconditionally | **RED** (1 failure) — I7 |
| D | stop writing `parent_run` in `create_run` | **RED** (1 failure, 10 errors) — I4 and everything downstream of it |
| E | make `delegate` always return `run.output or ""` (O1's naive delegation) | **RED** (3 failures, 9 errors) — I1, I5 |
| F | drop both `assert_run_owner` calls | **first run GREEN**, see below; **RED** after the test below was added |

**Probe F is the honest finding of this task.** With both `assert_run_owner` calls removed the
suite stayed green, because `load_session`'s own session-owner check rejects the other user a moment
later. The invariant held, but *not for the reason it names*, and a test that cannot fail is a
comment. `test_i6_the_run_owner_check_fires_before_the_specialists_session_is_opened` now patches
`load_session` to raise if it is reached at all, so the run-owner guard has to fire first; probe F
then goes **RED**. If ADR-002 is accepted, that ordering belongs in its acceptance criteria:
**the hop must check the owner before it opens anything.**

## What it takes (the cost, for the decision)
- One doctype field and therefore a migration (`bench --site <site> migrate`).
- One optional dataclass field on `Question`. It is persisted into `Flow Run.questions`, so any
  consumer of that JSON sees a new key.
- ~160 lines in one new module, plus two signatures widened (`create_run`, `FlowSession.chat`) and
  five lines at the top of `FlowSession.resume`.
- **Nothing on the write-confirmation path changes.** That is the most important cost finding here:
  ADR-002's "Consequences" predicted that `flow/lib/agent.py` "would have to learn that a tool
  result can be a `Question`". It already knows. The feature the ADR treats as the risky part does
  not need writing.

## Known limitations of the prototype (not of the design)
- **One handed-up question at a time.** If a specialist pauses with more than one pending
  confirmation call, `delegate` folds them into a single question whose digest covers all of them,
  so one "Approve" would approve several writes. That is a real product question the ADR does not
  answer and this prototype does not resolve. **NOT ATTEMPTED** as a tested case.
- **Free text is a one-shot redirect.** The answer is passed down, the specialist adjusts and
  answers; if the specialist pauses *again* on the retry, the prototype does not hand the new
  question up a second time. `test_i2_free_text_executes_nothing` covers only that nothing runs.
- **A code-defined specialist needs a process-local registry** (`register_specialist`) to be
  resumable, because a code agent's session cannot rebuild its own runtime (O1 finding). A shipped
  version would use record-defined specialists and need none of it. The registry is prototype
  scaffolding, and it would not survive a second worker process.
- **I7's refusal is a tool error, not a hard stop.** The depth guard throws inside the specialist's
  tool, and `Agent._run_tool` catches every exception and hands the message back to the model as a
  tool result. So the third agent never runs (asserted: exactly one run has a `parent_run`), but
  the specialist is told "no" rather than being stopped. Whether that is right is a decision.

## Not established
- **The HIGH-1 regression test was never watched failing.**
  `test_a_second_delegation_in_one_turn_is_still_recorded_as_a_child` is green and was written
  against an attack a reviewer executed on `ca9be13`, but the run's clock ran out before its probe
  (revert `_assert_depth`'s `if not parent_run: throw` to `return`) could be run. **Treat it as
  unproven until someone reverts that line and watches it go red.** The two fixes that WERE probed
  are HIGH-2 (probe I → RED) and HIGH-3 (probe H → RED).
- **I3 is still weak by design.** `Question.handup` is written by the tool and nothing binds it to
  the `prompt` a person reads. The digest proves the child has not changed since the record was
  built; it does not prove the record describes what the prompt says, and it is taken over the
  pending calls of the paused run whether or not those are the write. If ADR-002 is accepted, the
  binding between the question's text and the call it authorises needs its own acceptance criteria.
- **A hand-up has side effects before anyone is asked.** `delegate` runs the specialist's whole
  loop — a model call and every non-confirmation tool it picks — just to build the question, and
  outside tests the nested `chat()` commits the transaction first. That is the E5 lesson (a
  question must not execute anything) at a much larger scale, and this prototype does not solve it.
- **Direct approval of the specialist's run bypasses everything.** The child session is Manual and
  owned by the person, so it shows in their history and its paused run can be approved through the
  ordinary resume API without the parent link, the digest or the owner hop. Today only
  `load_session`'s code-agent guard blocks it, and that guard disappears with record-defined
  specialists.
- **Trigger runs park forever.** `delegate` does not pass `auto_approve` down, so an unattended run
  pauses with a hand-up question in a session hidden from the chat panel.
- **Streaming.** Every test here is the synchronous path. `stream_with_persistence` also sets
  `frappe.flags.flow_run = None` in its `finally`, so a streamed hand-up would hit the
  cannot-identify-the-caller refusal rather than work. A streamed hand-up is **UNKNOWN and expected
  to refuse**.
  *(An earlier version of this note said `delegate` reads the flag before starting the child "so it
  is unaffected". That was wrong, and a reviewer executed the attack it excused.)*
- **Concurrency and locking**, unchanged from O1: `chat()` commits before the model call only
  outside tests, so production row locking on `Flow Session` is never exercised here.
- **What happens to a parked specialist run nobody ever answers.** ADR-002 asks this; the prototype
  does not answer it.
- **Cost.** A hand-up costs one extra model turn on the caller (the resume), and nothing was
  measured about budgets.

## Links
[[10-specs/o2-approval-handup-spike]] · [[20-adr/ADR-002-specialist-approval-routing]] ·
[[40-architecture/agent-handoff-findings]] · [[MOC]]
