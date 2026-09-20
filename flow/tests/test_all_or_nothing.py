# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""S14b — all-or-nothing in a group of approvals.

S14 settled that a denial in a group executes nothing. It deliberately left the other half open:
`{"k1": "Approve", "k2": "some free text"}` still executes `k1`. This module is the stricter rule —
in a group of more than one question, a call executes only if EVERY answer is exactly "Approve" —
built so the decision between the two costs a merge rather than another build.

Nothing here decides which is right. The spec's status says so, and the tests that pin the
opposite answer live on `veyqon` in `test_deny_stops_batch.py`, unmodified, so whichever way it is
decided the other side goes red rather than quiet.

Every tool records its own calls, so "nothing executed" is asserted against a list.
"""

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

from frappe.tests import UnitTestCase

from flow.lib import agent as agent_module
from flow.lib.agent import Agent, Done, ToolEnded
from flow.lib.model import ChatResponse, ToolCall
from flow.lib.tool import tool

WITHHELD = {
	"status": "not_executed",
	"message": "Nothing in this group ran. The user approved this action but denied another action in the same group.",
	"user_answer": "Approve",
}
DENIED = {"status": "denied", "message": "User denied this tool call."}


class FakeModel:
	def __init__(self, responses: list[ChatResponse]):
		self._responses = list(responses)
		self.calls: list[dict[str, Any]] = []

	def chat(self, messages, tools=None, *, stream=False):
		self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
		if not self._responses:
			raise AssertionError("FakeModel ran out of scripted responses")
		response = self._responses.pop(0)
		if stream:
			return _scripted_stream(response)
		return response


def _scripted_stream(response: ChatResponse):
	if response.content:
		yield response.content
	return response


def _final(text: str) -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _calls(*specs: tuple[str, dict[str, Any], str]) -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=args) for name, args, call_id in specs],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, str]:
	return {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}


class _Recorder:
	def __init__(self):
		self.ran: list[tuple[str, dict[str, Any]]] = []
		me = self

		@tool(requires_confirmation=True)
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			me.ran.append(("send_money", {"to": to, "amount": amount}))
			return f"sent {amount} to {to}"

		@tool(requires_confirmation=True)
		def delete_records(folder: str) -> str:
			"""Delete a folder of records."""
			me.ran.append(("delete_records", {"folder": folder}))
			return f"deleted {folder}"

		@tool(requires_confirmation=True)
		def send_email(to: str) -> str:
			"""Send an email."""
			me.ran.append(("send_email", {"to": to}))
			return f"emailed {to}"

		self.send_money, self.delete_records, self.send_email = send_money, delete_records, send_email
		self.tools = [send_money, delete_records, send_email]


def _pause_on_two(recorder: _Recorder, extra: list[ChatResponse] | None = None):
	model = FakeModel(
		[
			_calls(
				("send_money", {"to": "alice", "amount": 500}, "k1"),
				("delete_records", {"folder": "invoices"}, "k2"),
			),
			*(extra or []),
		]
	)
	agent = Agent(model=model, tools=recorder.tools)
	return agent, model, agent.run("pay alice and clear the invoices")


def _pause_on_one(recorder: _Recorder, extra: list[ChatResponse] | None = None):
	model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1")), *(extra or [])])
	agent = Agent(model=model, tools=recorder.tools)
	return agent, model, agent.run("pay alice")


class TestEveryAnswerInAGroupMustBeApprove(UnitTestCase):
	"""The change. Under S14 alone, the first two of these execute `k1`."""

	def test_the_pause_really_does_carry_two_questions_and_nothing_has_run(self):
		"""Precondition. Without it every other test here measures nothing."""
		recorder = _Recorder()
		_agent, _model, paused = _pause_on_two(recorder)

		self.assertTrue(paused.paused)
		self.assertEqual([q.key for q in paused.questions], ["k1", "k2"])
		self.assertEqual(recorder.ran, [])

	def test_approve_beside_free_text_executes_nothing(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("adjusted")])

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "make it smaller"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(json.loads(_tool_results(resumed.messages)["k1"]), WITHHELD)
		redirect = json.loads(_tool_results(resumed.messages)["k2"])
		self.assertEqual(redirect["status"], "redirect")
		self.assertEqual(redirect["user_feedback"], "make it smaller")

	def test_free_text_beside_approve_executes_nothing_either_way_round(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("adjusted")])

		agent.resume(paused.messages, {"k1": "make it smaller", "k2": "Approve"})

		self.assertEqual(recorder.ran, [])

	def test_an_unanswered_question_withholds_the_answered_one(self):
		"""A group is answered as a group. Half an answer is not consent to the other half."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("ok")])

		resumed = agent.resume(paused.messages, {"k1": "Approve"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(json.loads(_tool_results(resumed.messages)["k1"]), WITHHELD)

	def test_two_approvals_and_one_free_text_withhold_both_approvals(self):
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("send_money", {"to": "alice", "amount": 500}, "k1"),
					("delete_records", {"folder": "invoices"}, "k2"),
					("send_email", {"to": "bob"}, "k3"),
				),
				_final("ok"),
			]
		)
		agent = Agent(model=model, tools=recorder.tools)
		paused = agent.run("do three things")

		agent.resume(paused.messages, {"k1": "Approve", "k2": "Approve", "k3": "not yet"})

		self.assertEqual(recorder.ran, [])

	def test_an_answer_for_nothing_pending_still_withholds_the_group(self):
		"""The second half of the unanimity test. A key that names no pending call cannot be
		honoured, so a group carrying one was not unanimously approved — the same direction
		`_has_denial` already takes for a stray "Deny", and the same reason: a malformed answer
		map fails toward withholding."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("ok")])

		agent.resume(paused.messages, {"k1": "Approve", "k2": "Approve", "not_a_call": "later"})

		self.assertEqual(recorder.ran, [])

	def test_the_run_still_continues_so_the_model_can_re_ask(self):
		"""Withholding is not refusing. Free text is still a redirect, so the turn goes on —
		that is what separates this rule from a denial, which halts."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("shall I try again?")])

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "make it smaller"})

		self.assertEqual(resumed.output, "shall I try again?")
		self.assertFalse(resumed.paused)

	def test_the_streaming_resume_withholds_identically(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("ok")])

		events = list(agent.resume(paused.messages, {"k1": "Approve", "k2": "wait"}, stream=True))

		self.assertEqual(recorder.ran, [])
		ended = {e.id: e.result for e in events if isinstance(e, ToolEnded)}
		self.assertEqual(json.loads(ended["k1"]), WITHHELD)
		self.assertTrue(any(isinstance(e, Done) for e in events))


