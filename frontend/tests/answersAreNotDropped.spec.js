import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// THE ANSWER A PERSON CLICKED MUST REACH THE SERVER.
//
// `store.js`'s `answerQuestion` collects the answers into one object keyed by `q.key` and sends it.
// `q.key` is a tool-call id that arrives from the model's own reply, and on the resume path
// `prepareQuestions` spreads whole question rows out of a stored JSON blob without validating a field
// of them. On a PLAIN object, `answers["__proto__"] = "Approve"` invokes the prototype setter, which
// ignores a string: no own property is created, `JSON.stringify` sends `{}`, and an approval a person
// clicked is transmitted as though they never answered it.
//
// This is the same inherited-key defect the display side had, on the surface that carries the
// DECISION rather than the picture of it — and worse there, because the card failed loudly while this
// fails silently and in the direction of doing nothing.
//
// WHAT THIS FILE IS HONEST ABOUT: the first test is the DEFECT, demonstrated on the two constructions
// themselves — it is what makes the second test mean something, and it is a fact about JavaScript that
// no fix of ours changes. The second reads `store.js` and requires the safe construction at the one
// site that matters. A source read proves no pixel and no behaviour, and is weaker than a mount; the
// mount is not written here because `store.js` has no test harness in this repo (it opens API calls on
// import), and building one is a spec rather than a patch. Recorded as the gap it is.
describe("an approval keyed by an inherited name is not silently dropped", () => {
	it("THE DEFECT: a plain object drops it, and says nothing", () => {
		const plain = {};
		plain["__proto__"] = "Approve";
		plain["c1"] = "Deny";
		// The answer is simply gone — not an error, not a warning, just absent from the payload.
		expect(Object.keys(plain)).toEqual(["c1"]);
		expect(JSON.stringify(plain)).toBe('{"c1":"Deny"}');

		// And the construction the fix uses keeps it, with a payload that is otherwise identical.
		const safe = Object.create(null);
		safe["__proto__"] = "Approve";
		safe["c1"] = "Deny";
		expect(Object.keys(safe).sort()).toEqual(["__proto__", "c1"]);
		expect(JSON.parse(JSON.stringify(safe))["__proto__"]).toBe("Approve");
	});

	it("store.js builds the answers payload with the construction that keeps it", () => {
		// `process.cwd()` is the repo root under this runner; `import.meta.url` is not a file URL
		// once vite has transformed the module, which is why this is not the obvious spelling.
		const src = readFileSync(resolve(process.cwd(), "frontend/src/store.js"), "utf8");
		// The one site: the object the answers are collected into before `resume(answers, msg)`.
		expect(src).toContain("const answers = Object.create(null);");
		expect(src).not.toContain("const answers = {};");
		// The cache keyed by an Agent name went the same way, for the same reason.
		expect(src).toContain("const toolApprovalCache = new Map();");
		expect(src).not.toContain("const toolApprovalCache = {};");
	});
});
