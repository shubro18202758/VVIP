<script lang="ts">
	/**
	 * ConvoyHud — the VVIP corridor module's movement HUD, honest end to end.
	 *
	 * Shown once a plan exists (PLANNED) and through the escort (ESCORT LIVE).
	 * Every readout comes from the stack, never a timer:
	 *
	 *   speed     the escort's GPS speed (GET /api/convoy/movements/{id}/escort,
	 *             contract C-J) or the convoy entity's (the C-B GPS mirror in
	 *             the city registry); before any fix, the corridor AVERAGE —
	 *             labelled CORRIDOR AVG, greyed when the summary has no live
	 *             feed, and SIMULATED FEED when it stands on synthetic rows
	 *   progress  the escort's progress along the primary route (C-J / C-B);
	 *             '—' + AWAITING GPS until a fix exists
	 *   ETA       the escort's eta_sec; before that the plan's estimated
	 *             transit time (C-I), labelled 'planned'
	 *   elapsed   wall time since the escort started (server started_at when
	 *             the brain reports it), frozen when the escort ends
	 *   lifecycle ESCORT LIVE while it runs; COMPLETED / STOPPED / FAILED once
	 *             the brain says so (the badge stops breathing, the timer
	 *             stops) — never a LIVE badge over an escort that ended
	 *
	 * The status comes from the shared escort follower (escort.svelte.ts —
	 * the planner rail reads the same one). A brain that predates the escort
	 * status route answers 405/404 — the HUD then reads the convoy entity
	 * alone while the follower backs off.
	 *
	 *   hold      'HOLDING on <roads> (<ordered by>)' while the escort holds
	 *   proposal  ESCORT AUTHORITY — officer approves every switch: the
	 *             escort's ONE open proposal (GET /api/convoy/escorts, the
	 *             store's lanes — or, while those are not answering, the
	 *             escort's own status document, C-J pending_approval),
	 *             first under the header so the AI TRACE
	 *             console docked below never covers its buttons —
	 *             'AI PROPOSES: <route / hold / closures,
	 *             with the km> · decider <tier> <confidence> · expires in
	 *             mm:ss' ('RULE PROPOSES' when no model answered and the
	 *             deterministic rule stood in) with APPROVE / REJECT, disabled until a duty
	 *             officer is named, one POST per click (the store's busy
	 *             guard, shared with the planner rail and the /ops queue).
	 *             Its route is drawn dashed on the canvas. Unanswered, it
	 *             expires and the convoy holds its current approved plan;
	 *             the outcome (approved by <officer> / rejected / expired —
	 *             current plan held) stays on the HUD and in the feed. The
	 *             AI never approves anything itself.
	 *   approvals a brain that predates escort authority (no /escorts):
	 *             its referred actions with APPROVE / REJECT, as before.
	 *   provenance SIMULATED FEED when the congestion the escort reads (or an
	 *             approval's numbers) come from a synthetic feed; EMULATED
	 *             GPS when the position is the brain's route walker.
	 */
	import { approveEscort, type EscortApprovalOutcome } from '$lib/ops/client';
	import { tOps } from '$lib/i18n/strings.ops';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { cityRegistry } from '$lib/gods-eye/entities/registry.svelte';
	import { corridor } from './corridor.svelte';
	import { escortFollower, type EscortTerminal } from './escort.svelte';

	type EscortHudState = {
		movementId: string | null;
		eventId: string | null;
		startedAt: number | null;
		plannedEtaSec: number | null;
		plannedDistanceM: number | null;
		phase: 'planned' | 'starting' | 'escorting' | 'stopping' | 'ended';
		/** The brain's final status once the escort ended (phase 'ended'). */
		endedStatus: EscortTerminal | null;
		vvipClass: string;
		destination: string;
	};

	let { escort }: { escort: EscortHudState } = $props();

	let now = $state(Date.now());

	// 1 Hz clock (the elapsed timer; the status itself is the follower's).
	$effect(() => {
		const clock = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(clock);
	});

	/** The follower's answer — only for THIS movement. */
	const followed = $derived(!!escort.movementId && escortFollower.movementId === escort.movementId);
	const status = $derived(followed ? escortFollower.status : null);
	const statusNote = $derived(followed ? escortFollower.note : null);

	/** The C-B GPS mirror for this movement, from the fused registry. */
	const convoy = $derived(escort.movementId ? cityRegistry.get(`convoy:${escort.movementId}`) : null);

	function num(v: unknown): number | null {
		return typeof v === 'number' && Number.isFinite(v) ? v : null;
	}

	const fixSpeed = $derived(status?.position?.speedKmh ?? num(convoy?.props.speed_kmh));
	const hasFix = $derived(!!status?.position || !!convoy);
	const gpsSource = $derived(
		status?.position?.source ?? (typeof convoy?.props.position_source === 'string' ? (convoy.props.position_source as string) : null),
	);
	const progress = $derived(status?.progress ?? num(convoy?.props.progress));
	const etaSec = $derived(status?.etaSec ?? null);
	/** No ETA once the escort ended — the last fix's remaining time is not a forecast any more. */
	const shownEtaSec = $derived(escort.phase === 'ended' ? null : (etaSec ?? escort.plannedEtaSec));
	const etaPlanned = $derived(etaSec == null && escort.phase !== 'ended');

	/** Speed on the gauge: the convoy's own, else the corridor average (labelled). */
	const gaugeSource = $derived(fixSpeed != null ? 'convoy' : 'corridor');
	const corridorDark = $derived(gaugeSource === 'corridor' && (corridor.noLiveFeed || !corridor.row));
	const speed = $derived(fixSpeed ?? (corridorDark ? 0 : (corridor.row?.avgSpeedKmh ?? 0)));

	const startedAt = $derived(status?.startedAt ?? escort.startedAt);
	const ended = $derived(escort.phase === 'ended');
	/** The timer stops where the escort ended (the brain's update time, else when the end was seen). */
	const until = $derived(ended ? ((followed ? escortFollower.endedAt : null) ?? now) : now);
	const elapsed = $derived(startedAt ? Math.max(0, Math.floor((until - startedAt) / 1000)) : 0);
	const elapsedLabel = $derived(
		`${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`,
	);

	/** Gauge geometry: 240° arc from 150° to -30° (SVG path coords). */
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

	function etaText(sec: number | null): string {
		if (sec == null || !Number.isFinite(sec) || sec <= 0) return '—';
		return sec < 90 ? `${Math.round(sec)}s` : `${Math.round(sec / 60)} ${t(ops.lang, 'tpl_min')}`;
	}

	const live = $derived(escort.phase === 'escorting' || escort.phase === 'stopping');

	// ── hold, proposal, approvals, provenance (C-J status doc) ──────────────
	const hold = $derived(status?.hold ?? null);
	/**
	 * The escort's ONE open proposal: the store's lanes, else this escort's
	 * own status document (the follower's — C-J pending_approval) when the
	 * lanes are not answering; null on an older brain.
	 */
	const proposal = $derived(live ? ops.escortProposalFor(escort.movementId, status) : null);
	/** How the newest closed proposal ended (shown for 5 minutes). */
	const outcome = $derived.by(() => {
		const o = ops.escortOutcomeFor(escort.movementId, status);
		return o && (o.decidedAt == null || now - o.decidedAt < 300_000) ? o : null;
	});
	const proposalRemainingSec = $derived(
		proposal?.expiresAt != null ? Math.max(0, Math.round((proposal.expiresAt - now) / 1000)) : null,
	);
	const proposalCountdown = $derived(
		proposalRemainingSec == null
			? null
			: proposalRemainingSec <= 0
				? tOps(ops.lang, 'escExpiring')
				: tOps(
						ops.lang,
						'escExpiresIn',
						`${String(Math.floor(proposalRemainingSec / 60)).padStart(2, '0')}:${String(proposalRemainingSec % 60).padStart(2, '0')}`,
					),
	);
	const proposalAnswer = $derived(proposal ? (ops.escortDecisions[proposal.approvalId] ?? null) : null);
	/** The launching officer when this console names none (the brain's own record). */
	const laneOfficer = $derived(ops.escorts.find((l) => l.movementId === escort.movementId)?.officer ?? null);
	// Legacy referred actions only on a brain without escort authority (the
	// lanes or the escort's own document carry the proposal otherwise —
	// never shown twice).
	const approvals = $derived(
		live && ops.escortsDeployed !== true && status?.proposalsReported !== true ? (status?.pendingApprovals ?? []) : [],
	);
	const feedSimulated = $derived(
		status?.trafficSimulated === true || approvals.some((a) => a.simulated) || proposal?.simulated === true,
	);
	const gpsEmulated = $derived(status?.gpsMode === 'emulated' || gpsSource === 'emulated_gps');

	/** Road names of segment ids (the junction-free signage name, else the id). */
	function roads(ids: number[]): string {
		const names: string[] = [];
		for (const id of ids) {
			const n = ops.roadName(id) ?? `seg ${id}`;
			if (!names.includes(n)) names.push(n);
		}
		return names.length > 3 ? `${names.slice(0, 3).join(', ')} +${names.length - 3}` : names.join(', ') || '—';
	}

	/** The approval request in flight ('<approvalId>:<decision>') — one at a time. */
	let deciding = $state<string | null>(null);
	let outcomes = $state<Record<string, EscortApprovalOutcome>>({});

	/** The officer answers the open proposal (the store guards one POST at a time). */
	async function decideProposal(decision: 'approve' | 'reject') {
		if (!proposal || ops.escortDecisionBusy) return;
		await ops.decideEscortProposal(proposal, decision);
		escortFollower.refresh();
	}

	async function decide(approvalId: string, decision: 'approve' | 'reject') {
		if (!escort.movementId || deciding) return;
		deciding = `${approvalId}:${decision}`;
		try {
			const res = await approveEscort(escort.movementId, { approval_id: approvalId, decision, officer: ops.dutyOfficer });
			outcomes = { ...outcomes, [approvalId]: res };
			escortFollower.refresh();
		} catch (err) {
			outcomes = {
				...outcomes,
				[approvalId]: { ok: false, httpStatus: 0, status: null, action: null, detail: err instanceof Error ? err.message : String(err) },
			};
		} finally {
			deciding = null;
		}
	}

	const ENDED_KEY: Record<EscortTerminal, string> = {
		completed: 'tpl_escort_completed',
		stopped: 'tpl_escort_stopped',
		failed: 'tpl_escort_failed',
	};
	const badge = $derived(
		live
			? t(ops.lang, 'tpl_escort_live')
			: ended && escort.endedStatus
				? t(ops.lang, ENDED_KEY[escort.endedStatus])
				: `${t(ops.lang, 'tpl_planned_eta').toUpperCase()} · ${escort.vvipClass}`,
	);
