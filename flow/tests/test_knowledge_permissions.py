# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""A knowledge hit whose source record the asking user cannot read never reaches them.

Every test that makes a claim about permissions runs as a named, non-Administrator user and
asserts `frappe.session.user` before searching: `has_permission` allows Administrator
everything, so a check run as Administrator proves nothing at all.

The fixture is `Note`, a core doctype of this bench, whose controller hook is
`bool(doc.public or doc.owner == user)`. A non-public Note owned by one person is therefore
readable by that person and by nobody else, through the same controller-permission path a
folder-by-role rule uses. No restriction is invented inside these tests.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.knowledge import store
from flow.knowledge.ingest import ingest_source
from flow.knowledge.retriever import CANDIDATE_CEILING, OVERFETCH, retrieve

DIM = 4

READER = "s18-reader@example.com"
OUTSIDER = "s18-outsider@example.com"

SECRET = "zarquonium"
PUBLIC = "brillig"

# Words that must never appear in anything this path hands to the model or a person.
FORBIDDEN = ("frappe", "flow", "erpnext", "mariadb", "openai", "lancedb", "localhost", "http")


def _embedding_response(vectors):
	return SimpleNamespace(data=[{"embedding": v, "index": i} for i, v in enumerate(vectors)])


def _ensure_user(email: str) -> str:
	"""A desk user with one harmless role.

	The role matters: a user with no role at all is created as a website user, is not given the
	automatic desk role, and so has no doctype-level read on the fixture — which would make
	every test below pass for the wrong reason. `Translator` grants nothing on the fixture, so
	doctype-level read comes from the automatic desk role and the only thing left to decide is
	the record.
	"""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"roles": [{"role": "Translator"}],
			}
		).insert(ignore_permissions=True)
	frappe.clear_cache(user=email)
	return email


def _set_settings(**values):
	for fieldname, value in values.items():
		frappe.db.set_single_value("Flow Knowledge Settings", fieldname, value)
	frappe.clear_document_cache("Flow Knowledge Settings", "Flow Knowledge Settings")


def _make_model(title="S18 Embed Model"):
	if frappe.db.exists("Flow Model", title):
		return frappe.get_doc("Flow Model", title)
	return frappe.get_doc(
		{
			"doctype": "Flow Model",
			"title": title,
			"model_id": "openai/text-embedding-3-small",
			"api_key": "sk-test",
			"enabled": 1,
		}
	).insert()


