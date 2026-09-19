# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt
"""PROTOTYPE — ADR-002: hand a specialist's approval request up to the person.

Evidence for a decision, not a feature. Each test is named for the invariant it establishes
(I1..I7 in brain/10-specs/o2-approval-handup-spike.md); the findings note
brain/40-architecture/approval-handup-prototype.md cites these names.

No real model is called: every agent here runs on a scripted stand-in.
"""

import json
from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib import handup
from flow.lib.agent import Agent
from flow.lib.model import ChatResponse, ToolCall
from flow.lib.tool import tool

# ---------------------------------------------------------------------------------------------
# Scripted model plumbing (same shape as flow/tests/test_spike_agent_handoff.py; duplicated so
# each spike file stands alone and can be deleted whole)
# ---------------------------------------------------------------------------------------------


class ScriptedModel:
	def __init__(self, responses: list[ChatResponse], model_id: str = "openai/gpt-4o-mini"):
		self.model_id = model_id
		self.responses = list(responses)
		self.calls: list[dict[str, Any]] = []

	def chat(self, messages, tools=None, *, stream=False):
		self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
		if not self.responses:
			raise AssertionError("ScriptedModel ran out of scripted responses")
		return self.responses.pop(0)


def _final(text: str) -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _calls(name: str, arguments: dict[str, Any], call_id: str = "c1") -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _calls_many(pairs: list[tuple[str, dict[str, Any], str]]) -> ChatResponse:
	"""One assistant message carrying several tool calls — what a model does when it fans out."""
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=args) for name, args, call_id in pairs],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


THE_WRITE = {"folder": "2025 invoices", "count": 812}
SPECIALIST_LABEL = "records"


class HandupFixture(IntegrationTestCase):
	"""A generalist the person is talking to, and a specialist that needs a write approved."""

	def tearDown(self):
		frappe.db.rollback()
		handup._SPECIALISTS.clear()

	def build(self, *, specialist_replies=None, generalist_replies=None):
		"""Return (outer_session, executed) — `executed` records every real call of the write."""
		executed: list[dict[str, Any]] = []

		@tool(requires_confirmation=True)
		def delete_records(folder: str, count: int) -> str:
			"""Delete records in a folder."""
			executed.append({"folder": folder, "count": count})
			return f"Deleted {count}."

		specialist = Agent(
			model=ScriptedModel(
				specialist_replies
				if specialist_replies is not None
				else [_calls("delete_records", THE_WRITE, call_id="s1"), _final("Deleted 812.")]
			),
			name="specialist",
			instructions="You look after the records.",
			tools=[delete_records],
		)
		handup.register_specialist(SPECIALIST_LABEL, specialist)

		@tool
		def ask_the_specialist(question: str) -> Any:
			"""Hand a question to the specialist."""
			return handup.delegate(specialist, question, label=SPECIALIST_LABEL)

		generalist = Agent(
			model=ScriptedModel(
				generalist_replies
				if generalist_replies is not None
				else [
					_calls("ask_the_specialist", {"question": "clear the 2025 invoices"}, call_id="g1"),
					_final("I have removed them."),
				]
			),
			name="generalist",
			tools=[ask_the_specialist],
		)
		self.generalist = generalist
		self.specialist = specialist
		return generalist.new_session(), executed

	def pause_it(self):
		"""Run one turn that ends with the specialist's approval handed up. Returns
		(outer_session, outer_run, child_run, executed)."""
		outer, executed = self.build()
		outer_run = outer.chat("clear the 2025 invoices")
		outer_run.reload()
		child_name = frappe.db.get_value("Flow Run", {"parent_run": outer_run.name}, "name")
		child_run = frappe.get_doc("Flow Run", child_name) if child_name else None
		return outer, outer_run, child_run, executed

	def questions_of(self, run) -> list[dict[str, Any]]:
		return json.loads(run.questions) if run.questions else []


class TestI1OneQuestionInThePersonsOwnConversation(HandupFixture):
	def test_i1_the_specialists_pause_raises_one_question_on_the_callers_run(self):
		_, outer_run, _, executed = self.pause_it()

		self.assertEqual(outer_run.status, "Paused")
		questions = self.questions_of(outer_run)
		self.assertEqual(len(questions), 1)
		self.assertEqual(questions[0]["options"], ["Approve", "Deny"])
		self.assertEqual(executed, [])

	def test_i1_the_question_names_the_specialist_and_shows_the_exact_arguments(self):
		_, outer_run, _, _ = self.pause_it()
		prompt = self.questions_of(outer_run)[0]["prompt"]

		self.assertIn(SPECIALIST_LABEL, prompt)
		self.assertIn("delete_records", prompt)
		self.assertIn("2025 invoices", prompt)
		self.assertIn("812", prompt)

	def test_i1_the_question_is_keyed_to_the_callers_own_tool_call(self):
		_, outer_run, _, _ = self.pause_it()
		self.assertEqual(self.questions_of(outer_run)[0]["key"], "g1")


