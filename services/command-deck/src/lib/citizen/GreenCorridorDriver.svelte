<script lang="ts">
	/**
	 * GreenCorridorDriver — "The Window" (Daylight, Gujarati-first).
	 *
	 * The 108 driver flow on the REAL dispatch lifecycle (units/router.py):
	 *
	 *   request   POST /api/v1/events/{event}/dispatch — agency ambulance,
	 *             the driver's own 108 unit, the stretch's signalised
	 *             junction as waypoint, requested_by '108-driver'. The
	 *             event is the live public event that owns the stretch.
	 *   approval  a named duty officer approves or defers it on /ops
	 *             (DispatchTiles). The driver CANNOT approve: the page polls
	 *             its own request (GET /api/public/dispatches/{id}, one poll
	 *             in flight, statuses never regress) and says "awaiting duty
	 *             officer" until the officer decides.
	 *   window    doctrine: the corridor opens 60–180 s before the
	 *             ambulance reaches the junction (the event's advance
	 *             closure, clamped; its provenance — closure plan, live
	 *             adaptation or doctrine baseline — is shown). The bound
	 *             unit is the oracle's EMULATED projection, whose journey
	 *             starts at approved_at; its projected arrival times the
	 *             window (holds.windowTiming): "approved — opens in N s",
	 *             then "open — closes in N s" as it reaches the junction.
	 *             Without a projection the window runs from approved_at.
	 *             Server clock, skew corrected from as_of.
	 *
	 * Only REAL corridors are requestable: an event the emulator opened
	 * (SIMULATED FEED · EMULATED) is never offered as a live corridor — the
	 * page says how many it withheld — so a driver never files a 108 request
	 * against a demonstration run.
	 *
	 * A request in flight survives the public feed dropping out or the
	 * event leaving it (the request's event is snapshotted). The SCRIPTED
	 * FALLBACK (labelled, nothing sent, demo-scale numbers, no duty officer
	 * in the loop) appears only when no live public event exists, and once
	 * started it runs to its end even if a live event appears. Stretches
	 * carry their signage road names.
	 */
	import { onMount } from 'svelte';
	import { kindLabel, localizeNumber, t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import { requestDispatch } from '$lib/ops/client';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import LightCorridorMap from './LightCorridorMap.svelte';
	import { clockAt, clockAtMs, doctrineWindowSec, windowState, windowTiming, type Stretch } from './holds';
	import { PUBLIC_DISPATCH_API, publicFeed } from './public-feed.svelte';
	import { fixturePublicEvents, type PublicCorridorEvent } from './public-model';

	type Phase =
		| 'idle'
		| 'evaluating'
		| 'requested'
		| 'approved'
		| 'proposal'
		| 'open'
		| 'done'
		| 'deferred'
		| 'cancelled';
	type Urgency = 'cardiac' | 'trauma' | 'stroke' | 'other';

	interface TrackedUnit {
		status: string | null;
		etaSec: number | null;
		arrived: boolean;
		position: { lon: number; lat: number } | null;
	}

	let { lang, onBack }: { lang: string; onBack?: () => void } = $props();

	const UNIT_RE = /^108-[A-Za-z0-9]{1,8}$/;
	const POLL_MS = 3000;
	/** Backend lifecycle order (units/router.py); terminal states share the top rank. */
	const STATUS_RANK: Record<string, number> = {
		requested: 0,
		approved: 1,
		en_route: 2,
		on_scene: 3,
		completed: 4,
		deferred: 4,
		cancelled: 4,
	};

	let phase = $state<Phase>('idle');
	/** "<eventId>|<segmentId>" — the stretch AND the event that owns it. */
	let choice = $state('');
	let unitNo = $state('');
	let urgency = $state<Urgency>('cardiac');
	let requestError = $state<string | null>(null);
	let goldenElapsed = $state(0);
	let nowMs = $state(Date.now());

	// Live request state (the dispatch the officer decides on).
	let dispatchId = $state<string | null>(null);
	let dispatchEventId = $state<string | null>(null);
	/** The event + stretch the request was filed under (outlives the feed). */
	let requestSnapshot = $state<PublicCorridorEvent | null>(null);
	let requestStretch = $state<Stretch | null>(null);
	let dispatchStatus = $state<string | null>(null);
	let approvedAtMs = $state<number | null>(null);
	/** Projected arrival at the junction, server clock (eta_at + eta_sec). */
	let arrivalAtMs = $state<number | null>(null);
	let skewMs = $state(0);
	let pollFailing = $state(false);
	let unit = $state<TrackedUnit | null>(null);

	// Scripted-fallback state (demo-scale numbers, labelled).
	let scriptRunning = $state(false);
	let reachInSec = $state(0);
	let scriptedWindowSec = $state(0);
	let scriptedOpenedAt = $state(0);
	let scriptedUnit = $state<'none' | 'en_route' | 'on_scene'>('none');

	let pollTimer: ReturnType<typeof setInterval> | null = null;
	/** The dispatch whose status poll is in flight (one at a time). */
	let pollingId: string | null = null;
	let scriptTimers: ReturnType<typeof setTimeout>[] = [];

	/** Live public events with a spine (the request can only name one of these). */
	const liveEvents = $derived<PublicCorridorEvent[]>(
		publicFeed.status === 'live' || publicFeed.status === 'stale'
			? publicFeed.events.filter((e) => e.source === 'live' && e.stretches.length > 0)
			: [],
	);
	/** Emulated corridor events withheld from the picker (said, not hidden silently). */
	const emulatedWithheld = $derived(
		publicFeed.status === 'live' || publicFeed.status === 'stale'
			? publicFeed.events.filter((e) => e.source === 'emulated').length
			: 0,
	);
	const connecting = $derived(publicFeed.status === 'connecting');
	/**
	 * Scripted fallback ONLY when no live event exists and no live request
	 * is open — and once a scripted run starts it is latched to its end.
	 */
	const scripted = $derived(scriptRunning || (!connecting && liveEvents.length === 0 && dispatchId === null));
	const pickable = $derived<PublicCorridorEvent[]>(scripted ? fixturePublicEvents().slice(0, 1) : liveEvents);

	const chosen = $derived.by(() => {
		const [eid, sid] = choice.split('|');
		const ev = pickable.find((e) => e.eventId === eid);
		const stretch = ev?.stretches.find((s) => String(s.segmentId) === sid);
		return ev && stretch ? { ev, stretch } : null;
	});
	/** The event a live request was filed under: fresh from the feed, else its snapshot. */
	const requestEvent = $derived<PublicCorridorEvent | null>(
		dispatchEventId
			? (liveEvents.find((e) => e.eventId === dispatchEventId) ?? requestSnapshot)
			: (chosen?.ev ?? null),
	);
	const shownStretch = $derived<Stretch | null>(dispatchEventId ? requestStretch : (chosen?.stretch ?? null));

	/** The event's advance closure before the doctrine clamp, with its provenance. */
	const windowBasis = $derived.by(() => {
		if (scripted) return null;
		const adv = requestEvent?.hold.advanceClosureSec ?? null;
		return adv == null || !(adv > 0) ? null : { raw: Math.round(adv), source: requestEvent?.hold.source ?? null };
	});
	/** Window length: the event's advance closure inside the 60–180 s doctrine. */
	const windowSec = $derived(scripted ? scriptedWindowSec : doctrineWindowSec(windowBasis?.raw));
	const serverNow = $derived(nowMs + skewMs);
	const liveWindow = $derived(scripted ? null : windowTiming(approvedAtMs, arrivalAtMs, windowSec));

	/** Seconds left in an open window (scripted: from its scripted opening). */
	const countdown = $derived.by(() => {
		if (phase !== 'open' || windowSec == null) return null;
		if (scripted) {
			if (!scriptedOpenedAt) return null;
			return Math.max(0, windowSec - Math.floor((nowMs - scriptedOpenedAt) / 1000));
		}
		return liveWindow ? Math.max(0, Math.ceil((liveWindow.closesAt - serverNow) / 1000)) : null;
	});
	/** Seconds until an approved window opens (live, arrival-timed only). */
	const opensIn = $derived(
		phase === 'approved' && liveWindow ? Math.max(0, Math.ceil((liveWindow.opensAt - serverNow) / 1000)) : null,
	);

	// Scripted: the window closes when its countdown runs out.
	$effect(() => {
		if (scripted && countdown === 0 && phase === 'open') {
			phase = 'done';
			scriptRunning = false;
		}
	});

	// Live: an approved request's phase follows its window on the server clock.
	$effect(() => {
		if (scripted || (phase !== 'approved' && phase !== 'open')) return;
		if (!liveWindow) {
			// Approved, but no window length is published: open, untimed.
			if (phase !== 'open') phase = 'open';
			return;
		}
		const state = windowState(liveWindow, serverNow);
		if (state === 'closed') {
			phase = 'done';
			stopPolling();
		} else {
			const next: Phase = state === 'open' ? 'open' : 'approved';
			if (phase !== next) phase = next;
		}
	});

	const minutesSaved = $derived(Math.max(1, Math.round(reachInSec / 60)));

	const URGENCIES: Array<{ key: Urgency; labelKey: string }> = [
		{ key: 'cardiac', labelKey: 'urgCardiac' },
		{ key: 'trauma', labelKey: 'urgTrauma' },
		{ key: 'stroke', labelKey: 'urgStroke' },
		{ key: 'other', labelKey: 'urgOther' },
	];

	const rail = $derived<Array<{ key: Phase; labelKey: string }>>(
		scripted
			? [
					{ key: 'idle', labelKey: 'phase_idle' },
					{ key: 'evaluating', labelKey: 'phase_evaluating' },
					{ key: 'proposal', labelKey: 'phase_proposal' },
					{ key: 'open', labelKey: 'phase_open' },
				]
			: [
					{ key: 'idle', labelKey: 'phase_idle' },
					{ key: 'requested', labelKey: 'phase_requested' },
					{ key: 'approved', labelKey: 'phase_approved' },
					{ key: 'open', labelKey: 'phase_open' },
				],
	);
	const railActive = $derived<Phase>(
		phase === 'done' ? 'open' : phase === 'evaluating' && !scripted ? 'requested' : phase,
	);

	onMount(() => {
		const golden = setInterval(() => {
			goldenElapsed += 1;
			nowMs = Date.now();
		}, 1000);
		return () => {
			clearInterval(golden);
			stopPolling();
			for (const tm of scriptTimers) clearTimeout(tm);
		};
	});

	function goldenClock(): string {
		const mm = String(Math.floor(goldenElapsed / 60)).padStart(2, '0');
		const ss = String(goldenElapsed % 60).padStart(2, '0');
		return localizeNumber(`${mm}:${ss}`, lang);
	}

	function stretchLabel(roadName: string | null, segmentId: number): string {
		const seg = localizeNumber(segmentId, lang);
		return roadName ? citizenT(lang, 'driver_stretch_option', roadName, seg) : `${t(lang, 'freezeZone')} ${seg}`;
	}

	/** Where the window length came from, clamping and timing included. */
	function windowSourceText(): string {
		const basis = windowBasis;
		if (!basis || windowSec == null) return citizenT(lang, 'driver_window_unpublished');
		const key =
			basis.source === 'adaptive_params'
				? 'driver_window_from_adaptive'
				: basis.source === 'adaptive_baseline'
					? 'driver_window_from_baseline'
					: 'driver_window_from_policy';
		let text = citizenT(lang, key, localizeNumber(windowSec, lang));
		if (basis.raw !== windowSec) text += ` ${citizenT(lang, 'driver_window_clamped', localizeNumber(basis.raw, lang))}`;
		if (liveWindow) {
			text += ` · ${citizenT(lang, liveWindow.anchor === 'eta' ? 'driver_window_anchor_eta' : 'driver_window_anchor_approval')}`;
		}
		return text;
	}

	function stopPolling() {
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = null;
	}

	function resetLive() {
		stopPolling();
		pollingId = null;
		dispatchId = null;
		dispatchEventId = null;
		requestSnapshot = null;
		requestStretch = null;
		dispatchStatus = null;
		approvedAtMs = null;
		arrivalAtMs = null;
		unit = null;
		pollFailing = false;
	}

	/**
	 * Map the officer's decision onto the driver's phase. Answers can land
	 * out of order (3 s poll, 8 s timeout): a status BEHIND the one already
	 * shown, or anything after a terminal state, is ignored.
	 */
	function applyStatus(status: string) {
		const rank = STATUS_RANK[status];
		if (rank === undefined) return;
		const current = dispatchStatus ? STATUS_RANK[dispatchStatus] : undefined;
		if (current !== undefined && (rank < current || (current === 4 && status !== dispatchStatus))) return;
		// The ticker refreshes once a second; window timing must not start
		// up to a second stale when the officer's approval lands.
		nowMs = Date.now();
		dispatchStatus = status;
		if (status === 'requested') phase = 'requested';
		else if (status === 'deferred') phase = 'deferred';
		else if (status === 'cancelled') phase = 'cancelled';
		else if (status === 'completed') phase = 'done';
		else if (phase === 'requested' || phase === 'evaluating') phase = 'approved'; // the window effect times it
		if (rank === 4) stopPolling();
	}

	async function pollDispatch() {
		const id = dispatchId;
		const eventId = dispatchEventId;
		if (!id || !eventId || pollingId === id) return;
		pollingId = id;
		try {
			const res = await fetch(`${PUBLIC_DISPATCH_API}/${id}?event=${eventId}`, {
				headers: { accept: 'application/json' },
				signal: AbortSignal.timeout(8000),
			});
			if (id !== dispatchId) return; // a newer request (or a reset) superseded this poll
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const body = (await res.json()) as {
				as_of?: string;
				dispatch?: { status?: string; approved_at?: string | null };
				unit?: {
					status?: string | null;
					eta_sec?: number | null;
					eta_at?: string | null;
					arrived?: boolean;
					position?: [number, number] | null;
				} | null;
			};
			if (id !== dispatchId) return;
			const asOf = body.as_of ? Date.parse(body.as_of) : Number.NaN;
			if (Number.isFinite(asOf)) skewMs = asOf - Date.now();
			// Approval is a fact once seen: a later answer never clears it.
			const approved = body.dispatch?.approved_at ? Date.parse(body.dispatch.approved_at) : Number.NaN;
			if (Number.isFinite(approved)) approvedAtMs = approved;
			const u = body.unit;
			if (u) {
				unit = {
					status: u.status ?? null,
					etaSec: typeof u.eta_sec === 'number' ? Math.round(u.eta_sec) : null,
					arrived: u.arrived === true,
					position: u.position ? { lon: u.position[0], lat: u.position[1] } : null,
				};
				const etaAt = u.eta_at ? Date.parse(u.eta_at) : Number.NaN;
				if (typeof u.eta_sec === 'number' && Number.isFinite(etaAt)) arrivalAtMs = etaAt + u.eta_sec * 1000;
			}
			pollFailing = false;
			applyStatus(body.dispatch?.status ?? 'requested');
		} catch {
			if (id === dispatchId) pollFailing = true;
		} finally {
			if (pollingId === id) pollingId = null;
		}
	}

	async function request() {
		const c = chosen;
		if (!c) return;
		requestError = null;
		if (scripted) {
			runScripted(c.stretch.segmentId);
			return;
		}
		if (!UNIT_RE.test(unitNo.trim())) {
			requestError = citizenT(lang, 'driver_unit_invalid');
			return;
		}
		resetLive();
		phase = 'evaluating';
		try {
			const rec = await requestDispatch(c.ev.eventId, {
				agency: 'ambulance',
				unitId: unitNo.trim(),
				waypointJunction: c.stretch.junctionId ?? undefined,
				requestedBy: '108-driver',
				note: `green corridor · ${urgency} · ${c.stretch.roadName ?? `segment ${c.stretch.segmentId}`}`,
			});
			if (!rec) throw new Error('no dispatch id returned');
			dispatchId = rec.dispatchId;
			dispatchEventId = c.ev.eventId;
			requestSnapshot = c.ev;
			requestStretch = c.stretch;
			applyStatus(rec.status);
			pollTimer = setInterval(() => void pollDispatch(), POLL_MS);
			void pollDispatch();
		} catch (err) {
			phase = 'idle';
			requestError = citizenT(lang, 'driver_request_failed', err instanceof Error ? err.message : String(err));
		}
	}

	/** The labelled scripted fallback: nothing is sent; numbers are demo-scale. */
	function runScripted(segmentId: number) {
		for (const tm of scriptTimers) clearTimeout(tm);
		scriptTimers = [];
		scriptRunning = true;
		phase = 'evaluating';
		scriptedUnit = 'none';
		scriptTimers.push(
			setTimeout(() => {
				reachInSec = 120 + (segmentId % 3) * 30;
				scriptedWindowSec = urgency === 'cardiac' ? 90 : urgency === 'stroke' ? 75 : 60;
				phase = 'proposal';
			}, 1400),
			setTimeout(() => {
				scriptedOpenedAt = Date.now();
				scriptedUnit = 'en_route';
				phase = 'open';
			}, 4400),
			setTimeout(() => (scriptedUnit = 'on_scene'), 13_400),
		);
	}

	function again() {
		for (const tm of scriptTimers) clearTimeout(tm);
		scriptTimers = [];
		scriptRunning = false;
		resetLive();
		phase = 'idle';
	}

	function statusWord(status: string | null): string {
		return status ? citizenT(lang, `dispatch_status_${status}`) : '—';
	}
</script>

{#snippet liveCorridor()}
	<div class="corridor-block">
		<h3>{citizenT(lang, 'corridor_map')} · {citizenT(lang, 'corridor_granted_stretch')}</h3>
		<LightCorridorMap
			{lang}
			compact
			highlightSegmentId={shownStretch?.segmentId ?? null}
			routes={requestEvent?.stretches.map((s) => ({ segmentId: s.segmentId, live: !scripted })) ?? []}
			unit={scripted ? null : (unit?.position ?? null)}
		/>
		{#if scripted}
			<p class="unit" data-unit-status={scriptedUnit}>
				<Badge variant="emulated" label={citizenT(lang, 'comms_dispatch_demo')} />
				<span>{scriptedUnit === 'on_scene' ? citizenT(lang, 'unit_on_scene') : citizenT(lang, 'unit_en_route')}</span>
			</p>
			<p class="unit-note">{citizenT(lang, 'units_note_fallback')}</p>
		{:else}
			<p class="unit" data-unit-status={dispatchStatus ?? 'none'}>
				<Badge variant="emulated" label={citizenT(lang, 'driver_unit_emulated')} />
				<span class="mono">{unitNo.trim()}</span>
				<span>· {statusWord(dispatchStatus)}</span>
				{#if unit?.arrived}
					<span>· {citizenT(lang, 'driver_unit_arrived')}</span>
				{:else if unit?.etaSec != null}
					<span class="mono">· {citizenT(lang, 'driver_unit_eta', localizeNumber(unit.etaSec, lang))}</span>
				{:else}
					<span>· {citizenT(lang, 'driver_unit_no_position')}</span>
				{/if}
			</p>
		{/if}
	</div>
{/snippet}

<div class="driver" data-screen="driver" data-phase={phase} data-mode={connecting && dispatchId === null && !scriptRunning ? 'connecting' : scripted ? 'scripted' : 'live'}>
	<!-- Golden hour: always on screen, mono, ticking. -->
	<div class="golden" data-testid="golden-hour">
		<span class="golden-label">{citizenT(lang, 'golden_hour')}</span>
		<span class="golden-clock mono" data-golden-seconds={goldenElapsed}>{goldenClock()}</span>
		<span class="golden-elapsed">{citizenT(lang, 'golden_elapsed')}</span>
	</div>

	<header class="head">
		{#if onBack}
			<button class="back" onclick={onBack}>← {t(lang, 'driverBack')}</button>
		{/if}
		<h1>{t(lang, 'driverTitle')}</h1>
		<p>{t(lang, 'driverSub')}</p>
	</header>

	<!-- Fixed verb rail. -->
	<ol class="rail" data-testid="phase-rail">
		{#each rail as r (r.key)}
			<li
				class="step"
				class:active={railActive === r.key}
				class:done={railActive !== 'idle' && rail.findIndex((x) => x.key === railActive) > rail.findIndex((x) => x.key === r.key)}
			>
				<span class="dot" aria-hidden="true"></span>
				<span class="word">{citizenT(lang, r.labelKey)}</span>
			</li>
		{/each}
	</ol>

	{#if connecting && dispatchId === null && !scriptRunning}
		<!-- no mode claim until the feed has answered -->
	{:else if scripted}
		<p class="scripted-note" data-testid="scripted-note">
			<Badge variant="emulated" label={citizenT(lang, 'scripted_badge')} />
			{citizenT(lang, 'scripted_note')}
		</p>
	{:else}
		<p class="live-note">
			<Badge variant="live" label={t(lang, 'opsSourceLive')} />
			{citizenT(lang, 'driver_live_note')}
		</p>
	{/if}

	{#if connecting && dispatchId === null && !scriptRunning}
		<EmptyState title={citizenT(lang, 'citizen_feed_connecting')} />
	{:else if phase === 'evaluating'}
		<section class="evaluating" data-testid="evaluating">
			<div class="pulse" aria-hidden="true"></div>
			<p>{t(lang, 'evaluating')}</p>
		</section>
	{:else if phase === 'requested'}
		<!-- Live: the officer decides on /ops; the driver can only wait. -->
		<section class="awaiting" data-testid="awaiting-officer">
			<div class="pulse" aria-hidden="true"></div>
			<div>
				<h2>{citizenT(lang, 'driver_awaiting_title')}</h2>
				<p>{citizenT(lang, 'driver_awaiting_body')}</p>
				{#if dispatchId}
					<p class="ref mono">{citizenT(lang, 'driver_request_ref', dispatchId.slice(0, 8))} · {unitNo.trim()}</p>
				{/if}
				{#if pollFailing}
					<p class="poll-note">{citizenT(lang, 'driver_status_retry')}</p>
				{/if}
			</div>
		</section>
	{:else if phase === 'proposal'}
		<!-- Scripted fallback only: demo-scale numbers, approval is scripted. -->
		<section class="proposal" data-testid="proposal">
			<div class="proposal-head">
				<h2>{t(lang, 'proposalTitle')}</h2>
				<Badge variant="emulated" label={citizenT(lang, 'scripted_badge')} />
			</div>
			<p class="reach">
				{t(lang, 'convoyReachesIn')}
				<strong class="mono">{localizeNumber(reachInSec, lang)} s</strong>
				<span class="mono">({clockAt(reachInSec, lang)})</span>
			</p>
			<p class="cross">
				{t(lang, 'crossBefore')} <strong class="mono">{clockAt(reachInSec + scriptedWindowSec, lang)}</strong>
			</p>
			<p class="doctrine">{citizenT(lang, 'doctrine_note')}</p>
			<p class="scripted-approval">{citizenT(lang, 'driver_scripted_approval')}</p>
		</section>
	{:else if phase === 'approved' || phase === 'open'}
		<!--
			One branch for both live states, so the corridor map is NOT
			remounted at the moment the window opens: approved = primed (opens
			one window-length before the unit's projected arrival), open =
			counting down to that arrival. The scripted run uses 'open' only.
		-->
		<section
			class={phase === 'open' ? 'window-open' : 'primed'}
			data-testid={phase === 'open' ? 'window-open' : 'window-primed'}
		>
			{#if phase === 'open'}
				<h2 class="open-title">{t(lang, 'windowOpen')}</h2>
				{#if countdown != null}
					<p class="countdown mono" data-countdown={countdown}>
						{t(lang, 'windowClosesIn')} {localizeNumber(countdown, lang)} s
					</p>
				{/if}
			{:else}
				<h2 class="primed-title">{citizenT(lang, 'driver_primed_title')}</h2>
				{#if opensIn != null && liveWindow}
					<p class="countdown mono" data-opens-in={opensIn}>
						{citizenT(lang, 'driver_primed_countdown', localizeNumber(opensIn, lang))}
					</p>
					<p class="primed-when">{citizenT(lang, 'driver_primed_when', clockAtMs(liveWindow.opensAt - skewMs, lang))}</p>
				{/if}
			{/if}
			{#if !scripted}
				<p class="window-source" data-window-source={windowBasis?.source ?? 'none'} data-window-anchor={liveWindow?.anchor ?? 'none'}>
					{windowSourceText()}
				</p>
				{#if pollFailing}
					<p class="poll-note">{citizenT(lang, 'driver_status_retry')}</p>
				{/if}
			{/if}
			{@render liveCorridor()}
		</section>
	{:else}
		{#if phase === 'deferred' || phase === 'cancelled'}
			<p class="rail-note" data-phase-note={phase}>
				{phase === 'deferred' ? citizenT(lang, 'driver_deferred_body') : citizenT(lang, 'driver_cancelled_body')}
			</p>
		{:else if phase === 'done'}
			<p class="rail-note done-note" data-phase-note="done">{citizenT(lang, 'driver_window_closed')}</p>
		{/if}
		{#if pickable.length === 0}
			<EmptyState title={t(lang, 'noMovement')} hint={t(lang, 'noMovementSub')} />
		{:else}
			<section class="request-card">
				<label class="stretch-pick">
					<span>{t(lang, 'atStretch')}</span>
					<select bind:value={choice} data-testid="stretch-select">
						<option value="">—</option>
						{#each pickable as ev (ev.eventId)}
							<optgroup label={citizenT(lang, 'driver_event_label', kindLabel(lang, ev.eventKind))}>
								{#each ev.stretches as st (st.segmentId)}
									<option value={`${ev.eventId}|${st.segmentId}`}>{stretchLabel(st.roadName, st.segmentId)}</option>
								{/each}
							</optgroup>
						{/each}
					</select>
				</label>
				{#if !scripted}
					<label class="unit-pick">
						<span>{citizenT(lang, 'driver_unit_label')}</span>
						<input
							type="text"
							bind:value={unitNo}
							placeholder="108-14"
							autocomplete="off"
							spellcheck="false"
							aria-label={citizenT(lang, 'driver_unit_hint')}
							data-testid="unit-input"
						/>
					</label>
				{/if}
				<fieldset class="urgency">
					<legend>{t(lang, 'urgency')}</legend>
					{#each URGENCIES as u (u.key)}
						<button type="button" class="urg" class:active={urgency === u.key} onclick={() => (urgency = u.key)}>
							{t(lang, u.labelKey)}
						</button>
					{/each}
				</fieldset>
				<div class="request-row">
					<Button
						variant="primary"
						size="citizen"
						actor="108-driver"
						onclick={() => void request()}
						disabled={!chosen || (!scripted && !UNIT_RE.test(unitNo.trim()))}
					>
						{t(lang, 'requestBtn')}
					</Button>
				</div>
				{#if requestError}
					<p class="request-error" data-testid="request-error">{requestError}</p>
				{/if}
				{#if phase === 'done' && scripted}
					<div class="after-action" data-testid="after-action">
						<Badge variant="emulated" label={citizenT(lang, 'scripted_badge')} />
						<p class="saved mono">~{localizeNumber(minutesSaved, lang)} {t(lang, 'minutesReturned')}</p>
						<p>{citizenT(lang, 'after_action', localizeNumber(minutesSaved, lang))}</p>
					</div>
				{/if}
			</section>
		{/if}
	{/if}

	{#if (phase === 'deferred' || phase === 'cancelled' || (phase === 'done' && !scripted)) && pickable.length === 0}
		<!-- Only when no request card is on screen (it carries the one request button). -->
		<Button variant="ghost" size="sm" onclick={again}>{citizenT(lang, 'driver_new_request')}</Button>
	{/if}

	{#if emulatedWithheld > 0 && dispatchId === null}
		<p class="emulated-withheld" data-testid="driver-emulated-withheld" data-count={emulatedWithheld}>
			<Badge variant="emulated" label={citizenT(lang, 'citizen_emulated_badge')} />
			{citizenT(lang, 'driver_emulated_hidden', localizeNumber(emulatedWithheld, lang))}
		</p>
	{/if}

	<div class="control-room">
		<h3>{citizenT(lang, 'control_room')}</h3>
		<p class="mono">{citizenT(lang, 'control_room_line')}</p>
	</div>

	<p class="baseline">{t(lang, 'baselineNote')}</p>
	{#if scripted}
		<p class="demo">{t(lang, 'demoNote')}</p>
	{/if}
</div>

<style>
	.driver {
		max-width: 640px;
		margin: 0 auto;
		padding: var(--v-space-4);
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		font-size: var(--v-text-md);
		color: var(--v-ink-1);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.emulated-withheld {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--v-space-2);
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	/* ── Golden hour: always on screen ──────────────────────────────────── */
	.golden {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-3);
		padding: var(--v-space-3) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
	}

	.golden-label {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-act-text);
	}

	.golden-clock {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xl);
		font-weight: 600;
	}

	.golden-elapsed {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.head h1 {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-xl);
	}

	.head p {
		margin: var(--v-space-1) 0 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
	}

	.back {
		background: none;
		border: none;
		padding: 0;
		color: var(--v-data);
		font-size: var(--v-text-sm);
		cursor: pointer;
	}

	/* ── Verb rail ──────────────────────────────────────────────────────── */
	.rail {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.step {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		padding: var(--v-space-1) var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-pill);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.step .dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		border: 1px solid var(--v-line-2);
	}

	.step.active {
		border-color: var(--v-act-text);
		color: var(--v-ink-1);
	}

	.step.active .dot {
		background: var(--v-act);
		border-color: var(--v-act-text);
	}

	.step.done {
		color: var(--v-ink-2);
	}

	.step.done .dot {
		background: var(--v-band-green);
		border-color: var(--v-band-green);
	}

	.rail-note {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-act-text);
	}

	/* ── Request card ───────────────────────────────────────────────────── */
	.request-card,
	.proposal,
	.primed,
	.window-open,
	.control-room {
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		padding: var(--v-space-4);
	}

	.stretch-pick {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		margin-bottom: var(--v-space-3);
	}

	/* min-width 0: on a phone the select shrinks to the card (its longest
	 * option never pushes it past the edge or squeezes the label) */
	.stretch-pick select {
		flex: 1;
		min-width: 0;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2) var(--v-space-3);
		font-size: var(--v-text-sm);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
		height: var(--v-ctl-touch);
	}


	.urgency {
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2) var(--v-space-3);
		margin: 0 0 var(--v-space-4);
		display: flex;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.urgency legend {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		padding: 0 var(--v-space-1);
	}

	.urg {
		border: 1px solid var(--v-line-2);
		background: var(--v-surface-2);
		border-radius: var(--v-radius-pill);
		padding: var(--v-space-2) var(--v-space-4);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
		cursor: pointer;
		min-height: var(--v-ctl-sm);
	}

	.urg.active {
		border-color: var(--v-act-text);
		color: var(--v-act-text);
		background: color-mix(in srgb, var(--v-act) 8%, transparent);
	}

	.request-row {
		width: 100%;
	}

	.request-row :global(button) {
		width: 100%;
	}

	/* ── Evaluating ─────────────────────────────────────────────────────── */
	.evaluating {
		display: flex;
		align-items: center;
		gap: var(--v-space-4);
		padding: var(--v-space-6) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
	}

	.pulse {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--v-act);
		animation: pulse var(--v-dur-pulse) infinite alternate;
	}

	@keyframes pulse {
		from {
			opacity: 0.35;
		}
		to {
			opacity: 1;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.pulse {
			animation: none;
			opacity: 1;
		}
	}

	/* ── Proposal ───────────────────────────────────────────────────────── */
	.proposal-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--v-space-2);
	}

	.proposal h2 {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-lg);
	}

	.reach,
	.cross {
		margin: var(--v-space-1) 0;
		font-size: var(--v-text-md);
	}

	.reach strong,
	.cross strong {
		color: var(--v-act-text);
	}

	.doctrine {
		margin: var(--v-space-2) 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		border-top: 1px dashed var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.scripted-approval {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	/* ── Approved: window primed (live) ─────────────────────────────────── */
	.primed {
		border-color: color-mix(in srgb, var(--v-act-text) 35%, transparent);
	}

	.primed-title {
		margin: 0;
		font-size: var(--v-text-lg);
		color: var(--v-act-text);
	}

	.primed-when {
		margin: calc(-1 * var(--v-space-2)) 0 var(--v-space-3);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.primed .poll-note,
	.window-open .poll-note {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	/* ── Window open ────────────────────────────────────────────────────── */
	.window-open {
		border-color: color-mix(in srgb, var(--v-band-green) 40%, transparent);
		background: color-mix(in srgb, var(--v-band-green) 6%, var(--v-surface-1));
	}

	.open-title {
		margin: 0;
		color: var(--v-band-green);
		font-size: var(--v-text-lg);
		letter-spacing: 0.04em;
	}

	.countdown {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xl);
		font-weight: 600;
		margin: var(--v-space-2) 0 var(--v-space-3);
	}

	.corridor-block h3 {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.unit {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-sm);
	}

	.unit-note {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.after-action {
		margin-top: var(--v-space-4);
		border-top: 1px dashed var(--v-line-1);
		padding-top: var(--v-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		align-items: flex-start;
	}

	.saved {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-lg);
		color: var(--v-band-green);
		margin: 0;
	}

	.after-action p:last-child {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	/* ── Control room + notes ───────────────────────────────────────────── */
	.control-room h3 {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-md);
	}

	.control-room p {
		margin: 0;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.baseline {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		border-top: 1px dashed var(--v-line-1);
		padding-top: var(--v-space-3);
	}

	.scripted-note,
	.live-note {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.unit-pick {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		margin-bottom: var(--v-space-3);
	}

	.unit-pick input {
		flex: 1;
		min-width: 0;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2) var(--v-space-3);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-sm);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
		height: var(--v-ctl-touch);
	}

	.unit-pick input:focus-visible,
	.stretch-pick select:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.request-error {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-sm);
		color: var(--v-band-red);
	}

	/* ── Awaiting the duty officer (live) ────────────────────────────────── */
	.awaiting {
		display: flex;
		align-items: flex-start;
		gap: var(--v-space-4);
		padding: var(--v-space-5) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid color-mix(in srgb, var(--v-act-text) 35%, transparent);
		border-radius: var(--v-radius-panel);
	}

	.awaiting h2 {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-lg);
	}

	.awaiting p {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.awaiting .ref,
	.awaiting .poll-note {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.awaiting .pulse {
		margin-top: var(--v-space-2);
	}

	.window-source {
		margin: 0 0 var(--v-space-3);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.done-note {
		color: var(--v-ink-2);
	}

	.demo {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}
</style>
