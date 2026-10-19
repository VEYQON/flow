# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, validate_call

_PRIMITIVE_SCHEMAS: dict[type, dict[str, str]] = {
	str: {"type": "string"},
	int: {"type": "integer"},
	float: {"type": "number"},
	bool: {"type": "boolean"},
	bytes: {"type": "string"},
}

# Verbs and prefixes indicating write, mutation, or side-effecting operations
MUTATION_PREFIXES = (
	"create_", "insert_", "add_", "update_", "modify_", "edit_", "patch_",
	"delete_", "remove_", "drop_", "clear_", "cancel_", "submit_", "amend_",
	"send_", "post_", "execute_", "run_", "apply_", "approve_", "reject_",
	"assign_", "mark_", "draft_", "close_", "terminate_", "archive_", "reset_",
	"disable_", "enable_", "set_",
)

MUTATION_EXACT_NAMES = frozenset({
	"create", "update", "delete", "submit", "cancel", "amend", "send",
	"approve", "reject", "execute", "run_action", "update_memory", "post",
	"insert", "remove", "patch", "modify", "apply", "execute_action",
})

READ_ONLY_PREFIXES = (
	"get_", "list_", "search_", "find_", "fetch_", "read_", "describe_",
	"show_", "view_", "check_", "count_", "summary_", "query_", "load_",
)

READ_ONLY_EXACT_NAMES = frozenset({
	"find_doctypes", "describe", "read", "search_knowledge", "search",
	"get", "list", "fetch", "count", "ping", "check",
})


def is_write_tool(name: str | None) -> bool:
	"""Check if a tool's name or slug indicates write/destructive/side-effecting operations."""
	if not name:
		return False
	clean = name.strip().lower().replace("-", "_")
	if clean in MUTATION_EXACT_NAMES:
		return True
	return any(clean.startswith(prefix) for prefix in MUTATION_PREFIXES)


def is_read_only_tool(name: str | None) -> bool:
	"""Check if a tool's name or slug is explicitly classified as read-only."""
	if not name:
		return False
	clean = name.strip().lower().replace("-", "_")
	if clean in READ_ONLY_EXACT_NAMES:
		return True
	return any(clean.startswith(prefix) for prefix in READ_ONLY_PREFIXES)


@dataclass
class Tool:
	name: str
	description: str
	parameters: dict[str, Any]
	func: Callable[..., Any]
	requires_confirmation: bool = False
	confirm_prompt: Callable[[dict[str, Any]], str] | None = None
	# How the approval question reads. `title` names the tool the way a person would, in place of
	# the slug. `confirm_template` states the request in plain language, filled in from the call's
	# arguments. Both optional, both about wording only — neither changes what the tool does.
	title: str | None = None
	confirm_template: str | None = None

	def __post_init__(self) -> None:
		self._validated = validate_call(config=ConfigDict(arbitrary_types_allowed=True))(self.func)

	def __call__(self, **kwargs: Any) -> Any:
		return self._validated(**kwargs)

	def to_dict(self) -> dict[str, Any]:
		return {
			"type": "function",
			"function": {
				"name": self.name,
				"description": self.description,
				"parameters": self.parameters,
			},
		}


def tool(
	func: Callable[..., Any] | None = None,
	*,
	name: str | None = None,
	description: str | None = None,
	requires_confirmation: bool | None = None,
	confirm_prompt: Callable[[dict[str, Any]], str] | None = None,
) -> Tool | Callable[[Callable[..., Any]], Tool]:
	def wrap(f: Callable[..., Any]) -> Tool:
		if not callable(f):
			raise TypeError("@tool can only be applied to callables")
		resolved_name = name or f.__name__
		req_conf = requires_confirmation
		if req_conf is None:
			# Fail-closed: missing confirmation flag defaults to True for mutating tools
			req_conf = True if is_write_tool(resolved_name) else False
		return Tool(
			name=resolved_name,
			description=description or (inspect.getdoc(f) or "").strip(),
			parameters=build_schema(f),
			func=f,
			requires_confirmation=req_conf,
			confirm_prompt=confirm_prompt,
		)

	return wrap(func) if func is not None else wrap


