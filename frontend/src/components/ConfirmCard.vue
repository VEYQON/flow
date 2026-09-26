<script setup>
import { ref, computed, nextTick, onMounted } from "vue";
import { Button, FeatherIcon } from "@/lib/ui";
import ArgsView from "./ArgsView.vue";
import { confirmTitle, hasArgs, parseArgs, blockKeysFor } from "@/lib/toolMeta";
import { __ } from "@/lib/translate";

const props = defineProps({
	question: { type: Object, required: true },
	tool: { type: Object, default: null },
});
const emit = defineEmits(["answer"]);

const otherEl = ref(null);
const rootEl = ref(null);

// The engine composes the question as one head line, a blank line, then the body the tool wrote.
// Split it back apart exactly as it was joined, so the body is restored byte for byte, blank lines
// included.
const paragraphs = computed(() => props.question.prompt.split("\n\n"));
const engineHead = computed(() => (paragraphs.value[0] ?? "").trim());
const engineBody = computed(() => paragraphs.value.slice(1).join("\n\n").trim());

// Tool confirmation: name the specific action; free-text ask: show the prompt.
const confirm = computed(() =>
	props.tool ? confirmTitle(props.tool.name, props.tool.arguments) : null
);
// Only an approval question's head is the engine's own sentence. Every question the engine raises
// carries exactly these two options in this order — the same signal the server uses to recognise
// one — and a tool is allowed to return a Question of its own, whose first paragraph would be the
// tool's words rather than the engine's. No tool does that today; this keeps it from mattering if
// one ever does.
const isApprovalQuestion = computed(() => {
	const o = props.question.options;
	return Array.isArray(o) && o.length === 2 && o[0] === "Approve" && o[1] === "Deny";
});
const title = computed(() =>
	engineBody.value && (!confirm.value || isApprovalQuestion.value)
		? engineHead.value
		: confirm.value?.title ?? engineHead.value
);
const danger = computed(() => Boolean(confirm.value?.danger));
// The question the engine asked, whole, as text. It used to be blanked whenever there was a tool —
// which is always, on this path — so an approver read a label and a table of raw argument values and
// never the sentence the tool's author wrote. The fallback covers the one-paragraph question and the
// gated call with no arguments at all: both used to draw a card with nothing in it but two buttons.
const body = computed(
	() => engineBody.value || (confirm.value ? props.question.prompt.trim() : "")
);
// execute's description reaches the reader through the body now, not the title, so it is still right
// to drop it from the table below — for that reason rather than the old one.
const displayArgs = computed(() => {
	if (props.tool?.name !== "execute") return props.tool?.arguments;
	const { description, ...rest } = parseArgs(props.tool.arguments);
	return rest;
});
const showArgs = computed(() => Boolean(props.tool) && hasArgs(displayArgs.value));
const blockKeys = computed(() => blockKeysFor(props.tool?.name));
const answered = computed(() => props.question._answer !== undefined);

// Options are stable tokens ("Approve"/"Deny"); translate only for display.
// LLM-authored options pass through as-is.
function optLabel(opt) {
	if (opt === "Approve") return __("Approve");
	if (opt === "Deny") return __("Deny");
	return opt;
}

// A pause used to scroll the message list to its bottom. With the body capped at 220px that left
// the whole card on screen; with the cap gone (see the stylesheet) the bottom of the list is the
// button row, and a question taller than the panel would sit above the viewport — the approver would
// be shown Approve and Deny and not the text they answer for. So the card puts its own top on
// screen instead, and Approve cannot be reached without passing the question.
onMounted(() => {
	if (props.question._answer !== undefined) return;
	nextTick(() => rootEl.value?.scrollIntoView({ block: "start", behavior: "smooth" }));
});

function pick(option) {
	emit("answer", option);
}
function showOther() {
	props.question._showOther = true;
	nextTick(() => otherEl.value?.focus());
}
function cancelOther() {
	props.question._showOther = false;
	props.question._otherText = "";
}
function sendOther() {
	const text = props.question._otherText?.trim();
	if (text) emit("answer", text);
}
</script>

<template>
	<div
		ref="rootEl"
		class="rounded-lg border border-outline-gray-2 bg-surface-white p-2.5"
		:class="{ 'opacity-70': answered }"
	>
		<div class="flex items-center gap-2">
			<span
				class="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-surface-gray-2 text-ink-gray-7"
			>
				<FeatherIcon :name="danger ? 'alert-triangle' : 'shield'" class="h-3.5 w-3.5" />
			</span>
			<div class="break-words text-sm font-medium leading-snug text-ink-gray-9">
				{{ title }}
			</div>
		</div>

		<pre v-if="body" class="flow-confirm-body">{{ body }}</pre>
		<div v-if="showArgs" class="mt-2.5">
			<div
				class="mb-1.5 text-[11px] font-semibold uppercase leading-none tracking-wide text-ink-gray-4"
			>
				{{ __("Details") }}
			</div>
			<ArgsView :arguments="displayArgs" :block-keys="blockKeys" />
		</div>

		<div
			v-if="answered"
			class="mt-2 flex items-center justify-end gap-1.5 text-xs text-ink-gray-6"
		>
			<FeatherIcon name="check" class="h-3 w-3" />
			<span class="truncate">{{ optLabel(question._answer) }}</span>
		</div>

		<template v-else>
			<div v-if="!question._showOther" class="mt-2.5 flex flex-wrap justify-end gap-1.5">
				<Button
					v-for="opt in question.options"
					:key="opt"
					:variant="opt === 'Approve' ? 'solid' : 'outline'"
					theme="gray"
					:label="optLabel(opt)"
					@click="pick(opt)"
				/>
				<Button
					v-if="question.allow_other !== false"
					variant="ghost"
					:label="__('Other…')"
					@click="showOther"
				/>
			</div>

			<div v-else class="mt-2.5">
				<textarea
					ref="otherEl"
					v-model="question._otherText"
					rows="2"
					class="flow-textarea w-full resize-none rounded-md border border-outline-gray-2 bg-surface-white px-2.5 py-2 text-sm text-ink-gray-9 outline-none focus:border-outline-gray-3"
					:placeholder="__('Describe what you want instead…')"
					@keydown.enter.exact.prevent="sendOther"
					@keydown.esc="cancelOther"
				></textarea>
				<div class="mt-1.5 flex justify-end gap-1.5">
					<Button variant="ghost" :label="__('Cancel')" @click="cancelOther" />
					<Button
						variant="solid"
						theme="gray"
						:label="__('Send')"
						:disabled="!question._otherText?.trim()"
						@click="sendOther"
					/>
				</div>
			</div>
		</template>
	</div>
</template>

<style scoped>
/* Kill the desk's global blue focus ring on the free-text box. */
.flow-textarea:focus {
	border-color: var(--outline-gray-3);
	box-shadow: none;
	outline: none;
}
.flow-confirm-body {
	margin: 8px 0 0;
	padding: 8px 10px;
	/* No height cap and no "Show more": a surface showing part of the text being authorised reads
	   exactly like one showing all of it, and the part left out is the part somebody needed. A long
	   question makes a taller card and the list scrolls; `word-break` below is what stops one long
	   unbroken value widening it instead. */
	background: var(--surface-gray-1);
	border: 1px solid var(--outline-gray-1);
	border-radius: 6px;
	font-family: var(--font-stack-monospace, ui-monospace, monospace);
	font-size: 12.5px;
	line-height: 1.55;
	color: var(--ink-gray-8);
	white-space: pre-wrap;
	word-break: break-word;
}
</style>
