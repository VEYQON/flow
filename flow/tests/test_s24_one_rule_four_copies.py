# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""The rule is written down once. This is what checks the copies against the writing.

THE PROBLEM. The same Unicode rule is written out four times across this estate — twice in this
repository (`flow/lib/agent.py` and `frontend/src/lib/display.js`), once in `q_flow_tools`, once in
each front end — and on 28 September 2026 a consolidation found that three of the four were wrong,
each in a different way. Nothing can be shared between the repositories. So the rule is written down
ONCE, in `brain/40-architecture/the-invisible-character-rule.md`, with every range, every clause of
the exception, every test vector and the reason for each — and this module PARSES THAT NOTE and
fails when a copy in this repository has drifted from it.

WHY A TEST AND NOT A COMMENT. CLAUDE.md: "a gate whose failure mode has never been observed is a
comment." Every assertion here has been watched go red by changing one code point, in the note and
in each copy in turn; the run log for 29 Sep 2026 records the hashes.

WHAT IT CANNOT REACH, and the reason it is still worth having: `q_flow_tools` and the two front-end
repositories are not on this machine's test path. What this module buys them is that the note it
reads is kept true, so a person changing one of those copies has something to compare against that
is known to match a working implementation rather than known to be prose.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unicodedata

from frappe.tests import IntegrationTestCase

from flow.lib.agent import _DEFAULT_IGNORABLE_RANGES, _escaped, _paints_nothing

_REPO = pathlib.Path(__file__).resolve().parents[2]
_NOTE = _REPO / "brain" / "40-architecture" / "the-invisible-character-rule.md"
_PANEL = _REPO / "frontend" / "src" / "lib" / "display.js"


def _block(name: str) -> list[str]:
	"""The lines of the note's fenced block tagged `name`, comments and blanks dropped."""
	text = _NOTE.read_text(encoding="utf-8")
	match = re.search(rf"^```{name}\n(.*?)^```$", text, re.DOTALL | re.MULTILINE)
	assert match, f"the note has no ```{name} block — {_NOTE}"
	return [line for line in match.group(1).splitlines() if line.strip()]


class TestTheNoteIsReadableAtAll(IntegrationTestCase):
	"""The positive control for every test below. A note that had been renamed, emptied or had its
	fences renamed would otherwise make this whole module pass by finding nothing to disagree with.
	"""

	def test_the_note_exists_and_the_three_blocks_are_in_it(self):
		self.assertTrue(_NOTE.exists(), _NOTE)
		self.assertEqual(len(_block("default-ignorable-ranges")), 17)
		self.assertGreater(len(_block("viramas")), 0)
		self.assertGreater(len(_block("vectors")), 15)

	def test_a_block_that_is_not_there_is_an_error_and_not_an_empty_pass(self):
		with self.assertRaises(AssertionError):
			_block("no-such-block")


class TestTableOneMatchesTheEnginesOwnTable(IntegrationTestCase):
	"""§2 of the note against `_DEFAULT_IGNORABLE_RANGES`."""

	def _written(self) -> tuple[tuple[int, int], ...]:
		rows = []
		for line in _block("default-ignorable-ranges"):
			low, high = line.split()[:2]
			rows.append((int(low[2:], 16), int(high[2:], 16)))
		return tuple(rows)

	def test_the_ranges_are_the_same_ranges(self):
		self.assertEqual(self._written(), _DEFAULT_IGNORABLE_RANGES)

	def test_the_counts_written_beside_each_range_are_the_range(self):
		"""The third column is not decoration: it is what a reader checks the hex against."""
		for line in _block("default-ignorable-ranges"):
			low, high, count = line.split()[:3]
			with self.subTest(range=f"{low}..{high}"):
				self.assertEqual(int(high[2:], 16) - int(low[2:], 16) + 1, int(count))

	def test_the_total_the_note_states_is_the_total(self):
		total = sum(high - low + 1 for low, high in self._written())
		self.assertEqual(total, 4174)
		self.assertIn("4174 code points in all", _NOTE.read_text(encoding="utf-8"))

	def test_the_table_is_sorted_and_disjoint_because_the_scan_depends_on_it(self):
		"""`_paints_nothing` stops early on the first range whose low is above the code point. That
		is only correct for a sorted, non-overlapping table, and nothing else asserts it."""
		previous = -1
		for low, high in _DEFAULT_IGNORABLE_RANGES:
			self.assertLess(previous, low)
			self.assertLessEqual(low, high)
			previous = high

	def test_the_early_exit_agrees_with_a_plain_membership_test(self):
		"""The property, over every code point the table names and the code points either side of
		each range — where an off-by-one in the scan would live."""
		members = {cp for low, high in _DEFAULT_IGNORABLE_RANGES for cp in range(low, high + 1)}
		edges = set()
		for low, high in _DEFAULT_IGNORABLE_RANGES:
			edges.update({low - 1, low, high, high + 1})
		for cp in sorted(edges):
			if 0 <= cp <= 0x10FFFF:
				with self.subTest(cp=hex(cp)):
					self.assertEqual(_paints_nothing(chr(cp)), cp in members)


