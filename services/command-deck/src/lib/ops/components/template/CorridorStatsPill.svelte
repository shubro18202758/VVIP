<script lang="ts">
	/**
	 * CorridorStatsPill — the template's floating live-stats pill (scanline
	 * glass, mono readouts) over the canvas, fed by the shared corridor
	 * feed (real oracle summary). A summary with no live observations behind
	 * it renders NO LIVE FEED in neutral ink — its zeros are an absence of
	 * data, never 'FLOW CLEAR · CORRIDOR GREEN'. A summary standing on the
	 * synthetic signal-ingress feed leads with SIMULATED FEED (EN/HI/GU) —
	 * provenance is never dressed as live.
	 */
	import { corridor } from './corridor.svelte';
	import { t } from '$lib/i18n/strings';
	import { ops } from '$lib/ops/store.svelte';

	const flowToken = $derived(
		corridor.row && !corridor.noLiveFeed
			? corridor.row.avgCongestionIdx <= 0.2
				? '--v-band-green'
				: corridor.row.avgCongestionIdx <= 0.4
					? '--v-flow-light'
					: corridor.row.avgCongestionIdx <= 0.6
						? '--v-band-amber'
						: corridor.row.avgCongestionIdx <= 0.8
							? '--v-act'
							: '--v-band-red'
			: null,
	);

	const statusToken = $derived(
		corridor.row && !corridor.noLiveFeed
			? corridor.row.status === 'green'
				? '--v-band-green'
				: corridor.row.status === 'amber'
					? '--v-band-amber'
					: '--v-band-red'
			: null,
	);
</script>

<div class="pill" data-template="corridor-pill" data-feed={corridor.noLiveFeed ? 'no-live-feed' : corridor.simulated ? 'simulated' : corridor.row ? 'live' : 'pending'}>
	<span class="scanline" aria-hidden="true"></span>
	{#if corridor.row && corridor.noLiveFeed}
		<span class="stat" title="corridor summary has no live observations behind it">
			<span class="value mono neutral" data-testid="corridor-no-live-feed">NO LIVE FEED</span>
		</span>
		<i class="sep" aria-hidden="true"></i>
		<span class="stat">
			<span class="label">{t(ops.lang, 'tpl_corridor_status')}</span>
			<span class="value mono neutral">—</span>
		</span>
	{:else if corridor.row}
		{#if corridor.simulated}
			<span class="stat" title="synthetic signal-ingress observations (data_quality synthetic) — a simulated feed, not field sensors">
				<span class="value mono sim" data-testid="corridor-simulated-feed">{t(ops.lang, 'cc_simulated_feed')}</span>
			</span>
			<i class="sep" aria-hidden="true"></i>
		{/if}
		<span class="stat">
			<span class="label">{t(ops.lang, 'tpl_avg_speed')}</span>
			<span class="value mono">{corridor.row.avgSpeedKmh.toFixed(1)}</span>
			<span class="unit mono">km/h</span>
		</span>
		<i class="sep" aria-hidden="true"></i>
		<span class="stat">
			<span class="label">CGX</span>
			<span class="value mono">{corridor.row.avgCongestionIdx.toFixed(2)}</span>
		</span>
		<i class="sep" aria-hidden="true"></i>
		<span class="stat">
			<span class="label">{t(ops.lang, 'tpl_critical')}</span>
			<span class="value mono" class:hot={corridor.row.criticalSegments > 0}>
				{corridor.row.criticalSegments}
			</span>
		</span>
		<i class="sep" aria-hidden="true"></i>
		<span class="stat">
			<span class="label">{t(ops.lang, 'tpl_flow')}</span>
			<span class="value mono flow" style="color: var({flowToken})">{corridor.flowWord}</span>
		</span>
		<i class="sep" aria-hidden="true"></i>
		<span class="stat">
			<span class="label">{t(ops.lang, 'tpl_corridor_status')}</span>
			<span class="value mono" style="color: var({statusToken})">
				{corridor.row.status.toUpperCase()}
			</span>
		</span>
	{:else}
		<span class="stat"><span class="value mono dim">— corridor feed —</span></span>
	{/if}
</div>

<style>
	.pill {
		position: relative;
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-4);
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		border: 1px solid var(--v-glass-border);
		border-radius: var(--v-radius-pill);
		overflow: hidden;
		box-shadow: 0 8px 28px var(--v-veil);
	}

	.scanline {
		position: absolute;
		inset: 0;
		background: linear-gradient(
			90deg,
			transparent,
			var(--v-act-veil) 45%,
			var(--v-act-veil) 55%,
			transparent
		);
		background-size: 260% 100%;
		animation: pill-scan var(--v-dur-sweep, 3.5s) linear infinite;
		pointer-events: none;
	}

	@keyframes pill-scan {
		from {
			background-position: 200% 0;
		}
		to {
			background-position: -100% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.scanline {
			animation: none;
			opacity: 0;
		}
	}

	.stat {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-1);
	}

	.label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.value {
		font-size: var(--v-text-sm);
		font-weight: 700;
		color: var(--v-ink-1);
		font-variant-numeric: tabular-nums;
	}

	.value.hot {
		color: var(--v-band-red);
	}

	.value.dim {
		color: var(--v-ink-3);
		font-weight: 400;
	}

	/* Synthetic feed: the tool accent of machine senses, dashed — never a band colour. */
	.value.sim {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-tool);
		border: 1px dashed var(--v-tool);
		border-radius: var(--v-radius-pill);
		padding: 0 6px;
	}

	/* No live data: neutral ink, never a band or flow colour. */
	.value.neutral {
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
	}

	.unit {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.sep {
		width: 1px;
		height: 14px;
		background: var(--v-glass-border);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
