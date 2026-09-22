# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""S19 Part A — a batch is one decision.

The model can ask for several tools in one reply. Before this change the engine ran them one by
one and only decided to pause after the last of them, so every ungated call in a pausing turn had
already executed by the time the person saw the question. They were asked about the write having
already had the read done in their name — and a Deny undid nothing and said nothing.

Now nothing in a turn runs if anything in that turn will ask a person: the calls nobody was asked
about are held back and execute on resume, in their original order, after the answers are in.

Every tool here records its own calls, so "nothing ran" is asserted against a list rather than
inferred from the shape of a result.
"""

import json
from typing import Any

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from flow.lib.agent import CONFIRM_ANSWER_OPTIONS, Agent, Done, Question, ToolEnded, ToolStarted
from flow.lib.model import ChatResponse, ToolCall
from flow.lib.tool import tool


class FakeModel:
	"""Scripted responses, and a record of every call, so a test can assert the model was not
	consulted again."""

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
	return ChatResponse(content=text, finish_reason="stop", usage={})


def _calls(*specs: tuple[str, dict[str, Any], str]) -> ChatResponse:
	"""ONE assistant turn asking for several tools at once: (name, arguments, call_id).

	Deliberately not two scripted single-call responses — those are two turns, and two turns is
	the shape this defect does not live in.
	"""
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=args) for name, args, call_id in specs],
		finish_reason="tool_calls",
		usage={},
	)


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, str]:
	return {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}


class _Books:
	"""An ungated read and a gated write, each recording what actually ran."""

	def __init__(self):
		self.read: list[dict[str, Any]] = []
		self.wrote: list[dict[str, Any]] = []
		books = self

		@tool
		def read_balance(account: str) -> str:
			"""Read an account balance."""
			books.read.append({"account": account})
			return "120"

		@tool(requires_confirmation=True)
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			books.wrote.append({"to": to, "amount": amount})
			return f"sent {amount} to {to}"

		self.read_balance = read_balance
		self.send_money = send_money
		self.tools = [read_balance, send_money]


def _pause_on_a_mixed_batch(books: _Books, extra: list[ChatResponse] | None = None, *, read_first=True):
	"""Run one batch holding an ungated read (r1) and a gated write (c2), to the pause."""
	specs = [
		("read_balance", {"account": "alice"}, "r1"),
		("send_money", {"to": "alice", "amount": 500}, "c2"),
	]
	if not read_first:
		specs.reverse()
	model = FakeModel([_calls(*specs), *(extra or [])])
	agent = Agent(model=model, tools=books.tools)
	return agent, model, agent.run("read alice's balance and pay her")


class TestABatchIsOneDecision(UnitTestCase):
	def test_an_ungated_call_beside_a_gated_one_does_not_run(self):
		"""AT1. The defect itself: the read used to have run by the time the question was shown."""
		books = _Books()
		_agent, _model, paused = _pause_on_a_mixed_batch(books)

		self.assertTrue(paused.paused)
		self.assertEqual(len(paused.questions), 1)
		self.assertEqual(paused.questions[0].key, "c2")
		self.assertEqual(paused.tool_calls, [])
		self.assertEqual(books.read, [], "the read ran before anyone was asked about the write")
		self.assertEqual(books.wrote, [])
		self.assertEqual(_tool_results(paused.messages), {})

	def test_the_gated_call_first_makes_no_difference(self):
		"""AT2. The whole batch is classified before any of it runs, so the order the model
		happened to choose decides nothing."""
		books = _Books()
		_agent, _model, paused = _pause_on_a_mixed_batch(books, read_first=False)

		self.assertTrue(paused.paused)
		self.assertEqual(len(paused.questions), 1)
		self.assertEqual(paused.tool_calls, [])
		self.assertEqual(books.read, [])
		self.assertEqual(books.wrote, [])

	def test_the_deferred_call_runs_on_resume_before_the_approved_one(self):
		"""AT3. Held back, not dropped — and in the order the model asked for them."""
		books = _Books()
		agent, _model, paused = _pause_on_a_mixed_batch(books, extra=[_final("done")])
		# Held back, not merely late: without this the test passes against the engine that ran the
		# read before anyone was asked, because by resume time the recorder looks the same.
		self.assertEqual(books.read, [], "the read ran in the pausing turn")

		# `asked` is what the production resume always passes (flow_session.py:384,393). It is
		# what tells a call HELD BACK from a call whose own question went unanswered — see AT7d —
		# so a test about a held-back call has to resume the way the engine's own caller does.
		resumed = agent.resume(paused.messages, {"c2": "Approve"}, asked=paused.questions)

		self.assertEqual(books.read, [{"account": "alice"}])
		self.assertEqual(books.wrote, [{"to": "alice", "amount": 500}])
		self.assertEqual([c.name for c in resumed.tool_calls], ["read_balance", "send_money"])
		self.assertEqual(_tool_results(resumed.messages)["r1"], "120")
		self.assertEqual(resumed.output, "done")

	def test_a_denial_holds_back_the_deferred_call_too(self):
		"""AT4. Refusing one action shown in a group refuses the group, and a deferred read is
		part of that group precisely because it was held back for the group's sake."""
		books = _Books()
		agent, model, paused = _pause_on_a_mixed_batch(books)

		resumed = agent.resume(paused.messages, {"c2": "Deny"}, asked=paused.questions)

		self.assertEqual(books.read, [])
		self.assertEqual(books.wrote, [])
		self.assertIsNone(resumed.output)
		self.assertFalse(resumed.paused)
		deferred = json.loads(_tool_results(resumed.messages)["r1"])
		self.assertEqual(deferred["status"], "not_executed")
		self.assertEqual(len(model.calls), 1, "a denied turn must not consult the model again")

	def test_a_tool_that_asks_its_own_question_still_lets_the_others_run(self):
		"""AT5. The fork's own copy of upstream's `test_other_tools_run_while_a_question_pauses`.

		A question a TOOL returns is not an approval: the tool body has already run by the time
		anyone sees it, so holding its neighbours back would protect nothing and would change a
		behaviour upstream pins. The classification pass never sees it.
		"""
		ran: list[str] = []

		@tool
		def safe() -> str:
			"""Safe."""
			ran.append("safe")
			return "safe-result"

		@tool
		def ask_user(prompt: str) -> Question:
			"""Ask the user."""
			return Question(prompt=prompt, options=["A", "B"])

		agent = Agent(
			model=FakeModel([_calls(("safe", {}, "c1"), ("ask_user", {"prompt": "ok?"}, "c2"))]),
			tools=[safe, ask_user],
		)

		result = agent.run("do both")

		self.assertTrue(result.paused)
		self.assertEqual(ran, ["safe"])
		self.assertEqual([c.name for c in result.tool_calls], ["safe"])
		self.assertEqual([q.key for q in result.questions], ["c2"])

	def test_a_turn_with_no_approval_is_unchanged(self):
		"""AT6. The control. Two ungated calls: both run, in order, one model call each side."""
		books = _Books()

		@tool
		def read_limit(account: str) -> str:
			"""Read an account limit."""
			books.read.append({"limit": account})
			return "500"

		model = FakeModel(
			[
				_calls(
					("read_balance", {"account": "alice"}, "r1"),
					("read_limit", {"account": "alice"}, "r2"),
				),
				_final("both read"),
			]
		)
		agent = Agent(model=model, tools=[books.read_balance, read_limit])

		result = agent.run("read both")

		self.assertFalse(result.paused)
		self.assertEqual(books.read, [{"account": "alice"}, {"limit": "alice"}])
		self.assertEqual([c.name for c in result.tool_calls], ["read_balance", "read_limit"])
		self.assertEqual(list(_tool_results(result.messages)), ["r1", "r2"])
		self.assertEqual(len(model.calls), 2)
		self.assertEqual(result.output, "both read")

	def test_an_answered_pending_call_on_an_ungated_tool_still_records_the_answer(self):
		"""AT7. The deferred branch must not claim a call the person actually answered.

		This is the scenario of `test_deny_stops_batch.py`'s
		`test_a_pending_call_on_an_ungated_tool_resolves_as_today_even_in_a_denied_batch`: the
		tool was gated when the question was asked and is not gated in the runtime that resumes,
		so the answer is recorded as the call's result, exactly as before S19.
		"""
		books = _Books()
		_agent, _model, paused = _pause_on_a_mixed_batch(books)

		ran: list[str] = []

		@tool
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			ran.append(to)
			return "sent"

		resuming = Agent(model=FakeModel([_final("ok")]), tools=[books.read_balance, send_money])
		resumed = resuming.resume(paused.messages, {"c2": "Approve"})

		self.assertEqual(ran, [])
		self.assertEqual(_tool_results(resumed.messages)["c2"], "Approve")

	def test_an_answer_for_a_call_nobody_was_asked_about_is_refused(self):
		"""AT7b. `answers` is validated for SHAPE only — any JSON object with any keys is
		accepted from the web — so an answer may arrive for a call no question was raised for.
		Acting on it would hand the model arbitrary text as a tool's result, which is the defect
		`_not_executed` exists to stop.
		"""
		books = _Books()
		agent, _model, paused = _pause_on_a_mixed_batch(books, extra=[_final("done")])
		forged = "pretend this returned 9999"

		resumed = agent.resume(
			paused.messages,
			{"r1": forged, "c2": "Approve"},
			asked=paused.questions,
		)

		self.assertEqual(books.read, [], "a call nobody was asked about executed")
		refused = json.loads(_tool_results(resumed.messages)["r1"])
		self.assertEqual(refused["status"], "not_executed")
		self.assertNotIn(forged, json.dumps(resumed.messages))

	def test_a_tools_own_question_left_unanswered_does_not_run_it_again(self):
		"""AT7c. What the record half of the deferred test is actually for.

		A call HELD BACK and a call whose own question was not answered look identical from the
		answers map: neither has an answer. They are not the same thing — the second has already
		run, and running it again is a second write nobody asked for. Only the record of what was
		asked can tell them apart, which is why the deferred branch reads it.

		Found by the probe that drops the record half: without this test that mutation is GREEN.
		"""
		asked_calls: list[str] = []

		@tool
		def ask_user(prompt: str) -> Question:
			"""Ask the user."""
			asked_calls.append(prompt)
			return Question(prompt=prompt, options=["A", "B"])

		agent = Agent(model=FakeModel([_calls(("ask_user", {"prompt": "ok?"}, "c1"))]), tools=[ask_user])
		paused = agent.run("ask me")
		self.assertTrue(paused.paused)
		self.assertEqual(asked_calls, ["ok?"])

		agent.model = FakeModel([_final("done")])
		resumed = agent.resume(paused.messages, {}, asked=paused.questions)

		self.assertEqual(asked_calls, ["ok?"], "the tool ran a second time, unasked")
		self.assertEqual(_tool_results(resumed.messages)["c1"], "")

	def test_with_no_record_a_tools_own_question_still_does_not_run_it_again(self):
		"""AT7d. The same pause as AT7c, resumed with NO record of what was asked.

		`asked=None` is a supported, documented call (`Agent.resume`), it is what
		`_asked_questions` returns for a run whose stored questions are missing or unreadable,
		and it is what `evals/run.py` passes. The docstring of `_prepare_resume` promises that
		with no record behaviour is exactly what it was before any of this was recorded —
		nothing runs on the strength of an answer nobody gave. The deferred branch broke that
		promise: with no record its guard collapsed to "true for every unanswered call", so the
		tool's body ran a SECOND time.
		"""
		asked_calls: list[str] = []

		@tool
		def ask_user(prompt: str) -> Question:
			"""Ask the user."""
			asked_calls.append(prompt)
			return Question(prompt=prompt, options=["A", "B"])

		agent = Agent(model=FakeModel([_calls(("ask_user", {"prompt": "ok?"}, "c1"))]), tools=[ask_user])
		paused = agent.run("ask me")
		self.assertTrue(paused.paused)
		self.assertEqual(asked_calls, ["ok?"])

		agent.model = FakeModel([_final("done")])
		resumed = agent.resume(paused.messages, {}, asked=None)

		self.assertEqual(asked_calls, ["ok?"], "the tool ran a second time, unasked")
		self.assertEqual(_tool_results(resumed.messages)["c1"], "")

	def test_with_no_record_an_unanswered_call_does_not_run(self):
		"""AT7e. The other half of AT7d: a call nobody answered, in a pause with no record.

		Without a record there is nothing that can say whether this call was held back for the
		group or was a question in its own right, so it must not execute on the strength of an
		answer that does not exist. Before the fix it did — and for a call that was gated when
		the run paused and has since been un-gated, it executed with no answer of any kind.
		"""
		books = _Books()
		agent, _model, paused = _pause_on_a_mixed_batch(books, extra=[_final("done")])
		self.assertTrue(paused.paused)
		self.assertEqual(books.read, [])

		resumed = agent.resume(paused.messages, {}, asked=None)

		self.assertEqual(books.read, [], "a held-back call ran with no answer and no record")
		self.assertEqual(books.wrote, [])
		self.assertEqual(_tool_results(resumed.messages)["r1"], "")

	def test_the_streamed_loop_defers_the_same_calls(self):
		"""AT8. The streaming loop is a second copy of the same decision, and a UI card left
		spinning on a call that never ran is its own kind of lie."""
		books = _Books()
		model = FakeModel(
			[
				_calls(
					("read_balance", {"account": "alice"}, "r1"),
					("send_money", {"to": "alice", "amount": 500}, "c2"),
				)
			]
		)
		agent = Agent(model=model, tools=books.tools)

		events = list(agent.run("read and pay", stream=True))

		self.assertEqual(books.read, [])
		self.assertEqual(books.wrote, [])
		started = [e.id for e in events if isinstance(e, ToolStarted)]
		self.assertNotIn("r1", started, "a card was opened for a call that never ran")
		ended = [(e.id, e.result) for e in events if isinstance(e, ToolEnded)]
		self.assertIn(("r1", ""), ended, "the deferred call's card was left spinning")
		done = next(e for e in events if isinstance(e, Done))
		self.assertTrue(done.result.paused)
		self.assertEqual(done.result.tool_calls, [])

	def test_the_pause_and_answer_shapes_are_unchanged(self):
		"""AT18. The web apps read these three things and nothing else about a pause. How much
		has already run when the pause arrives is the only thing S19 changes.
		"""
		books = _Books()
		agent, _model, paused = _pause_on_a_mixed_batch(books, extra=[_final("done")])

		for question in paused.questions:
			self.assertEqual(question.options, list(CONFIRM_ANSWER_OPTIONS))
			self.assertTrue(question.allow_other)
		self.assertEqual([q.key for q in paused.questions], ["c2"])

		resumed = agent.resume(paused.messages, {"c2": "Approve"})

		self.assertEqual(books.wrote, [{"to": "alice", "amount": 500}])
		self.assertEqual(resumed.output, "done")


