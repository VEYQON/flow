# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt
"""A run must append only the messages it produced.

`_new_messages_for_session` decides which of a finished run's transcript entries are new by
counting the session's stored rows and slicing from that index. That is correct only while the
prompt sent to the model has exactly one entry per stored row.

It does not always. `_build_prompt_messages` can prepend a system message that no row backs —
the memory block, when the transcript has no system row of its own to append it to. The slice
then starts one entry early and the session stores the last of its own prior messages a second
time.

These tests reproduce that through the public API, with no real model.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.agent import Agent
from flow.lib.model import ChatResponse, Model
from flow.lib.session import load_session, new_session
from flow.memory.memory import save_memory
from flow.tools.builtins import sync_builtin_tools


def _final(text: str = "ok") -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


class TestNewMessagesForSession(IntegrationTestCase):
	def setUp(self):
		sync_builtin_tools()
		self.model = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": f"NMS Model {frappe.generate_hash(length=6)}",
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
		doc = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": f"NMS Agent {frappe.generate_hash(length=6)}",
				"model": self.model.name,
				"instructions": "be terse",
				"enabled": 1,
			}
		)
		doc.append("tools", {"tool": "update_memory"})
		self.agent_doc = doc.insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _rows(self, session: str) -> list[tuple[str, str]]:
		doc = frappe.get_doc("Flow Session", session)
		return [(row.role, row.content or "") for row in doc.messages]

	def _session_without_a_stored_system_row(self):
		"""A session linked to an agent record, continued with a code agent that has no
		instructions — so the first turn stores no system row, while the agent link still makes
		the memory block available."""
		convo = new_session(self.agent_doc)
		save_memory(self.agent_doc.name, content="The customer prefers email.", scope="agent")
		runtime = Agent(model=Model(model_id="openai/gpt-4o-mini"), name="continuer", instructions=None)
		return load_session(convo.name, agent=runtime)

	def test_the_prompt_carries_a_system_message_no_row_backs(self):
		"""The precondition, asserted rather than assumed: the prompt is one entry longer than
		the stored rows, and the extra entry is a leading system message."""
		convo = self._session_without_a_stored_system_row()
		with patch.object(Model, "chat", return_value=_final("hi")):
			convo.chat("hello")

		convo.reload()
		prompt = convo._build_prompt_messages()
		stored = frappe.db.count("Flow Session Message", {"parent": convo.name})
		self.assertEqual(prompt[0]["role"], "system")
		self.assertNotEqual(convo.messages[0].role, "system")
		self.assertEqual(len(prompt), stored + 1)

	def test_a_run_does_not_store_a_prior_message_twice(self):
		"""The bug. One turn, one user message — the transcript must not hold it twice."""
		convo = self._session_without_a_stored_system_row()
		with patch.object(Model, "chat", return_value=_final("hi")):
			convo.chat("hello")

		rows = self._rows(convo.name)
		self.assertEqual([role for role, _ in rows], ["user", "assistant"])
		self.assertEqual([content for _, content in rows], ["hello", "hi"])

	def test_the_duplicate_compounds_over_turns(self):
		"""Left alone it is not a one-off: every turn re-stores the entry before it."""
		convo = self._session_without_a_stored_system_row()
		with patch.object(Model, "chat", return_value=_final("first")):
			convo.chat("one")
		convo.reload()
		with patch.object(Model, "chat", return_value=_final("second")):
			convo.chat("two")

		self.assertEqual(
			[content for _, content in self._rows(convo.name)],
			["one", "first", "two", "second"],
		)

	def test_an_ordinary_session_is_unaffected(self):
		"""The control: with a stored system row the memory block is appended to it, the prompt
		matches the rows one for one, and nothing is duplicated. If this test ever fails the fix
		has broken the common path, not the rare one."""
		convo = new_session(self.agent_doc)
		save_memory(self.agent_doc.name, content="The customer prefers email.", scope="agent")
		with patch.object(Model, "chat", return_value=_final("hi")):
			convo.chat("hello")

		self.assertEqual(
			[role for role, _ in self._rows(convo.name)],
			["system", "user", "assistant"],
		)

	def test_a_session_with_no_memory_block_is_unaffected(self):
		"""The second control: no memory stored, so no unbacked system message, so no duplicate
		even on the same shape of session."""
		convo = new_session(self.agent_doc)
		runtime = Agent(model=Model(model_id="openai/gpt-4o-mini"), name="continuer", instructions=None)
		convo = load_session(convo.name, agent=runtime)
		with patch.object(Model, "chat", return_value=_final("hi")):
			convo.chat("hello")

		self.assertEqual([role for role, _ in self._rows(convo.name)], ["user", "assistant"])
