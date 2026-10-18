<script lang="ts">
	/**
	 * /driver — "The Window" (Daylight, Gujarati-first).
	 *
	 * The 108 driver green-corridor flow: golden-hour counter always on
	 * screen, one large corridor request with the urgency pick, the fixed
	 * verb rail, the honest SCRIPTED FALLBACK badge (no live request
	 * endpoint is deployed — numbers are demo-scale), the granted map with
	 * the Jan Rakshak unit en_route→on_scene, the control-room line and
	 * the after-action minutes saved. Gujarati is the default language;
	 * the shell switch flips EN/HI/GU.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import GreenCorridorDriver from '$lib/citizen/GreenCorridorDriver.svelte';
	import '$lib/i18n/strings.citizen';

	onMount(() => {
		ops.lang = 'gu';
		ops.start();
	});

	onDestroy(() => {
		ops.stop();
	});

	// R3 wiring: Gujarati-first public surface; band from the shared store.
	$effect(() => {
		commandBar.lang = ops.lang;
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSource;
		commandBar.onLangChange = (l: string) => (ops.lang = l);
	});
</script>

<svelte:head>
	<title>The Window · Green Corridor</title>
</svelte:head>

<div class="driver-page">
	<GreenCorridorDriver />
</div>

<style>
	.driver-page {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
	}
</style>
