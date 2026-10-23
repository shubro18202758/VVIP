<script lang="ts">
	/**
	 * /chat — the agent console (operator, command theme).
	 *
	 * THE takeover page (R7): (legacy)/chat is deleted in the same atomic
	 * step that creates this route, so /chat never resolves twice and never
	 * loses its feed. The console keeps the real NDJSON streamChat wiring
	 * (POST /api/convoy/chat/stream) on the new design system; its context
	 * header is the SELECTED EVENT from the ops store (sent as event_id,
	 * contract C-D2). The agent's canvas directives queue on the canvas bus
	 * and wait on City Command for a human to apply them.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import ChatConsole from '$lib/comms/ChatConsole.svelte';
	import '$lib/i18n/strings.citizen';
	import '$lib/i18n/packs.agent';

	onMount(() => {
		ops.start();
	});

	onDestroy(() => {
		ops.stop();
	});

	// R3 wiring: operator surface — lang + band on the shell.
	$effect(() => {
		commandBar.lang = ops.lang;
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSource;
		commandBar.onLangChange = (l: string) => (ops.lang = l);
	});
</script>

<svelte:head>
	<title>Agent Console · Veer</title>
</svelte:head>

<div class="chat-page">
	<ChatConsole lang={ops.lang} />
</div>

<style>
	.chat-page {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
</style>
