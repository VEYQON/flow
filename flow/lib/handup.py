# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE
"""PROTOTYPE — hand a specialist's approval request up to the person.

Evidence for ADR-002, not a feature. Nothing here is meant to be merged as it stands; it exists
so the seven invariants in brain/10-specs/o2-approval-handup-spike.md can be measured rather than
predicted.

The shape: a tool that delegates work calls `delegate()`. If the specialist's run completes, its
answer comes back as a string exactly as today. If the specialist's run PAUSES for an approval,
`delegate()` returns a `Question` instead — which the agent loop already treats as a pause, so the
caller's own turn pauses too and the question reaches the person in the conversation they are
actually having. On resume, `route_answers_down()` sends the answer to the specialist's parked run
first, after checking that what is about to execute is byte-for-byte what was shown.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _

from flow.lib.agent import Question

if TYPE_CHECKING:
	from flow.flow.doctype.flow_run.flow_run import FlowRun

# Depth is exactly one: the person's own run may delegate; a specialist may not delegate again.
MAX_HANDUP_DEPTH = 1
# The specialist's label is framing text, not an argument, and is not covered by the digest.
LABEL_MAX_LENGTH = 60

# A code-defined specialist's session cannot rebuild its own runtime, so a resume has to be handed
# the same object back. A shipped version would use record-defined specialists and need none of
# this; the registry exists ONLY so the prototype can drive scripted code agents across a resume.
_SPECIALISTS: dict[str, Any] = {}


def register_specialist(label: str, agent: Any) -> None:
	"""Prototype-only: make a code-defined specialist resolvable by label on resume."""
	_SPECIALISTS[label] = agent


def delegate(specialist: Any, question: str, *, label: str) -> Any:
	"""Run `specialist` on its own session and return its answer — or, if it paused for an
	approval, a `Question` that pauses the caller's turn and puts that approval to the person."""
	from flow.lib.session import new_session

	parent_run = frappe.flags.flow_run
	_assert_depth(parent_run)

	convo = new_session(specialist)
	run = convo.chat(question, parent_run=parent_run)
	if run.status != "Paused":
		return run.output or ""
	return _hand_up(run, label=label)


def _assert_depth(parent_run: str | None) -> None:
	"""Refuse a second hop. The run doing the delegating is a specialist's run exactly when it
	already has a parent run.

	Refuse too when the calling run cannot be identified at all. The flag naming the run in
	progress is cleared by each nested turn on its way out, so the SECOND delegation of one turn
	sees nothing — and a run stored with no parent is both an orphan and free to delegate again. A
	security reviewer executed exactly that to reach depth 2 (19 Sep 2026). Failing closed keeps
	the flag's own meaning intact (the O1 spike characterises it) at the cost of allowing only one
	delegation per turn, which is recorded as a limitation rather than hidden.
	"""
	if not parent_run:
		frappe.throw(
			_("Only one piece of work can be handed to a specialist at a time."),
			title=_("Handoff Depth"),
		)
	if frappe.db.get_value("Flow Run", parent_run, "parent_run"):
		frappe.throw(
			_("A specialist cannot hand work on to another specialist."),
			title=_("Handoff Depth"),
		)


def _hand_up(child_run: FlowRun, *, label: str) -> Question:
	"""Turn the specialist's parked question into one question for the person."""
	calls = pending_calls_of(child_run)
	if not calls:
		# A paused run always carries a pending question; if it does not, say so rather than
		# inventing one.
		frappe.throw(_("The specialist paused without a question to ask."), title=_("Nothing to Ask"))

	body = "\n\n".join(
		"{0}\n{1}".format(call["name"], json.dumps(call["arguments"], indent=2, default=str))
		for call in calls
	)
	return Question(
		prompt=_("Approve this, asked for by the {0} specialist?\n\n{1}").format(_one_line(label), body),
		options=["Approve", "Deny"],
		allow_other=True,
		handup={
			"child_run": child_run.name,
			"specialist": label,
			"args_digest": arguments_digest(calls),
			"child_keys": [call["id"] for call in calls],
		},
	)


def _one_line(label: str) -> str:
	"""The framing line above the arguments is NOT covered by the digest, so nothing in it may add
	a line of its own or run long enough to push the arguments out of view. Whitespace is collapsed
	and the result capped."""
	return " ".join(str(label).split())[:LABEL_MAX_LENGTH] or "unnamed"