def build_schema(func: Callable[..., Any]) -> dict[str, Any]:
	sig = inspect.signature(func)
	hints = get_type_hints(func, include_extras=True)

	properties: dict[str, dict[str, Any]] = {}
	required: list[str] = []

	for param_name, param in sig.parameters.items():
		if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
			continue
		if param_name in ("self", "cls"):
			continue

		hint = hints.get(param_name, Any)
		actual_type, description = _unwrap_annotated(hint)
		actual_type, is_optional = _unwrap_optional(actual_type)

		prop_schema = _type_to_schema(actual_type)
		if description:
			prop_schema["description"] = description

		properties[param_name] = prop_schema
		if param.default is inspect.Parameter.empty and not is_optional:
			required.append(param_name)

	schema: dict[str, Any] = {
		"type": "object",
		"properties": properties,
		"additionalProperties": False,
	}
	if required:
		schema["required"] = required
	return schema


def _unwrap_annotated(t: Any) -> tuple[Any, str | None]:
	metadata = getattr(t, "__metadata__", None)
	if metadata is None:
		return t, None
	actual = getattr(t, "__origin__", t)
	description = next((m for m in metadata if isinstance(m, str)), None)
	return actual, description


def _unwrap_optional(t: Any) -> tuple[Any, bool]:
	origin = get_origin(t)
	if origin not in (Union, types.UnionType):
		return t, False

	args = get_args(t)
	if type(None) not in args:
		return t, False

	remaining = tuple(a for a in args if a is not type(None))
	if len(remaining) == 1:
		return remaining[0], True
	if not remaining:
		return Any, True
	return Union[remaining], True


def _type_to_schema(t: Any) -> dict[str, Any]:
	if t is Any or t is object or t is None:
		return {}

	if t in _PRIMITIVE_SCHEMAS:
		return dict(_PRIMITIVE_SCHEMAS[t])

	if t is dict:
		return {"type": "object"}
	if t is list:
		return {"type": "array"}

	origin = get_origin(t)
	args = get_args(t)

	if origin in (list, tuple, set, frozenset, typing.List, typing.Tuple, typing.Set, typing.FrozenSet):
		items_schema = _type_to_schema(args[0]) if args else {}
		return {"type": "array", "items": items_schema}

	if origin in (dict, typing.Dict):
		return {"type": "object"}

	if origin is Literal:
		return _enum_schema(list(args))

	if origin in (Union, types.UnionType):
		return {"anyOf": [_type_to_schema(a) for a in args]}

	if isinstance(t, type):
		if issubclass(t, BaseModel):
			return _inline_pydantic_refs(t.model_json_schema())
		if issubclass(t, Enum):
			return _enum_schema([m.value for m in t])

	return {}


def _inline_pydantic_refs(schema: dict[str, Any]) -> dict[str, Any]:
	defs = schema.pop("$defs", {})
	if not defs:
		return schema

	resolved: dict[str, dict[str, Any]] = {}

	def walk(node: Any) -> Any:
		if isinstance(node, list):
			return [walk(item) for item in node]
		if not isinstance(node, dict):
			return node
		ref = node.get("$ref")
		if isinstance(ref, str) and ref.startswith("#/$defs/"):
			def_name = ref.split("/")[-1]
			if def_name in resolved:
				return resolved[def_name]
			if def_name in defs:
				return walk(defs[def_name])
			return {"type": "object"}
		return {key: walk(value) for key, value in node.items()}

	for def_name, def_schema in defs.items():
		resolved[def_name] = walk(def_schema)

	return walk(schema)


def _enum_schema(values: list[Any]) -> dict[str, Any]:
	value_types = {type(v) for v in values}
	if value_types == {str}:
		return {"type": "string", "enum": values}
	if value_types == {bool}:
		return {"type": "boolean", "enum": values}
	if value_types <= {int}:
		return {"type": "integer", "enum": values}
	if value_types <= {int, float}:
		return {"type": "number", "enum": values}
	return {"enum": values}
