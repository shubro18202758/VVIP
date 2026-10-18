<script lang="ts">
	/**
	 * LiveRail — the template's right operations rail, rebuilt on real
	 * feeds: corridor readouts (shared feed), the live event board
	 * (click = select + camera subject-lock, the wall-mode contract), and
	 * the decision-audit tail (the same FeedItem contract as /ops).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { godsEye } from '$lib/gods-eye/store.svelte';
	import { corridor } from './corridor.svelte';
	import { t } from '$lib/i18n/strings';
	import type { AuditEntry } from '$lib/ops/types';

	const liveEvents = $derived(ops.events.filter((e) => e.source !== 'offline-fixture'));
	const auditTail = $derived(ops.audit.slice(-8).reverse());

	function selectEvent(id: string) {
		ops.selectEvent(id);
		godsEye.subjectLockId = id; // wall camera follows the selected event
	}

	function actorKind(a: AuditEntry): 'human' | 'machine' {
		if (a.source === 'operator' || a.actor === 'operator') return 'human';
		if (a.actor.includes('Officer') || a.actor.includes('officer')) return 'human';
		return 'machine';
	}
</script>

<div class="rail">
	<Panel variant="glass" title={t(ops.lang, 'tpl_corridor')} lang={ops.lang}>
		{#if corridor.row}
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
							onclick={() => selectEvent(ev.eventId)}
						>
							<span class="ev-name">{ev.name}</span>
							<span class="ev-kind mono">{ev.eventKind}</span>
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
						actor={entry.actor.startsWith('mcp:') ? entry.actor.slice(4) : entry.actor}
						actorKind={actorKind(entry)}
						level={entry.level}
						text={entry.text}
						detail={entry.detail}
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
		font-size: 9px;
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-4);
	}

	.dim {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
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
		font-size: 9px;
		color: var(--v-ink-4);
		letter-spacing: 0.04em;
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
