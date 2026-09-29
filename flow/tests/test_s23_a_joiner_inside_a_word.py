# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""A joiner that spells a word is not a joiner that hides one.

THE DEFECT THIS MODULE WAS WRITTEN AGAINST. `_escaped` marks every code point carrying Unicode's
`Default_Ignorable_Code_Point` property, and U+200D ZERO WIDTH JOINER carries it. In Sinhala, Tamil,
Devanagari and every other Indic script the joiner is not decoration: a conjunct is spelled
CONSONANT + VIRAMA + ZWJ + CONSONANT, so `\u0dc1\u0dca\u200d\u0dbb\u0dd3` — the first word of this
company's own country — arrived on the approval card as `\u0dc1\u0dca\\u200d\u0dbb\u0dd3`, six
characters of machine escape dropped into the middle of a word. The defence meant to stop a person
approving something they could not read was making the text unreadable for the people who read it.

WHAT IS ADOPTED, AND FROM WHERE. Two sibling front ends had already decided this and tested it —
`agentq-web/src/features/chat/lib/visibleControls.ts` and its character-for-character twin in
`veyqon-web`, both at `joinsRatherThanHides`: *"a joiner that FOLLOWS a virama is orthography, not
hiding, and is kept."* This module does not invent a policy; it ports one, and then tightens it,
because the front ends' version never looks at what comes AFTER the joiner and so keeps a joiner
with nothing to join.

THE RULE, WHOLE. A joiner — U+200C ZWNJ or U+200D ZWJ — is left as itself, unescaped, only when
every one of these holds:
  1. it is neither the first nor the last code point of the value;
  2. the code point immediately before it is a VIRAMA (canonical combining class 9);
  3. the code point immediately after it is a LETTER (general category L*);
  4. those two neighbours lie in the same 128-code-point aligned block.
Everything else is escaped exactly as it was before: a joiner at either edge, a doubled joiner, a
joiner in a run, a joiner beside a space or a bracket, a joiner between two ordinary letters, and
every other character the escaper has ever marked.

WHY EACH CLAUSE IS THERE, since a rule nobody can justify is a rule the next person will widen:
  - (1) and (3) are the shape of the attack. A joiner with nothing on one side of it is joining
    nothing; it is only hiding. They also dispose of the doubled and the run cases for free, because
    the code point after the first joiner in `VIRAMA ZWJ ZWJ LETTER` is a joiner, which is not a
    letter.
  - (2) is what makes it orthography rather than decoration, and it is READ FROM THE LIBRARY —
    `unicodedata.combining(ch) == 9` is Unicode's own Virama combining class. The front ends write
    out sixteen viramas by hand; measured here on UCD 16.0.0 there are SIXTY-NINE, so their list is
    a strict subset missing fifty-three, which is the same table-drift this whole change is about.
  - (4) is the one clause the front ends do not have at all, and it closes a hole they still carry.
    Without it `SO-000A\u094d\u200dx` — a Latin letter, a Devanagari virama, a joiner, a Latin `x`
    — satisfies every other clause while the joiner forms no conjunct with anything, so it is purely
    hidden. A virama binds the letter beside it in its OWN script.

WHAT THIS DOES NOT DO, SAID PLAINLY. It changes what is DISPLAYED and nothing else. The arguments
the tool receives are the model's own bytes, before and after this change alike, and
`TestTheValueThatRUNSIsUntouched` asserts that through a real approved call rather than by reading
the code.