class KnowledgePermissionCase(IntegrationTestCase):
	"""Shared fixture: a knowledge base, notes with known readability, and a search helper."""

	def setUp(self):
		store.drop_table()
		self.model = _make_model()
		_set_settings(
			embedding_model=self.model.name,
			embedding_dimension=DIM,
			chunk_size=2000,
			chunk_overlap=20,
			search_type="Hybrid",
		)
		self.reader = _ensure_user(READER)
		self.outsider = _ensure_user(OUTSIDER)

	def tearDown(self):
		frappe.set_user("Administrator")
		store.drop_table()
		frappe.db.rollback()

	# --- fixture -------------------------------------------------------------

	def _kb(self, title):
		return frappe.get_doc({"doctype": "Flow Knowledge Base", "title": title}).insert()

	def _fake_embed(self, input, **kwargs):
		return _embedding_response([[0.1, 0.2, 0.3, 0.4]] * len(input))

	def _ingest(self, source_name):
		with (
			patch("litellm.embedding", side_effect=self._fake_embed),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			ingest_source(source_name)

	def _note(self, title, body, *, owner=None, public=0):
		"""A Note, inserted as Administrator, then stamped with its owner.

		The owner is set with db.set_value rather than by inserting as that user, so the
		fixture does not depend on the test users' create permission.
		"""
		note = frappe.get_doc(
			{"doctype": "Note", "title": title, "content": f"<p>{body}</p>", "public": public}
		).insert(ignore_permissions=True)
		if owner:
			frappe.db.set_value("Note", note.name, "owner", owner, update_modified=False)
			frappe.clear_document_cache("Note", note.name)
			note.reload()
		return note

	def _note_source(self, kb, *, title_like, title="Notes"):
		with patch("flow.knowledge.ingest.enqueue_ingestion"):
			source = frappe.get_doc(
				{
					"doctype": "Flow Knowledge Source",
					"knowledge_base": kb,
					"source_type": "DocType",
					"title": title,
					"reference_doctype": "Note",
					"content_fields": "title, content",
					"filters": json.dumps({"title": ["like", title_like]}),
				}
			).insert()
		self._ingest(source.name)
		return source

	def _text_source(self, kb, content, title="Plain Doc"):
		with patch("flow.knowledge.ingest.enqueue_ingestion"):
			source = frappe.get_doc(
				{
					"doctype": "Flow Knowledge Source",
					"knowledge_base": kb,
					"source_type": "Text",
					"title": title,
					"content": content,
				}
			).insert()
		self._ingest(source.name)
		return source

	def _retrieve_as(self, user, query, **kwargs):
		frappe.set_user(user)
		self.assertEqual(frappe.session.user, user)  # a check run as Administrator proves nothing
		try:
			with patch("litellm.embedding", side_effect=self._fake_embed):
				return retrieve(query, **kwargs)
		finally:
			frappe.set_user("Administrator")


class TestUnreadableHitsAreDropped(KnowledgePermissionCase):
	def setUp(self):
		super().setUp()
		self.kb = self._kb("S18 Secret KB")
		self.secret = self._note("S18 Secret", f"the {SECRET} process is confidential", owner=self.reader)
		self._note_source(self.kb.name, title_like="S18 Secret%")

	def test_a_user_who_may_not_read_the_record_gets_nothing_at_all(self):
		"""Feature 1. Asserted field by field over the whole structure, not assertFalse."""
		results = self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name])

		self.assertEqual(len(results), 0)
		self.assertEqual(results, [])
		blob = json.dumps(results, default=str)
		for fragment in (SECRET, self.secret.title, self.secret.name, "score", "content", "source"):
			with self.subTest(fragment=fragment):
				self.assertNotIn(fragment, blob)

	def test_control_the_owner_does_get_the_record(self):
		"""Feature 2. Without this, the test above passes by returning nothing to anybody."""
		results = self._retrieve_as(self.reader, SECRET, kbs=[self.kb.name])

		self.assertEqual(len(results), 1)
		self.assertIn(SECRET, results[0]["content"])
		self.assertEqual(results[0]["reference_doctype"], "Note")
		self.assertEqual(results[0]["reference_name"], self.secret.name)

	def test_a_dropped_hit_produces_no_message_and_does_not_raise(self):
		"""Feature 5. `throw=True` on the permission call would do both."""
		frappe.local.message_log = []
		results = self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name])

		self.assertEqual(results, [])
		self.assertEqual(frappe.local.message_log, [])

	def test_the_serialized_tool_result_carries_no_trace_of_the_record(self):
		"""Feature 6. A sweep over the serialized JSON the model is handed, not over the dict."""
		from flow.lib.agent import _serialize_tool_result

		results = self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name])
		serialized = _serialize_tool_result(results)

		self.assertNotIn(SECRET, serialized)
		self.assertNotIn(self.secret.title, serialized)
		self.assertNotIn(self.secret.name, serialized)

	def test_the_search_knowledge_tool_goes_through_the_filtered_path(self):
		"""Feature 8. The builtin the model calls, not the function underneath it."""
		from flow.tools.builtins import bind_search_knowledge

		tool = bind_search_knowledge([self.kb.name])

		frappe.set_user(self.outsider)
		self.assertEqual(frappe.session.user, self.outsider)
		try:
			with patch("litellm.embedding", side_effect=self._fake_embed):
				denied = tool(query=SECRET)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(denied, [])
		self.assertNotIn(SECRET, json.dumps(denied, default=str))

		frappe.set_user(self.reader)
		self.assertEqual(frappe.session.user, self.reader)
		try:
			with patch("litellm.embedding", side_effect=self._fake_embed):
				allowed = tool(query=SECRET)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(len(allowed), 1)  # control: the tool does reach the record for its owner

	def test_the_permission_check_is_about_the_record_not_the_doctype(self):
		"""Both users have doctype-level read on the fixture; only one owns the record."""
		self.assertTrue(frappe.has_permission("Note", "read", user=self.outsider))
		self.assertFalse(frappe.has_permission("Note", "read", self.secret.name, user=self.outsider))
		self.assertTrue(frappe.has_permission("Note", "read", self.secret.name, user=self.reader))


class TestChunksWithNoReference(KnowledgePermissionCase):
	def test_a_chunk_with_no_reference_is_returned_exactly_as_before(self):
		"""Feature 3. Backwards compatibility: Text, File and URL sources set no reference."""
		kb = self._kb("S18 Plain KB")
		source = self._text_source(kb.name, f"the {PUBLIC} handbook for everyone. " * 4)

		results = self._retrieve_as(self.outsider, PUBLIC, kbs=[kb.name])

		self.assertTrue(results)
		self.assertTrue(all(PUBLIC in r["content"] for r in results))
		self.assertTrue(all(r["source"] == source.name for r in results))
		self.assertTrue(all(r["reference_doctype"] is None for r in results))
		self.assertTrue(all(r["reference_name"] is None for r in results))

	def test_a_text_source_hit_is_titled_by_its_source_row_and_has_no_url(self):
		"""Feature 11."""
		kb = self._kb("S18 Plain KB 2")
		self._text_source(kb.name, f"the {PUBLIC} handbook for everyone. " * 4, title="Staff Handbook")

		results = self._retrieve_as(self.outsider, PUBLIC, kbs=[kb.name])

		self.assertTrue(results)
		self.assertTrue(all(r["title"] == "Staff Handbook" for r in results))
		self.assertTrue(all(r["url"] is None for r in results))


