import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import ConfirmCard from "@/components/ConfirmCard.vue";

// Every test in this directory mounts through here, for one reason that is easy to get wrong and
// silently fatal:
//
// The panel's CSS is run through `postcss-prefix-selector` (see `postcss.config.js`), so EVERY rule
// ships as `#flow-root .flow-confirm-body[data-v-…]` — the desk loads the bundle into its own <head>
// and the prefix is what stops frappe-ui's preflight bleeding onto the surrounding page. In
// production the card is therefore always inside `#flow-root` (`main.js` creates
// `div#flow-root` and mounts the app into it).
//
// A plain `mount()` attaches to a bare `document.body`, so NONE of those rules match, and
// `getComputedStyle` answers "" for every property the stylesheet sets. A height-cap assertion
// written against that mount would read `maxHeight === ""` and pass whatever the stylesheet said —
// a green that means "no CSS was applied at all". So the container is part of the fixture.
export function mountCard(props) {
	const host = document.createElement("div");
	host.id = "flow-root";
	document.body.appendChild(host);
	return mount(ConfirmCard, { attachTo: host, props });
}

// `onMounted` defers the scroll into a `nextTick`; two ticks lets it land.
export async function settle() {
	await nextTick();
	await nextTick();
}

// The engine composes an approval question as one head line, a blank line, then the tool's body.
// `flow/tools/builtins.py` is what writes it; these helpers build the same shape so a test states
// what the engine produces rather than a hand-made string that resembles it.
export function approval(prompt, tool = null) {
	return { question: { prompt, options: ["Approve", "Deny"] }, tool };
}
