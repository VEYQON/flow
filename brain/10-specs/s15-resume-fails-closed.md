---
type: spec
status: approved     # draft → approved (HUMAN ONLY) → in-progress → implemented
approved-by: owner pre-approval for unattended run 5 2026-09-20 — REVIEW BEFORE MERGE
created: 2026-09-20
upstreamable: yes
---
# Spec: S15 — an approval must never be silently swallowed

## Problem
A person is shown an approval question, reads it, and answers `Approve`. The run resumes. The tool
that the question was about is **no longer in the resumed runtime** — it was removed, renamed or
disabled while the run was paused.

Today **nothing executes, nothing is denied, and the string `"Approve"` is written into the
transcript as that tool call's result.** The model reads its own tool call as having returned
`"Approve"` and may tell the person the action is done. The run completes. Nothing anywhere says
the approval was dropped.

The person believes they authorised a payment. The record says the turn completed. The payment
never happened, and nothing reported that.

This was found by run 4's ADR-003 evidence spike
(`brain/40-architecture/tool-groups-findings.md`, `loop/o3-tool-groups-spike` @ `2642fbf`), where
it is reached by the ADR's own ordinary path. **It does not need ADR-003.** A tool removed from an
agent, renamed, or its record disabled, while a run is paused, reaches it today on `veyqon`.

It fails **silently**, not closed. That is the defect.

## Goals
- At resume, a pending call whose tool the runtime **cannot find** never executes and never has the
  person's answer recorded as its result. It gets a fixed, self-describing not-executed result.
- A pending call whose tool **no longer requires confirmation** — the gate was turned off while the
  question was open — never executes on the old answer either. The basis of the question changed,
  so it must be asked again.
- In both cases the model is told plainly that nothing was done, so it cannot report the action as
  complete on the strength of a string it wrote itself.

## Non-goals
- **Changing what an answer means when the tool IS there and IS gated.** Only the exact `"Approve"`
  executes; `["Approve", "Deny"]` and `allow_other` are untouched (hard limit 8).
- Changing ordinary answered tool results — a tool that *returned* a `Question` of its own, whose
  tool is still present, resolves exactly as today.
- Re-asking automatically. S15 fails closed and says so. Whether the engine should re-raise the
  question is Open question 2, for the owner.
- Anything about ADR-003, tool groups, or loading tools mid-run. ADR-003 stays `proposed`.
- Recovering the answer later. The answer is recorded against its call as a not-executed result;
  it is not replayed.

## Current behaviour
Read on `veyqon` @ `6fad779`, in the function, not the docstring.

- `Agent._prepare_resume` (`flow/lib/agent.py:229-242`) loops over the pending calls and branches:
  ```
  tool = self._tools_by_name.get(call.name)
  if tool is not None and tool.requires_confirmation:
          ... S14 group rule, then _resolve_confirmation ...
  else:
          content = _serialize_tool_result(answer)
  ```
  The **`else` is reached by three different situations that have nothing in common**:
  1. the tool is not in the runtime at all (`tool is None`);
  2. the tool is there but `requires_confirmation` is now false;
  3. the tool is there, is not gated, and the pause was a `Question` the tool itself returned —
     the only one of the three the branch was written for.
  For (1) and (2) the person's answer is serialised as the tool's output
  (`_serialize_tool_result`, `flow/lib/agent.py:240`), so `"Approve"` becomes the tool result.
- `Agent.resume` / `_resume_stream` (`flow/lib/agent.py:149-179`) take only `messages` and
  `answers`. **Nothing reaching `_prepare_resume` records what was asked**, so the function cannot
  today tell (2) from (3).
- The asked questions *are* persisted: `FlowRun.apply_result` writes
  `[asdict(q) for q in result.questions]` to `run.questions` when the run paused
  (`flow/flow/doctype/flow_run/flow_run.py:101`), and `FlowSession.resume` already holds that `run`
  doc (`flow/flow/doctype/flow_session/flow_session.py:319`) before it calls the runtime at
  `:328`/`:331`. They are simply never passed down.
- The runtime at resume is rebuilt from the record, not carried over: `resume_run`
  (`flow/api/api.py:61`) calls `load_session(run.session)` with no agent, so
  `_tools_by_name` is whatever the agent's tool rows say **now**. That is what makes (1) and (2)
  reachable in production rather than theoretical.
- A confirmation question is identifiable by its options. `_confirmation_question`
  (`flow/lib/agent.py:594`) builds `options=["Approve", "Deny"]`, and hard limit 8 plus upstream's
  `TestAgentConfirmation` pin that exact pair. So a stored question with those exact options, in
  that order, was an approval question.

## Proposed behaviour
`_prepare_resume`'s single branch becomes four, in this order, per pending call:

| # | condition | result |
|---|---|---|
| 1 | `tool is None` | **not executed** — fixed message, reason `unavailable` |
| 2 | `tool.requires_confirmation` | **unchanged** — S14's group rule, then `_resolve_confirmation` |
| 3 | the call was asked as an approval question (from the asked-questions record) | **not executed** — fixed message, reason `approval_no_longer_applies` |
| 4 | otherwise | **unchanged** — `_serialize_tool_result(answer)` |

Row 1 is deliberately ahead of everything, including the question record: if the tool is gone there
is no reading of any answer under which the engine can honour it, so it fails closed without
needing to know what was asked. Row 4 is the branch the `else` was written for and is byte-identical
to today.

**How row 3 knows.** `Agent.resume` gains an optional keyword `asked` carrying the questions the
pause raised — the rows already stored on the run. `FlowSession.resume` passes `run.questions`.
A pending call was an approval question iff `asked` holds an entry whose `key` is the call's id and
whose `options` are exactly `["Approve", "Deny"]`. When `asked` is absent (an in-process caller that
does not pass it, or a run paused before this change) row 3 cannot fire and behaviour is exactly
today's — rows 1 and 2 still hold. That is a documented limitation, not a silent one: AC 11 pins it.

**The two fixed results.** Both are literals. Neither contains the person's answer, the tool's
name, or any value from the answers map.

    {"status": "not_executed",
     "reason": "unavailable",
     "message": "This action was not carried out and nothing was done. It is no longer available.
                 Do not report it as done. Tell the user it did not happen."}

    {"status": "not_executed",
     "reason": "approval_no_longer_applies",
     "message": "This action was not carried out and nothing was done. What it requires changed
                 while the question was open, so the answer that was given no longer applies to it.
                 Do not report it as done. Ask again before doing it."}

**S14's group rule is untouched and still holds.** It is row 2's business; rows 1 and 3 execute
nothing at all, so a denial elsewhere in the group cannot make them execute and they cannot make a
denial execute anything. A run with a denial still halts through the same `_has_denial` call.

**The four load-bearing functions do not change.** `_invoke`, `_resolve_confirmation`,
`_confirmation_question` and `_has_denial` are byte-identical to `veyqon`; S15 adds branches
*around* `_resolve_confirmation` and reads the options `_confirmation_question` already produces.
No test in this spec requires any of the four to change, so none does. AC 12 pins that by digest.

## Model-facing impact
Two new strings enter the model's context, quoted above. Checked against CLAUDE.md rule 3: neither
names Frappe, Flow, ERPNext, MariaDB, OpenAI, any other vendor, or any model. They say what
happened in the person's terms ("this action", "the user") and nothing about the machinery.

Each ends with an instruction not to report the action as done. That is the whole point of the
change: today the model is handed `"Approve"` and can reasonably conclude the tool succeeded.

Nothing model-authored and nothing caller-authored is echoed into either string. The answers map
never reaches them.

## Acceptance criteria
1. A resume whose pending call names a tool absent from the runtime does **not** execute anything
   and does **not** write the person's answer as the tool result.
2. That call's tool result is the fixed `unavailable` record, on its own `tool_call_id`.
3. The same holds for an answer of `Deny` and for free text on a missing tool: nothing executes and
   the fixed `unavailable` record is written, never the answer.
4. A pending call whose tool is present but whose `requires_confirmation` became false while the
   run was paused does not execute, and gets the fixed `approval_no_longer_applies` record.
5. A pending call that was **not** an approval question, whose tool is present and ungated,
   resolves byte-identically to today (the answer is serialised as the tool result).
6. Single-question approve / deny / free-text behaviour on a present, gated tool is byte-identical
   to today.
7. S14 still holds: a `Deny` anywhere in the group withholds every `Approve` in it, and a group
   mixing a missing tool with a gated one executes nothing.
8. The streaming resume behaves identically and emits the same `ToolEnded` events for the
   not-executed calls.
9. It holds through the **public** path: `flow.api.api.resume_run` on a record-backed session whose
   linked agent lost the tool between pause and resume executes nothing and stores the fixed
   record, not the answer.
10. The model is never handed the person's answer as a tool result in cases 1–4: no tool message
    written by those rows equals or contains the answer string.
11. With no asked-questions record available, rows 1 and 2 still hold and row 4 is unchanged —
    the fallback is pinned, so a later change to it shows as a red test.
12. `_invoke`, `_resolve_confirmation`, `_confirmation_question` and `_has_denial` are
    byte-identical to their reviewed form, and upstream's `TestAgentConfirmation` is unmodified and
    green.

## Risks
- **Fail-closed changes behaviour for an ordinary tool-authored question whose tool went missing.**
  Row 1 catches it and says "nothing was done", while in fact the tool ran before it asked. The
  wording says the *action* was not carried out, which is true — but it is less precise for that
  case. Accepted: it is strictly safer than echoing the answer, and the alternative is trusting a
  runtime that has already lost the tool.
- **A tool-authored `Question` with options exactly `["Approve", "Deny"]`** is read by row 3 as an
  approval question. It then fails closed rather than resolving. Accepted, and it fails in the safe
  direction; a tool wanting an approval-shaped question should set `requires_confirmation`.
- **Old paused runs** carry no marker beyond their stored options, which is exactly what row 3
  reads — so they are covered without a migration.
- The `asked` argument widens `Agent.resume`'s signature. It is keyword-only with a default, so
  every existing caller (16 in the test suite, 2 in `flow_session.py`, 1 in `evals/run.py`) keeps
  compiling and keeps its behaviour.

## Open questions
1. **Should the engine re-ask instead of failing closed?** S15 records a not-executed result and
   lets the run continue, so the model can raise the question again in its own words. An engine
   that re-raised the original question automatically would be stronger, and is a bigger change —
   it needs the arguments to be re-validated against a tool that may no longer exist. **Not decided
   here.** AC 1–4 pin today's answer so either decision shows as a red test.
2. **Should row 1 also halt the run, as a Deny does?** It does not today: the turn continues and
   the model is told nothing happened. The argument for halting is that a dropped approval is a
   failure, not a tool error. The argument against is that halting on a missing tool makes an
   ordinary tool-authored question fatal. Left as it is; **the owner's call.**

## Links
- Found by [[40-architecture/tool-groups-findings]] (run 4, `loop/o3-tool-groups-spike`).
- Group rule it must not disturb: [[10-specs/s14-deny-stops-batch]].
- Approval-question wording it reads: [[10-specs/e5-confirm-prompt-field-v2]].
- [[20-adr/ADR-003-one-agent-scoped-tools]] — proposed; S15 is independent of it, and closes the
  acceptance criterion that note recommended.

---
## Plan
Written by /loop-plan, 2026-09-20 (unattended run 5). Seven tasks, six files.

| # | Task | Files | Test | Satisfies AC |
|---|---|---|---|---|
| 1 | Write the failing module first: every AC as a named test against the unmodified engine, driving `Agent.resume` directly | `flow/tests/test_resume_fails_closed.py` (new) | itself — watched RED before task 2 | 1–8, 10–12 |
| 2 | Two module-level helpers: `_not_executed(reason)` builds the fixed record; `_asked_as_approval(call_id, asked)` reads the stored question's options | `flow/lib/agent.py` | task 1's fixed-record and no-echo tests | 2, 4, 10 |
| 3 | `_prepare_resume`: the four-row branch, missing-tool row first; `resume` and `_resume_stream` gain the keyword-only `asked` | `flow/lib/agent.py` | task 1's rows 1/3 tests; S14's module stays green | 1, 3–8, 11 |
| 4 | `FlowSession.resume` passes the paused run's stored questions down as `asked` | `flow/flow/doctype/flow_session/flow_session.py` | task 5's public-path test | 9 |
| 5 | End-to-end test through `flow.api.api.resume_run`: a record-backed session whose linked agent loses the tool between pause and resume | `flow/tests/test_resume_fails_closed.py` | itself — watched RED | 9 |
| 6 | Evals: a per-answer-case tool override so the resumed runtime can differ from the one that paused, plus the tool-result assertions the scenario needs | `evals/run.py` | the new scenario, RED on `veyqon` | — |
| 7 | Evals scenario `missing_tool_at_resume` | `evals/scenarios/missing_tool_at_resume.yaml` (new) | itself — RED on `veyqon`, GREEN here | 1, 2, 10 |

**DO NOT CHANGE:** `_invoke`, `_resolve_confirmation`, `_confirmation_question`, `_has_denial`
(CLAUDE.md rule 4 — no test in this spec requires any of them to change, and AC 12 pins all four by
digest). `flow/tests/test_ai_agent.py` and every other test that exists on `veyqon` (hard limit 6).
`_withheld_confirmation` and S14's group rule inside row 2. The `["Approve", "Deny"]` options and
`allow_other`. Anything under `scripts/`, `.claude/` or `CLAUDE.md` (hard limit 5). ADR-003's
status.

**Rollback plan:** the engine change is two helpers plus one branch in `_prepare_resume` and one
keyword on two signatures. `flow/lib/agent.py` and `flow_session.py` are backed up byte-for-byte
before each probe and restored by copy, verified with `sha256sum -c`. Reverting the branch to
`veyqon`'s two files restores today's behaviour exactly; the new test module and the evals scenario
then fail, which is the intended signal. No migration, no doctype change, no stored-data change —
`run.questions` is already written today and is only read by this change.