NOT TOUCHED BY THIS MODULE: `_invoke`, `_resolve_confirmation`, `_confirmation_question` and
`_has_denial` (CLAUDE.md rule 4). `_escaped` gains one branch; no second escaper is created.
"""

from __future__ import annotations

import unicodedata

from frappe.tests import IntegrationTestCase

from flow.lib.agent import _escaped

ZWJ = "\u200d"
ZWNJ = "\u200c"
RTL = "\u202e"  # RIGHT-TO-LEFT OVERRIDE

# Real words, written the way a person writes them. Each is CONSONANT VIRAMA ZWJ CONSONANT ...
SINHALA = "\u0dc1\u0dca\u200d\u0dbb\u0dd3 \u0dbd\u0d82\u0d9a\u0dcf"  # "Sri Lanka"
TAMIL = "\u0b95\u0bcd\u200d\u0bb7"  # the conjunct KSSA
DEVANAGARI = "\u0915\u094d\u200d\u0937"  # the conjunct KSSA


class TestAConjunctSurvivesTheCard(IntegrationTestCase):
	"""T1. The damage, asserted on the exact output — not on "contains a marker"."""

	def test_a_sinhala_word_reaches_the_reader_as_it_was_written(self):
		self.assertEqual(_escaped(SINHALA), SINHALA)

	def test_a_tamil_conjunct_reaches_the_reader_as_it_was_written(self):
		self.assertEqual(_escaped(TAMIL), TAMIL)

	def test_a_devanagari_conjunct_reaches_the_reader_as_it_was_written(self):
		self.assertEqual(_escaped(DEVANAGARI), DEVANAGARI)

	def test_a_zero_width_non_joiner_spells_too(self):
		"""ZWNJ after a virama asks for the SEPARATE form where ZWJ asks for the joined one. Both
		are spelling, so both are exempt, and the front ends' `JOINERS` list says the same."""
		word = "\u0dc1\u0dca\u200c\u0dbb"
		self.assertEqual(_escaped(word), word)


class TestTheControlsThatMustStillGoRed(IntegrationTestCase):
	"""T1's other half: the cases that were already right and must stay right."""

	def test_a_joiner_with_no_letter_either_side_is_still_escaped(self):
		self.assertEqual(_escaped(ZWJ), "\\u200d")
		self.assertEqual(_escaped(f"paid{ZWJ}unpaid"), "paid\\u200dunpaid")

	def test_a_right_to_left_override_is_still_escaped(self):
		self.assertEqual(_escaped(f"gnp{RTL}exe"), "gnp\\u202eexe")

	def test_ordinary_text_is_still_returned_untouched(self):
		self.assertEqual(_escaped("Sales Order SO-0001"), "Sales Order SO-0001")


