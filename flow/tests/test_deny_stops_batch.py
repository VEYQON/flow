# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""S14 — a Deny in a batch executes nothing.

One pause can carry more than one question. Before this change, answering two of them
`{"k1": "Approve", "k2": "Deny"}` ran k1's tool and only then halted the turn: a person who
refused one action in a group had not stopped the group.

These tests exercise the real resume path. Every tool here records its own calls, so "nothing
executed" is asserted against a list, never inferred from the result shape.
"""

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

from frappe.tests import UnitTestCase

from flow.lib.agent import Agent, Done, ToolEnded
from flow.lib.model import ChatResponse, ToolCall
from flow.lib.tool import tool


class FakeModel:
	"""Returns scripted responses and remembers every call, so a test can assert the model
	was not consulted again."""

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
	"""One assistant turn asking for several tools at once: (name, arguments, call_id)."""
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=args) for name, args, call_id in specs],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


WITHHELD = {
	"status": "not_executed",
	"message": "Nothing in this group ran. The user approved this action but denied another action in the same group.",
	"user_answer": "Approve",
}
DENIED = {"status": "denied", "message": "User denied this tool call."}


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, str]:
	"""tool_call_id → content, for every tool result in the transcript."""
	return {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}


class _Recorder:
	"""A set of confirmation-gated tools that record what actually ran."""

	def __init__(self):
		self.ran: list[tuple[str, dict[str, Any]]] = []

		recorder = self

		@tool(requires_confirmation=True)
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			recorder.ran.append(("send_money", {"to": to, "amount": amount}))
			return f"sent {amount} to {to}"

		@tool(requires_confirmation=True)
		def delete_records(folder: str) -> str:
			"""Delete a folder of records."""
			recorder.ran.append(("delete_records", {"folder": folder}))
			return f"deleted {folder}"

		@tool(requires_confirmation=True)
		def send_email(to: str) -> str:
			"""Send an email."""
			recorder.ran.append(("send_email", {"to": to}))
			return f"emailed {to}"

		self.send_money = send_money
		self.delete_records = delete_records
		self.send_email = send_email
		self.tools = [send_money, delete_records, send_email]


def _pause_on_two(recorder: _Recorder, extra: list[ChatResponse] | None = None):
	"""Run to a pause holding two questions: k1 send_money, k2 delete_records."""
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
	paused = agent.run("pay alice and clear the invoices")
	return agent, model, paused


class TestDenyStopsTheBatch(UnitTestCase):
	def test_the_pause_really_does_carry_two_questions_and_nothing_has_run(self):
		"""Precondition. If this fails, every other test in this class is measuring nothing."""
		recorder = _Recorder()
		_agent, _model, paused = _pause_on_two(recorder)

		self.assertTrue(paused.paused)
		self.assertEqual([q.key for q in paused.questions], ["k1", "k2"])
		self.assertEqual(recorder.ran, [])

	def test_approve_beside_deny_executes_nothing(self):
		recorder = _Recorder()
		# Only the pausing response is scripted: another model call would raise.
		agent, model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(len(model.calls), 1)
		self.assertFalse(resumed.paused)

	def test_deny_beside_approve_is_the_same_whichever_way_round_the_answers_are(self):
		"""Same transcript, the Deny on the first call instead of the second."""
		recorder = _Recorder()
		agent, model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Deny", "k2": "Approve"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(len(model.calls), 1)
		self.assertFalse(resumed.paused)

	def test_the_order_of_the_answers_map_does_not_change_it(self):
		"""The same two answers, inserted in the opposite order. Execution follows the
		transcript, so a dict that happens to iterate Deny-first must not be what saves it."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder)
		answers = {}
		answers["k2"] = "Deny"
		answers["k1"] = "Approve"

		agent.resume(paused.messages, answers)

		self.assertEqual(recorder.ran, [])

	def test_one_deny_withholds_every_approve_in_a_batch_of_three(self):
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("send_money", {"to": "alice", "amount": 500}, "k1"),
					("send_email", {"to": "bob"}, "k2"),
					("delete_records", {"folder": "invoices"}, "k3"),
				)
			]
		)
		agent = Agent(model=model, tools=recorder.tools)
		paused = agent.run("do three things")

		agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny", "k3": "Approve"})

		self.assertEqual(recorder.ran, [])

	def test_the_withheld_call_records_the_persons_own_answer(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		results = _tool_results(resumed.messages)
		self.assertEqual(json.loads(results["k1"]), WITHHELD)
		self.assertEqual(json.loads(results["k1"])["user_answer"], "Approve")

	def test_the_denied_calls_own_result_is_unchanged(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertEqual(json.loads(_tool_results(resumed.messages)["k2"]), DENIED)

	def test_the_run_halts_exactly_as_a_deny_halts_it_today(self):
		recorder = _Recorder()
		agent, model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertIsNone(resumed.output)
		self.assertEqual(resumed.iterations, 0)
		self.assertFalse(resumed.paused)
		self.assertEqual(len(model.calls), 1)

	def test_every_pending_call_still_gets_a_result_so_none_is_left_dangling(self):
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder)

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertEqual(sorted(_tool_results(resumed.messages)), ["k1", "k2"])

	def test_the_streaming_resume_withholds_identically(self):
		recorder = _Recorder()
		agent, model, paused = _pause_on_two(recorder)

		events = list(agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"}, stream=True))

		self.assertEqual(recorder.ran, [])
		self.assertEqual(len(model.calls), 1)
		ended = [e for e in events if isinstance(e, ToolEnded)]
		self.assertEqual([e.id for e in ended], ["k1", "k2"])
		self.assertEqual(json.loads(ended[0].result), WITHHELD)
		self.assertEqual(json.loads(ended[1].result), DENIED)
		self.assertIsInstance(events[-1], Done)
		self.assertFalse(events[-1].result.paused)
		self.assertIsNone(events[-1].result.output)


class TestWhatS14MustNotChange(UnitTestCase):
	"""Approve-only, free-text-only and single-question behaviour, pinned."""

	def test_two_approvals_still_execute_both_and_the_run_continues(self):
		recorder = _Recorder()
		agent, model, paused = _pause_on_two(recorder, extra=[_final("both done")])

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "Approve"})

		self.assertEqual(
			recorder.ran,
			[("send_money", {"to": "alice", "amount": 500}), ("delete_records", {"folder": "invoices"})],
		)
		self.assertEqual(resumed.output, "both done")
		self.assertEqual(len(model.calls), 2)

	def test_approve_beside_free_text_still_executes_the_approved_one_and_redirects_the_other(self):
		"""Today's behaviour, pinned deliberately. The spec's Open question 1 asks the owner
		whether free text should stop the batch as a Deny now does; until that is decided this
		test is what makes a change to it visible."""
		recorder = _Recorder()
		agent, _model, paused = _pause_on_two(recorder, extra=[_final("adjusted")])

		resumed = agent.resume(paused.messages, {"k1": "Approve", "k2": "make it smaller"})

		self.assertEqual(recorder.ran, [("send_money", {"to": "alice", "amount": 500})])
		redirect = json.loads(_tool_results(resumed.messages)["k2"])
		self.assertEqual(redirect["status"], "redirect")
		self.assertEqual(redirect["user_feedback"], "make it smaller")
		self.assertEqual(resumed.output, "adjusted")

	def test_a_lone_approve_still_executes(self):
		recorder = _Recorder()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1")), _final("done")])
		agent = Agent(model=model, tools=recorder.tools)
		paused = agent.run("pay alice")

		resumed = agent.resume(paused.messages, {"c1": "Approve"})

		self.assertEqual(recorder.ran, [("send_money", {"to": "alice", "amount": 500})])
		self.assertEqual(_tool_results(resumed.messages)["c1"], "sent 500 to alice")
		self.assertEqual(resumed.output, "done")

	def test_a_lone_deny_halts_and_its_record_is_byte_identical(self):
		recorder = _Recorder()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1"))])
		agent = Agent(model=model, tools=recorder.tools)
		paused = agent.run("pay alice")

		resumed = agent.resume(paused.messages, {"c1": "Deny"})

		self.assertEqual(recorder.ran, [])
		self.assertEqual(json.loads(_tool_results(resumed.messages)["c1"]), DENIED)
		self.assertIsNone(resumed.output)
		self.assertEqual(len(model.calls), 1)

	def test_a_lone_free_text_answer_still_redirects_and_the_run_continues(self):
		recorder = _Recorder()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1")), _final("ok")])
		agent = Agent(model=model, tools=recorder.tools)
		paused = agent.run("pay alice")

		resumed = agent.resume(paused.messages, {"c1": "pay bob instead"})

		self.assertEqual(recorder.ran, [])
		redirect = json.loads(_tool_results(resumed.messages)["c1"])
		self.assertEqual(redirect["status"], "redirect")
		self.assertEqual(redirect["user_feedback"], "pay bob instead")
		self.assertEqual(resumed.output, "ok")

	def test_a_pending_call_on_an_ungated_tool_resolves_as_today_even_in_a_denied_batch(self):
		"""`_prepare_resume`'s else-branch: a pending call whose tool does not require
		confirmation records the answer and executes nothing, with or without a Deny beside it.
		Reachable by resuming with an agent whose copy of the tool is ungated."""
		recorder = _Recorder()
		_agent, _model, paused = _pause_on_two(recorder)

		ran: list[str] = []

		@tool
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			ran.append(to)
			return "sent"

		resuming = Agent(
			model=FakeModel([]),
			tools=[send_money, recorder.delete_records],
		)
		resumed = resuming.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertEqual(ran, [])
		self.assertEqual(recorder.ran, [])
		self.assertEqual(_tool_results(resumed.messages)["k1"], "Approve")


