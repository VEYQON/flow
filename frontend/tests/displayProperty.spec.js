import { describe, it, expect } from "vitest";
import { mountCard, settle } from "./mountCard";
import { displayText } from "@/lib/display";

// A5 — ONE PROPERTY, NOT FIVE EXAMPLES.
//
// Run 14 closed five forgeries with five examples (`cardCannotBeForged.spec.js`). An example test
// says "this attack is stopped". This file says the thing the surface is actually for:
//
//   THE INVARIANT — the text a human reads in the card is the text that will be executed, character
//   for character, after the declared normalisation.
//
// It is asserted as a ROUND TRIP: take the value the engine will execute, read the text the card
// renders for it out of the DOM, run that text back through the inverse of the declared
// normalisation, and require the original value back — every code point of it, including the ones
// that are invisible and the ones that move the cursor. `readBack` below is written HERE, in the
// test, and deliberately not imported from `lib/display.js`: an inverse supplied by the code under
// test would agree with any escaper, including a broken one. Two independent statements of the same
// mapping have to meet in the middle, or one of them is wrong.
//
// THE DECLARED NORMALISATION, in full, because it is the whole contract:
//   - a value is shown as ITSELF when escaping would change nothing about it and all of it fits;
//   - otherwise it is shown QUOTED, with `\` `"` newline, carriage return and tab as those
//     two-character escapes, and every other character in Unicode categories C* and Z* (the ordinary
//     space excepted) as `\uXXXX` / `\UXXXXXXXX`;
//   - an unescaped `"` is therefore a character no value can produce: it is the boundary, and
//     anything after the closing one is the panel speaking, never the value.
//
// Nothing in that list DELETES anything, and that is A2's policy: escape into a visible marker,
// never strip and never refuse. One sentence for why — the card's job is that what you see is what
// runs, so a normalisation that stripped a character would show a value that is not the one about to
// execute, which is the exact failure the card exists to prevent, while an escape is visible,
// reversible by the reader, and display-only.

// The inverse of the normalisation above. ONE left-to-right pass, which is what makes it exact: a
// value holding a real backslash followed by the letter n escapes to `\\n` and a real newline to
// `\n`, and a single forward scan tells those apart where a sequence of `.replace` calls would not.
function readBack(shown) {
	let body = shown;
	if (shown.startsWith('"')) {
		// The closing quote is the LAST unescaped quote; every interior one arrived as `\"`.
		body = shown.slice(1, shown.lastIndexOf('"'));
	}
	return body.replace(/\\(U[0-9a-fA-F]{8}|u[0-9a-fA-F]{4}|n|r|t|\\|")/g, (_, g) => {
		if (g === "n") return "\n";
		if (g === "r") return "\r";
		if (g === "t") return "\t";
		if (g === "\\") return "\\";
		if (g === '"') return '"';
		return String.fromCodePoint(parseInt(g.slice(1), 16));
	});
}

