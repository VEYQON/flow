// Tool name/args → plain-English labels, context, and value-shape helpers.
import { __ } from "@/lib/translate";
import { displayText, displayScalar } from "@/lib/display";

export function parseArgs(args) {
	if (!args) return {};
	if (typeof args === "object") return args;
	try {
		return JSON.parse(args);
	} catch {
		return {};
	}
}

// The raw argument string when it can't be parsed into fields, so a malformed or
// truncated payload is shown verbatim rather than silently dropped. "" otherwise.
export function rawArgs(args) {
	if (typeof args !== "string" || !args.trim()) return "";
	try {
		JSON.parse(args);
		return "";
	} catch {
		return args;
	}
}

// snake_case / a name → "Snake case".
//
// The argument LABELS on the approval card come through here, and they are as model-chosen as the
// values beside them: they are the keys of the JSON object the model emitted for its own tool call.
// A key can therefore carry a bidi override, or be 50 000 characters long, so the humanized name
// goes through the one display rule — which returns an ordinary key unchanged, so no label a person
// reads today looks any different.
export function humanize(name) {
	return displayText(
		String(name || "")
			.replace(/_/g, " ")
			.replace(/^./, (c) => c.toUpperCase())
	);
}

// Present-tense label per builtin; custom tools fall back to a humanized name.
const LABELS = {
	find_doctypes: "Finding relevant DocTypes",
	describe: "Reading DocType Meta",
	read: "Reading DocType Records",
	search_knowledge: "Searching Knowledge",
	execute: "Executing",
	create: "Creating Records",
	update: "Updating Records",
	delete: "Deleting Records",
	run_action: "Running Document Actions",
};

export function toolLabel(name) {
	return LABELS[name] ? __(LABELS[name]) : humanize(name);
}

// Strip leaked model special tokens (e.g. "describe<|channel|>commentary") so
// labels and approval lookups see the real tool name.
export function normalizeToolName(name) {
	const clean = String(name || "")
		.split("<|")[0]
		.trim();
	return clean || String(name || "").trim();
}

export const hasArgs = (args) => Object.keys(parseArgs(args)).length > 0 || Boolean(rawArgs(args));

// The error message when a tool call wholly failed, else null. Two failure shapes:
// a thrown tool → {error: "..."}; a bulk create/update/delete where every record
// failed → {created|updated|deleted: [], failures: [{error}, ...]}.
export function toolError(result) {
	if (typeof result !== "string") return null;
	let parsed;
	try {
		parsed = JSON.parse(result);
	} catch {
		return null; // a plain-text result, not an error payload
	}
	if (!parsed || typeof parsed !== "object") return null;
	if (typeof parsed.error === "string") return parsed.error;

	const failures = parsed.failures;
	if (Array.isArray(failures) && failures.length) {
		const succeeded = [parsed.created, parsed.updated, parsed.deleted].some(
			(a) => Array.isArray(a) && a.length
		);
		if (!succeeded) {
			const msg = failures
				.map((f) => f && f.error)
				.filter((e) => typeof e === "string")
				.join("\n");
			return msg || null;
		}
	}
	return null;
}

export const isScalar = (v) => v === null || typeof v !== "object";

// Every scalar argument value on the approval card becomes text HERE, and nowhere else: the
// two-column cell, the tuple inside a filter condition, and every chip in a list all call this one
// function. So this is where the escaping belongs — one call, covering four render sites, rather than
// four call sites that can drift apart. `displayScalar` keeps this function's own type handling
// unchanged and adds the rule for the one type that can hide a control character inside it.
export function formatScalar(v) {
	return displayScalar(v);
}

// Text that must render as a full-width code block rather than inline: multi-line
// or long. Anything with a newline, or a single line past the limit.
const LONG_TEXT_LIMIT = 120;
export const isBlockText = (v) =>
	typeof v === "string" && (v.includes("\n") || v.length > LONG_TEXT_LIMIT);

