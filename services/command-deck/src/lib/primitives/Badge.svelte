<script lang="ts">
	/**
	 * Badge — the 18px status pill. Hairline only; 'emulated' is dashed
	 * ink-2 caps; 'act' is act-border + act-text with NO fill (orange never
	 * fills); the 'band' variant MUST go through BandChip (BLACK's inverted
	 * rules apply) and is enforced here by delegation.
	 */
	import BandChip from './BandChip.svelte';
	import type { Band } from '$lib/design/tokens';

	export type BadgeVariant =
		| 'live'
		| 'offline'
		| 'fixture'
		| 'emulated'
		| 'not-deployed'
		| 'stale'
		| 'pending'
		| 'approved'
		| 'rejected'
		| 'deferred'
		| 'act'
		| 'neutral'
		| 'band';

	interface Props {
		variant: BadgeVariant;
		/** Required for variant="band"; ignored otherwise. */
		band?: Band | null;
		label?: string;
		title?: string;
	}

	let { variant, band = null, label, title }: Props = $props();

	const text = $derived(label ?? variant);
</script>

{#if variant === 'band'}
	<BandChip band={(band ?? 'GREEN') as Band} label={label} size="sm" />
{:else}
	<span
		class="badge v-{variant}"
		{title}
		data-prim="badge"
	>{text}</span>
{/if}

<style>
	.badge {
		display: inline-flex;
		align-items: center;
		height: 18px;
		padding: 0 var(--v-space-2);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-line-2);
		color: var(--v-ink-2);
		background: transparent;
		white-space: nowrap;
	}

	/* feed-health dots reuse band-green only as ≤8px dots elsewhere; the
	 * 'live' badge keeps the green EDGE, never a fill. */
	.v-live {
		border-color: var(--v-band-green);
		color: var(--v-band-green);
	}

	.v-offline,
	.v-not-deployed,
	.v-neutral {
		border-color: var(--v-line-2);
		color: var(--v-ink-3);
	}

	.v-not-deployed,
	.v-stale {
		border-style: dashed;
		color: var(--v-ink-3);
	}

	.v-fixture,
	.v-deferred {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	/* emulated evidence — dashed, ink-2, caps (never green). */
	.v-emulated {
		border-style: dashed;
		border-color: var(--v-ink-2);
		color: var(--v-ink-2);
	}

	.v-pending {
		border-color: var(--v-act);
		color: var(--v-act-text);
	}

	/* the human-must-act badge: edge + text, never a fill. */
	.v-act {
		border-color: var(--v-act);
		color: var(--v-act-text);
	}

	.v-approved {
		border-color: var(--v-band-green);
		color: var(--v-band-green);
	}

	.v-rejected {
		border-color: var(--v-band-red);
		color: var(--v-band-red);
	}
</style>