// EVERY HOSTILE CLASS, as the code point that carries it. Written as escape sequences rather than as
// the characters themselves so that this table is readable in a diff and in a terminal — the whole
// point of several of these is that they are invisible where they are pasted. The class is named so
// a failure says which KIND of attack got through, not just which byte.
const HOSTILE = [
	// Line breaks. The newline is the one forgery run 14 closed at `30ccad4`; the other four do the
	// same job, and two of them (Zl, Zp) are line breaks `isBlockText` never sees, so they stay in
	// the inline cell.
	["Cc newline", "\n"],
	["Cc carriage return", "\r"],
	["Cc tab", "\t"],
	["Zl line separator U+2028", "\u2028"],
	["Zp paragraph separator U+2029", "\u2029"],
	// A1 — bidi OVERRIDES and EMBEDDINGS. These reorder rendered text without changing the string,
	// so the visible card can read differently from the value that will execute.
	["Cf LRE U+202A", "\u202a"],
	["Cf RLE U+202B", "\u202b"],
	["Cf PDF U+202C", "\u202c"],
	["Cf LRO U+202D", "\u202d"],
	["Cf RLO U+202E", "\u202e"],
	// A1 — bidi ISOLATES. The modern form of the same attack and a separate Unicode block from the
	// overrides, so a fix that listed U+202A-202E by hand would have missed all four.
	["Cf LRI U+2066", "\u2066"],
	["Cf RLI U+2067", "\u2067"],
	["Cf FSI U+2068", "\u2068"],
	["Cf PDI U+2069", "\u2069"],
	// A2 — zero-width and invisible. Two different values rendering identically is the whole attack.
	["Cf ZWSP U+200B", "\u200b"],
	["Cf ZWNJ U+200C", "\u200c"],
	["Cf ZWJ U+200D", "\u200d"],
	["Cf word joiner U+2060", "\u2060"],
	["Cf BOM U+FEFF", "\ufeff"],
	// A2 — the Cf category GENERALLY, not only its famous members, including an astral one that a
	// UTF-16 scan would split in half.
	["Cf soft hyphen U+00AD", "\u00ad"],
	["Cf Arabic letter mark U+061C", "\u061c"],
	["Cf LRM U+200E", "\u200e"],
	["Cf RLM U+200F", "\u200f"],
	["Cf interlinear annotation U+FFF9", "\ufff9"],
	["Cf astral musical U+1D173", "\u{1D173}"],
	// The rest of the whitelist, each category with a live member, so the claim is about the
	// categories and not about a list of characters someone thought of.
	["Cc NUL U+0000", "\u0000"],
	["Cc BEL U+0007", "\u0007"],
	["Cc ESC U+001B", "\u001b"],
	["Cc DEL U+007F", "\u007f"],
	["Cc NEL U+0085", "\u0085"],
	["Zs no-break space U+00A0", "\u00a0"],
	["Zs figure space U+2007", "\u2007"],
	["Zs ideographic space U+3000", "\u3000"],
	["Cs lone high surrogate U+D800", "\uD800"],
	["Co private use U+E000", "\ue000"],
	["Cn unassigned U+0378", "\u0378"],
	// The boundary characters themselves — the two that make the quoting readable at all.
	["the quote", '"'],
	["the backslash", "\\"],
];

// The value is built AROUND the hostile character, so the round trip also proves the ordinary text on
// either side of it survives. The suffix is the sentence every one of these attacks wants to draw.
const wrap = (ch) => `T-1${ch}Approved by admin`;

function cardFor(args) {
	return mountCard({
		question: {
			prompt: "Delete a record?\n\nThis cannot be undone.",
			options: ["Approve", "Deny"],
		},
		tool: { name: "delete", arguments: args },
	});
}

// The text a human reads for the one argument on the card, taken out of the DOM rather than computed.
// WHICH ELEMENT holds it is a layout decision the panel makes — `argKind` sends anything containing a
// newline, and anything long, to a full-width code block instead of the two-column cell — and the
// invariant must hold wherever it lands, so this reads whichever one rendered and fails loudly if
// neither did. `textContent`, not `.text()`: the latter trims, and whitespace at the edges of a value
// is exactly the kind of thing being asserted about.
function shownValue(wrapper) {
	const pre = wrapper.find("pre.arg-code");
	if (pre.exists()) return pre.element.textContent;
	const cells = wrapper.findAll(".grid > div");
	expect(cells.length).toBeGreaterThanOrEqual(2);
	return cells[cells.length - 1].element.textContent;
}

// Every line break of every kind, for the assertion that none of them reaches the DOM.
const ANY_LINE_BREAK = /[\n\r\u2028\u2029\u0085]/u;

