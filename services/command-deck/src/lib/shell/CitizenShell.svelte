<script lang="ts">
	/**
	 * CitizenShell — public chrome (daylight tokens apply via the (public)
	 * layout's data-theme). Same command-bar contract, citizen-sized type
	 * and tap targets; nav is the two public views plus a way back to the
	 * operator deck.
	 */
	import { page } from '$app/stores';
	import { t } from '$lib/i18n/strings';
	import '$lib/i18n/packs';
	import CommandBar from './CommandBar.svelte';
	import Toaster from './Toaster.svelte';
	import { commandBar } from './command-bar.svelte';
	import { PUBLIC_NAV } from './nav';

	let { children } = $props();

	const pathname = $derived($page.url.pathname);
</script>

<div class="shell" data-shell="citizen">
	<CommandBar {...commandBar} />
	<nav class="tabs" aria-label={t(commandBar.lang, 'nav_section_public')}>
		{#each PUBLIC_NAV as item (item.href)}
			<a
				href={item.href}
				class:active={pathname === item.href || pathname.startsWith(item.href + '/')}
			>
				<span class="glyph" aria-hidden="true">{item.glyph}</span>
				{t(commandBar.lang, item.key)}
			</a>
		{/each}
	</nav>
	<main class="main">
		{@render children?.()}
	</main>
	<Toaster />
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}

	.tabs {
		display: flex;
		gap: var(--v-space-2);
		padding: var(--v-space-2) var(--v-space-4);
		background: var(--v-paper);
		border-bottom: 1px solid var(--v-line-1);
		flex-shrink: 0;
	}

	.tabs a {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
		height: var(--v-ctl-touch);
		padding: 0 var(--v-space-4);
		border: 1px solid transparent;
		border-radius: var(--v-radius-pill);
		color: var(--v-ink-2);
		text-decoration: none;
		font-size: var(--v-text-sm);
	}

	.tabs a.active {
		border-color: var(--v-line-2);
		background: var(--v-surface-1);
		color: var(--v-ink-1);
	}

	.tabs a:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.glyph {
		flex-shrink: 0;
	}

	.main {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}
</style>
