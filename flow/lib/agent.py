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
	):
		if max_iterations < 1:
			raise ValueError("max_iterations must be at least 1")

		# Autonomous runs (triggers) auto-run confirmation tools; nobody is there to approve.
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
		for call, content in resolved:
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
	) -> tuple[list[dict[str, Any]], list[tuple[ToolCall, str]]]:
		"""Append a tool result for each pending call. Returns the new messages plus the
		(call, content) pairs resolved, so a streaming resume can replay them as events.

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
		_validate_messages(messages)
		messages = list(messages)
		pending = self._pending_calls(messages)
		if not pending:
			raise ValueError("No questions awaiting an answer in the provided messages")

		denied_group = _has_denial(answers)
		approval_keys = _approval_question_keys(asked)
		have_record = bool(asked)
		resolved: list[tuple[ToolCall, str]] = []
		for call in pending:
			answer = answers.get(call.id)
			tool = self._tools_by_name.get(call.name)
			# What the person was asked. With no record of the pause there is nothing to read it
			# from, so the tool's own gate stands in and the two can never disagree — which is
			# exactly the behaviour that existed before any of this was recorded.
			asked_to_approve = (
				call.id in approval_keys if have_record else bool(tool and tool.requires_confirmation)
			)
			if tool is None:
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
			else:
				content = _serialize_tool_result(answer)
			messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
			resolved.append((call, content))
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
			for call in response.tool_calls:
				result = self._invoke(call)
				if isinstance(result, Question):
					result.key = call.id
					questions.append(result)
					continue

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
			# Tool calls are announced mid-stream (ToolCallBegin) so the UI shows the tool the moment
			# the model starts it, before its arguments finish streaming.
			try:
				while True:
					item = next(chunks)
					if isinstance(item, ToolCallBegin):
						yield ToolStarted(id=item.id, name=item.name, arguments={})
					else:
						yield TextChunk(text=item)
			except StopIteration as e:
				response = e.value
			_accumulate_usage(usage_total, response.usage)
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
			for call in response.tool_calls:
				# Re-announce with the full arguments now that they've finished streaming, before the
				# tool runs — so the UI shows the arguments during execution, not only with the result.
				yield ToolStarted(id=call.id, name=call.name, arguments=call.arguments)
				result = self._invoke(call)
				if isinstance(result, Question):
					result.key = call.id
					questions.append(result)
					yield ToolEnded(id=call.id, name=call.name, result="")
					continue

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
		_validate_messages(input, refuse_indistinguishable_calls=False)
		return list(input)

	def _invoke(self, call: ToolCall) -> Any:
		"""Run a tool and return its raw result. A Question (returned or synthesized for
		`requires_confirmation` tools) signals a pause."""
		if call.error:
			return json.dumps({"error": call.error})
		tool = self._tools_by_name.get(call.name)
		if tool is None:
			return json.dumps({"error": f"Unknown tool: {call.name!r}"})
		if tool.requires_confirmation and not self.auto_approve:
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
NO_CALL_REFERENCE = "<none>"

# A plain literal, deliberately, like every other module-level message in this file. `_()` here
# would run at IMPORT time and freeze the translation for the whole worker in whatever language
# happened to be current then — and it would mean the vendor-word check over this string only ever
# inspects the English one, which is the single thing that check exists to prevent.
_UNANSWERABLE_TURN = (
	"Two actions in one reply could not be told apart, so one approval would have answered both. "
	"Nothing was carried out."
)


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
		reference = id if isinstance(id, str) and id else NO_CALL_REFERENCE
		if reference in seen:
			return True, reference
		seen.add(reference)
	return None


def _validate_messages(messages: Any, *, refuse_indistinguishable_calls: bool = True) -> None:
	"""Structural checks on a message list, plus — unless asked not to — the refusal of an
	assistant turn whose calls cannot be told apart.

	`refuse_indistinguishable_calls` defaults to True so a new call site is guarded unless it
	says otherwise: the only caller that opts out is `_build_initial_messages`, and it says why.
	"""
	if not isinstance(messages, list):
		raise TypeError(f"input must be a str or list of message dicts, got {type(messages).__name__}")
	for i, message in enumerate(messages):
		if not isinstance(message, dict):
			raise TypeError(f"messages[{i}] must be a dict, got {type(message).__name__}")
		role = message.get("role")
		if role not in VALID_ROLES:
			raise ValueError(f"messages[{i}].role must be one of {sorted(VALID_ROLES)}, got {role!r}")
		if role == "tool" and not message.get("tool_call_id"):
			raise ValueError(f"messages[{i}] is a tool message but has no tool_call_id")
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
		if _indistinguishable_tool_call(call.id for call in response.tool_calls) is not None:
			# One question cannot address two actions: both would carry the same key, one answer
			# would resolve both, and the tool would run twice on one approval. Refused HERE,
			# before the message is returned to be appended and therefore before anything is
			# invoked — both loops build this message before they touch `response.tool_calls`.
			#
			# `is not None`, never `if collided:` — the no-reference case is a real collision and
			# its reference is the falsy one. The predicate returns a tuple so this cannot be
			# "simplified" into a bug.
			raise ValueError(_UNANSWERABLE_TURN)
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
