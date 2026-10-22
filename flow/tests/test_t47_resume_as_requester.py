# License: MIT. See LICENSE
"""An approval authorises; it does not lend the answerer's permissions.

Measured on a live site (22 Oct 2026): a user who may not create an Employee asked an agent to
create one. The approval card was answered by a System Manager, the tool ran as the System
Manager, and the Employee was created. These tests replay that with a record type the requester
cannot create (Role) and pin who the approved tool performs as.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.api import resume_run, start_run
from flow.lib.model import ChatResponse, Model, ToolCall
from flow.tests.test_ai_api import _agent_doc, _ensure_user, _final, _model_doc
from flow.tools.builtins import sync_builtin_tools

ROLE = "T47 Escalation Role"


def _create_role_call(call_id: str = "c1") -> ChatResponse:
	return ChatResponse(
		content=None,
		tool_calls=[ToolCall(id=call_id, name="create", arguments={"doctype": "Role", "records": [{"role_name": ROLE}]})],
		finish_reason="tool_calls",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


class TestResumeAsRequester(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		frappe.set_user("Administrator")
		model = frappe.get_doc(_model_doc(title="T47 Model")).insert()
		self.agent = frappe.get_doc(_agent_doc(model.name, title="T47 Agent", tools=[{"tool": "create"}])).insert()
		self.requester = _ensure_user("t47-requester@example.com")
		self.manager = _ensure_user("t47-manager@example.com")
		frappe.get_doc("User", self.manager).add_roles("System Manager")
		self.assertFalse(frappe.db.exists("Role", ROLE))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _pause_as(self, user: str) -> str:
		frappe.set_user(user)
		with patch.object(Model, "chat", return_value=_create_role_call()):
			payload = start_run("create the role", agent=self.agent.name)
		self.assertEqual(payload["status"], "Paused")
		return payload["name"]

	def _approve_as(self, user: str, run_name: str) -> dict:
		frappe.set_user(user)
		with patch.object(Model, "chat", return_value=_final("ok")):
			return resume_run(run_name, {"c1": "Approve"})

	def test_manager_approval_does_not_lend_the_managers_permissions(self):
		run_name = self._pause_as(self.requester)
		payload = self._approve_as(self.manager, run_name)

		self.assertEqual(payload["status"], "Completed")
		self.assertFalse(frappe.db.exists("Role", ROLE), "the approved tool ran as the approver")

	def test_the_answerer_is_signed_in_again_afterwards(self):
		run_name = self._pause_as(self.requester)
		self._approve_as(self.manager, run_name)

		self.assertEqual(frappe.session.user, self.manager)

	def test_a_requester_who_may_create_it_still_gets_it(self):
		run_name = self._pause_as(self.manager)
		payload = self._approve_as(self.manager, run_name)

		self.assertEqual(payload["status"], "Completed")
		self.assertTrue(frappe.db.exists("Role", ROLE))

	def test_a_disabled_requester_fails_closed(self):
		run_name = self._pause_as(self.requester)
		frappe.set_user("Administrator")
		frappe.db.set_value("User", self.requester, "enabled", 0)

		with self.assertRaises(frappe.PermissionError):
			self._approve_as(self.manager, run_name)
		self.assertFalse(frappe.db.exists("Role", ROLE))

	def test_nobody_else_continues_an_administrator_turn(self):
		run_name = self._pause_as("Administrator")

		with self.assertRaises(frappe.PermissionError):
			self._approve_as(self.manager, run_name)
		self.assertFalse(frappe.db.exists("Role", ROLE))