class TestI2OnlyApproveExecutes(HandupFixture):
	def test_i2_nothing_executes_while_the_question_is_pending(self):
		_, _, child_run, executed = self.pause_it()
		self.assertEqual(executed, [])
		self.assertEqual(child_run.status, "Paused")

	def test_i2_the_exact_approve_executes_the_write_the_person_was_shown(self):
		outer, _outer_run, child_run, executed = self.pause_it()

		outer.resume({"g1": "Approve"})

		self.assertEqual(executed, [THE_WRITE])
		child_run.reload()
		self.assertEqual(child_run.status, "Completed")

	def test_i2_deny_executes_nothing_at_either_depth(self):
		outer, outer_run, child_run, executed = self.pause_it()

		outer.resume({"g1": "Deny"})

		self.assertEqual(executed, [])
		outer_run.reload()
		child_run.reload()
		self.assertEqual(outer_run.status, "Completed")
		self.assertEqual(child_run.status, "Completed")
		self.assertIsNone(outer_run.output)

	def test_i2_free_text_executes_nothing(self):
		outer, executed = self.build(
			specialist_replies=[
				_calls("delete_records", THE_WRITE, call_id="s1"),
				_final("Understood — I have not deleted anything."),
			]
		)
		outer.chat("clear the 2025 invoices")

		outer.resume({"g1": "only the ones from January, and tell me first"})

		self.assertEqual(executed, [])

	def test_i2_a_lowercase_approve_does_not_execute(self):
		outer, executed = self.build(
			specialist_replies=[
				_calls("delete_records", THE_WRITE, call_id="s1"),
				_final("I did not delete anything."),
			]
		)
		outer.chat("clear the 2025 invoices")

		outer.resume({"g1": "approve"})

		self.assertEqual(executed, [])


class TestI3WhatExecutesIsWhatWasShown(HandupFixture):
	def test_i3_the_digest_covers_the_arguments_the_person_was_shown(self):
		_, outer_run, child_run, _ = self.pause_it()
		handup_record = self.questions_of(outer_run)[0]["handup"]

		self.assertEqual(
			handup_record["args_digest"],
			handup.arguments_digest(handup.pending_calls_of(child_run)),
		)

	def test_i3_arguments_changed_after_the_question_was_asked_do_not_execute(self):
		outer, _outer_run, child_run, executed = self.pause_it()
		self._retarget_the_write(child_run, {"folder": "every invoice", "count": 999999})

		with self.assertRaises(frappe.ValidationError):
			outer.resume({"g1": "Approve"})

		self.assertEqual(executed, [])

	def _retarget_the_write(self, child_run, arguments):
		"""Rewrite the specialist's stored call so that what would execute is no longer what was
		shown — the tampering this invariant exists to catch."""
		session = frappe.get_doc("Flow Session", child_run.session)
		row = next(r for r in session.messages if r.role == "assistant" and r.tool_calls)
		calls = json.loads(row.tool_calls)
		calls[0]["function"]["arguments"] = json.dumps(arguments)
		frappe.db.set_value("Flow Session Message", row.name, "tool_calls", json.dumps(calls))


class TestI4TheRunsAreLinkedBothWays(HandupFixture):
	def test_i4_the_child_run_points_at_the_run_that_is_waiting_on_it(self):
		_, outer_run, child_run, _ = self.pause_it()
		self.assertEqual(child_run.parent_run, outer_run.name)

	def test_i4_the_waiting_run_points_at_the_child_it_is_waiting_on(self):
		_, outer_run, child_run, _ = self.pause_it()
		self.assertEqual(self.questions_of(outer_run)[0]["handup"]["child_run"], child_run.name)

	def test_i4_a_paused_child_is_reachable_from_the_conversation_the_person_is_in(self):
		outer, _outer_run, child_run, _ = self.pause_it()
		reachable = frappe.get_all(
			"Flow Run",
			filters={"parent_run": ["in", frappe.get_all("Flow Run", {"session": outer.name}, pluck="name")]},
			pluck="name",
		)
		self.assertEqual(reachable, [child_run.name])


