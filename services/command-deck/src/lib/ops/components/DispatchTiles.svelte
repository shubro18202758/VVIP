<script lang="ts">
	/**
	 * DispatchTiles — the agency lifecycle board (Jan Rakshak first):
	 * requested → approved → en_route → on_scene, each transition a REAL
	 * city-writer POST under the named-officer gate. The request row picks
	 * the AGENCY from the closed registry (jan_rakshak, police, traffic, 108
	 * ambulance, fire) and a WAYPOINT: for a live pursuit, 'dispatch to choke
	 * #N' from its interception plan; otherwise the junctions nearest the
	 * event's spine. A waypoint is what lets GET /units project the unit on
	 * the canvas once a duty officer approves and it goes en route. /driver
	 * 108 requests (agency ambulance, requested_by '108-driver') sort first.
	 * The verb rail shows only transitions the server's DISPATCH_TRANSITIONS
	 * allows; audit tails keep their actor kind (system:event_closed is
	 * bookkeeping, never a person). Notes are free text an officer or a 108
	 * driver typed — they print under the plate rule like every other row.
	 *
	 * The board shows what is LIVE: terminal dispatches (completed,
	 * cancelled, deferred) older than 30 minutes fold behind 'history (n)'.
	 * A request's note is cleared once it was sent (it belonged to that
	 * request), and its 'awaiting APPROVE' line retires once that dispatch
	 * is decided. The EMULATED chip (shared with the decision queue) hides
	 * emulated dispatches, with the hidden count said.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import { ops, isLiveUuidEvent, actorKindOfName } from '$lib/ops/store.svelte';
	import { isEmulatedActor } from '$lib/ops/client';
	import { AGENCIES, DEFAULT_UNIT_ID, DISPATCH_TRANSITIONS, POLL_MS, type AgencyCode, type DispatchState } from '$lib/ops/endpoints';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtClock, fmtGoldenHour } from '$lib/ops/format';
	import type { DispatchRecord } from '$lib/ops/types';

	const lang = $derived(ops.lang);

	let openAudit = $state<string | null>(null);
	let agency = $state<AgencyCode>('jan_rakshak');
	let unitId = $state(DEFAULT_UNIT_ID.jan_rakshak);
	let unitTouched = $state(false);
	/** '' = first offered waypoint; 'none' = no waypoint; else a junction id. */
	let waypointChoice = $state<string>('');
	let requestNote = $state('');

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	const selected = $derived(ops.selectedEvent);
	const canRequest = $derived(isLiveUuidEvent(selected));
	const waypoints = $derived(ops.waypointOptions(selected));
	const waypoint = $derived.by(() => {
		if (waypointChoice === 'none') return null;
		if (waypointChoice) return waypoints.find((w) => String(w.junctionId) === waypointChoice) ?? waypoints[0] ?? null;
		return waypoints[0] ?? null;
	});

	// A new subject resets the waypoint pick to its first (best) option
	// (keyed on the selected id, so a board re-poll keeps the operator's pick).
	$effect(() => {
		void ops.selectedEventId;
		waypointChoice = '';
	});

	function onAgency(code: AgencyCode) {
		agency = code;
		if (!unitTouched) unitId = DEFAULT_UNIT_ID[code] ?? unitId;
	}

	/** 108-driver requests first, then other open requests, active units, history. */
	function rank(d: DispatchRecord): number {
		if (d.status === 'requested' && d.agency === 'ambulance' && d.requestedBy === '108-driver') return 0;
		if (d.status === 'requested') return 1;
		if (d.status === 'approved' || d.status === 'en_route' || d.status === 'on_scene') return 2;
		return 3;
	}

	/** Terminal dispatches older than this fold behind 'history (n)'. */
	const HISTORY_AFTER_MS = 30 * 60_000;
	const TERMINAL = new Set(['completed', 'cancelled', 'deferred']);
	let historyOpen = $state(false);

	function isHistory(d: DispatchRecord): boolean {
		if (!TERMINAL.has(d.status)) return false;
		const at = Date.parse(d.updatedAt ?? d.createdAt ?? '');
		return !Number.isFinite(at) || clock - at > HISTORY_AFTER_MS;
	}

	const sorted = $derived(
		[...ops.dispatches]
			.filter((d) => !(ops.hideEmulated && emulatedDispatch(d)))
			.sort((a, b) => rank(a) - rank(b) || Date.parse(b.updatedAt ?? b.createdAt ?? '') - Date.parse(a.updatedAt ?? a.createdAt ?? '')),
	);
	const history = $derived(sorted.filter(isHistory));
	const tiles = $derived(sorted.filter((d) => !isHistory(d)).slice(0, 10));
	const hiddenEmulated = $derived(ops.hideEmulated ? ops.dispatches.filter(emulatedDispatch).length : 0);
	const anyEmulated = $derived(ops.dispatches.some(emulatedDispatch));

	const statusWord: Record<string, string> = {
		requested: 'requested',
		approved: 'approved',
		en_route: 'en route',
		on_scene: 'on scene',
		completed: 'completed',
		cancelled: 'cancelled',
		deferred: 'deferred',
	};

	function badgeVariant(status: string): 'pending' | 'approved' | 'deferred' | 'live' | 'neutral' {
		if (status === 'requested') return 'pending';
		if (status === 'deferred') return 'deferred';
		if (status === 'cancelled' || status === 'completed') return 'neutral';
		return 'live';
	}

	function nextVerbs(status: string): DispatchState[] {
		return [...(DISPATCH_TRANSITIONS[status as DispatchState] ?? [])];
	}

	function verbLabel(v: DispatchState): string {
		if (v === 'approved') return tOps(lang, 'dispatchApprove');
		if (v === 'deferred') return tOps(lang, 'dispatchDefer');
		if (v === 'en_route') return tOps(lang, 'dispatchEnRoute');
		if (v === 'on_scene') return tOps(lang, 'dispatchOnScene');
		if (v === 'completed') return tOps(lang, 'dispatchComplete');
		return tOps(lang, 'dispatchCancel');
	}

	const LADDER = ['requested', 'approved', 'en_route', 'on_scene'] as const;

	/** A rail step is done when the live status has reached/passed it. */
	function stepDone(status: string, step: string): boolean {
		const s = LADDER.indexOf(status as (typeof LADDER)[number]);
		const p = LADDER.indexOf(step as (typeof LADDER)[number]);
		if (s < 0) return false; // completed/cancelled/deferred freeze the rail
		return s >= p;
	}

	// A tile's verb carries no note: the request row's note belongs to the
	// NEXT request, never to a decision on some other tile.
	async function fireVerb(dispatchId: string, verb: DispatchState) {
		if (verb === 'approved') await ops.decideDispatchAction(dispatchId, 'approve');
		else if (verb === 'deferred') await ops.decideDispatchAction(dispatchId, 'defer');
		else await ops.advanceDispatchStatus(dispatchId, verb as 'en_route' | 'on_scene' | 'completed' | 'cancelled');
	}

	async function requestUnit() {
		const ev = ops.selectedEvent;
		if (!isLiveUuidEvent(ev)) return;
		const sent = await ops.requestDispatchUnit(ev.eventId, {
			agency,
			unitId: unitId.trim() || DEFAULT_UNIT_ID[agency],
			waypointJunction: waypoint?.junctionId ?? null,
			note: requestNote.trim() || undefined,
		});
		// The note belonged to THAT request — the next one starts blank.
		if (sent) requestNote = '';
	}

	function waypointText(w: { source: string; rank: number; name: string; distanceM: number | null; likelyPassed: boolean; junctionId: number }): string {
		return w.source === 'interception'
			? tOps(lang, 'dispatchToChoke', w.rank, w.name) + (w.likelyPassed ? ` · ${tOps(lang, 'pursuitLikelyPassed')}` : '')
			: tOps(lang, 'dispatchNearSpine', w.junctionId, w.name, w.distanceM ?? 0);
	}

	const requestLabel = $derived(
		waypoint
			? tOps(
					lang,
					'dispatchRequestTo',
					AGENCIES[agency].label,
					waypoint.source === 'interception' ? tOps(lang, 'dispatchChokeShort', waypoint.rank) : `J${waypoint.junctionId}`,
				)
			: tOps(lang, 'dispatchRequestUnit'),
	);

	function eventTitle(id: string | null): string | null {
		const ev = ops.eventById(id);
		return ev ? ops.text(ev.name) : null;
	}

	function emulatedDispatch(d: DispatchRecord): boolean {
		const ev = ops.eventById(d.eventId);
		return !!ev?.emulated || isEmulatedActor(d.requestedBy);
	}
