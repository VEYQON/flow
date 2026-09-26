# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""No platform, vendor or product name appears in text the model reads.

CLAUDE.md rule 3, made a gate instead of a habit: *"Anything the model reads (system/context blocks,
tool descriptions, error strings returned to it) must not say Frappe, Flow, ERPNext, MariaDB, OpenAI,
or any vendor/model name. The product requirement is absolute."* Until this module existed the rule
was enforced by whoever happened to read the diff, and the instructions shipped with the assistant
agent opened by naming the platform three times in one sentence.

WHAT IS IN SCOPE, and it is model-facing text only. Every registered builtin's `name` and
`description` — a description IS the tool's documentation as the model receives it, straight into the
request — the whole schema payload each tool sends, the body of every approval question those tools
compose, and the instructions shipped with the assistant agent. Human-facing Desk labels are
deliberately NOT in scope: a person reading their own admin interface is entitled to see what it is.

THE BAN IS ON THE NAME, AND THE SCAN IS THEREFORE CASE-SENSITIVE. That is not a loophole, it is the
distinction the requirement rests on. `Frappe` and `DocType` are a company and a product concept, and
prose containing them tells the model what it is running on. `frappe` and `doctype` are an object the
sandbox puts in scope and a parameter the model has to fill in: the model must type them for a tool
call to work at all, and they are not the engine describing its platform. So the capitalised forms are
banned outright and the lowercase identifiers are PINNED instead — see
`TestTheLowercaseIdentifiersAreExactlyWhereTheyWere`, which fails if the set of surfaces carrying one
grows. Pinning the residual is stronger than allowlisting it: an allowlist excuses a new occurrence,
a pin reports it.

