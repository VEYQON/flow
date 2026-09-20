# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""ADR-003 prototype — the tool group for a turn is chosen BEFORE the run starts.

EVIDENCE ONLY. This is not a feature and is not for merge. ADR-003 stays `proposed`.

Run 4 established that loading tools DURING a run is not viable: the tool list sent to the model is
built once above the iteration loop, nothing a tool adds survives a request, and a resume rebuilds
the runtime from the record — where a mid-run-loaded tool has never been — so a person's approval
of it is dropped without a denial.

The alternative tested here does the choosing at the other end. The caller names the turn's group
before the run starts; it is recorded on the run; and every later reader rebuilds from that record
rather than from whatever happens to be registered when they look. Each T below is one of the five
claims the run's task set, and each is asserted against what actually executed or what was actually
sent to the model, never against what a result says about itself.
"""

import json
from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.model import ChatResponse, Model, ToolCall
from flow.lib.tool import tool

FINANCE = ["send_money", "read_balance"]
RECORDS = ["delete_records"]


def _final(text: str) -> ChatResponse:
	return ChatResponse(content=text, finish_reason="stop", usage={})


def _call(name: str, arguments: dict[str, Any], call_id: str) -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
		finish_reason="tool_calls",
		usage={},
	)


class _Tools:
	"""Three tools across two groups, each recording its own calls."""

	def __init__(self):
		self.ran: list[tuple[str, dict[str, Any]]] = []
		me = self

		@tool(requires_confirmation=True)
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			me.ran.append(("send_money", {"to": to, "amount": amount}))
			return f"sent {amount} to {to}"

		@tool
		def read_balance(account: str) -> str:
			"""Read an account balance."""
			me.ran.append(("read_balance", {"account": account}))
			return "120"

		@tool(requires_confirmation=True)
		def delete_records(folder: str) -> str:
			"""Delete a folder of records."""
			me.ran.append(("delete_records", {"folder": folder}))
			return f"deleted {folder}"

		self.all = [send_money, read_balance, delete_records]


def _agent_and_session(title: str):
	"""A record-backed session, so every turn rebuilds its runtime from the record — which is
	the whole point: a prototype that kept one Agent object alive would prove nothing."""
	model_doc = frappe.get_doc(
		{"doctype": "Flow Model", "title": f"{title} Model", "model_id": "openai/gpt-4o-mini", "enabled": 1}
	).insert()
	agent_doc = frappe.get_doc(
		{
			"doctype": "Flow Agent",
			"title": f"{title} Agent",
			"model": model_doc.name,
			"instructions": "be terse",
			"enabled": 1,
		}
	).insert()
	return agent_doc


class TestTheTurnsToolGroupIsFixedBeforeTheRun(IntegrationTestCase):
	"""T1-T5. Every assertion is against what the model was actually offered, or against what
	actually executed — never against what a result says about itself."""

	def setUp(self):
		self.agent_doc = _agent_and_session("Turn Groups")
		self.tools = _Tools()
		self.advertised: list[list[str]] = []

	def tearDown(self):
		frappe.db.rollback()

	# -- helpers ------------------------------------------------------------------------------

	def _session(self):
		from flow.lib.session import load_session

		session = load_session(
			frappe.get_doc({"doctype": "Flow Session", "agent": self.agent_doc.name})
			.insert(ignore_permissions=True)
			.name
		)
		return self._with_all_tools(session)

	def _with_all_tools(self, session):
		"""A record-backed agent resolves its tools from Flow Tool rows; these are code tools,
		so ALL THREE are put back by hand every time a runtime is rebuilt. That is deliberate:
		the narrowing must come from the record, not from what happens to be registered."""
		for t in self.tools.all:
			if t.name not in session._runtime._tools_by_name:
				session._runtime.tools.append(t)
				session._runtime._tools_by_name[t.name] = t
		return session

	def _chat(self, session, message, script, group):
		"""Run one turn, recording the tool names offered to the model on each call."""
		responses = list(script)

		def chat(messages, tools=None, **_):
			self.advertised.append(sorted(t["function"]["name"] for t in (tools or [])))
			return responses.pop(0)

		with patch.object(Model, "chat", side_effect=chat):
			return session.chat(message, tool_group=group)

	# -- T1 -----------------------------------------------------------------------------------

	def test_t1_the_group_is_chosen_before_the_run_and_recorded_on_the_run(self):
		session = self._session()

		run = self._chat(session, "what is the balance?", [_final("120")], FINANCE)

		# What the model was actually offered, on the only call it got.
		self.assertEqual(self.advertised, [sorted(FINANCE)])
		snapshot = json.loads(run.config_snapshot)
		self.assertEqual(sorted(snapshot["tools"]), sorted(FINANCE))
		self.assertEqual(snapshot["tool_group"], FINANCE)

	def test_t1_the_group_is_recorded_even_though_the_agent_has_more_tools(self):
		"""The control. Without a group the SAME session offers strictly more — all three of
		these tools and the agent's own builtins besides — so the assertion above is measuring a
		narrowing and not an empty tool set.

		The builtins are the reason this is written as a superset rather than an equality: a
		record-backed agent brings its own tools, which is exactly the situation a group has to
		narrow in production."""
		session = self._session()

		run = self._chat(session, "what is the balance?", [_final("120")], None)

		offered = set(self.advertised[0])
		self.assertTrue(set(FINANCE + RECORDS) <= offered, f"expected all three in {sorted(offered)}")
		self.assertGreater(len(offered), len(FINANCE + RECORDS), "the agent brings builtins too")
		self.assertIn("delete_records", offered, "which the FINANCE turn above did NOT offer")
		self.assertIsNone(json.loads(run.config_snapshot).get("tool_group"))

	# -- T3 -----------------------------------------------------------------------------------

	def test_t3_a_tool_outside_the_group_is_refused_and_nothing_executes(self):
		session = self._session()

		run = self._chat(
			session,
			"clear the invoices",
			[_call("delete_records", {"folder": "invoices"}, "x1"), _final("I cannot do that.")],
			FINANCE,
		)

		self.assertEqual(self.tools.ran, [])
		self.assertEqual(run.status, "Completed")
		rows = frappe.get_doc("Flow Session", run.session).messages
		result = next(r.content for r in rows if r.role == "tool" and r.tool_call_id == "x1")
		self.assertIn("Unknown tool", result)

	# -- T4 -----------------------------------------------------------------------------------

	def test_t4_a_gated_tool_inside_the_group_still_asks_and_still_needs_the_exact_approve(self):
		from flow.api.api import resume_run
		from flow.lib.session import load_session

		session = self._session()
		run = self._chat(
			session, "pay alice", [_call("send_money", {"to": "alice", "amount": 500}, "c1")], FINANCE
		)

		self.assertEqual(run.status, "Paused")
		self.assertEqual(json.loads(run.questions)[0]["options"], ["Approve", "Deny"])
		self.assertEqual(self.tools.ran, [])

		with (
			patch(
				"flow.lib.session.load_session",
				side_effect=lambda n, **k: self._with_all_tools(load_session(n, **k)),
			),
			patch.object(Model, "chat", side_effect=lambda *a, **k: _final("paid")),
		):
			resume_run(run.name, {"c1": "Approve"})

		self.assertEqual(self.tools.ran, [("send_money", {"to": "alice", "amount": 500})])

	def test_t4_a_denial_inside_a_group_still_executes_nothing(self):
		from flow.api.api import resume_run
		from flow.lib.session import load_session

		session = self._session()
		run = self._chat(
			session, "pay alice", [_call("send_money", {"to": "alice", "amount": 500}, "c1")], FINANCE
		)

		with patch(
			"flow.lib.session.load_session",
			side_effect=lambda n, **k: self._with_all_tools(load_session(n, **k)),
		):
			resume_run(run.name, {"c1": "Deny"})

		self.assertEqual(self.tools.ran, [])

	# -- T2 -----------------------------------------------------------------------------------

	def test_t2_a_resume_in_that_turn_sees_exactly_the_same_tool_set(self):
		"""The runtime that resumes is rebuilt from the record, and ALL THREE tools are put back
		on it by hand before the narrowing runs. If the group were taken from what is registered
		rather than from the run's own record, the model would be offered three tools here."""
		from flow.api.api import resume_run
		from flow.lib.session import load_session

		session = self._session()
		run = self._chat(
			session, "pay alice", [_call("send_money", {"to": "alice", "amount": 500}, "c1")], FINANCE
		)
		self.advertised.clear()

		def chat(messages, tools=None, **_):
			self.advertised.append(sorted(t["function"]["name"] for t in (tools or [])))
			return _final("paid")

		with (
			patch(
				"flow.lib.session.load_session",
				side_effect=lambda n, **k: self._with_all_tools(load_session(n, **k)),
			),
			patch.object(Model, "chat", side_effect=chat),
		):
			resume_run(run.name, {"c1": "Approve"})

		self.assertEqual(self.advertised, [sorted(FINANCE)])

	# -- T5 -----------------------------------------------------------------------------------

	def test_t5_the_next_turn_may_use_a_different_group(self):
		from flow.api.api import resume_run
		from flow.lib.session import load_session

		session = self._session()
		run = self._chat(
			session, "pay alice", [_call("send_money", {"to": "alice", "amount": 500}, "c1")], FINANCE
		)
		with (
			patch(
				"flow.lib.session.load_session",
				side_effect=lambda n, **k: self._with_all_tools(load_session(n, **k)),
			),
			patch.object(Model, "chat", side_effect=lambda *a, **k: _final("paid")),
		):
			resume_run(run.name, {"c1": "Approve"})

		self.advertised.clear()
		second = self._chat(self._session(), "clear the invoices", [_final("done")], RECORDS)

		self.assertEqual(self.advertised, [sorted(RECORDS)])
		self.assertEqual(json.loads(second.config_snapshot)["tool_group"], RECORDS)

	def test_t5_a_paused_call_is_resolved_against_its_own_turns_group(self):
		"""The claim this prototype exists to make. The session's idea of the current group is
		irrelevant to a run that is already paused: the answer is resolved against the tools the
		question was asked about, rebuilt from that run's own record."""
		from flow.api.api import resume_run
		from flow.lib.session import load_session

		session = self._session()
		run = self._chat(
			session, "pay alice", [_call("send_money", {"to": "alice", "amount": 500}, "c1")], FINANCE
		)
		self.advertised.clear()

		def rebuilt(name, **kw):
			"""A resume that narrows to the SESSION's current group instead of the run's would
			offer RECORDS here and lose send_money entirely."""
			return self._with_all_tools(load_session(name, **kw))

		def chat(messages, tools=None, **_):
			self.advertised.append(sorted(t["function"]["name"] for t in (tools or [])))
			return _final("paid")

		with (
			patch("flow.lib.session.load_session", side_effect=rebuilt),
			patch.object(Model, "chat", side_effect=chat),
		):
			resume_run(run.name, {"c1": "Approve"})

		self.assertEqual(self.tools.ran, [("send_money", {"to": "alice", "amount": 500})])
		self.assertEqual(self.advertised, [sorted(FINANCE)])
		self.assertEqual(json.loads(run.config_snapshot)["tool_group"], FINANCE)
