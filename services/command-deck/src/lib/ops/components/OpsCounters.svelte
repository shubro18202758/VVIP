<script lang="ts">
	/**
	 * OpsCounters — the commander's four DataTiles: active events, pending
	 * human decisions, units dispatched, ambulance windows granted. Each
	 * carries its own live/fallback honesty chip (the store's counters are
	 * live-derived; the tile says which feed answered).
	 */
	import DataTile from '$lib/primitives/DataTile.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';

	const lang = $derived(ops.lang);
	const c = $derived(ops.counters);

	const boardLive = $derived(ops.boardStatus !== 'offline');
	const queueLive = $derived(ops.recommendationsLive !== false && ops.dispatchesLive !== false);
</script>

<div class="counters" role="group" aria-label={tOps(lang, 'mastheadPosture')}>
	<DataTile
		label={tOps(lang, 'counterActiveEvents')}
		value={c.activeEvents}
		source={{ variant: boardLive ? 'live' : 'offline', label: boardLive ? tOps(lang, 'labelLive') : tOps(lang, 'labelOffline') }}
	/>
	<DataTile
		label={tOps(lang, 'counterPendingDecisions')}
		value={c.pendingDecisions}
		source={{ variant: queueLive ? 'live' : 'stale', label: queueLive ? tOps(lang, 'labelLive') : tOps(lang, 'labelStale') }}
	/>
	<DataTile
		label={tOps(lang, 'counterUnitsDispatched')}
		value={c.unitsDispatched}
		source={{ variant: ops.dispatchesLive === false ? 'offline' : 'live', label: ops.dispatchesLive === false ? tOps(lang, 'labelOffline') : tOps(lang, 'labelLive') }}
	/>
	<DataTile
		label={tOps(lang, 'counterAmbulanceWindows')}
		value={c.ambulanceWindows}
		source={{ variant: 'live', label: tOps(lang, 'labelLive') }}
	/>
</div>

<style>
	.counters {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: var(--v-gap-panel);
	}

	@media (max-width: 1100px) {
		.counters {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
