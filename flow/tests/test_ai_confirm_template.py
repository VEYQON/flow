# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt
"""How an approval question is worded.

Nothing here touches what executes, or on which answer. That is `TestAgentConfirmation` in
test_ai_agent.py, which stays green and unmodified.
"""

import frappe
from frappe.tests import IntegrationTestCase


class TestConfirmationQuestionWording(IntegrationTestCase):
	"""What the approval question SAYS. Nothing here touches what executes, on which answer —
	that is `TestAgentConfirmation`, which must stay green and unmodified."""

	def _call(self, arguments=None, name="post_invoice"):
		from flow.lib.model import ToolCall

		return ToolCall(id="c1", name=name, arguments=arguments or {"amount": 4200, "customer": "Acme"})

	def _tool(self, **overrides):
		from flow.lib.tool import Tool

		defaults = dict(
			name="post_invoice",
			description="Post an invoice.",
			parameters={"type": "object", "properties": {}},
			func=lambda **kw: "ok",
			requires_confirmation=True,
		)
		defaults.update(overrides)
		return Tool(**defaults)

	def _question(self, **overrides):
		from flow.lib.agent import _confirmation_question

		return _confirmation_question(self._call(), self._tool(**overrides))

	# --- the three-way precedence ------------------------------------------------------------
	def test_a_record_template_is_rendered_from_the_calls_arguments(self):
		q = self._question(confirm_template="Post an invoice for {{ amount }} to {{ customer }}.")
		self.assertIn("Post an invoice for 4200 to Acme.", q.prompt)
		self.assertNotIn("{", q.prompt.split("?", 1)[1])  # no raw JSON, no unrendered braces

	def test_the_code_callable_wins_over_the_record_template(self):
		q = self._question(
			confirm_prompt=lambda args: "FROM THE CALLABLE",
			confirm_template="FROM THE TEMPLATE",
		)
		self.assertIn("FROM THE CALLABLE", q.prompt)
		self.assertNotIn("FROM THE TEMPLATE", q.prompt)

	def test_with_neither_the_arguments_are_shown_exactly_as_before(self):
		import json

		q = self._question()
		self.assertIn(json.dumps({"amount": 4200, "customer": "Acme"}, indent=2, default=str), q.prompt)

	# --- rendering can never take the question away ------------------------------------------
	def test_a_template_that_raises_falls_back_to_the_arguments(self):
		q = self._question(confirm_template="{{ amount / 0 }}")
		self.assertIn('"amount": 4200', q.prompt)
		self.assertEqual(q.options, ["Approve", "Deny"])

	def test_a_template_naming_an_argument_the_call_did_not_supply_falls_back(self):
		"""A blank where a value belongs is worse than the raw arguments — the person would be
		approving a sentence with a hole in it."""
		q = self._question(confirm_template="Post {{ amount }} to {{ nobody_supplied_this }}.")
		self.assertIn('"amount": 4200', q.prompt)
		self.assertNotIn("Post 4200 to .", q.prompt)

	def test_a_syntactically_broken_template_falls_back(self):
		q = self._question(confirm_template="Post {{ amount for }}")
		self.assertIn('"amount": 4200', q.prompt)

	def test_an_empty_render_falls_back(self):
		q = self._question(confirm_template="{# just a comment #}")
		self.assertIn('"amount": 4200', q.prompt)

	# --- it must not execute, escape, or read the disk ----------------------------------------
	def test_a_template_cannot_reach_through_an_attribute_to_escape(self):
		"""Attribute traversal is refused outright, so nothing about the running process can be
		reflected back into the question. The person sees the arguments instead."""
		q = self._question(confirm_template="{{ amount.__class__.__mro__ }}")
		self.assertIn('"amount": 4200', q.prompt)
		for leak in ("class", "mro", "builtins", "object"):
			self.assertNotIn(leak, q.prompt.lower())

	def test_a_template_cannot_read_a_file(self):
		q = self._question(confirm_template="{{ open('/etc/hostname').read() }}")
		self.assertIn('"amount": 4200', q.prompt)
		self.assertEqual(q.options, ["Approve", "Deny"])

	def test_a_path_shaped_template_is_rendered_as_text_not_loaded_from_disk(self):
		"""The renderer treats a single-line string whose last dotted segment looks like a file
		extension as a PATH and reads it off the disk. Pinned off, so this renders."""
		q = self._question(confirm_template="Delete the report for {{ customer }}.txt")
		self.assertIn("Delete the report for Acme.txt", q.prompt)

	def test_an_argument_that_looks_like_a_template_is_shown_not_evaluated(self):
		"""Arguments come from the model. They are values, never template source."""
		from flow.lib.agent import _confirmation_question
		from flow.lib.model import ToolCall

		call = ToolCall(id="c1", name="post_invoice", arguments={"customer": "{{ 7 * 7 }}"})
		q = _confirmation_question(call, self._tool(confirm_template="Bill {{ customer }}."))
		self.assertNotIn("49", q.prompt)

	# --- the first line ------------------------------------------------------------------------
	def test_the_first_line_uses_the_human_title_when_there_is_one(self):
		q = self._question(title="Post Invoice", confirm_template="Post {{ amount }}.")
		self.assertTrue(q.prompt.startswith("Approve Post Invoice?"))
		self.assertNotIn("post_invoice", q.prompt)

	def test_the_first_line_falls_back_to_the_internal_name(self):
		q = self._question()
		self.assertTrue(q.prompt.startswith("Approve `post_invoice`?"))

	# --- nothing about the decision moved -------------------------------------------------------
	def test_the_options_are_unchanged_in_every_case(self):
		for overrides in (
			{},
			{"confirm_template": "Post {{ amount }}."},
			{"confirm_template": "{{ broken"},
			{"confirm_prompt": lambda a: "x"},
			{"title": "Post Invoice"},
		):
			with self.subTest(overrides=sorted(overrides)):
				q = self._question(**overrides)
				self.assertEqual(q.options, ["Approve", "Deny"])
				self.assertTrue(q.allow_other)