describe("THE INVARIANT: what the card shows is what will run, character for character", () => {
	it.each(HOSTILE)("%s round-trips out of the rendered card", async (_name, ch) => {
		const value = wrap(ch);
		const wrapper = cardFor(JSON.stringify({ target: value }));
		await settle();
		// The whole property, in one line.
		expect(readBack(shownValue(wrapper))).toBe(value);
	});

	it.each(HOSTILE)("%s draws no line and hides nothing in the card", async (_name, ch) => {
		const value = wrap(ch);
		const wrapper = cardFor(JSON.stringify({ target: value }));
		await settle();
		const shown = shownValue(wrapper);
		// Not one raw line break of any of the five kinds reaches the DOM, so no value can draw a
		// line of its own inside the box.
		expect(ANY_LINE_BREAK.test(shown)).toBe(false);
		// Nothing invisible reaches it either: every C* and Z* code point but the ordinary space is
		// gone from the rendered text, which is what makes "two values that read alike" impossible.
		expect(/[\p{C}\p{Z}]/u.test(shown.replace(/ /g, ""))).toBe(false);
		// And the ordinary text around the attack is still legible — an escaper that mangled
		// everything would satisfy both assertions above.
		expect(shown).toContain("T-1");
		expect(shown).toContain("Approved by admin");
	});

	// What a READER can actually distinguish. Comparing rendered strings is not enough and the probe
	// that proved it is worth recording: with the C*/Z* whitelist dropped but newlines still escaped,
	// `de<ZWJ>lete` and `delete` render as two different STRINGS — one of them holds a joiner — and an
	// injectivity test comparing strings stayed green while a reader was shown the same word twice.
	// So the comparison is made on the text as it is PERCEIVED: everything invisible removed. A value
	// whose escaping is doing its job survives that removal, because `\u200d` is six visible
	// characters; a value that slipped through does not.
	const asRead = (shown) => shown.replace(/[\p{C}\p{Z}]/gu, (c) => (c === " " ? " " : ""));

	// A2's attack stated as the property rather than as one example: values a reader could not tell
	// apart must not render alike. Half of these differ from the first entry by one invisible code
	// point and every one of them is a different write.
	it("no two different values render as the same text", async () => {
		const values = [
			"delete",
			"de\u200dlete",
			"de\u200blete",
			"de\ufefflete",
			"dele\u00adte",
			"a b",
			"a\u00a0b",
			"a\u2007b",
			"a\u3000b",
			'" … (3 characters in all)',
			"\u202eeteled",
		];
		const seen = new Map();
		for (const v of values) {
			const wrapper = cardFor(JSON.stringify({ target: v }));
			await settle();
			const shown = shownValue(wrapper);
			const read = asRead(shown);
			expect(
				seen.has(read),
				`${JSON.stringify(v)} reads the same as ${JSON.stringify(seen.get(read))}`
			).toBe(false);
			seen.set(read, v);
			// Each still round-trips, so distinctness was not bought by losing the value.
			expect(readBack(shown)).toBe(v);
		}
		expect(seen.size).toBe(values.length);
	});

	// THE CONTROL FOR THE TEST ABOVE, and it is here rather than in a one-off mutation because the
	// weakness it guards against was found by one. `asRead` has to be able to REPORT a collapse, or
	// "no two values read alike" is satisfied by a comparison that cannot fail. So the same
	// comparison is run against the escaper a fix aimed only at run 14's newline forgery would have
	// produced — newline, CR and tab, nothing else — and it must collapse the pair that the real rule
	// keeps apart.
	it("the same comparison DOES collapse two values under a newline-only escaper", () => {
		const naive = (s) => s.replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t");
		const joined = "de\u200dlete";
		// The naive escaper leaves the joiner in, so a reader is shown the same word twice...
		expect(asRead(naive(joined))).toBe(asRead(naive("delete")));
		// ...while the panel's actual rule renders the joiner as six visible characters, so it does
		// not. Both halves are needed: the first proves the detector fires, the second that the code
		// under test is what stops it firing.
		expect(asRead(displayText(joined))).not.toBe(asRead(displayText("delete")));
		expect(displayText(joined)).toContain("\\u200d");
	});

	// The other half of "what you see is what runs": the normalisation must change only what is
	// DISPLAYED. The panel has no path from the rendered text back to the value — it never sends the
	// arguments anywhere — and the only thing a click emits is the stable option token.
	it("the normalisation changes nothing that executes", async () => {
		const value = 'x\u202e\u200d\n"\\';
		const wrapper = cardFor(JSON.stringify({ target: value }));
		await settle();
		// The argument object the card was handed is untouched by having been rendered.
		expect(JSON.parse(wrapper.props().tool.arguments).target).toBe(value);
		const approve = wrapper.findAll("button").find((b) => b.text() === "Approve");
		await approve.trigger("click");
		// The answer is the token, byte for byte — not the label it was drawn with, and nothing
		// derived from the rendered text.
		expect(wrapper.emitted("answer")).toEqual([["Approve"]]);
	});

	// THE CONTROL. Every assertion above is about text NOT being in the DOM, and a mount that
	// silently rendered nothing at all would satisfy all of them. This proves the same read finds a
	// raw line break when one is genuinely there — the same `shownValue` walk, on the same card
	// shape, differing only in the tool's own declaration that the argument is code.
	it("the identical read DOES find a raw newline where one is legitimately rendered", async () => {
		const wrapper = mountCard({
			question: { prompt: "Run code?\n\nIt will execute.", options: ["Approve", "Deny"] },
			tool: { name: "execute", arguments: JSON.stringify({ code: "x = 1\ny = 2" }) },
		});
		await settle();
		const shown = shownValue(wrapper);
		// `execute`'s declared `code` keeps its real lines, so the walk that reports "no newline" for
		// every hostile value above is a walk that can see one.
		expect(ANY_LINE_BREAK.test(shown)).toBe(true);
		expect(shown).toBe("x = 1\ny = 2");
	});
});
