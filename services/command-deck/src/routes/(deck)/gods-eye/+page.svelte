<script lang="ts">
	/**
	 * /gods-eye — the cinematic fullscreen God's-eye route (polished).
	 *
	 * Fullscreen canvas + HUD (sensor mode, layer chips, AMA chip, share,
	 * guided tour). The share-link hash (#v=2&lat=..&style=flir&layers=…)
	 * restores camera + sensor + layer state on load, so a Jan Rakshak unit
	 * or a police control room opens the exact view the sender saw. The
	 * pursuit overlay rides the ops store's interception plan; the panel
	 * degrades alone when WebGL/the bundle is unavailable.
	 */
	import { onMount, onDestroy } from 'svelte';
	import GodsEyeCanvas from '$lib/gods-eye/GodsEyeCanvas.svelte';
	import { godsEye } from '$lib/gods-eye/store.svelte';
	import { ops } from '$lib/ops/store.svelte';

	onMount(() => {
		godsEye.followSelection = false; // cinematic route: free camera
		ops.start();
	});

	onDestroy(() => {
		ops.stop();
	});
</script>

<svelte:head>
	<title>God's-eye · VVIP Command Deck</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<main class="gods-eye-page">
	<GodsEyeCanvas hud="full" />
	<footer class="hint">
		drag = orbit · wheel = zoom · click an entity = identify · SHARE VIEW copies this exact
		camera for handoff · TOUR runs the guided demo · Esc stops the tour
	</footer>
</main>

<style>
	.gods-eye-page {
		/* Cinematic: escape the app layout entirely. */
		position: fixed;
		inset: 0;
		z-index: 40;
		background: var(--v-canvas);
		display: flex;
		flex-direction: column;
	}

	.gods-eye-page :global(.ge-wrap) {
		flex: 1;
		min-height: 0;
		border-radius: 0;
	}

	.hint {
		flex-shrink: 0;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		text-align: center;
		padding: var(--v-space-1) var(--v-space-2) var(--v-space-2);
		background: var(--v-canvas);
	}
</style>
