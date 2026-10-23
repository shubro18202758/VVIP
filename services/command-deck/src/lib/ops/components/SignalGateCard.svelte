<script lang="ts">
	/**
	 * SignalGateCard — the city signal-plan gate: writer posture (enabled,
	 * its live state — writing / queued / idle — and the twin shadow-evals
	 * in flight as a COUNT), the band plan table (writes vs hold), the last
	 * plan, APPLY, RESTORE and the program history.
	 *
	 * The fail-closed refusal is the FEATURE: a 409 from POST /plans/apply
	 * renders INLINE — a firm red hairline, the words 'held at baseline',
	 * and the refusal report expanded — never a modal, never hidden, never a
	 * shake. RESTORE targets the selected LIVE event (a preview fixture has
	 * no signal history and a slug id the writer cannot cast — the button is
	 * disabled while one is selected); RESTORE ALL rebuilds every assumed
	 * program city-wide, so it asks once more before it commits. One restore
	 * at a time (a double click sends one POST). HISTORY lists the
	 * snapshots/restores with their actor, in IST; officer-typed reasons
	 * print under the plate rule. Smokes stub APPLY and RESTORE (no smoke
	 * ever fires a writer).
	 *
	 * An UNKNOWN city band (its engine poll is failing — the oracle answers
	 * 409 'city band unknown — pass band explicitly', or the band reads
	 * stale) is never applied as a GREEN default: the card says why and the
	 * officer picks the band to apply, explicitly, under their name.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops, isLiveUuidEvent } from '$lib/ops/store.svelte';
	import { POLL_MS } from '$lib/ops/endpoints';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtClock, istClock } from '$lib/ops/format';

	const lang = $derived(ops.lang);
	const plans = $derived(ops.signalPlans);
	const outcome = $derived(ops.signalApply);
	const gate = $derived(plans?.gate ?? null);
	const restorable = $derived(isLiveUuidEvent(ops.selectedEvent));

	let detailOpen = $state(false);
	let historyOpen = $state(false);
	/** RESTORE ALL is city-wide: the first click arms it, the second commits. */
	let confirmRestoreAll = $state(false);

	// An armed RESTORE ALL never outlives the officer who armed it.
	$effect(() => {
		if (!ops.officerOn) confirmRestoreAll = false;
	});

	async function restoreAll() {
		if (!confirmRestoreAll) {
			confirmRestoreAll = true;
			return;
		}
		confirmRestoreAll = false;
		await ops.restorePrograms(true);
	}

	/** The writer refused for want of a band (409), or the city band is stale: the officer names one. */
	const bandUnknown = $derived(
		(!!outcome && !outcome.applied && outcome.status === 409 && /city band unknown/i.test(outcome.refusalReason ?? '')) ||
			ops.cityBand?.stale === true,
	);
	const BANDS = ['GREEN', 'AMBER', 'RED', 'BLACK'] as const;

	const writerState = $derived.by(() => {
		if (!gate) return null;
		if (gate.writerActive) return { variant: 'pending' as const, label: tOps(lang, 'gateWriterActive') };
		if ((gate.writerWaiters ?? 0) > 0) return { variant: 'pending' as const, label: tOps(lang, 'gateWriterQueued', gate.writerWaiters) };
		return { variant: 'neutral' as const, label: tOps(lang, 'gateWriterIdle') };
	});
</script>

<Panel
	title={tOps(lang, 'gateTitle')}
	sub={tOps(lang, 'gateSub')}
	status={ops.signalPlansUpdated ? { state: ops.signalPlansNote ? 'stale' : 'live', updatedAt: ops.signalPlansUpdated } : null}
	pollMs={POLL_MS.signals}
	lang={lang}
