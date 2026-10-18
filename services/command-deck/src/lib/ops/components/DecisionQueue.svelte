<script lang="ts">
	/**
	 * DecisionQueue — everything a machine has proposed and a human must
	 * dispose of, on DecisionCards:
	 *
	 *   · pending e-challan recommendations (GET /enforcement/recommendations)
	 *     with APPROVE / REJECT-WITH-REASON → POST /{id}/decision under the
	 *     named-officer gate (C6) — the real backend approval that replaces
	 *     the legacy client-only Approve button;
	 *   · 108 corridor requests (ambulance dispatches in 'requested') with
	 *     the golden-hour counter;
	 *   · pursuit-open confirmations from the watchlist (human-initiated).
	 *
	 * Every commit renders through Button primary with `actor` — the
	 * primitive refuses to enable without the named officer.
	 */
	import DecisionCard, { type DecisionAction } from '$lib/primitives/DecisionCard.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtGoldenHour, fmtRetentionUntil } from '$lib/ops/format';

	const lang = $derived(ops.lang);

	// ── e-challan cards ────────────────────────────────────────────────────
	let openEvidence = $state<string | null>(null);
	let rejectOpen = $state<string | null>(null);
	let notes = $state<Record<string, string>>({});

	const pending = $derived(ops.recommendations.filter((r) => r.status === 'pending'));

	function retentionOf(vehicleId: string | null): string {
		const row = ops.watchlist.find((w) => w.vehicleId === vehicleId);
		return fmtRetentionUntil(row?.retentionUntil ?? null);
	}

	function evidenceRows(ev: Record<string, unknown> | null): Array<[string, string]> {
		if (!ev) return [];
		const out: Array<[string, string]> = [];
		for (const [k, v] of Object.entries(ev)) {
			if (v == null) continue;
			out.push([k, typeof v === 'object' ? JSON.stringify(v) : String(v)]);
		}
		return out.slice(0, 10);
	}

	async function onEchallan(id: string, action: 'approve' | 'reject') {
		if (action === 'reject' && rejectOpen !== id) {
			rejectOpen = id;
			return;
		}
		await ops.decideRecommendation(id, action, notes[id] ?? '');
		rejectOpen = null;
	}

	// ── 108 corridor-request cards (ambulance dispatches awaiting a human) ──
	const corridorRequests = $derived(
		ops.dispatches.filter((d) => d.agency === 'ambulance' && d.status === 'requested'),
	);

	async function onCorridor(dispatchId: string, action: 'approve' | 'defer') {
		await ops.decideDispatchAction(dispatchId, action);
	}

	// ── pursuit confirmations (watchlist flagged + live sighting) ──────────
	const pursuitCandidates = $derived(
		ops.watchlist.filter((w) => w.earmarkState === 'flagged' && w.lastSighting),
	);

	const anchorSegment = $derived(ops.selectedEvent?.routeSegmentIds[0] ?? null);

	async function onPursuit(vehicleId: string, plate: string | null) {
		if (!anchorSegment) return;
		await ops.openPursuit(vehicleId, plate, anchorSegment);
	}
</script>

