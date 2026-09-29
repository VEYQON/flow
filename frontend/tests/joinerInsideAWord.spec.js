import { describe, it, expect } from "vitest";
import { escapeForDisplay, displayText } from "@/lib/display";

// THE PANEL'S HALF OF THE SAME DEFECT.
//
// `flow/lib/agent.py` marks every code point carrying `Default_Ignorable_Code_Point`, and U+200D
// ZERO WIDTH JOINER carries it. In Sinhala, Tamil, Devanagari and every other Indic script the
// joiner is SPELLING, not decoration: a conjunct is written CONSONANT + VIRAMA + ZWJ + CONSONANT.
// This module is the port of the engine's fix, and it is a port for the reason the header of
// `lib/display.js` already gives — two escapers on one card would be two rules, and the first
// thing to drift.
//
// THE RULE, WHOLE, and identical to `_spells_rather_than_hides` in `flow/lib/agent.py`:
// a joiner (U+200C or U+200D) is kept as itself only when (1) it is at neither edge of the value,
// (2) a VIRAMA sits immediately before it, (3) a LETTER sits immediately after it, and (4) those
// two neighbours share a 128-code-point aligned block. Everything else is escaped as before.
//
// The specification both copies are measured against is `docs/one-rule-four-copies.md`, and
// `flow/tests/test_s24_one_rule_four_copies.py` fails if either copy drifts from it.

const ZWJ = "\u200d";
const ZWNJ = "\u200c";
const RTL = "\u202e";

const SINHALA = "\u0dc1\u0dca\u200d\u0dbb\u0dd3 \u0dbd\u0d82\u0d9a\u0dcf"; // "Sri Lanka"
const TAMIL = "\u0b95\u0bcd\u200d\u0bb7";
const DEVANAGARI = "\u0915\u094d\u200d\u0937";

describe("a conjunct survives the card", () => {
	it("leaves a Sinhala word exactly as it was written", () => {
		expect(escapeForDisplay(SINHALA)).toBe(SINHALA);
	});

	it("leaves a Tamil conjunct exactly as it was written", () => {
		expect(escapeForDisplay(TAMIL)).toBe(TAMIL);
	});

	it("leaves a Devanagari conjunct exactly as it was written", () => {
		expect(escapeForDisplay(DEVANAGARI)).toBe(DEVANAGARI);
	});

	it("keeps a zero-width NON-joiner that asks for the separate form", () => {
		const word = "\u0dc1\u0dca\u200c\u0dbb";
		expect(escapeForDisplay(word)).toBe(word);
	});

	it("shows the word UNQUOTED, because nothing about it needed escaping", () => {
		// The quote is the panel's signal that something was escaped or elided. A word that is
		// shown as itself must not wear one, or the signal stops meaning anything.
		expect(displayText(SINHALA)).toBe(SINHALA);
	});
});

describe("a joiner with nothing to join is still marked", () => {
	it("marks one at the end of a value", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u200d")).toBe("\u0dc1\u0dca\\u200d");
	});

	it("marks one at the start of a value", () => {
		expect(escapeForDisplay("\u200d\u0dbb")).toBe("\\u200d\u0dbb");
	});

	it("marks one before a space", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u200d \u0dbb")).toBe("\u0dc1\u0dca\\u200d \u0dbb");
	});

	it("marks both halves of a doubled joiner", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u200d\u200d\u0dbb")).toBe(
			"\u0dc1\u0dca\\u200d\\u200d\u0dbb"
		);
	});

	it("marks a run of them end to end", () => {
		expect(escapeForDisplay("\u0dc1\u0dca" + ZWJ.repeat(8) + "\u0dbb")).toBe(
			"\u0dc1\u0dca" + "\\u200d".repeat(8) + "\u0dbb"
		);
	});

	it("marks one with no virama before it, however Indic the letters are", () => {
		expect(escapeForDisplay("\u0dc1\u200d\u0dbb")).toBe("\u0dc1\\u200d\u0dbb");
	});

	it("marks one between two letters of ordinary Latin", () => {
		expect(escapeForDisplay(`paid${ZWJ}unpaid`)).toBe("paid\\u200dunpaid");
	});

	// Clause 3 ON ITS OWN. Each of these sits in the SAME block as the virama, so clause 4 admits
	// it; none of them is a letter, and VIRAMA + JOINER + (vowel sign | virama | digit) spells
	// nothing. Without these four, deleting clause 3 leaves the suite green.
	it("marks one before a vowel sign of the same script", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u200d\u0dcf")).toBe("\u0dc1\u0dca\\u200d\u0dcf");
	});

	it("marks one before a second virama", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u200d\u0dca")).toBe("\u0dc1\u0dca\\u200d\u0dca");
	});

	it("marks one before a digit of the same script", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u200d\u0de6")).toBe("\u0dc1\u0dca\\u200d\u0de6");
	});

	it("marks one whose virama belongs to another script entirely", () => {
		// Clause 4, and the hole the two sibling web apps still carry: a Devanagari virama on a
		// Latin letter forms no conjunct with a Latin `x`, so the joiner between them only hides.
		expect(escapeForDisplay("SO-000A\u094d\u200dx")).toBe("SO-000A\u094d\\u200dx");
		expect(escapeForDisplay("\u0dc1\u0dca\u200d\u0937")).toBe("\u0dc1\u0dca\\u200d\u0937");
	});
});

