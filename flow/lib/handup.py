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
	already has a parent run."""
	if not parent_run:
		return
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
		prompt=_("Approve this, asked for by the {0} specialist?\n\n{1}").format(label, body),
		options=["Approve", "Deny"],
		allow_other=True,
		handup={
			"child_run": child_run.name,
			"specialist": label,
			"args_digest": arguments_digest(calls),
			"child_keys": [call["id"] for call in calls],
		},
	)


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
		key = question.get("key")
		if not handup or key not in routed:
			continue

		answer = routed[key]
		child_run = frappe.get_doc("Flow Run", handup["child_run"])
		assert_run_owner(parent_run)
		assert_run_owner(child_run)
		_assert_unchanged(child_run, handup)

		child_session = load_session(child_run.session, agent=_SPECIALISTS.get(handup["specialist"]))
		child_session.resume({call_id: answer for call_id in handup["child_keys"]})
		child_run.reload()

		if answer == "Deny":
			continue
		routed[key] = child_run.output or ""
	return routed


def _assert_unchanged(child_run: FlowRun, handup: dict[str, Any]) -> None:
	"""What executes must be byte-for-byte what was shown. If the specialist's pending call has
	changed since the question was asked, nothing runs."""
	if arguments_digest(pending_calls_of(child_run)) == handup["args_digest"]:
		return
	frappe.throw(
		_("What was approved is not what is about to run. Nothing has been done."),
		title=_("Approval No Longer Matches"),
	)
