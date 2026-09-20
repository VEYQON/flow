# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""E4 — what a dropped stream costs today.

Characterisation only. Nothing here is a fix; these tests pin the behaviour the E4 spec asks the
owner to decide about, so that whichever option is chosen the change shows up as a test going red.

The mechanism, read in `stream_with_persistence` (`flow_run.py:172-217`): a consumer that stops
iterating raises `GeneratorExit` inside the generator. `GeneratorExit` is a `BaseException`, so it
does NOT enter the `except Exception` arm; it goes straight to `finally`, where `persisted` is still
False and the run is stamped `mark_failed("Stream interrupted")`.

`apply_result` is the only thing that writes `questions`, and it is only reached on `Done`. So a
turn that was about to pause for an approval and lost its consumer first is recorded Failed with
`questions = None`: the approval is not delayed, it does not exist.
"""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.model import ChatResponse, Model, ToolCall
from flow.lib.tool import tool


def _text(text: str) -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _call(name: str, arguments: dict[str, Any], call_id: str) -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


class TestStreamDisconnect(IntegrationTestCase):
	def setUp(self):
		self.model_doc = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "E4 Stream Model",
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		).insert()
		self.agent_doc = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "E4 Stream Agent",
				"model": self.model_doc.name,
				"instructions": "be terse",
				"enabled": 1,
			}
		).insert()

	def tearDown(self):
		frappe.db.rollback()

	def _session(self):
		from flow.lib.session import load_session

		doc = frappe.get_doc({"doctype": "Flow Session", "agent": self.agent_doc.name}).insert(
			ignore_permissions=True
		)
		return load_session(doc.name)

	def _streaming(self, response: ChatResponse, parts: list[str]):
		"""A Model.chat stand-in that streams `parts` then returns `response`."""

		def chat(messages, tools=None, *, stream=False):
			if not stream:
				return response

			def gen():
				yield from parts
				return response

			return gen()

		return chat

	def test_a_consumer_that_stops_early_loses_the_text_it_had_already_seen(self):
		session = self._session()

		with patch.object(
			Model, "chat", side_effect=self._streaming(_text("hello there"), ["hello ", "there"])
		):
			events = session.chat("hi", stream=True)
			next(events)  # RunStarted
			next(events)  # the first text chunk — the consumer has seen "hello "
			events.close()  # the client goes away

		run = frappe.get_doc("Flow Run", {"session": session.name})
		self.assertEqual(run.status, "Failed")
		self.assertEqual(run.error, "Stream interrupted")
		self.assertIsNone(run.output)

	def test_the_persons_message_survives_but_the_reply_does_not(self):
		"""`_persist_turn` stores the person's turn BEFORE the model call (flow_session.py:167),
		so the user row is safe. `apply_result` is what appends the run's OWN messages, and it is
		never reached — so the assistant's reply, and the text the person watched arrive, are
		gone while their question remains."""
		session = self._session()

		with patch.object(
			Model, "chat", side_effect=self._streaming(_text("hello there"), ["hello ", "there"])
		):
			events = session.chat("hi", stream=True)
			next(events)
			next(events)
			events.close()

		rows = frappe.get_doc("Flow Session", session.name).messages
		self.assertEqual([r.role for r in rows], ["system", "user"])

	def test_a_disconnect_erases_a_pending_approval_rather_than_delaying_it(self):
		"""THE SHARP ONE. The turn was about to pause for an approval. The consumer leaves
		first, so `Done` is never yielded, `apply_result` never runs, and `questions` is never
		written. The run is Failed with nothing to come back to."""
		executed: list[float] = []

		@tool(requires_confirmation=True)
		def post_payment(amount: float) -> str:
			"""Post a payment."""
			executed.append(amount)
			return "posted"

		session = self._session()
		session._runtime.tools.append(post_payment)
		session._runtime._tools_by_name[post_payment.name] = post_payment

		with patch.object(
			Model, "chat", side_effect=self._streaming(_call("post_payment", {"amount": 10.0}, "c1"), [])
		):
			events = session.chat("pay someone", stream=True)
			next(events)  # RunStarted
			next(events)  # ToolStarted — the approval question has NOT been emitted yet
			events.close()

		run = frappe.get_doc("Flow Run", {"session": session.name})
		self.assertEqual(run.status, "Failed")
		self.assertIsNone(run.questions)
		self.assertEqual(executed, [])  # nothing ran, which is the one good part

	def test_a_stream_that_is_consumed_to_the_end_persists_normally(self):
		"""Control. If this failed, the three above would be measuring a broken fixture rather
		than a disconnect."""
		session = self._session()

		with patch.object(
			Model, "chat", side_effect=self._streaming(_text("hello there"), ["hello ", "there"])
		):
			list(session.chat("hi", stream=True))

		run = frappe.get_doc("Flow Run", {"session": session.name})
		self.assertEqual(run.status, "Completed")
		self.assertEqual(run.output, "hello there")
		rows = frappe.get_doc("Flow Session", session.name).messages
		self.assertEqual([r.role for r in rows], ["system", "user", "assistant"])
