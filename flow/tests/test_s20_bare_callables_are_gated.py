# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""S20 — a tool whose code says nothing about approval asks is gated.

`flow/lib/resolver.py` had a floor for one of its four resolution branches: an imported `Tool`
object's own `requires_confirmation` could turn a gate ON that the record had turned off (S16b D5).
The other two branches that actually build a runnable tool — a bare module-level callable, and a
Script body — passed no floor at all, so for them the record was the only say.

The record is not enough on three routes:
  - the box can simply be unticked (only the six shipped write builtins refuse that);
  - `db.set_value` writes the column behind `validate`'s back;
  - and a row inserted during an import gets no doctype default AT ALL —
    `frappe/model/document.py:1069-1071` returns early from `_set_defaults` when
    `frappe.flags.in_import` is set, which is the fixture / migrate path. Measured by AT2 and AT8,
    not assumed.

So the floor is computed at RESOLVE time from what the code says, where a record cannot reach it.
Turning a gate on is never refused; only taking one off that was never declared.

Every test runs as a named non-Administrator user. Flow Tool's only permission rule is the System
Manager one (`flow_tool.json:134-145`), and a test running as Administrator would pass even if that
rule were deleted.
"""

import frappe
from frappe.tests import IntegrationTestCase

from flow.lib.tool import Tool, tool

S20_TESTER = "s20-tester@example.com"


def a_plain_function(city: str, days: int = 1) -> str:
	"""A bare module-level callable: it says nothing whatever about approval."""
	return f"{city} for {days}"


A_CONSTANT = 42


@tool
def an_ungated_tool(city: str) -> str:
	"""A tool whose code declares that it does not need asking."""
	return city


@tool(requires_confirmation=True)
def a_gated_tool(city: str) -> str:
	"""A tool whose code declares that it does."""
	return city


class S20Base(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.enterClassContext(cls.enable_safe_exec())
		super().setUpClass()

	def setUp(self):
		if not frappe.db.exists("User", S20_TESTER):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": S20_TESTER,
					"first_name": "S20",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
			user.add_roles("System Manager")
		frappe.set_user(S20_TESTER)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _imported(self, slug: str, path: str, **overrides) -> dict:
		doc = {
			"doctype": "Flow Tool",
			"slug": slug,
			"title": slug.replace("_", " ").title(),
			"type": "Imported",
			"description": "A tool from an installed app.",
			"import_path": path,
		}
		doc.update(overrides)
		return doc

	def _script(self, slug: str, **overrides) -> dict:
		doc = {
			"doctype": "Flow Tool",
			"slug": slug,
			"title": slug.replace("_", " ").title(),
			"type": "Script",
			"description": "A hand-written tool.",
			"code": "def main(x: str):\n\treturn x\n",
		}
		doc.update(overrides)
		return doc


_HERE = "flow.tests.test_s20_bare_callables_are_gated"


class TestCodeWithNoVoiceIsGated(S20Base):
	def test_a_row_pointing_at_a_bare_function_is_gated_even_with_the_box_unticked(self):
		"""AT1. The record is not the only say for code that says nothing about itself.

		`frappe.get_attr` imports any dotted path a System Manager can type — there is no
		allowlist (measured: no ALLOWED_IMPORT / IMPORT_ALLOWLIST anywhere in the engine; the
		positive control for that search shape is IMPORT_PATH_PATTERN, which does hit) — so this
		is the widest surface in the registry and the one branch where the code has no voice.
		"""
		doc = frappe.get_doc(
			self._imported("s20_helper", f"{_HERE}.a_plain_function", requires_confirmation=0)
		).insert(ignore_permissions=True)

		self.assertFalse(doc.requires_confirmation, "the record really does say no")
		self.assertIs(doc.to_tool().requires_confirmation, True)

	def test_the_import_default_does_not_save_us(self):
		"""AT2. `frappe.flags.in_import` skips `_set_defaults` entirely
		(`frappe/model/document.py:1069-1071`), so a fixture row never sees the doctype's
		`"default": "1"`. This measures that, rather than citing it.

		The flag is restored to False in a `finally` — never `del`, it is process-global and every
		later test in this process would lose its doctype defaults — and the control below is what
		proves the restore actually happened.
		"""
		try:
			frappe.flags.in_import = True
			imported = frappe.get_doc(self._imported("s20_fixture", f"{_HERE}.a_plain_function")).insert(
				ignore_permissions=True
			)
		finally:
			frappe.flags.in_import = False

		self.assertFalse(
			frappe.db.get_value("Flow Tool", "s20_fixture", "requires_confirmation"),
			"the doctype default was expected NOT to apply on the import path",
		)
		self.assertIs(imported.to_tool().requires_confirmation, True)

		# The control: with the flag restored, a row with the field omitted DOES get the default.
		# Without this the test above passes just as well against a `finally` that never ran.
		ordinary = frappe.get_doc(self._imported("s20_desk", f"{_HERE}.a_plain_function")).insert(
			ignore_permissions=True
		)
		self.assertTrue(ordinary.requires_confirmation)

	def test_a_decorated_tool_keeps_its_own_answer_in_both_directions(self):
		"""AT3. The control that stops this fix gating every read-only tool in the registry.

		A `Tool` object HAS a voice and it is final in both directions: an ungated one stays
		ungated however the record was written, and a gated one cannot be un-gated by it (S16b D5,
		unchanged).
		"""
		ungated = frappe.get_doc(
			self._imported("s20_ungated", f"{_HERE}.an_ungated_tool", requires_confirmation=0)
		).insert(ignore_permissions=True)
		gated = frappe.get_doc(
			self._imported("s20_gated", f"{_HERE}.a_gated_tool", requires_confirmation=0)
		).insert(ignore_permissions=True)

		self.assertIs(ungated.to_tool().requires_confirmation, False)
		self.assertIs(gated.to_tool().requires_confirmation, True)

	def test_a_read_only_builtin_row_is_still_ungated(self):
		"""AT4. The shipped rows resolve through the `Tool` branch, so none of them moves.

		**Measured, and the spec was wrong about what this catches.** The spec named the wrong
		repair as "move the floor into `_build_tool`'s default"; that mutation was run and this
		module stayed GREEN. It has to: after D1 and D2 every production call site passes the
		argument explicitly, so the default is unreachable from the engine and moving the floor
		there changes no behaviour at all. Recorded rather than left as a comment.

		What this test DOES catch, measured: applying the floor to the `isinstance(obj, Tool)`
		branch — the repair that would gate every read-only builtin in the registry. That reddens
		this test and AT3, and nothing else in the module.
		"""
		from flow.tools.builtins import sync_builtin_tools

		frappe.set_user("Administrator")
		sync_builtin_tools()
		frappe.set_user(S20_TESTER)
		row = frappe.get_doc("Flow Tool", "read")

		self.assertEqual(row.import_path, "flow.tools.builtins.read")
		self.assertIs(row.to_tool().requires_confirmation, False)

	def test_the_record_can_still_turn_a_gate_on(self):
		"""AT5. One-directional, like every other gate rule in this engine.

		**Measured:** the spec said `or` -> `and` at the gate computation reddens this test. It
		does not — with the box ticked AND the floor on, `and` is still True. That mutation was
		run and reddened five tests in this module (AT1, AT2, AT3, AT7, AT8), AT3's gated-`Tool`
		half being the one that speaks to this direction.

		Removing `bool(doc.requires_confirmation)` from the `or` was then run too, and THAT was
		green as well while this test used only a bare callable: the floor already makes such a
		row gated, so the record's half decided nothing and the test passed for the wrong reason.
		The case that can fail is the one where the code says False and the record says yes — an
		ungated `Tool` object — so it is asserted here as well.
		"""
		bare = frappe.get_doc(
			self._imported("s20_on", f"{_HERE}.a_plain_function", requires_confirmation=1)
		).insert(ignore_permissions=True)
		over_the_code = frappe.get_doc(
			self._imported("s20_on_tool", f"{_HERE}.an_ungated_tool", requires_confirmation=1)
		).insert(ignore_permissions=True)

		self.assertIs(bare.to_tool().requires_confirmation, True)
		self.assertIs(
			over_the_code.to_tool().requires_confirmation,
			True,
			"a record asking for a gate on a tool whose code does not want one was ignored",
		)

	def test_a_non_callable_import_path_is_still_refused(self):
		"""AT6. The refusal at the end of `_resolve_module` must stay reachable: a floor added to
		a branch widened to `obj is not None` would swallow it."""
		doc = frappe.get_doc(self._imported("s20_constant", f"{_HERE}.A_CONSTANT")).insert(
			ignore_permissions=True
		)

		with self.assertRaisesRegex(frappe.ValidationError, "not a Tool or callable"):
			doc.to_tool()

	def test_a_script_tool_imported_as_a_fixture_is_still_gated(self):
		"""AT8. The worse half, and the one v1 of this spec left open.

		A Script tool is arbitrary code in a sandbox that a RECORD carries, and it declares nothing
		about itself at all. The doctype default gates one created at a desk; a row shipped as a
		fixture never sees a default, and that is the row nobody reviews as a tool definition.
		"""
		try:
			frappe.flags.in_import = True
			doc = frappe.get_doc(self._script("s20_script")).insert(ignore_permissions=True)
		finally:
			frappe.flags.in_import = False

		self.assertFalse(
			frappe.db.get_value("Flow Tool", "s20_script", "requires_confirmation"),
			"the doctype default was expected NOT to apply on the import path",
		)
		self.assertIs(doc.to_tool().requires_confirmation, True)

	def test_a_script_tool_created_at_a_desk_is_gated_too(self):
		"""The control for AT8: the default and the floor agree, so the fixture case above is the
		only thing the floor changes for a Script tool."""
		doc = frappe.get_doc(self._script("s20_script_desk")).insert(ignore_permissions=True)

		self.assertTrue(doc.requires_confirmation)
		self.assertIs(doc.to_tool().requires_confirmation, True)


class TestTheGateNamesNothingItShouldNot(S20Base):
	"""AT7 — CLAUDE.md rule 3, over the question a person is actually shown for such a tool."""

	FORBIDDEN = ("frappe", "erpnext", "mariadb", "openai", "anthropic", "gpt-", "claude")

	def _assert_clean(self, text: str) -> None:
		for word in self.FORBIDDEN:
			self.assertNotIn(word, text.lower(), f"{word!r} appears in text a person reads")

	def test_the_approval_question_for_such_a_tool_names_no_platform_or_vendor(self):
		from flow.lib.agent import _confirmation_question
		from flow.lib.model import ToolCall

		doc = frappe.get_doc(
			self._imported("s20_q", f"{_HERE}.a_plain_function", requires_confirmation=0)
		).insert(ignore_permissions=True)
		runtime = doc.to_tool()
		self.assertIs(runtime.requires_confirmation, True)

		question = _confirmation_question(
			ToolCall(id="c1", name=runtime.name, arguments={"city": "Paris"}), runtime
		)

		self._assert_clean(question.prompt)
		self._assert_clean(" ".join(question.options))

	def test_control_the_check_itself_can_go_red(self):
		"""The control is CODE, not prose: the same helper over a string that does contain one of
		the words must raise, or the test above proves nothing."""
		with self.assertRaises(AssertionError):
			self._assert_clean("this sentence mentions Frappe by name")
