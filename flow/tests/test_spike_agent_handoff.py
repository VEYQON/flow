# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt
"""SPIKE — can one agent hand work to another?

These are CHARACTERISATION tests. Each one records what the engine does TODAY, so that the
findings note can cite a test name rather than an opinion, and so the file becomes a tripwire if
an answer ever changes. Nothing here is a feature and nothing here is a fix.

Where a test pins behaviour the spike considers WRONG, its docstring says so explicitly. Read
brain/40-architecture/agent-handoff-findings.md alongside this file.

No real model is called: agent A runs on the suite's scripted fake, and a doctype agent's model
is stubbed at the boundary.
"""

from typing import Any

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.agent import Agent
from flow.lib.model import ChatResponse, Model, ToolCall
from flow.lib.tool import tool

# ---------------------------------------------------------------------------------------------
# Scripted model plumbing (mirrors flow/tests/test_ai_agent.py's FakeModel, which is module-local
# there; duplicated rather than imported so this spike file stands alone and can be deleted whole)
# ---------------------------------------------------------------------------------------------


class ScriptedModel:
	# A session snapshots its agent's config on every run, and reads `model_id` off the model to
	# do it, so a stand-in must carry one even though it never reaches a provider.
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


def _calls(name: str, arguments: dict[str, Any], call_id: str = "c1") -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


def _make_model_record() -> str:
	return (
		frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": f"Spike Model {frappe.generate_hash(length=6)}",
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _make_agent_record(instructions: str = "You are the specialist.", tools: list[str] | None = None) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Flow Agent",
			"title": f"Spike Specialist {frappe.generate_hash(length=6)}",
			"model": _make_model_record(),
			"instructions": instructions,
			"enabled": 1,
		}
	)
	for slug in tools or []:
		doc.append("tools", {"tool": slug})
	return doc.insert(ignore_permissions=True).name


class TestHandoffToACodeAgent(IntegrationTestCase):
	"""(a) Can a tool inside agent A's run start a run on agent B and return B's answer to A?"""

	def tearDown(self):
		frappe.db.rollback()

	def test_a_tool_can_run_a_code_agent_and_return_its_answer(self):
		seen: dict[str, Any] = {}

		specialist = Agent(
			model=ScriptedModel([_final("The invoice total is 4,200.")]),
			name="specialist",
			instructions="You are the specialist.",
		)

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist and return its answer."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			run = convo.chat(question)
			seen["inner_session"] = convo.name
			seen["inner_run"] = run.name
			seen["inner_status"] = run.status
			return run.output or ""

		generalist = Agent(
			model=ScriptedModel(
				[
					_calls("ask_the_specialist", {"question": "what is the invoice total?"}),
					_final("It is 4,200."),
				]
			),
			name="generalist",
			tools=[ask_the_specialist],
		)
		outer = generalist.new_session()
		outer_run = outer.chat("what is the invoice total?")

		# VERIFIED: the nested run completed and its answer came back through the tool.
		self.assertEqual(seen["inner_status"], "Completed")
		self.assertEqual(outer_run.status, "Completed")
		self.assertEqual(outer_run.output, "It is 4,200.")
		tool_messages = [m for m in frappe.get_doc("Flow Session", outer.name).messages if m.role == "tool"]
		self.assertEqual(len(tool_messages), 1)
		self.assertEqual(tool_messages[0].content, "The invoice total is 4,200.")

		# VERIFIED: B's run is recorded, on its own session, separate from A's.
		self.assertNotEqual(seen["inner_session"], outer.name)
		self.assertEqual(frappe.db.get_value("Flow Run", seen["inner_run"], "session"), seen["inner_session"])

	def test_neither_run_records_the_other(self):
		"""(d) Is A's run linked to B's run anywhere in the record?"""
		seen: dict[str, Any] = {}
		specialist = Agent(model=ScriptedModel([_final("done")]), name="specialist", instructions="s")

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			seen["inner_run"] = convo.chat(question).name
			return "done"

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("ok")]),
			name="generalist",
			tools=[ask_the_specialist],
		)
		outer = generalist.new_session()
		outer_run = outer.chat("q")

		inner = frappe.get_doc("Flow Run", seen["inner_run"])
		outer_doc = frappe.get_doc("Flow Run", outer_run.name)
		# VERIFIED: no field on either row points at the other. The only pointers a run has are
		# session, trigger and reference_doctype/reference_name, and none is set to the other run.
		for row, other in ((inner, outer_doc), (outer_doc, inner)):
			self.assertIsNone(row.trigger)
			self.assertNotEqual(row.session, other.session)
			self.assertNotEqual((row.reference_doctype, row.reference_name), ("Flow Run", other.name))
		self.assertIsNone(inner.reference_doctype)
		self.assertIsNone(outer_doc.reference_doctype)