<div class="queue">
	{#if pending.length === 0 && corridorRequests.length === 0 && pursuitCandidates.length === 0}
		<EmptyState title={tOps(lang, 'queueEmpty')} />
	{/if}

	{#each pending as rec (rec.recommendationId)}
		<DecisionCard
			kind="echallan"
			title={`${rec.plate ?? rec.recommendationId.slice(0, 8)} · ${rec.recommendation ?? tOps(lang, 'queueEchallan')}`}
			deadline={retentionOf(rec.vehicleId)}
			namedActor={ops.officerOn ? ops.dutyOfficer : null}
			onaction={(id) => onEchallan(rec.recommendationId, id as 'approve' | 'reject')}
			lang={lang}
			actions={[
				{ id: 'approve', label: tOps(lang, 'queueApprove'), kind: 'commit' },
				{ id: 'reject', label: tOps(lang, 'queueReject'), kind: 'reject' }
			]}
		>
			{#snippet meta()}
				{#if rec.ruleId}<span class="meta-chip mono">{tOps(lang, 'queueRule')} {rec.ruleId}</span>{/if}
				<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} title="source: emulated_anpr" />
			{/snippet}
			{#snippet evidence()}
				<button class="ev-toggle" type="button" onclick={() => (openEvidence = openEvidence === rec.recommendationId ? null : rec.recommendationId)}>
					{openEvidence === rec.recommendationId ? tOps(lang, 'queueHideEvidence') : tOps(lang, 'queueEvidence')}
				</button>
				{#if rejectOpen === rec.recommendationId}
					<div class="reject-row">
						<input
							class="note-input"
							type="text"
							placeholder={tOps(lang, 'queueReasonPlaceholder')}
							bind:value={notes[rec.recommendationId]}
						/>
						<button class="ev-toggle danger" type="button" onclick={() => onEchallan(rec.recommendationId, 'reject')}>
							{tOps(lang, 'queueReject')}
						</button>
					</div>
				{/if}
				{#if openEvidence === rec.recommendationId}
					<dl class="evidence">
						{#each evidenceRows(rec.evidence) as [k, v] (k)}
							<div class="ev-row"><dt class="mono">{k}</dt><dd class="mono">{v}</dd></div>
						{/each}
						<div class="ev-row"><dt class="mono">sighting_id</dt><dd class="mono">{rec.sightingId ?? '—'}</dd></div>
						<div class="ev-row"><dt class="mono">filed</dt><dd class="mono">{rec.createdAt ?? '—'}</dd></div>
					</dl>
				{/if}
			{/snippet}
		</DecisionCard>
	{/each}

	{#each corridorRequests as d (d.dispatchId)}
		<DecisionCard
			kind="corridor-request"
			title={`${d.agencyLabel ?? d.agency} · ${d.unitId ?? 'unit'}`}
			deadline={`${tOps(lang, 'queueGoldenHour')} ${fmtGoldenHour(d.createdAt)}`}
			namedActor={ops.officerOn ? ops.dutyOfficer : null}
			onaction={(id) => onCorridor(d.dispatchId, id as 'approve' | 'defer')}
			lang={lang}
			actions={[
				{ id: 'approve', label: tOps(lang, 'dispatchApprove'), kind: 'commit' },
				{ id: 'defer', label: tOps(lang, 'dispatchDefer'), kind: 'defer' }
			]}
		>
			{#snippet meta()}
				{#if d.waypointJunction != null}
					<span class="meta-chip mono">{tOps(lang, 'dispatchAtWaypoint', d.waypointJunction)}</span>
				{/if}
			{/snippet}
		</DecisionCard>
	{/each}

	{#each pursuitCandidates.filter((w) => !ops.interception || ops.interception.vehicleId !== w.vehicleId) as w (w.vehicleId)}
		<DecisionCard
			kind="pursuit"
			title={`${w.plate ?? w.vehicleId.slice(0, 8)} · ${w.earmarkReason ?? tOps(lang, 'watchState')}`}
			deadline={w.lastSighting ? `${tOps(lang, 'watchLastSeen')} ${w.lastSighting.t?.slice(11, 19) ?? '—'}` : null}
			namedActor={ops.officerOn ? ops.dutyOfficer : null}
			onaction={() => onPursuit(w.vehicleId, w.plate)}
			lang={lang}
			actions={[{ id: 'pursuit', label: tOps(lang, 'queueOpenPursuit'), kind: 'commit' }]}
		>
			{#snippet meta()}
				<Badge variant="act" label={tOps(lang, 'queuePursuitConfirm')} />
				{#if anchorSegment}
					<span class="meta-chip mono">{tOps(lang, 'pursuitSeedSegment')} #{anchorSegment}</span>
				{/if}
			{/snippet}
		</DecisionCard>
	{/each}
</div>

<style>
	.queue {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
	}

	.meta-chip {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
		white-space: nowrap;
	}

	.ev-toggle {
		align-self: flex-start;
		font: inherit;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
		background: transparent;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: 2px var(--v-space-2);
		cursor: pointer;
	}

	.ev-toggle:hover {
		border-color: var(--v-line-select);
		color: var(--v-ink-1);
	}

	.ev-toggle.danger {
		border-color: var(--v-band-red);
		color: var(--v-band-red);
	}

	.reject-row {
		display: flex;
		gap: var(--v-space-2);
		width: 100%;
	}

	.note-input {
		flex: 1;
		min-width: 0;
		height: var(--v-ctl-md);
		padding: 0 var(--v-space-2);
		font: inherit;
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.evidence {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		width: 100%;
	}

	.ev-row {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-3);
		border-bottom: 1px solid var(--v-line-1);
		padding: 2px 0;
	}

	.ev-row dt {
		color: var(--v-ink-4);
		font-size: var(--v-text-2xs);
	}

	.ev-row dd {
		margin: 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-2xs);
		text-align: right;
		word-break: break-all;
		max-width: 60%;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
