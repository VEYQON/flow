# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""S16c — an unattended run keeps no notes by construction, and a kept note is budgeted.

The decision that a run with nobody to answer keeps no notes was already made. How it was enforced
is what this module is about. The tool built for such a run is deliberately UNGATED — a question
nobody can answer parks the run forever — and the only thing that stopped it writing was a flag on
worker-global state, read at the moment the body ran. So an ungated write tool's safety depended on
a second statement, somewhere else, having set a flag correctly.

Here the refusal is a property of the object instead: the tool built for an unattended run cannot
write, whatever any flag says, because its body decides from the value it was bound with.

The flag refusal is kept as well, for the callers a binding cannot see, and that is asserted too.
"""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.model import ChatResponse, Model, ToolCall
from flow.memory import memory as memory_module
from flow.tools.builtins import bind_update_memory

NOT_EXECUTED = "not_executed"


class FakeModel:
	"""Scripted responses, remembering every call so a test can assert what the model was shown."""

	def __init__(self, responses: list[ChatResponse]):
		self._responses = list(responses)
		self.calls: list[dict[str, Any]] = []

	def chat(self, messages, tools=None, *, stream=False):
		self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
		if not self._responses:
			raise AssertionError("FakeModel ran out of scripted responses")
		return self._responses.pop(0)


def _final(text: str = "done") -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _memory_call(arguments: dict[str, Any], call_id: str = "m1") -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name="update_memory", arguments=arguments)],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _model_doc(**overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Model",
		"title": "S16c Model",
		"model_id": "openai/gpt-4o-mini",
		"enabled": 1,
	}
	doc.update(overrides)
	return doc


def _agent_doc(model_name: str, **overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Agent",
		"title": "S16c Agent",
		"model": model_name,
		"instructions": "Be terse.",
		"enabled": 1,
		"tools": [{"tool": "update_memory"}],
	}
	doc.update(overrides)
	return doc


def _result_of(value: Any) -> dict[str, Any]:
	"""A tool's return value as a dict, whether it came back as one or as serialised JSON."""
	import json

	if isinstance(value, dict):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return {"_raw": value}


# ---------------------------------------------------------------------------------------------
# The refusal is a property of the tool, not of the worker's state
# ---------------------------------------------------------------------------------------------


class TestAnUnattendedToolCannotWrite(IntegrationTestCase):
	"""Features 1, 2, 3, 4 and 7."""

	def setUp(self):
		self.model_doc = frappe.get_doc(_model_doc()).insert()
		self.agent = frappe.get_doc(_agent_doc(self.model_doc.name)).insert()
		self.unattended = bind_update_memory(self.agent.name, unattended=True)
		self.attended = bind_update_memory(self.agent.name)

	def tearDown(self):
		frappe.flags.flow_run = None
		frappe.flags.flow_unattended = None
		frappe.db.rollback()

	def _rows(self) -> list[Any]:
		return frappe.get_all(
			"Flow Agent Memory", filters={"agent": self.agent.name}, fields=["name", "content", "scope"]
		)

	def test_it_refuses_with_the_flag_explicitly_false(self):
		"""Feature 1. The flag set to the value that used to mean "go ahead and write".

		This is the whole point of the change. Before it, this call wrote a row: the tool was
		ungated, its body asked the flag, and the flag said a person was there.
		"""
		frappe.flags.flow_unattended = False

		result = _result_of(self.unattended(content="Learned overnight.", scope="user"))

		self.assertEqual(result.get("status"), NOT_EXECUTED)
		self.assertEqual(result.get("reason"), "unattended")
		self.assertEqual(self._rows(), [])

	def test_it_refuses_whatever_the_flag_says(self):
		"""Feature 2. Every value the flag can hold, including not being there at all. A refusal
		that depends on worker state is a refusal that can be turned off by a bug two files away."""
		for value in (None, False, True, 0, 1, "", "yes"):
			with self.subTest(flag=value):
				frappe.flags.flow_unattended = value
				result = _result_of(self.unattended(content="Learned overnight.", scope="user"))
				self.assertEqual(result.get("status"), NOT_EXECUTED)
				self.assertEqual(self._rows(), [])

		frappe.flags.pop("flow_unattended", None)
		result = _result_of(self.unattended(content="Learned overnight.", scope="user"))
		self.assertEqual(result.get("status"), NOT_EXECUTED)
		self.assertEqual(self._rows(), [])

	def test_it_never_reaches_the_write_path_at_all(self):
		"""Feature 3. Not "it writes nothing" — it does not get as far as the function that could.

		Asserting on the absence of a row leaves the write path reached and merely refusing, which
		is the state this change replaces. This asserts on the call that never happens, so nothing
		below the tool can be the thing that saves it.
		"""

		def explode(*args, **kwargs):
			raise AssertionError("the unattended tool reached save_memory")

		with patch.object(memory_module, "save_memory", explode):
			result = _result_of(self.unattended(content="Learned overnight.", scope="user"))

		self.assertEqual(result.get("status"), NOT_EXECUTED)
		self.assertEqual(self._rows(), [])

	def test_control_an_attended_tool_does_reach_the_write_path(self):
		"""Feature 4. Without this, the test above passes on a tool that reaches nothing ever —
		a tool that had simply stopped working would look identical to one refusing correctly."""
		seen: list[dict[str, Any]] = []

		def record(agent, **kwargs):
			seen.append({"agent": agent, **kwargs})
			return {"action": "added", "memory_id": "fake"}

		with patch.object(memory_module, "save_memory", record):
			self.attended(content="Learned in a conversation.", scope="user")

		self.assertEqual(len(seen), 1)
		self.assertEqual(seen[0]["agent"], self.agent.name)
		self.assertTrue(seen[0]["from_conversation"])

	def test_the_flag_refusal_is_still_there_for_a_caller_the_binding_cannot_see(self):
		"""Feature 7. The binding is the first defence, not the only one.

		A resume is always treated as attended, and a session with no agent record is never
		rebound, so there are paths where the tool in hand was built the attended way inside a run
		with nobody to answer. The flag refusal in `save_memory` is what covers those, and removing
		it because the binding now exists would reopen exactly that hole."""
		frappe.flags.flow_unattended = True

		result = _result_of(self.attended(content="Learned overnight.", scope="user"))

		self.assertEqual(result.get("status"), NOT_EXECUTED)
		self.assertEqual(result.get("reason"), "unattended")
		self.assertEqual(self._rows(), [])

	def test_the_two_bindings_differ_in_what_they_can_do_not_only_in_their_gate(self):
		"""The gate and the ability to write are separate things, and this pins both at once:
		the unattended tool is ungated (so it never parks a run) AND cannot write (so being
		ungated costs nothing)."""
		self.assertFalse(self.unattended.requires_confirmation)
		self.assertTrue(self.attended.requires_confirmation)

		frappe.flags.flow_unattended = False
		self.assertEqual(
			_result_of(self.unattended(content="A note.", scope="user")).get("status"), NOT_EXECUTED
		)
		self.assertEqual(_result_of(self.attended(content="A note.", scope="user")).get("action"), "added")


