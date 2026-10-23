<script lang="ts">
	/**
	 * SituationBandPanel — band history + dwell on the design system: the
	 * committed band via BandChip (the only legal renderer; a null band is an
	 * UNKNOWN chip, never a GREEN default), how long it has held since its
	 * last commit (the log's transitioned flag), the hysteresis candidate as
	 * a HOLLOW chip beside a mono tick counter (the log's observed_class —
	 * ops.bandCandidate, never recomputed here), the sparkline in SVG, and
	 * the shadow-eval evidence of the newest published parameter set (GET
	 * /api/v1/adaptive/overrides). Freshness keys on the band source
	 * ANSWERING (ops.bandPolledAt at the band cadence), so a healthy band
	 * that simply has not changed never reads STALE; the age of the newest
	 * reading is printed separately. When the traffic readings behind the
	 * band are synthetic, the panel says SIMULATED FEED.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { POLL_MS } from '$lib/ops/endpoints';
	import { fmtAgeSince, fmtDuration, istClock } from '$lib/ops/format';
	import { t, tOps } from '$lib/i18n/strings.ops';
	import { bandColor, bandEdgeColor } from '$lib/design/tokens';
	import type { BandPoint } from '$lib/ops/types';

	const LEVEL: Record<string, number> = { GREEN: 0, AMBER: 1, RED: 2, BLACK: 3 };

	const H = 44;
	const W = 260;

	const lang = $derived(ops.lang);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

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

	const candidate = $derived(ops.bandCandidate);

	/** The band's scope in words — and, for a stale city band, since when its engine poll failed. */
	const sourceWord = $derived(
		ops.bandStaleSince != null
			? ops.bandSource
			: ops.bandSourceKind === 'situation_log'
				? tOps(lang, 'bandSourceEvent')
				: ops.bandSourceKind === 'city-band'
					? tOps(lang, 'bandSourceCity')
					: ops.bandSourceKind === 'adaptive-status'
						? t(lang, 'bandSourceStatus')
						: tOps(lang, 'bandSourceNone'),
	);

	const newest = $derived(ops.overrides[0] ?? null);

	const feedChip = $derived.by(() => {
		const f = ops.trafficFeed;
		if (!f) return null;
		if (f.label === 'simulated') return { variant: 'emulated' as const, label: tOps(lang, 'feedSimulated') };
		if (f.label === 'mixed') {
			const pct = Math.round((100 * f.synthetic) / Math.max(1, f.real + f.synthetic));
			return { variant: 'emulated' as const, label: tOps(lang, 'feedMixed', pct) };
		}
		if (f.label === 'live') return { variant: 'live' as const, label: tOps(lang, 'feedLive') };
		return { variant: 'stale' as const, label: tOps(lang, 'feedNone') };
	});
</script>

