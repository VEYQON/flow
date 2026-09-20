#!/usr/bin/env python3
"""Run the agent-loop scenarios in this process against a scripted model.

There is no network here, no credential, and no real model. The runner builds a code `Agent`
around `ScriptedModel` below and never constructs the platform's own model class, so there is no
code path from here to a provider. `assert_no_real_model()` refuses to start if the environment
suggests otherwise.

These scenarios test the plumbing — which tool is called, whether approval is asked for, what the
exact "Approve" does and what "Deny" and free text do. They say nothing about whether a real model
would choose well. See README.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

# Any of these being set means someone has a real model configured in this shell. The runner is
# in-process and could import engine code that reads them, so it refuses rather than risks it.
CREDENTIAL_VARS = (
	"OPENAI_API_KEY",
	"ANTHROPIC_API_KEY",
	"OPENROUTER_API_KEY",
	"AZURE_OPENAI_API_KEY",
	"GOOGLE_API_KEY",
	"GEMINI_API_KEY",
	"MISTRAL_API_KEY",
	"COHERE_API_KEY",
	"GROQ_API_KEY",
	"TOGETHER_API_KEY",
	"FLOW_EVALS_ALLOW_REAL",
)


class RealModelConfigured(RuntimeError):
	"""Raised when the environment looks like a real model is available."""


def assert_no_real_model(environ: dict[str, str] | None = None) -> None:
	"""Refuse to run if a real model could be reached from this process.

	Deliberately a hard error and not a warning: an evaluation that quietly used a real model
	would bill someone and would report a different thing from what it claims to report.
	"""
	env = os.environ if environ is None else environ
	found = sorted(name for name in CREDENTIAL_VARS if env.get(name))
	if found:
		raise RealModelConfigured(
			"These scenarios run against a scripted model only, and this environment has "
			f"{', '.join(found)} set. Unset it, or run in a shell that has no model credentials."
		)


# --------------------------------------------------------------------------------------------
# The scripted model
# --------------------------------------------------------------------------------------------


class ScriptedModel:
	"""Returns the scenario's scripted turns in order. It is the only model the runner knows."""

	def __init__(self, script: list[dict[str, Any]]):
		self._script = list(script)
		self.calls: list[dict[str, Any]] = []

	def chat(self, messages, tools=None, *, stream=False):
		from flow.lib.model import ChatResponse, ToolCall

		self.calls.append({"messages": list(messages), "tools": tools})
		if not self._script:
			raise AssertionError("the scenario's model script ran out of turns")
		turn = self._script.pop(0)
		usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
		if turn.get("tool_calls"):
			return ChatResponse(
				content=turn.get("content"),
				tool_calls=[
					ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", {}))
					for c in turn["tool_calls"]
				],
				finish_reason="tool_calls",
				usage=usage,
			)
		return ChatResponse(content=turn.get("content", ""), finish_reason="stop", usage=usage)


# --------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------


@dataclass
class Result:
	name: str
	description: str
	passed: bool
	failures: list[str] = field(default_factory=list)


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[dict[str, Any]]:
	scenarios = []
	for path in sorted(directory.glob("*.yaml")):
		data = yaml.safe_load(path.read_text())
		if data.get("name") != path.stem:
			raise ValueError(f"{path.name}: 'name' is {data.get('name')!r}, expected {path.stem!r}")
		scenarios.append(data)
	return scenarios


def _build_tools(spec: list[dict[str, Any]], executed: list[tuple[str, dict[str, Any]]]):
	"""Every tool is a stub that records its call. Nothing in a scenario touches anything real."""
	from flow.lib.tool import Tool

	def make(name: str, result: str):
		# A closure per tool, so the stub's signature is **kwargs only and nothing about the
		# recording machinery can be mistaken for one of the tool's own parameters.
		def run(**kwargs: Any) -> str:
			executed.append((name, kwargs))
			return result

		return run

	tools = []
	for t in spec:
		name, result = t["name"], t.get("result", "ok")
		tools.append(
			Tool(
				name=name,
				description=t["description"],
				parameters=t.get("parameters", {"type": "object", "properties": {}}),
				func=make(name, result),
				requires_confirmation=bool(t.get("requires_confirmation", False)),
			)
		)
	return tools


def _check_tool_calls(expected: list[dict[str, Any]], actual, failures: list[str]) -> None:
	if len(expected) != len(actual):
		failures.append(
			f"expected {len(expected)} tool call(s), saw {len(actual)}: {[c.name for c in actual]}"
		)
		return
	for i, (want, got) in enumerate(zip(expected, actual, strict=True)):
		if want["name"] != got.name:
			failures.append(f"tool call {i}: expected {want['name']!r}, saw {got.name!r}")
		for key, value in (want.get("arguments") or {}).items():
			if got.arguments.get(key) != value:
				failures.append(
					f"tool call {i} argument {key!r}: expected {value!r}, saw {got.arguments.get(key)!r}"
				)


def _check_tool_results(expected: dict[str, Any], messages, label: str, failures: list[str]) -> None:
	"""Assert on what each pending call was told, by tool_call_id.

	`executed` says what ran; this says what the model was handed. They are different questions,
	and the defect that motivated this one is invisible to the first: nothing ran, and the
	person's own answer was written in as the tool's output.
	"""
	results = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
	for call_id, checks in expected.items():
		if call_id not in results:
			failures.append(f"[{label}] no tool result was recorded for {call_id!r}")
			continue
		content = results[call_id]
		for text in checks.get("contains") or []:
			if text not in content:
				failures.append(f"[{label}] {call_id} result does not contain {text!r}: {content!r}")
		for text in checks.get("absent") or []:
			if text in content:
				failures.append(f"[{label}] {call_id} result must not contain {text!r}: {content!r}")
		if "equals" in checks and content != checks["equals"]:
			failures.append(f"[{label}] {call_id} result is {content!r}, expected {checks['equals']!r}")


