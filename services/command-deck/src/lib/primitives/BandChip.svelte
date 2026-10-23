<script lang="ts">
	/**
	 * BandChip — the ONLY legal renderer of a band color as a chip.
	 *
	 * GREEN/AMBER/RED: token edge + token ink at the band veil fill.
	 * BLACK: an INVERTED chip — translucent near-black fill (never an
	 * opaque void) with a 1px band-red hairline. The hex sweep and review
	 * enforce that no other component renders band colors as chips.
	 */
	import type { Band } from '$lib/design/tokens';

	interface Props {
		band: Band;
		/** Hollow = candidate band during hysteresis (outline only). */
		hollow?: boolean;
		/** Plain-language override (daylight: citizens never see 'BLACK'). */
		label?: string;
		size?: 'sm' | 'md';
	}

	let { band, hollow = false, label, size = 'md' }: Props = $props();

	const word = $derived(label ?? band);
</script>

<span
	class="chip b-{band}"
	class:hollow
	class:sm={size === 'sm'}
	data-band={band}
	data-prim="band-chip"
>{word}</span>

<style>
	.chip {
		display: inline-flex;
		align-items: center;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		line-height: 1;
		padding: var(--v-space-1) var(--v-space-2);
		border-radius: var(--v-radius-chip);
		border: 1px solid var(--v-band-green);
		color: var(--v-band-green);
		background: transparent;
		white-space: nowrap;
	}

	.sm {
		padding: 1px var(--v-space-2);
		font-size: var(--v-text-2xs);
	}

	.b-GREEN {
		border-color: var(--v-band-green);
		color: var(--v-band-green);
	}

	.b-AMBER {
		border-color: var(--v-band-amber);
		color: var(--v-band-amber);
	}

	.b-RED {
		border-color: var(--v-band-red);
		color: var(--v-band-red);
	}

	/* BLACK — inverted: translucent fill + red hairline, never a filled void. */
	.b-BLACK {
		border-color: var(--v-band-black-hairline);
		color: var(--v-ink-1);
		background: var(--v-band-black-fill);
	}

	/* Veil fill for committed bands (skip for BLACK: it has its own fill). */
	.b-GREEN:not(.hollow) {
		background: var(--v-band-veil);
		background: color-mix(in srgb, var(--v-band-green) 14%, transparent);
	}

	.b-AMBER:not(.hollow) {
		background: var(--v-band-veil);
		background: color-mix(in srgb, var(--v-band-amber) 14%, transparent);
	}

	.b-RED:not(.hollow) {
		background: var(--v-band-veil);
		background: color-mix(in srgb, var(--v-band-red) 14%, transparent);
	}

	/* Candidate during hysteresis: outline only, no fill at all. */
	.hollow {
		background: transparent;
		border-style: dashed;
	}
</style>