class TestWhatMustNotChange(UnitTestCase):
	"""S14b narrows groups of more than one. It must not touch anything else, and these are the
	tests that say so. Each of them passes on `veyqon` too; that is their job."""

	def test_two_approvals_still_execute_both_and_the_run_continues(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, [_final("both done")])

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Approve"})

		self.assertEqual(
			recorder.ran,
			[("send_money", {"to": "alice", "amount": 500}), ("delete_records", {"folder": "invoices"})],
		)
		self.assertEqual(resumed.output, "both done")

	def test_approve_beside_deny_still_executes_nothing_and_still_halts(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(json.loads(_tool_results(resumed.messages)["k1"]), WITHHELD)
		self.assertEqual(json.loads(_tool_results(resumed.messages)["k2"]), DENIED)
		self.assertIsNone(resumed.output)
		self.assertEqual(resumed.iterations, 0)

	def test_a_lone_approve_still_executes(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_one(recorder, [_final("paid")])

		resumed = agent.resume(paused.messages, {"c1": "Approve"})

		self.assertEqual(recorder.ran, [("send_money", {"to": "alice", "amount": 500})])
		self.assertEqual(_tool_results(resumed.messages)["c1"], "sent 500 to alice")
		self.assertEqual(resumed.output, "paid")

	def test_a_lone_deny_is_byte_identical(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_one(recorder)

		resumed = agent.resume(paused.messages, {"c1": "Deny"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(json.loads(_tool_results(resumed.messages)["c1"]), DENIED)
		self.assertIsNone(resumed.output)

	def test_a_lone_free_text_is_byte_identical(self):
		"""The single-question case the whole rule turns on. One question answered with free text
		is a redirect and always was; only a GROUP is narrowed."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_one(recorder, [_final("adjusted")])

		resumed = agent.resume(paused.messages, {"c1": "make it 400"})

		self.assertEqual(recorder.ran, [])
		record = json.loads(_tool_results(resumed.messages)["c1"])
		self.assertEqual(record["status"], "redirect")
		self.assertEqual(record["user_feedback"], "make it 400")
		self.assertEqual(resumed.output, "adjusted")

	def test_a_lone_unanswered_question_is_byte_identical(self):
		"""One question, no answer: a redirect on `None`, exactly as today. The new rule must not
		reach this — it is the same shape as the group case it DOES reach, which is why it is
		worth pinning separately."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_one(recorder, [_final("ok")])

		resumed = agent.resume(paused.messages, {})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(json.loads(_tool_results(resumed.messages)["c1"])["status"], "redirect")

	def test_one_question_with_a_stray_answer_beside_it_still_executes(self):
		"""The test that holds `len(pending) > 1` in place. Drop that guard and the unanimity
		rule reaches a LONE question too: this resume would withhold, where today it executes.
		Written because a probe that dropped the guard came back green without it — the claim
		that single-question behaviour is byte-identical was, until this, unmeasured for the one
		input that distinguishes the two."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_one(recorder, [_final("paid")])

		agent.resume(paused.messages, {"c1": "Approve", "not_a_call": "later"})

		self.assertEqual(recorder.ran, [("send_money", {"to": "alice", "amount": 500})])

	def test_a_pending_call_on_an_ungated_tool_is_unchanged(self):
		recorder = _Recorder()
		_agent, _model, paused = _pause_on_two(recorder)
		ran: list[str] = []

		@tool
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			ran.append(to)
			return "sent"

		resuming = Agent(model=FakeModel([_final("ok")]), tools=[send_money, recorder.delete_records])
		resumed = resuming.resume(paused.messages, {"k1": "Approve", "k2": "later"})

		self.assertEqual(ran, [])
		self.assertEqual(_tool_results(resumed.messages)["k1"], "Approve")


class TestTheLoadBearingFunctionsAreUntouched(UnitTestCase):
	"""CLAUDE.md rule 4. S14b reads nothing from these and changes none of them.

	The digests are literals, taken from the unmodified engine. A version of this pin that
	resolved its baseline by shelling out to git silently skipped itself wherever the comparison
	branch was not a local ref; that failure mode has been observed, so this one carries its own
	baseline and cannot opt out.
	"""

	BASELINE_DIGESTS: ClassVar[dict[str, str]] = {
		"_invoke": "745260a7da1fe7e9221cf6257de98eeb7c7e23edf5eb4e1ce17d4d0505f5fa4f",
		"_resolve_confirmation": "adbb8b26e0b0fb166a5a9fff1c658c531d081b3bd2f2967e55b8e838b5ab8f47",
		"_confirmation_question": "32916612298d4ebdb423d9904e268992a2e638b56a67f2ca14fa67deba9cf34c",
		"_has_denial": "80f799b6827afea159589dcee7889282ad8c376aacc15be424c273cd0e55b209",
	}

	def _engine_source(self) -> str:
		return Path(agent_module.__file__).read_text()

	def _digests(self, text: str) -> dict[str, str]:
		return {
			node.name: hashlib.sha256(ast.get_source_segment(text, node).encode()).hexdigest()
			for node in ast.walk(ast.parse(text))
			if isinstance(node, ast.FunctionDef) and node.name in self.BASELINE_DIGESTS
		}

	def test_the_four_functions_are_byte_identical_to_their_reviewed_form(self):
		found = self._digests(self._engine_source())

		self.assertEqual(sorted(found), sorted(self.BASELINE_DIGESTS))
		for name, digest in self.BASELINE_DIGESTS.items():
			self.assertEqual(found[name], digest, f"{name} changed; rule 4 requires a spec that names it")

	def test_the_comparison_can_fail(self):
		"""The positive control: the same comparison over a deliberately altered copy must report
		a difference, or the test above proves nothing."""
		altered = self._engine_source().replace('if answer == "Approve":', 'if answer == "approve":', 1)

		digests = self._digests(altered)

		self.assertNotEqual(digests["_resolve_confirmation"], self.BASELINE_DIGESTS["_resolve_confirmation"])
		self.assertEqual(digests["_has_denial"], self.BASELINE_DIGESTS["_has_denial"])