class TestHandoffToARecordDefinedAgent(IntegrationTestCase):
	"""(a, second half) The same, when the specialist is defined as a record rather than in code."""

	def tearDown(self):
		frappe.db.rollback()

	def test_a_tool_can_run_a_record_defined_agent_by_name(self):
		specialist = _make_agent_record(instructions="You are the specialist.")
		seen: dict[str, Any] = {}

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist and return its answer."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			run = convo.chat(question)
			seen["inner_session"] = convo.name
			seen["inner_run"] = run.name
			seen["inner_agent_link"] = convo.agent
			return run.output or ""

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("relayed")]),
			name="generalist",
			tools=[ask_the_specialist],
		)
		outer = generalist.new_session()

		# The record-defined specialist builds a real Model; stub it at the boundary so no
		# provider is contacted. Only the specialist's model is a real Model instance — the
		# generalist runs on the scripted stand-in — so this patch reaches B's call only.
		def specialist_reply(messages, tools=None, **_):
			seen["specialist_saw"] = [dict(m) for m in messages]
			return _final("The specialist says 4,200.")

		with self.patch_model(specialist_reply):
			outer_run = outer.chat("q")

		# VERIFIED: the record-defined agent ran and its answer reached the generalist.
		self.assertEqual(outer_run.output, "relayed")
		self.assertEqual(frappe.db.get_value("Flow Run", seen["inner_run"], "status"), "Completed")
		tool_messages = [m for m in frappe.get_doc("Flow Session", outer.name).messages if m.role == "tool"]
		self.assertEqual(tool_messages[0].content, "The specialist says 4,200.")
		# VERIFIED: a record-defined agent's session carries the agent link; a code agent's does not.
		self.assertEqual(seen["inner_agent_link"], specialist)
		# VERIFIED: B was sent ITS OWN instructions, not A's.
		self.assertEqual(seen["specialist_saw"][0]["role"], "system")
		self.assertIn("You are the specialist.", seen["specialist_saw"][0]["content"])

	def patch_model(self, side_effect):
		from unittest.mock import patch

		return patch.object(Model, "chat", side_effect=side_effect)