class TestI5NothingIsClaimedWhileSomethingIsPending(HandupFixture):
	def test_i5_the_caller_never_says_the_work_is_done_or_under_way(self):
		_, outer_run, _, _ = self.pause_it()

		self.assertIsNone(outer_run.output)
		# The scripted "I have removed them." was never reached: the caller's turn paused instead
		# of taking another model turn on an empty tool result.
		self.assertEqual([r.content for r in self.generalist.model.responses], ["I have removed them."])
		self.assertEqual(len(self.generalist.model.calls), 1)

	def test_i5_no_stored_message_claims_the_work_happened(self):
		outer, _, _, _ = self.pause_it()
		text = " ".join((row.content or "") for row in frappe.get_doc("Flow Session", outer.name).messages)
		for claim in ("removed them", "under way", "done"):
			self.assertNotIn(claim, text)


class TestI6ResumeRunsAsTheOwnerOfTheParentRun(HandupFixture):
	def test_i6_another_user_cannot_approve_the_handed_up_question(self):
		outer, _outer_run, child_run, executed = self.pause_it()
		other = self._a_different_user()

		frappe.set_user(other)
		try:
			with self.assertRaises(frappe.PermissionError):
				outer.resume({"g1": "Approve"})
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(executed, [])
		child_run.reload()
		self.assertEqual(child_run.status, "Paused")

	def test_i6_the_run_owner_check_fires_before_the_specialists_session_is_opened(self):
		"""The hop has its own guard. Without this test the invariant passes on the session-owner
		check inside load_session instead, and removing the run-owner check stays green."""
		from unittest.mock import patch

		import flow.lib.session as session_lib

		_outer, outer_run, _, executed = self.pause_it()
		other = self._a_different_user()

		def must_not_be_called(*args, **kwargs):
			raise AssertionError("the specialist's session was opened before the owner was checked")

		frappe.set_user(other)
		try:
			with patch.object(session_lib, "load_session", must_not_be_called):
				with self.assertRaises(frappe.PermissionError):
					handup.route_answers_down(outer_run, {"g1": "Approve"})
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(executed, [])

	def test_i6_the_owner_of_the_parent_run_owns_the_child_run_too(self):
		_, outer_run, child_run, _ = self.pause_it()
		self.assertEqual(child_run.owner, outer_run.owner)
		self.assertEqual(child_run.owner, frappe.session.user)

	def _a_different_user(self) -> str:
		email = f"handup-{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Handup", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		return email


class TestI7DepthIsExactlyOne(HandupFixture):
	def test_i7_a_specialist_cannot_hand_work_on_to_another_specialist(self):
		reached: list[str] = []

		third = Agent(model=ScriptedModel([_final("never")]), name="third")

		@tool
		def ask_someone_else(question: str) -> Any:
			"""A specialist trying to delegate again."""
			reached.append(question)
			return handup.delegate(third, question, label="third")

		specialist = Agent(
			model=ScriptedModel(
				[
					_calls("ask_someone_else", {"question": "pass it on"}, call_id="s1"),
					_final("I could not pass that on."),
				]
			),
			name="specialist",
			tools=[ask_someone_else],
		)

		@tool
		def ask_the_specialist(question: str) -> Any:
			"""Hand a question to the specialist."""
			return handup.delegate(specialist, question, label=SPECIALIST_LABEL)

		generalist = Agent(
			model=ScriptedModel(
				[
					_calls("ask_the_specialist", {"question": "pass it on"}, call_id="g1"),
					_final("It could not be passed on."),
				]
			),
			name="generalist",
			tools=[ask_the_specialist],
		)
		outer = generalist.new_session()
		outer_run = outer.chat("pass it on")

		# The second hop was attempted and refused: the third agent's run never existed.
		self.assertEqual(reached, ["pass it on"])
		self.assertEqual(outer_run.status, "Completed")
		self.assertEqual(frappe.db.count("Flow Run", {"parent_run": ["is", "set"]}), 1)
		refusal = self._tool_results_of(specialist)
		self.assertTrue(any("hand work on" in r for r in refusal), refusal)

	def _tool_results_of(self, agent) -> list[str]:
		seen = []
		for call in agent.model.calls:
			for message in call["messages"]:
				if message.get("role") == "tool":
					seen.append(message.get("content") or "")
		return seen


class TestTheApprovalGateItselfIsUntouched(HandupFixture):
	def test_the_confirmation_functions_are_byte_identical_to_the_branch_point(self):
		"""The write-confirmation path is load-bearing: this prototype must not have altered it."""
		import ast
		import inspect
		import subprocess

		import flow.lib.agent as agent_module

		source = inspect.getsource(agent_module)
		baseline = subprocess.run(
			["git", "show", "veyqon:flow/lib/agent.py"],
			cwd=frappe.get_app_path("flow", ".."),
			capture_output=True,
			text=True,
			check=True,
		).stdout

		guarded = ("_invoke", "_resolve_confirmation", "_confirmation_question", "_has_denial")
		for name in guarded:
			# Positive control: a misspelled name would compare None to None and pass silently.
			self.assertIsNotNone(_segment(baseline, name), f"{name} not found in the baseline")
			self.assertIsNotNone(_segment(source, name), f"{name} not found on this branch")
			self.assertEqual(
				_segment(baseline, name), _segment(source, name), f"{name} changed on this branch"
			)
		self.assertIsNone(_segment(baseline, "_no_such_function_"), "the control itself is broken")


