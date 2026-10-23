<script lang="ts">
	/**
	 * LiveRail — the template's right operations rail, rebuilt on real
	 * feeds: corridor readouts (shared feed — NO LIVE FEED in neutral ink when
	 * no observation backs the summary, SIMULATED FEED when synthetic rows
	 * do; never zeros-as-clear, never LIVE for a simulated feed), the live
	 * event board (click = select + a focus request on the canvas bus: the
	 * canvas flies to the event's spine and shows LOCKED only when the flight
	 * lands — the rail never claims a lock itself; titles mask plates unless a
	 * duty officer is named), and the decision-audit tail (the same FeedItem
	 * contract as /ops; server rows carry their actor kind). C6 holds on every
	 * text this rail prints: event titles, the focus note, audit rows and
	 * their evidence payloads read GJ27EM•••1 until an officer is named.
	 * A labelled test / rehearsal movement (event metadata.label, carried by
	 * the brain from the movement's title) wears a TEST pill beside its kind,
	 * as the dossier does — it never reads as a real Z movement.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { godsEye } from '$lib/gods-eye/store.svelte';
	import { focus } from '$lib/gods-eye/focus.svelte';
	import { corridor } from './corridor.svelte';
	import { t } from '$lib/i18n/strings';
	import { displayDetail, displayText } from '$lib/gods-eye/plates';
	import type { AuditEntry } from '$lib/ops/types';

	const liveEvents = $derived(ops.events.filter((e) => e.source !== 'offline-fixture'));
	const auditTail = $derived(ops.audit.slice(-8).reverse());

	function selectEvent(id: string) {
		// Re-selecting the selected event must not reset its band + audit
		// tail (selectEvent clears both and re-pushes the audit rows).
		if (ops.selectedEventId !== id) ops.selectEvent(id);
		focus.request(id); // the canvas flies, locks when it lands, then reports back
	}

	/** Honest per-row focus state: LOCKED only once the canvas holds the camera. */
	function focusState(id: string): { label: string; tone: 'ok' | 'wait' | 'warn' } | null {
		if (godsEye.subjectLockId === id) return { label: '⊕ locked', tone: 'ok' };
		if (focus.eventId !== id) return null;
		if (focus.outcome === 'pending') return { label: 'locating…', tone: 'wait' };
		if (focus.outcome === 'no-geometry') return { label: 'no spine on canvas', tone: 'warn' };
		if (focus.outcome === 'no-canvas') return { label: 'canvas unavailable', tone: 'warn' };
		return null; // locked-then-released, or cancelled by the operator
	}

	/** The actor as the narrow rail shows it (mcp: prefix dropped, long officer labels cut; the text names them in full). */
	function railActor(actor: string): string {
		const a = actor.startsWith('mcp:') ? actor.slice(4) : actor;
		return a.length > 26 ? `${a.slice(0, 25)}…` : a;
	}

	function actorKind(a: AuditEntry): 'human' | 'machine' {
		// Server rows (C-C FeedItem) say who acted; only legacy rows are inferred.
		if (a.actorKind) return a.actorKind === 'human' ? 'human' : 'machine';
		if (a.source === 'operator' || a.actor === 'operator') return 'human';
		if (a.actor.includes('Officer') || a.actor.includes('officer')) return 'human';
		return 'machine';
	}
</script>

