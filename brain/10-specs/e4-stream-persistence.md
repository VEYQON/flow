---
type: spec
status: needs-decision   # draft → approved (HUMAN ONLY) → in-progress → implemented
created: 2026-09-20
upstreamable: yes
---
# Spec: E4 — what a dropped stream should cost

**This spec does not propose a change. It presents two options and asks the owner to choose one.**
Nothing was built. Today's behaviour is pinned by `flow/tests/test_stream_disconnect.py` so that
whichever option is taken, the change shows up as a test going red rather than as a silent shift.

## Problem
A streamed turn is persisted only when the agent's `Done` event reaches
`stream_with_persistence`. If the client stops consuming the stream first, three things are lost:
the text the person already watched arrive, the turn's transcript, and — the sharp one — **any
approval the turn was about to ask for**.

## Goals
- State exactly what is lost today, measured rather than argued.
- Give the owner two implementable options with their real costs.
- Say, for each, what happens to a pending approval — because that is the case that matters.

## Non-goals
- Building either option. That needs a decision first.
- Changing what executes, or the approval contract. Both options must leave
  "only the exact Approve executes" untouched.
- Reconnecting a client to a live stream (resumable SSE). That is a bigger feature and is named in
  Open questions, not specified here.

## Current behaviour
Read on `veyqon` @ `0560bdd`, in the functions.

1. `stream_with_persistence` (`flow_run.py:172-217`) yields `RunStarted`, iterates the agent's
   events, remembers the `RunResult` off the `Done` event, and calls `run.apply_result(...)` only
   **after** the loop finishes. `persisted` is set to True only there or in the `except Exception`
   arm.
2. A consumer that stops iterating raises **`GeneratorExit`** inside the generator.
   `GeneratorExit` is a `BaseException`, **not** an `Exception`, so it does **not** enter the
   `except Exception` arm. It goes straight to `finally`.
3. In `finally`, `persisted` is still False, so the run is stamped
   **`mark_failed("Stream interrupted")`** (`flow_run.py:211`).
4. `mark_failed` (`flow_run.py:112-116`) writes `status` and `error` and nothing else. It does not
   write `output`, it does not write `questions`, and it does not call
   `_new_messages_for_session` — so **the session's transcript never receives the turn at all**,
   not even the person's own message.
5. `apply_result` (`flow_run.py:88-110`) is the ONLY writer of `questions`, and it writes them only
   when `result.paused`. It is reached only on `Done`.

**Therefore: a turn that was going to pause for an approval and lost its consumer first is recorded
`Failed` with `questions = None`.** The approval is not delayed. It does not exist, and the person
has nothing to come back to. The one good part is that nothing executed: the gated tool had not run
when the stream died.

`finally` also clears `frappe.flags.flow_run`, which the O1/O2 spikes flagged as interacting with
nesting. A streamed hand-up remains UNKNOWN and is out of scope here.

6. One thing is **not** lost, and it matters to the argument: `_persist_turn` writes the person's
   own message before the model call (`flow_session.py:167`), so the user row survives. What is
   lost is the run's own messages — the assistant's reply and any tool results — because
   `apply_result` is what appends those.

**Pinned by** `flow/tests/test_stream_disconnect.py` — 4 tests, GATE=GREEN, including a control
that consumes the stream to the end so a broken fixture cannot masquerade as a disconnect. Both
probes below turn it red, so it is a pin and not a comment:

| probe | mutation | result |
|---|---|---|
| A | stop stamping the interrupted run as Failed | **RED, 2** — the disconnect tests |
| B | write a partial output as the stream runs (a crude sketch of option A) | **RED, 1** — `test_a_consumer_that_stops_early_loses_the_text_it_had_already_seen` |

Probe B is the one worth noticing: **a first sketch of option A already turns the pin red**, which
is exactly what these tests are for.

## The two options

