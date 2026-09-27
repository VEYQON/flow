// Model-chosen text, made safe to READ. The panel's one escaping rule, and the only one it may have.
//
// The engine already has this rule: `flow/lib/agent.py` defines `_escaped` and exports it as
// `escape_for_display`, and says in as many words that a second escaper elsewhere in the codebase
// would be a second rule and the first thing to drift. That helper is **Python-side only**. It runs
// when the engine composes the approval question, so the question's own sentence arrives at the
// panel already escaped — and the arguments beside it do not, because they are shipped as data and
// rendered by the panel itself. There is no way for the panel to call the Python helper: the values
// it renders come out of its own store, not out of a request it makes per value.
//
// So the JS side needs the same rule in one place, and this module is that place: a faithful port,
// character for character, of `_escaped` and of `_for_display` in `flow/tools/builtins.py`. It is
// deliberately a port and not an improvement. If the two ever have to differ, the difference is a
// defect in one of them.
//
// WHY, in one sentence: Vue's mustache escapes HTML and nothing else, so a newline in a value draws
// a real line inside the approval card, a right-to-left override reorders the sentence around it
// while adding no character, and a zero-width joiner is invisible by definition. Each is a way to
// show a person a question other than the one being asked, on the one surface whose whole job is to
// be trustworthy.
import { __ } from "@/lib/translate";

// The whitelist, and it has to stay one. `\p{C}` is every Other category (Cc control, Cf format,
// Cn unassigned, Co private use, Cs surrogate) and `\p{Z}` every Separator (Zs space, Zl line,
// Zp paragraph) — exactly the `C*`/`Z*` test the engine makes with `unicodedata.category(ch)[0]`.
// The engine's docstring records how it got here: the rule began as the categories that looked
// dangerous, Cc and Cf, and that list missed U+2028 and U+2029, which every layout engine treats as
// line breaks. A whitelist means a later revision of Unicode cannot quietly add a new way through.
//
// `\p{Default_Ignorable_Code_Point}` is the third term and it was MISSING, which a reviewer caught
// by measuring rather than by reading: 267 code points carry that property and are in neither C*
// nor Z*, so every one of them came through raw AND UNQUOTED — without even the quote that is
// supposed to be the signal that something was escaped. The property means precisely what this
// module is defending against: "a conformant renderer paints nothing here". Four of the 267 are
// category `Lo`, LETTERS (U+115F, U+1160, U+3164, U+FFA0), so no rule about controls, formats,
// separators or combining marks was ever going to reach them, and `SO-0001` beside
// `SO-0001<U+3164>` is two different writes that a reader cannot tell apart.
//
// It takes in the variation selectors (U+FE00-FE0F, U+E0100-E01EF) as well, and that is a decision
// rather than an oversight: an argument holding an emoji written with U+FE0F now renders quoted,
// as `"...\ufe0f"`. Slightly uglier, and correct — the cost of the other choice is a class of
// invisible character judged safe by whoever last thought about it, which is the mistake this
// whitelist exists to make impossible. The escape is visible and reversible; nothing is stripped.
const CONTROL_OR_SEPARATOR = /[\p{C}\p{Z}\p{Default_Ignorable_Code_Point}]/u;

// How much escaped text the panel will show before it elides, per value.
//
// Looser than the engine's own caps (120 in `builtins.py`, 200 in `_quoted_argument`) and on purpose:
// those bound a SENTENCE a person reads in one breath, while this bounds a cell in the table a
// person consults to check the exact value that will execute. Too tight a cap here hides the data
// the table exists to show. What makes either number a cap rather than a guess is that the true
// length is always stated when anything is left out.
export const DISPLAY_LIMIT = 2000;

/**
 * Every character that could move the cursor, shown instead of obeyed.
 *
 * The backslash and the quote are escaped too. Without that, `\n` in the output could be either a
 * real line break or those two characters, and a quote inside a value could close the pair holding
 * it — the point of escaping is that the reader can tell exactly what the value was.
 */