class TestAJoinerWithNothingToJoinIsStillMarked(IntegrationTestCase):
	"""T2. Every shape that is not a joiner doing its job. The virama is present in all of them,
	so each one isolates a single clause of the rule rather than the presence of Indic text."""

	def test_a_joiner_at_the_end_of_a_value_is_marked(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d"), "\u0dc1\u0dca\\u200d")

	def test_a_joiner_at_the_start_of_a_value_is_marked(self):
		self.assertEqual(_escaped("\u200d\u0dbb"), "\\u200d\u0dbb")

	def test_a_joiner_before_a_space_is_marked(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d \u0dbb"), "\u0dc1\u0dca\\u200d \u0dbb")

	def test_a_joiner_before_punctuation_is_marked(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d.\u0dbb"), "\u0dc1\u0dca\\u200d.\u0dbb")

	def test_a_doubled_joiner_is_marked_on_both_halves(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u200d\u0dbb"), "\u0dc1\u0dca\\u200d\\u200d\u0dbb")

	def test_a_long_run_of_joiners_is_marked_end_to_end(self):
		self.assertEqual(
			_escaped("\u0dc1\u0dca" + ZWJ * 8 + "\u0dbb"), "\u0dc1\u0dca" + "\\u200d" * 8 + "\u0dbb"
		)

	def test_a_joiner_with_no_virama_before_it_is_marked_however_indic_the_letters_are(self):
		self.assertEqual(_escaped("\u0dc1\u200d\u0dbb"), "\u0dc1\\u200d\u0dbb")

	def test_a_virama_from_one_script_does_not_license_a_joiner_before_another(self):
		"""Clause 4, and the hole the two front ends still have. A Devanagari virama sitting on a
		Latin letter forms no conjunct with a Latin `x`; the joiner between them is hiding."""
		self.assertEqual(_escaped("SO-000A\u094d\u200dx"), "SO-000A\u094d\\u200dx")
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u0937"), "\u0dc1\u0dca\\u200d\u0937")

	def test_a_joiner_before_a_vowel_sign_is_marked(self):
		"""Clause 3 ON ITS OWN, and the reason it is not redundant. U+0DCF is in the SAME Sinhala
		block as the virama, so clause 4 waves it through; it is a vowel sign and not a letter, and
		VIRAMA + JOINER + VOWEL SIGN spells nothing. The first version of this module had no test
		that failed when clause 3 was deleted — a guard whose failure nobody had watched."""
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u0dcf"), "\u0dc1\u0dca\\u200d\u0dcf")

	def test_a_joiner_before_a_second_virama_is_marked(self):
		"""Same block, same category as the character that licensed it, and still not a letter."""
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u0dca"), "\u0dc1\u0dca\\u200d\u0dca")

	def test_a_joiner_before_a_digit_of_the_same_script_is_marked(self):
		"""U+0DE6 SINHALA LITH DIGIT ZERO: same block, category Nd. A digit is not a consonant and
		a joiner cannot bind one — this is the shape that hides a difference between two amounts."""
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u0de6"), "\u0dc1\u0dca\\u200d\u0de6")

	def test_a_joiner_before_punctuation_of_the_same_script_is_marked(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u0df4"), "\u0dc1\u0dca\\u200d\u0df4")

	def test_a_bidi_override_beside_an_exempt_joiner_is_still_marked(self):
		"""The exception is for joiners and reaches nothing else. Here the joiner IS doing its job
		and stays; the override beside it is marked all the same."""
		self.assertEqual(_escaped(RTL + "\u0dc1\u0dca\u200d\u0dbb"), "\\u202e\u0dc1\u0dca\u200d\u0dbb")

	def test_an_override_immediately_after_a_virama_takes_no_exemption_from_it(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u202e\u0dbb"), "\u0dc1\u0dca\\u202e\u0dbb")

	def test_a_joiner_between_a_virama_and_an_override_is_marked(self):
		self.assertEqual(_escaped("\u0dc1\u0dca\u200d\u202e\u0dbb"), "\u0dc1\u0dca\\u200d\\u202e\u0dbb")


class TestTwoValuesAReaderCannotTellApartStillEscapeDifferently(IntegrationTestCase):
	"""T2's whole point: the exception must not dissolve the defence it is an exception to.

	The property the card rests on is that `_escaped` is INJECTIVE — two different values never
	produce the same displayed text — because that is what makes "what you see is what runs" true.
	An exception hands characters back unescaped, which is exactly how injectivity gets lost.
	"""

	PAIRS = (
		("\u0dc1\u0dca\u0dbb", "\u0dc1\u0dca\u200d\u0dbb"),  # the exempt joiner is the difference
		("paid", f"paid{ZWJ}"),
		("SO-0001", "SO-0001\u200d"),
		("\u0dc1\u0dca\u200d\u0dbb", "\u0dc1\u0dca\u200d\u200d\u0dbb"),
		("\u0dc1\u0dca\u200d\u0dbb", "\u0dc1\u0dca\u200c\u0dbb"),  # ZWJ and ZWNJ are not each other
		("\u0dc1\u0dca\u200d\u0dbb", "\u0dc1\u0dca\\u200d\u0dbb"),  # the word and its own escape
	)

	def test_no_two_of_them_escape_alike(self):
		for left, right in self.PAIRS:
			with self.subTest(left=repr(left), right=repr(right)):
				self.assertNotEqual(left, right)  # positive control: they really are two values
				self.assertNotEqual(_escaped(left), _escaped(right))

	def test_a_value_cannot_forge_the_escape_of_another(self):
		"""The backslash is escaped, so the six literal characters `\\u200d` can never be mistaken
		for the escape the card emits for a real joiner."""
		self.assertEqual(_escaped("\\u200d"), "\\\\u200d")

	def test_the_exemption_is_injective_over_a_swept_alphabet(self):
		"""The property rather than the six examples: every arrangement of a small alphabet that
		contains both joiners, a virama, two letters and a space maps to a distinct display."""
		alphabet = ("\u0dc1", "\u0dca", ZWJ, ZWNJ, "\u0dbb", " ")
		seen: dict[str, str] = {}
		for a in alphabet:
			for b in alphabet:
				for c in alphabet:
					value = a + b + c
					shown = _escaped(value)
					self.assertNotIn(shown, seen, f"{value!r} and {seen.get(shown)!r} display alike")
					seen[shown] = value


class TestTheRuleIsReadFromTheLibraryNotCopiedIntoIt(IntegrationTestCase):
	"""A hand-written virama list is a fourth table. `unicodedata` already knows them."""

	# The sixteen the two front ends write out by hand, quoted from `visibleControls.ts`.
	FRONT_END_VIRAMAS = (
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
	)

	def test_every_virama_the_front_ends_name_is_one_the_library_names(self):
		for cp in self.FRONT_END_VIRAMAS:
			with self.subTest(cp=hex(cp)):
				self.assertEqual(unicodedata.combining(chr(cp)), 9)

	def test_the_library_knows_more_of_them_than_the_front_ends_do(self):
		"""Not a boast: it is why the list is not copied. A hand-written sixteen leaves every other
		Indic script escaping its own conjuncts."""
		self.assertGreater(
			sum(1 for cp in range(0x110000) if unicodedata.combining(chr(cp)) == 9),
			len(self.FRONT_END_VIRAMAS),
		)

	def test_a_script_outside_the_front_ends_list_gets_its_conjunct_too(self):
		"""Telugu's virama U+0C4D is in their sixteen; Chakma's U+11133 is not, and the library
		knows it. Same rule, no new entry."""
		word = "\U00011103\U00011133\u200d\U00011104"
		self.assertEqual(unicodedata.combining("\U00011133"), 9)
		self.assertEqual(_escaped(word), word)


class TestTheValueThatRUNSIsUntouched(IntegrationTestCase):
	"""A normalisation may change what is DISPLAYED and never what is SENT.

	The whole change above is display-only, and "display-only" is a claim about the other end of the
	call, so it is measured there: a real gated tool, really approved, and the argument the function
	body actually received compared byte for byte with the argument the model produced. Reading
	`_escaped` cannot establish this, because the question is whether anything ELSE consumed its
	output on the way to the tool.

	THE POSITIVE CONTROL is the second test. A value whose joiner is NOT exempt is displayed with
	the escape in it and STILL arrives at the tool raw — so the first test is not passing merely
	because nothing on this path ever changes anything.
	"""

	def _agent_and_tool(self):
		from flow.lib.agent import Agent
		from flow.lib.tool import Tool

		received: list[str] = []

		def note(text: str) -> str:
			received.append(text)
			return "ok"

		gated = Tool(
			name="note",
			description="Write a note.",
			parameters={
				"type": "object",
				"properties": {"text": {"type": "string"}},
				"additionalProperties": False,
				"required": ["text"],
			},
			func=note,
			requires_confirmation=True,
			title="Write Note",
			# The templated body is the one that goes through `_escaped`. The UNtemplated fallback
			# does not, and `TestTheFallbackDumpIsADifferentDefectEntirely` below is why.
			confirm_template="Write {text}.",
		)
		return Agent, gated, received

	def _model(self, text: str):
		from flow.lib.model import ChatResponse, ToolCall

		class Scripted:
			model_id = "openai/gpt-4o-mini"

			def __init__(self, rs):
				self._rs = list(rs)

			def chat(self, messages, tools=None, *, stream=False):
				return self._rs.pop(0)

		usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
		pause = ChatResponse(
			content=None,
			tool_calls=[ToolCall(id="c1", name="note", arguments={"text": text})],
			finish_reason="tool_calls",
			usage=usage,
		)
		done = ChatResponse(content="done", finish_reason="stop", usage=usage)
		return Scripted([pause, done])

	def _run_and_approve(self, text: str):
		Agent, gated, received = self._agent_and_tool()
		agent = Agent(model=self._model(text), tools=[gated])
		paused = agent.run("write it")
		self.assertTrue(paused.paused)
		self.assertEqual(received, [])  # the question was asked before anything ran
		prompt = paused.questions[0].prompt
		agent.resume(paused.messages, {"c1": "Approve"})
		return prompt, received

	def test_an_exempt_joiner_reaches_the_tool_exactly_as_the_model_wrote_it(self):
		prompt, received = self._run_and_approve(SINHALA)
		self.assertEqual(received, [SINHALA])
		self.assertEqual(received[0].encode(), SINHALA.encode())
		self.assertIn(SINHALA, prompt)  # and the reader saw the word, not an escape

	def test_a_marked_joiner_is_marked_on_the_card_and_still_sent_raw(self):
		"""The positive control: display and wire disagree, which is the point of the whole file."""
		hidden = f"paid{ZWJ}unpaid"
		prompt, received = self._run_and_approve(hidden)
		self.assertEqual(received, [hidden])
		self.assertEqual(received[0].encode(), hidden.encode())
		self.assertIn("paid\\u200dunpaid", prompt)
		self.assertNotIn(hidden, prompt)


class TestTheFallbackDumpIsADifferentDefectEntirely(IntegrationTestCase):
	"""FOUND WHILE PROVING THE ABOVE, RECORDED, AND DELIBERATELY NOT FIXED HERE.

	`_confirmation_question` builds its fallback body as `json.dumps(call.arguments, indent=2,
	default=str)` (`flow/lib/agent.py`), and `json.dumps` defaults to `ensure_ascii=True`. So on
	the commonest card of all — a gated tool with neither a `confirm_prompt` nor a
	`confirm_template` — EVERY non-ASCII character is escaped, not merely the joiner: a Sinhala
	value arrives as twenty-odd backslash-u sequences and a Tamil one likewise. The fix above cannot
	reach it, because that dump never passes through `_escaped` at all.

	WHY IT IS NOT FIXED IN THIS COMMIT, and this is not reluctance:
	  - `_confirmation_question` is one of the four functions CLAUDE.md rule 4 protects: "no change
	    without a spec that names it." The brief for this run names the marking code and the
	    joiner; it does not name this function.
	  - The obvious one-word change is WRONG AND UNSAFE. `ensure_ascii=False` alone would put a
	    RIGHT-TO-LEFT OVERRIDE straight onto the card unescaped, because `ensure_ascii=True` is
	    today the only thing escaping it on this path. The correct change is to build the body from
	    `_quoted_argument` instead of from `json.dumps`, which is a design change to a load-bearing
	    function and needs its own spec.

	These two tests PIN the present behaviour so that nobody changes it by accident and so that the
	day someone writes that spec, the tests that must flip are already named.
	"""

	def _fallback_body(self, value: str) -> str:
		from flow.lib.agent import _confirmation_question
		from flow.lib.model import ToolCall
		from flow.lib.tool import Tool

		plain = Tool(
			name="note",
			description="Write a note.",
			parameters={"type": "object", "properties": {"text": {"type": "string"}}},
			func=lambda text: "ok",
			requires_confirmation=True,
		)
		call = ToolCall(id="c1", name="note", arguments={"text": value})
		return _confirmation_question(call, plain).prompt

	def test_a_sinhala_value_is_still_unreadable_on_an_untemplated_card(self):
		body = self._fallback_body(SINHALA)
		self.assertNotIn(SINHALA, body)
		self.assertIn("\\u0dc1", body)  # the FIRST LETTER of the word, escaped as machine text

	def test_the_same_card_with_a_sentence_shows_the_word_correctly(self):
		"""The positive control that makes the test above a finding rather than a fact of life:
		the templated half of the SAME question renders the word, so the dump is what is wrong."""
		from flow.lib.agent import _render_confirm_template

		self.assertEqual(_render_confirm_template("Write {text}.", {"text": SINHALA}), f'Write "{SINHALA}".')
