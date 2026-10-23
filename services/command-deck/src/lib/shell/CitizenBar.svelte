<script lang="ts">
	/**
	 * CitizenBar — the public chrome bar: brand, the plain-language posture
	 * and the language switch. Deliberately nothing operator-side: no band
	 * code (citizens see "Planned holds on", never RED), no operator search,
	 * no share, no link into the operator deck.
	 */
	import { SUPPORTED, t } from '$lib/i18n/strings';
	import type { PublicTier } from './command-bar.svelte';

	interface Props {
		lang: string;
		tier: PublicTier | null;
		onLangChange?: ((lang: string) => void) | null;
	}

	let { lang, tier, onLangChange = null }: Props = $props();
</script>

<header class="bar" data-shell="citizen-bar">
	<span class="brand">{t(lang, 'brand')}</span>

	<span class="tier tier-{tier ?? 'unknown'}" data-tier={tier ?? 'unknown'} aria-live="polite">
		<span class="dot" aria-hidden="true"></span>
		{t(lang, tier ? `tier_${tier}` : 'tier_unknown')}
	</span>

	<div class="lang" role="group" aria-label={t(lang, 'shell_language')}>
		{#each SUPPORTED as s (s.code)}
			<button
				type="button"
				lang={s.code}
				class:active={lang === s.code}
				aria-pressed={lang === s.code}
				onclick={() => onLangChange?.(s.code)}
			>
				{s.label}
			</button>
		{/each}
	</div>
</header>

<style>
	.bar {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--v-space-2) var(--v-space-3);
		min-height: var(--v-ctl-lg);
		padding: var(--v-space-2) var(--v-space-4);
		background: var(--v-surface-1);
		border-bottom: 1px solid var(--v-line-1);
		flex-shrink: 0;
	}

	.brand {
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-1);
	}

	.tier {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
		padding: var(--v-space-1) var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-pill);
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
	}

	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--v-line-2);
	}

	.tier-calm .dot {
		background: var(--v-band-green);
	}

	.tier-watch .dot {
		background: var(--v-band-amber);
	}

	.tier-hold .dot {
		background: var(--v-band-red);
	}

	.tier-severe .dot {
		background: var(--v-act-text);
	}

	.tier-unknown {
		border-style: dashed;
		color: var(--v-ink-3);
	}

	.lang {
		display: flex;
		margin-left: auto;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		overflow: hidden;
	}

	.lang button {
		background: transparent;
		border: none;
		border-right: 1px solid var(--v-line-1);
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
		min-height: var(--v-ctl-sm);
		padding: var(--v-space-1) var(--v-space-3);
		cursor: pointer;
	}

	.lang button:last-child {
		border-right: none;
	}

	.lang button.active {
		background: var(--v-surface-3);
		color: var(--v-ink-1);
	}

	.lang button:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: -2px;
	}
</style>