describe("the exception reaches joiners and nothing else", () => {
	it("still marks a right-to-left override standing next to an exempt joiner", () => {
		expect(escapeForDisplay(RTL + "\u0dc1\u0dca\u200d\u0dbb")).toBe(
			"\\u202e\u0dc1\u0dca\u200d\u0dbb"
		);
	});

	it("does not let a virama license an override that follows it", () => {
		expect(escapeForDisplay("\u0dc1\u0dca\u202e\u0dbb")).toBe("\u0dc1\u0dca\\u202e\u0dbb");
	});

	it("still marks an invisible LETTER, which is what run 15 was about", () => {
		expect(escapeForDisplay("SO-0001\u3164")).toBe("SO-0001\\u3164");
	});

	it("still marks every other zero-width character", () => {
		for (const cp of [0x200b, 0x2060, 0xfeff, 0x00ad, 0x180e]) {
			const ch = String.fromCodePoint(cp);
			expect(escapeForDisplay(`a${ch}b`)).not.toBe(`a${ch}b`);
		}
	});
});

describe("two values a reader cannot tell apart still display differently", () => {
	const PAIRS = [
		["\u0dc1\u0dca\u0dbb", "\u0dc1\u0dca\u200d\u0dbb"],
		["paid", `paid${ZWJ}`],
		["SO-0001", "SO-0001\u200d"],
		["\u0dc1\u0dca\u200d\u0dbb", "\u0dc1\u0dca\u200d\u200d\u0dbb"],
		["\u0dc1\u0dca\u200d\u0dbb", "\u0dc1\u0dca\u200c\u0dbb"],
		["\u0dc1\u0dca\u200d\u0dbb", "\u0dc1\u0dca\\u200d\u0dbb"],
	];

	it.each(PAIRS)("keeps %j and %j apart", (left, right) => {
		expect(left).not.toBe(right); // positive control: they really are two values
		expect(escapeForDisplay(left)).not.toBe(escapeForDisplay(right));
	});

	it("is injective over an alphabet that contains both joiners and a virama", () => {
		const alphabet = ["\u0dc1", "\u0dca", ZWJ, ZWNJ, "\u0dbb", " "];
		const seen = new Map();
		for (const a of alphabet)
			for (const b of alphabet)
				for (const c of alphabet) {
					const value = a + b + c;
					const shown = escapeForDisplay(value);
					expect(
						seen.has(shown),
						`${JSON.stringify(value)} collides with ${JSON.stringify(seen.get(shown))}`
					).toBe(false);
					seen.set(shown, value);
				}
	});
});

describe("the panel and the engine are one rule", () => {
	// The same cases the engine's own module asserts, so a divergence fails on BOTH sides rather
	// than sitting undetected in whichever copy nobody ran.
	it.each([
		[SINHALA, SINHALA],
		[TAMIL, TAMIL],
		[DEVANAGARI, DEVANAGARI],
		["\u0dc1\u0dca\u200d", "\u0dc1\u0dca\\u200d"],
		["\u200d\u0dbb", "\\u200d\u0dbb"],
		["paid\u200dunpaid", "paid\\u200dunpaid"],
		["gnp\u202eexe", "gnp\\u202eexe"],
		["Sales Order SO-0001", "Sales Order SO-0001"],
	])("agrees with flow/tests/test_s23 on %j", (input, expected) => {
		expect(escapeForDisplay(input)).toBe(expected);
	});
});
