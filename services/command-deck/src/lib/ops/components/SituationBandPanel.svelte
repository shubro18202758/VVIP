<script lang="ts">
	/**
	 * SituationBandPanel — band history + dwell on the design system: the
	 * current band via BandChip (the only legal renderer), the hysteresis
	 * candidate as a HOLLOW chip beside a mono dwell counter, and the
	 * sparkline in SVG (concrete colors from the typed token mirror — SVG
	 * cannot read CSS vars; this is the sanctioned tokens.ts consumer).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t, tOps } from '$lib/i18n/strings.ops';
	import { bandColor, bandEdgeColor } from '$lib/design/tokens';
	import type { Band } from '$lib/design/tokens';
	import type { BandPoint } from '$lib/ops/types';

	const LEVEL: Record<string, number> = { GREEN: 0, AMBER: 1, RED: 2, BLACK: 3 };
	const DWELL_REQUIRED = 4;

	const H = 44;
	const W = 260;

	const lang = $derived(ops.lang);

	const points = $derived<BandPoint[]>(ops.bandHistory.slice(-60));

	/** SVG stroke/inks from the typed token mirror (command theme). */
	const lineColor = 'var(--v-data)';

	let pathD = $derived.by(() => {
		if (points.length < 2) return '';
		const step = W / (points.length - 1);
		return points
			.map((p, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(1)},${(H - 4 - (LEVEL[p.band] ?? 0) * ((H - 10) / 3)).toFixed(1)}`)
			.join(' ');
	});

	const candidate = $derived.by(() => {
		const h = ops.bandHistory;
		if (h.length < 2 || !ops.band) return null;
		const newest = h[h.length - 1].band;
		if (newest === ops.band) return null;
		let ticks = 0;
		for (let i = h.length - 1; i >= 0 && h[i].band === newest; i--) ticks++;
		return { band: newest as Band, ticks, required: DWELL_REQUIRED };
	});

	const sourceWord = $derived(
		ops.bandSource === 'situation_log'
			? t(lang, 'bandSourceLog')
			: ops.bandSource === 'adaptive-status'
				? t(lang, 'bandSourceStatus')
				: t(lang, 'bandAccumulated'),
	);
</script>

{#snippet headRight()}
	<span class="src mono">{sourceWord}</span>
{/snippet}

<Panel
	title={t(lang, 'bandHistory')}
	sub={t(lang, 'bandSub')}
	status={ops.bandUpdated ? { state: 'live', updatedAt: ops.bandUpdated } : null}
	lang={lang}
	{...{ 'header-right': headRight }}
>
	<div class="band-row">
		{#if ops.band}
			<BandChip band={ops.band as Band} size="md" />
		{:else}
			<span class="band-unknown mono">band —</span>
		{/if}
		<span class="band-name">{ops.band ? t(lang, `band_${ops.band}`) : t(lang, 'opsNever')}</span>
		{#if candidate}
			<span class="dwell" title={t(lang, 'masthead_dwell_hint')}>
				<BandChip band={candidate.band} hollow size="sm" />
				<span class="dwell-count mono">{candidate.band} {t(lang, 'masthead_held')} · {t(lang, 'masthead_tick')} {candidate.ticks}/{candidate.required}</span>
			</span>
		{/if}
	</div>

	<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" aria-label="band history">
		{#if pathD}
			<path d={pathD} fill="none" stroke={lineColor} stroke-width="1.5" />
			{#each points as p, i (i)}
				{#if i % Math.max(1, Math.floor(points.length / 12)) === 0}
					<circle
						cx={(i * (W / Math.max(1, points.length - 1))).toFixed(1)}
						cy={(H - 4 - (LEVEL[p.band] ?? 0) * ((H - 10) / 3)).toFixed(1)}
						r="2.2"
						fill={bandColor(p.band)}
						stroke={p.band === 'BLACK' ? bandEdgeColor(p.band) : 'none'}
						stroke-width="0.6"
					/>
				{/if}
			{/each}
		{:else}
			<text x="4" y="26" fill="currentColor" font-size="10">no history yet</text>
		{/if}
		<line x1="0" y1={H - 4 - ((H - 10) / 3)} x2={W} y2={H - 4 - ((H - 10) / 3)} stroke="var(--v-line-2)" stroke-dasharray="2 3" stroke-width="0.5" />
		<line x1="0" y1={H - 4 - (2 * (H - 10)) / 3} x2={W} y2={H - 4 - (2 * (H - 10)) / 3} stroke="var(--v-line-2)" stroke-dasharray="2 3" stroke-width="0.5" />
	</svg>

	{#if ops.overrides.length}
		<p class="overrides mono">{ops.overrides.length} override rows{ops.overridesNote ? ` · ${ops.overridesNote}` : ''}</p>
	{:else if ops.overridesNote}
		<p class="overrides mono">{ops.overridesNote}</p>
	{/if}
	{#if ops.bandEvidence?.shadowEval}
		<p class="shadow mono">shadow_eval={ops.bandEvidence.shadowEval} floor_applied={ops.bandEvidence.floorApplied ?? '—'} fail_closed={ops.bandEvidence.failClosedReason ?? '—'}</p>
	{/if}
</Panel>

<style>
	.src {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.band-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.band-unknown {
		font-size: var(--v-text-sm);
		color: var(--v-ink-4);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-1) var(--v-space-2);
	}

	.band-name {
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.dwell {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
		margin-left: auto;
	}

	.dwell-count {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.spark {
		width: 100%;
		height: 44px;
		color: var(--v-ink-4);
		margin-top: var(--v-space-2);
	}

	.overrides,
	.shadow {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		word-break: break-word;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
