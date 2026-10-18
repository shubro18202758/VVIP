<script lang="ts">
	/**
	 * ConvoyHud — the template's CONVOY LIVE HUD, rebuilt honestly: the
	 * gauge reads the live CORRIDOR average speed (labelled as such — no
	 * fabricated per-vehicle telemetry), ETA/distance come from the
	 * decider's plan, the timer is real elapsed time since the named duty
	 * officer launched the escort.
	 */
	import { corridor } from './corridor.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';

	let {
		escort
	}: {
		escort: {
			movementId: string | null;
			startedAt: number | null;
			plannedEtaMin: number | null;
			vvipClass: string;
			destination: string;
		};
	} = $props();

	let elapsed = $state(0);
	let timer: ReturnType<typeof setInterval> | null = null;

	$effect(() => {
		if (escort.startedAt) {
			const tick = () => (elapsed = Math.max(0, Math.floor((Date.now() - escort.startedAt!) / 1000)));
			tick();
			if (timer) clearInterval(timer);
			timer = setInterval(tick, 1000);
		}
		return () => {
			if (timer) clearInterval(timer);
			timer = null;
		};
	});

	const elapsedLabel = $derived.by(() => {
		const m = Math.floor(elapsed / 60);
		const s = elapsed % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	});

	/** Gauge geometry: 240° arc from 150° to -30° (SVG path coords). */
	const speed = $derived(corridor.row?.avgSpeedKmh ?? 0);
	const MAX_KMH = 100;
	const needleAngle = $derived(150 - Math.min(speed / MAX_KMH, 1) * 240);

	function polar(cx: number, cy: number, r: number, deg: number): [number, number] {
		const rad = (deg * Math.PI) / 180;
		return [cx + r * Math.cos(rad), cy - r * Math.sin(rad)];
	}

	const arcPath = $derived.by(() => {
		const [x1, y1] = polar(60, 62, 46, 150);
		const [x2, y2] = polar(60, 62, 46, -30);
		return `M ${x1.toFixed(1)} ${y1.toFixed(1)} A 46 46 0 1 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`;
	});

	const needleTip = $derived(polar(60, 62, 40, needleAngle));
	const progress = $derived(
		escort.plannedEtaMin ? Math.min(1, elapsed / (escort.plannedEtaMin * 60)) : 0,
	);
</script>

{#if escort.movementId && escort.startedAt}
	<aside class="hud" data-template="convoy-hud" aria-label={t(ops.lang, 'tpl_escort_live')}>
		<header class="hud-head">
			<span class="live-badge">{t(ops.lang, 'tpl_escort_live')}</span>
			<span class="elapsed mono">{elapsedLabel}</span>
		</header>

		<div class="gauge-wrap">
			<svg viewBox="0 0 120 92" class="gauge" role="img" aria-label="corridor average speed gauge">
				<path class="track" d={arcPath} />
				<path class="zone z-green" d={arcPath} stroke-dasharray="24 999" stroke-dashoffset="0" />
				<path class="zone z-amber" d={arcPath} stroke-dasharray="24 999" stroke-dashoffset="-80" />
				<path class="zone z-red" d={arcPath} stroke-dasharray="32 999" stroke-dashoffset="-160" />
				<line
					class="needle"
					x1="60"
					y1="62"
					x2={needleTip[0].toFixed(1)}
					y2={needleTip[1].toFixed(1)}
				/>
				<circle class="hub" cx="60" cy="62" r="4" />
				<text x="60" y="86" class="gauge-value">{speed.toFixed(0)}</text>
			</svg>
			<div class="gauge-side">
				<span class="gauge-label">CORRIDOR AVG</span>
				<span class="gauge-unit mono">km/h</span>
			</div>
		</div>

		<div class="grid mono">
			<span class="cell"><b>{t(ops.lang, 'tpl_dist')}</b>{escort.destination}</span>
			<span class="cell"><b>{t(ops.lang, 'tpl_eta')}</b>{escort.plannedEtaMin ?? '—'} {t(ops.lang, 'tpl_min')}</span>
			<span class="cell"><b>{t(ops.lang, 'tpl_class')}</b>{escort.vvipClass}</span>
			<span class="cell"><b>OFFICER</b>{ops.dutyOfficer || '—'}</span>
		</div>

		<div class="progress" title="elapsed / planned ETA">
			<i style={`width: ${Math.round(progress * 100)}%`}></i>
		</div>

		<p class="mid mono">mvmt {escort.movementId}</p>
	</aside>
{/if}

<style>
	.hud {
		position: absolute;
		top: var(--v-space-4);
		right: var(--v-space-3);
		width: 232px;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-space-3);
		background: linear-gradient(160deg, var(--v-glass-bg), var(--v-surface-2));
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		border: 1px solid var(--v-glass-border);
		border-radius: var(--v-radius-panel);
		box-shadow: 0 10px 32px var(--v-veil);
		z-index: 25;
	}

	.hud-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.live-badge {
		font-family: var(--v-font-display);
		font-size: var(--v-text-2xs);
		font-weight: 800;
		letter-spacing: 0.14em;
		color: var(--v-band-green);
		animation: live-breathe var(--v-dur-breathe) var(--v-ease) infinite;
	}

	@keyframes live-breathe {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.5;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.live-badge {
			animation: none;
		}
	}

	.elapsed {
		font-size: var(--v-text-md);
		font-weight: 700;
		color: var(--v-ink-1);
		font-variant-numeric: tabular-nums;
	}

	.gauge-wrap {
		display: flex;
		align-items: flex-end;
		gap: var(--v-space-2);
	}

	.gauge {
		width: 120px;
		height: 92px;
		flex-shrink: 0;
	}

	.track {
		fill: none;
		stroke: var(--v-surface-3);
		stroke-width: 7;
		stroke-linecap: round;
	}

	.zone {
		fill: none;
		stroke-width: 7;
		stroke-linecap: butt;
		opacity: 0.85;
	}

	.z-green {
		stroke: var(--v-band-green);
	}

	.z-amber {
		stroke: var(--v-band-amber);
	}

	.z-red {
		stroke: var(--v-band-red);
	}

	.needle {
		stroke: var(--v-act);
		stroke-width: 2.5;
		stroke-linecap: round;
		filter: drop-shadow(0 0 3px var(--v-glow-act));
	}

	.hub {
		fill: var(--v-act);
	}

	.gauge-value {
		font-family: var(--v-font-mono);
		font-size: 15px;
		font-weight: 700;
		fill: var(--v-ink-1);
		text-anchor: middle;
	}

	.gauge-side {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding-bottom: 6px;
	}

	.gauge-label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
	}

	.gauge-unit {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--v-space-1) var(--v-space-2);
	}

	.cell {
		display: flex;
		flex-direction: column;
		gap: 1px;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-1);
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.cell b {
		font-weight: 400;
		font-size: 9px;
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-4);
	}

	.progress {
		height: 4px;
		border-radius: 2px;
		background: var(--v-surface-3);
		overflow: hidden;
	}

	.progress i {
		display: block;
		height: 100%;
		background: linear-gradient(90deg, var(--v-act), var(--v-act-2));
		box-shadow: 0 0 8px var(--v-glow-act);
	}

	.mid {
		margin: 0;
		font-size: 9px;
		color: var(--v-ink-4);
		letter-spacing: 0.06em;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