# ---------------------------------------------------------------------------------------------
# The flags do not outlive the run that set them
# ---------------------------------------------------------------------------------------------


class TestTheFlagsDoNotOutliveTheRun(IntegrationTestCase):
	"""Features 5 and 6, on the NON-streaming path.

	The streamed path grew a `finally` that clears both flags, with a comment saying why: a
	trigger's "nobody is here" carried into the next request this worker serves would silently stop
	that person's notes being kept. The non-streaming path does the same thing and nothing asserted
	it. Both directions matter — a flag that fails to CLEAR silences a person's notes, and a flag
	that fails to be SET used to mean an ungated tool wrote in a run nobody was watching.
	"""

	def setUp(self):
		self.model_doc = frappe.get_doc(_model_doc()).insert()
		self.agent = frappe.get_doc(_agent_doc(self.model_doc.name)).insert()

	def tearDown(self):
		frappe.flags.flow_run = None
		frappe.flags.flow_unattended = None
		frappe.db.rollback()

	def _assert_flags_clear(self):
		self.assertIsNone(frappe.flags.get("flow_run"))
		self.assertFalse(frappe.flags.get("flow_unattended"))

	def test_after_an_unattended_run_returns(self):
		fake = FakeModel([_memory_call({"content": "Learned overnight.", "scope": "user"}), _final("ok")])

		with patch.object(Model, "chat", new=fake.chat):
			run = self.agent.run("nightly check", source="Trigger", auto_approve=True)

		self.assertEqual(run.status, "Completed")
		self._assert_flags_clear()

	def test_after_an_unattended_run_raises(self):
		"""The case a `finally` exists for. A model call that throws must not leave a trigger's
		"nobody is here" on this worker for whoever it serves next."""

		def boom(self, messages, tools=None, *, stream=False):
			raise RuntimeError("kaboom")

		with patch.object(Model, "chat", new=boom):
			with self.assertRaises(RuntimeError):
				self.agent.run("nightly check", source="Trigger", auto_approve=True)

		self._assert_flags_clear()

	def test_after_an_attended_run_returns(self):
		fake = FakeModel([_final("ok")])

		with patch.object(Model, "chat", new=fake.chat):
			run = self.agent.run("hello")

		self.assertEqual(run.status, "Completed")
		self._assert_flags_clear()

	def test_after_an_attended_run_raises(self):
		def boom(self, messages, tools=None, *, stream=False):
			raise RuntimeError("kaboom")

		with patch.object(Model, "chat", new=boom):
			with self.assertRaises(RuntimeError):
				self.agent.run("hello")

		self._assert_flags_clear()

	def test_a_paused_run_leaves_no_flags_either(self):
		"""A pause is a return, not an exception, and it is the most likely state to forget: the
		run is not over, but this request is."""
		fake = FakeModel([_memory_call({"content": "Remember this.", "scope": "user"})])

		with patch.object(Model, "chat", new=fake.chat):
			run = self.agent.run("remember this")

		self.assertEqual(run.status, "Paused")
		self._assert_flags_clear()
