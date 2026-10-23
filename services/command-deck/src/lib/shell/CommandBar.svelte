<script lang="ts">
	/**
	 * CommandBar — the one chrome bar. Fields come ONLY from the R3 state
	 * module (CommandShell spreads commandBar into this component); pages
	 * assign the fields they own in an $effect, and a page that assigns
	 * nothing renders calm chrome (band:null shows an explicit 'band —'
	 * mono chip — a hollow state cannot hide).
	 *
	 * Chrome's own data: global search over the nav index ('/' focuses,
	 * Enter jumps, Escape clears), the language switch and — only when the
	 * deck runs the opt-in operator gate — the duty-officer sign-in.
	 */
	import { goto } from '$app/navigation';
	import { SUPPORTED, t } from '$lib/i18n/strings';
	import type { Band } from '$lib/design/tokens';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { NAV_INDEX } from './nav';
	import OperatorSignIn from './OperatorSignIn.svelte';

	interface Props {
		band: Band | null;
		bandSource?: string | null;
		lang: string;
		onLangChange?: ((lang: string) => void) | null;
		onShare?: (() => void) | null;
	}

	let { band, bandSource = null, lang, onLangChange = null, onShare = null }: Props = $props();

	let q = $state('');
	let searchEl: HTMLInputElement | undefined = $state();

	const matches = $derived(
		q.trim() ? NAV_INDEX.filter((n) => n.terms.includes(q.trim().toLowerCase())) : []
	);

	function searchKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			q = '';
			searchEl?.blur();
		} else if (e.key === 'Enter' && matches.length > 0) {
			const href = matches[0].href;
			q = '';
			searchEl?.blur();
			goto(href);
		}
	}

	function globalKeydown(e: KeyboardEvent) {
		const target = e.target as HTMLElement | null;
		const typing =
			target instanceof HTMLInputElement ||
			target instanceof HTMLTextAreaElement ||
			target?.isContentEditable;
		if (e.key === '/' && !typing) {
			e.preventDefault();
			searchEl?.focus();
		}
	}

	function pickLang(code: string) {
		onLangChange?.(code);
	}
</script>

<svelte:window onkeydown={globalKeydown} />

<header class="bar" data-shell="command-bar">
	<span class="brand">{t(lang, 'shell_brand')}</span>

	{#if band}
		<span class="band-holder" title={bandSource ?? ''}>
			<BandChip band={band} size="sm" />
		</span>
	{:else}
		<span class="band-pending mono" data-band="pending">{t(lang, 'shell_band_pending')}</span>
	{/if}

	<div class="search" role="search">
		<input
			type="search"
			bind:this={searchEl}
			bind:value={q}
			onkeydown={searchKeydown}
			placeholder={t(lang, 'shell_search')}
			aria-label={t(lang, 'shell_search')}
			data-shell="command-search"
		/>
		{#if q.trim()}
			<span class="search-note mono">
				{#if matches.length}{matches.length}{:else}{t(lang, 'shell_search_none')}{/if}
			</span>
		{:else}
			<kbd class="kbd mono">/</kbd>
		{/if}
	</div>

	<div class="lang" role="group" aria-label={t(lang, 'shell_language')}>
		{#each SUPPORTED as s (s.code)}
			<button
				type="button"
				class:active={lang === s.code}
				onclick={() => pickLang(s.code)}
			>
				{s.label}
			</button>
		{/each}
	</div>

	{#if onShare}
		<button class="share" type="button" onclick={() => onShare()}>
			{t(lang, 'shell_share')}
		</button>
	{/if}

	<OperatorSignIn {lang} />
</header>

<style>
	.bar {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		height: var(--v-ctl-lg);
		padding: 0 var(--v-space-3);
		background: var(--v-surface-1);
		border-bottom: 1px solid var(--v-line-1);
		flex-shrink: 0;
	}

	.brand {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-1);
	}

	.band-holder {
		display: inline-flex;
	}

	.band-pending {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.search {
		position: relative;
		display: flex;
		align-items: center;
		flex: 1;
		max-width: 380px;
		min-width: 120px;
	}

	.search input {
		width: 100%;
		height: var(--v-ctl-sm);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-1);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		padding: 0 var(--v-space-5) 0 var(--v-space-2);
	}

	.search input:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.kbd,
	.search-note {
		position: absolute;
		right: var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 0 var(--v-space-1);
		pointer-events: none;
	}

	.lang {
		display: flex;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		overflow: hidden;
	}

	.lang button {
		background: transparent;
		border: none;
		border-right: 1px solid var(--v-line-1);
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
		padding: var(--v-space-1) var(--v-space-2);
		cursor: pointer;
	}

	.lang button:last-child {
		border-right: none;
	}

	.lang button.active {
		background: var(--v-surface-3);
		color: var(--v-ink-1);
	}

	.share {
		background: transparent;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-2);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		cursor: pointer;
	}

	.share:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
	}
</style>