export function escapeForDisplay(text) {
	let out = "";
	// `for…of` iterates by CODE POINT, as Python does, so a surrogate pair is one character and a
	// lone surrogate is one character that `\p{Cs}` matches.
	for (const ch of String(text)) {
		if (ch === "\\" || ch === '"') out += "\\" + ch;
		else if (ch === "\n") out += "\\n";
		else if (ch === "\r") out += "\\r";
		else if (ch === "\t") out += "\\t";
		else if (ch !== " " && CONTROL_OR_SEPARATOR.test(ch)) {
			const cp = ch.codePointAt(0);
			out +=
				cp <= 0xffff
					? "\\u" + cp.toString(16).padStart(4, "0")
					: "\\U" + cp.toString(16).padStart(8, "0");
		} else out += ch;
	}
	return out;
}

// A translated string cannot be allowed to open a line of its own: the desk reads these from
// Translation records, and one of those can carry a newline. `builtins.py` flattens for exactly this
// reason, and the string being flattened here is the one that says something was left out.
const oneLine = (text) => String(text).split(/\s+/).filter(Boolean).join(" ");

/**
 * One value, quoted, escaped, and capped — in that order, because the order is the point.
 *
 * ESCAPED FIRST, THEN CAPPED. Escaping only lengthens: one override becomes the six characters of an
 * escape sequence, so a cap of N measured on the raw value admits 6N characters of escape into the
 * card. Measuring the cap on what a person will actually read is what makes it a cap.
 *
 * The count goes OUTSIDE the closing quote (run 12's M1). Inside it, a value whose own text read
 * `short… (9999 characters in all)` was byte-identical to a genuinely elided 9 999-character value,
 * so a reader could not tell 33 characters from 9 999. `escapeForDisplay` maps `"` to `\"`, so an
 * UNESCAPED quote is a character no value can produce: it is the boundary, and everything after it
 * is the panel speaking rather than the value.
 */
export function quoteForDisplay(value, limit = DISPLAY_LIMIT) {
	const text = String(value);
	const shown = [];
	let used = 0;
	for (const ch of text) {
		const escaped = escapeForDisplay(ch);
		if (used + escaped.length > limit) {
			const count = oneLine(__("… ({0} characters in all)", [[...text].length]));
			return `"${shown.join("")}" ${count}`;
		}
		shown.push(escaped);
		used += escaped.length;
	}
	return `"${shown.join("")}"`;
}

/**
 * Text as a person should meet it: ITSELF when that is safe, and quoted when it is not.
 *
 * "Safe" is not "short": it means escaping would change nothing about this value AND the whole of it
 * fits. The consequence is the property the whole surface rests on — **the appearance of a quote is
 * itself the signal that something was escaped or elided.** An ordinary card looks exactly as it did
 * before this module existed; a hostile value is visibly marked as one, by a character it cannot
 * produce.
 *
 * It is NOT idempotent, and the comment that used to claim it was has been removed as false: this
 * escaper maps `\\` to `\\\\`, so text that already came out of the engine's `_escaped` is escaped a
 * second time and quoted. Never apply it to engine-escaped text — the question's own sentence arrives
 * at the panel already escaped, and it is rendered as it arrived, by nobody's second rule.
 */
export function displayText(value, limit = DISPLAY_LIMIT) {
	const text = String(value);
	if (text.length <= limit && escapeForDisplay(text) === text) return text;
	return quoteForDisplay(text, limit);
}

/**
 * One scalar argument value, ready to read.
 *
 * The type handling is `formatScalar`'s, unchanged, and the reason it survives is the reason
 * `_display_json` gives for the same decision in the engine: a type that cannot carry an escape is
 * shown as ITSELF, not as text. Quoting everything made a cleared field indistinguishable from a
 * model writing the four letters "None", and a quantity of 3 indistinguishable from the character
 * "3" — different writes, and the approval surface has to tell them apart. Only a string can hide a
 * control character inside it, and only a string is quoted.
 *
 * A number is shown as itself only when that is safe. In JavaScript that is a weaker condition than
 * in Python — `JSON.parse` cannot produce a 3 000-digit integer or a bigint, so the only unsafe
 * numbers are `NaN` and `±Infinity`, which are not JSON but which model output supplies anyway.
 */
export function displayScalar(value, limit = DISPLAY_LIMIT) {
	if (value === null || value === undefined || value === "") return "—";
	if (typeof value === "boolean") return value ? __("Yes") : __("No");
	if (typeof value === "object") return "—"; // empty {} / [] — non-empty renders elsewhere
	if (typeof value === "number")
		return Number.isFinite(value) ? String(value) : quoteForDisplay(String(value), limit);
	return displayText(String(value), limit);
}