const FILTER_OPERATORS = new Set([
	"=",
	"!=",
	">",
	"<",
	">=",
	"<=",
	"like",
	"not like",
	"in",
	"not in",
	"between",
	"is",
]);

// A Frappe filter condition: ["in", [...]] / ["like", "%x%"].
const isFilterTuple = (v) =>
	v.length === 2 &&
	typeof v[0] === "string" &&
	FILTER_OPERATORS.has(v[0].toLowerCase()) &&
	(isScalar(v[1]) || (Array.isArray(v[1]) && v[1].every(isScalar)));

// Rendering shape of an argument value (see ArgValue.vue).
export function argKind(v) {
	if (v === null || v === undefined || v === "") return "empty";
	if (Array.isArray(v)) {
		if (!v.length) return "empty";
		if (isFilterTuple(v)) return "tuple";
		return v.every(isScalar) ? "list" : "records";
	}
	if (typeof v === "object") return Object.keys(v).length ? "object" : "empty";
	if (isBlockText(v)) return "code";
	return "scalar";
}

const TITLE_KEYS = ["title", "name", "subject", "label", "slug"];

// The key used as a record list's title column: a title-ish key on the first
// record, else its first non-empty scalar field.
export function recordLabelKey(records) {
	const first = records.find((r) => r && typeof r === "object" && !Array.isArray(r));
	if (!first) return null;
	for (const key of TITLE_KEYS) {
		if (typeof first[key] === "string" && first[key] && !isBlockText(first[key])) return key;
	}
	const entry = Object.entries(first).find(
		([, v]) => isScalar(v) && v !== null && v !== "" && !isBlockText(v)
	);
	return entry ? entry[0] : null;
}

// Args to always render as a code block regardless of content, keyed by tool name.
// execute's "code" is Python source even when short/single-line.
const CODE_ARG_KEYS = { execute: new Set(["code"]) };
export const blockKeysFor = (name) => CODE_ARG_KEYS[name] || new Set();

// Muted suffix that distinguishes a step (which doctype / search / action).
export function toolContext(args) {
	const a = parseArgs(args);
	for (const key of ["doctype", "search", "action"]) {
		const v = a[key];
		if (typeof v === "string" && v) return key === "action" ? humanize(v) : v;
	}
	return null;
}

const pick = (one, many, n, doctype) => (n === 1 ? __(one, [doctype]) : __(many, [n, doctype]));

// What exactly is being approved, derived from the call's own arguments.
// The headline sits ABOVE `pre.flow-confirm-body`, so S21's D0 escaping — which the engine applies
// when it composes the question — never reached it. Every value substituted into it is model-chosen:
// the doctype, the action, and `execute`'s own `description`. They are escaped one by one, at the
// point of substitution, rather than by escaping the composed line: the line itself is a translated
// literal that carries quotes of its own (`Run "{0}" on {1}`), and escaping it whole would put a
// backslash in front of those.
export function confirmTitle(name, args) {
	const a = parseArgs(args);
	const doctype = typeof a.doctype === "string" ? displayText(a.doctype) : "";
	const count = (v) => (Array.isArray(v) && v.length ? v.length : 1);
	let title = null;
	if (name === "create" && doctype)
		title = pick("Create 1 {0} record", "Create {0} {1} records", count(a.records), doctype);
	else if (name === "update" && doctype)
		title = pick("Update 1 {0} record", "Update {0} {1} records", count(a.names), doctype);
	else if (name === "delete" && doctype)
		title = pick("Delete 1 {0} record", "Delete {0} {1} records", count(a.names), doctype);
	else if (name === "run_action" && typeof a.action === "string" && a.action)
		title = __('Run "{0}" on {1}', [
			humanize(a.action),
			doctype ? `${count(a.names)} ${doctype}` : __("records"),
		]);
	else if (name === "execute")
		title =
			(typeof a.description === "string" &&
				a.description.trim() &&
				displayText(a.description.trim())) ||
			__("Run Python code");
	return { title: title || toolLabel(name), danger: name === "delete" };
}
