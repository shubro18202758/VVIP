<script lang="ts">
	/**
	 * /driver — "The Window" (Daylight, Gujarati-first).
	 *
	 * The 108 driver green-corridor flow on the real dispatch lifecycle:
	 * request → a named duty officer approves on /ops → the window counts
	 * down from the approval (GreenCorridorDriver). Fed by the public
	 * projection only (no operator store). The labelled scripted fallback
	 * appears only while no live public event exists.
	 *
	 * Language: Gujarati by default, held PAGE-LOCALLY — the page's default
	 * never rewrites the shared preference, so /ops does not reopen in
	 * Gujarati after a visit here. An explicit choice persisted in the
	 * veer_lang cookie (the switch on any page) wins, exactly as the server
	 * render's <html lang> does.
	 */
	import { onMount } from 'svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import { publicFeed } from '$lib/citizen/public-feed.svelte';
	import GreenCorridorDriver from '$lib/citizen/GreenCorridorDriver.svelte';
	import { readLangCookie } from '$lib/i18n/lang-cookie';
	import '$lib/i18n/strings.citizen';

	let lang = $state('gu');

	onMount(() => {
		lang = readLangCookie() ?? lang;
		publicFeed.start();
		return () => publicFeed.stop();
	});

	// R3 wiring: Gujarati-first public surface; plain-language tier only.
	$effect(() => {
		commandBar.lang = lang;
		commandBar.tier = publicFeed.tier;
		commandBar.onLangChange = (l: string) => (lang = l);
	});
</script>

<svelte:head>
	<title>The Window · Green Corridor</title>
</svelte:head>

<div class="driver-page">
	<GreenCorridorDriver {lang} />
</div>

<style>
	.driver-page {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
	}
</style>