def _segment(source: str, name: str) -> str | None:
	import ast

	tree = ast.parse(source)
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name == name:
			return ast.get_source_segment(source, node)
	return None


class TestTheSecurityReviewsFindings(HandupFixture):
	"""Three attacks the security reviewer executed against the first version of this prototype
	(19 Sep 2026). Each is pinned here so the fix cannot quietly come undone."""

	def test_a_second_delegation_in_one_turn_is_still_recorded_as_a_child(self):
		"""The depth guard reads the flag naming the run in progress. A nested run CLEARS that flag
		on the way out, so the second delegation of the same turn sees nothing. It used to be
		stored with no parent — an orphan, and free to delegate again, which a security reviewer
		rode to depth 2. It is now refused instead."""
		executed: list[dict[str, Any]] = []

		@tool(requires_confirmation=True)
		def delete_records(folder: str, count: int) -> str:
			"""Delete records in a folder."""
			executed.append({"folder": folder, "count": count})
			return "done"

		specialist = Agent(
			model=ScriptedModel(
				[
					_calls("delete_records", THE_WRITE, call_id="s1"),
					_calls("delete_records", THE_WRITE, call_id="s2"),
				]
			),
			name="specialist",
			instructions="You look after the records.",
			tools=[delete_records],
		)
		handup.register_specialist(SPECIALIST_LABEL, specialist)

		@tool
		def ask_the_specialist(question: str) -> Any:
			"""Hand a question to the specialist."""
			return handup.delegate(specialist, question, label=SPECIALIST_LABEL)

		generalist = Agent(
			model=ScriptedModel(
				[
					_calls_many(
						[
							("ask_the_specialist", {"question": "a"}, "g1"),
							("ask_the_specialist", {"question": "b"}, "g2"),
						]
					)
				]
			),
			name="generalist",
			tools=[ask_the_specialist],
		)
		outer = generalist.new_session()
		outer_run = outer.chat("do both")
		outer_run.reload()

		# The second delegation is REFUSED, not silently orphaned. What must never happen is a run
		# stored with no parent: that is what reached depth 2 in the reviewer's attack.
		children = frappe.get_all(
			"Flow Run", filters={"parent_run": outer_run.name}, pluck="name", order_by="creation asc"
		)
		self.assertEqual(len(children), 1)
		orphans = [
			r
			for r in frappe.get_all("Flow Run", fields=["name", "parent_run", "session"])
			if not r.parent_run and r.name != outer_run.name
		]
		self.assertEqual(orphans, [], f"a delegated run was stored with no parent: {orphans}")
		self.assertEqual(executed, [])

	def test_a_turn_resumed_without_an_answer_for_its_question_does_not_complete(self):
		"""The delegating tool does not itself require confirmation, so an answers dict that does
		not name its call used to serialise to "" — the caller finished and said the work was done
		while the write was still parked."""
		outer, outer_run, child_run, executed = self.pause_it()

		with self.assertRaises(frappe.ValidationError):
			outer.resume({"s1": "Approve"})  # the CHILD's call id, not the caller's

		self.assertEqual(executed, [])
		outer_run.reload()
		child_run.reload()
		self.assertEqual(outer_run.status, "Paused")
		self.assertEqual(child_run.status, "Paused")

	def test_a_specialist_that_stops_again_does_not_let_the_caller_claim_it_is_done(self):
		"""After the approved write the specialist asked for a second one. The caller used to be
		handed the paused run's empty output and finish the turn — the original failure, restored."""
		outer, executed = self.build(
			specialist_replies=[
				_calls("delete_records", THE_WRITE, call_id="s1"),
				_calls("delete_records", {"folder": "everything else", "count": 99}, call_id="s2"),
			]
		)
		outer_run = outer.chat("clear the 2025 invoices")

		with self.assertRaises(frappe.ValidationError):
			outer.resume({"g1": "Approve"})

		# The approved write ran — it was approved. The one nobody was asked about did not.
		self.assertEqual(executed, [THE_WRITE])
		outer_run.reload()
		self.assertEqual(outer_run.status, "Paused")
		self.assertIsNone(outer_run.output)