</script>

{#if escort.movementId}
	<aside
		class="hud"
		class:planned={!live && !ended}
		data-template="convoy-hud"
		data-phase={escort.phase}
		data-ended={escort.endedStatus ?? ''}
		data-eta-sec={shownEtaSec ?? ''}
		aria-label={badge}
	>
		<header class="hud-head">
			<span
				class="live-badge"
				class:idle={!live && !ended}
				class:ended
				class:failed={escort.endedStatus === 'failed'}
				data-testid="convoy-hud-badge"
			>
				{badge}
			</span>
			{#if live || ended}<span class="elapsed mono" class:frozen={ended}>{elapsedLabel}</span>{/if}
		</header>

		<!-- the AI's open proposal first: an officer's decision outranks the instruments -->
		{#if proposal}
			{@const p = proposal}
			<section
				class="proposal"
				data-testid="convoy-hud-proposal"
				data-approval-id={p.approvalId}
				data-kind={p.kind}
				data-expires-sec={proposalRemainingSec ?? ''}
				aria-label={ops.escortProposerTag(p)}
				aria-live="polite"
			>
				<header class="pr-head">
					<span class="pr-tag" data-testid="convoy-hud-proposal-tag">{ops.escortProposerTag(p)}</span>
					{#if proposalCountdown}
						<span class="pr-count mono" class:urgent={(proposalRemainingSec ?? 99) < 20} data-testid="convoy-hud-proposal-countdown">{proposalCountdown}</span>
					{/if}
				</header>
				<p class="pr-what" title={p.summary ?? undefined} data-testid="convoy-hud-proposal-text">{ops.escortProposalText(p)}</p>
				<p class="pr-meta mono">
					<span data-testid="convoy-hud-proposal-decider">{ops.escortDeciderText(p)}</span>
					{#if p.simulated}<Badge variant="emulated" label={tOps(ops.lang, 'hudSimulatedFeed')} />{/if}
				</p>
				{#if ops.escortProposalWhy(p)}<p class="ap-why" data-testid="convoy-hud-proposal-why">{ops.escortProposalWhy(p)}</p>{/if}
				{#if proposalAnswer && !proposalAnswer.ok}
					<p class="ap-out mono err" data-testid="convoy-hud-proposal-refused">{tOps(ops.lang, 'hudApprovalFailed', proposalAnswer.detail ?? `HTTP ${proposalAnswer.httpStatus}`)}</p>
				{/if}
				<div class="ap-actions">
					<Button
						variant="primary"
						size="sm"
						actor={ops.officerOn ? ops.dutyOfficer : ''}
						busy={ops.escortDecisionBusy === p.approvalId}
						disabled={!!ops.escortDecisionBusy || !ops.officerOn}
						onclick={() => void decideProposal('approve')}
					>
						<span data-testid="convoy-hud-proposal-approve">{tOps(ops.lang, 'escApprove')}</span>
					</Button>
					<Button
						variant="danger-outline"
						size="sm"
						busy={false}
						disabled={!!ops.escortDecisionBusy || !ops.officerOn}
						onclick={() => void decideProposal('reject')}
					>
						<span data-testid="convoy-hud-proposal-reject">{tOps(ops.lang, 'escReject')}</span>
					</Button>
				</div>
				{#if !ops.officerOn}<p class="pr-need mono" data-testid="convoy-hud-proposal-officer">{tOps(ops.lang, 'escNeedOfficer')}</p>{/if}
				<p class="ap-note">{tOps(ops.lang, 'escAuthorityNote')}</p>
			</section>
		{:else if outcome && live}
			<p class="pr-outcome mono" class:approved={outcome.status === 'approved' && outcome.applied !== false} data-testid="convoy-hud-proposal-outcome" data-status={outcome.status} title={outcome.summary ?? undefined}>
				{ops.escortOutcomeText(outcome)}
			</p>
		{/if}

		<div class="gauge-wrap">
			<svg viewBox="0 0 120 92" class="gauge" class:dark={corridorDark} class:settled={ended} role="img" aria-label="speed gauge">
				<path class="track" d={arcPath} />
				<path class="zone z-green" d={arcPath} stroke-dasharray="24 999" stroke-dashoffset="0" />
				<path class="zone z-amber" d={arcPath} stroke-dasharray="24 999" stroke-dashoffset="-80" />
				<path class="zone z-red" d={arcPath} stroke-dasharray="32 999" stroke-dashoffset="-160" />
				{#if !corridorDark}
					<line class="needle" x1="60" y1="62" x2={needleTip[0].toFixed(1)} y2={needleTip[1].toFixed(1)} />
				{/if}
				<circle class="hub" cx="60" cy="62" r="4" />
				<text x="60" y="86" class="gauge-value">{corridorDark ? '—' : speed.toFixed(0)}</text>
			</svg>
			<div class="gauge-side">
				<span class="gauge-label">
					{gaugeSource === 'convoy' ? t(ops.lang, 'tpl_convoy_speed') : t(ops.lang, 'tpl_corridor_avg')}
				</span>
				<span class="gauge-unit mono">km/h</span>
				{#if gaugeSource === 'convoy' && gpsEmulated}
					<span class="tag mono" data-testid="convoy-hud-emulated">{tOps(ops.lang, 'hudEmulatedGps')}</span>
				{:else if gaugeSource === 'corridor' && corridorDark}
					<span class="tag mono">{t(ops.lang, 'tpl_no_live_feed')}</span>
				{:else if gaugeSource === 'corridor' && corridor.simulated}
					<span class="tag sim mono">{t(ops.lang, 'cc_simulated_feed')}</span>
				{/if}
			</div>
		</div>

		<div class="grid mono">
			<span class="cell"><b>{t(ops.lang, 'tpl_destination')}</b>{escort.destination}</span>
			<span class="cell" data-testid="convoy-hud-eta">
				<b>{t(ops.lang, 'tpl_eta')}{etaPlanned ? ` · ${t(ops.lang, 'tpl_planned_eta')}` : ''}</b>{etaText(shownEtaSec)}
			</span>
			<span class="cell"><b>{t(ops.lang, 'tpl_class')}</b>{escort.vvipClass}</span>
			<span class="cell"><b>{tOps(ops.lang, 'hudOfficer')}</b>{ops.dutyOfficer || laneOfficer || '—'}</span>
			{#if escort.plannedDistanceM != null}
				<span class="cell"><b>{t(ops.lang, 'tpl_dist')}</b>{(escort.plannedDistanceM / 1000).toFixed(1)} {t(ops.lang, 'tpl_km')}</span>
			{/if}
			{#if status}
				<span class="cell"><b>{tOps(ops.lang, 'hudStatus')}</b>{status.status.replace(/_/g, ' ')}{status.iteration != null ? ` · #${status.iteration}` : ''}</span>
			{/if}
		</div>

		{#if feedSimulated || (gpsEmulated && gaugeSource !== 'convoy')}
			<div class="prov" data-testid="convoy-hud-provenance">
				{#if feedSimulated}<span data-testid="convoy-hud-simulated"><Badge variant="emulated" label={tOps(ops.lang, 'hudSimulatedFeed')} title={tOps(ops.lang, 'hudSimulatedFeedHint')} /></span>{/if}
				{#if gpsEmulated && gaugeSource !== 'convoy'}<Badge variant="emulated" label={tOps(ops.lang, 'hudEmulatedGps')} />{/if}
			</div>
		{/if}

		{#if hold && live}
			<p class="hold mono" data-testid="convoy-hud-hold" data-ordered-by={hold.orderedBy ?? ''} title={hold.reason ?? undefined}>
				{tOps(ops.lang, 'hudHolding', roads(hold.segmentIds), hold.orderedBy ?? '—')}
				{#if hold.peakCongestion != null}<span class="dimmed"> · {tOps(ops.lang, 'hudPeak', Math.round(hold.peakCongestion * 100))}</span>{/if}
			</p>
		{/if}

		{#if approvals.length}
			<section class="approvals" data-testid="convoy-hud-approvals" aria-label={tOps(ops.lang, 'hudApprovalsTitle')}>
				<h4>{tOps(ops.lang, 'hudApprovalsTitle')}</h4>
				{#each approvals as a (a.approvalId)}
					{@const out = outcomes[a.approvalId]}
					<article class="approval" data-testid="convoy-hud-approval" data-approval-id={a.approvalId}>
						<p class="ap-what">
							<b>{a.action.replace(/_/g, ' ')}</b>
							{#if a.simulated}<Badge variant="emulated" label={tOps(ops.lang, 'hudSimulatedFeed')} />{/if}
						</p>
						{#if a.reason}<p class="ap-why">{a.reason}</p>{/if}
						{#if a.detourSegmentIds.length}
							<p class="ap-why mono">
								{tOps(ops.lang, 'hudDetour', roads(a.detourSegmentIds))}{a.detourM != null ? ` · ${(a.detourM / 1000).toFixed(1)} km` : ''}
							</p>
						{/if}
						{#if out}
							<p class="ap-out mono" class:err={!out.ok} data-testid="convoy-hud-approval-outcome">
								{out.ok ? tOps(ops.lang, 'hudApprovalDone', out.status ?? '—') : tOps(ops.lang, 'hudApprovalFailed', out.detail ?? `HTTP ${out.httpStatus}`)}
							</p>
						{/if}
						{#if !out?.ok}
							<div class="ap-actions">
								<Button
									variant="primary"
									size="sm"
									actor={ops.officerOn ? ops.dutyOfficer : ''}
									busy={deciding === `${a.approvalId}:approve`}
									disabled={!!deciding}
									onclick={() => void decide(a.approvalId, 'approve')}
								>
									<span data-testid="convoy-hud-approve">{tOps(ops.lang, 'hudApprove')}</span>
								</Button>
								<Button
									variant="danger-outline"
									size="sm"
									busy={deciding === `${a.approvalId}:reject`}
									disabled={!!deciding || !ops.officerOn}
									onclick={() => void decide(a.approvalId, 'reject')}
								>
									<span data-testid="convoy-hud-reject">{tOps(ops.lang, 'hudReject')}</span>
								</Button>
							</div>
						{/if}
					</article>
				{/each}
				<p class="ap-note">{tOps(ops.lang, 'hudApprovalsNote')}</p>
			</section>
		{/if}

		{#if live || ended}
			<div class="progress" title={progress != null ? `${Math.round(progress * 100)}% of the primary route` : t(ops.lang, 'tpl_awaiting_gps')}>
				{#if progress != null}
					<i style={`width: ${Math.round(progress * 100)}%`}></i>
				{/if}
			</div>
			<p class="prog-note mono">
				{t(ops.lang, 'tpl_progress')}: {progress != null ? `${Math.round(progress * 100)}%` : '—'}
				{#if !hasFix && live}· {t(ops.lang, 'tpl_awaiting_gps')}{/if}
				{#if status?.activeDiversions}· {tOps(ops.lang, 'hudDiversions', status.activeDiversions)}{/if}
			</p>
			{#if statusNote}<p class="prog-note mono dim">{statusNote}</p>{/if}
		{/if}

		<p class="mid mono">mvmt {escort.movementId}</p>
	</aside>
{/if}

<style>
	.hud {
		/* the host (cockpit) places floating instruments clear of its rails */
		position: absolute;
		top: var(--float-top, var(--v-space-4));
		right: var(--float-right, var(--v-space-3));
		width: 268px;
		max-height: calc(100% - var(--v-space-6));
		overflow-y: auto;
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

	.hud.planned {
		border-style: dashed;
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

	.live-badge.idle {
		color: var(--v-data);
		animation: none;
	}

	/* the escort ended: a settled readout, not a breathing LIVE badge */
	.live-badge.ended {
		color: var(--v-ink-2);
		animation: none;
	}

	.live-badge.ended.failed {
		color: var(--v-band-red);
	}

	.elapsed.frozen {
		color: var(--v-ink-3);
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

	.gauge.dark {
		opacity: 0.4;
		filter: grayscale(1);
	}

	/* the escort ended: the last fix stays readable, no longer live */
	.gauge.settled {
		opacity: 0.55;
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
		color: var(--v-ink-3);
	}

	.tag {
		font-size: var(--v-text-2xs);
		letter-spacing: 0.06em;
		color: var(--v-ink-3);
		border: 1px dashed var(--v-ink-3);
		border-radius: var(--v-radius-pill);
		padding: 0 4px;
		white-space: nowrap;
	}

	.tag.sim {
		color: var(--v-tool);
		border-color: var(--v-tool);
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
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
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

	.prog-note {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.prog-note.dim {
		color: var(--v-ink-3);
	}

	.mid {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: 0.06em;
	}

	.prov {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	/* the escort is held: the operator reads who ordered it and where */
	.hold {
		margin: 0;
		padding: var(--v-space-1) var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-band-amber);
		border: 1px solid color-mix(in srgb, var(--v-band-amber) 45%, transparent);
		border-radius: var(--v-radius-chip);
		background: color-mix(in srgb, var(--v-band-amber) 8%, transparent);
	}

	.hold .dimmed {
		color: var(--v-ink-2);
	}

	/* the AI's proposal: the act colour, the officer decides (never filled) */
	.proposal {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		padding: var(--v-space-2);
		border: 1px solid var(--v-act);
		border-left-width: 3px;
		border-radius: var(--v-radius-chip);
		background: color-mix(in srgb, var(--v-act) 7%, transparent);
	}

	.pr-head {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		justify-content: space-between;
		gap: 0 var(--v-space-2);
	}

	.pr-tag {
		font-family: var(--v-font-display);
		font-size: var(--v-text-2xs);
		font-weight: 800;
		letter-spacing: 0.14em;
		color: var(--v-act-text);
		white-space: nowrap;
	}

	.pr-count {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.pr-count.urgent {
		color: var(--v-band-amber);
	}

	.pr-what {
		margin: 0;
		font-size: var(--v-text-xs);
		line-height: 1.35;
		color: var(--v-ink-1);
	}

	.pr-meta {
		margin: 0;
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.pr-need {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
	}

	.pr-outcome {
		margin: 0;
		padding: var(--v-space-1) var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
	}

	.pr-outcome.approved {
		color: var(--v-band-green);
		border-color: color-mix(in srgb, var(--v-band-green) 45%, transparent);
	}

	/* an action referred to the duty officer — the act edge, never filled */
	.approvals {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-space-2);
		border: 1px solid var(--v-act-text);
		border-radius: var(--v-radius-chip);
	}

	.approvals h4 {
		margin: 0;
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-act-text);
	}

	.approval {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.ap-what {
		margin: 0;
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		text-transform: capitalize;
	}

	.ap-why {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.ap-out {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-green);
	}

	.ap-out.err {
		color: var(--v-band-red);
	}

	.ap-actions {
		display: flex;
		gap: var(--v-space-1);
	}

	.ap-note {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
