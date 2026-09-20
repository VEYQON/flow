---
id: ADR-003
title: One Agent Q with per-domain tool sets, instead of agent-to-agent hand-off for writes
status: proposed
date: 2026-09-19
decides-against: ADR-002 (specialist approval routing) for any tool that writes
deciders: owner and CEO
---

# ADR-003 · One Agent Q, per-domain tool sets

## Context

The architecture diagram has Agent Q in the centre and eight module agents around it (HRMS, CRMS,
ERP, Stock, Assets, Sub Contracting, Projects, Frameworks), each with its own tools. ADR-002
proposed how a specialist's write approval would be passed up to the person talking to Agent Q.
Three spikes tested this against the engine (upstream 4a3189b), with a fake model:

- **O1, run 1:** naive hand-off fails silently and falsely. The specialist's write is parked,
  the caller receives an empty string, and the person is told the work is under way.
- **O2 prototype, run 3:** two security reviews broke it seven ways. Four were HIGH and executed
  end to end:
  - one "Approve" ran a delete two levels down, under a question that did not name it
  - the parent said "done" while a payment sat parked, twice
  - a resume with no approval at all completed the turn
  - the specialist's own output landed where a person's "Approve"/"Deny" goes

  All were fixed only by failing closed, three of them with no test. **Invariant I3 still FAILS:
  nothing binds the question text a person approves to the call that executes.** One delegation
  per turn is now the limit.
- **The same run found the engine's write-approval path needed NO change.** `_invoke`,
  `_resolve_confirmation`, `_confirmation_question` and `_has_denial` are byte-identical, and every
  HIGH came from the routing between agents.

So the hard, proven-safe part already exists: one run, one question, the exact "Approve". The risk
is entirely in carrying that across agents.

## Decision (proposed)

1. **Agent Q is the only agent that ever executes a write.** There is one run, one approval path,
   and the engine's existing, tested confirmation.
2. **Domains are tool sets, not agents.** The diagram's eight modules become eight named tool
   groups. Each request loads only the groups it needs: HR questions get HR tools, stock
   questions get stock tools. That keeps the tool list small, which is the real reason people
   reach for specialists: fewer tools means better tool choice and lower cost. The prompt carries
   short per-domain guidance next to each group.
3. **How groups are chosen.** Start simple: Agent Q calls a read-only `load_tools(domain)` tool,
   which is cheap and auditable because it's in the transcript. A classifier comes only if the
   evaluations (O11) show `load_tools` choosing badly.
4. **Specialist agents may return later for READ-ONLY work only**, such as research, summaries, or
   drafting a proposal that Agent Q then carries out under its own approval. A specialist's
   output is always data, never an answer to a question.
5. ADR-002 stays `proposed` and is revisited only if a real need appears that 1–4 cannot meet,
   with I3 (text bound to call by digest) as a precondition.

## What the person experiences

It's the same: one assistant that knows every module. The difference is internal. Every change they
approve is one question in their own conversation, showing the exact arguments, executed by the run
that asked.

## Consequences

- **Removed:** the whole class of bugs O1 and O2 found (orphaned writes, depth, cross-run keys,
  model text in the answer slot, question text unbound from the call).
- **Kept:** the diagram's module boundaries, as tool groups with owners.
- **Cost:** one prompt carries more domain guidance, and a request that spans domains loads two
  groups. Watch token cost per turn (X5).
- **New work (small):** tool-group metadata in `q_flow_tools`, the `load_tools` tool, and prompt
  sections per group. Most of this is config.
- **Still required regardless of this decision:**
  - (a) every write tool has `requires_confirmation`. Run 3 showed an ungated call beside a gated
    one executes during the turn that pauses.
  - (b) a batch that contains a Deny executes nothing. Today Approve+Deny runs the approved call.
  - (c) the client sends every key of a multi-question pause in one resume.

## Evidence

- `brain/40-architecture/agent-handoff-findings.md` (O1)
- `brain/40-architecture/approval-handup-prototype.md` at `77a3e3a` (O2, corrected invariant table)
- `flow/tests/test_spike_two_questions_one_pause.py` at `83f5620` (multi-question facts)
- run logs: `agent-run-2026-09-19-flow.md`, `-flow-2.md`, `-flow-3.md`
