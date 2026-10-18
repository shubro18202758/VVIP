<script lang="ts">
	/**
	 * DispatchTiles — the Jan Rakshak (and other agency) lifecycle board:
	 * requested → approved → en_route → on_scene, each transition a REAL
	 * city-writer POST under the named-officer gate. The verb rail shows
	 * only transitions the server's DISPATCH_TRANSITIONS allows (mirror in
	 * endpoints.ts); the audit tail renders as FeedItems (human rows flash).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { DISPATCH_TRANSITIONS, type DispatchState } from '$lib/ops/endpoints';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtClock } from '$lib/ops/format';

	const lang = $derived(ops.lang);

	let openAudit = $state<string | null>(null);
	let unitId = $state('JR-EM-01');
	let requestNote = $state('');

	const tiles = $derived(ops.dispatches.slice(0, 8));

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

	function verbKind(v: DispatchState): 'commit' | 'ghost' {
		// The officer APPROVE is the human-commit; lifecycle verbs are ghost
		// buttons (secondary actions on a live tile).
		return v === 'approved' ? 'commit' : 'ghost';
	}

	const LADDER = ['requested', 'approved', 'en_route', 'on_scene'] as const;

	/** A rail step is done when the live status has reached/passed it. */
	function stepDone(status: string, step: string): boolean {
		const s = LADDER.indexOf(status as (typeof LADDER)[number]);
		const p = LADDER.indexOf(step as (typeof LADDER)[number]);
		if (s < 0) return false; // completed/cancelled/deferred freeze the rail
		return s >= p;
	}

	async function fireVerb(dispatchId: string, verb: DispatchState) {
		if (verb === 'approved') await ops.decideDispatchAction(dispatchId, 'approve', requestNote);
		else if (verb === 'deferred') await ops.decideDispatchAction(dispatchId, 'defer', requestNote);
		else await ops.advanceDispatchStatus(dispatchId, verb as 'en_route' | 'on_scene' | 'completed' | 'cancelled');
	}

	async function requestUnit() {
		const ev = ops.selectedEvent;
		if (!ev || ev.source === 'offline-fixture') return;
		await ops.requestJanRakshak(ev.eventId, unitId.trim() || 'JR-EM-01');
	}
</script>

<Panel
	title={tOps(lang, 'dispatchTitle')}
	sub={tOps(lang, 'dispatchSub')}
	status={ops.dispatchesUpdated ? { state: ops.dispatchesLive === false ? 'stale' : 'live', updatedAt: ops.dispatchesUpdated } : null}
	lang={lang}
	scroll
>
	{#if ops.selectedEvent && ops.selectedEvent.source !== 'offline-fixture'}
		<div class="request-row">
			<input
				class="unit-input mono"
				type="text"
				placeholder={tOps(lang, 'dispatchUnitPlaceholder')}
				bind:value={unitId}
				spellcheck="false"
			/>
			<Button variant="secondary" size="sm" actor={ops.officerOn ? ops.dutyOfficer : null} onclick={() => requestUnit()}>
				{tOps(lang, 'dispatchRequestUnit')}
			</Button>
		</div>
	{/if}

	{#if tiles.length === 0}
		<EmptyState title={tOps(lang, 'dispatchNone')} />
	{/if}

	<div class="tiles">
		{#each tiles as d (d.dispatchId)}
			<article class="tile st-{d.status}" data-status={d.status}>
				<header class="tile-head">
					<span class="unit mono">{d.unitId ?? '—'}</span>
					<Badge variant={badgeVariant(d.status)} label={statusWord[d.status] ?? d.status} />
				</header>
				<p class="agency">{d.agencyLabel ?? d.agency}{d.waypointJunction != null ? ` · ${tOps(lang, 'dispatchAtWaypoint', d.waypointJunction)}` : ''}</p>
				<div class="rail" aria-hidden="true">
					{#each LADDER as step (step)}
						<span class="rail-step" class:done={stepDone(d.status, step)}></span>
					{/each}
				</div>
				{#if nextVerbs(d.status).length && d.status !== 'cancelled' && d.status !== 'completed'}
					<div class="verbs">
						{#each nextVerbs(d.status) as verb (verb)}
							<Button
								variant={verbKind(verb) === 'commit' ? 'primary' : 'ghost'}
								size="sm"
								busy={ops.dispatchBusy === d.dispatchId}
								actor={verbKind(verb) === 'commit' ? (ops.officerOn ? ops.dutyOfficer : null) : null}
								onclick={() => fireVerb(d.dispatchId, verb)}
							>
								{verbLabel(verb)}
							</Button>
						{/each}
					</div>
				{/if}
				<button class="audit-toggle" type="button" onclick={() => (openAudit = openAudit === d.dispatchId ? null : d.dispatchId)}>
					{tOps(lang, 'dispatchAudit')}
				</button>
				{#if openAudit === d.dispatchId}
					<ul class="audit">
						{#each [...d.audit].reverse() as entry, i (i)}
							<li>
								<FeedItem
									ts={Date.parse(entry.at) || 0}
									actor={entry.actor}
									actorKind="human"
									level={entry.toStatus === 'approved' ? 'decision' : 'info'}
									text={`${entry.fromStatus ?? '—'} → ${entry.toStatus}${entry.note ? ` · ${entry.note}` : ''}`}
									source="operator"
									lang={lang}
								/>
							</li>
						{/each}
					</ul>
				{/if}
				<footer class="tile-foot mono">{fmtClock(Date.parse(d.updatedAt ?? d.createdAt ?? '') || 0)}</footer>
			</article>
		{/each}
	</div>
</Panel>

<style>
	.request-row {
		display: flex;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-2);
	}

	.unit-input {
		flex: 1;
		min-width: 0;
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
		gap: var(--v-gap-panel);
	}

	.tile {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-pad-panel);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		min-width: 0;
	}

	.st-requested {
		border-color: var(--v-act-veil);
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
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
