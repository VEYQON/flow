# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt
"""SPIKE — can ONE pause carry MORE THAN ONE question?

These are CHARACTERISATION tests. Each one records what the engine does TODAY. Nothing here is a
feature and nothing here is a fix. Where a test pins behaviour this spike considers DANGEROUS, its
docstring says so in those words.

The question being answered, and why: a client is being built on the assumption that a single
paused turn can present two approval questions at once. These tests confirm or refute that, and
pin what `resume` does when the answers map does not line up with the questions.

No real model is called: every agent here runs on a scripted fake.
"""

import json
from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.agent import Agent, Question
from flow.lib.model import ChatResponse, ToolCall
from flow.lib.tool import tool

# ---------------------------------------------------------------------------------------------
# Scripted model plumbing (same shape as test_spike_agent_handoff.py's, duplicated rather than
# imported so this spike file stands alone and can be deleted whole)
# ---------------------------------------------------------------------------------------------


class ScriptedModel:
	def __init__(self, responses: list[ChatResponse], model_id: str = "openai/gpt-4o-mini"):
		self.model_id = model_id
		self._responses = list(responses)
		self.calls: list[dict[str, Any]] = []

	def chat(self, messages, tools=None, *, stream=False):
		self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
		if not self._responses:
			raise AssertionError("ScriptedModel ran out of scripted responses")
		return self._responses.pop(0)