class TestOverFetchAndCeiling(KnowledgePermissionCase):
	"""The store's ranking is not the thing under test, so it is replaced by a fixed order.

	Every chunk id in the fixed order is a real MariaDB row with a real reference, so
	hydration and the permission check are the production ones. The spy honours the `limit`
	it is given, which is what makes the over-fetch a real measurement rather than a comment.
	"""

	def setUp(self):
		super().setUp()
		self.kb = self._kb("S18 Mixed KB")
		self.hidden = [
			self._note(f"S18 Mixed hidden {i}", f"{SECRET} restricted {i}", owner=self.reader)
			for i in range(6)
		]
		self.open = [self._note(f"S18 Mixed open {i}", f"{SECRET} shared {i}", public=1) for i in range(6)]
		self._note_source(self.kb.name, title_like="S18 Mixed%")
		self.hidden_ids = self._chunk_ids([n.name for n in self.hidden])
		self.open_ids = self._chunk_ids([n.name for n in self.open])
		self.assertEqual(len(self.hidden_ids), 6)
		self.assertEqual(len(self.open_ids), 6)

	def _chunk_ids(self, note_names):
		"""The chunk ids for these notes, in the order the notes were given.

		The query's own order is not the notes' order, and these tests are about what the
		filter does to a given order, so the mapping is made explicit.
		"""
		rows = frappe.get_all(
			"Flow Knowledge Chunk",
			filters={"reference_doctype": "Note", "reference_name": ["in", note_names]},
			fields=["name", "reference_name"],
		)
		by_note = {row["reference_name"]: int(row["name"]) for row in rows}
		return [by_note[name] for name in note_names if name in by_note]

	def _ordered_search(self, ordered_ids):
		"""A store stand-in that returns `ordered_ids`, truncated to the limit it is asked for."""
		calls = []

		def fake_search(vector, *, text=None, kbs=None, limit=5, **kwargs):
			calls.append(limit)
			return [{"id": i, "kb": "kb", "source": "src", "score": 1.0} for i in ordered_ids[:limit]]

		return fake_search, calls

	def test_unreadable_hits_ranked_first_do_not_cost_the_reader_their_results(self):
		"""Feature 4. With OVERFETCH reverted to 1 the store returns only hidden rows."""
		fake_search, calls = self._ordered_search(self.hidden_ids + self.open_ids)

		with patch("flow.knowledge.store.search", side_effect=fake_search):
			results = self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name], limit=5)

		self.assertEqual(calls, [5 * OVERFETCH])
		self.assertEqual(len(results), 5)
		self.assertEqual([r["reference_name"] for r in results], [n.name for n in self.open[:5]])

	def test_the_ceiling_is_the_stores_own_clamp(self):
		"""The ceiling is written as a literal because the store is imported lazily. If the
		store's clamp ever moves, this is what says so."""
		self.assertEqual(CANDIDATE_CEILING, store.MAX_SEARCH_LIMIT)

	def test_the_store_is_never_asked_for_more_than_the_ceiling(self):
		"""Feature 7, first half: the ceiling is the store's own clamp, so it is not invented."""
		fake_search, calls = self._ordered_search(self.hidden_ids)

		with patch("flow.knowledge.store.search", side_effect=fake_search):
			self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name], limit=CANDIDATE_CEILING)

		self.assertEqual(calls, [CANDIDATE_CEILING])
		self.assertLess(CANDIDATE_CEILING, CANDIDATE_CEILING * OVERFETCH)  # the clamp did work

	def test_more_unreadable_candidates_than_the_ceiling_ends_the_search(self):
		"""Feature 7, second half: it stops at the ceiling and returns what it has.

		The candidate list is longer than the ceiling, and every candidate inside the
		ceiling is unreadable. The readable rows sit beyond it and are never reached — the
		search does not page deeper to find them.
		"""
		beyond = [self.hidden_ids[0]] * CANDIDATE_CEILING + self.open_ids
		fake_search, calls = self._ordered_search(beyond)
		checked = []
		real = frappe.has_permission

		def counting_has_permission(*args, **kwargs):
			checked.append(args[:3])
			return real(*args, **kwargs)

		with (
			patch("flow.knowledge.store.search", side_effect=fake_search),
			patch.object(frappe, "has_permission", counting_has_permission),
		):
			results = self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name], limit=50)

		self.assertEqual(results, [])
		self.assertEqual(calls, [CANDIDATE_CEILING])
		self.assertEqual(len(checked), CANDIDATE_CEILING)

	def test_the_readable_and_unreadable_rows_keep_their_relative_order(self):
		"""A filter that also reorders would be a different change."""
		interleaved = []
		for hidden, open_ in zip(self.hidden_ids, self.open_ids, strict=True):
			interleaved.extend([hidden, open_])
		fake_search, _ = self._ordered_search(interleaved)

		with patch("flow.knowledge.store.search", side_effect=fake_search):
			results = self._retrieve_as(self.outsider, SECRET, kbs=[self.kb.name], limit=6)

		self.assertEqual([r["reference_name"] for r in results], [n.name for n in self.open])


