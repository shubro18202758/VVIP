<script lang="ts">
	/**
	 * PlannerRail — the VVIP corridor module's planning rail: the REAL Blue
	 * Book lifecycle on the live two-tier brain.
	 *
	 *   pick origin/destination (canonical Ahmedabad POIs, the template's
	 *   own list) + protection class → PLAN runs createMovement +
	 *   planMovement through convoy-brain (the audited pre-movement
	 *   workflow) → the plan document (contract C-I) renders:
	 *     · its status — approved / CONDITIONAL (protocol violations, waiver
	 *       first) / HOLD (the decider delays departure) / NO ROUTE — with
	 *       the overall reasoning, never a canned 'no candidates' line;
	 *     · the candidates, each with ETA (estimated_time_sec), distance,
	 *       route tier and its lane-floor violations; the RECOMMENDED ribbon
	 *       sits on the decider's pick when the decider's pick was applied
	 *       (else on the tactical primary, labelled so), and the 'decider
	 *       pick' badge shows only when the plan carries decider_pick;
	 *     · rejected routes with their reasons;
	 *   the plan is registered as a corridor event (C-I event_id): the board
	 *   reloads at once and the canvas flies to it (focus bus) — the primary
	 *   spine glows RECOMMENDED, the alternates dash.
	 *   A named duty officer (the C6 gate) launches the escort; the officer
	 *   travels in the request (contract C-J) and activates the event (C-F).
	 *   The escort is offered only on a plan that may depart: never on NO
	 *   ROUTE or a HOLD (the decider delayed departure — re-plan), and on a
	 *   CONDITIONAL plan only once the officer accepts its lane-floor
	 *   violations in the rail. While it runs, STOP ESCORT (the officer's
	 *   call, C-J /stop) is the way out; the lifecycle follows the brain's
	 *   escort status (escort.svelte.ts) — COMPLETED / STOPPED / FAILED end
	 *   it, never a timer or a hidden panel. Once the escort ended (or for a
	 *   plan that never departed) CLEAR runs the post-clearance restoration
	 *   (C-J /clear, officer named) and shows how the corridor event closed:
	 *   'completed' after a live run, 'cancelled' for one that never went
	 *   live. A plan asked for while an escort runs comes back 409: the rail
	 *   says 'escort running — stop it first'.
	 *
	 *   ESCORT AUTHORITY (officer approves every switch): while the escort
	 *   runs, the AI's ONE open proposal — a route switch, Plan B, a hold,
	 *   new closures — sits here too ('AI PROPOSES …', its countdown,
	 *   APPROVE / REJECT under the named officer, the same store action and
	 *   busy guard as the HUD), and how the last one ended (approved by /
	 *   rejected / expired — current plan held).
	 *
	 *   An escort launched elsewhere (the API, another console, a reload)
	 *   is picked up on mount: the active protected movement on the board is
	 *   asked for its escort status, and a running one is followed here —
	 *   the HUD and STOP come back with it.
	 *
	 *   The duty officer field is the store's (setDutyOfficer, so the plate
	 *   rule and audit follow it) and is read-only while the operator gate's
	 *   signed-in officer holds it.
	 *
	 *   An optional LABEL (an acceptance test, a rehearsal) rides the
	 *   movement as its title: the brain prefixes the corridor event's title
	 *   with it and carries it as metadata.label, so every board shows the
	 *   movement with a TEST pill — never as a real Z movement. An escort the
	 *   brain refuses with 409 (the movement or its corridor event was closed
	 *   on /ops) shows the brain's own reason, not a raw HTTP body.
	 *
	 * No client-side simulation anywhere: every step is a real call; every
	 * failure renders honestly in the rail.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { onDestroy } from 'svelte';
	import { ApiError, createMovement, planMovement, launchEscort, stopEscort } from '$lib/api/client';
	import type { MovementPlanDoc, PlanRouteCandidate } from '$lib/api/client';
	import { clearMovement } from '$lib/ops/client';
	import { fetchEscortStatus } from '$lib/gods-eye/city-api';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import { focus } from '$lib/gods-eye/focus.svelte';
	import { toasts } from '$lib/shell/toast.svelte';
	import { t } from '$lib/i18n/strings';
	import { escortFollower } from './escort.svelte';
	import { displayText } from '$lib/gods-eye/plates';

	/**
	 * The template's canonical POI list ([lon, lat] — API order) at the real
	 * places on the OpenStreetMap road network (mirrors
	 * scripts/poi_route_matrix.py POIS; each resolves to a corridor road
	 * within 600 m — the planner snaps to the nearest road).
	 */
	const POIS: ReadonlyArray<{ name: string; coord: [number, number] }> = [
		{ name: 'Sarkhej–Ashram South', coord: [72.5714, 23.0225] },
		{ name: 'Lal Darwaja', coord: [72.5813, 23.0264] },
		{ name: 'Ellis Bridge / CG Road', coord: [72.562, 23.03] },
		{ name: 'Sabarmati Riverfront', coord: [72.578, 23.035] },
		{ name: 'Sabarmati Ashram', coord: [72.5802, 23.0607] },
		{ name: 'Narendra Modi Stadium', coord: [72.5957, 23.0916] },
		{ name: 'Drive-In Road', coord: [72.5293, 23.0464] },
		{ name: 'Prahlad Nagar', coord: [72.512, 23.0065] },
		{ name: 'Satellite', coord: [72.5191, 23.0279] },
		{ name: 'Kalupur Railway Station', coord: [72.6005, 23.027] },
		{ name: 'Naroda', coord: [72.6428, 23.0649] },
		{ name: 'Kankaria Lake', coord: [72.5994, 23.0094] },
		{ name: 'SVPI Airport', coord: [72.6221, 23.072] },
		{ name: 'Science City', coord: [72.4978, 23.0813] },
	];

	/** Protection-class pills (template coding: Z+ red · Z orange · Y blue · X slate — token slots only). */
	const CLASSES: ReadonlyArray<{ id: 'Z+' | 'Z' | 'Y' | 'X'; token: string }> = [
		{ id: 'Z+', token: '--v-band-red' },
		{ id: 'Z', token: '--v-act' },
		{ id: 'Y', token: '--v-data' },
		{ id: 'X', token: '--v-ink-3' },
	];

	let originIdx = $state(1);
	let destIdx = $state(12);
	let vvipClass = $state<'Z+' | 'Z' | 'Y' | 'X'>('Z');
	let departure = $state('');
	/** Optional operator label (test / rehearsal): the movement's title (≤120). */
	let label = $state('');

	let phase = $state<'idle' | 'planning' | 'planned' | 'starting' | 'escorting' | 'stopping' | 'ended'>('idle');
	let movementId = $state<string | null>(null);
	let doc = $state.raw<MovementPlanDoc | null>(null);
	let error = $state<string | null>(null);
	let escortNote = $state<string | null>(null);
	let escortStartedAt = $state<number | null>(null);
	/** CONDITIONAL plans: the duty officer accepted the listed lane-floor violations (reset per plan). */
	let waiverAccepted = $state(false);
	/** The brain answered the officer's STOP with status 'stopped' (before the follower re-reads). */
	let stopAnswered = $state(false);
	/** CLEAR in flight / its answer: how the corridor event closed (C-F to_status). */
	let clearing = $state(false);
	let clearOutcome = $state<{ ok: boolean; toStatus: string | null; detail: string | null } | null>(null);
	/** Movements already asked whether an escort runs for them (one adoption attempt each). */
	const probedMovements = new Set<string>();

	const candidates = $derived(
		doc ? [doc.primary_route, ...(doc.alternate_routes ?? [])].filter((r): r is PlanRouteCandidate => r != null) : [],
	);
	const pick = $derived(doc?.decider_pick ?? null);
	/** The recommended route: the decider's applied pick, else the tactical primary. */
	const recommendedId = $derived(
		pick?.applied && pick.route_id && candidates.some((c) => c.route_id === pick.route_id)
			? pick.route_id
			: (candidates[0]?.route_id ?? null),
	);
	const recommendedBy = $derived(pick?.applied && pick.route_id ? 'decider' : 'tactical');
	const planStatus = $derived(doc?.status ?? null);
	const recommendedRoute = $derived(candidates.find((c) => c.route_id === recommendedId) ?? null);
	const plannedEtaSec = $derived(
		recommendedRoute?.estimated_time_sec && recommendedRoute.estimated_time_sec > 0 ? recommendedRoute.estimated_time_sec : null,
	);
	/** The decider delayed departure (status hold, or a hold riding on a conditional plan). */
	const held = $derived(planStatus === 'hold' || doc?.hold === true);
	const needsWaiver = $derived(planStatus === 'conditional' && !held);
	/** START ESCORT is offered at all: a plan that may depart. */
	const escortOffered = $derived(!!movementId && !!doc && planStatus !== 'no_route' && !held && candidates.length > 0);
	const escortRunning = $derived(phase === 'escorting' || phase === 'stopping');
	/** The brain's final word on this escort (follower), or the STOP answer while it re-reads. */
	const endedStatus = $derived(
		escortFollower.movementId === movementId ? (escortFollower.terminal ?? (stopAnswered ? 'stopped' : null)) : null,
	);

	// The brain says the escort ended (completed / stopped / failed): the
	// lifecycle closes here — the badge and timer stop, re-planning reopens.
	$effect(() => {
		if (escortRunning && endedStatus) phase = 'ended';
	});

	// Leaving the module drops the follower (its state lives in this rail).
	onDestroy(() => escortFollower.follow(null));

	/**
	 * An escort this rail did not launch (the API, another console, a
	 * reload): the active protected movement on the board is asked once for
	 * its escort status, and a running one is followed here.
	 */
	$effect(() => {
		if (phase !== 'idle' || movementId) return;
		const ev = ops.events.find(
			(e) => e.source !== 'offline-fixture' && e.eventKind === 'protected_movement' && e.state === 'active' && !!e.movementId,
		);
		const mid = ev?.movementId;
		if (!mid || probedMovements.has(mid)) return;
		probedMovements.add(mid);
		void fetchEscortStatus(mid, AbortSignal.timeout(6000))
			.then((st) => {
				if (phase !== 'idle' || movementId) return;
				if (st.status !== 'running' && st.status !== 'waiting_for_gps') return;
				movementId = mid;
				escortStartedAt = st.startedAt ?? Date.now();
				phase = 'escorting';
				escortNote = tOps(ops.lang, 'escortAdopted');
				escortFollower.follow(mid);
			})
			.catch(() => {
				// no escort route / no escort: the planner stays idle
			});
	});

	function defaultDeparture(): string {
		const d = new Date(Date.now() + 30 * 60 * 1000);
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
	}

	function statusWord(status: string | null): string {
		switch (status) {
			case 'approved':
				return t(ops.lang, 'tpl_status_approved');
			case 'conditional':
				return t(ops.lang, 'tpl_status_conditional');
			case 'hold':
				return t(ops.lang, 'tpl_status_hold');
			case 'no_route':
				return t(ops.lang, 'tpl_status_no_route');
			default:
				return status ?? '—';
		}
	}

	async function plan() {
		if (phase === 'planning' || escortRunning) return;
		error = null;
		escortNote = null;
		doc = null;
		waiverAccepted = false;
		stopAnswered = false;
		escortFollower.follow(null);
		phase = 'planning';
		const dep = departure || defaultDeparture();
		const params = {
			origin: POIS[originIdx].coord,
			destination: POIS[destIdx].coord,
			vvipClass,
			plannedDeparture: new Date(dep).toISOString(),
		};
		try {
			const { movementId: id } = await createMovement({ ...params, title: label.trim() || null });
			movementId = id;
			const planned = await planMovement(id!, params);
			doc = planned;
			const ranked = [planned.primary_route, ...(planned.alternate_routes ?? [])].filter(Boolean).length;
			// No candidates: the status block below says NO ROUTE with the
			// overall reasoning — once (not also as a red error line).
			phase = ranked ? 'planned' : 'idle';
			toasts.push({
				kind: 'act',
				title: `${t(ops.lang, 'tpl_candidates')} · ${statusWord(planned.status ?? null)}`,
				body: `${id} · ${ranked} ${t(ops.lang, 'tpl_planned_by')}`,
			});
			// The plan is a corridor event now (C-I): load it onto the board at
			// once and fly the canvas to it (the focus bus waits for geometry).
			if (planned.event_id) {
				const eventId = planned.event_id;
				void ops.pollBoard().then(() => {
					if (ops.events.some((e) => e.eventId === eventId) && ops.selectedEventId !== eventId) ops.selectEvent(eventId);
				});
				focus.request(eventId);
			}
		} catch (err) {
			phase = 'idle';
			// A 409 from /plan: an escort is running for this movement — the
			// brain will not re-plan under a live convoy.
			const status = (err as { status?: unknown } | null)?.status;
			const msg = err instanceof Error ? err.message : String(err);
			error = status === 409 || /^409\b/.test(msg) ? tOps(ops.lang, 'escortRunningStopFirst') : msg;
		}
	}

	async function escort() {
		if (!movementId || !escortOffered || escortRunning || phase === 'starting') return;
		error = null;
		escortNote = null;
		if (!ops.officerOn) {
			error = t(ops.lang, 'tpl_officer_needed');
			return;
		}
		if (needsWaiver && !waiverAccepted) {
			error = t(ops.lang, 'tpl_waiver_needed');
			return;
		}
		phase = 'starting';
		stopAnswered = false;
		try {
			const res = await launchEscort(movementId, { officer: ops.dutyOfficer, destination: POIS[destIdx].coord });
			if (res.status === 'running' || res.task_id) {
				// Contract C-J: the escort runs as a background task; the HUD
				// follows GET /movements/{id}/escort and the convoy GPS mirror.
				phase = 'escorting';
				escortStartedAt = Date.now();
				escortFollower.follow(movementId);
				toasts.push({ kind: 'act', title: t(ops.lang, 'tpl_escort_live'), body: `${movementId} · ${ops.dutyOfficer}` });
			} else {
				// A brain without escort control ran its loop inline and it ended
				// at once (no GPS stream) — say so instead of a timer pretending.
				phase = 'planned';
				escortNote = `${t(ops.lang, 'tpl_escort_legacy')}${res.final_status ? ` · final status ${res.final_status}` : ''}${
					res.total_iterations != null ? ` · ${res.total_iterations} iteration(s)` : ''
				}`;
			}
		} catch (err) {
			phase = 'planned';
			error = escortRefusal(err);
		}
	}

	/**
	 * Why the brain refused the escort: a 409 carries its reason as
	 * ApiError.detail ('movement is cancelled …', 'corridor event … is
	 * cancelled — an officer closed it …') — shown as written (plate-masked
	 * unless an officer is named), never the raw HTTP body.
	 */
	function escortRefusal(err: unknown): string {
		if (err instanceof ApiError && err.status === 409 && typeof err.detail === 'string' && err.detail.trim()) {
			return displayText(err.detail, ops.officerOn);
		}
		return err instanceof Error ? err.message : String(err);
	}

	/** The officer stops the escort (C-J POST /movements/{id}/stop, idempotent). */
	async function stop() {
		if (!movementId || phase !== 'escorting') return;
		error = null;
		if (!ops.officerOn) {
			error = t(ops.lang, 'tpl_officer_needed');
			return;
		}
		phase = 'stopping';
		try {
			const res = (await stopEscort(movementId, ops.dutyOfficer)) as { status?: string } | null;
			stopAnswered = res?.status === 'stopped';
			escortFollower.refresh();
			toasts.push({ kind: 'act', title: t(ops.lang, 'tpl_escort_stopped'), body: `${movementId} · ${ops.dutyOfficer}` });
			if (!stopAnswered) phase = 'escorting'; // the follower reports the brain's real state
		} catch (err) {
			phase = 'escorting';
			error = err instanceof Error ? err.message : String(err);
		}
	}

	/**
	 * CLEAR (C-J POST /movements/{id}/clear {officer}): post-clearance
	 * restoration under the named officer; the corridor event closes
	 * 'completed' after a live run, 'cancelled' if it never went live.
	 */
	async function clear() {
		if (!movementId || clearing || escortRunning) return;
		error = null;
		if (!ops.officerOn) {
			error = t(ops.lang, 'tpl_officer_needed');
			return;
		}
		clearing = true;
		clearOutcome = null;
		try {
			const res = await clearMovement(movementId, ops.dutyOfficer);
			clearOutcome = { ok: res.ok, toStatus: res.toStatus, detail: res.detail };
			if (res.ok) {
				void ops.pollBoard(true);
				toasts.push({
					kind: 'act',
					title: tOps(ops.lang, 'escortCleared'),
					body: `${movementId} · ${res.toStatus ?? '—'} · ${ops.dutyOfficer}`,
				});
			}
		} catch (err) {
			clearOutcome = { ok: false, toStatus: null, detail: err instanceof Error ? err.message : String(err) };
		} finally {
			clearing = false;
		}
	}

	function reset() {
		phase = 'idle';
		movementId = null;
		doc = null;
		error = null;
		escortNote = null;
		escortStartedAt = null;
		waiverAccepted = false;
		stopAnswered = false;
		clearOutcome = null;
		escortFollower.follow(null);
	}

	/** Readouts for the escort HUD (the page wires them via the callback prop). */
	const escortState = $derived({
		movementId,
		eventId: doc?.event_id ?? null,
		startedAt: escortStartedAt,
		plannedEtaSec,
		plannedDistanceM: recommendedRoute?.distance_m ?? recommendedRoute?.total_distance_m ?? null,
		phase:
			phase === 'escorting' || phase === 'stopping' || phase === 'ended' || phase === 'starting'
				? phase
				: ('planned' as const),
		endedStatus: phase === 'ended' ? endedStatus : null,
		vvipClass,
		destination: POIS[destIdx].name,
	});

	let {
		onEscortState = () => {},
	}: { onEscortState?: (s: typeof escortState) => void } = $props();

	$effect(() => {
		onEscortState(escortState);
	});

	// ── the escort's open proposal (store lanes: GET /api/convoy/escorts;
	//    the escort's own status document while those are not answering) ──
	let clock = $state(Date.now());
	$effect(() => {
		if (!escortRunning) return;
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});
	/** This escort's own status document (the follower's), the lanes' fallback. */
	const followedDoc = $derived(movementId && escortFollower.movementId === movementId ? escortFollower.status : null);
	const proposal = $derived(escortRunning ? ops.escortProposalFor(movementId, followedDoc) : null);
	const lastOutcome = $derived.by(() => {
		const o = escortRunning || phase === 'ended' ? ops.escortOutcomeFor(movementId, followedDoc) : null;
		return o && (o.decidedAt == null || clock - o.decidedAt < 300_000) ? o : null;
	});
	const proposalLeft = $derived(proposal?.expiresAt != null ? Math.max(0, Math.round((proposal.expiresAt - clock) / 1000)) : null);

	function mmss(sec: number): string {
		return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
	}

	function distanceKm(r: PlanRouteCandidate): string | null {
		const m = r.distance_m ?? r.total_distance_m;
		return m != null ? (m / 1000).toFixed(1) : null;
	}
