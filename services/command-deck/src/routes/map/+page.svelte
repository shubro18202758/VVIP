<script lang="ts">
	import CorridorMap from '$components/CorridorMap.svelte';
	import TrafficHeatmap from '$components/TrafficHeatmap.svelte';
	import SegmentInspector from '$components/SegmentInspector.svelte';
	import SimulationViewer from '$components/SimulationViewer.svelte';
	import { liveTraffic } from '$stores/traffic';
	import type { RoadSegment } from '$lib/types';

	let selectedSegment = $state<RoadSegment | null>(null);
	let sidebarTab = $state<'layers' | 'inspect' | 'simulation'>('layers');

	function handleSegmentClick(segment: RoadSegment) {
		selectedSegment = segment;
		sidebarTab = 'inspect';
	}
</script>

<div class="map-page">
	<div class="map-container">
		<CorridorMap onSegmentClick={handleSegmentClick} />
		<!-- Map overlay stats -->
		<div class="map-overlay-stats">
			<span>{$liveTraffic.size.toLocaleString()} segments</span>
		</div>
	</div>
	<aside class="map-sidebar">
		<div class="sidebar-tabs">
			<button class:active={sidebarTab === 'layers'} onclick={() => sidebarTab = 'layers'}>Layers</button>
			<button class:active={sidebarTab === 'inspect'} onclick={() => sidebarTab = 'inspect'}>Inspect</button>
			<button class:active={sidebarTab === 'simulation'} onclick={() => sidebarTab = 'simulation'}>Sim</button>
		</div>

		<div class="sidebar-content">
			{#if sidebarTab === 'layers'}
				<TrafficHeatmap />
			{:else if sidebarTab === 'inspect'}
				{#if selectedSegment}
					<SegmentInspector segment={selectedSegment} />
				{:else}
					<div class="empty-inspect">
						<p>Click a road segment on the map to inspect it.</p>
					</div>
				{/if}
			{:else if sidebarTab === 'simulation'}
				<SimulationViewer />
			{/if}
		</div>
	</aside>
</div>

<style>
	.map-page {
		display: grid;
		grid-template-columns: 1fr 320px;
		flex: 1;
		min-height: 0;
		gap: 0;
	}

	.map-container {
		border-radius: 0.5rem 0 0 0.5rem;
		overflow: hidden;
		position: relative;
	}

	.map-overlay-stats {
		position: absolute;
		top: 0.75rem;
		left: 0.75rem;
		background: rgba(15, 23, 42, 0.85);
		padding: 0.35rem 0.65rem;
		border-radius: 0.375rem;
		font-size: 0.75rem;
		color: #94a3b8;
		font-family: monospace;
		pointer-events: none;
		z-index: 10;
	}

	.map-sidebar {
		background: #1e293b;
		border-left: 1px solid #334155;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.sidebar-tabs {
		display: flex;
		border-bottom: 1px solid #334155;
		flex-shrink: 0;
	}

	.sidebar-tabs button {
		flex: 1;
		padding: 0.6rem;
		background: none;
		border: none;
		color: #94a3b8;
		font-size: 0.8rem;
		font-weight: 600;
		cursor: pointer;
		border-bottom: 2px solid transparent;
		transition: color 0.15s, border-color 0.15s;
	}

	.sidebar-tabs button:hover {
		color: #f1f5f9;
	}

	.sidebar-tabs button.active {
		color: #3b82f6;
		border-bottom-color: #3b82f6;
	}

	.sidebar-content {
		flex: 1;
		overflow-y: auto;
	}

	.empty-inspect {
		padding: 2rem 1rem;
		text-align: center;
		color: #64748b;
		font-size: 0.875rem;
	}
</style>