class TestTheLoadBearingFunctionsAreUntouched(UnitTestCase):
	"""CLAUDE.md rule 4. These four decide what executes and on which answer; S14 reads
	`_has_denial` and changes none of them."""

	LOAD_BEARING = ("_invoke", "_resolve_confirmation", "_confirmation_question", "_has_denial")

	def _sources(self, text: str) -> dict[str, str]:
		tree = ast.parse(text)
		found: dict[str, str] = {}
		for node in ast.walk(tree):
			if isinstance(node, ast.FunctionDef) and node.name in self.LOAD_BEARING:
				found[node.name] = ast.get_source_segment(text, node)
		return found

	def _baseline(self) -> str | None:
		repo = Path(__file__).resolve().parents[2]
		try:
			return subprocess.run(
				["git", "show", "veyqon:flow/lib/agent.py"],
				cwd=repo,
				capture_output=True,
				text=True,
				check=True,
			).stdout
		except (OSError, subprocess.CalledProcessError):
			return None

	def test_the_four_functions_are_byte_identical_to_veyqon(self):
		baseline = self._baseline()
		if baseline is None:
			self.skipTest("veyqon not resolvable from this checkout")
		current = Path(__file__).resolve().parents[1].joinpath("lib", "agent.py").read_text()

		before, after = self._sources(baseline), self._sources(current)

		# Positive control: the comparison must be capable of failing. If a name is missing
		# from either side, `None == None` would pass silently.
		self.assertEqual(sorted(before), sorted(self.LOAD_BEARING))
		self.assertEqual(sorted(after), sorted(self.LOAD_BEARING))
		for name in self.LOAD_BEARING:
			self.assertEqual(after[name], before[name], f"{name} changed")

	def test_the_comparison_can_fail(self):
		"""The control for the test above: the same comparison over a deliberately altered
		copy must report a difference."""
		current = Path(__file__).resolve().parents[1].joinpath("lib", "agent.py").read_text()
		altered = current.replace('if answer == "Approve":', 'if answer == "approve":', 1)

		self.assertNotEqual(altered, current)
		self.assertNotEqual(
			self._sources(altered)["_resolve_confirmation"],
			self._sources(current)["_resolve_confirmation"],
		)