>
	{#if plans}
		<div class="posture-row" data-testid="gate-posture">
			<Badge
				variant={plans.writerEnabled ? 'live' : 'not-deployed'}
				label={plans.writerEnabled ? tOps(lang, 'gateWriterEnabled') : tOps(lang, 'gateWriterDisabled')}
				title={plans.writerFlagEnv ?? undefined}
			/>
			{#if writerState}<Badge variant={writerState.variant} label={writerState.label} />{/if}
			<span data-testid="gate-twin" data-count={gate?.twinInFlight ?? ''}>
				<Badge
					variant={(gate?.twinInFlight ?? 0) > 0 ? 'pending' : 'neutral'}
					label={tOps(lang, 'gateTwinInFlight', gate?.twinInFlight ?? 0)}
					title="signal-policy gate: twin shadow-evals holding the writer"
				/>
			</span>
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
						<td class="num mono" class:writes={row.writes === 'writes'}>{row.writes ?? '—'}</td>
					</tr>
				{/each}
			</tbody>
		</table>

		{#if plans.lastPlan}
			<p class="last-plan">
				{tOps(lang, 'gateLastPlan')}:
				{#if plans.lastPlan.applied === false}
					<strong class="held">{tOps(lang, 'gateHeldBaseline')}</strong>
					{#if plans.lastPlan.reason}<span class="mono reason">{ops.text(plans.lastPlan.reason)}</span>{/if}
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
			<span title={restorable ? undefined : tOps(lang, 'gateRestoreNeedsLive')}>
				<Button
					variant="ghost"
					size="sm"
					busy={ops.signalRestoring === 'event'}
					disabled={!restorable || !ops.officerOn || ops.signalRestoring !== null}
					onclick={() => void ops.restorePrograms(false)}
				>
					<span data-testid="gate-restore">{tOps(lang, 'gateRestore')}</span>
				</Button>
			</span>
			<Button
				variant={confirmRestoreAll ? 'danger-outline' : 'ghost'}
				size="sm"
				busy={ops.signalRestoring === 'all'}
				disabled={!ops.officerOn || ops.signalRestoring !== null}
				onclick={() => void restoreAll()}
			>
				<span data-testid="gate-restore-all" data-armed={confirmRestoreAll ? '1' : '0'}>
					{confirmRestoreAll ? tOps(lang, 'gateRestoreAllConfirm') : tOps(lang, 'gateRestoreAll')}
				</span>
			</Button>
			{#if confirmRestoreAll}
				<button class="link" type="button" onclick={() => (confirmRestoreAll = false)}>{tOps(lang, 'gateRestoreAllKeep')}</button>
			{/if}
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
		{#if !restorable}<p class="hint mono">{tOps(lang, 'gateRestoreNeedsLive')}</p>{/if}

		{#if bandUnknown}
			<div class="band-pick" data-testid="gate-band-unknown">
				<p class="hint mono">{tOps(lang, 'gateBandUnknown')}</p>
				<div class="band-row">
					{#each BANDS as b (b)}
						<Button
							variant="ghost"
							size="sm"
							busy={ops.signalApplying}
							disabled={!ops.officerOn || ops.signalApplying}
							onclick={() => void ops.applyCityPlan(b)}
							title={tOps(lang, 'gateApplyBand', b)}
						>
							<span data-testid="gate-apply-band" data-band={b}><BandChip band={b} size="sm" /></span>
						</Button>
					{/each}
				</div>
			</div>
		{/if}

		{#if outcome}
			{#if outcome.applied}
				<div class="outcome applied" role="status">
					<span class="outcome-word">{tOps(lang, 'gateApplied')}</span>
					<span class="mono">{fmtClock(outcome.at)} IST · {outcome.status}</span>
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
						<pre class="held-detail mono">{JSON.stringify(ops.detail(outcome.detail), null, 2)}</pre>
					{/if}
				</div>
			{/if}
		{/if}

		{#if historyOpen}
			<div class="history" data-testid="gate-history">
				{#if ops.signalHistory.length === 0}
					<p class="history-empty">{ops.signalHistoryNote ?? tOps(lang, 'gateHistoryEmpty')}</p>
				{:else}
					<ul class="history-rows">
						{#each ops.signalHistory as h (h.historyId)}
							<li class="mono">
								<span class="h-time">{h.capturedAt ? istClock(h.capturedAt) : '—'}</span>
								<span class="h-kind" class:restore={h.kind === 'restore'}>{h.kind}</span>
								<span class="h-j">J{h.junctionId ?? '—'}{ops.junctionName(h.junctionId) ? ` ${ops.junctionName(h.junctionId)}` : ''}</span>
								<span class="h-actor">{h.actor ?? tOps(lang, 'gateHistoryUnattributed')}</span>
								{#if h.reason}<span class="h-reason">{ops.text(h.reason)}</span>{/if}
							</li>
						{/each}
					</ul>
					{#if ops.signalHistoryNote}<p class="history-empty">{ops.signalHistoryNote}</p>{/if}
				{/if}
			</div>
		{/if}

		{#if plans.scope}<p class="scope mono">{plans.scope}</p>{/if}
	{:else}
		<EmptyState title={ops.signalPlansNote ?? tOps(lang, 'labelNotDeployed')} />
	{/if}
</Panel>

<style>
	.band-pick {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		margin-top: var(--v-space-2);
		padding: var(--v-space-2);
		border: 1px dashed var(--v-band-amber);
		border-radius: var(--v-radius-chip);
	}

	.band-row {
		display: flex;
		gap: var(--v-space-1);
		flex-wrap: wrap;
	}

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
		color: var(--v-ink-3);
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
		color: var(--v-ink-3);
		margin-left: var(--v-space-2);
	}

	.apply-row {
		display: flex;
		gap: var(--v-space-2);
		margin-top: var(--v-space-3);
		flex-wrap: wrap;
		align-items: center;
	}

	.link {
		font: inherit;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		background: transparent;
		border: none;
		border-bottom: 1px dashed var(--v-line-2);
		padding: 0;
		cursor: pointer;
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
		color: var(--v-ink-3);
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
		color: var(--v-ink-3);
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
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border-bottom: 1px solid var(--v-line-1);
		padding: 1px 0;
	}

	.h-time {
		color: var(--v-ink-2);
	}

	.h-kind {
		color: var(--v-data);
	}

	.h-kind.restore {
		color: var(--v-band-amber);
	}

	.h-actor {
		color: var(--v-act-text);
	}

	.h-reason {
		color: var(--v-ink-3);
		overflow-wrap: anywhere;
	}

	.hint {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.plan-table td.writes {
		color: var(--v-ink-1);
	}

	.scope {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
