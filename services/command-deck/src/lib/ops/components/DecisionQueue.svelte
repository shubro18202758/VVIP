<script lang="ts">
	/**
	 * DecisionQueue — everything a machine has proposed and a human must
	 * dispose of, on DecisionCards:
	 *
	 *   · pending e-challan recommendations (GET /enforcement/recommendations)
	 *     with APPROVE / REJECT-WITH-REASON → POST /{id}/decision under the
	 *     named-officer gate (C6). The card is `busy` while its decision is in
	 *     flight, so a double click sends ONE POST. Its countdown is the
	 *     EVIDENCE PURGE (retention), said so with a tooltip;
	 *   · stolen vehicles (C-N): the oracle files R_STOLEN_SIGHTING — evidence,
	 *     never a fine — grouped per vehicle into ONE card ('Stolen vehicle
	 *     sighted — e-challan withheld (vehicle reported stolen, FIR …)') with
	 *     AUTHORISE PURSUIT (approve the newest sighting: the C6 pursuit gate's
	 *     human approval) and DISMISS (reject them, with a reason). A PENAL
	 *     card for a vehicle whose watchlist basis is stolen_vehicle_fir shows
	 *     a warning chip and NO approve — only reject with a note: the owner
	 *     is the theft victim. An oracle 409 ('e-challan withheld — vehicle
	 *     reported stolen') is printed verbatim and never retried;
	 *   · ESCORT PROPOSALS (officer approves every switch) — the escort lane
	 *     first: each running escort's ONE open proposal (a route switch,
	 *     Plan B, a hold, new closures) with what it would do, the decider's
	 *     tier and confidence and its expiry countdown; APPROVE / REJECT
	 *     under the named officer (the store's busy guard — one POST per
	 *     click, shared with the cockpit HUD). Unanswered, it expires and the
	 *     convoy holds its current approved plan. A proposal the brain closed
	 *     leaves a receipt line for 90 s (approved by / rejected by <officer>,
	 *     expired — current plan held, superseded), whichever console decided;
	 *   · 108 corridor requests — real ambulance dispatches in 'requested'
	 *     (the /driver page and the living city file them) with the
	 *     golden-hour counter, /driver requests first;
	 *   · pursuit openings for FLAGGED vehicles with a latest sighting and no
	 *     open pursuit: the card anchors on the segment of the vehicle's
	 *     latest sighting (the interception seed rule) and opens from the
	 *     newest human-APPROVED recommendation for it. When either link is
	 *     missing the card says which, inline, and cannot commit.
	 *
	 * Plates and recommendation texts are masked unless a duty officer is
	 * named (C6). Every commit renders through Button primary with `actor`
	 * — the primitive refuses to enable without the named officer. The
	 * EMULATED chip hides (or shows) everything the emulator produced —
	 * emulated evidence, an emulated requester, an emulated event — with
	 * the hidden count said, never silently.
	 */
	import DecisionCard from '$lib/primitives/DecisionCard.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtAgeSince, fmtGoldenHour, fmtRetentionUntil, istClock } from '$lib/ops/format';
	import { approvalSpent, isEmulatedActor } from '$lib/ops/client';
	import type { DispatchRecord, EnforcementRecommendation, EscortProposal } from '$lib/ops/types';

	const lang = $derived(ops.lang);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	// ── e-challan cards ────────────────────────────────────────────────────
	let openEvidence = $state<string | null>(null);
	let rejectOpen = $state<string | null>(null);
	let notes = $state<Record<string, string>>({});

	const STOLEN_RULE = 'R_STOLEN_SIGHTING';

	const pendingAll = $derived(ops.recommendations.filter((r) => r.status === 'pending'));
	/** Penal e-challans (everything but the evidence-only stolen sightings). */
	const pending = $derived(pendingAll.filter((r) => r.ruleId !== STOLEN_RULE && !(ops.hideEmulated && emulatedRec(r))));

	/** R_STOLEN_SIGHTING per vehicle, newest first — one card per stolen vehicle. */
	const stolenGroups = $derived.by(() => {
		const by = new Map<string, EnforcementRecommendation[]>();
		for (const r of pendingAll) {
			if (r.ruleId !== STOLEN_RULE || (ops.hideEmulated && emulatedRec(r))) continue;
			const key = r.vehicleId ?? r.recommendationId;
			const list = by.get(key) ?? [];
			list.push(r);
			by.set(key, list);
		}
		return [...by.entries()].map(([vehicleId, recs]) => {
			const sorted = [...recs].sort((a, b) => Date.parse(b.createdAt ?? '') - Date.parse(a.createdAt ?? ''));
			// an approval already spent on a closed pursuit (C6: one approval, one
			// pursuit) never stands in for the officer's next authorisation
			const approved =
				ops.approvedRecommendations.find((a) => a.vehicleId === vehicleId && a.ruleId === STOLEN_RULE && !approvalSpent(a)) ?? null;
			return { vehicleId, recs: sorted, newest: sorted[0], approved };
		});
	});

	/**
	 * The FIR reference of a stolen vehicle: the oracle's own evidence.fir_ref
	 * (C-N), else extracted from the sighting text / earmark reason the way
	 * the oracle does — the word FIR on its own (never 'confirmed'), and a
	 * reference that carries a digit.
	 */
	function firOf(recs: EnforcementRecommendation[], vehicleId: string | null): string | null {
		for (const r of recs) {
			const ref = r.evidence?.fir_ref;
			if (typeof ref === 'string' && ref.trim()) return ref.trim();
		}
		const texts = [...recs.map((r) => r.recommendation ?? ''), ops.watchlist.find((w) => w.vehicleId === vehicleId)?.earmarkReason ?? ''];
		for (const text of texts) {
			const m = /\bFIR\b[\s:#.-]*(?:No\b\.?\s*)?([A-Za-z0-9][A-Za-z0-9/-]{0,31})/i.exec(text);
			const ref = m?.[1].replace(/[/-]+$/, '');
			if (ref && /\d/.test(ref)) return ref;
		}
		return null;
	}

	/** Emulated items the chip is hiding right now (said, never silent). */
	const hiddenEmulated = $derived(
		ops.hideEmulated
			? pendingAll.filter(emulatedRec).length +
					ops.openDispatches.filter((d) => d.agency === 'ambulance' && d.status === 'requested' && emulatedDispatch(d)).length +
					ops.pursuitCandidates.filter((p) => p.vehicle.lastSighting?.source === 'emulated_anpr').length
			: 0,
	);
	const anyEmulated = $derived(
		pendingAll.some(emulatedRec) ||
			ops.openDispatches.some((d) => d.agency === 'ambulance' && d.status === 'requested' && emulatedDispatch(d)) ||
			ops.pursuitCandidates.some((p) => p.vehicle.lastSighting?.source === 'emulated_anpr'),
	);

	function retentionOf(vehicleId: string | null): string {
		const row = ops.watchlist.find((w) => w.vehicleId === vehicleId);
		return tOps(lang, 'queueEvidencePurge', fmtRetentionUntil(row?.retentionUntil ?? null, clock));
	}

	/** Evidence rows for the expander — plates masked unless an officer is named. */
	function evidenceRows(ev: Record<string, unknown> | null): Array<[string, string]> {
		if (!ev) return [];
		const out: Array<[string, string]> = [];
		for (const [k, v] of Object.entries(ev)) {
			if (v == null) continue;
			const text = typeof v === 'object' ? JSON.stringify(v) : String(v);
			out.push([k, k === 't' ? `${istClock(text)} IST` : ops.text(text)]);
		}
		return out.slice(0, 12);
	}

	function emulatedRec(rec: EnforcementRecommendation): boolean {
		const src = rec.evidence?.['source'];
		return typeof src === 'string' && src.toLowerCase().includes('emulat');
	}

	/** A dispatch the emulator produced: its event is emulated, or its requester is. */
	function emulatedDispatch(d: DispatchRecord): boolean {
		return !!ops.eventById(d.eventId)?.emulated || isEmulatedActor(d.requestedBy);
	}

	async function onEchallan(id: string, action: 'approve' | 'reject') {
		if (action === 'reject' && rejectOpen !== id) {
			rejectOpen = id;
			return;
		}
		if (action === 'reject' && !(notes[id] ?? '').trim()) return; // a rejection names its reason
		await ops.decideRecommendation(id, action, notes[id] ?? '');
		rejectOpen = null;
	}

	/**
	 * A stolen vehicle's card: AUTHORISE PURSUIT approves the newest sighting
	 * (the human approval the C6 pursuit gate needs — not a fine); DISMISS
	 * rejects every pending sighting of the vehicle under one reason.
	 */
	async function onStolen(group: { vehicleId: string; recs: EnforcementRecommendation[]; newest: EnforcementRecommendation }, action: string) {
		const key = `stolen:${group.vehicleId}`;
		if (action === 'authorise') {
			await ops.decideRecommendation(group.newest.recommendationId, 'approve', tOps(lang, 'queueStolenAuthoriseNote'));
			return;
		}
		if (rejectOpen !== key) {
			rejectOpen = key;
			return;
		}
		const note = (notes[key] ?? '').trim();
		if (!note) return;
		for (const r of group.recs) {
			if (!(await ops.decideRecommendation(r.recommendationId, 'reject', note))) break;
		}
		rejectOpen = null;
	}

	// ── escort lane: the AI's open proposals (officer approves every switch) ──
	const escortProposals = $derived(ops.escortProposals);
	/** How long a closed proposal's receipt stays in the lane. */
	const ESCORT_RECEIPT_MS = 90_000;
	/**
	 * Proposals that closed in the last ESCORT_RECEIPT_MS — approved by /
	 * rejected by a named officer, expired (current plan held), superseded —
	 * newest first: the card leaves the queue once the brain closes it, the
	 * receipt says how it ended (on every console, whoever decided).
	 */
	const escortReceipts = $derived(
		ops.escorts
			.flatMap((lane) =>
				lane.outcomes
					.filter((o) => o.decidedAt != null && clock - o.decidedAt < ESCORT_RECEIPT_MS)
					.map((o) => ({ o, eventId: lane.eventId })),
			)
			.sort((a, b) => (b.o.decidedAt ?? 0) - (a.o.decidedAt ?? 0))
			.slice(0, 3),
	);

	function expiresText(p: EscortProposal): string | null {
		if (p.expiresAt == null) return null;
		const left = Math.max(0, Math.round((p.expiresAt - clock) / 1000));
		if (left <= 0) return tOps(lang, 'escExpiring');
		return tOps(lang, 'escExpiresIn', `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`);
	}

	/** The escort's corridor event name (its label part when it is long — the full name on hover). */
	function escortName(p: { eventId: string | null; movementId: string }): string {
		const ev = ops.eventById(p.eventId);
		return ev ? ops.text(ev.name) : p.movementId.slice(0, 8);
	}

	function escortShortName(p: { eventId: string | null; movementId: string }): string {
		const name = escortName(p);
		const head = name.split(' · ')[0];
		return head.length > 44 ? `${head.slice(0, 43)}…` : head;
	}

	async function onEscort(p: EscortProposal, action: string) {
		await ops.decideEscortProposal(p, action === 'approve' ? 'approve' : 'reject');
	}

	// ── 108 corridor-request cards (ambulance dispatches awaiting a human) ──
	const corridorRequests = $derived(
		ops.openDispatches
			.filter((d) => d.agency === 'ambulance' && d.status === 'requested' && !(ops.hideEmulated && emulatedDispatch(d)))
			.sort((a, b) => Number(b.requestedBy === '108-driver') - Number(a.requestedBy === '108-driver') || Date.parse(a.createdAt ?? '') - Date.parse(b.createdAt ?? '')),
	);

	function eventOf(d: DispatchRecord) {
		return ops.eventById(d.eventId);
	}

	async function onCorridor(dispatchId: string, action: 'approve' | 'defer') {
		await ops.decideDispatchAction(dispatchId, action);
	}

	// ── pursuit openings ────────────────────────────────────────────────────
	const pursuits = $derived(
		ops.pursuitCandidates.filter((p) => !(ops.hideEmulated && p.vehicle.lastSighting?.source === 'emulated_anpr')),
	);

	const empty = $derived(
		escortProposals.length === 0 &&
			pending.length === 0 &&
			stolenGroups.length === 0 &&
			corridorRequests.length === 0 &&
			pursuits.length === 0,
	);
	/**
	 * Unit requests other than 108 corridors wait on the dispatch tiles — the
	 * PENDING HUMAN DECISIONS counter includes them, so an empty queue says
	 * where they are instead of reading as 'nothing pending'.
	 */
	const tileRequests = $derived(ops.openDispatches.filter((d) => d.status === 'requested' && d.agency !== 'ambulance').length);
</script>

<div class="queue" data-testid="decision-queue">
	{#if anyEmulated || ops.hideEmulated}
		<div class="filters">
			<button
				type="button"
				class="filter-chip"
				class:on={ops.hideEmulated}
				aria-pressed={ops.hideEmulated}
				data-testid="queue-emulated-filter"
				onclick={() => (ops.hideEmulated = !ops.hideEmulated)}
				title={tOps(lang, 'filterEmulatedHint')}
			>
				{ops.hideEmulated ? tOps(lang, 'filterEmulatedShow') : tOps(lang, 'filterEmulatedHide')}
			</button>
			{#if hiddenEmulated}<span class="filter-note mono" data-testid="queue-emulated-hidden">{tOps(lang, 'filterEmulatedHidden', hiddenEmulated)}</span>{/if}
		</div>
	{/if}
	{#if empty}
		<EmptyState
			title={ops.recommendationsNote ?? tOps(lang, 'queueEmpty')}
			hint={tileRequests ? tOps(lang, 'queueEmptyTileRequests', tileRequests) : undefined}
		/>
	{/if}

	{#each escortProposals as p (p.approvalId)}
		{@const answer = ops.escortDecisions[p.approvalId]}
		{@const lane = ops.escorts.find((l) => l.movementId === p.movementId)}
		<div class="card-wrap" data-testid="queue-card" data-kind="escort-proposal" data-id={p.approvalId} data-movement-id={p.movementId}>
			<DecisionCard
				kind="dispatch"
				badgeLabel={ops.escortProposerTag(p, true)}
				title={ops.escortProposalText(p)}
				deadline={expiresText(p)}
				deadlineTitle={tOps(lang, 'escAuthorityNote')}
				namedActor={ops.officerOn ? ops.dutyOfficer : null}
				busy={ops.escortDecisionBusy === p.approvalId}
				onaction={(id) => onEscort(p, id)}
				lang={lang}
				actions={[
					{ id: 'approve', label: tOps(lang, 'escApprove'), kind: 'commit' },
					{ id: 'reject', label: tOps(lang, 'escReject'), kind: 'reject' }
				]}
			>
				{#snippet meta()}
					<span class="meta-chip mono" title={escortName(p)}>{tOps(lang, 'escQueueEvent', escortShortName(p))}</span>
					<span class="meta-chip mono" data-testid="escort-proposal-decider">{ops.escortDeciderText(p)}</span>
					{#if p.simulated}<Badge variant="emulated" label={tOps(lang, 'labelSimulatedFeed')} />{/if}
					{#if lane?.gpsMode === 'emulated'}<Badge variant="emulated" label={tOps(lang, 'hudEmulatedGps')} />{/if}
				{/snippet}
				{#if ops.escortProposalWhy(p)}<p class="escort-why">{ops.escortProposalWhy(p)}</p>{/if}
				{#if answer && !answer.ok}
					<p class="refusal" data-testid="escort-proposal-refused">{answer.detail ?? `HTTP ${answer.httpStatus}`}</p>
				{/if}
			</DecisionCard>
		</div>
	{/each}

	{#each escortReceipts as { o, eventId } (`${o.approvalId}:${o.status}`)}
		{@const mine = ops.escortDecisions[o.approvalId]}
		<p
			class="escort-receipt mono"
			class:approved={o.status === 'approved' && o.applied !== false}
			data-testid="escort-outcome"
			data-approval-id={o.approvalId}
			data-status={o.status}
			data-refused={mine && !mine.ok ? '1' : undefined}
			title={mine && !mine.ok ? (mine.detail ?? undefined) : (o.summary ?? undefined)}
		>
			<span class="er-what" title={escortName({ eventId, movementId: o.movementId })}>{tOps(lang, 'escQueueEvent', escortShortName({ eventId, movementId: o.movementId }))}</span>
			{ops.escortOutcomeText(o)}
		</p>
	{/each}

	{#each corridorRequests as d (d.dispatchId)}
		{@const ev = eventOf(d)}
		<div class="card-wrap" data-testid="queue-card" data-kind="corridor-request" data-id={d.dispatchId}>
			<DecisionCard
				kind="corridor-request"
				title={`${d.agencyLabel ?? d.agency} · ${d.unitId ?? 'unit'}${ev ? ` · ${ops.text(ev.name)}` : ''}`}
				deadline={`${tOps(lang, 'queueGoldenHour')} ${fmtGoldenHour(d.createdAt, clock)}`}
				namedActor={ops.officerOn ? ops.dutyOfficer : null}
				busy={ops.dispatchBusy === d.dispatchId}
				onaction={(id) => onCorridor(d.dispatchId, id as 'approve' | 'defer')}
				lang={lang}
				actions={[
					{ id: 'approve', label: tOps(lang, 'dispatchApprove'), kind: 'commit' },
					{ id: 'defer', label: tOps(lang, 'dispatchDefer'), kind: 'defer' }
				]}
			>
				{#snippet meta()}
					{#if d.requestedBy === '108-driver'}<Badge variant="act" label={tOps(lang, 'dispatch108')} />{/if}
					{#if d.requestedBy}<span class="meta-chip mono">{tOps(lang, 'dispatchRequestedBy', d.requestedBy)}</span>{/if}
					{#if d.waypointJunction != null}
						{@const wpLabel = `${tOps(lang, 'dispatchAtWaypoint', d.waypointJunction)}${ops.junctionName(d.waypointJunction) ? ` ${ops.junctionName(d.waypointJunction)}` : ''}`}
						<span class="meta-chip mono" title={wpLabel}>{wpLabel}</span>
					{/if}
					{#if ev?.emulated}<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />{/if}
				{/snippet}
			</DecisionCard>
		</div>
	{/each}

	{#each stolenGroups as g (g.vehicleId)}
		{@const key = `stolen:${g.vehicleId}`}
		{@const fir = firOf(g.recs, g.vehicleId)}
		{@const refusal = ops.recommendationRefusals[g.newest.recommendationId]}
		<div
			class="card-wrap"
			data-testid="queue-card"
			data-kind="stolen-sighting"
			data-id={g.vehicleId}
			data-count={g.recs.length}
			data-rec-ids={g.recs.map((r) => r.recommendationId).join(' ')}
		>
			<DecisionCard
				kind="stolen-sighting"
				badgeLabel={tOps(lang, 'queueStolenBadge')}
				title={`${ops.plate(g.newest.plate) ?? g.vehicleId.slice(0, 8)} · ${tOps(lang, 'queueStolenTitle', fir ?? '')}`}
				deadline={retentionOf(g.vehicleId)}
				deadlineTitle={tOps(lang, 'queueEvidencePurgeHint')}
				namedActor={ops.officerOn ? ops.dutyOfficer : null}
				busy={g.recs.some((r) => ops.recommendationBusy === r.recommendationId)}
				blocked={refusal ? tOps(lang, 'queueRefusedVerbatim', refusal.status, refusal.detail) : null}
				onaction={(id) => onStolen(g, id)}
				lang={lang}
				actions={g.approved || refusal
					? [{ id: 'dismiss', label: tOps(lang, 'queueDismiss'), kind: 'reject' }]
					: [
							{ id: 'authorise', label: tOps(lang, 'queueAuthorisePursuit'), kind: 'commit' },
							{ id: 'dismiss', label: tOps(lang, 'queueDismiss'), kind: 'reject' }
						]}
			>
				{#snippet meta()}
					<Badge variant="act" label={tOps(lang, 'queueStolenNoFine')} title={tOps(lang, 'queueStolenNoFineHint')} />
					<span class="meta-chip mono">{tOps(lang, 'queueStolenSightings', g.recs.length)}</span>
					{#if g.newest.createdAt}<span class="meta-chip mono">{tOps(lang, 'queueFiled', istClock(g.newest.createdAt))}</span>{/if}
					{#if g.recs.some(emulatedRec)}<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} title="evidence.source: emulated_anpr" />{/if}
				{/snippet}
				{#if g.approved}
					<p class="authorised" data-testid="stolen-authorised">{tOps(lang, 'queueStolenAuthorised', g.approved.decidedBy ?? '—')}</p>
				{/if}
				{#if refusal}
					<p class="refusal" data-testid="decision-refusal">{refusal.detail}</p>
				{/if}
				{#snippet evidence()}
					{#if rejectOpen === key}
						<div class="reject-row">
							<input
								class="note-input"
								type="text"
								placeholder={tOps(lang, 'queueDismissReason')}
								bind:value={notes[key]}
								maxlength="500"
								data-testid="stolen-dismiss-reason"
							/>
							<button
								class="ev-toggle danger"
								type="button"
								disabled={!ops.officerOn || !(notes[key] ?? '').trim() || ops.recommendationBusy !== null}
								onclick={() => onStolen(g, 'dismiss')}
							>
								{tOps(lang, 'queueDismiss')}
							</button>
						</div>
					{/if}
				{/snippet}
			</DecisionCard>
		</div>
	{/each}

	{#each pending as rec (rec.recommendationId)}
		{@const stolen = ops.stolenVehicle(rec.vehicleId)}
		{@const refusal = ops.recommendationRefusals[rec.recommendationId]}
		<div class="card-wrap" data-testid="queue-card" data-kind="echallan" data-id={rec.recommendationId} data-stolen={stolen ? '1' : ''} data-refused={refusal ? '1' : ''}>
			<DecisionCard
				kind="echallan"
				badgeLabel={tOps(lang, 'queueEchallanBadge')}
				title={`${ops.plate(rec.plate) ?? rec.recommendationId.slice(0, 8)} · ${ops.text(rec.recommendation) || tOps(lang, 'queueEchallan')}`}
				deadline={retentionOf(rec.vehicleId)}
				deadlineTitle={tOps(lang, 'queueEvidencePurgeHint')}
				namedActor={ops.officerOn ? ops.dutyOfficer : null}
				busy={ops.recommendationBusy === rec.recommendationId}
				onaction={(id) => onEchallan(rec.recommendationId, id as 'approve' | 'reject')}
				lang={lang}
				actions={stolen || refusal
					? [{ id: 'reject', label: tOps(lang, 'queueReject'), kind: 'reject' }]
					: [
							{ id: 'approve', label: tOps(lang, 'queueApprove'), kind: 'commit' },
							{ id: 'reject', label: tOps(lang, 'queueReject'), kind: 'reject' }
						]}
			>
				{#snippet meta()}
					{#if stolen}
						<span data-testid="stolen-warning"><Badge variant="act" label={tOps(lang, 'queueStolenChip')} title={tOps(lang, 'queueStolenOwnerVictim')} /></span>
					{/if}
					{#if rec.ruleId}<span class="meta-chip mono">{tOps(lang, 'queueRule')} {rec.ruleId}</span>{/if}
					{#if rec.createdAt}<span class="meta-chip mono">{tOps(lang, 'queueFiled', istClock(rec.createdAt))}</span>{/if}
					{#if emulatedRec(rec)}<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} title="evidence.source: emulated_anpr" />{/if}
				{/snippet}
				{#if stolen}
					<p class="refusal" data-testid="stolen-note">{tOps(lang, 'queueStolenOwnerVictim')}</p>
				{/if}
				{#if refusal}
					<p class="refusal" data-testid="decision-refusal">{refusal.detail}</p>
				{/if}
				{#snippet evidence()}
					<button class="ev-toggle" type="button" onclick={() => (openEvidence = openEvidence === rec.recommendationId ? null : rec.recommendationId)}>
						{openEvidence === rec.recommendationId ? tOps(lang, 'queueHideEvidence') : tOps(lang, 'queueEvidence')}
					</button>
					{#if rejectOpen === rec.recommendationId}
						<div class="reject-row">
							<input
								class="note-input"
								type="text"
								data-testid="reject-reason"
								placeholder={tOps(lang, stolen ? 'queueStolenRejectPlaceholder' : 'queueReasonPlaceholder')}
								bind:value={notes[rec.recommendationId]}
								maxlength="500"
							/>
							<button
								class="ev-toggle danger"
								type="button"
								disabled={!ops.officerOn || !(notes[rec.recommendationId] ?? '').trim() || ops.recommendationBusy !== null}
								onclick={() => onEchallan(rec.recommendationId, 'reject')}
							>
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
						</dl>
					{/if}
				{/snippet}
			</DecisionCard>
		</div>
	{/each}

	{#each pursuits as p (p.vehicle.vehicleId)}
		{@const w = p.vehicle}
		<div class="card-wrap" data-testid="queue-card" data-kind="pursuit" data-id={w.vehicleId} data-refusal={p.refusal ?? ''}>
			<DecisionCard
				kind="pursuit"
				title={`${ops.plate(w.plate) ?? w.vehicleId.slice(0, 8)} · ${ops.text(w.earmarkReason) || tOps(lang, 'watchState')}`}
				deadline={w.lastSighting ? `${tOps(lang, 'watchLastSeen')} ${fmtAgeSince(w.lastSighting.t, clock)}` : null}
				namedActor={ops.officerOn ? ops.dutyOfficer : null}
				busy={ops.pursuitBusy === w.vehicleId}
				blocked={p.refusal === 'no-anchor' ? tOps(lang, 'queuePursuitBlockedAnchor') : p.refusal ? tOps(lang, 'queuePursuitBlockedApproval') : null}
				onaction={() => void ops.openPursuitFor(w.vehicleId)}
				lang={lang}
				actions={[{ id: 'pursuit', label: tOps(lang, 'queueOpenPursuit'), kind: 'commit' }]}
			>
				{#snippet meta()}
					<!-- a short badge; the full rule is a wrapping line in the card body -->
					<Badge variant="act" label={tOps(lang, 'queuePursuitHumanConfirms')} title={tOps(lang, 'queuePursuitConfirm')} />
					{#if p.anchor}
						{@const anchorLabel = tOps(lang, 'queuePursuitAnchor', p.anchor.segmentId, p.anchor.roadName ?? '', p.anchor.distanceM)}
						<span class="meta-chip mono" title={anchorLabel}>{anchorLabel}</span>
					{/if}
					{#if p.recommendationId}
						<span class="meta-chip mono">{tOps(lang, 'queuePursuitFrom', p.recommendationId.slice(0, 8))}</span>
					{/if}
					{#if w.lastSighting?.source === 'emulated_anpr'}<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />{/if}
				{/snippet}
				<p class="confirm-note" data-testid="pursuit-confirm-note">{tOps(lang, 'queuePursuitConfirm')}</p>
				{#if p.refusal}
					<p class="refusal" data-testid="pursuit-refusal">
						{p.refusal === 'no-anchor' ? tOps(lang, 'queuePursuitNoAnchor') : tOps(lang, 'queuePursuitNoApproval')}
					</p>
				{:else if ops.pursuitNote?.vehicleId === w.vehicleId}
					<p class="refusal">{ops.text(ops.pursuitNote.text)}</p>
				{/if}
			</DecisionCard>
		</div>
	{/each}
</div>

<style>
	.queue {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
	}

	.card-wrap {
		display: contents;
	}

	.meta-chip {
		/* inline-block: overflow / ellipsis apply (an inline span ignores them
		 * and its text ran out of the card, cut mid-word) */
		display: inline-block;
		vertical-align: middle;
		box-sizing: border-box;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
		white-space: nowrap;
		/* a long junction / road name ellipsizes inside the card (full text on hover) */
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* the pursuit card's rule, said in full: wraps, never clipped like a badge */
	.confirm-note {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-act-text);
		overflow-wrap: anywhere;
	}

	.refusal {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		border-top: 2px solid var(--v-band-red);
		padding-top: var(--v-space-1);
	}

	.escort-why {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	/* how a closed escort proposal ended — a receipt, not a card */
	.escort-receipt {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: var(--v-space-1) var(--v-space-2);
		padding: var(--v-space-1) var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border: 1px dashed var(--v-line-2);
		border-left: 2px solid var(--v-act);
		border-radius: var(--v-radius-chip);
	}

	.escort-receipt.approved {
		color: var(--v-band-green);
		border-left-color: var(--v-band-green);
	}

	.er-what {
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
	}

	.authorised {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-band-green);
	}

	/* the EMULATED filter: one chip, the hidden count said beside it */
	.filters {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
	}

	.filter-chip {
		font: inherit;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
		background: transparent;
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-pill);
		padding: 2px var(--v-space-2);
		cursor: pointer;
	}

	.filter-chip.on {
		color: var(--v-ink-1);
		border-style: solid;
		border-color: var(--v-line-select);
	}

	.filter-note {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
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

	.ev-toggle:hover:not(:disabled) {
		border-color: var(--v-line-select);
		color: var(--v-ink-1);
	}

	.ev-toggle:disabled {
		opacity: 0.45;
		cursor: not-allowed;
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
		color: var(--v-ink-3);
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
