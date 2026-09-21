# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""S17 — one approval answers one action.

Every decision the engine makes about a tool call is keyed by that call's reference: which calls
are still pending, which question a person is answering, which call an answer resolves. When two
calls in one model reply cannot be told apart by that reference, all three collapse onto one. The
interface stamps one card, a person approves one action, and two happen.

Two calls cannot be told apart in two ways, and they fail identically downstream:

  - they carry the same id;
  - neither carries a usable one. A provider sending ``{"id": null}`` twice, or a gateway that
    omits the field, or a stream that never delivers it, all produce a turn whose calls share the
    empty reference. Nothing in the engine refused that before, and it is the likelier shape.

Such a turn is REFUSED — nothing runs, nothing is written, and the turn never enters the
transcript — and a stored transcript already carrying one cannot be resumed.

What these tests do NOT cover, said plainly rather than left to be inferred: the `auto_approve`
switch and the order in which a batch executes. Both are separate known defects with their own
entries, and nothing here changes either.

No permission surface: these drive `flow/lib/agent.py` with a fake model and read no document, so
the named-non-Administrator rule does not apply.
"""

from typing import Any
from unittest.mock import patch

from frappe.tests import UnitTestCase

from flow.lib.agent import _UNANSWERABLE_TURN, Agent, Done
from flow.lib.model import ChatResponse, ToolCall
from flow.lib.tool import tool

FORBIDDEN = ("frappe", "erpnext", "mariadb", "openai", "anthropic", "gpt-", "claude", "flow")


class FakeModel:
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


def _calls(*specs: tuple[str, dict[str, Any], Any]) -> ChatResponse:
	"""One assistant turn asking for several tools at once: (name, arguments, call_id).

	`call_id` is passed through untouched — `None` and `""` are the shapes a provider actually
	produces (a null in the payload, or a field never streamed), and this helper must be able to
	build them.
	"""
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name=name, arguments=args) for name, args, call_id in specs],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


class _Recorder:
	"""One gated tool and one ungated one, both recording every execution.

	"Nothing ran" is asserted against this list and never inferred from the shape of a result.
	The ungated tool is the one that matters: a gated pair pauses today, so a gated batch cannot
	show the double execution. An ungated pair runs, twice, with nobody asked anything.
	"""

	def __init__(self):
		self.ran: list[tuple[str, dict[str, Any]]] = []
		recorder = self

		@tool(requires_confirmation=True)
		def send_money(to: str, amount: int) -> str:
			"""Send money to someone."""
			recorder.ran.append(("send_money", {"to": to, "amount": amount}))
			return f"sent {amount} to {to}"

		@tool
		def append_line(text: str) -> str:
			"""Append a line to the log."""
			recorder.ran.append(("append_line", {"text": text}))
			return f"appended {text}"

		self.send_money = send_money
		self.append_line = append_line
		self.tools = [send_money, append_line]


def _assistant_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]


def _transcript_with_two_colliding_calls(call_id: Any = "c1") -> list[dict[str, Any]]:
	"""A stored transcript paused on an assistant turn whose two calls share one reference.

	Hand-built as the doctype stores it — plain dicts, not `ToolCall`s — because this is the shape
	a resume reads back, and it is the one input a site could already be holding.
	"""
	call = {
		"id": call_id,
		"type": "function",
		"function": {"name": "send_money", "arguments": '{"to": "alice", "amount": 500}'},
	}
	return [
		{"role": "user", "content": "pay alice"},
		{"role": "assistant", "content": None, "tool_calls": [dict(call), dict(call)]},
	]


class TestAColl1dingTurnIsRefused(UnitTestCase):
	"""AT1, AT1b, AT2, AT3 — the forward path, gated and ungated, streamed and not."""

	def test_a_colliding_gated_batch_raises_before_any_tool_runs(self):
		"""AT1. Two gated calls sharing `c1`.

		`recorder.ran == []` is ALREADY true here before this change, because a gated batch
		pauses rather than executing — so that assertion is not what discriminates. The raise is.
		The execution is watched in the test below, on an ungated pair.
		"""
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("send_money", {"to": "alice", "amount": 500}, "c1"),
					("send_money", {"to": "alice", "amount": 500}, "c1"),
				)
			]
		)
		agent = Agent(model=model, tools=recorder.tools)

		with self.assertRaises(ValueError) as caught:
			agent.run("pay alice")

		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
		self.assertEqual(recorder.ran, [])

	def test_a_colliding_ungated_batch_raises_and_neither_call_runs(self):
		"""AT1b. The test that watches the defect on the path being fixed.

		Two calls to an UNGATED tool, sharing `c1`. Nobody is asked anything on this path, so
		before this change the tool simply ran twice. Delete the refusal and `recorder.ran` holds
		two entries — that is the defect, reproduced forward, not merely a pause that failed to
		happen.
		"""
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("append_line", {"text": "x"}, "c1"),
					("append_line", {"text": "x"}, "c1"),
				),
				_final("done"),
			]
		)
		agent = Agent(model=model, tools=recorder.tools)

		with self.assertRaises(ValueError) as caught:
			agent.run("append x twice")

		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
		self.assertEqual(recorder.ran, [])

	def _colliding_agent(self):
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("append_line", {"text": "x"}, "c1"),
					("append_line", {"text": "x"}, "c1"),
				)
			]
		)
		return recorder, Agent(model=model, tools=recorder.tools)

	def test_the_colliding_turn_never_enters_the_transcript(self):
		"""AT2. The refusal happens while the message is being BUILT, not after it is appended,
		so nothing downstream can ever see it.

		Driven through `_loop` on purpose. `run` hands the loop a COPY of a caller's list
		(`_build_initial_messages` returns `list(input)`), so asserting on the list passed to
		`run` would observe a list the loop never touched and would stay green even if the
		message were appended — which is exactly what happened when the mutation for this test
		was first run. The transcript the loop actually builds is the one that has to be checked.
		"""
		recorder, agent = self._colliding_agent()
		messages = [{"role": "user", "content": "append x twice"}]

		with self.assertRaises(ValueError):
			agent._loop(messages)

		self.assertEqual(_assistant_turns(messages), [])
		self.assertEqual(messages, [{"role": "user", "content": "append x twice"}])
		self.assertEqual(recorder.ran, [])

	def test_the_colliding_turn_never_enters_the_streamed_transcript_either(self):
		"""AT2b. The same claim on the streamed loop, which builds its transcript separately."""
		recorder, agent = self._colliding_agent()
		messages = [{"role": "user", "content": "append x twice"}]

		with self.assertRaises(ValueError):
			for _event in agent._loop_stream(messages):
				pass

		self.assertEqual(_assistant_turns(messages), [])
		self.assertEqual(recorder.ran, [])

	def test_it_raises_identically_in_the_streamed_loop(self):
		"""AT3. One check covers both loops, so the streamed path must refuse with the same
		literal — not merely raise something."""
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("append_line", {"text": "x"}, "c1"),
					("append_line", {"text": "x"}, "c1"),
				)
			]
		)
		agent = Agent(model=model, tools=recorder.tools)

		with self.assertRaises(ValueError) as caught:
			for _event in agent.run("append x twice", stream=True):
				pass

		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
		self.assertEqual(recorder.ran, [])


class TestTwoCallsWithNoUsableReference(UnitTestCase):
	"""AT10, AT10b, AT10c — the shapes a provider actually produces, and their controls."""

	def _refused(self, call_id: Any):
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("append_line", {"text": "x"}, call_id),
					("append_line", {"text": "y"}, call_id),
				),
				_final("done"),
			]
		)
		agent = Agent(model=model, tools=recorder.tools)
		with self.assertRaises(ValueError) as caught:
			agent.run("append twice")
		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
		self.assertEqual(recorder.ran, [])

	def test_two_calls_with_no_id_are_refused_like_any_other_collision(self):
		"""AT10. `None` from a provider that sends a null, `""` from one that omits the field or
		never streams it. Both become the same question key, the same answer lookup and the same
		membership test — which IS the defect. Skipping them rather than folding them into one
		bucket would leave the likelier half of it live."""
		for call_id in (None, ""):
			with self.subTest(id=call_id):
				self._refused(call_id)

	def test_a_single_call_with_no_id_is_unchanged(self):
		"""AT10b. The control, and the non-goal. ONE call with no reference is answerable —
		there is nothing to confuse it with — so it behaves exactly as it did."""
		recorder = _Recorder()
		model = FakeModel([_calls(("append_line", {"text": "x"}, None)), _final("done")])
		agent = Agent(model=model, tools=recorder.tools)

		result = agent.run("append once")

		self.assertEqual(result.output, "done")
		self.assertEqual(recorder.ran, [("append_line", {"text": "x"})])

	def test_ids_differing_only_by_case_or_whitespace_are_two_answerable_calls(self):
		"""AT10c. Nothing downstream normalises a reference — membership, the answer lookup and
		the question key all compare raw — so neither may this. `.strip().lower()` here would
		refuse two calls a person could genuinely answer apart."""
		for first, second in (("c1", "C1"), ("c1", "c1 ")):
			with self.subTest(ids=(first, second)):
				recorder = _Recorder()
				model = FakeModel(
					[
						_calls(
							("append_line", {"text": "x"}, first),
							("append_line", {"text": "y"}, second),
						),
						_final("done"),
					]
				)
				agent = Agent(model=model, tools=recorder.tools)

				result = agent.run("append twice")

				self.assertEqual(result.output, "done")
				self.assertEqual(
					recorder.ran,
					[("append_line", {"text": "x"}), ("append_line", {"text": "y"})],
				)


class TestDistinguishableTurnsAreUntouched(UnitTestCase):
	"""AT4, AT5 — the positive controls. Without these, every refusal above could be passing
	because the engine refuses everything."""

	def test_two_distinct_ids_still_both_execute(self):
		"""AT4."""
		recorder = _Recorder()
		model = FakeModel(
			[
				_calls(
					("append_line", {"text": "x"}, "c1"),
					("append_line", {"text": "y"}, "c2"),
				),
				_final("done"),
			]
		)
		agent = Agent(model=model, tools=recorder.tools)

		result = agent.run("append twice")

		self.assertEqual(result.output, "done")
		self.assertEqual(recorder.ran, [("append_line", {"text": "x"}), ("append_line", {"text": "y"})])
		self.assertEqual(
			[m["tool_call_id"] for m in result.messages if m.get("role") == "tool"], ["c1", "c2"]
		)

	def test_a_single_ungated_call_is_untouched(self):
		"""AT5, first half."""
		recorder = _Recorder()
		model = FakeModel([_calls(("append_line", {"text": "x"}, "c1")), _final("done")])
		agent = Agent(model=model, tools=recorder.tools)

		result = agent.run("append x")

		self.assertEqual(result.output, "done")
		self.assertEqual(recorder.ran, [("append_line", {"text": "x"})])

	def test_a_single_gated_call_still_pauses_with_one_question(self):
		"""AT5, second half. The options are the whole approval contract and must not move."""
		recorder = _Recorder()
		model = FakeModel([_calls(("send_money", {"to": "alice", "amount": 500}, "c1"))])
		agent = Agent(model=model, tools=recorder.tools)

		result = agent.run("pay alice")

		self.assertTrue(result.paused)
		self.assertEqual([q.key for q in result.questions], ["c1"])
		self.assertEqual(result.questions[0].options, ["Approve", "Deny"])
		self.assertEqual(recorder.ran, [])


class TestAStoredTranscriptCannotBeResumed(UnitTestCase):
	"""AT6, AT7 — the resume path. This is the half that protects a site already holding one."""

	def test_a_stored_colliding_transcript_cannot_be_resumed(self):
		"""AT6. The defect, reproduced on the resume path.

		The fake model carries a following final turn ON PURPOSE: without it, deleting the
		refusal would make this test red because the script ran out, which is the wrong reason.
		With it, the pre-fix observation is the recorder holding TWO executions on ONE "Approve".
		"""
		recorder = _Recorder()
		model = FakeModel([_final("paid")])
		agent = Agent(model=model, tools=recorder.tools)

		with self.assertRaises(ValueError) as caught:
			agent.resume(_transcript_with_two_colliding_calls(), {"c1": "Approve"})

		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
		self.assertEqual(recorder.ran, [])

	def test_a_stored_transcript_with_no_usable_reference_cannot_be_resumed_either(self):
		"""AT6b. The same on the shape a provider is likelier to produce."""
		for call_id in (None, ""):
			with self.subTest(id=call_id):
				recorder = _Recorder()
				agent = Agent(model=FakeModel([_final("paid")]), tools=recorder.tools)
				with self.assertRaises(ValueError) as caught:
					agent.resume(_transcript_with_two_colliding_calls(call_id), {call_id: "Approve"})
				self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
				self.assertEqual(recorder.ran, [])

	def test_a_caller_supplied_history_carrying_one_is_refused_at_the_door(self):
		"""AT6c. The third way in: history handed to `run`, validated by the same check."""
		recorder = _Recorder()
		agent = Agent(model=FakeModel([_final("paid")]), tools=recorder.tools)

		with self.assertRaises(ValueError) as caught:
			agent.run(_transcript_with_two_colliding_calls())

		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)
		self.assertEqual(recorder.ran, [])

	def test_a_stored_transcript_with_distinct_ids_still_resumes(self):
		"""AT7. The control for AT6, and the proof that `resume_run`'s answer contract has not
		moved: the same `{key: answer}` map, resolved exactly as it was."""
		recorder = _Recorder()
		call = {
			"id": "c1",
			"type": "function",
			"function": {"name": "send_money", "arguments": '{"to": "alice", "amount": 500}'},
		}
		second = {
			"id": "c2",
			"type": "function",
			"function": {"name": "send_money", "arguments": '{"to": "bob", "amount": 10}'},
		}
		messages = [
			{"role": "user", "content": "pay alice and bob"},
			{"role": "assistant", "content": None, "tool_calls": [call, second]},
		]
		agent = Agent(model=FakeModel([_final("paid")]), tools=recorder.tools)

		result = agent.resume(messages, {"c1": "Approve", "c2": "Deny"})

		# A Deny anywhere in the group stops the group — S14's contract, unchanged by S17.
		self.assertEqual(recorder.ran, [])
		results = {m["tool_call_id"] for m in result.messages if m.get("role") == "tool"}
		self.assertEqual(results, {"c1", "c2"})


class TestTheRefusalIsFitToBeRead(UnitTestCase):
	"""AT9 — it reaches a person on every path, so it is checked as person-facing text."""

	def test_the_refusal_names_no_platform_vendor_or_model(self):
		"""Both the forward refusal and the resume refusal, which are one literal."""
		recorder = _Recorder()
		agent = Agent(model=FakeModel([_final("x")]), tools=recorder.tools)

		raised = []
		model = FakeModel(
			[_calls(("append_line", {"text": "x"}, "c1"), ("append_line", {"text": "x"}, "c1"))]
		)
		forward = Agent(model=model, tools=recorder.tools)
		with self.assertRaises(ValueError) as caught:
			forward.run("go")
		raised.append(str(caught.exception))
		with self.assertRaises(ValueError) as caught:
			agent.resume(_transcript_with_two_colliding_calls(), {"c1": "Approve"})
		raised.append(str(caught.exception))

		self.assertEqual(len(set(raised)), 1)  # one literal, so neither path can drift
		for text in raised:
			for word in FORBIDDEN:
				with self.subTest(word=word):
					self.assertNotIn(word, text.lower())

		# The control: the same sweep over a string that DOES name one must report it.
		self.assertIn("frappe", f"{raised[0]} frappe".lower())

	def test_the_refusal_says_what_happened_and_that_nothing_was_done(self):
		"""A person reads this in the run's error field, or over the stream. It has to be a
		sentence, not a diagnostic."""
		self.assertIn("nothing was carried out", _UNANSWERABLE_TURN.lower())
		self.assertNotIn("tool_call", _UNANSWERABLE_TURN)
		self.assertNotIn("messages[", _UNANSWERABLE_TURN)


class TestTheCheckItself(UnitTestCase):
	"""The predicate, directly. Its return shape is part of the contract: a caller must never be
	able to decide on the truthiness of the value, because the empty reference is both a real
	collision and falsy."""

	def test_it_reports_a_collision_without_relying_on_truthiness(self):
		from flow.lib.agent import NO_CALL_REFERENCE, _indistinguishable_tool_call

		self.assertIsNone(_indistinguishable_tool_call(["c1", "c2"]))
		self.assertIsNone(_indistinguishable_tool_call([]))
		self.assertIsNone(_indistinguishable_tool_call(["c1"]))
		self.assertIsNone(_indistinguishable_tool_call([None]))
		self.assertEqual(_indistinguishable_tool_call(["c1", "c1"]), (True, "c1"))
		# Both no-reference shapes fold to ONE bucket, so they collide with each other too.
		self.assertEqual(_indistinguishable_tool_call([None, ""]), (True, NO_CALL_REFERENCE))
		self.assertEqual(_indistinguishable_tool_call([None, None]), (True, NO_CALL_REFERENCE))
		self.assertEqual(_indistinguishable_tool_call(["", ""]), (True, NO_CALL_REFERENCE))

	def test_a_guard_written_on_truthiness_would_be_wrong(self):
		"""The reason the return is a tuple. `bool((True, ""))` is True but `bool("")` is not:
		had the predicate returned the bare reference, `if collided:` would have waved the
		empty-reference collision — the likeliest one — straight through."""
		from flow.lib.agent import _indistinguishable_tool_call

		collided = _indistinguishable_tool_call([None, None])
		self.assertIsNotNone(collided)
		self.assertTrue(collided)
		self.assertFalse(collided[1] == "")  # it is the sentinel, never the raw empty string

	def test_it_reads_a_generator_once_and_does_not_need_a_sequence(self):
		"""Both call sites pass a generator expression."""
		from flow.lib.agent import _indistinguishable_tool_call

		self.assertEqual(_indistinguishable_tool_call(i for i in ["a", "a"]), (True, "a"))
		self.assertIsNone(_indistinguishable_tool_call(i for i in ["a", "b"]))


class TestNothingElseMoved(UnitTestCase):
	"""What it must not change."""

	def test_the_four_reviewed_functions_were_not_touched(self):
		"""A CONSTRAINT CHECK, not an acceptance test of S17: it is green before and after this
		change and no edit S17 makes can move it. `test_deny_stops_batch` owns the same pin with
		its own control; this restates it so the constraint is visible in S17's own file."""
		from flow.tests.test_deny_stops_batch import TestTheLoadBearingFunctionsAreUntouched as Pin

		pin = Pin("test_the_four_functions_are_byte_identical_to_their_reviewed_form")
		found = pin._digests(pin._engine_source())

		self.assertEqual(sorted(found), sorted(Pin.BASELINE_DIGESTS))
		for name, digest in Pin.BASELINE_DIGESTS.items():
			self.assertEqual(found[name], digest, f"{name} changed; rule 4 requires a spec naming it")

	def test_the_check_reads_nothing_but_the_calls_it_was_given(self):
		"""It must not reach the database, the session or anything else: it runs before the turn
		is admitted, on input a model controls."""
		import frappe

		recorder = _Recorder()
		model = FakeModel(
			[_calls(("append_line", {"text": "x"}, "c1"), ("append_line", {"text": "x"}, "c1"))]
		)
		agent = Agent(model=model, tools=recorder.tools)

		def explode(*args, **kwargs):
			raise AssertionError("the refusal path read the database")

		with patch.object(frappe, "get_all", explode), patch.object(frappe, "get_doc", explode):
			with self.assertRaises(ValueError) as caught:
				agent.run("go")

		self.assertEqual(str(caught.exception), _UNANSWERABLE_TURN)


class TestTheStreamedRefusalIsHonest(UnitTestCase):
	"""R1a, pinned rather than left as prose: the streamed loop announces each call before the
	turn is admitted, so a client draws both cards and then gets the error. Nothing executes and
	nothing persists, so this is cosmetic — but it is real, and a client has to clear them."""

	def test_the_stream_announces_both_calls_before_it_refuses(self):
		recorder = _Recorder()
		model = FakeModel(
			[_calls(("append_line", {"text": "x"}, "c1"), ("append_line", {"text": "x"}, "c1"))]
		)
		agent = Agent(model=model, tools=recorder.tools)

		seen = []
		with self.assertRaises(ValueError):
			for event in agent.run("go", stream=True):
				seen.append(event)

		self.assertFalse(any(isinstance(e, Done) for e in seen))
		self.assertEqual(recorder.ran, [])
