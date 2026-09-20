---
type: architecture
created: 2026-09-20
about: ADR-003 — can the tool set change during a run?
status: evidence
---
# O3 — can the tool set change during a run? Evidence for ADR-003

**This note gathers evidence. It decides nothing.** [[20-adr/ADR-003-one-agent-scoped-tools|ADR-003]]
stays `status: proposed`; the owner decides.

Branch `loop/o3-tool-groups-spike`, cut from `veyqon` @ `0560bdd`.
Tests: `flow/tests/test_tool_groups_spike.py` — 11 tests, fake model, **characterisation only.**
**No engine code was changed**: `flow/lib/agent.py` is byte-identical to `veyqon`
(sha256 `d10a9098…ab7b79`, re-verified with `sha256sum -c` after every probe).
`GATE=GREEN`, `EXIT=0 TESTS_RUN=681 FAILURE_LINES=0` (veyqon's 670 + 11).

ADR-003 proposes one Agent Q that loads per-domain tool groups through a read-only
`load_tools(domain)` tool, and calls the work "small … mostly config". The assumption underneath is
that **a tool call can change the tool set the same run goes on to use.** That is what this spike
tests.

The stand-in for `load_tools` is the smallest thing that could possibly work: it appends to
`agent.tools` and to `agent._tools_by_name`. Any real implementation must do at least that much.

## The answers

| # | the question | answer | status | test |
|---|---|---|---|---|
| Q1 | can a tool call add tools that the SAME run can call on its next iteration? | **It can CALL them, but the model is never TOLD they exist.** `tool_schemas = [t.to_dict() for t in self.tools] or None` is evaluated **once, above the iteration loop**, at `agent.py:265` (`_loop`) and `agent.py:316` (`_loop_stream`) — those are the only two places tools are advertised anywhere in the engine. The second model call is handed the *same list object* as the first. Meanwhile `_invoke` resolves `self._tools_by_name.get(call.name)` fresh on every call (`agent.py:404`), so the registry **is** live. | **VERIFIED** | `test_the_tool_schemas_are_computed_once_so_a_loaded_tool_is_never_advertised`, `test_the_loaded_tool_is_callable_anyway_if_the_model_names_it`, `test_the_streaming_loop_computes_its_schemas_once_too`, control: `test_an_unloaded_name_is_an_error_not_a_pause` |
| Q2 | does a tool added this way keep `requires_confirmation` and the exact-Approve path? | **Yes, completely.** The gate is read off the `Tool` object at call time, not from any registration step, so a loaded tool pauses the run with `options=["Approve","Deny"]` and `allow_other=True`, executes on the exact `"Approve"`, and does not execute on `"Deny"` (which halts with no further model call). **Nothing about the approval path needs to know a tool arrived late.** | **VERIFIED** | `test_a_loaded_gated_tool_pauses_the_run_and_does_not_execute`, `test_the_exact_approve_executes_a_loaded_tool`, `test_deny_does_not_execute_a_loaded_tool` |
| Q3 | do added tools persist into the next turn of the same session, or must they be re-loaded? | **They must be re-loaded — for a record-backed session.** `load_session` → `_resolve_existing_agent` → `agent_doc.assemble()` (`session.py:78-80`) builds a **new** `Agent` from the record's tool rows on every request, so a runtime addition is gone. A **code-agent** session keeps it, but only because the caller passes the same `Agent` object back in — it survives through the caller's memory, not through anything stored. Nothing about a loaded group is persisted anywhere. | **VERIFIED** | `test_a_record_backed_session_rebuilds_its_tools_every_turn`, `test_a_code_agent_session_keeps_a_loaded_tool_only_because_the_caller_holds_it` |
| Q4 | what does a resume see after a pause — the loaded set or the original? | **It depends on who resumes, and the production answer is the ORIGINAL.** Within one `Agent` object, `resume` re-enters `_loop`, which recomputes the schemas, so the loaded set *is* advertised. But the real path — `resume_run` (`api.py:61`) — calls `load_session(run.session)` **with no agent**, which rebuilds from the record. The paused call then names a tool the rebuilt runtime does not have. **See the finding below.** | **VERIFIED** | `test_a_resume_does_advertise_the_loaded_tool_because_it_reenters_the_loop`, `test_resuming_through_the_record_loses_the_loaded_tool_and_swallows_the_approval` |

## The finding ADR-003 most needs to read

**A gated call on a loaded tool, approved by a person, resolves without executing and without a
denial.** It fails *silently*, not closed.

The chain, every link executed in
`test_resuming_through_the_record_loses_the_loaded_tool_and_swallows_the_approval`:

1. A turn loads a group, the model calls a gated tool from it, and the run pauses. Correct so far:
   nothing has executed and the person is asked.
2. The person answers `"Approve"` through `resume_run`, which calls `load_session(run.session)` with
   no agent — so the runtime is rebuilt from the agent record and **does not contain the loaded
   tool**.
3. `_prepare_resume` looks the call's tool up and gets `None`. Its condition is
   `if tool is not None and tool.requires_confirmation:` (`agent.py:236`), so it takes the **else**
   branch: `content = _serialize_tool_result(answer)`.
4. The gated call is closed out with the literal string **`"Approve"`** as its tool result. Nothing
   ran. Nothing was denied. The run continues to the model, which is handed the person's word
   `"Approve"` as though it were a tool's output, and the turn completes normally.

The person believes they authorised a payment. The payment did not happen, the record says the turn
completed, and nothing anywhere says the approval was dropped.

**This is the same shape as the defect run 3's second security review found on
`loop/o2-approval-handup-spike`** — a runtime that does not own the shown call making
`_prepare_resume` take the non-confirmation branch, so a gated call resolves with no execution and
no denial. That was reached there through a model-chosen `label`. **Here it is reached with no
attacker at all**: it is what ADR-003's own design does on its ordinary path, the moment a loaded
tool is involved in a pause. It is not a bug in ADR-003's routing; it is a property of the engine
that ADR-003's design walks into.

**For the ADR's acceptance criteria, if it is accepted:** a resume that cannot resolve a pending
call's tool must **fail closed** — never record the person's answer as the call's result. That is a
one-line direction and it belongs in the ADR, not in a patch. Probe C below shows a fail-closed
`else` branch turns this test red immediately, so the criterion is testable the day it is written.

## What this means for ADR-003, in four lines

1. **The approval path needs no change.** Q2 is completely clean — a late-arriving tool keeps its
   gate and the exact-Approve contract. This matches run 3's finding that ADR-002 over-estimated
   the engine work, and it is the good news.
2. **`load_tools` does not work as written.** Q1: the model is never told what was loaded, so on
   the next iteration it cannot choose the tool it just asked for. It would have to guess the name.
3. **Nothing about a loaded group survives a request.** Q3: every turn starts from the record's
   tool rows, so the group must be re-loaded every turn — which is a token cost the ADR's
   Consequences do not mention, and it undercuts "the tool list stays small" if the model has to
   re-load on each turn anyway.
4. **The pause is where it actually breaks.** Q4 and the finding above. This is the one that has a
   consequence for a person rather than for a bill.

## The minimal engine change, described and NOT built

The task said to describe it if a test proved one is needed. A test does (`PROBE A`), so:

> Move `tool_schemas = [t.to_dict() for t in self.tools] or None` from above the `for iteration`
> loop to inside it, in both `_loop` (`agent.py:265`) and `_loop_stream` (`agent.py:316`).

Two lines moved, in two places. `PROBE A` and `PROBE B` apply exactly that change and each turns
exactly one characterisation test red, which is the evidence that it is sufficient for Q1.

**It is not built, and it should not be built on this branch.** What it costs and what else it
needs, honestly:
- Rebuilding the schema list once per iteration is cheap (a list comprehension over `to_dict()`),
  but it is no longer *free*, and a run with many tools pays it on every iteration.
- It only fixes Q1. It does **nothing** for Q3 (the group still evaporates between turns) or for
  the Q4 finding (the swallowed approval), and shipping only this would make the system *more*
  likely to reach the swallow, not less, by making loaded tools actually usable.
- It changes what the model is sent mid-run, which is exactly the kind of change CLAUDE.md rule 5
  is about. It needs its own spec.

## Not established

Said plainly, because a spike that does not list its gaps is advertising.

- **Whether any of this is what `load_tools` would really do.** The stand-in mutates `agent.tools`
  and `agent._tools_by_name` directly. A real implementation might rebuild the `Agent`, or carry
  groups on the record. Those would have different answers to Q3, and this spike says nothing
  about them.
- **Anything about tool-group *metadata* on `q_flow_tools`**, per-group prompt sections, or how a
  group is chosen. The ADR's items 2 and 3 are untested here.
- **Whether the model chooses groups well.** That is O11's job and it needs real-model evals, which
  this run did not have.
- **Token cost per turn** (the ADR's X5 watch item). Not measured.
- **The streamed case of the Q4 finding.** The swallow was executed through the non-streaming
  resume. The streaming resume shares `_prepare_resume`, so it is very likely identical, but
  "very likely" is not VERIFIED and it is recorded as **INFERRED**.
- **Concurrency.** Two requests loading groups onto one session were not tested.

## Probes — proving the characterisation can go red

These tests pass by construction, so there is no honest red-first. What stands in for it: every
assertion was written as a **prediction**, from reading `_loop`, `_loop_stream`, `_invoke`,
`_prepare_resume`, `load_session` and `api.resume_run`, **before the module was ever run** — and all
11 passed on the first run, so no prediction was quietly corrected afterwards. Then:

| probe | mutation | result |
|---|---|---|
| A | recompute `tool_schemas` inside `_loop`'s iteration loop | **RED, 1 failure** — `test_the_tool_schemas_are_computed_once_so_a_loaded_tool_is_never_advertised`, and nothing else |
| B | the same inside `_loop_stream` | **RED, 1 failure** — `test_the_streaming_loop_computes_its_schemas_once_too`, and nothing else |
| C | make `_prepare_resume`'s else-branch fail closed instead of recording the raw answer | **RED, 1 failure** — `test_resuming_through_the_record_loses_the_loaded_tool_and_swallows_the_approval` |

`flow/lib/agent.py` was restored from a byte backup after each probe and proved with
`sha256sum -c` → **OK** every time. No `git checkout --`, no `git restore`, no reset, no clean.

## Links
- [[20-adr/ADR-003-one-agent-scoped-tools]] (`status: proposed` — unchanged by this spike)
- `flow/tests/test_tool_groups_spike.py`
- run 3's log, for the `o2` finding this one rhymes with: `agent-run-2026-09-19-flow-3.md`