<div class="rail">
	<Panel variant="glass" title={t(ops.lang, 'tpl_corridor')} lang={ops.lang}>
		{#if corridor.row && corridor.noLiveFeed}
			<p class="no-feed mono" data-testid="liverail-no-live-feed">{t(ops.lang, 'tpl_no_live_feed')}</p>
			<p class="dim mono">the corridor summary has no fresh observation behind it — readouts withheld</p>
		{:else if corridor.row}
			{#if corridor.simulated}
				<p class="sim mono" data-testid="liverail-simulated-feed">{t(ops.lang, 'cc_simulated_feed')}</p>
			{/if}
			<div class="tiles">
				<span class="tile">
					<b>{corridor.row.avgSpeedKmh.toFixed(1)}</b>
					<i>km/h avg</i>
				</span>
				<span class="tile">
					<b>{corridor.row.avgCongestionIdx.toFixed(2)}</b>
					<i>cgx</i>
				</span>
				<span class="tile">
					<b>{corridor.row.congestedSegments}</b>
					<i>congested</i>
				</span>
				<span class="tile">
					<b>{corridor.row.totalSegments}</b>
					<i>segments</i>
				</span>
			</div>
		{:else}
			<p class="dim mono">— corridor feed —</p>
		{/if}
	</Panel>

	<Panel variant="glass" title={t(ops.lang, 'tpl_events')} lang={ops.lang} scroll>
		{#if liveEvents.length === 0}
			<EmptyState title="no live corridor events" hint="inject one from /ops · emulator panel" />
		{:else}
			<ul class="events">
				{#each liveEvents as ev (ev.eventId)}
					<li>
						<button
							type="button"
							class="event"
							class:selected={ops.selectedEventId === ev.eventId}
							data-event-id={ev.eventId}
							onclick={() => selectEvent(ev.eventId)}
						>
							<span class="ev-name" title={displayText(ev.name, ops.officerOn)}>{displayText(ev.name, ops.officerOn)}</span>
							<span class="ev-kind mono">
								{ev.eventKind}
								<!-- on the kind line, never inside the cut name: the marker always shows -->
								{#if ev.emulated}<span class="ev-emu" data-testid="liverail-emulated">EMULATED</span>{/if}
								{#if ev.metadata?.label}
									<span
										class="ev-test"
										data-testid="liverail-test"
										title={`${t(ops.lang, 'tpl_test_hint')}: ${displayText(ev.metadata.label, ops.officerOn)}`}
									>{t(ops.lang, 'tpl_test')}</span>
								{/if}
								{#if focusState(ev.eventId)}
									{@const fs = focusState(ev.eventId)!}
									<span
										class="ev-focus tone-{fs.tone}"
										title={focus.eventId === ev.eventId && focus.note ? displayText(focus.note, ops.officerOn) : undefined}
									>
										· {fs.label}
									</span>
								{/if}
							</span>
							<Badge variant={ev.state === 'active' ? 'live' : 'pending'} label={ev.state} />
						</button>
					</li>
				{/each}
			</ul>
		{/if}
	</Panel>

	<Panel variant="glass" title={t(ops.lang, 'tpl_audit')} lang={ops.lang} scroll>
		{#if auditTail.length === 0}
			<EmptyState title={t(ops.lang, 'auditEmpty')} />
		{:else}
			<div class="audit">
				{#each auditTail as entry (entry.id)}
					<FeedItem
						ts={entry.ts}
						actor={railActor(entry.actor)}
						actorKind={actorKind(entry)}
						level={entry.level}
						text={displayText(entry.text, ops.officerOn)}
						detail={displayDetail(entry.detail, ops.officerOn)}
						lang={ops.lang}
					/>
				{/each}
			</div>
		{/if}
	</Panel>
</div>

<style>
	.rail {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		min-width: 0;
	}

	.tiles {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--v-space-2);
	}

	.tile {
		display: flex;
		flex-direction: column;
		gap: 1px;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2);
		background: var(--v-surface-1);
	}

	.tile b {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-md);
		font-weight: 700;
		color: var(--v-ink-1);
		font-variant-numeric: tabular-nums;
	}

	.tile i {
		font-style: normal;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.dim {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.no-feed {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-sm);
		font-weight: 700;
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
	}

	.sim {
		display: inline-block;
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.08em;
		padding: 1px 6px;
		border: 1px dashed var(--v-tool);
		border-radius: var(--v-radius-pill);
		color: var(--v-tool);
	}

	.events {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.event {
		display: grid;
		grid-template-columns: 1fr auto;
		grid-template-areas: 'name badge' 'kind badge';
		align-items: center;
		column-gap: var(--v-space-2);
		width: 100%;
		padding: var(--v-space-1) var(--v-space-2);
		background: transparent;
		border: 1px solid transparent;
		border-left: 3px solid transparent;
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-2);
		cursor: pointer;
		text-align: left;
		transition:
			border-color var(--v-dur-out) var(--v-ease),
			background var(--v-dur-out) var(--v-ease),
			transform var(--v-dur-out) var(--v-ease);
	}

	.event:hover {
		background: var(--v-act-veil);
		border-left-color: var(--v-act);
		transform: translateX(2px);
	}

	.event.selected {
		border-left-color: var(--v-act);
		background: var(--v-act-veil);
	}

	.ev-name {
		grid-area: name;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ev-kind {
		grid-area: kind;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: 0.04em;
	}

	.ev-emu {
		margin-left: var(--v-space-1);
		padding: 0 5px;
		border: 1px dashed var(--v-tool);
		border-radius: var(--v-radius-pill);
		color: var(--v-tool);
		letter-spacing: 0.08em;
	}

	.ev-test {
		margin-left: var(--v-space-1);
		padding: 0 5px;
		border: 1px solid var(--v-band-amber);
		border-radius: var(--v-radius-pill);
		color: var(--v-band-amber);
		letter-spacing: 0.08em;
	}

	.ev-focus {
		letter-spacing: 0.04em;
	}

	.ev-focus.tone-ok {
		color: var(--v-data);
	}

	.ev-focus.tone-wait {
		color: var(--v-ink-3);
	}

	.ev-focus.tone-warn {
		color: var(--v-band-amber);
	}

	.audit {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
