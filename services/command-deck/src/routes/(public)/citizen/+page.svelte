<script lang="ts">
	/**
	 * /citizen — "Your road, next 2 hours" (Daylight, trilingual).
	 *
	 * The public advisory: the SAME corridor-event state the operators see
	 * on /ops (live events first, labelled demo fixtures otherwise), on the
	 * CitizenShell. The page owns the command-bar fields a public surface
	 * can: language (wired to the shared ops lang so the shell switch and
	 * the advisory agree) and the city band (rendered by the shell as its
	 * chip; the in-page strip renders it plain-language — citizens never
	 * see the code).
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import CitizenAdvisory from '$lib/citizen/CitizenAdvisory.svelte';
	import '$lib/i18n/strings.citizen';

	onMount(() => {
		ops.start();
	});

	onDestroy(() => {
		ops.stop();
	});

	// R3 wiring: this page owns lang + band on the shared shell chrome.
	$effect(() => {
		commandBar.lang = ops.lang;
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSource;
		commandBar.onLangChange = (l: string) => (ops.lang = l);
	});
</script>

<svelte:head>
	<title>Corridor Alerts · Citizen Advisory</title>
</svelte:head>

<div class="citizen-page">
	<CitizenAdvisory />
</div>

<style>
	.citizen-page {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
	}
</style>
