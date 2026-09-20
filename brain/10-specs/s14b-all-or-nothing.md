---
type: spec
status: implemented-awaiting-owner-decision
approved-by: owner pre-approval for unattended run 5 2026-09-20 — REVIEW BEFORE MERGE
created: 2026-09-20
implemented: 2026-09-20
upstreamable: yes
---
# Spec: S14b — all-or-nothing in a group of approvals

## Problem
S14 settled one half of a question and deliberately left the other half open. Its Open question 1
is still open, and it is the owner's to answer:

> `{"k1": "Approve", "k2": "some free text"}` still **executes k1** and redirects k2.

S14's reasoning: free text is a *redirect*, not a refusal, and the run continues so the model can
adjust and re-ask. The counter-argument is equally real: a person shown two changes as one group,
who asks for changes to one of them, has not obviously consented to the other half going ahead
while they are still talking about it.

This branch is **not an argument for either side.** It is the stricter rule, built and tested, so
that the decision costs a merge rather than another build.

## Goals
- In a resume holding **more than one** question, a call executes **only if every answer in the
  group is exactly `"Approve"`**. Any other answer — `Deny`, free text, or no answer at all —
  withholds every `Approve` in the group.
- **Single-question behaviour is byte-identical.** One question answered `Approve` executes;
  `Deny` denies; free text redirects. Nothing about a lone question moves.
- The owner can take this branch or drop it without either choice leaving work behind.

## Non-goals
- Deciding. This spec does not recommend a side; `status` says so.
- Changing what a single answer *means*. Only the exact `"Approve"` ever executes anything, options
  stay `["Approve", "Deny"]`, `allow_other` is unchanged.
- Changing the halt. A `Deny` still halts the run through `_has_denial`; free text still does not.
  Under this rule a group holding free text **withholds everything and the run continues**, so the
  model can re-ask — which is the redirect contract doing its job at group scale.
- S15's fail-closed rows. This branch is cut from `veyqon` and does not contain them.

## Current behaviour
On `veyqon` @ `6fad779`, `flow/lib/agent.py:227` and `:235`:
`denied_group = _has_denial(answers)` is read once before the loop, and an `"Approve"` is withheld
only when the group holds an exact `"Deny"`. `{"k1": "Approve", "k2": "make it smaller"}` therefore
executes `k1`, pinned today by
`test_approve_beside_free_text_still_executes_the_approved_one_and_redirects_the_other`
(`flow/tests/test_deny_stops_batch.py`).

## Proposed behaviour
One more predicate beside `denied_group`, read once, before the loop:

    strict_group = len(pending) > 1 and not _all_approved(pending, answers)

and the withholding condition becomes `if (denied_group or strict_group) and answer == "Approve":`.

`_all_approved` is true only when **every pending call has an answer of exactly `"Approve"`** and
**no other answer was sent alongside** — the second half so that a stray key valued anything else
withholds, which is the same generous-to-safety direction `_has_denial` already takes for a stray
`"Deny"` (S14's LOW, dismissed with that reason).

`denied_group` is kept rather than folded in. With one pending call `strict_group` is false by
construction, so **everything about a single question is decided by exactly the code that decides
it today** — that is what makes "byte-identical" a fact rather than a hope.

The withheld record is **`_withheld_confirmation()` unchanged**, as the task specified. See Risks:
its wording is now sometimes wrong, and that is the one thing about this branch the owner should
look at hardest.

## Model-facing impact
No new string. The existing withheld record is reached in more cases:
`{"status": "not_executed", "message": "Nothing in this group ran. The user approved this action
but denied another action in the same group.", "user_answer": "Approve"}`.

**It now reaches a case where nobody denied anything** — a group where the other answer was free
text or missing. The model is then told "denied another action", which is not what happened.
No platform, vendor or model name, and `user_answer` remains a fixed literal, so rule 3 still holds
and no caller text is echoed. See Risks and Open question 2.

## Acceptance criteria
1. Two questions, `{Approve, free text}`: **nothing executes**. (This is the whole change; under
   S14 the approved one runs.)
2. Two questions, `{free text, Approve}`: same, so neither the answer map's order nor the
   transcript's call order matters.
3. Two questions, one `Approve` and one **unanswered**: nothing executes.
4. Three questions, two `Approve` and one free text: nothing executes.
5. Two questions, both exactly `"Approve"`: **both execute** and the run continues — the rule
   withholds, it does not refuse.
6. Two questions, `{Approve, Deny}`: nothing executes and the run halts — S14 unchanged.
7. A single question answered `"Approve"` executes, and its tool result is byte-identical to today.
8. A single question answered `"Deny"` produces today's denial record and halts, byte-identical.
9. A single question answered with free text produces today's redirect record, byte-identical,
   and the run continues.
10. A withheld call still gets a tool result on its own `tool_call_id` carrying
    `status: not_executed`.
11. The streaming resume withholds identically and emits the same `ToolEnded` events.
12. `_invoke`, `_resolve_confirmation`, `_confirmation_question` and `_has_denial` are
    byte-identical to their reviewed form, and upstream's `TestAgentConfirmation` is unmodified and
    green.

## Risks
- **The withheld message is now sometimes untrue.** It says the user "denied another action" when
  the other answer may have been free text or absent. The task specified the existing message, so
  that is what ships; a one-line wording change would fix it and is deliberately not made here
  because it would put a second decision inside the first. **Open question 2.**
- **A slower conversation.** A person who approves one of two things and asks a question about the
  other now has to answer again for the first. That is the cost of the rule and it is the point of
  the decision.
- **A stray answer key withholds a whole group.** Same direction as S14's, and the same reason.

## Open questions
1. **This whole branch is the question.** Both options are written up, with the test that pins
   each, in [[10-specs/s14-deny-stops-batch]] Open question 1. Merging this branch chooses B.
2. **If B is chosen, should the withheld message be reworded** so it does not claim a denial that
   did not happen? One line, in `_withheld_confirmation` — a rule-4 function, so it needs a spec
   that names it.

## The gate on this branch is RED, on purpose, and it is the deliverable
`MIN_TESTS=690 scripts/run-tests.sh` → **`EXIT=1 TESTS_RUN=706 FAILURE_LINES=0... GATE=RED`**, with
**exactly one failing test in the whole suite**:
`test_approve_beside_free_text_still_executes_the_approved_one_and_redirects_the_other`
(`flow/tests/test_deny_stops_batch.py`), which is S14's AC 9 — the test that pins **option A**.

That is not a defect in this branch. **The two options contradict each other by construction**, and
the repository is built so that saying so is unavoidable: whichever way the owner decides, the other
side's test goes red. The agent did **not** edit or delete that test to make this branch green —
workflow rule 4 forbids it, and doing so would have destroyed the only signal that a decision was
being made.

**If option B is adopted**, updating that one test is part of accepting the decision, and it is a
human's edit to make. **If option A stands**, this branch is dropped and nothing else changes.

## Links
- [[10-specs/s14-deny-stops-batch]] — Open question 1, which this branch exists to answer.
- [[10-specs/s15-resume-fails-closed]] — the other resume change in this run, on its own branch;
  they touch the same function and both are one-predicate additions, so they merge in either order
  with a trivial conflict in `_prepare_resume`'s condition.