class TestTheRecordCarriesTheWordingToTheRuntime(IntegrationTestCase):
	"""A tool defined as a record could never have a plain-language approval question: the
	attribute existed only in code. This is the path that closes that gap."""

	def tearDown(self):
		frappe.db.rollback()

	def _record(self, **overrides):
		doc = {
			"doctype": "Flow Tool",
			"title": "Post Invoice",
			"slug": f"post_invoice_{frappe.generate_hash(length=6)}",
			"type": "Imported",
			"description": "Post an invoice.",
			"import_path": "flow.tools.builtins.find_doctypes",
			"requires_confirmation": 1,
		}
		doc.update(overrides)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def test_the_field_persists_and_is_optional(self):
		with_template = self._record(confirm_template="Post {{ amount }}.")
		without = self._record()
		self.assertEqual(
			frappe.db.get_value("Flow Tool", with_template.name, "confirm_template"), "Post {{ amount }}."
		)
		self.assertFalse(frappe.db.get_value("Flow Tool", without.name, "confirm_template"))

	def test_the_runtime_tool_carries_the_title_and_the_template(self):
		runtime = self._record(confirm_template="Post {{ amount }}.").to_tool()
		self.assertEqual(runtime.title, "Post Invoice")
		self.assertEqual(runtime.confirm_template, "Post {{ amount }}.")
		self.assertTrue(runtime.requires_confirmation)

	def test_a_tool_defined_in_code_carries_neither(self):
		from flow.lib.tool import tool

		@tool(requires_confirmation=True)
		def write_file(path: str) -> str:
			"""Write a file."""
			return path

		self.assertIsNone(write_file.title)
		self.assertIsNone(write_file.confirm_template)

	def test_a_script_tool_can_have_one_too(self):
		"""Script tools could never carry a code callable at all, so they were stuck with the
		raw arguments no matter what."""
		runtime = self._record(
			type="Script",
			import_path=None,
			code="def main(amount: float) -> str:\n\treturn str(amount)\n",
			confirm_template="Post {{ amount }}.",
		).to_tool()
		self.assertEqual(runtime.confirm_template, "Post {{ amount }}.")
		self.assertEqual(runtime.title, "Post Invoice")


class TestTheApprovalGateIsUnmovedByTheWording(IntegrationTestCase):
	"""The wording changed; the decision did not. Only the exact "Approve" executes."""

	def tearDown(self):
		frappe.db.rollback()

	def _agent_with_templated_tool(self):
		from flow.lib.agent import Agent
		from flow.lib.tool import Tool

		executed: list = []

		def post(amount: float) -> str:
			executed.append(amount)
			return f"posted {amount}"

		templated = Tool(
			name="post_invoice",
			description="Post an invoice.",
			parameters={
				"type": "object",
				"properties": {"amount": {"type": "number"}},
				"additionalProperties": False,
				"required": ["amount"],
			},
			func=post,
			requires_confirmation=True,
			title="Post Invoice",
			confirm_template="Post an invoice for {{ amount }}.",
		)
		return Agent, templated, executed

	def _model(self, responses):
		from flow.lib.model import ChatResponse, ToolCall

		class Scripted:
			model_id = "openai/gpt-4o-mini"

			def __init__(self, rs):
				self._rs = list(rs)

			def chat(self, messages, tools=None, *, stream=False):
				return self._rs.pop(0)

		pause = ChatResponse(
			content=None,
			tool_calls=[ToolCall(id="c1", name="post_invoice", arguments={"amount": 4200.0})],
			finish_reason="tool_calls",
			usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
		)
		done = ChatResponse(
			content="done",
			finish_reason="stop",
			usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
		)
		return Scripted([pause] + ([done] if responses == 2 else []))

	def test_the_templated_question_is_asked_and_the_tool_has_not_run(self):
		Agent, templated, executed = self._agent_with_templated_tool()
		agent = Agent(model=self._model(1), tools=[templated])
		result = agent.run("post it")

		self.assertTrue(result.paused)
		self.assertEqual(result.questions[0].options, ["Approve", "Deny"])
		self.assertIn("Post an invoice for 4200.0.", result.questions[0].prompt)
		self.assertTrue(result.questions[0].prompt.startswith("Approve Post Invoice?"))
		self.assertEqual(executed, [])

	def test_only_the_exact_approve_executes_a_templated_tool(self):
		Agent, templated, executed = self._agent_with_templated_tool()
		agent = Agent(model=self._model(2), tools=[templated])
		paused = agent.run("post it")
		agent.resume(paused.messages, {"c1": "Approve"})
		self.assertEqual(executed, [4200.0])

	def test_deny_does_not_execute_a_templated_tool(self):
		Agent, templated, executed = self._agent_with_templated_tool()
		agent = Agent(model=self._model(1), tools=[templated])
		paused = agent.run("post it")
		agent.resume(paused.messages, {"c1": "Deny"})
		self.assertEqual(executed, [])

	def test_free_text_does_not_execute_a_templated_tool(self):
		Agent, templated, executed = self._agent_with_templated_tool()
		agent = Agent(model=self._model(2), tools=[templated])
		paused = agent.run("post it")
		agent.resume(paused.messages, {"c1": "no, make it 1"})
		self.assertEqual(executed, [])
