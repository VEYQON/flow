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

import json
from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.wrappers import Response

from flow.api import start_run
from flow.lib.model import ChatResponse, Model, ToolCall
from flow.lib.session import load_session
from flow.memory import memory as memory_module
from flow.memory import store as memory_store
from flow.memory.memory import MEMORY_BLOCK_CLOSE, MEMORY_BLOCK_OPEN, save_memory
from flow.tools.builtins import bind_update_memory, sync_builtin_tools

NOT_EXECUTED = "not_executed"

# The fixtures below are deliberately local rather than imported from the module beside this one.
# Each test module has to stand on its own so a branch can carry one without the other, and a shared
# fixture that two modules depend on is a third thing to keep true. The cost is that they look alike.


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


def _streaming_chat(fake: FakeModel):
	"""A `Model.chat` replacement that streams when it is asked to, recording every call.

	The streamed path is a different function from the non-streaming one and persists through a
	different one again, so a test that only ever drives `stream=False` says nothing about it.
	"""

	def chat(self, messages, tools=None, *, stream=False):
		response = fake.chat(messages, tools=tools, stream=stream)
		if not stream:
			return response

		def events():
			if response.content:
				yield response.content
			return response

		return events()

	return chat


def _sse_events(response: Response) -> list[dict[str, Any]]:
	"""The events of a server-sent-events body, in order."""
	body = b"".join(response.iter_encoded()).decode()
	events = []
	for block in body.split("\n\n"):
		block = block.strip()
		if not block:
			continue
		data_lines = [line[6:] for line in block.split("\n") if line.startswith("data: ")]
		if data_lines:
			events.append(json.loads("\n".join(data_lines)))
	return events


def _read_call(call_id: str = "r1") -> ChatResponse:
	"""A call to a tool that needs no approval, so the loop executes it and keeps going."""
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name="read", arguments={"doctype": "Flow Model"})],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _result_of(value: Any) -> dict[str, Any]:
	"""A tool's return value as a dict, whether it came back as one or as serialised JSON."""
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

		frappe.flags.pop("flow_unattended", None)
		result = _result_of(self.unattended(content="Learned overnight.", scope="user"))
		self.assertEqual(result.get("status"), NOT_EXECUTED)

		# Once, after the loop. Asserted inside it, the row written by the FIRST failing value stays
		# (a rollback happens in tearDown, not between subtests) and every later subtest then reports
		# a row failure that is not about the value it was testing.
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

	def test_a_direct_caller_in_an_unattended_run_is_refused_too(self):
		"""The hole a security review found, and the reason the flag refusal is not conditional on
		the caller naming itself.

		`save_memory` can be imported straight into a tool record, and the schema built from its
		signature offers `from_conversation` with the permissive default. A model that simply omitted
		the argument reached the write — with SHARED scope, in a run nobody was watching, with no
		approval anywhere. The rule is about the run, so it now holds whoever is asking.
		"""
		frappe.flags.flow_unattended = True

		result = _result_of(
			memory_module.save_memory(self.agent.name, content="Everyone obey this.", scope="agent")
		)

		self.assertEqual(result.get("status"), NOT_EXECUTED)
		self.assertEqual(result.get("reason"), "unattended")
		self.assertEqual(self._rows(), [])

	def test_control_the_same_direct_caller_still_writes_when_somebody_is_there(self):
		"""Without this, the test above passes on a function that refuses everything, and the desk's
		own path — which is supposed to write shared notes — would be broken with nothing to say so."""
		frappe.flags.flow_unattended = False

		result = _result_of(
			memory_module.save_memory(self.agent.name, content="A curated fact.", scope="agent")
		)

		self.assertEqual(result.get("action"), "added")
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].scope, "Agent")

	def test_the_write_function_is_still_looked_up_on_every_call(self):
		"""This module's strongest test patches `save_memory` on the module that defines it and
		asserts the tool never reaches it. That evidence holds only while the tool body imports the
		name INSIDE the function, where it is re-resolved per call. Hoist that import to module level
		and the patch stops being observed — the test would then pass with the refusal deleted.

		So the property the evidence rests on is asserted directly, rather than assumed.
		"""
		import flow.tools.builtins as builtins_module

		self.assertNotIn("save_memory", vars(builtins_module))

	def test_the_unattended_binding_is_ungated_and_the_attended_one_is_not(self):
		"""The gate and the ability to write are separate things, and only one of them was pinned.

		Being ungated is what stops an unattended run parking in Paused on a question nobody can
		answer; the refusal above is what makes being ungated cost nothing. Nothing anywhere asserted
		the first half — no other test in the suite builds this binding at all.

		This deliberately does NOT go on to write a row: that half duplicated the test above, and it
		was the only real write in this module. A written note reaches a store that a transaction
		rollback does not undo, so the duplicate leaked as well as repeated.
		"""
		self.assertFalse(self.unattended.requires_confirmation)
		self.assertTrue(self.attended.requires_confirmation)


