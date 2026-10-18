<script lang="ts">
	/**
	 * SignalGateCard — the city signal-plan gate: writer posture, the band
	 * plan table, the last plan, and APPLY.
	 *
	 * The fail-closed refusal is the FEATURE: a 409 from POST /plans/apply
	 * renders INLINE — a firm red hairline, the words 'held at baseline',
	 * and the refusal report expanded — never a modal, never hidden, never a
	 * shake. RESTORE and HISTORY are one click away. Smokes stub APPLY (the
	 * shared oracle has writer_enabled:true — no smoke ever fires a writer).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtClock } from '$lib/ops/format';

	const lang = $derived(ops.lang);
	const plans = $derived(ops.signalPlans);
	const outcome = $derived(ops.signalApply);

	let detailOpen = $state(false);
	let historyOpen = $state(false);
</script>

<Panel
	title={tOps(lang, 'gateTitle')}
	sub={tOps(lang, 'gateSub')}
	status={ops.signalPlansUpdated ? { state: ops.signalPlansNote ? 'stale' : 'live', updatedAt: ops.signalPlansUpdated } : null}
	lang={lang}
>
	{#if plans}
		<div class="posture-row">
			<Badge
				variant={plans.writerEnabled ? 'live' : 'not-deployed'}
				label={plans.writerEnabled ? tOps(lang, 'gateWriterEnabled') : tOps(lang, 'gateWriterDisabled')}
				title={plans.writerFlagEnv ?? undefined}
			/>
			{#if plans.cityBand}<BandChip band={plans.cityBand} size="sm" />{/if}
			{#if plans.bandSubjects != null}<span class="subjects mono">{tOps(lang, 'gateSubjects', plans.bandSubjects)}</span>{/if}
		</div>

		<table class="plan-table">
			<thead>
				<tr>
					<th>{tOps(lang, 'gatePlanTableBand')}</th>
					<th class="num">{tOps(lang, 'gatePlanTableShare')}</th>
					<th class="num">{tOps(lang, 'gatePlanTableFloor')}</th>
					<th class="num">{tOps(lang, 'gatePlanTableWrites')}</th>
				</tr>
			</thead>
			<tbody>
				{#each plans.bandPlanTable as row (row.band)}
					<tr class:current={plans.cityBand === row.band}>
						<td><BandChip band={row.band} size="sm" /></td>
						<td class="num mono">{row.priorityShare != null ? row.priorityShare.toFixed(2) : '—'}</td>
						<td class="num mono">{row.crossGreenFloorSec ?? '—'}</td>
						<td class="num mono">{row.writes ?? '—'}</td>
					</tr>
				{/each}
			</tbody>
		</table>

		{#if plans.lastPlan}
			<p class="last-plan">
				{tOps(lang, 'gateLastPlan')}:
				{#if plans.lastPlan.applied === false}
					<strong class="held">{tOps(lang, 'gateHeldBaseline')}</strong>
					{#if plans.lastPlan.reason}<span class="mono reason">{plans.lastPlan.reason}</span>{/if}
				{:else}
					<span class="mono">{plans.lastPlan.plannedJunctions.length} junctions</span>
				{/if}
			</p>
		{/if}

		<!-- The APPLY half. The refusal state below is not an error path. -->
		<div class="apply-row">
			<Button
				variant="primary"
				size="sm"
				busy={ops.signalApplying}
				actor={ops.officerOn ? ops.dutyOfficer : null}
				onclick={() => void ops.applyCityPlan()}
			>
				{tOps(lang, 'gateApply')}
			</Button>
			<Button variant="ghost" size="sm" actor={ops.officerOn ? ops.dutyOfficer : null} onclick={() => void ops.restorePrograms(false)}>
				{tOps(lang, 'gateRestore')}
			</Button>
			<Button
				variant="ghost"
				size="sm"
				onclick={() => {
					historyOpen = !historyOpen;
					if (historyOpen) void ops.loadSignalHistory();
				}}
			>
				{tOps(lang, 'gateHistory')}
			</Button>
		</div>

		{#if outcome}
			{#if outcome.applied}
				<div class="outcome applied" role="status">
					<span class="outcome-word">{tOps(lang, 'gateApplied')}</span>
					<span class="mono">{fmtClock(outcome.at)} · {outcome.status}</span>
				</div>
			{:else}
				<!-- HELD AT BASELINE — inline, dignified, in full view. -->
				<div class="outcome held" role="status">
					<div class="held-line">
						<span class="held-word">{tOps(lang, 'gateHeldBaseline')}</span>
						{#if outcome.refusalReason}
							<span class="held-reason mono">{tOps(lang, 'gateRefusal')}: {outcome.refusalReason}</span>
						{/if}
						<span class="mono held-status">{outcome.status}</span>
					</div>
					<button class="held-toggle" type="button" onclick={() => (detailOpen = !detailOpen)}>
						{detailOpen ? tOps(lang, 'queueHideEvidence') : tOps(lang, 'gateRefusal')} · {tOps(lang, 'queueEvidence')}
					</button>
					{#if detailOpen && outcome.detail}
						<pre class="held-detail mono">{JSON.stringify(outcome.detail, null, 2)}</pre>
					{/if}
				</div>
			{/if}
		{/if}

		{#if historyOpen}
			<div class="history">
				{#if ops.signalHistory.length === 0}
					<p class="history-empty">{tOps(lang, 'gateHistoryEmpty')}</p>
				{:else}
					<ul class="history-rows">
						{#each ops.signalHistory as h, i (i)}
							<li class="mono">{JSON.stringify(h).slice(0, 200)}</li>
						{/each}
					</ul>
				{/if}
			</div>
		{/if}

		{#if plans.scope}<p class="scope mono">{plans.scope}</p>{/if}
	{:else}
		<EmptyState title={ops.signalPlansNote ?? tOps(lang, 'labelNotDeployed')} />
	{/if}
</Panel>

<style>
	.posture-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
		margin-bottom: var(--v-space-2);
	}

	.subjects {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.plan-table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--v-text-2xs);
	}

	.plan-table th {
		text-align: left;
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-4);
		padding: var(--v-space-1) var(--v-space-2);
		border-bottom: 1px solid var(--v-line-2);
	}

	.plan-table td {
		padding: var(--v-space-1) var(--v-space-2);
		border-bottom: 1px solid var(--v-line-1);
		color: var(--v-ink-2);
	}

	.plan-table tr.current td {
		background: var(--v-band-veil);
	}

	.num {
		text-align: right;
		font-family: var(--v-font-mono);
	}

	.last-plan {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.last-plan .held {
		color: var(--v-band-red);
	}

	.last-plan .reason {
		color: var(--v-ink-4);
		margin-left: var(--v-space-2);
	}

	.apply-row {
		display: flex;
		gap: var(--v-space-2);
		margin-top: var(--v-space-3);
		flex-wrap: wrap;
	}

	.outcome {
		margin-top: var(--v-space-2);
		padding: var(--v-space-2) var(--v-space-3);
		border-radius: var(--v-radius-panel);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.outcome.applied {
		border: 1px solid var(--v-band-green);
	}

	.outcome-word {
		font-size: var(--v-text-xs);
		color: var(--v-band-green);
	}

	/* The firm red hairline — the doctrine's fail-closed render. */
	.outcome.held {
		border: 1px solid var(--v-line-1);
		border-top: 2px solid var(--v-band-red);
	}

	.held-line {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-3);
		flex-wrap: wrap;
	}

	.held-word {
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-band-red);
	}

	.held-reason {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.held-status {
		margin-left: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.held-toggle {
		align-self: flex-start;
		font: inherit;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		background: transparent;
		border: none;
		padding: 0;
		cursor: pointer;
		border-bottom: 1px dashed var(--v-line-2);
	}

	.held-detail {
		margin: 0;
		padding: var(--v-space-2);
		max-height: 180px;
		overflow: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		white-space: pre-wrap;
		word-break: break-all;
	}

	.history {
		margin-top: var(--v-space-2);
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.history-empty {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.history-rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 140px;
		overflow: auto;
	}

	.history-rows li {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		word-break: break-all;
	}

	.scope {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