def _final(text: str) -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _two_calls() -> ChatResponse:
	"""ONE assistant turn issuing TWO tool calls, both on confirmation-gated tools."""
	return ChatResponse(
		content=None,
		tool_calls=[
			ToolCall(id="k1", name="delete_invoice", arguments={"invoice": "INV-001"}),
			ToolCall(id="k2", name="email_customer", arguments={"to": "a@example.com"}),
		],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


class TwoWriteTools:
	"""Two confirmation-gated tools that record every execution, so 'did it run?' is a fact."""

	def __init__(self):
		self.executed: list[tuple[str, dict[str, Any]]] = []
		recorder = self.executed

		@tool(requires_confirmation=True)
		def delete_invoice(invoice: str) -> str:
			"""Delete an invoice."""
			recorder.append(("delete_invoice", {"invoice": invoice}))
			return f"deleted {invoice}"

		@tool(requires_confirmation=True)
		def email_customer(to: str) -> str:
			"""Email a customer."""
			recorder.append(("email_customer", {"to": to}))
			return f"emailed {to}"

		self.delete_invoice = delete_invoice
		self.email_customer = email_customer

	@property
	def tools(self):
		return [self.delete_invoice, self.email_customer]


def _agent(model: ScriptedModel, tools) -> Agent:
	return Agent(model=model, name="generalist", instructions="You help.", tools=tools)


class TestOnePauseCarriesTwoQuestions(IntegrationTestCase):
	"""(a) How many questions does one pause carry, and does the second call wait or get dropped?"""

	def tearDown(self):
		frappe.db.rollback()

	def test_one_pause_carries_two_questions_one_per_tool_call(self):
		"""ANSWERED: two confirmation-gated calls in one turn produce TWO questions on ONE pause."""
		kit = TwoWriteTools()
		model = ScriptedModel([_two_calls()])
		result = _agent(model, kit.tools).run("Clean up INV-001 and tell the customer")

		self.assertTrue(result.paused)
		self.assertEqual(len(result.questions), 2)
		self.assertEqual([q.key for q in result.questions], ["k1", "k2"])
		for question in result.questions:
			self.assertIsInstance(question, Question)
			self.assertEqual(question.options, ["Approve", "Deny"])
			self.assertTrue(question.allow_other)

	def test_the_second_call_waits_it_is_not_dropped(self):
		"""ANSWERED: the second call is NOT dropped. It waits, with its own question and its own
		arguments, and is still pending in the transcript."""
		kit = TwoWriteTools()
		model = ScriptedModel([_two_calls()])
		agent = _agent(model, kit.tools)
		result = agent.run("Clean up INV-001 and tell the customer")

		second = result.questions[1]
		self.assertEqual(second.key, "k2")
		self.assertIn("email_customer", second.prompt)
		self.assertIn("a@example.com", second.prompt)
		# Neither tool ran, and neither has a tool result in the transcript.
		self.assertEqual(kit.executed, [])
		pending = {call.id for call in agent._pending_calls(result.messages)}
		self.assertEqual(pending, {"k1", "k2"})

	def test_each_question_shows_only_its_own_arguments(self):
		"""The questions are not merged: k1 shows the invoice, k2 shows the address, neither
		shows the other's arguments."""
		kit = TwoWriteTools()
		result = _agent(ScriptedModel([_two_calls()]), kit.tools).run("do both")

		first, second = result.questions
		self.assertIn("INV-001", first.prompt)
		self.assertNotIn("a@example.com", first.prompt)
		self.assertIn("a@example.com", second.prompt)
		self.assertNotIn("INV-001", second.prompt)

	def test_a_gated_call_beside_an_ungated_call_does_not_stop_the_ungated_one(self):
		"""DANGEROUS, and pinned deliberately. When one call in the turn is gated and another is
		not, the UNGATED one EXECUTES during the same turn that pauses for the gated one. The
		person is asked about the delete while the email has already gone out."""
		executed: list[str] = []

		@tool(requires_confirmation=True)
		def delete_invoice(invoice: str) -> str:
			"""Delete an invoice."""
			executed.append("delete_invoice")
			return "deleted"

		@tool
		def email_customer(to: str) -> str:
			"""Email a customer. NOT gated."""
			executed.append("email_customer")
			return "emailed"

		model = ScriptedModel([_two_calls()])
		result = _agent(model, [delete_invoice, email_customer]).run("do both")

		self.assertTrue(result.paused)
		self.assertEqual(len(result.questions), 1)
		self.assertEqual(executed, ["email_customer"])


class TestResumingTwoQuestions(IntegrationTestCase):
	"""(b) What does resume do when `answers` does not match the questions one for one?"""

	def tearDown(self):
		frappe.db.rollback()

	def _paused(self, kit: TwoWriteTools, after: list[ChatResponse] | None = None):
		model = ScriptedModel([_two_calls(), *(after or [])])
		agent = _agent(model, kit.tools)
		return agent, model, agent.run("do both")

	def test_both_approved_runs_both_tools(self):
		"""The baseline: two Approves execute both calls, in the order they were asked."""
		kit = TwoWriteTools()
		agent, _model, paused = self._paused(kit, [_final("Both done.")])
		agent.resume(paused.messages, {"k1": "Approve", "k2": "Approve"})

		self.assertEqual(
			kit.executed,
			[("delete_invoice", {"invoice": "INV-001"}), ("email_customer", {"to": "a@example.com"})],
		)

	def test_answering_only_one_key_silently_redirects_the_other(self):
		"""DANGEROUS, and pinned deliberately. `answers` holding only `k1` does NOT raise and does
		NOT leave k2 pending. `_prepare_resume` iterates every pending call, so k2 gets
		`answers.get("k2")` → None, which is neither "Approve" nor "Deny" and therefore falls into
		the FREE-TEXT REDIRECT branch: the model is told the person asked for changes, with
		`user_feedback: null`. Nothing executes for k2 — but the missing answer is reported to the
		model as if the person had spoken."""
		kit = TwoWriteTools()
		agent, _model, paused = self._paused(kit, [_final("Only the first, then.")])
		result = agent.resume(paused.messages, {"k1": "Approve"})

		# k1 ran; k2 did not.
		self.assertEqual(kit.executed, [("delete_invoice", {"invoice": "INV-001"})])

		k2_result = next(m for m in result.messages if m.get("tool_call_id") == "k2")
		payload = json.loads(k2_result["content"])
		self.assertEqual(payload["status"], "redirect")
		self.assertIsNone(payload["user_feedback"])
		self.assertNotIn("k2", {call.id for call in agent._pending_calls(result.messages)})

	def test_an_empty_answers_map_redirects_both_and_never_raises(self):
		"""The same shape, taken to its limit: `{}` resolves BOTH calls as null redirects. Only a
		transcript with no pending call at all raises."""
		kit = TwoWriteTools()
		agent, _model, paused = self._paused(kit, [_final("Nothing then.")])
		result = agent.resume(paused.messages, {})

		self.assertEqual(kit.executed, [])
		for key in ("k1", "k2"):
			payload = json.loads(next(m for m in result.messages if m.get("tool_call_id") == key)["content"])
			self.assertEqual(payload["status"], "redirect")

	def test_approve_plus_deny_executes_the_approved_one_and_then_halts_the_run(self):
		"""DANGEROUS, and the finding that matters most for a two-question client.
		`{"k1": "Approve", "k2": "Deny"}` is NOT an all-or-nothing rejection. k1's tool RUNS —
		`_prepare_resume` resolves every pending call before `_has_denial` is consulted — and only
		then does the Deny halt the turn. The person who denied the second question has already
		caused the first write to execute."""
		kit = TwoWriteTools()
		agent, model, paused = self._paused(kit)
		result = agent.resume(paused.messages, {"k1": "Approve", "k2": "Deny"})

		self.assertEqual(kit.executed, [("delete_invoice", {"invoice": "INV-001"})])
		# The run stopped without a further model call: the pause turn is the only one scripted.
		self.assertEqual(len(model.calls), 1)
		self.assertIsNone(result.output)
		self.assertFalse(result.paused)
		self.assertEqual(result.iterations, 0)

		k2_result = next(m for m in result.messages if m.get("tool_call_id") == "k2")
		self.assertEqual(json.loads(k2_result["content"])["status"], "denied")

	def test_deny_plus_approve_is_the_same_regardless_of_order(self):
		"""The order of the keys in `answers` does not save the denied-but-approved write: the
		execution order follows the TRANSCRIPT, not the answers map."""
		kit = TwoWriteTools()
		agent, _model, paused = self._paused(kit)
		agent.resume(paused.messages, {"k2": "Approve", "k1": "Deny"})

		self.assertEqual(kit.executed, [("email_customer", {"to": "a@example.com"})])

	def test_an_answer_for_a_key_that_was_never_asked_is_ignored(self):
		"""An unknown key does not raise and does not execute anything. It is simply never read,
		while the two real questions fall through to the null redirect."""
		kit = TwoWriteTools()
		agent, _model, paused = self._paused(kit, [_final("ok")])
		result = agent.resume(paused.messages, {"k9": "Approve"})

		self.assertEqual(kit.executed, [])
		self.assertNotIn("k9", {m.get("tool_call_id") for m in result.messages})


class TestTwoQuestionsThroughAPersistedSession(IntegrationTestCase):
	"""(c) The same thing through the persisted session, which is what a client actually reads."""

	def tearDown(self):
		frappe.db.rollback()

	def test_both_questions_are_stored_on_the_paused_run(self):
		"""ANSWERED for the client: `Flow Run.questions` holds a JSON list of TWO entries, each
		with its own `key`, so a client can render both from one paused run."""
		kit = TwoWriteTools()
		agent = _agent(ScriptedModel([_two_calls()]), kit.tools)
		session = agent.new_session()
		run = session.chat("do both")

		self.assertEqual(run.status, "Paused")
		stored = json.loads(run.questions)
		self.assertEqual(len(stored), 2)
		self.assertEqual([q["key"] for q in stored], ["k1", "k2"])

	def test_resuming_a_session_with_one_answer_of_two_leaves_no_paused_run_behind(self):
		"""DANGEROUS, and pinned deliberately. A client that answers one of two questions and
		resumes does NOT get a still-paused run to answer the rest of. The run leaves Paused, the
		second question is gone, and there is nothing left to approve — the only record that it was
		asked is the null redirect in the transcript."""
		kit = TwoWriteTools()
		agent = _agent(ScriptedModel([_two_calls(), _final("Done what I could.")]), kit.tools)
		session = agent.new_session()
		run = session.chat("do both")
		self.assertEqual(run.status, "Paused")

		resumed = session.resume({"k1": "Approve"})

		self.assertNotEqual(resumed.status, "Paused")
		self.assertEqual(kit.executed, [("delete_invoice", {"invoice": "INV-001"})])
		self.assertFalse(
			frappe.db.get_value("Flow Run", {"session": session.name, "status": "Paused"}, "name")
		)
