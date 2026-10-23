<script lang="ts">
	/**
	 * TwinComparePanel — twin shadow-eval comparison on the design system.
	 * Behavior preserved exactly (POST /simulate/twin/compare with the
	 * labelled two-arm fallback, cached per scenario/policies/seed, run only
	 * on operator demand); every number renders mono.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { tOps } from '$lib/i18n/strings.ops';
	import { USE_CASES, useCaseById } from '$lib/ops/usecases';
	import { istClock } from '$lib/ops/format';
	import type { ClosureType } from '$lib/ops/types';

	const CLOSURE_TYPES: ClosureType[] = ['full_closure', 'partial_closure', 'signal_hold'];

	let selectedScenario = $state(ops.twinScenarioId);
	let candidateAdvance = $state(ops.twinCandidate.advanceClosureSec);
	let candidateClosure = $state<ClosureType>(ops.twinCandidate.closureType);

	$effect(() => {
		// Keep the store in sync with the local form controls.
		ops.twinScenarioId = selectedScenario;
		ops.twinCandidate = { advanceClosureSec: candidateAdvance, closureType: candidateClosure };
	});

	let baselinePolicy = $derived(useCaseById(selectedScenario)?.baselinePolicy ?? null);
	let isCached = $derived(ops.twinCachedKey === ops.twinCacheKey() && ops.twinResult !== null);

	const lang = $derived(ops.lang);

	function fmt(n: number | null | undefined, digits = 1): string {
		return n == null || !Number.isFinite(n) ? '—' : n.toFixed(digits);
	}

	function deltaCell(v: number | null | undefined, better: 'lower' | 'higher' = 'lower'): { text: string; cls: string } {
		if (v == null || !Number.isFinite(v)) return { text: '—', cls: '' };
		const good = better === 'lower' ? v < 0 : v > 0;
		return { text: `${v > 0 ? '+' : ''}${fmt(v)}`, cls: v === 0 ? 'zero' : good ? 'good' : 'bad' };
	}

	const METRICS: Array<{ key: string; labelKey: string; better: 'lower' | 'higher' }> = [
		{ key: 'disruptionVehicleHours', labelKey: 'twinDisruption', better: 'lower' },
		{ key: 'maxQueueLengthM', labelKey: 'twinQueue', better: 'lower' },
		{ key: 'avgClosureDurationSec', labelKey: 'twinClosureDur', better: 'lower' },
		{ key: 'transitTimeSec', labelKey: 'twinTransit', better: 'lower' },
		{ key: 'recoverySec', labelKey: 'twinRecovery', better: 'lower' },
		{ key: 'ambulanceDelaySec', labelKey: 'twinAmbulance', better: 'lower' },
	];
</script>

{#snippet headRight()}
	{#if ops.twinResult}
		<Badge
			variant={ops.twinResult.source === 'compare-endpoint' ? 'live' : 'fixture'}
			label={ops.twinResult.source === 'compare-endpoint' ? t(lang, 'twinSourceCompare') : t(lang, 'twinSourceDirect')}
		/>
	{/if}
{/snippet}

<Panel title={t(lang, 'twinTitle')} sub={t(lang, 'twinSub')} lang={lang} {...{ 'header-right': headRight }}>
	<div class="controls">
		<label>
			<span>scenario</span>
			<select bind:value={selectedScenario}>
				{#each USE_CASES as u (u.id)}
					<option value={u.scenarioId}>{u.name}</option>
				{/each}
			</select>
		</label>
		<label>
			<span>{t(lang, 'twinAdvance')}</span>
			<input class="mono" type="number" min="0" max="1800" step="10" bind:value={candidateAdvance} />
		</label>
		<label>
			<span>{t(lang, 'twinClosure')}</span>
			<select bind:value={candidateClosure}>
				{#each CLOSURE_TYPES as ct (ct)}
					<option value={ct}>{ct}</option>
				{/each}
			</select>
		</label>
		<Button variant="secondary" size="sm" busy={ops.twinRunning} onclick={() => void ops.runTwinCompare(true)}>
			{ops.twinRunning ? t(lang, 'twinRunning') : t(lang, 'twinRun')}
		</Button>
	</div>

	{#if baselinePolicy}
		<p class="policy-line mono">
			{t(lang, 'twinBaseline')}: {baselinePolicy.advanceClosureSec}s {baselinePolicy.closureType}
			· {t(lang, 'twinCandidate')}: {candidateAdvance}s {candidateClosure}
			{#if useCaseById(selectedScenario)}
				· {t(lang, 'twinReplications', useCaseById(selectedScenario)!.nReplications)}
				· seed {useCaseById(selectedScenario)!.seed}
			{/if}
		</p>
	{/if}

	{#if ops.twinError}
		<p class="error">{ops.twinError}</p>
	{/if}

	{#if ops.twinResult}
		{#if isCached}<p class="cached mono">{t(lang, 'twinCached')}</p>{/if}
		<table class="results">
			<thead>
				<tr>
					<th></th>
					<th class="num">{t(lang, 'twinBaseline')}</th>
					<th class="num">{t(lang, 'twinCandidate')}</th>
					<th class="num">{t(lang, 'twinDelta')}</th>
				</tr>
			</thead>
			<tbody>
				{#each METRICS as m (m.key)}
					{@const d = deltaCell(ops.twinResult!.deltas[m.key], m.better)}
					<tr>
						<td class="metric">{t(lang, m.labelKey)}</td>
						<td class="num">{fmt(ops.twinResult!.baseline.metrics[m.key as keyof typeof ops.twinResult.baseline.metrics])}</td>
						<td class="num">{fmt(ops.twinResult!.candidate.metrics[m.key as keyof typeof ops.twinResult.candidate.metrics])}</td>
						<td class="num delta {d.cls}">{d.text}</td>
					</tr>
				{/each}
			</tbody>
		</table>
		<p class="meta mono">
			{#if ops.twinResult.calibrated != null}
				<span title={`calibrated=${String(ops.twinResult.calibrated)}`}>{tOps(ops.lang, ops.twinResult.calibrated ? 'twinCalibratedYes' : 'twinCalibratedNo')}</span> ·
			{/if}
			{#if ops.twinResult.forecastCoupling}
				<span title={`forecast_coupling=${ops.twinResult.forecastCoupling}`}>{tOps(ops.lang, 'twinForecastCoupling', ops.twinResult.forecastCoupling)}</span> ·
			{/if}
			{istClock(ops.twinResult.fetchedAt)} IST
		</p>
	{:else if !ops.twinRunning}
		<EmptyState title="Run a comparison to see both twin arms." />
	{/if}
</Panel>

<style>
	.controls {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-2);
		align-items: flex-end;
	}

	.controls label {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.controls label span {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.controls select,
	.controls input {
		height: var(--v-ctl-sm);
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: 0 var(--v-space-2);
	}

	.controls input {
		width: 76px;
	}

	.policy-line {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.error {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-band-red);
	}

	.cached {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.results {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--v-text-2xs);
	}

	.results th {
		text-align: right;
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
		padding: var(--v-space-1) var(--v-space-2);
		border-bottom: 1px solid var(--v-line-2);
	}

	.results td {
		padding: var(--v-space-1) var(--v-space-2);
		border-bottom: 1px solid var(--v-line-1);
		color: var(--v-ink-2);
	}

	.metric {
		color: var(--v-ink-3);
	}

	.num {
		text-align: right;
		font-family: var(--v-font-mono);
	}

	.delta.good {
		color: var(--v-band-green);
	}

	.delta.bad {
		color: var(--v-band-red);
	}

	.meta {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