class TestWhatHappensWhenTheSpecialistNeedsApproval(IntegrationTestCase):
	"""(b) THE question this spike exists for.

	The specialist needs a write approved. Nobody is watching the specialist's conversation — the
	person is talking to the generalist. What reaches them?

	Every assertion below records CURRENT behaviour. Several of them record behaviour the spike
	considers wrong; those say so. Nothing here is fixed.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def _handoff(self):
		"""Build a generalist whose tool runs a specialist that immediately needs approval."""
		executed: list[dict[str, Any]] = []
		seen: dict[str, Any] = {}

		@tool(requires_confirmation=True)
		def post_the_invoice(amount: float) -> str:
			"""Post an invoice. Needs approval."""
			executed.append({"amount": amount})
			return f"posted {amount}"

		specialist = Agent(
			model=ScriptedModel([_calls("post_the_invoice", {"amount": 4200.0}, call_id="inner_1")]),
			name="specialist",
			instructions="You are the specialist.",
			tools=[post_the_invoice],
		)

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist and return its answer."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			run = convo.chat(question)
			seen["inner_session"] = convo.name
			seen["inner_run"] = run.name
			seen["inner_status"] = run.status
			seen["inner_questions"] = run.questions
			# What the tool has to hand back. `output` is None on a paused run.
			return run.output or ""

		generalist = Agent(
			model=ScriptedModel(
				[_calls("ask_the_specialist", {"question": "post it"}), _final("I have asked.")]
			),
			name="generalist",
			tools=[ask_the_specialist],
		)
		return generalist, seen, executed

	def test_the_specialists_pause_is_invisible_to_the_caller(self):
		generalist, seen, executed = self._handoff()
		outer = generalist.new_session()
		outer_run = outer.chat("post it")

		# VERIFIED: the specialist DID pause, and its question was recorded on its own run.
		self.assertEqual(seen["inner_status"], "Paused")
		self.assertIsNotNone(seen["inner_questions"])
		self.assertIn("post_the_invoice", seen["inner_questions"])
		self.assertEqual(executed, [])  # the write did not happen — the approval gate held

		# VERIFIED, AND THIS IS THE PROBLEM: the tool had nothing but an empty string to return,
		# because a paused run's `output` is None. The generalist was told nothing about the
		# pause, finished normally, and answered the user as though the work were under way.
		tool_messages = [m for m in frappe.get_doc("Flow Session", outer.name).messages if m.role == "tool"]
		self.assertEqual(tool_messages[0].content, "")
		self.assertEqual(outer_run.status, "Completed")
		self.assertEqual(outer_run.output, "I have asked.")

		# VERIFIED: the outer run carries no question, so no approval is ever put to the user.
		self.assertIsNone(frappe.db.get_value("Flow Run", outer_run.name, "questions"))

	def test_the_specialists_paused_run_is_left_parked_and_reachable_only_by_id(self):
		generalist, seen, _ = self._handoff()
		outer = generalist.new_session()
		outer.chat("post it")

		inner_run = seen["inner_run"]
		# VERIFIED: the paused run is a real, owned, resumable row — it is simply orphaned. The
		# only thing that knows its id is the tool that made it, and the tool has already returned.
		self.assertEqual(frappe.db.get_value("Flow Run", inner_run, "status"), "Paused")
		self.assertEqual(frappe.db.get_value("Flow Run", inner_run, "owner"), frappe.session.user)

		# VERIFIED: the invoking user COULD resume it, if anything told them it existed. Ownership
		# is the same user, so the documented resume path's owner check passes.
		from flow.lib.session import assert_run_owner

		assert_run_owner(frappe.get_doc("Flow Run", inner_run))

		# VERIFIED: nothing links it to the conversation the person is actually having. Listing
		# paused runs for the outer session finds none.
		paused_on_outer = frappe.get_all(
			"Flow Run", filters={"session": outer.name, "status": "Paused"}, pluck="name"
		)
		self.assertEqual(paused_on_outer, [])


class TestWhoseIdentityTheSpecialistRunsAs(IntegrationTestCase):
	"""(c) Whose permissions do the specialist's tools execute with?"""

	def setUp(self):
		self.user = (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"spike-caller-{frappe.generate_hash(length=8)}@example.com",
					"first_name": "Caller",
					"send_welcome_email": 0,
					"roles": [{"role": "System Manager"}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def tearDown(self):
		frappe.db.rollback()

	def test_the_specialists_tools_run_as_the_user_who_invoked_the_generalist(self):
		seen: dict[str, Any] = {}

		@tool
		def who_am_i() -> str:
			"""Report the acting identity."""
			seen["inside_specialist_tool"] = frappe.session.user
			return frappe.session.user

		specialist = Agent(
			model=ScriptedModel([_calls("who_am_i", {}, call_id="i1"), _final("reported")]),
			name="specialist",
			instructions="s",
			tools=[who_am_i],
		)

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist."""
			from flow.lib.session import new_session

			seen["inside_generalist_tool"] = frappe.session.user
			return new_session(specialist).chat(question).output or ""

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("ok")]),
			name="generalist",
			tools=[ask_the_specialist],
		)

		with self.set_user(self.user):
			outer = generalist.new_session()
			outer.chat("q")
			seen["outer"] = frappe.session.user

		# VERIFIED: there is no identity switch anywhere in a handoff. The specialist's tools run
		# as the same user who invoked the generalist — no elevation, and no drop either.
		self.assertEqual(seen["inside_generalist_tool"], self.user)
		self.assertEqual(seen["inside_specialist_tool"], self.user)
		self.assertEqual(seen["outer"], self.user)

	def test_both_sessions_and_both_runs_are_owned_by_that_same_user(self):
		seen: dict[str, Any] = {}
		specialist = Agent(model=ScriptedModel([_final("done")]), name="specialist", instructions="s")

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			run = convo.chat(question)
			seen["inner_session"], seen["inner_run"] = convo.name, run.name
			return run.output or ""

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("ok")]),
			name="generalist",
			tools=[ask_the_specialist],
		)
		with self.set_user(self.user):
			outer = generalist.new_session()
			outer_run = outer.chat("q")

		# VERIFIED: ownership, which is what the resume and session guards key on, is the invoking
		# user throughout. So a design that surfaces B's question through A does not have to solve
		# a permissions problem — only a routing one.
		for doctype, name in (
			("Flow Session", outer.name),
			("Flow Session", seen["inner_session"]),
			("Flow Run", outer_run.name),
			("Flow Run", seen["inner_run"]),
		):
			self.assertEqual(frappe.db.get_value(doctype, name, "owner"), self.user)


class TestTheNestedRunClobbersTheOuterRunsMemoryStamp(IntegrationTestCase):
	"""A side effect of nesting that is NOT about approvals, found while writing this spike.

	The flag that tells the memory tool which run is writing is process-global, set on entry to a
	turn and cleared to None in a finally. A nested turn therefore clears it while the OUTER turn
	is still running, so anything the outer run does after its tool returns is no longer stamped.
	"""

	def tearDown(self):
		frappe.db.rollback()

	def test_the_inner_turn_clears_the_flag_the_outer_turn_was_relying_on(self):
		seen: dict[str, Any] = {}
		specialist = Agent(model=ScriptedModel([_final("done")]), name="specialist", instructions="s")

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist."""
			from flow.lib.session import new_session

			seen["before_inner"] = frappe.flags.flow_run
			new_session(specialist).chat(question)
			seen["after_inner"] = frappe.flags.flow_run
			return "done"

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("ok")]),
			name="generalist",
			tools=[ask_the_specialist],
		)
		outer = generalist.new_session()
		outer_run = outer.chat("q")

		# VERIFIED: entering the tool, the flag names the OUTER run.
		self.assertEqual(seen["before_inner"], outer_run.name)
		# VERIFIED, AND THIS IS A DEFECT: the nested turn cleared it on the way out. Any memory the
		# outer run writes after this point records no source run.
		self.assertIsNone(seen["after_inner"])


