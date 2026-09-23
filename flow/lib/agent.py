# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Generator, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from frappe import _

from flow.lib.model import ChatResponse, Model, ToolCall, ToolCallBegin
from flow.lib.tool import Tool

if TYPE_CHECKING:
	from flow.knowledge import Knowledge

DEFAULT_MAX_ITERATIONS = 20
ERROR_MESSAGE_LIMIT = 500
# An approval question is read by a person before they decide. Past this it stops being read —
# and a question nobody finishes reading is worse than a plain one, so an over-long sentence is
# abandoned rather than cut.
CONFIRM_BODY_LIMIT = 2000
# No single value may crowd out the sentence around it. A longer one is not shortened; the whole
# sentence is abandoned, because a value shown in part reads exactly like a value shown in full.
CONFIRM_VALUE_LIMIT = 200
# The entire grammar of an approval question: a name in braces, replaced by the argument of that
# name. There are no expressions, filters, attribute access, indexing, loops or conditionals, and
# no engine is involved — a general template engine stood here once and could read the database
# from inside a question nobody had answered yet. A question that can COMPUTE is a question someone
# can steer, and this one is read by a person about to authorise a write.
CONFIRM_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]{0,63})\}")
# Shown whole, or not at all. `None`, lists and objects are not; the arguments below the sentence
# show them properly.
CONFIRM_SCALAR_TYPES = (str, bool, int, float)
# Every approval question the engine raises carries exactly these options, in this order —
# `_confirmation_question` builds them and nothing else in the engine does. At resume they are how
# a question the person was asked to APPROVE is told apart from a question a tool asked itself.
CONFIRM_ANSWER_OPTIONS = ("Approve", "Deny")
# Said to the model when a pending call will not be acted on. Both are literals and neither carries
# the person's answer: recording that answer as the call's result is the defect these exist to
# stop, because the model then reads its own tool call as having returned the word a person typed.
NOT_EXECUTED_MESSAGES = {
	"unavailable": (
		"This action was not carried out and nothing was done. It is no longer available. "
		"Do not report it as done. Tell the user it did not happen."
	),
	"approval_no_longer_applies": (
		"This action was not carried out and nothing was done. What it requires changed while the "
		"question was open, so the answer that was given no longer applies to it. "
		"Do not report it as done. Ask again before doing it."
	),
	"unattended": (
		"This action was not carried out and nothing was done. It needs someone to approve it, and "
		"this run has nobody who can. Do not report it as done. Say it needs a person."
	),
	"group_refused": (
		"This action was not carried out and nothing was done. It was held back with the other "
		"actions in the same reply, and one of them was refused. "
		"Do not report it as done. Ask before doing it on its own."
	),
}
VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


@dataclass
class Question:
	"""An LLM-authored prompt shown to the user when a run pauses.

	The same shape covers a free-text ask (empty `options`), a single- or
	multi-select. When `allow_other` is true the picker always offers an "Other"
	choice that opens a textbox for the user to reiterate or redirect.
	`key` routes the answer back (e.g. the tool_call_id it belongs to).
	"""

	prompt: str
	options: list[str] = field(default_factory=list)
	multi_select: bool = False
	allow_other: bool = True
	key: str | None = None


@dataclass
class RunResult:
	output: str | None
	messages: list[dict[str, Any]]
	tool_calls: list[ToolCall] = field(default_factory=list)
	iterations: int = 0
	usage: dict[str, int] = field(default_factory=dict)
	paused: bool = False
	questions: list[Question] = field(default_factory=list)


@dataclass
class TextChunk:
	"""A token delta from the model. Emitted as the LLM streams its reply."""

	text: str


@dataclass
class ToolStarted:
	"""A tool call is about to execute. Lets the UI render a 'thinking' indicator. Emitted as soon
	as the model starts streaming the call, so `arguments` may still be empty at that point — the
	full arguments arrive on the matching ToolEnded."""

	id: str
	name: str
	arguments: dict[str, Any]


@dataclass
class ToolEnded:
	"""A tool call finished. `result` is the JSON-serialized return value."""

	id: str
	name: str
	result: str


@dataclass
class Done:
	"""Terminal event: the run finished (Completed or Paused)."""

	result: RunResult


Event = TextChunk | ToolStarted | ToolEnded | Done