class TestCitations(KnowledgePermissionCase):
	def setUp(self):
		super().setUp()
		self.kb = self._kb("S18 Cited KB")
		self.open = self._note("S18 Cited open", f"{PUBLIC} anyone may read this", public=1)
		self.hidden = self._note("S18 Cited hidden", f"{PUBLIC} nobody else may read this", owner=self.reader)
		self._note_source(self.kb.name, title_like="S18 Cited%")

	def test_every_doctype_hit_carries_the_records_own_title(self):
		"""Feature 9."""
		results = self._retrieve_as(self.outsider, PUBLIC, kbs=[self.kb.name])

		self.assertTrue(results)
		self.assertTrue(all(r["title"] for r in results))
		self.assertTrue(all(r["title"] == "S18 Cited open" for r in results))

	def test_every_doctype_hit_carries_a_relative_desk_path(self):
		"""Feature 10."""
		results = self._retrieve_as(self.outsider, PUBLIC, kbs=[self.kb.name])

		self.assertTrue(results)
		for row in results:
			with self.subTest(name=row["reference_name"]):
				self.assertEqual(row["url"], f"/app/note/{row['reference_name']}")
				self.assertTrue(row["url"].startswith("/"))
				self.assertNotIn("://", row["url"])

	def test_an_unreadable_hit_contributes_no_title(self):
		"""Feature 12. The titles are built after the permission check, so the dropped
		record's title is never even asked for."""
		from flow.knowledge import retriever

		asked = []
		real = retriever._titles_for

		def recording_titles_for(chunks):
			asked.extend(c.get("reference_name") for c in chunks)
			return real(chunks)

		with patch.object(retriever, "_titles_for", recording_titles_for):
			results = self._retrieve_as(self.outsider, PUBLIC, kbs=[self.kb.name])

		self.assertTrue(results)
		self.assertNotIn(self.hidden.name, asked)
		self.assertIn(self.open.name, asked)
		blob = json.dumps(results, default=str)
		self.assertNotIn("S18 Cited hidden", blob)

	def test_no_result_field_names_the_platform_a_vendor_or_the_host(self):
		"""Feature 13, with a positive control so a zero-hit sweep is a finding."""
		results = self._retrieve_as(self.outsider, PUBLIC, kbs=[self.kb.name])
		blob = json.dumps(results, default=str).lower()

		self.assertTrue(blob.strip())
		for word in FORBIDDEN:
			with self.subTest(word=word):
				self.assertNotIn(word, blob)

		# positive control: the sweep does find a forbidden word when one is present
		self.assertIn("flow", json.dumps([{"title": "Flow Knowledge Source"}]).lower())


class TestExistingGatesAreUntouched(KnowledgePermissionCase):
	"""Feature 14. The new filter is a third gate, never a replacement for the two that exist."""

	def setUp(self):
		super().setUp()
		self.kb = self._kb("S18 Gate KB")
		self._note("S18 Gate open", f"{PUBLIC} readable by all", public=1)
		self._note_source(self.kb.name, title_like="S18 Gate%")

	def test_an_empty_knowledge_base_scope_still_throws(self):
		frappe.set_user(self.outsider)
		try:
			with self.assertRaises(frappe.ValidationError):
				retrieve(PUBLIC, kbs=[])
		finally:
			frappe.set_user("Administrator")

	def test_a_disabled_knowledge_base_is_still_skipped(self):
		self.assertTrue(self._retrieve_as(self.outsider, PUBLIC, kbs=[self.kb.name]))

		frappe.db.set_value("Flow Knowledge Base", self.kb.name, "enabled", 0)
		self.assertEqual(self._retrieve_as(self.outsider, PUBLIC, kbs=[self.kb.name]), [])
