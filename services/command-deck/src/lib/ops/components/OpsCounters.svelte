<script lang="ts">
	/**
	 * OpsCounters — the commander's four DataTiles: active events, pending
	 * human decisions, units dispatched, ambulance windows granted. Each
	 * carries its own honesty chip derived from the feed that answered
	 * (never hard-coded): the board for events, the queue for decisions, the
	 * dispatch list for units and ambulance windows. ACTIVE EVENTS follows
	 * the real lifecycle (C-F) and says how many of them are EMULATED.
	 */
	import DataTile from '$lib/primitives/DataTile.svelte';
	import type { BadgeVariant } from '$lib/primitives/Badge.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';

	const lang = $derived(ops.lang);
	const c = $derived(ops.counters);

	function chip(live: boolean | null, offline = false): { variant: BadgeVariant; label: string } {
		if (offline) return { variant: 'offline', label: tOps(lang, 'labelOffline') };
		if (live === false) return { variant: 'stale', label: tOps(lang, 'labelStale') };
		if (live === null) return { variant: 'not-deployed', label: tOps(lang, 'labelWaiting') };
		return { variant: 'live', label: tOps(lang, 'labelLive') };
	}

	const boardChip = $derived(
		ops.boardStatus === 'offline'
			? chip(null, true)
			: c.activeEvents > 0 && c.activeEmulated === c.activeEvents
				? { variant: 'emulated' as BadgeVariant, label: tOps(lang, 'labelEmulated') }
				: chip(true),
	);
	const queueChip = $derived(
		chip(ops.recommendationsLive === false || ops.dispatchesLive === false ? false : ops.recommendationsLive ?? ops.dispatchesLive),
	);
	const dispatchChip = $derived(chip(ops.dispatchesLive));
</script>

<div class="counters" role="group" aria-label={tOps(lang, 'mastheadPosture')} data-testid="ops-counters">
	<div data-counter="active-events" data-value={c.activeEvents}>
		<DataTile
			label={tOps(lang, 'counterActiveEvents')}
			value={c.activeEvents}
			unit={c.activeEmulated ? tOps(lang, 'counterEmulatedSuffix', c.activeEmulated) : undefined}
			source={boardChip}
		/>
	</div>
	<div data-counter="pending-decisions" data-value={c.pendingDecisions}>
		<DataTile label={tOps(lang, 'counterPendingDecisions')} value={c.pendingDecisions} source={queueChip} />
	</div>
	<div data-counter="units-dispatched" data-value={c.unitsDispatched}>
		<DataTile label={tOps(lang, 'counterUnitsDispatched')} value={c.unitsDispatched} source={dispatchChip} />
	</div>
	<div data-counter="ambulance-windows" data-value={c.ambulanceWindows}>
		<DataTile label={tOps(lang, 'counterAmbulanceWindows')} value={c.ambulanceWindows} source={dispatchChip} />
	</div>
</div>

<style>
	.counters {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: var(--v-gap-panel);
	}

	.counters > div {
		display: contents;
	}

	@media (max-width: 1100px) {
		.counters {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