class Agent:
	def __init__(
		self,
		*,
		model: Model | str,
		name: str = "agent",
		instructions: str | None = None,
		tools: list[Tool] | None = None,
		knowledge: Knowledge | list[Knowledge] | None = None,
		max_iterations: int = DEFAULT_MAX_ITERATIONS,
		auto_approve: bool = False,
		unattended: bool = False,
	):
		if max_iterations < 1:
			raise ValueError("max_iterations must be at least 1")

		# A run with nobody in it: a trigger, a queued or background run, a run created with
		# approvals waived. Nobody can be asked anything, so a tool that requires asking is
		# refused rather than run or parked. Kept separate from `auto_approve` because a trigger
		# with that flag OFF has nobody in it too — it used to raise a question and wait forever.
		self.unattended = unattended
		self.auto_approve = auto_approve
		self.name = name
		self.model = Model(model) if isinstance(model, str) else model
		self.instructions = instructions
		self.tools = list(tools or [])
		if knowledge:
			from flow.tools.builtins import bind_search_knowledge

			items = knowledge if isinstance(knowledge, list) else [knowledge]
			self.tools.append(bind_search_knowledge([k.name for k in items]))
		self.max_iterations = max_iterations

		self._tools_by_name: dict[str, Tool] = {}
		for tool in self.tools:
			if tool.name in self._tools_by_name:
				raise ValueError(f"Duplicate tool name: {tool.name!r}")
			self._tools_by_name[tool.name] = tool

	def run(self, input: str | list[dict[str, Any]], *, stream: bool = False) -> RunResult | Generator[Event]:
		"""Run the agent on `input`. With `stream=True`, returns a generator of `Event`s
		(text deltas, tool start/end markers, and a final `Done` carrying the `RunResult`)."""
		messages = self._build_initial_messages(input)
		if stream:
			return self._loop_stream(messages)
		return self._loop(messages)

	def resume(
		self,
		messages: list[dict[str, Any]],
		answers: dict[str, Any],
		*,
		stream: bool = False,
		asked: list[Any] | None = None,
	) -> RunResult | Generator[Event]:
		"""Continue a run that paused on a question.

		`answers` maps each pending tool_call_id to the user's answer: "Approve" runs
		the tool, "Deny" records the rejection and stops the run, and any other free
		text is returned to the LLM as redirect feedback so it can adjust and retry.

		`asked` is the questions this pause raised, as they were recorded when it paused —
		`Question`s or the rows stored from them. The runtime resuming a run is not always the
		one that paused it, so what the person was asked cannot be re-derived from the tools
		present now; it is read from the record or not at all. Passing nothing is supported and
		safe: see `_prepare_resume`.
		"""
		# A resume exists because a person is answering, so it is attended by definition — and the
		# runtime that resumes is not always a fresh one. `flow/lib/session.py:78-79` hands back the
		# CALLER'S OWN object, and `FlowSession.chat` mutates this attribute on it every turn, so an
		# in-process caller that ran one unattended turn and then resumed carried `unattended=True`
		# into a run somebody was sitting in front of: their approval was honoured, and the model's
		# very next gated call in the same run was refused as having nobody to approve it.
		# Set here rather than in each caller, because every resume means the same thing.
		# `auto_approve` is deliberately left alone: it is a caller's standing instruction about the
		# whole run, not a statement about who is present, and upstream's callers rely on it.
		self.unattended = False
		if stream:
			return self._resume_stream(messages, answers, asked)
		messages, _ = self._prepare_resume(messages, answers, asked)
		if _has_denial(answers):
			return self._stopped_result(messages)
		return self._loop(messages, self._answered_calls(messages))

	def _resume_stream(
		self, messages: list[dict[str, Any]], answers: dict[str, Any], asked: list[Any] | None = None
	) -> Generator[Event]:
		"""Stream a resume: first replay the just-resolved tool results so the UI can fill in
		the tool cards that were awaiting an answer, then continue the agent loop (or stop
		if the user denied)."""
		messages, resolved = self._prepare_resume(messages, answers, asked)
		for call, content, announce in resolved:
			# A call held back for the group has no card: it was not announced when the run
			# paused, because it was not starting then. Here it is — with the arguments that ran,
			# so the card the client draws is the action, not an empty box.
			if announce:
				yield ToolStarted(id=call.id, name=call.name, arguments=call.arguments)
			yield ToolEnded(id=call.id, name=call.name, result=content)
		if _has_denial(answers):
			yield Done(result=self._stopped_result(messages))
			return
		yield from self._loop_stream(messages, self._answered_calls(messages))

	def _stopped_result(self, messages: list[dict[str, Any]]) -> RunResult:
		"""Terminal result for a run the user denied: pending calls are already resolved in
		`messages`, so end the turn here without another model call (no further tokens)."""
		return RunResult(
			output=None,
			messages=messages,
			tool_calls=self._answered_calls(messages),
			iterations=0,
		)

	def new_session(self, *, title: str | None = None) -> Any:
		"""Start a persisted conversation driven by this code agent (session's agent link is left empty)."""
		from flow.lib.session import new_session

		return new_session(self, title=title)

	def snapshot(self) -> dict[str, Any]:
		"""Config record stored on each Flow Run for traceability."""
		return {
			"title": self.name,
			"model": self.model.model_id,
			"instructions": self.instructions,
			"tools": [t.name for t in self.tools],
			"max_iterations": self.max_iterations,
		}

	def _prepare_resume(
		self, messages: list[dict[str, Any]], answers: dict[str, Any], asked: list[Any] | None = None
	) -> tuple[list[dict[str, Any]], list[tuple[ToolCall, str, bool]]]:
		"""Append a tool result for each pending call. Returns the new messages plus the
		(call, content, announce) triples resolved, so a streaming resume can replay them as
		events. `announce` is true for a call no question was ever raised for — one held back for
		the group. No card was drawn for it when the run paused, because it was not starting then;
		the client is told about it here, where it is, with the arguments that are about to run —
		and not at all when the answers hold a denial, because then it is not about to run either.

		A pause can hold several questions, and they are answered as one group. If any answer
		in the group is a denial, nothing in the group runs: an "Approve" beside it is recorded
		as approved and withheld rather than executed. Refusing one of several actions shown
		together is refusing all of them, not leaving the rest to go ahead.

		The denial is read from the same `_has_denial` the caller uses to halt the run, so the
		halt and the withholding can never disagree about what the answers said.

		The runtime that resumes a run is rebuilt from the record and is not always the one that
		paused it, so a pending call's tool may be gone, or may no longer be gated. Neither may be
		acted on and neither may be closed out with the person's own answer as the tool's result —
		a person who approved something and was told it was done, when nothing ran, has been
		misled by the engine rather than by anyone. Both fail closed, in that order:

		1. the tool is not here at all — nothing can honour the answer, whatever it said;
		2. the tool is here and gated — today's path, unchanged, group rule included;
		3. what the person was asked and what the tool now requires disagree in either direction —
		   the gate was turned off, or turned on, while the question was open — so the basis of
		   the answer is gone and it must be asked again rather than acted on;
		4. anything else — the tool asked its own question, and the answer is its result, as ever.

		Row 3 is symmetric, and deliberately so. A gate turned OFF while the question was open is
		an approval that no longer applies; a gate turned ON is a tool about to execute on an
		answer to a question nobody was asked to approve. The second of those EXECUTES if it is
		not caught, so both directions end in the same place: not executed, ask again.

		Rows 2 and 3 read `asked`, because it is the only thing that still knows what was asked.
		With no record the tool's own gate stands in for it, so the two can never disagree, row 3
		cannot fire, and behaviour is exactly what it was — the right fallback for a run that
		paused before any of this existed. Row 1 needs no record at all.
		"""
		# Structure over the whole list; indistinguishability over the turn about to be acted on,
		# and only that turn. A collision whose calls already have results can never execute again
		# — `_transcript_calls` counts BOTH of a colliding pair answered, so `_prepare_resume` does
		# not even iterate them — so refusing it stops nothing and costs everything: it refused
		# every later approval in that session, forever, recoverable only by deleting the stored
		# row, with a sentence that was false for the person reading it. Validation refuses what is
		# about to RUN, never old history — the same rule R7 settled for the replay path.
		_validate_messages(messages, refuse_indistinguishable_calls=False, refuse_unusable_tool_results=False)
		_refuse_indistinguishable_live_turn(messages, answers)
		messages = list(messages)
		pending = self._pending_calls(messages)
		if not pending:
			raise ValueError("No questions awaiting an answer in the provided messages")

		denied_group = _has_denial(answers)
		# Where the turn this pause is on begins in `pending`. Everything before it is the
		# wreckage of a run that was stopped or that failed; it is resolved with nothing and never
		# acted on — see the first branch of the ladder below.
		live_from = len(pending) - _live_turn_pending_count(messages)
		approval_keys = _approval_question_keys(asked)
		question_keys = _all_question_keys(asked)
		have_record = bool(asked)
		resolved: list[tuple[ToolCall, str, bool]] = []
		for index, call in enumerate(pending):
			answer = answers.get(call.id)
			# Nothing was asked about this call, so the client has no card for it: it was held
			# back, and a held-back call is announced when it runs, not before.
			#
			# All four clauses, and each closes a real card. `index >= live_from`: a call from a
			# turn nobody is resuming is not going to run, and announcing it drew model-authored
			# arguments onto the screen where the person had just approved something else — a
			# question's shape is never steered by values a model wrote. `have_record`: with no
			# record the engine cannot tell a held-back call from an unanswered one and runs
			# neither, so it announces neither. `not denied_group`: a denial starts nothing, so
			# it opens no card — drawing one, with the model's own arguments, for the action the
			# person had just declined is the same defect on the one path where the answer was
			# no. Announce exactly what this resume will run.
			announce = (
				index >= live_from and have_record and not denied_group and call.id not in question_keys
			)
			tool = self._tools_by_name.get(call.name)
			# What the person was asked. With no record of the pause there is nothing to read it
			# from, so the tool's own gate stands in and the two can never disagree — which is
			# exactly the behaviour that existed before any of this was recorded.
			asked_to_approve = (
				call.id in approval_keys if have_record else bool(tool and tool.requires_confirmation)
			)
			if index < live_from:
				# Not the turn this pause is on. `_pending_calls` spans the WHOLE transcript, so
				# every batch that paused and was then abandoned — Stop clears a run's questions
				# but not its messages, and a resume that raises does the same — leaves its calls
				# pending for ever. Decided FIRST, above every other rule, because acting on such
				# a call at all is the mistake: it ran a held-back action from a decision the
				# person walked away from, and where its reference happened to match the live
				# turn's it took the live turn's answer as its own, so one "Approve" executed two
				# gated writes. By POSITION, never by reference: two turns can carry the same
				# reference, and that is exactly the case this exists to separate.
				# Nothing here is a decision anybody is making. Closed out with
				# nothing, as it was before S19 — which also stops it being pending a second time.
				content = _serialize_tool_result(None)
			elif tool is None:
				content = _not_executed("unavailable")
			elif asked_to_approve and tool.requires_confirmation:
				# Only the exact "Approve" ever executes, so that is the only answer the group's
				# denial has to hold back; everything else resolves exactly as it always has.
				if denied_group and answer == "Approve":
					content = _withheld_confirmation()
				else:
					content = self._resolve_confirmation(call, answer)
			elif asked_to_approve or tool.requires_confirmation:
				content = _not_executed("approval_no_longer_applies")
			elif have_record and call.id not in answers and call.id not in question_keys:
				# Nobody was asked about this call: it was held back so that nothing in its turn
				# ran before the person answered. `not in`, never `.get(...) is None` — an answer
				# of None is an answer, and running a tool on the strength of one would be the
				# same defect one branch down. Refusing one action in the group refuses this one
				# with it, because it was held back for the group's sake.
				#
				# Reached only for the live turn — everything else was decided above.
				#
				# `have_record` first: a call HELD BACK and a call
				# whose own question went unanswered are indistinguishable from `answers` alone —
				# both simply have no answer — and only the record tells them apart. Without one,
				# running is the wrong guess in both directions: the second has already executed
				# once, and the first has no answer of any kind behind it. So with no record this
				# falls through to the pre-existing branch below and nothing runs, which is what
				# the ladder in this docstring promises for a pause it cannot read.
				content = (
					_not_executed("group_refused")
					if denied_group
					else _serialize_tool_result(self._run_tool(call))
				)
			elif have_record and call.id in answers and call.id not in question_keys:
				# An answer arrived for a call no question was ever raised for. `answers` is
				# validated for shape only, so this is reachable from the web: acting on it would
				# hand the model whatever text was sent as this tool's result.
				#
				# `call.id in answers` is what the sentence above always meant, and it has to be
				# said out loud now that the branch above can decline a call with no answer: an
				# UNANSWERED call from a turn nobody is resuming is not an answer that no longer
				# applies — nothing was asked and nothing was given — so it falls through to the
				# branch below and is closed out with nothing, exactly as it was before S19.
				content = _not_executed("approval_no_longer_applies")
			else:
				content = _serialize_tool_result(answer)
			messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
			resolved.append((call, content, announce))
		return messages, resolved

	def _resolve_confirmation(self, call: ToolCall, answer: Any) -> str:
		"""Run the tool if approved; deny if rejected; redirect with user feedback otherwise."""
		if answer == "Approve":
			result = self._run_tool(call)
			return _serialize_tool_result(result)
		if answer == "Deny":
			return json.dumps({"status": "denied", "message": "User denied this tool call."})
		# Free-text "Other"
		return json.dumps(
			{
				"status": "redirect",
				"message": "Tool not executed.",
				"user_feedback": answer,
				"instruction": "The user wants changes before this proceeds. Read their feedback carefully, adjust your approach, and try again.",
			}
		)

	def _loop(
		self, messages: list[dict[str, Any]], executed_calls: list[ToolCall] | None = None
	) -> RunResult:
		tool_schemas = [t.to_dict() for t in self.tools] or None
		executed_calls = executed_calls if executed_calls is not None else []
		usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

		for iteration in range(1, self.max_iterations + 1):
			response = self.model.chat(messages, tools=tool_schemas)
			_accumulate_usage(usage_total, response.usage)
			messages.append(_assistant_message(response))

			if not response.tool_calls:
				return RunResult(
					output=response.content,
					messages=messages,
					tool_calls=executed_calls,
					iterations=iteration,
					usage=usage_total,
				)

			questions: list[Question] = []
			# The whole batch is classified before any of it runs. A model can ask for a read and
			# a write in one breath; if the write needs a person, the read must not already have
			# happened in their name by the time they are asked, and a Deny must not leave them
			# with a done thing nobody mentioned. The calls nobody was asked about are held back
			# with no tool result, which is what makes them pending at resume.
			plan = [(call, self._disposition(call)) for call in response.tool_calls]
			if any(disposition == "ask" for _call, disposition in plan):
				for call, disposition in plan:
					if disposition != "ask":
						continue
					question = self._invoke(call)
					question.key = call.id
					questions.append(question)
				return RunResult(
					output=response.content,
					messages=messages,
					tool_calls=executed_calls,
					iterations=iteration,
					usage=usage_total,
					paused=True,
					questions=questions,
				)

			for call, disposition in plan:
				result = self._invoke(call)
				if isinstance(result, Question):
					# The second way a run pauses, and the one `_disposition` cannot classify:
					# a question the TOOL ITSELF returned, after its body had already run. In a
					# run with nobody in it that parks the run in `Paused`, holding its session,
					# waiting for an answer that can never come — the same failure a gated tool
					# used to cause, arriving by the other door. The body has run and is not
					# undone; what is refused is the QUESTION, so the model is told the action
					# was not carried through rather than being left mid-sentence.
					if not self._is_unattended():
						result.key = call.id
						questions.append(result)
						continue
					result = _not_executed("unattended")

				# A refused call is told to the model like any other result, and the run carries
				# on — but it did not run, so it is not one of the calls this turn executed.
				if disposition != "refuse":
					executed_calls.append(call)
				messages.append(
					{
						"role": "tool",
						"tool_call_id": call.id,
						"content": _serialize_tool_result(result),
					}
				)

			if questions:
				return RunResult(
					output=response.content,
					messages=messages,
					tool_calls=executed_calls,
					iterations=iteration,
					usage=usage_total,
					paused=True,
					questions=questions,
				)

		raise RuntimeError(f"Agent {self.name!r} exceeded max_iterations ({self.max_iterations})")

	def _loop_stream(
		self, messages: list[dict[str, Any]], executed_calls: list[ToolCall] | None = None
	) -> Generator[Event]:
		tool_schemas = [t.to_dict() for t in self.tools] or None
		executed_calls = executed_calls if executed_calls is not None else []
		usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

		for iteration in range(1, self.max_iterations + 1):
			chunks = self.model.chat(messages, tools=tool_schemas, stream=True)
			# Tool calls are announced (ToolCallBegin) the moment the model starts emitting them,
			# so the UI can show the tool before its arguments finish streaming. They are held
			# here rather than sent straight out: whether a call runs in this step is a property
			# of the WHOLE reply — one call needing a person holds every other call in that reply
			# back — and that cannot be known until the reply is complete. Announcing mid-stream
			# opened a card for an action that was not starting, on exactly the screen where
			# someone was being asked to approve its neighbour.
			held: list[ToolStarted] = []
			try:
				while True:
					item = next(chunks)
					if isinstance(item, ToolCallBegin):
						held.append(ToolStarted(id=item.id, name=item.name, arguments={}))
					else:
						yield TextChunk(text=item)
			except StopIteration as e:
				response = e.value
			_accumulate_usage(usage_total, response.usage)
			# Classified before the turn is admitted, only so the announcements can be released in
			# the order a client has always seen them. `_disposition` reads the tool's own flag and
			# nothing else — it runs no tool body and touches no message — so the refusal
			# `_assistant_message` may raise below still happens before anything is invoked.
			plan = [(call, self._disposition(call)) for call in response.tool_calls or []]
			asked_ids = {call.id for call, disposition in plan if disposition == "ask"}
			for event in held:
				# Nothing is announced that will not run in this step. When the reply has to ask
				# someone, the only calls that go anywhere are the ones being asked about; the rest
				# are held back and are announced on resume, when they run.
				if not asked_ids or event.id in asked_ids:
					yield event
			messages.append(_assistant_message(response))

			if not response.tool_calls:
				yield Done(
					result=RunResult(
						output=response.content,
						messages=messages,
						tool_calls=executed_calls,
						iterations=iteration,
						usage=usage_total,
					)
				)
				return

			questions: list[Question] = []
			# The same one decision as `_loop`, in the loop the web apps actually use. A held-back
			# call produces NO event here — not a start, not an end. It is not running, so there is
			# nothing to draw; `_resume_stream` announces it when the answers come back and it
			# actually runs, with its full arguments. (`plan` was built above.)
			if asked_ids:
				for call, disposition in plan:
					if disposition != "ask":
						continue
					yield ToolStarted(id=call.id, name=call.name, arguments=call.arguments)
					question = self._invoke(call)
					question.key = call.id
					questions.append(question)
					yield ToolEnded(id=call.id, name=call.name, result="")
				yield Done(
					result=RunResult(
						output=response.content,
						messages=messages,
						tool_calls=executed_calls,
						iterations=iteration,
						usage=usage_total,
						paused=True,
						questions=questions,
					)
				)
				return

			for call, disposition in plan:
				# Re-announce with the full arguments now that they've finished streaming, before the
				# tool runs — so the UI shows the arguments during execution, not only with the result.
				yield ToolStarted(id=call.id, name=call.name, arguments=call.arguments)
				result = self._invoke(call)
				if isinstance(result, Question):
					# The second way a run pauses, and the one `_disposition` cannot classify:
					# a question the TOOL ITSELF returned, after its body had already run. In a
					# run with nobody in it that parks the run in `Paused`, holding its session,
					# waiting for an answer that can never come — the same failure a gated tool
					# used to cause, arriving by the other door. The body has run and is not
					# undone; what is refused is the QUESTION, so the model is told the action
					# was not carried through rather than being left mid-sentence.
					if not self._is_unattended():
						result.key = call.id
						questions.append(result)
						yield ToolEnded(id=call.id, name=call.name, result="")
						continue
					result = _not_executed("unattended")

				if disposition != "refuse":
					executed_calls.append(call)
				serialized = _serialize_tool_result(result)
				messages.append({"role": "tool", "tool_call_id": call.id, "content": serialized})
				yield ToolEnded(id=call.id, name=call.name, result=serialized)

			if questions:
				yield Done(
					result=RunResult(
						output=response.content,
						messages=messages,
						tool_calls=executed_calls,
						iterations=iteration,
						usage=usage_total,
						paused=True,
						questions=questions,
					)
				)
				return

		raise RuntimeError(f"Agent {self.name!r} exceeded max_iterations ({self.max_iterations})")

	def _pending_calls(self, messages: list[dict[str, Any]]) -> list[ToolCall]:
		"""Tool calls in the transcript that have no tool result yet (awaiting an answer)."""
		return self._transcript_calls(messages, answered=False)

	def _answered_calls(self, messages: list[dict[str, Any]]) -> list[ToolCall]:
		"""Tool calls in the transcript that already have a tool result (executed)."""
		return self._transcript_calls(messages, answered=True)

	def _transcript_calls(self, messages: list[dict[str, Any]], *, answered: bool) -> list[ToolCall]:
		has_result = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
		calls: list[ToolCall] = []
		for message in messages:
			if message.get("role") != "assistant":
				continue
			for tc in message.get("tool_calls") or []:
				if (tc["id"] in has_result) != answered:
					continue
				fn = tc["function"]
				arguments = fn.get("arguments") or "{}"
				calls.append(ToolCall(id=tc["id"], name=fn["name"], arguments=json.loads(arguments)))
		return calls

	def _build_initial_messages(self, input: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
		"""For a string, build [system?, user]. For a list, trust the caller and use it as-is —
		the caller owns the system message and any history."""
		if isinstance(input, str):
			messages: list[dict[str, Any]] = []
			if self.instructions:
				messages.append({"role": "system", "content": self.instructions})
			messages.append({"role": "user", "content": input})
			return messages
		# Replayed history is read, never executed: on this path `_loop` only ever invokes calls
		# from the CURRENT reply, and `_pending_calls`/`_prepare_resume` are not reachable from
		# here. Refusing an indistinguishable turn in a caller's history therefore prevents no
		# double write, while a conversation is replayed through here on EVERY later message — so
		# one such turn stored in a transcript would refuse every future turn of that
		# conversation, forever, recoverable only by someone who can delete the stored row.
		# The refusal belongs where a tool can be reached from the calls in question: the model's
		# new reply, and a resume.
		_validate_messages(input, refuse_indistinguishable_calls=False, refuse_unusable_tool_results=False)
		return list(input)

	def _is_unattended(self) -> bool:
		"""A run with nobody who can answer a question."""
		return bool(self.unattended or self.auto_approve)

	def _disposition(self, call: ToolCall) -> str:
		"""What happens to this call before anything in its turn runs: "refuse", "ask" or "run".

		Read from the tool's own flag, never from the tool body, so a whole batch can be decided
		before any of it executes. A question a TOOL returns is not an approval and is not
		classified here: by the time it exists its body has already run, so holding its
		neighbours back would protect nothing.
		"""
		tool = self._tools_by_name.get(call.name)
		if call.error or tool is None:
			return "run"
		if not tool.requires_confirmation:
			return "run"
		# Refuse is decided BEFORE ask, and the order is the whole rule: an unattended run must
		# never reach the question at all. Read once and in one place, so a batch can never be
		# classified by a rule that differs from the one that executes it.
		return "refuse" if self._is_unattended() else "ask"

	def _invoke(self, call: ToolCall) -> Any:
		"""Run a tool and return its raw result. A Question (returned or synthesized for
		`requires_confirmation` tools) signals a pause."""
		if call.error:
			return json.dumps({"error": call.error})
		tool = self._tools_by_name.get(call.name)
		if tool is None:
			return json.dumps({"error": f"Unknown tool: {call.name!r}"})
		disposition = self._disposition(call)
		if disposition == "refuse":
			return _not_executed("unattended")
		if disposition == "ask":
			return _confirmation_question(call, tool)
		return self._run_tool(call)

	def _run_tool(self, call: ToolCall) -> Any:
		"""Invoke the tool, returning its result or a serialized error message."""
		tool = self._tools_by_name[call.name]
		try:
			return tool(**call.arguments)
		except Exception as e:
			return json.dumps({"error": str(e)[:ERROR_MESSAGE_LIMIT]})


# A tool call that cannot be told apart from another in the same turn cannot be approved separately
# from it. There are two such shapes and they fail the same way downstream, so they are one rule
# here: two calls carrying the same id, and two calls carrying no usable id at all — `None` from a
# provider that sends a null, `""` from one that omits the field or never streams it. Both end up as
# the same question key, the same answer lookup and the same membership test, which is the defect.
# Anything that is not a non-empty string is folded into one bucket rather than skipped: skipping it
# is what would leave the likelier half of this defect live.
#
# A SINGLE call folding to this sentinel is refused as well, by the sibling rule in
# `_assistant_message` and with its own sentence. That is not this rule reaching further: one call
# collides with nothing. It is the other thing a reference is for — filing the result — and the
# comment there says why waving one through ended the conversation rather than misrouting an answer.
NO_CALL_REFERENCE = "<none>"

# A plain literal, deliberately, like every other module-level message in this file. `_()` here
# would run at IMPORT time and freeze the translation for the whole worker in whatever language
# happened to be current then — and it would mean the vendor-word check over this string only ever
# inspects the English one, which is the single thing that check exists to prevent.
_UNANSWERABLE_TURN = (
	"Two actions in one reply could not be told apart, so one approval would have answered both. "
	"Nothing was carried out."
)

# Its sibling, and deliberately not the same sentence. A single call carrying no usable reference
# is refused too (see `_assistant_message`), but nothing collided: there is one action, nothing to
# tell it apart from, and — the shape a provider actually produces is as often an ungated tool as a
# gated one — frequently no approval question at all. Telling that person two actions could not be
# told apart and that one approval would have answered both is false on every clause, and a refusal
# a person cannot believe is the defect the rest of this run exists to remove. What IS true is the
# reason: a reference is how an answer is matched AND how the result is filed, and this reply
# carries neither.
_UNFILEABLE_CALL = (
	"An action in this reply arrived with nothing to answer or record it by. Nothing was carried out."
)


def _call_reference(id: Any) -> str:
	"""The reference a call will be answered by, and have its result filed under.

	Anything that is not a non-empty string folds to one sentinel, because that is what every
	lookup downstream does with it: the question key, the answer lookup, the `has_result`
	membership test, and the `tool_call_id` a result is written with. One definition, used by
	both rules below, so the two can never disagree about what counts as usable.
	"""
	return id if isinstance(id, str) and id else NO_CALL_REFERENCE


def _indistinguishable_tool_call(ids: Iterable[Any]) -> tuple[bool, str] | None:
	"""The first reference that appears twice in one assistant turn, or None.

	Returns `(found, reference)` rather than a bare string, so no caller can ever decide on the
	truthiness of the value — an empty reference is a real collision and the most likely one, and a
	guard written `if collided:` would wave it through.

	References are compared EXACTLY: no case-folding and no stripping, because nothing downstream
	normalises either. Membership, the answer lookup and the question key all compare raw, so "c1"
	and "C1" are two answerable calls and must stay two.
	"""
	seen: set[str] = set()
	for id in ids:
		reference = _call_reference(id)
		if reference in seen:
			return True, reference
		seen.add(reference)
	return None


def _live_turn_pending_count(messages: list[Any]) -> int:
	"""How many of `_pending_calls`'s results belong to the turn the pause is on.

	`_transcript_calls` walks the transcript in order, so the unanswered calls of the LAST
	assistant turn that has any are exactly the TAIL of the list it returns. This returns the
	length of that tail.

	Counted, never matched by reference, and that is the whole point: two turns can carry the same
	`tool_call_id`, which is precisely the case where taking the live turn's answer for an older
	turn's call executes a second gated write on one approval. A reference is not an identity
	across turns, so nothing here uses one.

	Everything before that tail is the wreckage of a run that was stopped or that failed — Stop
	clears a run's questions but not its messages, and a resume that raises does the same — and a
	transcript is replayed whole on every later turn, so that wreckage stays pending for ever.

	`messages` is a list, not an `Iterable`: this walks it twice, and a generator would make the
	second pass see nothing, return 0, and quietly treat EVERY pending call as wreckage — the
	person's approval would resolve nothing, with no error anywhere. `_prepare_resume` validates
	for a list before calling this, and the annotation now says so rather than inviting one.
	"""
	has_result = {m.get("tool_call_id") for m in messages if isinstance(m, dict) and m.get("role") == "tool"}
	count = 0
	for message in messages:
		if not isinstance(message, dict) or message.get("role") != "assistant":
			continue
		tool_calls = message.get("tool_calls")
		if not tool_calls:
			continue
		ids = [tc.get("id") if isinstance(tc, dict) else None for tc in tool_calls]
		unanswered = [id for id in ids if id not in has_result]
		if unanswered:
			count = len(unanswered)
	return count


def _refuse_indistinguishable_live_turn(messages: list[Any], answers: Any) -> None:
	"""Refuse an assistant turn a resume is LIVE on and whose calls cannot be told apart.

	The resume-path half of the rule `_validate_messages` applies to a model's new reply. A turn is
	live if it is the LAST one still holding a call with no result — the turn this pause is on —
	or if one of its calls is a key in `answers`, which is a person answering THAT turn. The second
	half is what keeps the nastiest stored shape closed: two colliding calls, one of which already
	ran, leaves nothing pending (`_transcript_calls` counts BOTH of a colliding pair answered), yet
	an answer for that reference would resolve the one that had already executed.

	Everything else is history — a turn already finished, or a pause the person abandoned, which
	stays unanswered for ever because Stop clears a run's questions and not its messages. Neither
	can execute: `_prepare_resume` closes every call outside the live turn out with nothing before
	any other rule is consulted. So refusing them stops nothing and costs a session every later
	approval it will ever make, which is the whole of what R10 is about. "Last turn with something
	pending", not "any turn with something pending", is the difference between the two.

	A call whose reference is `None` or `""` can never be in `has_result` — `_validate_messages`
	rejects a tool message with no `tool_call_id` outright — so a turn carrying those counts as
	holding something pending, and the last such turn is checked. That is the likelier half of the
	defect and the half a test written only around duplicate strings would miss.

	`messages` is a list, not an `Iterable`: this walks it twice, and a generator would silently
	report that no turn is live at all.
	"""
	answered_keys = answers if isinstance(answers, dict) else {}
	has_result = {m.get("tool_call_id") for m in messages if isinstance(m, dict) and m.get("role") == "tool"}

	def _ids(message: Any) -> list[Any] | None:
		if not isinstance(message, dict) or message.get("role") != "assistant":
			return None
		tool_calls = message.get("tool_calls")
		if not tool_calls:
			return None
		return [tc.get("id") if isinstance(tc, dict) else None for tc in tool_calls]

	last_pending = None
	for message in messages:
		ids = _ids(message)
		if ids is not None and any(id not in has_result for id in ids):
			last_pending = message

	for message in messages:
		ids = _ids(message)
		if ids is None:
			continue
		if message is not last_pending and not any(id in answered_keys for id in ids):
			continue
		if _indistinguishable_tool_call(ids) is not None:
			raise ValueError(_UNANSWERABLE_TURN)


def _validate_messages(
	messages: Any,
	*,
	refuse_indistinguishable_calls: bool = True,
	refuse_unusable_tool_results: bool = True,
) -> None:
	"""Structural checks on a message list, plus — unless asked not to — the refusal of an
	assistant turn whose calls cannot be told apart, and of a tool result carrying no reference.

	Both flags default to True so a new call site is guarded unless it says otherwise. The callers
	that opt out are the two that replay STORED history, and they say why: validation refuses what
	is about to RUN, never old history. A tool result with no reference is a record of something
	that already happened, so refusing it ends a conversation and prevents nothing — exactly the
	blast radius R7 and R10 settled for the other rule. A reply that would CREATE one is refused
	where every other unanswerable reply is, in `_assistant_message`, before it is stored.

	`refuse_unusable_tool_results` governs an EMPTY reference only. A tool message missing the
	field altogether is malformed rather than historical, and is refused on every path.
	"""
	if not isinstance(messages, list):
		raise TypeError(f"input must be a str or list of message dicts, got {type(messages).__name__}")
	for i, message in enumerate(messages):
		if not isinstance(message, dict):
			raise TypeError(f"messages[{i}] must be a dict, got {type(message).__name__}")
		role = message.get("role")
		if role not in VALID_ROLES:
			raise ValueError(f"messages[{i}].role must be one of {sorted(VALID_ROLES)}, got {role!r}")
		if role == "tool" and "tool_call_id" not in message:
			raise ValueError(f"messages[{i}] is a tool message but has no tool_call_id")
		if refuse_unusable_tool_results and role == "tool" and not message["tool_call_id"]:
			# Two different defects, and only one of them is history's. A message with no
			# `tool_call_id` FIELD is malformed — the caller built the wrong shape, and that is
			# refused wherever it arrives. A field that is present and empty is a RECORD of
			# something that already ran and was filed under nothing; refusing it ends a
			# conversation and prevents nothing, so the replay paths let it through.
			raise ValueError(f"messages[{i}] is a tool message with no usable tool_call_id")
		if "content" not in message and "tool_calls" not in message:
			raise ValueError(f"messages[{i}] must have 'content' or 'tool_calls'")
		if refuse_indistinguishable_calls and role == "assistant" and message.get("tool_calls"):
			# What makes this fix complete rather than only forward-looking: a run that paused
			# BEFORE it, with a transcript the engine would now refuse, is refused at resume
			# instead of double-executing. `_prepare_resume` validates here, so the ordinary
			# resume is closed as well as the model's new reply (`_assistant_message`).
			# Person-facing on the resume path — the text reaches the run's error field — so it
			# is the same one sentence, not a diagnostic.
			# Iterated exactly as `_transcript_calls` iterates it — any sequence, not only a list —
			# and an entry this cannot read folds to the sentinel instead of being SKIPPED.
			# Skipping is what let v1 of this rule wave two `None` ids through; a shape the check
			# drops but `_transcript_calls` still turns into a ToolCall is that hole in a new hat.
			if (
				_indistinguishable_tool_call(
					tc.get("id") if isinstance(tc, dict) else None for tc in message["tool_calls"]
				)
				is not None
			):
				raise ValueError(_UNANSWERABLE_TURN)


def _assistant_message(response: ChatResponse) -> dict[str, Any]:
	message: dict[str, Any] = {"role": "assistant", "content": response.content}
	if response.tool_calls:
		references = [_call_reference(call.id) for call in response.tool_calls]
		# Two rules, two sentences, and the order between them is what decides which sentence a
		# person reads. Refused HERE, before the message is returned to be appended and therefore
		# before anything is invoked — both loops build this message before they touch
		# `response.tool_calls`.
		#
		# COLLISION FIRST, and not for tidiness. Two calls that both carry no usable reference are
		# both things at once, and the collision is the graver of the two: one question cannot
		# address two actions — both would carry the same key, one answer would resolve both, and
		# the tool would run twice on one approval. That is what such a person needs told, so that
		# shape keeps the sentence it has always had.
		#
		# `is not None`, never `if collided:` — the no-reference case is a real collision and its
		# reference is the falsy one. The predicate returns a tuple so this cannot be "simplified"
		# into a bug.
		if _indistinguishable_tool_call(references) is not None:
			raise ValueError(_UNANSWERABLE_TURN)
		# What is left here is a SINGLE unusable reference, and it is refused for a different
		# reason than confusion: one call is confusable with nothing. A reference is also how a
		# result is FILED — `_prepare_resume` and both loops write `tool_call_id=call.id`, and a
		# tool message with no usable reference is one the structural check has always rejected.
		# So letting one through wrote a message into the stored transcript that made every later
		# turn in that conversation raise: the conversation ended, recoverable only by deleting
		# the stored row. Refused before anything is written, so nothing is lost but the one
		# unanswerable reply. Its own sentence, because the collision one is false for it — there
		# was one action, and the tool is as often ungated, so there was no approval either.
		if NO_CALL_REFERENCE in references:
			raise ValueError(_UNFILEABLE_CALL)
		message["tool_calls"] = [
			{
				"id": call.id,
				"type": "function",
				"function": {
					"name": call.name,
					"arguments": json.dumps(call.arguments),
				},
			}
			for call in response.tool_calls
		]
	return message


def _escaped(text: str) -> str:
	"""Text made safe to read: every character that could move the cursor is shown, never obeyed.

	The values in an approval question are chosen by the model. A newline in one would start a line
	of its own and could write a second, friendlier question underneath the real one; a right-to-left
	override would reorder the sentence around it while leaving every character in place. Both are
	ways to show a person a question other than the one being asked, so every control and format
	character is printed as an escape instead.

	The backslash and the quote are escaped too. Without that, `\\n` in the output could be either a
	real line break or those two characters, and a quote inside a value could close the pair holding
	it — the point of escaping is that the reader can tell exactly what the value was.

	The rule is a whitelist and has to stay one. It began as a list of the categories that looked
	dangerous — Cc and Cf — and that list missed U+2028 and U+2029, which `str.splitlines` and every
	layout engine treat as line breaks, so a value could still open a line of its own. Everything in
	a C* or Z* category is escaped now, the ordinary space excepted: unassigned code points, private
	use and lone surrogates included, so a later revision of Unicode cannot quietly add a new way
	through.
	"""
	out: list[str] = []
	for ch in text:
		if ch in '\\"':
			out.append("\\" + ch)
		elif ch == "\n":
			out.append("\\n")
		elif ch == "\r":
			out.append("\\r")
		elif ch == "\t":
			out.append("\\t")
		elif ch != " " and unicodedata.category(ch)[0] in "CZ":
			out.append(f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
		else:
			out.append(ch)
	return "".join(out)


def _quoted_argument(value: Any) -> str | None:
	"""One argument, ready to read: escaped, quoted, and never shortened.

	None when it cannot be shown whole — not a scalar, or longer than the cap once escaped. The
	caller then abandons the sentence entirely rather than show a shortened value, because a
	question holding part of a value reads exactly like one holding all of it, and the part left
	out is the part someone needed.
	"""
	if not isinstance(value, CONFIRM_SCALAR_TYPES):
		return None
	# Escaping only ever lengthens, so a value already over the cap is refused before the work of
	# escaping it: the value is chosen by the model and can be megabytes.
	if isinstance(value, str) and len(value) > CONFIRM_VALUE_LIMIT:
		return None
	shown = _escaped(value if isinstance(value, str) else json.dumps(value))
	if len(shown) > CONFIRM_VALUE_LIMIT:
		return None
	return f'"{shown}"'


def _render_confirm_template(template: str, arguments: dict[str, Any]) -> str | None:
	"""Fill an administrator's sentence in from this call's own arguments, or give up.

	This is a substitution, not an evaluation: `{name}` becomes the argument called `name` and
	nothing else happens, so there is nothing here for a value to be interpreted AS. Returns None
	whenever the sentence cannot be produced in full — a name the call did not supply, a value that
	is not a scalar or will not fit — and the caller falls back to the arguments as they are.

	Reads `template` and `arguments` and nothing else: no record, no session, no database, no disk.
	It cannot raise, because a badly written question must never be able to stop a person being
	asked.
	"""
	try:
		# The author's own line breaks are not the value's: the sentence is one line, so the
		# arguments printed beneath it can never be mistaken for part of it.
		sentence = " ".join(template.split())
		if not sentence:
			return None

		incomplete = False

		def substitute(match: re.Match[str]) -> str:
			nonlocal incomplete
			shown = _quoted_argument(arguments[match.group(1)]) if match.group(1) in arguments else None
			if shown is None:
				incomplete = True
				return ""
			return shown

		rendered = CONFIRM_PLACEHOLDER.sub(substitute, sentence)
		if incomplete or len(rendered) > CONFIRM_BODY_LIMIT:
			return None
		return rendered
	except Exception:
		return None


def _confirmation_question(call: ToolCall, tool: Tool) -> Question:
	"""Build the approval prompt shown to the user for a `requires_confirmation` tool call.

	The body is the tool's `confirm_prompt` when it has one — that is code, written and reviewed
	alongside the tool itself. Otherwise it is the record's plain-language sentence followed ALWAYS
	by the arguments that will execute, and the arguments alone when there is no sentence or it
	could not be filled in whole.

	The sentence never replaces the arguments. Its wording comes from an administrator, and a
	question reading "Read the invoices" above a call that deletes them must not be the only thing
	anyone sees. `confirm_prompt` is the exception, deliberately: it is code, written and reviewed
	with the tool rather than typed into a record, and `test_confirm_prompt_renders_plain_english_body`
	asserts that the argument shape does not appear beneath it.

	Wording only: what executes, and on which answer, is decided elsewhere and nothing here can
	reach it.
	"""
	body = tool.confirm_prompt(call.arguments) if tool.confirm_prompt else None
	if not body:
		# Built only where it is used. `json.dumps` can raise on an exotic argument, and this
		# function must not be the reason nobody is asked.
		dump = json.dumps(call.arguments, indent=2, default=str)
		sentence = (
			_render_confirm_template(tool.confirm_template, call.arguments) if tool.confirm_template else None
		)
		body = f"{sentence}\n\n{dump}" if sentence else dump

	title = _escaped(" ".join((tool.title or "").split()))
	# Joined, not interpolated. The body carries the model's own words and is never an argument to
	# a formatter, so there is nothing for a value shaped like `{0}` or `%s` to be read as.
	return Question(
		prompt=_("Approve {0}?").format(title or f"`{call.name}`") + "\n\n" + body,
		options=["Approve", "Deny"],
		allow_other=True,
	)


# The same escaping, under a name other modules may use. `_escaped` and every function that calls
# it stay byte-identical: a second escaper elsewhere in the codebase would be a second rule, and the
# first thing to drift. Text shown to a person, and text a person's answer depends on, is escaped
# here or nowhere.
escape_for_display = _escaped


def _approval_question_keys(asked: list[Any] | None) -> frozenset[str]:
	"""The keys of the questions in a pause that asked a person to APPROVE something.

	Read from the questions the pause raised, never from the runtime — the runtime is the thing
	that may have changed. An approval question is known by its options: every one the engine
	raises carries `CONFIRM_ANSWER_OPTIONS`, in that order, and nothing else in the engine does.

	Accepts `Question`s or the rows stored from them, because the caller that has this record is
	usually reading it back from storage. Anything it cannot read is simply not an approval
	question here: the branch this guards only ever withholds, so failing to recognise one costs
	today's behaviour and never an unasked execution.
	"""
	keys: set[str] = set()
	for question in asked or []:
		if isinstance(question, dict):
			key, options = question.get("key"), question.get("options")
		else:
			key, options = getattr(question, "key", None), getattr(question, "options", None)
		if (
			isinstance(key, str)
			and isinstance(options, list | tuple)
			and tuple(options) == CONFIRM_ANSWER_OPTIONS
		):
			keys.add(key)
	return frozenset(keys)


def _all_question_keys(asked: list[Any] | None) -> frozenset[str]:
	"""Every key in a pause's record, whatever kind of question raised it.

	`_approval_question_keys` filters to the ones that asked a person to APPROVE something, which
	is the wrong question for a call that was HELD BACK: a tool's own question is not an approval
	and its call is not deferred. Both are read from the record for the same reason — the runtime
	resuming a run is not always the one that paused it.
	"""
	keys: set[str] = set()
	for question in asked or []:
		key = question.get("key") if isinstance(question, dict) else getattr(question, "key", None)
		if isinstance(key, str):
			keys.add(key)
	return frozenset(keys)


def _not_executed(reason: str) -> str:
	"""Result for a pending call the engine will not act on, because the tool is gone or because
	the approval that was given no longer applies to it.

	Every part of this is a literal. The person's answer is deliberately absent: writing it here
	is the defect this exists to stop, since the model then reads its own tool call as having
	returned the word a person typed, and may report the action done when nothing ran.
	"""
	return json.dumps({"status": "not_executed", "reason": reason, "message": NOT_EXECUTED_MESSAGES[reason]})


def _withheld_confirmation() -> str:
	"""Result for a call the user approved inside a group that also held a denial.

	It says approved-and-not-run, which is not the same as denied and not the same as never
	asked. `user_answer` is a fixed literal, not the value from the answers map: nothing a
	caller supplies is echoed back into the model's context from here.
	"""
	return json.dumps(
		{
			"status": "not_executed",
			"message": "Nothing in this group ran. The user approved this action but denied another action in the same group.",
			"user_answer": "Approve",
		}
	)


def _has_denial(answers: dict[str, Any]) -> bool:
	"""A Deny answer halts the run — the user rejected an action, so stop rather than
	continue. Approve and free-text (redirect) answers let the run proceed."""
	return any(answer == "Deny" for answer in answers.values())


def _serialize_tool_result(result: Any) -> str:
	if isinstance(result, str):
		return result
	if result is None:
		return ""
	try:
		return json.dumps(result, default=str)
	except (TypeError, ValueError):
		return str(result)


def _accumulate_usage(total: dict[str, int], delta: dict[str, int]) -> None:
	for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
		total[key] += int(delta.get(key, 0) or 0)
