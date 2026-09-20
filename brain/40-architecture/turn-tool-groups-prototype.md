---
type: architecture
status: evidence
created: 2026-09-20
branch: loop/o3b-turn-tool-groups
---
# ADR-003 prototype — the tool group for a turn is chosen before the run starts

**EVIDENCE ONLY. Not for merge. ADR-003's status is untouched and is still `proposed`** — its file
is not on `veyqon` (it lives at the harness path, and run 4 copied it onto
`loop/o3-tool-groups-spike`, which was not merged). Nothing here accepts, rejects or amends it.

## Why this shape and not ADR-003's own
Run 4 established, by test, that loading tools **during** a run is not viable
(`brain/40-architecture/tool-groups-findings.md` on `loop/o3-tool-groups-spike` @ `2642fbf`):
the tool list sent to the model is built once above the iteration loop, so a tool added mid-run is
callable but never advertised; nothing a tool adds survives a request; and a resume rebuilds the
runtime from the record, where a mid-run-loaded tool has never been — so a person's approval of it
was **dropped silently**, which is the defect S15 now fails closed on.

This prototype does the choosing at the other end. **The caller names the turn's group before the
run starts; it is recorded on the run; every later reader rebuilds from that record** rather than
from whatever happens to be registered when they look. No classifier, and the model is never asked.

## The finding that makes it small
**The record it needs already exists.** `Agent.snapshot()` (`flow/lib/agent.py:197-205`) already
writes `"tools": [t.name for t in self.tools]`, and `FlowSession.chat` already stores it on every
run as `config_snapshot` (`flow_session.py:167` → `flow_run.py:140`). **A turn's tool set is
already recorded on every run in production today.** Nothing new is persisted, there is no doctype
change and no migration. The prototype only narrows the runtime before the snapshot is taken, and
reads the snapshot back at resume.

## Results
All five claims **VERIFIED**, by 8 tests in `flow/tests/test_turn_tool_groups.py`, every one of
them an integration test through a **record-backed session** — a prototype that kept one `Agent`
object alive would prove nothing, because the whole question is what a rebuilt runtime sees.
Each assertion is against **what the model was actually offered** (the `tools=` argument of the
model call, captured) or **what actually executed** (every tool records its own calls), never
against what a result says about itself.

| T | claim | status | test |
|---|---|---|---|
| **T1** | the group is chosen BEFORE the run starts, by an explicit argument, and recorded on the run | **VERIFIED** | `test_t1_the_group_is_chosen_before_the_run_and_recorded_on_the_run` — the model is offered exactly the group, and `run.config_snapshot` carries both `tools` and `tool_group` |
| | (its control) the same session without a group offers strictly more | **VERIFIED** | `test_t1_the_group_is_recorded_even_though_the_agent_has_more_tools` |
| **T2** | a pause and resume in that turn sees exactly the same tool set, rebuilt from the record | **VERIFIED** | `test_t2_a_resume_in_that_turn_sees_exactly_the_same_tool_set` — **all three tools are re-attached by hand before the narrowing runs**, so the narrowing demonstrably comes from the record and not from what is registered |
| **T3** | a tool outside the turn's group is refused and nothing executes | **VERIFIED** | `test_t3_a_tool_outside_the_group_is_refused_and_nothing_executes` — the stored tool result is `Unknown tool`, the recorder is empty, and **this needed no engine change at all**: a tool absent from `_tools_by_name` never reaches `_run_tool` (`flow/lib/agent.py:421-422`) |
| **T4** | a gated tool inside a group keeps `requires_confirmation` and the exact-Approve path | **VERIFIED** | `test_t4_a_gated_tool_inside_the_group_still_asks_and_still_needs_the_exact_approve` (options are exactly `["Approve","Deny"]`, nothing runs before the answer, the exact "Approve" executes) and `test_t4_a_denial_inside_a_group_still_executes_nothing` |
| **T5** | the next turn may use a different group, and a paused call from the previous turn is still resolved against ITS turn's group | **VERIFIED** | `test_t5_the_next_turn_may_use_a_different_group` and `test_t5_a_paused_call_is_resolved_against_its_own_turns_group` |

**Nothing FAILED. Nothing NOT ATTEMPTED** among T1–T5.

