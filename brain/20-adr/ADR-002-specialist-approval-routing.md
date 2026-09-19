---
type: adr
status: proposed     # proposed → accepted | superseded.  NOT ACCEPTED — the owner decides.
supersedes:
superseded-by:
created: 2026-09-19
---
# ADR-002 (DRAFT): Routing a specialist's approval request to the person

> **This is a draft for a decision, not a decision.** It was written by the unattended run of
> 2026-09-19 from the O1 spike and has not been accepted by anyone. Its purpose is to make the
> choice concrete enough to be argued with.

## Context
The O1 spike ([[40-architecture/agent-handoff-findings]]) established by test that one agent can
already hand work to another and get an answer back, that the specialist runs as the invoking user
throughout, and that the approval gate itself survives nesting — "Approve" still executes and only
"Approve".

One thing fails, silently and around writes. When the specialist pauses for approval:
- the caller's tool gets `""` (a paused run's `output` is `None`),
- the caller finishes normally and tells the person the work is under way,
- no question is ever put to the person,
- and the specialist's paused run is left parked where nothing links it to the conversation the
  person is actually in.

The person believes a write happened. It did not, and nobody will ever be asked. That is the
failure this decision has to remove.

The spike also established what does NOT need solving: permissions (same user throughout, and
`assert_run_owner` already passes on the parked run) and the approval machinery itself (resuming the
parked run by id works, for both Approve and Deny).

## Decision (proposed)
**A pause propagates. A run that calls another run cannot complete while the run it called is
waiting for a person.**

Concretely:
1. **Link the runs.** Add a nullable `parent_run` link on `Flow Run`, set when a run is created from
   inside another run's tool. The spike verified no such link exists today, and every option below
   needs one for the record to be navigable at all.
2. **Make the pause visible to the caller.** A tool that starts a nested run must be able to signal
   "not an answer — a question". The caller's turn then pauses too, rather than completing.
3. **Re-ask the question in the caller's conversation.** The caller's run carries the specialist's
   question, keyed so the answer can be routed back to the specialist's pending call. The person
   sees the question in the conversation they are actually having, and answers it there.
4. **Answer flows back down.** Resuming the caller resumes the specialist with that answer, then
   the caller's own turn continues with the specialist's result.
5. **Nothing about what executes changes.** Options stay exactly `["Approve", "Deny"]`; only the
   exact "Approve" executes; the question's wording is the only thing that travels.

## Alternatives considered
- **Leave it, and forbid specialists from having confirmation tools.** Cheapest and safest. Rejected
  as a permanent answer — the whole point of a specialist is that it does the specialised work,
  which is exactly the work worth confirming. Worth considering as an interim guard rail: refuse to
  hand off to an agent that has a `requires_confirmation` tool, so the failure is loud instead of
  silent.
- **Auto-approve the specialist.** Rejected outright. It converts an un-asked question into an
  unauthorised write, which is strictly worse than the bug. `auto_approve` exists for trigger runs
  where a human has pre-authorised the whole automation; a generalist's handoff is not that.
- **Surface the parked run in the UI as a separate pending item.** Cheaper than propagation: link
  the runs, then list the specialist's paused run against the caller's session. Rejected as the
  primary answer because the caller has already told the person the work is under way — the
  conversation and the pending item contradict each other. Reasonable as a safety net.
- **Let the tool poll until the nested run is resumed.** Rejected: it holds a request open waiting
  on a human, and the spike could not establish whether a nested run deadlocks against its caller
  in production (UNKNOWN-IN-TEST).

## Consequences
- `flow/lib/agent.py` would have to learn that a tool result can be a `Question`. That file carries
  the write-confirmation path, so this needs its own spec naming those functions, and upstream's
  `TestAgentConfirmation` must stay green unmodified.
- Question keys must stay unambiguous across two runs, or an answer could be routed to the wrong
  pending call. That is the sharpest risk in this design and needs its own acceptance criteria.
- A doctype change (`parent_run`) means a migration.
- Depth, cycles and cost are unaddressed here and are still UNKNOWN (see the findings note).
- Probably upstreamable, but it is a real engine feature, not a fix.

## Open questions for the owner
- Is propagation wanted at all, or is the interim guard rail (refuse handoff to an agent with
  confirmation tools) the right scope for now?
- Should the person see that a *specialist* is asking, or should the question read as the
  generalist's own? The first is honest; the second is simpler and may be less confusing.
- What happens to the parked specialist run if the person never answers?

---
ADRs are immutable once accepted. This one is not accepted; it may be edited freely until it is.
