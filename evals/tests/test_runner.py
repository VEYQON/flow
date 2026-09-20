"""Tests for the scenario runner itself.

Plain unittest, outside the bench's test gate, because nothing under `flow/` changes for these
evaluations. `run.py` imports the engine lazily inside its functions, so this module needs only
PyYAML:

    python3 -m unittest discover -s evals/tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run as runner


class TestItRefusesARealModel(unittest.TestCase):
	"""The runner must refuse rather than quietly use a real model: that would bill someone and
	would report a different thing from what the README says it reports."""

	def test_a_provider_credential_in_the_environment_refuses_the_run(self):
		for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
			with self.subTest(var=var), self.assertRaises(runner.RealModelConfigured) as caught:
				runner.assert_no_real_model({var: "sk-whatever"})
			self.assertIn(var, str(caught.exception))

	def test_the_explicit_escape_hatch_also_refuses(self):
		with self.assertRaises(runner.RealModelConfigured):
			runner.assert_no_real_model({"FLOW_EVALS_ALLOW_REAL": "1"})

	def test_an_empty_credential_is_not_treated_as_configured(self):
		"""An exported-but-blank variable is the ordinary state of a shell that has none."""
		runner.assert_no_real_model({"OPENAI_API_KEY": ""})

	def test_a_clean_environment_is_allowed(self):
		"""The positive control. Without this, every test above would pass on a function that
		raised unconditionally."""
		runner.assert_no_real_model({"PATH": "/usr/bin", "HOME": "/home/someone"})

	def test_every_listed_variable_is_actually_checked(self):
		"""A name in the list that the check ignores would be a comment, not a guard."""
		for var in runner.CREDENTIAL_VARS:
			with self.subTest(var=var), self.assertRaises(runner.RealModelConfigured):
				runner.assert_no_real_model({var: "x"})


class TestItCannotReachAProvider(unittest.TestCase):
	def test_the_runner_never_names_the_platforms_own_model_class(self):
		"""The only model the runner knows is the scripted one. If this file ever constructs the
		engine's `Model`, a scenario could make a real request."""
		source = (Path(runner.__file__)).read_text()
		self.assertNotIn("Model(", source.replace("ScriptedModel(", ""))
		self.assertNotIn("import requests", source)
		self.assertNotIn("urllib", source)
		self.assertNotIn("site_config", source)

	def test_the_scripted_model_is_what_a_scenario_gets(self):
		self.assertTrue(hasattr(runner.ScriptedModel, "chat"))
		model = runner.ScriptedModel([{"content": "hi"}])
		self.assertEqual(model.calls, [])


class TestScenarioLoading(unittest.TestCase):
	def test_every_shipped_scenario_parses_and_is_named_after_its_file(self):
		scenarios = runner.load_scenarios()

		self.assertGreaterEqual(len(scenarios), 5)
		for s in scenarios:
			with self.subTest(name=s["name"]):
				self.assertIn("user_message", s)
				self.assertIn("model_script", s)
				self.assertIn("expect", s)

	def test_a_scenario_whose_name_disagrees_with_its_filename_is_rejected(self):
		import tempfile

		with tempfile.TemporaryDirectory() as d:
			Path(d, "alpha.yaml").write_text("name: beta\n")
			with self.assertRaises(ValueError):
				runner.load_scenarios(Path(d))

	def test_the_approval_path_is_actually_covered(self):
		"""The five scenarios must between them exercise Approve, Deny and free text. A suite
		that only covered the happy path would say nothing about the approval contract."""
		answer_values = set()
		for s in runner.load_scenarios():
			for case in (s.get("expect", {}).get("after") or {}).values():
				answer_values.update(str(v) for v in case["answers"].values())

		self.assertIn("Approve", answer_values)
		self.assertIn("Deny", answer_values)
		self.assertTrue(
			[v for v in answer_values if v not in ("Approve", "Deny")],
			"no scenario answers with free text",
		)


class TestReporting(unittest.TestCase):
	def test_a_failing_scenario_makes_the_summary_say_so(self):
		import io

		out = io.StringIO()
		code = runner.report(
			[
				runner.Result("ok_one", "fine", True),
				runner.Result("bad_one", "not fine", False, ["it executed something it should not have"]),
			],
			out=out,
		)
		text = out.getvalue()

		self.assertEqual(code, 1)
		self.assertIn("EVALS=2 PASSED=1 FAILED=1", text)
		self.assertIn("it executed something it should not have", text)

	def test_all_passing_reports_zero_failures(self):
		import io

		out = io.StringIO()
		code = runner.report([runner.Result("ok_one", "fine", True)], out=out)

		self.assertEqual(code, 0)
		self.assertIn("FAILED=0", out.getvalue())


if __name__ == "__main__":
	unittest.main()