class _Unattended:
	"""A gated write and an ungated read, each recording what actually ran."""

	def __init__(self):
		self.wrote: list[dict[str, Any]] = []
		self.read: list[str] = []
		bag = self

		@tool(requires_confirmation=True)
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			bag.wrote.append({"to": to, "amount": amount})
			return f"sent {amount} to {to}"

		@tool
		def read_balance(account: str) -> str:
			"""Read an account balance."""
			bag.read.append(account)
			return "120"

		self.send_money = send_money
		self.read_balance = read_balance


class TestAnUnattendedRunRefusesEveryGate(UnitTestCase):
	"""S19 Part B. A run with nobody in it cannot be asked anything, so a tool that requires
	asking is refused: it does not run, and it does not park the run waiting for an answer that
	can never come.

	`auto_approve` answered the wrong question. It asked *may questions be skipped in this run*,
	when the only answerable one is *may this tool run unasked in this run* — and the answer to
	that is no.
	"""

	def test_an_unattended_run_refuses_a_gated_tool(self):
		"""AT9."""
		bag = _Unattended()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1")), _final("done")])
		agent = Agent(model=model, tools=[bag.send_money], unattended=True)

		result = agent.run("pay alice")

		self.assertEqual(bag.wrote, [], "a gated write ran in a run with nobody in it")
		self.assertFalse(result.paused)
		self.assertEqual(result.tool_calls, [], "a refused call was reported as one that ran")
		refused = json.loads(_tool_results(result.messages)["c1"])
		self.assertEqual(refused["status"], "not_executed")
		self.assertEqual(refused["reason"], "unattended")
		self.assertEqual(result.output, "done")

	def test_auto_approve_alone_still_counts_as_unattended(self):
		"""AT9b. The backward-compatible path: every caller that passes only `auto_approve` — a
		trigger with the flag on, and upstream's own test — means the same thing by it."""
		bag = _Unattended()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1")), _final("done")])
		agent = Agent(model=model, tools=[bag.send_money], auto_approve=True)

		result = agent.run("pay alice")

		self.assertEqual(bag.wrote, [])
		self.assertFalse(result.paused)
		refused = json.loads(_tool_results(result.messages)["c1"])
		self.assertEqual(refused["reason"], "unattended")

	def test_an_unattended_run_still_executes_an_ungated_tool(self):
		"""AT12. The control. Without it, AT9 is satisfied by an engine that refuses everything —
		which is an outage, not a gate."""
		bag = _Unattended()
		model = FakeModel([_calls(("read_balance", {"account": "alice"}, "r1")), _final("120")])
		agent = Agent(model=model, tools=[bag.read_balance], unattended=True)

		result = agent.run("read alice's balance")

		self.assertEqual(bag.read, ["alice"])
		self.assertEqual([c.name for c in result.tool_calls], ["read_balance"])
		self.assertEqual(result.output, "120")

	def test_an_attended_run_is_unaffected(self):
		"""AT13. Refuse is checked before ask, and must never win when there IS someone to ask."""
		bag = _Unattended()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1"))])
		agent = Agent(model=model, tools=[bag.send_money])

		result = agent.run("pay alice")

		self.assertTrue(result.paused)
		self.assertEqual(len(result.questions), 1)
		self.assertEqual(result.questions[0].options, list(CONFIRM_ANSWER_OPTIONS))
		self.assertEqual(bag.wrote, [])

	def test_a_refused_call_beside_an_ungated_one_stops_neither_the_read_nor_the_run(self):
		"""A refusal is not a pause: the rest of the reply runs and the turn continues, which is
		what keeps an unattended run from stalling on a tool it cannot be asked about."""
		bag = _Unattended()
		model = FakeModel(
			[
				_calls(
					("read_balance", {"account": "alice"}, "r1"),
					("send_money", {"to": "alice", "amount": 500}, "c1"),
				),
				_final("read it, did not pay"),
			]
		)
		agent = Agent(model=model, tools=[bag.read_balance, bag.send_money], unattended=True)

		result = agent.run("read and pay")

		self.assertEqual(bag.read, ["alice"])
		self.assertEqual(bag.wrote, [])
		self.assertFalse(result.paused)
		self.assertEqual([c.name for c in result.tool_calls], ["read_balance"])

	def test_the_refusal_names_no_platform_or_vendor(self):
		"""AT14. CLAUDE.md rule 3, over the literal the model reads."""
		from flow.lib.agent import NOT_EXECUTED_MESSAGES

		self._assert_clean(NOT_EXECUTED_MESSAGES["unattended"])

	def test_control_the_vendor_check_can_go_red(self):
		with self.assertRaises(AssertionError):
			self._assert_clean("this sentence mentions Frappe by name")

	def _assert_clean(self, text: str) -> None:
		for word in ("frappe", "flow", "erpnext", "mariadb", "openai", "anthropic", "gpt", "claude"):
			self.assertNotIn(word, text.lower(), f"{word!r} appears in text the model reads")