</script>

<Panel variant="glass" title={t(ops.lang, 'tpl_planner')} sub="pplx-27b decider · gemma tactical · audited" lang={ops.lang} scroll>
	<div class="form">
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_origin')}</span>
			<select bind:value={originIdx} disabled={phase === 'planning' || escortRunning} data-testid="planner-origin">
				{#each POIS as poi, i (poi.name)}
					<option value={i}>{poi.name}</option>
				{/each}
			</select>
		</label>
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_destination')}</span>
			<select bind:value={destIdx} disabled={phase === 'planning' || escortRunning} data-testid="planner-destination">
				{#each POIS as poi, i (poi.name)}
					<option value={i}>{poi.name}</option>
				{/each}
			</select>
		</label>
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_departure')}</span>
			<input type="datetime-local" bind:value={departure} placeholder={defaultDeparture()} />
		</label>
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_label')}</span>
			<input
				type="text"
				bind:value={label}
				maxlength="120"
				placeholder="e.g. ACCEPTANCE TEST"
				title={t(ops.lang, 'tpl_label_hint')}
				disabled={phase === 'planning' || escortRunning}
				autocomplete="off"
				data-testid="planner-label"
			/>
		</label>

		<div class="field">
			<span class="field-label">{t(ops.lang, 'tpl_class')}</span>
			<div class="class-pills" role="radiogroup" aria-label={t(ops.lang, 'tpl_class')}>
				{#each CLASSES as c (c.id)}
					<button
						type="button"
						role="radio"
						aria-checked={vvipClass === c.id}
						class="class-pill"
						class:selected={vvipClass === c.id}
						style={`--pill: var(${c.token})`}
						onclick={() => (vvipClass = c.id)}
						disabled={escortRunning}
					>
						{c.id}
					</button>
				{/each}
			</div>
		</div>

		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_officer')}</span>
			<!-- the store's setter: the plate rule, board and audit follow the name; the gate's officer locks it -->
			<input
				type="text"
				value={ops.dutyOfficer}
				oninput={(e) => ops.setDutyOfficer(e.currentTarget.value)}
				readonly={ops.dutyOfficerFromSession}
				class:locked={ops.dutyOfficerFromSession}
				placeholder="e.g. R. Mehta"
				title={ops.dutyOfficerFromSession ? tOps(ops.lang, 'officerFromSession') : t(ops.lang, 'officer_hint')}
				autocomplete="off"
				spellcheck="false"
				data-testid="planner-officer"
			/>
		</label>

		{#if needsWaiver && escortOffered && (phase === 'planned' || phase === 'starting')}
			<label class="waiver mono" data-testid="planner-waiver">
				<input type="checkbox" bind:checked={waiverAccepted} disabled={phase === 'starting'} />
				<span>{t(ops.lang, 'tpl_waiver_ack')}{ops.officerOn ? ` — ${ops.dutyOfficer.trim()}` : ''}</span>
			</label>
		{/if}

		<div class="actions">
			{#if escortRunning}
				<Button
					variant="primary"
					size="sm"
					busy={phase === 'stopping'}
					actor={ops.officerOn ? ops.dutyOfficer : ''}
					onclick={() => void stop()}
				>
					{phase === 'stopping' ? t(ops.lang, 'tpl_escort_stopping') : t(ops.lang, 'tpl_escort_stop')}
				</Button>
				{#if escortFollower.note}
					<!-- the brain cannot report this escort's state: closing only hides it, said so in the note -->
					<Button variant="secondary" size="sm" onclick={reset}>{t(ops.lang, 'btn_close')}</Button>
				{/if}
			{:else}
				<Button variant="secondary" size="sm" busy={phase === 'planning'} onclick={() => void plan()}>
					{phase === 'planning' ? t(ops.lang, 'tpl_planning') : t(ops.lang, 'tpl_plan')}
				</Button>
				{#if (phase === 'planned' || phase === 'starting') && escortOffered}
					<Button
						variant="primary"
						size="sm"
						busy={phase === 'starting'}
						disabled={needsWaiver && !waiverAccepted}
						actor={ops.officerOn ? ops.dutyOfficer : ''}
						onclick={() => void escort()}
					>
						{phase === 'starting' ? t(ops.lang, 'tpl_escort_starting') : t(ops.lang, 'tpl_escort')}
					</Button>
				{/if}
				{#if movementId && (phase === 'ended' || (phase === 'planned' && doc)) && !clearOutcome?.ok}
					<Button
						variant="secondary"
						size="sm"
						busy={clearing}
						actor={ops.officerOn ? ops.dutyOfficer : ''}
						onclick={() => void clear()}
						title={tOps(ops.lang, 'escortClearHint')}
					>
						<span data-testid="planner-clear">{clearing ? tOps(ops.lang, 'escortClearing') : tOps(ops.lang, 'escortClear')}</span>
					</Button>
				{/if}
				{#if phase === 'ended'}
					<Button variant="secondary" size="sm" onclick={reset}>{t(ops.lang, 'btn_close')}</Button>
				{/if}
			{/if}
		</div>
		{#if clearOutcome}
			<p class="note mono" class:error={!clearOutcome.ok} data-testid="planner-clear-outcome" data-to-status={clearOutcome.toStatus ?? ''}>
				{clearOutcome.ok
					? tOps(ops.lang, 'escortClearOutcome', clearOutcome.toStatus ?? '—') + (clearOutcome.detail ? ` · ${clearOutcome.detail}` : '')
					: tOps(ops.lang, 'escortClearFailed', clearOutcome.detail ?? '—')}
			</p>
		{/if}
		{#if held && doc && (phase === 'planned' || phase === 'idle')}
			<p class="note mono" data-testid="planner-hold">{t(ops.lang, 'tpl_escort_hold')}</p>
		{/if}

		{#if error}
			<p class="error mono">{error}</p>
		{/if}
		{#if escortNote}
			<p class="note mono">{escortNote}</p>
		{/if}

		{#if proposal}
			<section class="proposal" data-testid="planner-proposal" data-approval-id={proposal.approvalId} data-kind={proposal.kind}>
				<header class="pr-head">
					<span class="pr-tag">{ops.escortProposerTag(proposal)}</span>
					{#if proposalLeft != null}
						<span class="pr-count mono" class:urgent={proposalLeft < 20}>
							{proposalLeft > 0 ? tOps(ops.lang, 'escExpiresIn', mmss(proposalLeft)) : tOps(ops.lang, 'escExpiring')}
						</span>
					{/if}
				</header>
				<p class="pr-what" title={proposal.summary ?? undefined}>{ops.escortProposalText(proposal)}</p>
				<p class="pr-meta mono">{ops.escortDeciderText(proposal)}</p>
				<div class="pr-actions">
					<Button
						variant="primary"
						size="sm"
						actor={ops.officerOn ? ops.dutyOfficer : ''}
						busy={ops.escortDecisionBusy === proposal.approvalId}
						disabled={!!ops.escortDecisionBusy || !ops.officerOn}
						onclick={() => void ops.decideEscortProposal(proposal!, 'approve').then(() => escortFollower.refresh())}
					>
						<span data-testid="planner-proposal-approve">{tOps(ops.lang, 'escApprove')}</span>
					</Button>
					<Button
						variant="danger-outline"
						size="sm"
						disabled={!!ops.escortDecisionBusy || !ops.officerOn}
						onclick={() => void ops.decideEscortProposal(proposal!, 'reject').then(() => escortFollower.refresh())}
					>
						<span data-testid="planner-proposal-reject">{tOps(ops.lang, 'escReject')}</span>
					</Button>
				</div>
				{#if !ops.officerOn}<p class="pr-need mono">{tOps(ops.lang, 'escNeedOfficer')}</p>{/if}
			</section>
		{:else if lastOutcome}
			<p class="note mono" data-testid="planner-proposal-outcome" data-status={lastOutcome.status} title={lastOutcome.summary ?? undefined}>
				{ops.escortOutcomeText(lastOutcome)}
			</p>
		{/if}
	</div>

	{#if doc && candidates.length}
		<div class="candidates" data-testid="planner-candidates" data-status={planStatus ?? ''}>
			<div class="plan-head">
				<h3 class="rail-h">{t(ops.lang, 'tpl_candidates')}</h3>
				<span class="status mono s-{planStatus ?? 'unknown'}" data-testid="planner-status">{statusWord(planStatus)}</span>
			</div>
			{#if doc.overall_reasoning}
				<p class="rec" data-testid="planner-reasoning">{doc.overall_reasoning}</p>
			{/if}
			{#if pick}
				<p class="pick mono" class:applied={pick.applied} data-testid="planner-decider-pick">
					<b>{pick.applied ? t(ops.lang, 'tpl_decider_pick') : t(ops.lang, 'tpl_decider_withheld')}</b>
					{pick.route_id ?? pick.choice ?? '—'} · {pick.tier}{pick.confidence != null ? ` · conf ${pick.confidence.toFixed(2)}` : ''}
					{#if pick.reason}<span class="why">— {pick.reason}</span>{/if}
				</p>
			{/if}
			<p class="sec-note mono">
				{doc.security_compliant ? 'security ✓' : 'security: not compliant'}
				{typeof doc.security_score === 'number' ? ` ${doc.security_score.toFixed(2)}` : ''}
				{#if doc.event_id}· {t(ops.lang, 'tpl_event_on_canvas')} {doc.event_id.slice(0, 8)}{/if}
			</p>
			{#each candidates as route, idx (route.route_id)}
				{@const recommended = route.route_id === recommendedId}
				{@const score = route.score ?? route.weighted_score ?? route.composite_score ?? null}
				{@const violations = route.lane_violations ?? []}
				<article class="cand" class:recommended data-route-id={route.route_id}>
					{#if recommended}
						<span class="ribbon" title={recommendedBy === 'decider' ? 'the decider’s applied pick' : 'the tactical primary (no applied decider pick)'}>
							{t(ops.lang, 'tpl_recommended')}{recommendedBy === 'decider' ? ' · DECIDER' : ''}
						</span>
					{/if}
					<header class="cand-head">
						<span class="cand-name">{idx === 0 ? 'primary' : `alt-${idx}`} · {String.fromCharCode(65 + idx)}</span>
						{#if score != null}<span class="cand-score mono">{score.toFixed(2)}</span>{/if}
					</header>
					<div class="cand-grid mono">
						{#if distanceKm(route) != null}
							<span>{distanceKm(route)} {t(ops.lang, 'tpl_km')}</span>
						{/if}
						{#if route.estimated_time_sec != null && route.estimated_time_sec > 0}
							<span>{t(ops.lang, 'tpl_eta')} {Math.max(1, Math.round(route.estimated_time_sec / 60))} {t(ops.lang, 'tpl_min')}</span>
						{/if}
						<span>{route.segment_ids?.length ?? 0} {t(ops.lang, 'tpl_segs')}</span>
					</div>
					{#if route.route_tier}
						<p class="tier mono" class:conditional={route.route_tier !== 'compliant'}>
							{route.route_tier === 'compliant'
								? `✓ ${t(ops.lang, 'tpl_route_compliant')}`
								: `⚠ ${route.route_tier} · ${violations.length} ${t(ops.lang, 'tpl_lane_violations')}`}
						</p>
					{/if}
					{#if violations.length}
						<ul class="violations mono">
							{#each violations.slice(0, 4) as v, vi (vi)}
								<li>{v.segment_id != null ? `seg ${v.segment_id}: ` : ''}{v.detail ?? v.type ?? 'lane floor'}</li>
							{/each}
						</ul>
					{/if}
					{#if route.reason}
						<p class="cand-why"><em>{t(ops.lang, 'tpl_why')}:</em> {route.reason}</p>
					{/if}
					{#if route.security_score != null && route.disruption_score != null}
						<div class="cand-bars">
							<span class="bar" title={`security ${route.security_score.toFixed(2)}`}>
								<i style={`width: ${Math.round(route.security_score * 100)}%`}></i>
								<b>SEC</b>
							</span>
							<span class="bar" title={`disruption ${route.disruption_score.toFixed(2)}`}>
								<i style={`width: ${Math.round(route.disruption_score * 100)}%`}></i>
								<b>DIS</b>
							</span>
						</div>
					{/if}
				</article>
			{/each}
			{#if doc.rejected_routes?.length}
				<div class="rejected">
					<h3 class="rail-h">{t(ops.lang, 'tpl_rejected')} · {doc.rejected_routes.length}</h3>
					<ul class="mono">
						{#each doc.rejected_routes as r (r.route_id)}
							<li>{r.route_id}: {r.reason ?? '—'}</li>
						{/each}
					</ul>
				</div>
			{/if}
		</div>
	{:else if doc && !candidates.length}
		<div class="candidates">
			<span class="status mono s-no_route">{statusWord(planStatus ?? 'no_route')}</span>
			{#if doc.overall_reasoning}<p class="rec">{doc.overall_reasoning}</p>{/if}
		</div>
	{:else if phase === 'idle' && !error}
		<EmptyState title={t(ops.lang, 'tpl_no_plan')} />
	{/if}

	{#if movementId}
		<p class="mid mono">mvmt {movementId}</p>
	{/if}
</Panel>

<style>
	.form {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		min-width: 0;
	}

	.field-label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	select,
	input {
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-1);
		font-family: var(--v-font-ui);
		font-size: var(--v-text-xs);
		padding: var(--v-space-1) var(--v-space-2);
		width: 100%;
	}

	select:focus-visible,
	input:focus-visible {
		outline: none;
		border-color: var(--v-act);
		box-shadow: 0 0 0 2px var(--v-act-veil);
	}

	.class-pills {
		display: flex;
		gap: var(--v-space-1);
	}

	.class-pill {
		flex: 1;
		height: var(--v-ctl-md);
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-line-2);
		background: transparent;
		color: var(--pill, var(--v-ink-2));
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		font-weight: 700;
		cursor: pointer;
		transition:
			border-color var(--v-dur-out) var(--v-ease),
			box-shadow var(--v-dur-out) var(--v-ease);
	}

	.class-pill.selected {
		border-color: var(--pill, var(--v-act));
		box-shadow: 0 0 10px var(--v-glow-act);
		background: color-mix(in srgb, var(--pill, var(--v-act)) 12%, transparent);
	}

	.actions {
		display: flex;
		gap: var(--v-space-2);
	}

	/* CONDITIONAL plan: the officer's explicit acceptance before an escort (act rail) */
	.waiver {
		display: flex;
		align-items: flex-start;
		gap: var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-1);
		border-left: 3px solid var(--v-act);
		padding-left: var(--v-space-2);
		cursor: pointer;
	}

	.waiver input {
		margin: 1px 0 0;
		accent-color: var(--v-act);
	}

	.error {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
		border-left: 3px solid var(--v-band-red);
		padding-left: var(--v-space-2);
		word-break: break-word;
	}

	/* the AI's escort proposal: the act edge — the officer decides */
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

	.pr-meta,
	.pr-need {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.pr-need {
		color: var(--v-act-text);
	}

	.pr-actions {
		display: flex;
		gap: var(--v-space-1);
	}

	.note {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border-left: 3px solid var(--v-ink-3);
		padding-left: var(--v-space-2);
	}

	.rail-h {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.plan-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.status {
		font-size: var(--v-text-2xs);
		font-weight: 700;
		letter-spacing: 0.06em;
		padding: 1px var(--v-space-2);
		border-radius: var(--v-radius-chip);
		border: 1px solid var(--v-line-2);
		color: var(--v-ink-2);
	}

	.status.s-approved {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.status.s-conditional,
	.status.s-hold {
		border-color: var(--v-act);
		color: var(--v-act-text);
	}

	.status.s-no_route {
		border-color: var(--v-band-red);
		color: var(--v-band-red);
	}

	.rec {
		margin: var(--v-space-1) 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border-left: 3px solid var(--v-act);
		padding-left: var(--v-space-2);
		line-height: 1.45;
	}

	.pick {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		line-height: 1.45;
	}

	.pick b {
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--v-ink-2);
		margin-right: var(--v-space-1);
	}

	.pick.applied b {
		color: var(--v-data);
	}

	.pick .why {
		color: var(--v-ink-3);
	}

	.sec-note {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: 0.04em;
	}

	.candidates {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		margin-top: var(--v-space-2);
	}

	.cand {
		position: relative;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		background: var(--v-surface-1);
	}

	.cand.recommended {
		border-color: var(--v-act);
		box-shadow: 0 0 14px var(--v-glow-act);
		overflow: hidden;
	}

	.ribbon {
		position: absolute;
		top: 0;
		right: 0;
		background: linear-gradient(135deg, var(--v-act), var(--v-act-2));
		color: var(--v-surface-sink);
		font-family: var(--v-font-display);
		font-size: var(--v-text-2xs);
		font-weight: 800;
		letter-spacing: 0.12em;
		padding: 2px 8px;
		border-bottom-left-radius: var(--v-radius-btn);
	}

	.cand-head {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.cand-name {
		font-family: var(--v-font-display);
		font-size: var(--v-text-xs);
		font-weight: 700;
		color: var(--v-ink-1);
	}

	.cand-score {
		font-size: var(--v-text-sm);
		font-weight: 700;
		color: var(--v-data);
		font-variant-numeric: tabular-nums;
	}

	.cand-grid {
		display: flex;
		gap: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		font-variant-numeric: tabular-nums;
	}

	.tier {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-green);
	}

	.tier.conditional {
		color: var(--v-act-text);
	}

	.violations {
		margin: 0;
		padding-left: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.cand-why {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border-left: 3px solid var(--v-act);
		padding-left: var(--v-space-2);
	}

	.cand-why em {
		color: var(--v-act-text);
		font-style: normal;
	}

	.cand-bars {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.bar {
		position: relative;
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.bar i {
		display: block;
		height: 3px;
		border-radius: 2px;
		background: linear-gradient(90deg, var(--v-act), var(--v-act-2));
		box-shadow: 0 0 6px var(--v-glow-act);
	}

	.bar:first-child i {
		background: var(--v-band-green);
		box-shadow: none;
	}

	.bar b {
		font-weight: 400;
		letter-spacing: var(--v-tracking-status);
	}

	.rejected ul {
		margin: var(--v-space-1) 0 0;
		padding-left: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mid {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