class TestTheParkedApprovalCanStillBeAnswered(IntegrationTestCase):
	"""(b, final part) If the person were somehow handed the specialist's run id, would approving
	it work? This matters: it decides whether a fix has to change the engine's approval machinery
	or only has to route a question to the right conversation."""

	def tearDown(self):
		frappe.db.rollback()

	def test_approving_the_orphaned_run_by_id_executes_the_write(self):
		executed: list[dict[str, Any]] = []
		seen: dict[str, Any] = {}

		@tool(requires_confirmation=True)
		def post_the_invoice(amount: float) -> str:
			"""Post an invoice. Needs approval."""
			executed.append({"amount": amount})
			return f"posted {amount}"

		specialist = Agent(
			model=ScriptedModel(
				[
					_calls("post_the_invoice", {"amount": 4200.0}, call_id="inner_1"),
					_final("Posted it."),
				]
			),
			name="specialist",
			instructions="You are the specialist.",
			tools=[post_the_invoice],
		)

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			run = convo.chat(question)
			seen["inner_session"], seen["inner_run"] = convo.name, run.name
			return run.output or ""

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("asked")]),
			name="generalist",
			tools=[ask_the_specialist],
		)
		generalist.new_session().chat("q")
		self.assertEqual(executed, [])  # still not executed

		# Resume the orphaned run the way the documented path does: load its session, pass the
		# answer keyed by the pending call. The code agent has to be handed back in, because a
		# code-agent session cannot rebuild its own runtime.
		from flow.lib.session import load_session

		resumed = load_session(seen["inner_session"], agent=specialist).resume({"inner_1": "Approve"})

		# VERIFIED: it works. The approval machinery is intact across the handoff — what is
		# missing is only that nothing ever puts the question in front of the person.
		self.assertEqual(executed, [{"amount": 4200.0}])
		self.assertEqual(resumed.status, "Completed")
		self.assertEqual(resumed.output, "Posted it.")

	def test_denying_the_orphaned_run_stops_it_without_executing(self):
		executed: list[dict[str, Any]] = []
		seen: dict[str, Any] = {}

		@tool(requires_confirmation=True)
		def post_the_invoice(amount: float) -> str:
			"""Post an invoice. Needs approval."""
			executed.append({"amount": amount})
			return f"posted {amount}"

		specialist = Agent(
			model=ScriptedModel([_calls("post_the_invoice", {"amount": 1.0}, call_id="inner_1")]),
			name="specialist",
			instructions="s",
			tools=[post_the_invoice],
		)

		@tool
		def ask_the_specialist(question: str) -> str:
			"""Hand a question to the specialist."""
			from flow.lib.session import new_session

			convo = new_session(specialist)
			run = convo.chat(question)
			seen["inner_session"] = convo.name
			return run.output or ""

		generalist = Agent(
			model=ScriptedModel([_calls("ask_the_specialist", {"question": "q"}), _final("asked")]),
			name="generalist",
			tools=[ask_the_specialist],
		)
		generalist.new_session().chat("q")

		from flow.lib.session import load_session

		load_session(seen["inner_session"], agent=specialist).resume({"inner_1": "Deny"})

		# VERIFIED: the gate behaves identically inside a handoff — only "Approve" executes.
		self.assertEqual(executed, [])
