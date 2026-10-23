<script lang="ts">
	/**
	 * BandMasthead — the city's posture in one line. Band word in the band
	 * color + one-sentence posture; during hysteresis the candidate band
	 * renders as a HOLLOW outline beside a mono dwell counter, then commits
	 * as the 600ms wipe (the showcase/band_easing.json artifact: RED held
	 * n=1..3, transition at n=4). BLACK alone breathes (2s hairline — the
	 * deck's only breathing element).
	 */
	import type { Band } from '$lib/design/tokens';
	import BandChip from './BandChip.svelte';
	import { t } from '$lib/i18n/strings';

	interface Props {
		band: Band | null;
		/** One sentence, e.g. 'AMBER — Rath Yatra corridor active, 1 pursuit'. */
		posture?: string;
		/** Hysteresis candidate: { band, ticks, required } — hollow + counter. */
		candidate?: { band: Band; ticks: number; required: number } | null;
		bandSource?: string | null;
		lang?: string;
	}

	let {
		band,
		posture = '',
		candidate = null,
		bandSource = null,
		lang = 'en'
	}: Props = $props();

	const word = $derived(band ?? '—');
</script>

<header class="masthead" data-prim="band-masthead" data-band={band ?? 'none'}>
	<div class="line">
		{#key word}
			<span
				class="band-word bw-{word}"
				class:bw-black={word === 'BLACK'}
				class:bw-null={band === null}
			>
				<span class="wipe" aria-hidden="true"></span>
				{word}
			</span>
		{/key}
		<span class="posture">{posture || t(lang, 'masthead_posture_none')}</span>
		<span class="spacer"></span>
		{#if candidate}
			<span class="dwell" title={t(lang, 'masthead_dwell_hint')}>
				<BandChip band={candidate.band} hollow size="sm" />
				<span class="dwell-count">
					{candidate.band} {t(lang, 'masthead_held')} · {t(lang, 'masthead_tick')} {candidate.ticks}/{candidate.required}
				</span>
			</span>
		{/if}
		{#if bandSource}<span class="source mono">{bandSource}</span>{/if}
	</div>
	<div class="hairline" class:breathing={word === 'BLACK'} aria-hidden="true"></div>
</header>

<style>
	.masthead {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-space-3) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
	}

	.line {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		flex-wrap: wrap;
	}

	.band-word {
		position: relative;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-lg);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		line-height: 1;
		color: var(--v-ink-3);
	}

	.bw-GREEN {
		color: var(--v-band-green);
	}

	.bw-AMBER {
		color: var(--v-band-amber);
	}

	.bw-RED {
		color: var(--v-band-red);
	}

	.bw-black {
		color: var(--v-ink-1);
	}

	.bw-null {
		color: var(--v-ink-3);
	}

	/* The 600ms commit wipe — replays on every band change ({#key}). */
	.wipe {
		position: absolute;
		inset: -2px -4px;
		border-radius: var(--v-radius-chip);
		background: linear-gradient(
			90deg,
			color-mix(in srgb, var(--v-band-amber) 24%, transparent),
			transparent
		);
		transform-origin: left;
		animation: wipe var(--v-dur-wipe) var(--v-ease) 1;
		pointer-events: none;
	}

	.bw-GREEN .wipe {
		background: linear-gradient(
			90deg,
			color-mix(in srgb, var(--v-band-green) 24%, transparent),
			transparent
		);
	}

	.bw-RED .wipe {
		background: linear-gradient(
			90deg,
			color-mix(in srgb, var(--v-band-red) 24%, transparent),
			transparent
		);
	}

	.bw-black .wipe {
		background: linear-gradient(
			90deg,
			color-mix(in srgb, var(--v-band-black-hairline) 24%, transparent),
			transparent
		);
	}

	@keyframes wipe {
		from {
			transform: scaleX(1);
		}
		to {
			transform: scaleX(0);
		}
	}

	.posture {
		font-size: var(--v-text-base);
		color: var(--v-ink-2);
		min-width: 0;
	}

	.spacer {
		flex: 1;
	}

	.dwell {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
	}

	.dwell-count {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.source {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	/* The masthead hairline; BLACK breathes on it (2s, the only breathing). */
	.hairline {
		height: 1px;
		background: var(--v-line-1);
	}

	.breathing {
		background: var(--v-band-black-hairline);
		animation: breathe var(--v-dur-breathe) var(--v-ease) infinite;
	}

	@keyframes breathe {
		0%,
		100% {
			opacity: 0.25;
		}
		50% {
			opacity: 1;
		}
	}

	/* Doctrine: reduced motion — wipe becomes a static crossfade, breathing
	 * becomes a static hairline. */
	@media (prefers-reduced-motion: reduce) {
		.wipe {
			animation: none;
			opacity: 0;
			transition: opacity var(--v-dur-wipe) var(--v-ease);
		}

		.breathing {
			animation: none;
			opacity: 0.6;
		}
	}
</style>
