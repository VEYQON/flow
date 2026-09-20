# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""O3 — evidence for ADR-003: can the tool set change during a run?

ADR-003 proposes ONE agent that loads per-domain tool groups through a read-only
`load_tools(domain)` tool. This module establishes, with a fake model, what the engine does today
when a tool call adds tools to the running agent. It changes NO engine code: every test is
characterisation, and every assertion here was written as a prediction from reading `_loop`,
`_loop_stream`, `_invoke`, `_prepare_resume` and `load_session` BEFORE the module was first run.

`_load_group` below is the smallest possible stand-in for the proposed `load_tools(domain)`: it
appends to `agent.tools` and `agent._tools_by_name`, which is exactly what any implementation would
have to do. Nothing here proposes it as a design.

Findings are written up in brain/40-architecture/tool-groups-findings.md.
"""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from flow.lib.agent import Agent
from flow.lib.model import ChatResponse, Model, ToolCall
from flow.lib.tool import tool


class FakeModel:
	"""Scripted responses, and a record of the `tools` schema list sent on each call — which is
	the thing under test: what the model is TOLD it can call."""

	def __init__(self, responses: list[ChatResponse]):
		self._responses = list(responses)
		self.calls: list[dict[str, Any]] = []
		self.tools_seen: list[list[str]] = []

	def chat(self, messages, tools=None, *, stream=False):
		self.calls.append({"messages": list(messages), "tools": tools, "stream": stream})
		self.tools_seen.append(sorted(t["function"]["name"] for t in (tools or [])))
		if not self._responses:
			raise AssertionError("FakeModel ran out of scripted responses")
		return self._responses.pop(0)


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


class _Domain:
	"""A `load_tools(domain)` stand-in plus the gated tool it loads."""

	def __init__(self):
		self.ran: list[tuple[str, dict[str, Any]]] = []
		self.loaded: list[str] = []
		self.agent: Agent | None = None
		domain = self

		@tool(requires_confirmation=True)
		def post_payment(amount: float) -> str:
			"""Post a payment."""
			domain.ran.append(("post_payment", {"amount": amount}))
			return f"posted {amount}"

		@tool
		def load_tools(group: str) -> str:
			"""Load the tools for a domain."""
			domain.loaded.append(group)
			agent = domain.agent
			if post_payment.name not in agent._tools_by_name:
				agent.tools.append(post_payment)
				agent._tools_by_name[post_payment.name] = post_payment
			return f"loaded {group}"

		self.post_payment = post_payment
		self.load_tools = load_tools

	def agent_with(self, model) -> Agent:
		self.agent = Agent(model=model, tools=[self.load_tools])
		return self.agent


class TestALoadedToolWithinOneRun(UnitTestCase):
	"""Q1: can a tool call add tools that the SAME run can call on its next iteration?"""

	def test_the_tool_schemas_are_computed_once_so_a_loaded_tool_is_never_advertised(self):
		"""`_loop` builds `tool_schemas` ONCE, above the iteration loop (agent.py:261). The
		second model call is handed the SAME list object the first was."""
		domain = _Domain()
		model = FakeModel([_calls(("load_tools", {"group": "finance"}, "t1")), _final("ok")])
		agent = domain.agent_with(model)

		agent.run("pay someone")

		self.assertEqual(domain.loaded, ["finance"])
		self.assertIn("post_payment", agent._tools_by_name)  # it really was loaded
		self.assertEqual(model.tools_seen[0], ["load_tools"])
		self.assertEqual(model.tools_seen[1], ["load_tools"])  # still not advertised
		self.assertIs(model.calls[0]["tools"], model.calls[1]["tools"])

	def test_the_loaded_tool_is_callable_anyway_if_the_model_names_it(self):
		"""`_invoke` looks the name up in `self._tools_by_name` on every call (agent.py:404), so
		the registry IS live within the run — only the advertisement is stale."""
		domain = _Domain()
		model = FakeModel(
			[
				_calls(("load_tools", {"group": "finance"}, "t1")),
				_calls(("post_payment", {"amount": 10.0}, "t2")),
				_final("done"),
			]
		)
		agent = domain.agent_with(model)

		result = agent.run("pay someone")

		self.assertTrue(result.paused)  # gated, so it paused rather than ran
		self.assertEqual(result.questions[0].key, "t2")
		self.assertEqual(domain.ran, [])

	def test_an_unloaded_name_is_an_error_not_a_pause(self):
		"""Control for the test above: without the load, the same call is unknown. If this
		failed, `test_the_loaded_tool_is_callable_anyway` would be proving nothing."""
		domain = _Domain()
		model = FakeModel([_calls(("post_payment", {"amount": 10.0}, "t2")), _final("done")])
		agent = domain.agent_with(model)

		result = agent.run("pay someone")

		self.assertFalse(result.paused)
		tool_message = next(m for m in result.messages if m["role"] == "tool")
		self.assertIn("Unknown tool", tool_message["content"])

	def test_the_streaming_loop_computes_its_schemas_once_too(self):
		domain = _Domain()
		model = FakeModel([_calls(("load_tools", {"group": "finance"}, "t1")), _final("ok")])
		agent = domain.agent_with(model)

		def streamed(messages, tools=None, *, stream=False):
			response = model.chat(messages, tools=tools, stream=stream)

			def gen():
				if response.content:
					yield response.content
				return response

			return gen() if stream else response

		agent.model = type("M", (), {"chat": staticmethod(streamed)})()
		list(agent.run("pay someone", stream=True))

		self.assertEqual(model.tools_seen[0], ["load_tools"])
		self.assertEqual(model.tools_seen[1], ["load_tools"])


class TestALoadedToolKeepsItsApprovalGate(UnitTestCase):
	"""Q2: does a tool added this way keep requires_confirmation and the exact-Approve path?"""

	def _paused_on_a_loaded_tool(self, extra=None):
		domain = _Domain()
		model = FakeModel(
			[
				_calls(("load_tools", {"group": "finance"}, "t1")),
				_calls(("post_payment", {"amount": 10.0}, "t2")),
				*(extra or []),
			]
		)
		agent = domain.agent_with(model)
		return domain, agent, model, agent.run("pay someone")

	def test_a_loaded_gated_tool_pauses_the_run_and_does_not_execute(self):
		domain, _agent, _model, paused = self._paused_on_a_loaded_tool()

		self.assertTrue(paused.paused)
		self.assertEqual(paused.questions[0].options, ["Approve", "Deny"])
		self.assertTrue(paused.questions[0].allow_other)
		self.assertEqual(domain.ran, [])

	def test_the_exact_approve_executes_a_loaded_tool(self):
		domain, agent, _model, paused = self._paused_on_a_loaded_tool(extra=[_final("done")])

		resumed = agent.resume(paused.messages, {"t2": "Approve"})

		self.assertEqual(domain.ran, [("post_payment", {"amount": 10.0})])
		self.assertEqual(resumed.output, "done")

	def test_deny_does_not_execute_a_loaded_tool(self):
		domain, agent, model, paused = self._paused_on_a_loaded_tool()

		resumed = agent.resume(paused.messages, {"t2": "Deny"})

		self.assertEqual(domain.ran, [])
		self.assertIsNone(resumed.output)
		self.assertEqual(len(model.calls), 2)  # no further model call

	def test_a_resume_does_advertise_the_loaded_tool_because_it_reenters_the_loop(self):
		"""`resume` calls `_loop`, which recomputes `tool_schemas` from `self.tools` at that
		moment — so within ONE Agent object the loaded set survives the pause."""
		domain, agent, model, paused = self._paused_on_a_loaded_tool(extra=[_final("done")])

		agent.resume(paused.messages, {"t2": "Approve"})

		self.assertEqual(model.tools_seen[-1], ["load_tools", "post_payment"])
		self.assertEqual(domain.loaded, ["finance"])


class TestWhatSurvivesAcrossTurnsAndRequests(IntegrationTestCase):
	"""Q3 and Q4: do loaded tools persist into the next turn, and what does a resume that
	rebuilds from the record see?"""

	def setUp(self):
		self.model_doc = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "Tool Groups Spike Model",
				"model_id": "openai/gpt-4o-mini",
				"enabled": 1,
			}
		).insert()
		self.agent_doc = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "Tool Groups Spike Agent",
				"model": self.model_doc.name,
				"instructions": "be terse",
				"enabled": 1,
			}
		).insert()

	def tearDown(self):
		frappe.db.rollback()

	def test_a_record_backed_session_rebuilds_its_tools_every_turn(self):
		"""`load_session` → `_resolve_existing_agent` → `agent_doc.assemble()` builds a NEW
		Agent from the record's tool rows each time (session.py:78-80), so anything a previous
		turn loaded at runtime is gone."""
		from flow.lib.session import load_session, new_session

		session = new_session(self.agent_doc.name)
		first = session._runtime

		reloaded = load_session(session.name)

		self.assertIsNot(reloaded._runtime, first)
		self.assertEqual(
			sorted(reloaded._runtime._tools_by_name),
			sorted(first._tools_by_name),
		)

	def test_a_code_agent_session_keeps_a_loaded_tool_only_because_the_caller_holds_it(self):
		"""The other half of the answer: a code-agent session is continued by passing the SAME
		Agent object back in, so its runtime additions survive — through the caller, not through
		anything stored."""
		from flow.lib.session import load_session

		domain = _Domain()
		agent = domain.agent_with(FakeModel([]))
		agent.model = Model(self.model_doc.name)
		session = agent.new_session()
		agent.tools.append(domain.post_payment)
		agent._tools_by_name[domain.post_payment.name] = domain.post_payment

		reloaded = load_session(session.name, agent=agent)

		self.assertIs(reloaded._runtime, agent)
		self.assertIn("post_payment", reloaded._runtime._tools_by_name)

	def test_resuming_through_the_record_loses_the_loaded_tool_and_swallows_the_approval(self):
		"""THE FINDING. `resume_run` (api.py:61) calls `load_session(run.session)` with no
		agent, so a record-backed session rebuilds the ORIGINAL tool set. The paused call then
		names a tool the rebuilt runtime does not have, and `_prepare_resume`'s else-branch
		(agent.py:236) records the raw answer instead of resolving a confirmation: the gated
		call is closed out with the literal string "Approve", nothing executes, and NO denial is
		recorded. It fails silently, not closed."""
		from flow.lib.session import load_session

		executed: list[float] = []

		@tool(requires_confirmation=True)
		def post_payment(amount: float) -> str:
			"""Post a payment."""
			executed.append(amount)
			return "posted"

		runtime = self.agent_doc.assemble()
		runtime.tools.append(post_payment)
		runtime._tools_by_name[post_payment.name] = post_payment

		def pause(messages, tools=None, **_):
			return _calls(("post_payment", {"amount": 10.0}, "c1"))

		session = load_session(
			frappe.get_doc({"doctype": "Flow Session", "agent": self.agent_doc.name})
			.insert(ignore_permissions=True)
			.name
		)
		session._runtime = runtime
		with patch.object(Model, "chat", side_effect=pause):
			run = session.chat("pay someone")
		self.assertEqual(run.status, "Paused")
		self.assertEqual(executed, [])

		# The client comes back through the public path, which rebuilds from the record.
		rebuilt = load_session(session.name)
		self.assertNotIn("post_payment", rebuilt._runtime._tools_by_name)

		with patch.object(Model, "chat", side_effect=lambda *a, **k: _final("all done")):
			resumed = rebuilt.resume({"c1": "Approve"})

		self.assertEqual(executed, [])  # nothing ran
		self.assertEqual(resumed.status, "Completed")  # ...and nothing was denied either
		rows = frappe.get_doc("Flow Session", session.name).messages
		tool_rows = [r for r in rows if r.role == "tool"]
		self.assertEqual(tool_rows[-1].content, "Approve")  # the person's word, stored as the result
