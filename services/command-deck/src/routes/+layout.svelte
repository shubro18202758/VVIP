<script lang="ts">
	/**
	 * Root layout — reset + fonts + tokens ONLY. No chrome, no data
	 * lifecycle: the shells own chrome via the (deck)/(public) group
	 * layouts, and the retired legacy feed tree (components/stores/
	 * simulation/data-service) was removed with the (legacy) routes in
	 * the 2026-10-14 template restoration. The font request carries the
	 * Devanagari and Gujarati faces the token stacks name, so हिंदी and
	 * ગુજરાતી render in the deck's type, not a device fallback.
	 */
	import '$lib/design/tokens.css';

	let { children } = $props();
</script>

<svelte:head>
	<link rel="preconnect" href="https://fonts.googleapis.com" />
	<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
	<link
		rel="stylesheet"
		href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=JetBrains+Mono:wght@400;600&family=Outfit:wght@500;700;800&family=Noto+Sans:wght@400;600&family=Noto+Sans+Devanagari:wght@400;600&family=Noto+Sans+Gujarati:wght@400;600&display=swap"
	/>
</svelte:head>

{@render children?.()}

<style>
	:global(html, body) {
		margin: 0;
		padding: 0;
		height: 100%;
		overflow: hidden;
		font-family: var(--v-font-ui);
		background: var(--v-canvas);
		color: var(--v-ink-1);
	}

	:global(*) {
		box-sizing: border-box;
	}

	:global(::-webkit-scrollbar) {
		width: 6px;
	}

	:global(::-webkit-scrollbar-track) {
		background: var(--v-surface-sink);
	}

	:global(::-webkit-scrollbar-thumb) {
		background: var(--v-surface-3);
		border-radius: 3px;
	}
</style>