**PROBES — the tests discriminate.** Each mutation applied, module re-run,
`flow_session.py` restored by byte copy and proved by `sha256sum -c` → OK
(prototype sha `bd7d7179…47a345`):

| probe | mutation | result |
|---|---|---|
| A | `chat()` ignores the group — no narrowing before the run | **RED on 3** — T1, T3 and T5's next-turn test |
| B | the resume narrows to the session's current tools instead of the paused run's record | **RED on 2** — T2 and T5's paused-call test, exactly the two claims about the record |

**GATE:** `MIN_TESTS=690 scripts/run-tests.sh` → **GATE=GREEN**, `LOCK_WAITED_SECONDS=0`,
`EXIT=0 TESTS_RUN=698 FAILURE_LINES=0` (690 + 8). `pre-commit` clean.
**`git diff veyqon --stat -- flow/lib/` is EMPTY** — the prototype changes no approval code, no
`_prepare_resume`, none of the four rule-4 functions.

## The result worth reading
**T5 is the one that earns the branch, and it is the complement of S15, not an alternative to it.**
Run 4's swallow, and S15's whole reason to exist, is a resume that cannot find the tool its
question was about. **With the turn's group on the record, the resume rebuilds the set the question
was asked against, so the tool is there and the loss does not happen.** S15 makes the loss *safe*;
this makes it *not occur*. Either alone is worth having; together, a dropped approval needs both
the record to be missing and the tool to be gone.

The second result is smaller and useful: **T3 needed no engine change.** Scoping already refuses
out-of-group calls correctly, because a tool absent from the registry never reaches execution.
Whatever ADR-003 becomes, that part of it is already true.

## What an engine change for production would touch
1. **`FlowSession.chat`** — a `tool_group` argument, and the narrowing applied *before* the
   snapshot is taken. This is the only place the group may be decided; deciding it later is how
   run 4's version went wrong.
2. **`FlowSession.resume`** — read `config_snapshot["tool_group"]` from the **paused run**, never
   from the session. One line, and probe B shows it is the line that matters.
3. **`_narrow_runtime` must not survive as written.** In place mutation of `.tools` and
   `._tools_by_name` is prototype-grade: production should build the `Agent` with the group, via
   `assemble()` / `_resolve_existing_agent` (`flow/lib/session.py:71,82`), so the narrowing cannot
   be forgotten, undone by a later mutation, or applied twice to an already-narrow runtime.
4. **Where groups are defined.** This prototype takes a list of tool names from the caller, which
   is the smallest thing that proves the claims. Production needs them to live somewhere — a child
   table on Flow Agent is the obvious place — and that is a doctype change with a migration, which
   this prototype deliberately does not make.
5. **Who chooses.** Nothing here answers that. An explicit argument is enough to prove the
   mechanism; a real deployment has to decide whether the caller, a rule, or a classifier picks the
   group, and a classifier is a model call with a cost and a failure mode of its own.
6. **The whitelisted API.** `flow.api.api` does not expose `tool_group`; a client-chosen group is a
   new input on a whitelisted method and needs its own security review. Not prototyped.

## Not established
- **No real `load_tools` and no real group definitions.** Groups here are literal name lists in the
  test. An implementation storing them on a record could answer differently about what a rebuilt
  runtime sees.
- **Nothing about token cost** (ADR-003's X5), and nothing about whether a model works better with
  a narrowed tool set. Both need real-model evaluations and a budget decision.
- **The streaming path is not covered.** `chat(stream=True)` and `resume(stream=True)` share the
  narrowing, which runs before either branches — so it is very likely identical, and **"very
  likely" is not evidence.** INFERRED, not VERIFIED.
- **No reviewer read this note's conclusions**; one security reviewer saw the branch, and its scope
  was the prototype's safety, not whether ADR-003 is a good idea.
- **The `auto_approve` (trigger) path** was not exercised with a group.

## Links
- [[40-architecture/tool-groups-findings]] — run 4's spike, which ruled out the mid-run version.
- [[10-specs/s15-resume-fails-closed]] — the complement described above.
- ADR-003 (`proposed`, not in this repository's `brain/20-adr/`) — untouched by this note.