class TestTableTwoMatchesTheLibraryAndThePanel(IntegrationTestCase):
	"""§4 of the note, against Python's `unicodedata` and against the panel's generated table.

	The engine has no virama table — it reads the combining class — so the note is checked against
	the LIBRARY here, and the panel's table (which exists only because JavaScript cannot ask for a
	combining class) is checked against the note. Those two together are what stop the panel drifting.
	"""

	def _written(self) -> list[int]:
		return [int(token[2:], 16) for line in _block("viramas") for token in line.split()]

	def _from_the_library(self) -> list[int]:
		return [cp for cp in range(0x110000) if unicodedata.combining(chr(cp)) == 9]

	def test_the_note_lists_exactly_the_viramas_the_library_knows(self):
		self.assertEqual(self._written(), self._from_the_library())

	def test_the_panels_generated_table_is_the_same_table(self):
		source = _PANEL.read_text(encoding="utf-8")
		match = re.search(r"const VIRAMAS = new Set\(\[(.*?)\]\);", source, re.DOTALL)
		self.assertIsNotNone(match, "the panel no longer has a VIRAMAS table")
		panel = [int(token, 16) for token in re.findall(r"0x[0-9a-fA-F]+", match.group(1))]
		self.assertEqual(panel, self._written())

	def test_the_count_the_note_states_is_the_count(self):
		self.assertIn("**69** viramas", _NOTE.read_text(encoding="utf-8"))
		self.assertEqual(len(self._written()), 69)

	def test_the_sixteen_the_front_ends_hand_write_are_a_strict_subset(self):
		"""The claim §4 makes about copies #4 and #5, asserted rather than said."""
		front_ends = {
			0x094D,
			0x09CD,
			0x0A4D,
			0x0ACD,
			0x0B4D,
			0x0BCD,
			0x0C4D,
			0x0CCD,
			0x0D4D,
			0x0DCA,
			0x0F84,
			0x1039,
			0x103A,
			0x17D2,
			0x1BAA,
			0xA8C4,
		}
		self.assertTrue(front_ends < set(self._written()))


class TestTableThreeIsWhatTheEngineActuallyDoes(IntegrationTestCase):
	"""§5 of the note, run against `_escaped` line by line. The same list is run against the panel
	by `frontend/tests/joinerInsideAWord.spec.js`, so a divergence fails on both sides."""

	def _vectors(self) -> list[tuple[str, str, str]]:
		rows = []
		for line in _block("vectors"):
			raw, shown, why = line.split("\t", 2)
			rows.append((ast.literal_eval(raw), ast.literal_eval(shown), why))
		return rows

	def test_every_vector_in_the_note_is_the_engines_own_output(self):
		for raw, shown, why in self._vectors():
			with self.subTest(why=why):
				self.assertEqual(_escaped(raw), shown)

	def test_the_vectors_cover_all_four_clauses_and_the_control(self):
		"""A table of vectors that had quietly lost its negative cases would still pass above."""
		reasons = " ".join(why for _, _, why in self._vectors())
		for clause in ("clause 1", "clause 2", "clause 3", "clause 4"):
			self.assertIn(clause, reasons)
		self.assertIn("the control", reasons)

	def test_kept_and_marked_vectors_are_both_present_in_force(self):
		kept = [raw for raw, shown, _ in self._vectors() if raw == shown]
		marked = [raw for raw, shown, _ in self._vectors() if raw != shown]
		self.assertGreaterEqual(len(kept), 5)
		self.assertGreaterEqual(len(marked), 10)

	def test_the_note_still_carries_the_word_the_whole_change_is_about(self):
		"""If the exception is ever removed, §3 has to be removed with it, and this is what makes
		that a decision somebody takes rather than a paragraph that quietly stops being true."""
		text = _NOTE.read_text(encoding="utf-8")
		self.assertIn("CONSONANT + VIRAMA + ZWJ + CONSONANT", text)
		self.assertIn("ශ්‍රී", text)  # the word itself, joiner and all
