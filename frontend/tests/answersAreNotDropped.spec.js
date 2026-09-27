import { describe, it, expect } from "vitest";
import { answersPayload } from "@/lib/answers";

// THE ANSWER A PERSON CLICKED MUST REACH THE SERVER.
//
// `q.key` is a tool-call id that arrives from the model's own reply — set verbatim from the provider
// payload in `flow/lib/model.py` (`ToolCall(id=call_id, ...)`), with no filter for `__proto__`; only
// the empty/duplicate-id rule stands between the model and this key. On a PLAIN object,
// `answers["__proto__"] = "Approve"` invokes the prototype setter, which ignores a string: no own
// property is created, `JSON.stringify` sends `{}`, and an approval a person clicked is transmitted
// as though they never answered it.
//
// WHAT THIS FILE USED TO BE, AND WHY IT IS NOT THAT ANY MORE. It was a source-text grep — it read
// `store.js` and required the string `Object.create(null)` to appear in it. A reviewer left that
// exact line in place, added a plain object beside it, sent the plain one, and all 176 tests stayed
// green with the defect fully restored. The three lines now live in `@/lib/answers` so they can be
// CALLED, and every assertion below is on a returned payload rather than on a file's text.
describe("an approval keyed by an inherited name is not silently dropped", () => {
	it("THE DEFECT, on the construction itself — this is what makes the rest mean something", () => {
		// A fact about JavaScript that no fix of ours changes, and the reason the fix is needed.
		const plain = {};
		plain["__proto__"] = "Approve";
		plain["c1"] = "Deny";
		expect(Object.keys(plain)).toEqual(["c1"]);
		expect(JSON.stringify(plain)).toBe('{"c1":"Deny"}');
	});

	it("the approval survives the payload and survives being serialised", () => {
		const sent = JSON.parse(
			JSON.stringify(
				answersPayload([
					{ key: "__proto__", _answer: "Approve" },
					{ key: "c1", _answer: "Deny" },
				])
			)
		);

		expect(sent["__proto__"]).toBe("Approve");
		expect(sent["c1"]).toBe("Deny");
	});

	it("every other name JavaScript answers for on a plain object survives too", () => {
		// The same eight that broke the card's labels, because one of them is not a special case —
		// the whole of `Object.prototype` is the attack surface, and `__proto__` is only its most
		// famous member.
		const keys = [
			"__proto__",
			"constructor",
			"toString",
			"valueOf",
			"hasOwnProperty",
			"isPrototypeOf",
			"propertyIsEnumerable",
			"toLocaleString",
		];
		const sent = JSON.parse(
			JSON.stringify(answersPayload(keys.map((key) => ({ key, _answer: "Approve" }))))
		);

		expect(Object.keys(sent).sort()).toEqual([...keys].sort());
		for (const key of keys) expect(sent[key]).toBe("Approve");
	});

	it("the ordinary payload is unchanged, which is what stops the fix being a new bug", () => {
		// The control. A payload that dropped or renamed ordinary answers would pass every
		// assertion above.
		const sent = answersPayload([
			{ key: "call_abc", _answer: "Approve" },
			{ key: "call_def", _answer: "Deny" },
			{ key: "call_ghi", _answer: "use the draft instead" },
		]);

		expect(JSON.stringify(sent)).toBe(
			'{"call_abc":"Approve","call_def":"Deny","call_ghi":"use the draft instead"}'
		);
	});
});
