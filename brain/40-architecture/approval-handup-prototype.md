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

## The invariant table

| # | invariant | status | test |
|---|---|---|---|
| I1 | one approval question in the person's own conversation, naming the specialist, showing the exact arguments | **VERIFIED** | `test_i1_the_specialists_pause_raises_one_question_on_the_callers_run`, `test_i1_the_question_names_the_specialist_and_shows_the_exact_arguments`, `test_i1_the_question_is_keyed_to_the_callers_own_tool_call` |
| I2 | only the exact `"Approve"` executes, at any depth; zero tool calls before approval | **VERIFIED** | `test_i2_nothing_executes_while_the_question_is_pending`, `test_i2_the_exact_approve_executes_the_write_the_person_was_shown`, `test_i2_deny_executes_nothing_at_either_depth`, `test_i2_free_text_executes_nothing`, `test_i2_a_lowercase_approve_does_not_execute` |
| I3 | what executes is byte-for-byte what was shown, compared by digest | **VERIFIED** | `test_i3_the_digest_covers_the_arguments_the_person_was_shown`, `test_i3_arguments_changed_after_the_question_was_asked_do_not_execute` |
| I4 | parent run and child run linked in the stored records both ways | **VERIFIED** | `test_i4_the_child_run_points_at_the_run_that_is_waiting_on_it`, `test_i4_the_waiting_run_points_at_the_child_it_is_waiting_on`, `test_i4_a_paused_child_is_reachable_from_the_conversation_the_person_is_in` |
| I5 | nothing is claimed done or under way while anything is pending | **VERIFIED** | `test_i5_the_caller_never_says_the_work_is_done_or_under_way`, `test_i5_no_stored_message_claims_the_work_happened` |
| I6 | resume runs as the owner of the parent run only | **VERIFIED — but read the note below** | `test_i6_another_user_cannot_approve_the_handed_up_question`, `test_i6_the_run_owner_check_fires_before_the_specialists_session_is_opened`, `test_i6_the_owner_of_the_parent_run_owns_the_child_run_too` |
| I7 | depth is exactly one | **VERIFIED, with a caveat below** | `test_i7_a_specialist_cannot_hand_work_on_to_another_specialist` |

**None of the seven had to be bent.** ADR-002's proposed design, as far as this prototype goes,
can satisfy all of them. What it cost is listed under "What it takes", and what is still
unanswered under "Not established".

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
- **Streaming.** Every test here is the synchronous path. `stream_with_persistence` clears
  `frappe.flags.flow_run` in its `finally`, and the O1 spike already found that a nested run clears
  that flag while the outer turn is still running — `delegate` reads the flag *before* starting the
  child, so it is unaffected, but a streamed hand-up is **UNKNOWN**.
- **Concurrency and locking**, unchanged from O1: `chat()` commits before the model call only
  outside tests, so production row locking on `Flow Session` is never exercised here.
- **What happens to a parked specialist run nobody ever answers.** ADR-002 asks this; the prototype
  does not answer it.
- **Cost.** A hand-up costs one extra model turn on the caller (the resume), and nothing was
  measured about budgets.

## Links
[[10-specs/o2-approval-handup-spike]] · [[20-adr/ADR-002-specialist-approval-routing]] ·
[[40-architecture/agent-handoff-findings]] · [[MOC]]
