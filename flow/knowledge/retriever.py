# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Query-time retrieval over the knowledge store.

Embeds the query, runs a KB-scoped search (hybrid or vector-only per the
Flow Knowledge Settings search_type), and hydrates each hit from MariaDB (the
source of truth) for its text and provenance.

Permission model — three gates, and the asking user is one of them.

Scoping is fail-closed: an empty scope is refused, never widened to the whole
store. Only knowledge bases that currently exist and are enabled are searched,
so disabling one is a real off-switch. And a hit is kept only if the asking
user could open the record it came from: a chunk indexed from a record is shown
to somebody who may read that record and to nobody else.

Knowledge bases are curated by administrators, bound to agents by
administrators, and the caller cannot widen the scope. That makes the binding
the outer boundary — but it is not the only one, because a curated scope can
hold records whose own doctype restricts who may read them, and the scope
cannot tell those apart. A chunk with no record behind it is unchanged: there
is nothing to check it against, and the binding remains its only boundary.

A hit dropped for permission is dropped silently. "Some results were withheld"
is itself information about records the asker may not know exist.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import frappe
from frappe import _

KB_DOCTYPE = "Flow Knowledge Base"
CHUNK_DOCTYPE = "Flow Knowledge Chunk"
SOURCE_DOCTYPE = "Flow Knowledge Source"
DEFAULT_LIMIT = 5

# Filtering happens after the search, so a `limit`-sized search can leave the caller with one
# readable hit or none. Ask for more and stop at the first `limit` that survive.
OVERFETCH = 4
# The hard ceiling on candidates examined, and it is not ours: the store clamps its own limit
# to MAX_SEARCH_LIMIT, so a larger number here would be silently truncated by the store and
# would be a promise this code cannot keep. It bounds the added work per search at one
# single-row permission check per candidate, at most this many. Written as a literal because
# the store is imported lazily (it pulls in the vector library); a test pins the two together.
CANDIDATE_CEILING = 100


def retrieve_attachments(query: str, *, session: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
	"""Best-matching chunks from the files attached to `session`, most relevant first.

	Scoping to `session` is the boundary (enforced upstream by the session-owner check).
	Returns [{content, score}]; empty on a blank query or when nothing is indexed.
	"""
	query = (query or "").strip()
	if not query or not session:
		return []

	from flow.knowledge import attachment_store
	from flow.knowledge.embedder import embed_texts

	search_type = frappe.get_cached_value("Flow Knowledge Settings", "Flow Knowledge Settings", "search_type")
	text = query if search_type != "Vector" else None

	(vector,) = embed_texts([query])
	return attachment_store.search(vector, session=session, text=text, limit=limit)


def retrieve(query: str, *, kbs: list[str], limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
	"""Return the best-matching chunks within `kbs`, most relevant first.

	`kbs` must be non-empty — an empty scope is refused, not read as "all".
	"""
	if not kbs:
		frappe.throw(
			_("Knowledge search requires at least one knowledge base."),
			title=_("No Knowledge Base"),
		)
	query = (query or "").strip()
	if not query:
		return []

	kbs = _enabled_kbs(kbs)
	if not kbs:
		return []

	from flow.knowledge import store
	from flow.knowledge.embedder import embed_texts

	search_type = frappe.get_cached_value("Flow Knowledge Settings", "Flow Knowledge Settings", "search_type")
	text = query if search_type != "Vector" else None

	(vector,) = embed_texts([query])
	hits = store.search(vector, text=text, kbs=kbs, limit=min(limit * OVERFETCH, CANDIDATE_CEILING))
	if not hits:
		return []

	chunks = _hydrate({int(hit["id"]) for hit in hits})
	kept: list[tuple[dict[str, Any], dict[str, Any]]] = []
	for hit in hits:
		chunk = chunks.get(int(hit["id"]))
		if chunk is None:
			continue
		if not _may_read(chunk):
			continue
		kept.append((hit, chunk))
		if len(kept) >= limit:
			break

	titles = _titles_for([chunk for _, chunk in kept])
	return [
		{
			"content": chunk["content"],
			"score": hit["score"],
			"source": chunk["source"],
			"reference_doctype": chunk.get("reference_doctype"),
			"reference_name": chunk.get("reference_name"),
			"title": titles.get(_title_key(chunk)),
			"url": _url_of(chunk),
		}
		for hit, chunk in kept
	]


def _may_read(chunk: dict[str, Any]) -> bool:
	"""Whether the asking user may read the record this chunk came from.

	A chunk indexed from a record is shown only to somebody who could open that record. A chunk
	with no record behind it is unchanged: there is nothing to check it against.

	The record, not its type: the type-level question would answer the same for every row and
	so would check nothing. The user is whoever the call is running as.

	`throw` stays off, and this is a rule rather than a preference. Turning it on would raise out
	of the middle of a search, and would also switch on the "you do not have access to this
	document" messages — four dropped hits would name four records the asker may not know exist.
	"""
	doctype, name = chunk.get("reference_doctype"), chunk.get("reference_name")
	if not doctype or not name:
		return True
	return frappe.has_permission(doctype, "read", name)


def _title_key(chunk: dict[str, Any]) -> tuple[str, str]:
	"""Which record names this chunk: the referenced record, or the knowledge source row."""
	doctype, name = chunk.get("reference_doctype"), chunk.get("reference_name")
	if doctype and name:
		return (doctype, name)
	return (SOURCE_DOCTYPE, chunk["source"])


def _titles_for(chunks: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
	"""Titles for the records the kept chunks came from: one query per distinct doctype.

	Batched deliberately, never one query per hit. Reading with `frappe.get_all` is right here
	precisely because every chunk passed in has already been kept.
	"""
	wanted: dict[str, set[str]] = {}
	for chunk in chunks:
		doctype, name = _title_key(chunk)
		wanted.setdefault(doctype, set()).add(name)

	titles: dict[tuple[str, str], str] = {}
	for doctype, names in wanted.items():
		field = frappe.get_meta(doctype).get_title_field()
		if field == "name":
			titles.update({(doctype, name): name for name in names})
			continue
		rows = frappe.get_all(
			doctype,
			filters={"name": ["in", list(names)]},
			fields=["name", f"`{field}` as chunk_title"],
		)
		for row in rows:
			titles[(doctype, row["name"])] = row["chunk_title"]
	return titles


def _url_of(chunk: dict[str, Any]) -> str | None:
	"""A relative path to the record this chunk came from, or None when there is no record.

	Relative on purpose: an absolute address carries the host, which is noise to the caller and
	an avoidable leak. The name is percent-encoded so a record whose name contains a separator
	cannot change the shape of the path.
	"""
	doctype, name = chunk.get("reference_doctype"), chunk.get("reference_name")
	if not doctype or not name:
		return None
	slug = frappe.scrub(doctype).replace("_", "-")
	return f"/app/{slug}/{quote(str(name), safe='')}"


def _enabled_kbs(kbs: list[str]) -> list[str]:
	"""Keep only knowledge bases that still exist and are enabled. Disabling or
	deleting a KB removes it from every bound agent's reach without re-binding."""
	return frappe.get_all(KB_DOCTYPE, filters={"name": ["in", kbs], "enabled": 1}, pluck="name")


def _hydrate(ids: set[int]) -> dict[int, dict[str, Any]]:
	rows = frappe.get_all(
		CHUNK_DOCTYPE,
		filters={"name": ["in", list(ids)]},
		fields=["name", "content", "source", "reference_doctype", "reference_name"],
	)
	return {int(row["name"]): row for row in rows}