def pending_calls_of(run: FlowRun) -> list[dict[str, Any]]:
	"""The tool calls in `run`'s session transcript that have no result yet — the same set, from
	the same source, that the agent will execute on resume. Read-only mirror of the agent's own
	`_pending_calls`, so the digest is taken over exactly what will run."""
	session = frappe.get_doc("Flow Session", run.session)
	answered = {row.tool_call_id for row in session.messages if row.role == "tool" and row.tool_call_id}
	calls: list[dict[str, Any]] = []
	for row in session.messages:
		if row.role != "assistant" or not row.tool_calls:
			continue
		for tc in json.loads(row.tool_calls):
			if tc["id"] in answered:
				continue
			fn = tc["function"]
			calls.append(
				{"id": tc["id"], "name": fn["name"], "arguments": json.loads(fn.get("arguments") or "{}")}
			)
	return calls


def arguments_digest(calls: list[dict[str, Any]]) -> str:
	"""A digest over exactly what a person was shown: each pending call's id, name and arguments."""
	canonical = json.dumps(
		[{"id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in calls],
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def route_answers_down(parent_run: FlowRun, answers: dict[str, Any]) -> dict[str, Any]:
	"""Send each answer that belongs to a handed-up question to the specialist's parked run, and
	return the answers the caller's own turn should be resumed with.

	`"Deny"` is passed through unchanged, so the engine's existing denial halt still stops the
	caller. Any other answer is replaced by what the specialist produced, which becomes the
	delegating tool's result.
	"""
	from flow.lib.session import assert_run_owner, load_session

	questions = json.loads(parent_run.questions) if parent_run.questions else []
	routed = dict(answers)
	for question in questions:
		handup = question.get("handup")
		if not handup:
			continue
		key = question.get("key")
		if key not in routed:
			# Without an answer the delegating tool would be handed "" and the caller would finish
			# its turn — telling the person the work is done while the write is still parked. That
			# is the exact failure this design exists to remove, so refuse instead.
			frappe.throw(
				_("This turn is still waiting on an answer and cannot continue."),
				title=_("Still Waiting"),
			)

		answer = routed[key]
		assert_run_owner(parent_run)
		child_run = frappe.get_doc("Flow Run", handup["child_run"])
		assert_run_owner(child_run)
		_assert_unchanged(child_run, handup)

		child_session = load_session(child_run.session, agent=_SPECIALISTS.get(handup["specialist"]))
		_assert_same_tools(child_session, child_run)
		# A registered runtime is a long-lived object that another caller may have left with
		# approvals switched off. Nothing reached through a hand-up runs unapproved.
		child_session._runtime.auto_approve = False
		child_session.resume({call_id: answer for call_id in handup["child_keys"]})
		child_run.reload()

		if child_run.status == "Paused":
			# The answer was taken and acted on, and the run it went to has stopped for a SECOND
			# question. Handing the caller an empty result here would re-create the failure this
			# design exists to remove, so the caller's turn stays paused rather than completing on
			# nothing. Handing the new question up in turn is not built (see the prototype note).
			frappe.throw(
				_("More has been asked before this can go on. Nothing further has been done."),
				title=_("Still Waiting"),
			)

		if answer == "Deny":
			continue
		# NOT the bare string. This value is about to be read by code that compares answers against
		# "Approve" and "Deny"; a specialist whose reply happened to be one of those words would be
		# read as a person's decision. Model text and a human decision never share a value space.
		routed[key] = json.dumps({"status": "specialist_result", "output": child_run.output or ""})
	return routed


def _assert_same_tools(child_session: Any, child_run: FlowRun) -> None:
	"""The runtime about to execute is looked up by a label carried alongside the question, and the
	digest does not cover that label. So prove the runtime actually owns the calls that were shown,
	and that they are still the kind of call that needs approving — otherwise a substituted runtime
	could run a different implementation, or resolve the call with no execution and no denial at
	all, and the model would be told it was approved."""
	tools = child_session._runtime._tools_by_name
	for call in pending_calls_of(child_run):
		tool = tools.get(call["name"])
		if tool is None or not tool.requires_confirmation:
			frappe.throw(
				_("What was approved cannot be carried out as described. Nothing has been done."),
				title=_("Approval No Longer Matches"),
			)


def _assert_unchanged(child_run: FlowRun, handup: dict[str, Any]) -> None:
	"""What executes must be byte-for-byte what was shown. If the specialist's pending call has
	changed since the question was asked, nothing runs."""
	if arguments_digest(pending_calls_of(child_run)) == handup["args_digest"]:
		return
	frappe.throw(
		_("What was approved is not what is about to run. Nothing has been done."),
		title=_("Approval No Longer Matches"),
	)