class TestOnlyTheInvokeDigestMoved(UnitTestCase):
	"""AT15. CLAUDE.md rule 4: four functions decide what executes and on which answer. S19
	rewrites exactly one of them, `_invoke`, under an exception the owner gave for this spec.
	The other three are quoted here as literals, so this test fails if one of them moves even
	though the pin in `test_deny_stops_batch.py` was re-baselined in the same commit.
	"""

	UNCHANGED = {
		"_resolve_confirmation": "adbb8b26e0b0fb166a5a9fff1c658c531d081b3bd2f2967e55b8e838b5ab8f47",
		"_confirmation_question": "32916612298d4ebdb423d9904e268992a2e638b56a67f2ca14fa67deba9cf34c",
		"_has_denial": "80f799b6827afea159589dcee7889282ad8c376aacc15be424c273cd0e55b209",
	}

	def test_the_three_that_did_not_move_are_byte_identical(self):
		from flow.tests.test_deny_stops_batch import TestTheLoadBearingFunctionsAreUntouched as Pin

		pin = Pin("test_the_four_functions_are_byte_identical_to_their_reviewed_form")
		digests = pin._digests(pin._engine_source())

		for name, digest in self.UNCHANGED.items():
			self.assertEqual(digests[name], digest, f"{name} changed; rule 4 requires a spec naming it")

	def test_the_re_baselined_pin_and_the_engine_agree(self):
		"""The control: the pin next door must be describing the engine this test just hashed,
		or a re-baseline could quietly have been taken from something else."""
		from flow.tests.test_deny_stops_batch import TestTheLoadBearingFunctionsAreUntouched as Pin

		pin = Pin("test_the_four_functions_are_byte_identical_to_their_reviewed_form")
		digests = pin._digests(pin._engine_source())

		self.assertEqual(digests["_invoke"], Pin.BASELINE_DIGESTS["_invoke"])
		for name, digest in self.UNCHANGED.items():
			self.assertEqual(Pin.BASELINE_DIGESTS[name], digest)