THE MATCHER IS PROVEN, not assumed. `TestTheMatcherWorks` plants each banned word into the real text
in the real form the scan sees, and asserts the scan reports it. A zero-hits search is only a finding
when a positive control in the identical form hits (CLAUDE.md).
"""

from __future__ import annotations

import json
import re

from frappe.tests import IntegrationTestCase

from flow.assistant.assistant import ASSISTANT_INSTRUCTIONS
from flow.tools.builtins import BUILTIN_TOOLS

# The names, listed rather than derived, so the list is reviewable. Case-SENSITIVE and on a word
# boundary; see the module docstring for why the case carries the meaning.
BANNED_NAMES = (
	"Frappe",
	"DocType",
	"DocTypes",
	"ERPNext",
	"MariaDB",
	"OpenAI",
	"Anthropic",
	"Claude",
	"GPT",
	"Gemini",
	"Llama",
	"Mistral",
	"Ollama",
)
# The product's own record-type names. Banned as PROSE for the same reason: "create a Flow Agent row"
# tells the model the product's name. Matched as a phrase, because the bare word "Flow" also appears
# in ordinary English ("workflow" is caught by the boundary, deliberately not banned).
BANNED_PRODUCT_PHRASES = (
	"Flow Agent",
	"Flow Tool",
	"Flow Trigger",
	"Flow Knowledge",
	"Flow Model",
	"FLOW TOOL",
)

# The lowercase identifiers the model must emit verbatim, and the reason each cannot be removed here.
# NOT allowlisted — pinned, by the class at the bottom of this module.
#   `frappe`  the sandbox namespace. The code tool's description has to name the object the model
#             types; code it writes fails without it. Removing it needs an alias layer in
#             `flow/utils/safe_exec.py` and a rewrite of every example. OPEN, own spec.
#   `doctype` a tool parameter name and a stored tool slug (`find_doctypes`). Renaming either is a
#             data migration across every installed site plus every eval and test. OPEN, own spec.
PINNED_IDENTIFIERS = ("frappe", "doctype")

# One capitalised literal that cannot be neutralised either: `DocType` is a STORED Select option
# value on the knowledge-source record (`flow_knowledge_source.json`: `"options": "Text\nFile\nURL\nDocType"`),
# so the instructions have to name it for the model to fill the field in, and existing rows carry the
# value. Changing it is a data migration. Stripped from the instructions scan ONLY, as this one exact
# phrase, and pinned by `test_the_one_stored_option_value_is_the_only_capitalised_exception` — which
# also proves the strip is what is hiding it, so the exception cannot quietly widen.
STORED_OPTION_PHRASE = "Text/File/URL/DocType"

# The exact surfaces that carry a pinned identifier today. A new one is a failure, not an exception.
PINNED_SURFACES: dict[str, tuple[str, ...]] = {
	"frappe": ("execute",),
	# `execute` is not here: its description reaches the model with `find_doctypes` in it, and the
	# word-boundary match does not fire inside a slug — which is the correct answer, since the slug is
	# the thing the model types.
	"doctype": ("describe", "read", "create", "update", "delete", "run_action"),
}


def _scan(text: str) -> list[str]:
	"""Every banned name and product phrase in `text`. Case-sensitive, word-bounded."""
	hits = {w for w in BANNED_NAMES if re.search(rf"\b{re.escape(w)}\b", text)}
	hits |= {p for p in BANNED_PRODUCT_PHRASES if p in text}
	return sorted(hits)


def _model_facing(tool) -> str:
	"""Everything about one tool that reaches the model: the whole schema payload it is sent."""
	return json.dumps(tool.to_dict(), ensure_ascii=False)


def _confirm_body(tool) -> str:
	"""The approval question this tool composes, for a call shaped the way the model makes them."""
	if not tool.confirm_prompt:
		return ""
	return tool.confirm_prompt(
		{
			"doctype": "ToDo",
			"names": ["TODO-0001"],
			"records": [{"description": "x"}],
			"values": {"status": "Closed"},
			"action": "submit",
			"code": "result = 1",
			"description": "Count the open items",
			"content": "a note",
			"scope": "user",
		}
	)


class TestTheMatcherWorks(IntegrationTestCase):
	"""The positive control. Every assertion below it is a zero-hits search, and a zero-hits search
	proves nothing until the same search in the same form is shown to hit."""

	def test_the_matcher_finds_every_banned_name_planted_in_a_sentence(self):
		for word in BANNED_NAMES + BANNED_PRODUCT_PHRASES:
			with self.subTest(word=word):
				self.assertIn(word, _scan(f"Use the {word} interface to list records."))

	def test_the_matcher_finds_a_name_planted_in_the_real_instructions(self):
		"""Planted into the real text, in the real form the scan sees it — not into a toy string."""
		planted = ASSISTANT_INSTRUCTIONS.replace(STORED_OPTION_PHRASE, " ") + "\nThis runs on ERPNext."
		self.assertEqual(_scan(planted), ["ERPNext"])

	def test_the_matcher_finds_a_name_planted_in_a_real_tool_description(self):
		planted = _model_facing(BUILTIN_TOOLS[0]).replace("record", "DocType", 1)
		self.assertEqual(_scan(planted), ["DocType"])

	def test_the_matcher_does_not_report_the_lowercase_identifiers(self):
		"""The distinction the whole module rests on, asserted rather than assumed."""
		self.assertEqual(_scan("Call frappe.get_list with a doctype and find_doctypes."), [])

	def test_the_matcher_does_not_report_an_ordinary_english_word(self):
		"""`workflow` contains `flow`. The product phrases are phrases for this reason."""
		self.assertEqual(_scan("Move the record through its workflow."), [])


class TestNoBuiltinNamesThePlatform(IntegrationTestCase):
	def test_no_tool_slug_names_the_platform(self):
		for tool in BUILTIN_TOOLS:
			with self.subTest(tool=tool.name):
				self.assertEqual(_scan(tool.name), [])

	def test_no_tool_description_names_the_platform(self):
		for tool in BUILTIN_TOOLS:
			with self.subTest(tool=tool.name):
				self.assertEqual(
					_scan(tool.description),
					[],
					f"{tool.name}'s description names the platform — it goes to the model verbatim",
				)

	def test_nothing_in_the_schema_the_model_receives_names_the_platform(self):
		"""The whole payload, not just the description: a title or a parameter description added
		later would otherwise slip through."""
		for tool in BUILTIN_TOOLS:
			with self.subTest(tool=tool.name):
				self.assertEqual(_scan(_model_facing(tool)), [])

	def test_no_approval_question_names_the_platform(self):
		"""A confirmation body is read by the approver AND kept in the transcript the model is
		given back, so it is model-facing text twice over."""
		for tool in BUILTIN_TOOLS:
			with self.subTest(tool=tool.name):
				self.assertEqual(_scan(_confirm_body(tool)), [])


class TestTheShippedInstructionsNameNoPlatform(IntegrationTestCase):
	def test_the_assistant_instructions_name_no_platform(self):
		self.assertEqual(
			_scan(ASSISTANT_INSTRUCTIONS.replace(STORED_OPTION_PHRASE, " ")),
			[],
			"the instructions shipped with the assistant agent name the platform",
		)

	def test_the_one_stored_option_value_is_the_only_capitalised_exception(self):
		"""The exception, pinned and proved. It appears exactly once, and the scan DOES report it
		when the strip is not applied — so this test is what is holding the exception open, and
		widening it means editing this test."""
		self.assertEqual(ASSISTANT_INSTRUCTIONS.count(STORED_OPTION_PHRASE), 1)
		self.assertEqual(_scan(ASSISTANT_INSTRUCTIONS), ["DocType"])

	def test_the_instructions_still_say_what_they_are_for(self):
		"""The control against passing this by deleting the guidance. Neutral wording, same job."""
		for phrase in ("record type", "Discover", "find_doctypes", "run_action", "knowledge base"):
			with self.subTest(phrase=phrase):
				self.assertIn(phrase, ASSISTANT_INSTRUCTIONS)


class TestTheLowercaseIdentifiersAreExactlyWhereTheyWere(IntegrationTestCase):
	"""The residual exposure, pinned rather than excused.

	`frappe` and `doctype` are identifiers the model must type, so they cannot be removed without an
	alias layer and a data migration. Both are recorded OPEN. What this class stops is the exposure
	GROWING: a new tool whose description names the sandbox object, or a new prose mention, fails
	here and has to be argued for.
	"""

	def test_only_the_expected_tools_carry_a_pinned_identifier(self):
		for word, expected in PINNED_SURFACES.items():
			with self.subTest(word=word):
				carrying = tuple(
					t.name
					for t in BUILTIN_TOOLS
					if re.search(rf"\b{word}", _model_facing(t)) or re.search(rf"\b{word}", _confirm_body(t))
				)
				self.assertEqual(
					carrying,
					expected,
					f"the set of tools naming {word!r} to the model has changed — argue for it or remove it",
				)

	def test_the_instructions_carry_only_the_identifiers_they_must(self):
		"""`frappe` must no longer appear in the instructions at all: the sandbox paragraph names
		the tool that documents it instead. `doctype` stays, as an argument the model fills in."""
		self.assertNotIn("frappe", ASSISTANT_INSTRUCTIONS)
		self.assertIn("doctype", ASSISTANT_INSTRUCTIONS)