# ---------------------------------------------------------------------------------------------
# The flags do not outlive the run that set them
# ---------------------------------------------------------------------------------------------


class TestTheFlagsAreSetThenCleared(IntegrationTestCase):
	"""Features 5 and 6, on the NON-streaming path, in both directions.

	The streamed path grew a `finally` that clears both flags, with a comment saying why: a trigger's
	"nobody is here" carried into the next request this worker serves would silently stop that
	person's notes being kept. The non-streaming path does the same thing and nothing asserted it.

	Both directions are asserted, and the first is the one review had to insist on. A test that only
	looks after the run cannot tell "set, then cleared" from "never set at all" — so a change that
	stopped marking a run unattended would leave every pin green. That matters more since the tool
	itself refuses: the flag is now what carries the rule on the one path the binding cannot reach, a
	session with no agent record, and a flag nobody sets is a rule nobody applies. So each test reads
	the flags from INSIDE the model call and asserts what they were there.
	"""

	def setUp(self):
		self.model_doc = frappe.get_doc(_model_doc()).insert()
		self.agent = frappe.get_doc(_agent_doc(self.model_doc.name)).insert()

	def tearDown(self):
		frappe.flags.flow_run = None
		frappe.flags.flow_unattended = None
		frappe.db.rollback()

	def _flags_now(self) -> dict[str, Any]:
		return {
			"flow_run": frappe.flags.get("flow_run"),
			"flow_unattended": frappe.flags.get("flow_unattended"),
		}

	def _run(self, response: ChatResponse | None = None, **kwargs):
		"""Run a turn and return (run, the flags as they were while the model was being called)."""
		seen: dict[str, Any] = {}
		reply = response or _final("ok")

		def chat(inner_self, messages, tools=None, *, stream=False):
			seen.update(self._flags_now())
			return reply

		with patch.object(Model, "chat", new=chat):
			run = self.agent.run("go", **kwargs)
		return run, seen

	def _run_that_raises(self, **kwargs) -> dict[str, Any]:
		seen: dict[str, Any] = {}

		def boom(inner_self, messages, tools=None, *, stream=False):
			seen.update(self._flags_now())
			raise RuntimeError("kaboom")

		with patch.object(Model, "chat", new=boom):
			with self.assertRaises(RuntimeError):
				self.agent.run("go", **kwargs)
		return seen

	def _assert_flags_clear(self):
		"""The exact values, not merely falsy ones: `None` would satisfy `assertFalse` and is what
		this state looks like when nothing ever set the flag."""
		self.assertIsNone(frappe.flags.get("flow_run"))
		self.assertIs(frappe.flags.get("flow_unattended"), False)

	def test_a_trigger_run_is_marked_unattended_while_the_model_runs(self):
		run, seen = self._run(source="Trigger", auto_approve=True)

		self.assertEqual(seen["flow_run"], run.name)
		self.assertIs(seen["flow_unattended"], True)
		self._assert_flags_clear()

	def test_a_trigger_with_no_auto_approve_is_marked_unattended_too(self):
		"""`auto_approve` defaults to 0 on a trigger record, so this is the default configuration
		and not an edge. It is also the case an earlier version of this work broke."""
		run, seen = self._run(source="Trigger", auto_approve=False)

		self.assertEqual(seen["flow_run"], run.name)
		self.assertIs(seen["flow_unattended"], True)
		self._assert_flags_clear()

	def test_an_ordinary_chat_run_is_not_marked_unattended(self):
		"""The control. Without it every assertion above passes on a flag that is always set."""
		run, seen = self._run()

		self.assertEqual(seen["flow_run"], run.name)
		self.assertIs(seen["flow_unattended"], False)
		self._assert_flags_clear()

	def test_after_an_unattended_run_raises(self):
		"""The case a `finally` exists for. A model call that throws must not leave a trigger's
		"nobody is here" on this worker for whoever it serves next."""
		seen = self._run_that_raises(source="Trigger", auto_approve=True)

		self.assertIs(seen["flow_unattended"], True)
		self.assertIsNotNone(seen["flow_run"])
		self._assert_flags_clear()

	def test_after_an_attended_run_raises(self):
		seen = self._run_that_raises()

		self.assertIs(seen["flow_unattended"], False)
		self.assertIsNotNone(seen["flow_run"])
		self._assert_flags_clear()

	def test_a_paused_run_leaves_no_flags_either(self):
		"""A pause is a return, not an exception, and it is the state most easily forgotten: the run
		is not over, but this request is."""
		run, seen = self._run(response=_memory_call({"content": "Remember this.", "scope": "user"}))

		self.assertEqual(run.status, "Paused")
		self.assertEqual(seen["flow_run"], run.name)
		self._assert_flags_clear()