class TestARealTriggerRunRefusesInsteadOfParking(IntegrationTestCase):
	"""AT16. Part B pinned to a real run rather than to a code `Agent`.

	`FlowSession.chat` is the only place that knows a run has nobody in it — it computes
	`unattended` for the memory tool already — and until S19 it never told the runtime. A trigger
	with `auto_approve` OFF therefore raised a question nobody could answer and left the run in
	`Paused`, holding its session. Delete the one line that tells the runtime and this test is
	what goes red.

	It runs as the trigger's own `run_as` user, a named non-Administrator, because that is what
	`flow/triggers/triggers.py:75` sets and what any permission this path relies on would be
	measured against.
	"""

	S19_TESTER = "s19-trigger@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from flow.tools.builtins import sync_builtin_tools

		sync_builtin_tools()
		cls.enterClassContext(cls.enable_safe_exec())

	def setUp(self):
		if not frappe.db.exists("User", self.S19_TESTER):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": self.S19_TESTER,
					"first_name": "S19",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
			user.add_roles("System Manager")
		model = frappe.get_doc(
			{"doctype": "Flow Model", "title": "S19 Model", "model_id": "openai/gpt-4o-mini", "enabled": 1}
		).insert(ignore_permissions=True)
		agent = frappe.get_doc(
			{
				"doctype": "Flow Agent",
				"title": "S19 Agent",
				"model": model.name,
				"instructions": "Be terse.",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
		self.trigger = frappe.get_doc(
			{
				"doctype": "Flow Trigger",
				"title": "S19 Trigger",
				"agent": agent.name,
				"enabled": 1,
				"event": "DocType Event",
				"target_doctype": "ToDo",
				"doc_event": "after_insert",
				"prompt_template": "New {{ doc.doctype }}",
				"auto_approve": 0,
				"run_as": self.S19_TESTER,
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_a_trigger_with_auto_approve_off_refuses_instead_of_pausing(self):
		from unittest.mock import patch

		from flow.lib.model import Model
		from flow.triggers import fire

		seen_users: list[str] = []

		def _script(*args, **kwargs):
			seen_users.append(frappe.session.user)
			if len(seen_users) == 1:
				return ChatResponse(
					content=None,
					tool_calls=[ToolCall(id="c1", name="execute", arguments={"code": "result = 1"})],
					finish_reason="tool_calls",
					usage={},
				)
			return _final("done")

		todo = frappe.get_doc({"doctype": "ToDo", "description": "s19"}).insert(ignore_permissions=True)
		with patch.object(Model, "chat", side_effect=_script):
			run_name = fire(self.trigger.name, target_doctype="ToDo", target_name=todo.name)

		run = frappe.get_doc("Flow Run", run_name)
		self.assertEqual(run.status, "Completed", "the run parked waiting for an answer nobody can give")
		self.assertEqual(json.loads(run.tool_calls or "[]"), [], "the gated tool ran unattended")
		self.assertEqual(seen_users[0], self.S19_TESTER, "the run did not run as the trigger's user")
		results = [
			m.content
			for m in frappe.get_all(
				"Flow Session Message",
				filters={"parent": run.session, "role": "tool"},
				fields=["content"],
				order_by="idx",
			)
		]
		self.assertTrue(results, "the run stored no tool result at all")
		refused = json.loads(results[-1])
		self.assertEqual(refused["status"], "not_executed")
		self.assertEqual(refused["reason"], "unattended")