{#snippet headRight()}
	<span class="src mono" data-testid="band-source" data-source={ops.bandSourceKind}>{sourceWord}</span>
	{#if ops.bandSimulated === true}<Badge variant="emulated" label={tOps(lang, 'bandSimulatedTag')} title={tOps(lang, 'bandSimulatedHint')} />{/if}
	<!-- the corridor feed's own chip, unless it would only repeat the band's SIMULATED FEED -->
	{#if feedChip && !(ops.bandSimulated === true && ops.trafficFeed?.label === 'simulated')}
		<Badge variant={feedChip.variant} label={feedChip.label} title={tOps(lang, 'feedTitle')} />
	{/if}
{/snippet}

<Panel
	title={t(lang, 'bandHistory')}
	sub={t(lang, 'bandSub')}
	status={ops.bandPolledAt ? { state: 'live', updatedAt: ops.bandPolledAt } : null}
	pollMs={POLL_MS.band}
	lang={lang}
	{...{ 'header-right': headRight }}
>
	<div class="band-row" data-testid="band-row" data-band={ops.band ?? 'UNKNOWN'}>
		{#if ops.band}
			<BandChip band={ops.band} size="md" />
			<span class="band-name">{t(lang, `band_${ops.band}`)}</span>
		{:else}
			<span class="band-unknown mono">{tOps(lang, 'bandUnknown')}</span>
			<span class="band-name" data-testid="band-unknown-why">{ops.bandStaleSince != null ? ops.bandSource : ops.bandSourceKind === 'none' ? tOps(lang, 'bandNoSubject') : t(lang, 'opsNever')}</span>
		{/if}
		{#if candidate}
			<span class="dwell" title={t(lang, 'masthead_dwell_hint')} data-testid="band-candidate">
				<BandChip band={candidate.band} hollow size="sm" />
				<span class="dwell-count mono">{candidate.band} {t(lang, 'masthead_held')} · {t(lang, 'masthead_tick')} {candidate.ticks}/{candidate.required}</span>
			</span>
		{/if}
	</div>

	<p class="facts mono">
		{#if ops.bandCommittedAt}
			<span data-testid="band-held">{tOps(lang, 'bandHeld', fmtDuration((clock - ops.bandCommittedAt) / 1000), istClock(ops.bandCommittedAt))}</span>
		{:else if ops.band && ops.bandSourceKind === 'situation_log'}
			<span>{tOps(lang, 'bandHeldBeyondWindow')}</span>
		{/if}
		{#if ops.bandUpdated}
			<span>{tOps(lang, 'bandNewestReading', fmtAgeSince(ops.bandUpdated, clock))}</span>
		{/if}
	</p>

	<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" aria-label="band history">
		{#if pathD}
			<path d={pathD} fill="none" stroke={lineColor} stroke-width="1.5" />
			{#each points as p, i (i)}
				{#if i % Math.max(1, Math.floor(points.length / 12)) === 0 || p.transitioned}
					<circle
						cx={(i * (W / Math.max(1, points.length - 1))).toFixed(1)}
						cy={(H - 4 - (LEVEL[p.band] ?? 0) * ((H - 10) / 3)).toFixed(1)}
						r={p.transitioned ? 3 : 2.2}
						fill={bandColor(p.band)}
						stroke={p.band === 'BLACK' ? bandEdgeColor(p.band) : 'none'}
						stroke-width="0.6"
					/>
				{/if}
			{/each}
		{:else}
			<text x="4" y="26" fill="currentColor" font-size="10">{tOps(lang, 'bandNoHistory')}</text>
		{/if}
		<line x1="0" y1={H - 4 - ((H - 10) / 3)} x2={W} y2={H - 4 - ((H - 10) / 3)} stroke="var(--v-line-2)" stroke-dasharray="2 3" stroke-width="0.5" />
		<line x1="0" y1={H - 4 - (2 * (H - 10)) / 3} x2={W} y2={H - 4 - (2 * (H - 10)) / 3} stroke="var(--v-line-2)" stroke-dasharray="2 3" stroke-width="0.5" />
	</svg>

	<div class="evidence" data-testid="band-overrides" data-count={ops.overrides.length}>
		{#if newest}
			<p class="ev-line mono">
				{tOps(lang, 'overridesNewest', newest.source ?? '—', newest.situationClass ?? '—', newest.shadowEval ?? '—')}
				{#if newest.shadowEvalDetail && newest.shadowEvalDetail !== 'shadow_eval_not_required'}({newest.shadowEvalDetail}){/if}
				{#if typeof newest.params?.['advance_closure_sec'] === 'number'}· {tOps(lang, 'overridesAdvance', newest.params['advance_closure_sec'] as number)}{/if}
				{#if newest.floorApplied}· {tOps(lang, 'overridesFloor')}{/if}
				· {newest.ts ? `${istClock(newest.ts)} IST` : '—'}
			</p>
			<p class="ev-sub mono">{tOps(lang, 'overridesCount', ops.overrides.length)}{ops.overridesNote ? ` · ${ops.overridesNote}` : ''}</p>
		{:else if ops.overridesNote}
			<p class="ev-sub mono">{ops.overridesNote}</p>
		{:else if ops.overridesUpdated}
			<p class="ev-sub mono">{tOps(lang, 'overridesNone')}</p>
		{/if}
	</div>
</Panel>

<style>
	.src {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.band-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.band-unknown {
		font-size: var(--v-text-xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
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

	.facts {
		margin: var(--v-space-1) 0 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.spark {
		width: 100%;
		height: 44px;
		color: var(--v-ink-3);
		margin-top: var(--v-space-2);
	}

	.evidence {
		margin-top: var(--v-space-2);
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.ev-line,
	.ev-sub {
		margin: 0;
		font-size: var(--v-text-2xs);
		word-break: break-word;
	}

	.ev-line {
		color: var(--v-ink-2);
	}

	.ev-sub {
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