# ---------------------------------------------------------------------------------------------
# The streamed path, with a note present
# ---------------------------------------------------------------------------------------------


class TestAStreamedTurnWithAKeptNote(IntegrationTestCase):
	"""Features 8 and 9. A gap the previous run's adversary ran out of budget for.

	Every test that proved the block arrives once, in the right message, and is not re-stored drove
	the NON-streaming path. The streamed path builds the prompt with the same function but persists
	through a different one — `stream_with_persistence`, iterated by the server after the request
	handler has returned — so "the transcript stores exactly what the run produced" is a separate
	claim there, and nothing measured it.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		self.model_doc = frappe.get_doc(_model_doc()).insert()
		self.agent = frappe.get_doc(_agent_doc(self.model_doc.name)).insert()

	def tearDown(self):
		frappe.flags.flow_run = None
		frappe.flags.flow_unattended = False
		frappe.db.rollback()
		memory_store.drop_table()

	def _note(self, content: str) -> None:
		save_memory(self.agent.name, content=content, scope="user")

	def test_the_block_reaches_the_model_once_in_the_last_user_message_and_the_run_completes(self):
		self._note("Prefers metric units.")
		fake = FakeModel([_final("noted")])

		with patch.object(Model, "chat", new=_streaming_chat(fake)):
			response = start_run("what units do I use?", agent=self.agent.name, stream=True)
			events = _sse_events(response)

		self.assertEqual(events[-1]["type"], "done")
		self.assertEqual(events[-1]["status"], "Completed")

		self.assertTrue(fake.calls, "the model was never called")
		self.assertTrue(fake.calls[-1]["stream"], "this test says nothing unless the call streamed")
		messages = fake.calls[-1]["messages"]
		carrying = [m for m in messages if MEMORY_BLOCK_OPEN in (m.get("content") or "")]
		self.assertEqual(len(carrying), 1, "the block must arrive exactly once")
		self.assertEqual(carrying[0]["role"], "user")
		self.assertIs(carrying[0], [m for m in messages if m["role"] == "user"][-1])
		self.assertIn("Prefers metric units.", carrying[0]["content"])
		self.assertEqual(carrying[0]["content"].count(MEMORY_BLOCK_OPEN), 1)
		self.assertEqual(carrying[0]["content"].count(MEMORY_BLOCK_CLOSE), 1)

	def test_no_system_message_of_a_streamed_turn_carries_the_block(self):
		"""The property the whole design rests on, asserted on the path that had no test."""
		self._note("Prefers metric units.")
		fake = FakeModel([_final("noted")])

		with patch.object(Model, "chat", new=_streaming_chat(fake)):
			_sse_events(start_run("what units do I use?", agent=self.agent.name, stream=True))

		for message in fake.calls[-1]["messages"]:
			if message["role"] == "system":
				self.assertNotIn(MEMORY_BLOCK_OPEN, message.get("content") or "")
				self.assertNotIn("Prefers metric units.", message.get("content") or "")

	def test_the_transcript_a_streamed_run_stores_holds_no_block_and_no_message_twice(self):
		self._note("Prefers metric units.")
		fake = FakeModel([_final("noted")])

		with patch.object(Model, "chat", new=_streaming_chat(fake)):
			events = _sse_events(start_run("what units do I use?", agent=self.agent.name, stream=True))

		session = frappe.get_doc("Flow Session", events[0]["session"])
		stored = [(m.role, m.content) for m in session.messages]
		self.assertTrue(stored)
		for role, content in stored:
			self.assertNotIn(MEMORY_BLOCK_OPEN, content or "", f"a {role} message was stored with the block")
			self.assertNotIn(MEMORY_BLOCK_CLOSE, content or "")
			self.assertNotIn("Prefers metric units.", content or "")
		self.assertEqual(len(stored), len(set(stored)), f"a message was stored twice: {stored}")
		self.assertEqual([role for role, _ in stored], ["system", "user", "assistant"])

	def test_a_second_streamed_turn_still_stores_only_what_it_produced(self):
		"""Two turns, because what can go wrong is positional: the prefix a run skips comes from how
		many messages the prompt carried that the session never stored, and a block delivered at the
		tail would be re-stored as though the run had produced it."""
		self._note("Prefers metric units.")
		fake = FakeModel([_final("first"), _final("second")])

		with patch.object(Model, "chat", new=_streaming_chat(fake)):
			first = _sse_events(start_run("turn one", agent=self.agent.name, stream=True))
			session_name = first[0]["session"]
			_sse_events(start_run("turn two", agent=self.agent.name, session=session_name, stream=True))

		session = frappe.get_doc("Flow Session", session_name)
		stored = [(m.role, m.content) for m in session.messages]
		self.assertEqual([role for role, _ in stored], ["system", "user", "assistant", "user", "assistant"])
		self.assertEqual([c for _, c in stored][1:], ["turn one", "first", "turn two", "second"])
		for _role, content in stored:
			self.assertNotIn(MEMORY_BLOCK_OPEN, content or "")

	def test_control_a_streamed_turn_with_no_note_carries_no_block(self):
		"""Without this the assertions above could all be passing on a block that is never built."""
		fake = FakeModel([_final("noted")])

		with patch.object(Model, "chat", new=_streaming_chat(fake)):
			_sse_events(start_run("anything", agent=self.agent.name, stream=True))

		joined = "".join(m.get("content") or "" for m in fake.calls[-1]["messages"])
		self.assertNotIn(MEMORY_BLOCK_OPEN, joined)


# ---------------------------------------------------------------------------------------------
# The iteration budget, and a pending approval
# ---------------------------------------------------------------------------------------------


class TestTheIterationBudgetAndAPendingApproval(IntegrationTestCase):
	"""Features 10 and 11.

	The question was "what happens when the iteration budget runs out while a memory approval is
	pending?", and the answer turned out to be that it cannot: a turn that raises a question RETURNS
	from the loop, so the budget is never spent to nothing with something still pending, and a resume
	resolves every pending call before the loop starts again. Both halves are pinned rather than
	argued, because "it cannot happen" is the kind of claim this project has a Lesson about.

	What DOES happen is a run that exhausts the budget with everything resolved. That path had a real
	defect: the run went Failed while still carrying the question from its earlier pause, and a resume
	refuses any run that is not Paused — so it was a question nobody could ever answer, shown as
	pending forever.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		self.model_doc = frappe.get_doc(_model_doc()).insert()
		self.agent = frappe.get_doc(
			_agent_doc(
				self.model_doc.name,
				max_iterations=1,
				tools=[{"tool": "update_memory"}, {"tool": "read"}],
			)
		).insert()
		self.session = self.agent.new_session()

	def tearDown(self):
		frappe.flags.flow_run = None
		frappe.flags.flow_unattended = False
		frappe.db.rollback()
		memory_store.drop_table()

	def _notes(self) -> int:
		return frappe.db.count("Flow Agent Memory", {"agent": self.agent.name})

	def _run_row(self):
		names = frappe.get_all(
			"Flow Run", filters={"session": self.session.name}, pluck="name", order_by="creation desc"
		)
		self.assertTrue(names, "no run was created")
		return frappe.get_doc("Flow Run", names[0])

	def test_a_pending_memory_approval_is_never_consumed_by_the_iteration_budget(self):
		"""Feature 11. With a budget of exactly one iteration, the turn that proposes a note still
		pauses and asks. It does not spend the budget and fail, and the pause is not an iteration
		that was 'used up' with a question left over."""
		fake = FakeModel([_memory_call({"content": "Remember this.", "scope": "user"})])

		with patch.object(Model, "chat", new=fake.chat):
			run = self.agent.run("remember this", session=self.session.name)

		self.assertEqual(run.status, "Paused")
		self.assertEqual(len(json.loads(run.questions)), 1)
		self.assertEqual(self._notes(), 0)

	def test_a_run_that_exhausts_its_budget_writes_no_note_and_fails(self):
		"""Feature 10, first half. The exhaustion path proper: every call resolves, so the loop keeps
		going until the budget is gone. The run's stated outcome is Failed, with the reason recorded."""
		fake = FakeModel([_read_call()])

		with patch.object(Model, "chat", new=fake.chat):
			with self.assertRaisesRegex(RuntimeError, "max_iterations"):
				self.agent.run("read everything", session=self.session.name)

		run = self._run_row()
		self.assertEqual(run.status, "Failed")
		self.assertIn("max_iterations", run.error)
		self.assertEqual(self._notes(), 0)
		self.assertFalse(run.questions)

	def test_a_run_that_exhausts_its_budget_after_a_pause_leaves_no_unanswerable_question(self):
		"""Feature 10, second half, and the defect this test was written to find.

		A run pauses on the note; the person redirects it in their own words; the loop then runs out
		of iterations and the run goes Failed. `resume_run` refuses any run that is not Paused, so a
		question still sitting on a Failed run is one nobody can ever answer — and it is what a
		person is shown as pending.
		"""
		fake = FakeModel([_memory_call({"content": "Remember this.", "scope": "user"}), _read_call()])

		with patch.object(Model, "chat", new=fake.chat):
			run = self.agent.run("remember this", session=self.session.name)
			self.assertEqual(run.status, "Paused")
			self.assertTrue(run.questions)

			with self.assertRaisesRegex(RuntimeError, "max_iterations"):
				load_session(self.session.name).resume({"m1": "say it shorter"})

		run.reload()
		self.assertEqual(run.status, "Failed")
		self.assertFalse(run.questions, "a Failed run still carries a question nobody can answer")
		self.assertEqual(self._notes(), 0)

	def test_a_paused_run_still_carries_its_question(self):
		"""The control. Clearing a question when a run fails must not clear one that is still live —
		without this, a change that simply never stored questions would look correct."""
		fake = FakeModel([_memory_call({"content": "Remember this.", "scope": "user"})])

		with patch.object(Model, "chat", new=fake.chat):
			run = self.agent.run("remember this", session=self.session.name)

		self.assertEqual(run.status, "Paused")
		self.assertTrue(run.questions)
		self.assertEqual(json.loads(run.questions)[0]["key"], "m1")