def _check_absent_text(forbidden: list[str], haystack: str, where: str, failures: list[str]) -> None:
	for text in forbidden:
		if text.lower() in haystack.lower():
			failures.append(f"{where} contains text that must not appear: {text!r}")


def run_scenario(scenario: dict[str, Any]) -> Result:
	from flow.lib.agent import Agent

	failures: list[str] = []
	expect = scenario.get("expect", {})
	forbidden = expect.get("absent_text", [])

	executed: list[tuple[str, dict[str, Any]]] = []
	model = ScriptedModel(scenario["model_script"])
	agent = Agent(
		model=model,
		name=scenario["name"],
		instructions=scenario.get("agent", {}).get("instructions"),
		tools=_build_tools(scenario.get("tools", []), executed),
	)

	result = agent.run(scenario["user_message"])

	if expect.get("pauses") is not None and result.paused != bool(expect["pauses"]):
		failures.append(f"expected paused={expect['pauses']}, saw paused={result.paused}")
	_check_tool_calls(expect.get("tool_calls", []), result.tool_calls, failures)

	questions = expect.get("questions")
	if questions is not None:
		if len(result.questions) != questions.get("count", len(result.questions)):
			failures.append(f"expected {questions['count']} question(s), saw {len(result.questions)}")
		for q in result.questions:
			if "options" in questions and q.options != questions["options"]:
				failures.append(f"question options were {q.options}, expected {questions['options']}")
			_check_absent_text(forbidden, q.prompt, "an approval question", failures)

	if result.paused:
		_check_absent_text(
			forbidden, result.output or "", "the reply shown while a change is pending", failures
		)

	# Each answer set is applied to the SAME pause, from the same transcript.
	for label, case in (expect.get("after") or {}).items():
		_run_answer_case(scenario, label, case, result, forbidden, failures)

	return Result(scenario["name"], scenario.get("description", ""), not failures, failures)


def _run_answer_case(scenario, label, case, paused, forbidden, failures) -> None:
	"""Replay the pause with one answer set, on a fresh agent, and assert what actually ran.

	A case may give its own `tools`, which replace the scenario's for the resume only. That is
	not a convenience: the runtime that resumes a run is rebuilt from a record and is not
	necessarily the one that paused it, so a tool can be gone, renamed or no longer gated by the
	time a person's answer arrives. A scenario cannot describe that without saying it.
	"""
	from flow.lib.agent import Agent

	executed: list[tuple[str, dict[str, Any]]] = []
	model = ScriptedModel(case.get("model_script", []))
	agent = Agent(
		model=model,
		name=scenario["name"],
		instructions=scenario.get("agent", {}).get("instructions"),
		tools=_build_tools(case.get("tools", scenario.get("tools", [])), executed),
	)
	try:
		# Deliberately no record of what was asked: a scenario must run against any engine in
		# this repository's history, so the runner uses only the call every version has. The
		# missing-tool case below needs no such record — that is part of what it establishes.
		resumed = agent.resume([dict(m) for m in paused.messages], case["answers"])
	except Exception as e:  # a scenario that cannot even resume is a failure, not a crash
		failures.append(f"[{label}] resume raised {type(e).__name__}: {e}")
		return

	_check_tool_results(case.get("tool_results") or {}, resumed.messages, label, failures)

	ran = [name for name, _args in executed]
	if sorted(ran) != sorted(case.get("executed", [])):
		failures.append(f"[{label}] expected {sorted(case.get('executed', []))} to run, saw {sorted(ran)}")
	if "output" in case and resumed.output != case["output"]:
		failures.append(f"[{label}] expected output {case['output']!r}, saw {resumed.output!r}")
	if "paused" in case and resumed.paused != bool(case["paused"]):
		failures.append(f"[{label}] expected paused={case['paused']}, saw {resumed.paused}")
	_check_absent_text(forbidden, resumed.output or "", f"[{label}] the reply", failures)


# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------


def report(results: list[Result], out=sys.stdout) -> int:
	width = max((len(r.name) for r in results), default=4)
	print(f"{'SCENARIO'.ljust(width)}  RESULT  DESCRIPTION", file=out)
	print(f"{'-' * width}  ------  -----------", file=out)
	for r in results:
		print(f"{r.name.ljust(width)}  {'PASS  ' if r.passed else 'FAIL  '}  {r.description}", file=out)
		for failure in r.failures:
			print(f"{' ' * width}          - {failure}", file=out)
	passed = sum(1 for r in results if r.passed)
	print(f"\nEVALS={len(results)} PASSED={passed} FAILED={len(results) - passed}", file=out)
	return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--scenario", help="run one scenario by name")
	parser.add_argument("--list", action="store_true", help="list scenario names and exit")
	args = parser.parse_args(argv)

	assert_no_real_model()

	scenarios = load_scenarios()
	if args.list:
		for s in scenarios:
			print(s["name"])
		return 0
	if args.scenario:
		scenarios = [s for s in scenarios if s["name"] == args.scenario]
		if not scenarios:
			print(f"no scenario named {args.scenario!r}", file=sys.stderr)
			return 2

	return report([run_scenario(s) for s in scenarios])


if __name__ == "__main__":
	raise SystemExit(main())
