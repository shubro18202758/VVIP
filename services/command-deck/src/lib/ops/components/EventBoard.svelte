<script lang="ts">
	/**
	 * EventBoard — the live event board with lifecycle state. Rebuilt on the
	 * design system: per-subject BandChip (the only legal band renderer),
	 * source badges, and the fixed lifecycle verb set — planned renders
	 * hollow, active fills and carries the 8 s corridor sweep sheen,
	 * complete desaturates over 2 s and lifts toward history. Every mutable
	 * number (windows, spine ids, ages) is mono.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { tOps } from '$lib/i18n/strings.ops';
	import type { Band } from '$lib/design/tokens';
	import type { CorridorEvent, EventPhase } from '$lib/ops/types';

	const KIND_ICONS: Record<string, string> = {
		protected_movement: '⛨',
		procession: '🛕',
		mass_gathering: '🕉',
		race: '🏃',
		ceremonial: '🎖',
		egress_surge: '🏟',
		emergency_evacuation: '⛈',
		work_zone: '🚧',
		vehicle_pursuit: '➤',
	};

	function fmtWindow(phase: EventPhase): string {
		const fmt = (iso: string | null) =>
			iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
		return `${fmt(phase.startTime)} → ${phase.endTime ? fmt(phase.endTime) : 'open'}`;
	}

	function phaseLive(ev: CorridorEvent, phase: EventPhase): boolean {
		if (ev.source === 'offline-fixture') return false;
		const now = Date.now();
		const s = phase.startTime ? Date.parse(phase.startTime) : NaN;
		const e = phase.endTime ? Date.parse(phase.endTime) : Infinity;
		return Number.isFinite(s) ? now >= s && now <= e : false;
	}

	function stateVariant(state: string): 'pending' | 'live' | 'neutral' {
		if (state === 'active') return 'live';
		if (state === 'completed' || state === 'cancelled') return 'neutral';
		return 'pending';
	}

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	const lang = $derived(ops.lang);
</script>

<Panel
	title={t(lang, 'opsTitle')}
	sub={t(lang, 'opsSub')}
	status={ops.boardUpdated ? { state: ops.boardStatus === 'offline' ? 'stale' : ops.boardStatus === 'mixed' ? 'mixed' : 'live', updatedAt: ops.boardUpdated } : null}
	lang={lang}
	scroll
>
	{#if ops.boardNote}<p class="board-note mono">{ops.boardNote}</p>{/if}

	{#if ops.events.length === 0}
		<EmptyState title={t(lang, 'opsNoEvents')} />
	{:else}
		<div class="event-grid">
			{#each ops.events as ev (ev.eventId)}
				<article
					class="event-card st-{ev.state}"
					class:selected={ops.selectedEventId === ev.eventId}
					class:fixture={ev.source === 'offline-fixture'}
				>
					<div class="card-head">
						<span class="kind-icon" title={ev.eventKind}>{KIND_ICONS[ev.eventKind] ?? '◈'}</span>
						<div class="titles">
							<h3>{ev.name}</h3>
							<span class="kind-label">{t(lang, `kind_${ev.eventKind}`) !== `kind_${ev.eventKind}` ? t(lang, `kind_${ev.eventKind}`) : ev.eventKind}</span>
						</div>
						{#if ev.band}
							<BandChip band={ev.band as Band} size="sm" />
						{:else}
							<span class="band-unknown mono">band —</span>
						{/if}
					</div>

					<div class="chips">
						<Badge variant="neutral" label={ev.protectionClass} />
						<Badge variant="neutral" label={t(lang, `crowd_${ev.crowdClass}`)} />
						<Badge variant={stateVariant(ev.state)} label={ev.state} />
						<Badge
							variant={ev.source === 'live-events-api' ? 'live' : ev.source === 'live-movements-api' ? 'live' : 'fixture'}
							label={ev.source === 'live-events-api'
								? t(lang, 'opsSourceLive')
								: ev.source === 'live-movements-api'
									? t(lang, 'opsSourceMovements')
									: t(lang, 'opsSourceOffline')}
						/>
					</div>

					{#if ev.phases.length}
						<div class="phases">
							{#each ev.phases as ph, i (i)}
								<div class="phase-row" class:live={phaseLive(ev, ph)}>
									<span class="phase-dot" class:live={phaseLive(ev, ph)}></span>
									<span class="phase-label">{ph.label}</span>
									<span class="phase-window mono">{fmtWindow(ph)}</span>
									{#if ph.closurePolicy?.closureType}
										<span class="phase-policy mono">{ph.closurePolicy.closureType}{ph.closurePolicy.advanceClosureSec != null ? ` +${ph.closurePolicy.advanceClosureSec}s` : ''}</span>
									{/if}
								</div>
							{/each}
						</div>
					{/if}

					<div class="spine">
						<span class="spine-label">{t(lang, 'opsRouteSpine')}</span>
						<span class="spine-ids mono">{ev.routeSegmentIds.join(' · ')}</span>
					</div>

					<div class="row-foot">
						<button
							class="select-btn"
							class:selected={ops.selectedEventId === ev.eventId}
							type="button"
							onclick={() => ops.selectEvent(ev.eventId)}
						>
							{ops.selectedEventId === ev.eventId ? t(lang, 'opsSelected') : t(lang, 'opsSelectEvent')}
						</button>
						<span class="age mono">
							{#if ev.source !== 'offline-fixture' && ops.boardUpdated}
								{tOps(lang, 'updatedAgo', Math.max(0, Math.round((clock - ops.boardUpdated) / 1000)))}
							{/if}
						</span>
					</div>
				</article>
			{/each}
		</div>
	{/if}
</Panel>

<style>
	.board-note {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-1) var(--v-space-2);
	}

	.event-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: var(--v-gap-panel);
	}

	.event-card {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-pad-panel);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		overflow: hidden;
		min-width: 0;
	}

	.event-card.selected {
		border-color: var(--v-line-select);
	}

	.event-card.fixture {
		border-style: dashed;
	}

	/* Lifecycle verbs: planned hollow, active filled + sweep, complete lifts. */
	.event-card.st-planned {
		border-style: dashed;
		background: transparent;
	}

	.event-card.st-active {
		background: var(--v-surface-2);
	}

	.event-card.st-completed,
	.event-card.st-cancelled {
		filter: saturate(0.35);
		opacity: 0.72;
		transition:
			filter var(--v-dur-lift) var(--v-ease),
			opacity var(--v-dur-lift) var(--v-ease);
	}

	/* The 8 s corridor sweep sheen along the active card's top edge. */
	.event-card.st-active::after {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		background: linear-gradient(90deg, transparent, var(--v-act-veil), transparent);
		background-size: 40% 100%;
		background-repeat: no-repeat;
		animation: sweep 8s linear infinite;
		pointer-events: none;
	}

	@keyframes sweep {
		from {
			background-position: -40% 0;
		}
		to {
			background-position: 140% 0;
		}
	}

	.card-head {
		display: flex;
		align-items: flex-start;
		gap: var(--v-space-2);
	}

	.kind-icon {
		font-size: var(--v-text-md);
		line-height: 1;
	}

	.titles {
		flex: 1;
		min-width: 0;
	}

	.titles h3 {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.kind-label {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.band-unknown {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.phases {
		display: flex;
		flex-direction: column;
		gap: 2px;
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.phase-row {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.phase-dot {
		width: 5px;
		height: 5px;
		border-radius: var(--v-radius-pill);
		background: var(--v-line-2);
		flex-shrink: 0;
		align-self: center;
	}

	.phase-dot.live {
		background: var(--v-band-green);
	}

	.phase-label {
		color: var(--v-ink-2);
	}

	.phase-window {
		margin-left: auto;
		color: var(--v-ink-3);
	}

	.phase-policy {
		color: var(--v-ink-4);
	}

	.spine {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.spine-label {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		white-space: nowrap;
	}

	.spine-ids {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.row-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.select-btn {
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

	.select-btn:hover {
		border-color: var(--v-line-select);
		color: var(--v-ink-1);
	}

	.select-btn.selected {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.age {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	@media (prefers-reduced-motion: reduce) {
		.event-card.st-active::after {
			animation: none;
			background: var(--v-act-veil);
		}

		.event-card.st-completed,
		.event-card.st-cancelled {
			transition: none;
		}
	}
</style>
