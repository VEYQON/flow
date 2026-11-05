# Copyright (c) 2026, VEYQON and contributors
# License: AGPL-3.0-or-later. See license.txt
"""No AI on dev sites, and the token quota looked up by the exact site name. No model is called:
Model.chat is patched, and the quota database is replaced by a fake row lookup."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.api import resume_run, start_run
from flow.lib import site_rules
from flow.lib.model import Model
from flow.tests.test_ai_api import _agent_doc, _final, _model_doc

QUOTA_DB = {"user": "quota_ro", "password": "x", "database": "control_panel"}


def _row(quota=1000, bonus=0, used=0, status="Active"):
	return {"monthly_token_quota": quota, "bonus_credits": bonus, "tokens_used": used, "status": status}


class TestDevSiteRule(IntegrationTestCase):
	def test_a_dev_label_anywhere_marks_a_dev_site(self):
		for site in ("spm.dev.veyqon.co", "dev.veyqon.co", "hrms.DEV.veyqon.co", "dev"):
			self.assertTrue(site_rules.is_dev_site(site), site)

	def test_dev_inside_a_word_is_not_a_dev_site(self):
		for site in ("spm.veyqon.co", "devops.veyqon.co", "ayyanar.veyqon.co", "mydev.veyqon.co", "", None):
			self.assertFalse(site_rules.is_dev_site(site or ""), site)

	def test_a_dev_site_gets_no_provider_key(self):
		with patch.object(site_rules, "is_dev_site", return_value=True):
			from flow.lib.model import resolve_provider_credentials

			self.assertEqual(resolve_provider_credentials("openai/gpt-4o"), {})

	def test_a_dev_site_never_reaches_the_provider(self):
		model = Model(model_id="openai/gpt-4o-mini", api_key="not-used")
		with patch.object(site_rules, "is_dev_site", return_value=True), patch("litellm.completion") as completion:
			with self.assertRaises(frappe.ValidationError):
				model.chat("hello")
		completion.assert_not_called()

	def test_a_dev_site_turn_is_refused_before_any_run(self):
		frappe.set_user("Administrator")
		agent = _agent(self)
		runs = frappe.db.count("Flow Run")
		with patch.object(site_rules, "is_dev_site", return_value=True), patch.object(Model, "chat") as chat:
			with self.assertRaises(frappe.ValidationError):
				start_run("hello", agent=agent)
			streamed = b"".join(start_run("hello", agent=agent, stream=True).response)
		chat.assert_not_called()
		self.assertIn(b"development sites", streamed)
		self.assertIn(b'"status": "Completed"', streamed)
		self.assertEqual(frappe.db.count("Flow Run"), runs)


class TestQuota(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.looked_up = []

	def _check(self, row, conf=None, site="ayyanar.veyqon.co"):
		def fake_row(db, name):
			self.looked_up.append(name)
			return row

		config = {"ai_quota_db": QUOTA_DB, **(conf or {})}
		with patch.object(frappe.local, "site", site), patch.dict(frappe.local.conf, config), patch.object(site_rules, "_quota_row", fake_row):
			return site_rules.quota_message()

	def test_the_site_is_looked_up_by_its_exact_name(self):
		self.assertIsNone(self._check(_row(), site="Sample.veyqon.co "))
		self.assertEqual(self.looked_up, ["sample.veyqon.co"])

	def test_a_site_sharing_another_sites_database_names_its_row(self):
		self._check(_row(), conf={"ai_quota_site": "demo.veyqon.co"}, site="hrms.veyqon.co")
		self.assertEqual(self.looked_up, ["demo.veyqon.co"])

	def test_tokens_left_allows_the_turn(self):
		self.assertIsNone(self._check(_row(quota=1000, bonus=500, used=1499)))

	def test_used_up_or_exhausted_refuses_with_the_site_name(self):
		for row in (_row(quota=1000, bonus=500, used=1500), _row(used=0, status="Exhausted")):
			message = self._check(row)
			self.assertIn("ayyanar.veyqon.co", message)
			self.assertIn("Client Portal", message)  # Administrator gets the admin wording

	def test_a_missing_row_is_allowed_until_rows_are_required(self):
		self.assertIsNone(self._check(None))
		message = self._check(None, conf={"ai_quota_require_row": 1})
		self.assertIn("no token quota for this site", message)

	def test_not_configured_allows_and_never_connects(self):
		with patch.dict(frappe.local.conf, {"ai_quota_db": None}), patch.object(site_rules, "_quota_row") as lookup:
			self.assertIsNone(site_rules.quota_message())
		lookup.assert_not_called()

	def test_a_database_error_allows_and_is_logged(self):
		def broken(db, name):
			raise ConnectionError("db down")

		logged = frappe.db.count("Error Log", {"method": "AI quota check failed"})
		with patch.dict(frappe.local.conf, {"ai_quota_db": QUOTA_DB}), patch.object(site_rules, "_quota_row", broken):
			self.assertIsNone(site_rules.quota_message())
		self.assertEqual(frappe.db.count("Error Log", {"method": "AI quota check failed"}), logged + 1)

	def test_an_exhausted_site_cannot_start_or_continue_a_turn(self):
		agent = _agent(self)
		with patch.object(site_rules, "quota_message", return_value="out of tokens"), patch.object(Model, "chat") as chat:
			with self.assertRaises(frappe.ValidationError):
				start_run("hello", agent=agent)
			with self.assertRaises(frappe.ValidationError):
				resume_run("any-run", {"c1": "Approve"})
		chat.assert_not_called()

	def test_a_site_with_tokens_runs_normally(self):
		agent = _agent(self)
		with patch.object(site_rules, "quota_message", return_value=None), patch.object(Model, "chat", return_value=_final("hi")):
			self.assertEqual(start_run("hello", agent=agent)["status"], "Completed")


def _agent(case) -> str:
	model = frappe.get_doc(_model_doc(title="Site Rules Model")).insert()
	agent = frappe.get_doc(_agent_doc(model.name, title="Site Rules Agent")).insert()
	case.addCleanup(frappe.db.rollback)
	return agent.name
