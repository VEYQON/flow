---
type: inbox
status: open
created: 2026-09-20
---
# One approval can execute two writes

Found by the security review of `loop/s14b-all-or-nothing` (unattended run 5). **Pre-existing on
`veyqon` and on `develop`** — neither S14 nor S14b makes it better or worse, which is why it is
captured here rather than fixed inside either.

The transcript scan that finds pending calls does not deduplicate by `tool_call_id`. A model that
emits two tool calls with the SAME id produces two pending calls, one answer key covers both, and a
single `"Approve"` executes the tool **twice**. Measured by the reviewer against both branches.

The interface makes it worse rather than better: the client stamps only the first card for that id,
so the person sees one action, approves one action, and two happen.

Not solved here. Whoever picks this up should decide whether a duplicate id is rejected when the
assistant message is built, or deduplicated at resume, and whether an id collision should fail the
turn outright.

Related: [[10-specs/s14-deny-stops-batch]], [[10-specs/s15-resume-fails-closed]].