</script>

<Panel
	title={tOps(lang, 'dispatchTitle')}
	sub={tOps(lang, 'dispatchSub')}
	status={ops.dispatchesUpdated ? { state: ops.dispatchesLive === false ? 'stale' : 'live', updatedAt: ops.dispatchesUpdated } : null}
	pollMs={POLL_MS.dispatch}
	lang={lang}
	scroll
>
	{#if canRequest && selected}
		<div class="request" data-testid="dispatch-request">
			<p class="for mono">{tOps(lang, 'dispatchForEvent', ops.text(selected.name))}</p>
			<div class="request-row">
				<label class="field">
					<span>{tOps(lang, 'dispatchAgency')}</span>
					<select value={agency} onchange={(e) => onAgency(e.currentTarget.value as AgencyCode)} data-testid="dispatch-agency">
						{#each Object.entries(AGENCIES) as [code, a] (code)}
							<option value={code}>{a.label} · {a.labelNative}</option>
						{/each}
					</select>
				</label>
				<label class="field unit">
					<span>{tOps(lang, 'dispatchUnit')}</span>
					<input
						class="unit-input mono"
						type="text"
						placeholder={tOps(lang, 'dispatchUnitPlaceholder')}
						bind:value={unitId}
						oninput={() => (unitTouched = true)}
						spellcheck="false"
						maxlength="64"
						data-testid="dispatch-unit"
					/>
				</label>
			</div>
			<label class="field">
				<span>{tOps(lang, 'dispatchWaypoint')}</span>
				<select
					value={waypoint ? String(waypoint.junctionId) : 'none'}
					onchange={(e) => (waypointChoice = e.currentTarget.value)}
					data-testid="dispatch-waypoint"
				>
					{#each waypoints as w (w.junctionId)}
						<option value={String(w.junctionId)}>{waypointText(w)}</option>
					{/each}
					<option value="none">{tOps(lang, 'dispatchNoWaypoint')}</option>
				</select>
			</label>
			{#if !waypoints.length}
				<p class="hint mono">{ops.geometryNote ?? tOps(lang, 'dispatchNoWaypointHint')}</p>
			{/if}
			<div class="request-row">
				<input class="note-input" type="text" placeholder={tOps(lang, 'dispatchNotePlaceholder')} bind:value={requestNote} maxlength="500" />
				<Button
					variant="primary"
					size="sm"
					busy={ops.dispatchBusy === selected.eventId}
					disabled={ops.dispatchBusy !== null}
					actor={ops.officerOn ? ops.dutyOfficer : null}
					onclick={() => void requestUnit()}
				>
					<span data-testid="dispatch-request-btn">{requestLabel}</span>
				</Button>
			</div>
			{#if ops.dispatchRequestNote}
				<p class="result mono" class:ok={ops.dispatchRequestNote.ok} class:bad={!ops.dispatchRequestNote.ok}>{ops.text(ops.dispatchRequestNote.text)}</p>
			{/if}
		</div>
	{:else}
		<p class="hint mono">{tOps(lang, 'dispatchSelectLive')}</p>
	{/if}

	{#if anyEmulated || ops.hideEmulated}
		<div class="filters">
			<button
				type="button"
				class="filter-chip"
				class:on={ops.hideEmulated}
				aria-pressed={ops.hideEmulated}
				data-testid="tiles-emulated-filter"
				onclick={() => (ops.hideEmulated = !ops.hideEmulated)}
				title={tOps(lang, 'filterEmulatedHint')}
			>
				{ops.hideEmulated ? tOps(lang, 'filterEmulatedShow') : tOps(lang, 'filterEmulatedHide')}
			</button>
			{#if hiddenEmulated}<span class="filter-note mono">{tOps(lang, 'filterEmulatedHidden', hiddenEmulated)}</span>{/if}
		</div>
	{/if}

	{#if tiles.length === 0}
		<EmptyState title={ops.dispatchesNote ?? tOps(lang, history.length ? 'dispatchNoneLive' : 'dispatchNone')} />
	{/if}

	<div class="tiles">
		{#each [...tiles, ...(historyOpen ? history : [])] as d (d.dispatchId)}
			<article
				class="tile st-{d.status}"
				class:driver={d.requestedBy === '108-driver'}
				data-status={d.status}
				data-testid="dispatch-tile"
				data-dispatch-id={d.dispatchId}
				data-waypoint={d.waypointJunction ?? ''}
			>
				<header class="tile-head">
					<span class="unit mono">{d.unitId ?? '—'}</span>
					<Badge variant={badgeVariant(d.status)} label={statusWord[d.status] ?? d.status} />
				</header>
				<p class="agency">
					{d.agencyLabel ?? d.agency}
					{#if d.waypointJunction != null}· {tOps(lang, 'dispatchAtWaypoint', d.waypointJunction)}{ops.junctionName(d.waypointJunction) ? ` ${ops.junctionName(d.waypointJunction)}` : ''}{/if}
				</p>
				<div class="tags">
					{#if d.requestedBy === '108-driver'}<Badge variant="act" label={tOps(lang, 'dispatch108')} />{/if}
					{#if emulatedDispatch(d)}<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />{/if}
					{#if ops.dispatchEventClosed(d) && (d.status === 'approved' || d.status === 'en_route' || d.status === 'on_scene' || d.status === 'requested')}
						<span data-testid="dispatch-event-closed"><Badge variant="stale" label={tOps(lang, 'dispatchEventClosed')} title="its corridor event has left the board — GET /units does not project it and the counters do not count it" /></span>
					{/if}
					{#if d.status === 'requested'}<span class="golden mono">{tOps(lang, 'queueGoldenHour')} {fmtGoldenHour(d.createdAt, clock)}</span>{/if}
				</div>
				{#if eventTitle(d.eventId)}<p class="event mono">{eventTitle(d.eventId)}</p>{/if}
				{#if d.requestedBy}<p class="by mono">{tOps(lang, 'dispatchRequestedBy', d.requestedBy)}</p>{/if}
				<div class="rail" aria-hidden="true">
					{#each LADDER as step (step)}
						<span class="rail-step" class:done={stepDone(d.status, step)}></span>
					{/each}
				</div>
				{#if nextVerbs(d.status).length}
					<div class="verbs">
						{#each nextVerbs(d.status) as verb (verb)}
							<Button
								variant={verb === 'approved' ? 'primary' : verb === 'cancelled' ? 'danger-outline' : 'ghost'}
								size="sm"
								busy={ops.dispatchBusy === d.dispatchId && verb === 'approved'}
								disabled={ops.dispatchBusy !== null || (verb !== 'approved' && !ops.officerOn)}
								actor={verb === 'approved' ? (ops.officerOn ? ops.dutyOfficer : null) : null}
								onclick={() => void fireVerb(d.dispatchId, verb)}
							>
								<span data-verb={verb}>{verbLabel(verb)}</span>
							</Button>
						{/each}
					</div>
				{/if}
				<button class="audit-toggle" type="button" onclick={() => (openAudit = openAudit === d.dispatchId ? null : d.dispatchId)}>
					{tOps(lang, 'dispatchAudit')} ({d.audit.length})
				</button>
				{#if openAudit === d.dispatchId}
					<ul class="audit">
						{#each [...d.audit].reverse() as entry, i (i)}
							<li>
								<FeedItem
									ts={Date.parse(entry.at) || 0}
									actor={entry.actor}
									actorKind={actorKindOfName(entry.actor)}
									level={entry.toStatus === 'approved' ? 'decision' : 'info'}
									text={ops.text(`${entry.fromStatus ?? '—'} → ${entry.toStatus}${entry.note ? ` · ${entry.note}` : ''}`)}
									source="dispatch"
									lang={lang}
								/>
							</li>
						{/each}
					</ul>
				{/if}
				<footer class="tile-foot mono">{fmtClock(Date.parse(d.updatedAt ?? d.createdAt ?? '') || 0)} IST</footer>
			</article>
		{/each}
	</div>
	{#if history.length}
		<button
			class="history-toggle mono"
			type="button"
			aria-expanded={historyOpen}
			data-testid="dispatch-history"
			data-count={history.length}
			onclick={() => (historyOpen = !historyOpen)}
			title={tOps(lang, 'dispatchHistoryHint')}
		>
			{historyOpen ? tOps(lang, 'dispatchHistoryHide', history.length) : tOps(lang, 'dispatchHistory', history.length)}
		</button>
	{/if}
</Panel>

<style>
	.filters {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-2);
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

	.history-toggle {
		margin-top: var(--v-space-2);
		align-self: flex-start;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
		background: transparent;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		padding: 2px var(--v-space-2);
		cursor: pointer;
	}

	.history-toggle:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
	}

	.request {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-3);
		padding-bottom: var(--v-space-3);
		border-bottom: 1px solid var(--v-line-1);
	}

	.for {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.request-row {
		display: flex;
		gap: var(--v-space-2);
		align-items: flex-end;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
		flex: 1;
	}

	.field.unit {
		flex: 0 1 40%;
	}

	.field span {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
	}

	.field select,
	.unit-input,
	.note-input {
		height: var(--v-ctl-sm);
		min-width: 0;
		padding: 0 var(--v-space-2);
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.note-input {
		flex: 1;
	}

	.hint {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.result {
		margin: 0;
		font-size: var(--v-text-2xs);
		overflow-wrap: anywhere;
	}

	.result.ok {
		color: var(--v-band-green);
	}

	.result.bad {
		color: var(--v-band-red);
	}

	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
		gap: var(--v-gap-panel);
	}

	.tile {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		padding: var(--v-pad-panel);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		min-width: 0;
	}

	.st-requested {
		border-color: var(--v-act-veil);
	}

	.tile.driver {
		border-left: 2px solid var(--v-act);
	}

	.st-completed,
	.st-cancelled,
	.st-deferred {
		opacity: 0.72;
	}

	.tile-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.unit {
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.agency {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
		align-items: center;
	}

	.golden {
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
	}

	.event,
	.by {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.rail {
		display: flex;
		gap: 3px;
	}

	.rail-step {
		flex: 1;
		height: 3px;
		border-radius: var(--v-radius-pill);
		background: var(--v-line-2);
	}

	.rail-step.done {
		background: var(--v-band-green);
	}

	.verbs {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.audit-toggle {
		align-self: flex-start;
		font: inherit;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
		background: transparent;
		border: none;
		padding: 0;
		cursor: pointer;
		border-bottom: 1px dashed var(--v-line-2);
	}

	.audit {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.tile-foot {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