### Option A — persist partial output as the stream runs
Persist incrementally: write the accumulated text (and the turn's messages) to the run as the
stream produces them, so whatever the person saw is what the record holds.

**How much work:** moderate. The accumulation already exists in the agent's event stream; the new
part is a write path that is safe to call repeatedly, and a decision about how often
(every chunk is too many writes; a time or size threshold needs choosing).

**Cost**
- One database write per flush, per streamed turn. On a chatty turn that is a real multiple of
  today's single write. The flush interval is the knob and it has to be chosen with a number, not
  a feeling.
- The framework's end-of-request commit has already fired by the time a streamed generator is
  iterated (the existing docstring says so), so every flush needs its own explicit commit outside
  tests — the same pattern the current code already uses, repeated N times.
- A partially-written run is a new state the rest of the system has to tolerate: `recover_session`,
  the stale-run timeout and the chat panel all read `status` and `output`.

**Failure modes**
- A flush that fails mid-stream leaves the run holding *some* of the text with no marker saying so.
  Whatever is persisted must be explicitly labelled partial, or a reader cannot tell a truncated
  answer from a short one.
- Interleaving with `apply_result` at the end: the final write must be able to correct or replace
  what the flushes wrote, including the transcript rows, without duplicating them.
  `_new_messages_for_session` counts stored rows to decide what is new (`flow_run.py:226`), so
  partial rows written by a flush would change what that count means — **this is the part most
  likely to produce a duplicate-message bug**, and it is exactly the mechanism the upstream fix on
  `loop/upstream-new-messages-fix` was written for.
- A disconnect during a tool call persists text that the following tool result would have
  contradicted.

**What happens to a pending approval:** *nothing improves on its own.* A is about text. The pause
is carried by `Done`, and under A the turn still ends without one. To fix the approval, A must also
persist `questions` at the moment the agent decides to pause — which is a second, separate change
to `stream_with_persistence`, and it should be specified as such rather than assumed to come free
with A.

### Option B — finish the run server-side regardless of the client
Detach the run from its consumer: when the stream is cut, keep executing to completion (or to the
pause) and persist the real result, so the client can reload and find the turn finished.

**How much work:** larger, and it changes an architectural property — a run stops being something
a request drives and becomes something a request *watches*. That means a worker, the queue, or a
deliberate decision to keep going inside the dying request.

**Cost**
- Running work nobody is waiting for. A person who closes the tab still spends model tokens, and
  there is no consumer to stop it — a stop path (`stop_run` exists) has to be reachable and
  understood.
- If it runs in a background worker, the run's identity and permissions have to survive the hop.
  `assert_run_owner` is currently asserted on a request whose user is the person's; a detached run
  has to carry that ownership explicitly and must never widen it.
- If it keeps going inside the dying request instead, it is at the mercy of the WSGI server's
  teardown, which is the thing that just killed it.

**Failure modes**
- A run that finishes with nobody watching can still fail; the person now discovers it only by
  reloading, so the error has to be visible in the session rather than in a stream event.
- Two consumers (the reconnecting client and the detached run) writing the same run needs a lock.
  There is no lock on the run today.
- The `frappe.flags.flow_run` clearing in `finally` is per-request state; a detached run changes
  who owns that flag and when it is cleared.

**What happens to a pending approval:** **B is the option that fixes it.** A run that continues to
its pause reaches `Done` with `paused=True`, so `apply_result` writes `questions` and the person
finds the approval waiting when they come back. Note the limit, plainly: a detached run **cannot
execute anything that needs approving** — it either completes the part that needs no approval and
then parks, or parks immediately. So B does not remove A's need: it changes *what* is persisted,
and a person reconnecting mid-answer still sees nothing of the text unless A is also done.

### The honest summary
They are not alternatives on the same axis. **A saves the words. B saves the decision.** The
approval case — the one with consequences — is fixed by B and not by A. If only one is done, the
question is whether losing a person's approval silently matters more than losing a paragraph of
text. **This spec's recommendation, for the owner to accept or reject: do B's approval half first
(persist `questions` when the agent pauses, even if the consumer has gone), which is much smaller
than all of B, and treat the rest of B and all of A as separate decisions.**

## Model-facing impact
None in either option. Nothing here changes what the model is sent or what a tool result says.
`"Stream interrupted"` is an operator-facing error string on the run record, not model-facing text;
it names no platform, vendor or model, and both options should keep it that way.

## Acceptance criteria
This spec is `needs-decision`, so it has none yet. The criteria for whichever option is chosen must
include, at minimum:
1. Today's four characterisation tests are updated deliberately, in their own commit, and each
   change is visible in the diff.
2. Only the exact "Approve" still executes, and `TestAgentConfirmation` is green and unmodified.
3. Whatever a disconnect persists is labelled so a reader can tell partial from complete.
4. No message is stored twice: `_new_messages_for_session`'s count-based slice is re-verified
   against the new write path, with a control for the ordinary case.

## Risks
- **Doing A naively re-opens the duplicate-message defect** fixed on
  `loop/upstream-new-messages-fix`. Any partial write of transcript rows changes what
  `_new_messages_for_session`'s row count means.
- **Doing B naively creates a run nobody owns.** Run 3's spikes found four HIGHs in exactly that
  shape of seam — a run continuing under an identity nobody re-checked.
- Doing neither is a choice too, and it is the status quo: an approval a person never gets asked.

## Open questions — FOR THE OWNER
1. **A, B, B's approval half only, or nothing?** The recommendation above is B's approval half
   first. It is the owner's call.
2. **Is a resumable stream wanted at all** (a client reconnecting to a turn in flight), or is
   "reload and see the finished turn" the intended experience? That answer changes how much of A is
   worth doing.
3. **What is the flush interval for A**, if A is chosen? It needs a number.
4. **Should a detached run be stoppable by the person who left?** `stop_run` exists; under B it
   becomes load-bearing rather than a convenience.

## Links
- [[00-inbox/stream-disconnect-loses-output]]
- `flow/tests/test_stream_disconnect.py` (the pin)
- `loop/upstream-new-messages-fix` @ `c2ecf38` — the row-count slice that A must not break
