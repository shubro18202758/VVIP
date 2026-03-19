<script lang="ts">
	import CorridorMap from '$components/CorridorMap.svelte';
	import SimulationViewer from '$components/SimulationViewer.svelte';
	import { simulationConfig, simulationState, isRunning, formattedTime, simulationProgress } from '$stores/simulation';
	import { selectedRoute, diversions } from '$stores/convoy';
</script>

<div class="simulation-page">
	<div class="sim-map">
		<CorridorMap zoom={14} />
		<!-- Simulation canvas overlays on top in the map component -->
	</div>

	<aside class="sim-sidebar">
		<SimulationViewer />

		{#if $selectedRoute}
			<div class="config-card">
				<h3>Simulation Source</h3>
				<div class="cfg-row">
					<span>Route:</span>
					<span class="mono">{$selectedRoute.routeId.slice(0, 8)}...</span>
				</div>
				<div class="cfg-row">
					<span>Segments:</span>
					<span class="mono">{$selectedRoute.segmentIds.length}</span>
				</div>
				<div class="cfg-row">
					<span>Distance:</span>
					<span class="mono">{($selectedRoute.totalDistanceM / 1000).toFixed(1)} km</span>
				</div>
				<div class="cfg-row">
					<span>Diversions:</span>
					<span class="mono">{$diversions.length}</span>
				</div>
			</div>
		{:else}
			<div class="config-card">
				<p class="hint">Select a route in <a href="/planning">Route Planning</a> to configure simulation.</p>
			</div>
		{/if}

		{#if $isRunning || $simulationState.currentTimeSec > 0}
			<div class="results-card">
				<h3>Results Summary</h3>
				<div class="result-grid">
					<div class="result-item">
						<span class="result-value">{$simulationState.totalVehicles}</span>
						<span class="result-label">Total Vehicles</span>
					</div>
					<div class="result-item">
						<span class="result-value">{$simulationState.avgSpeedKmh.toFixed(1)}</span>
						<span class="result-label">Avg Speed (km/h)</span>
					</div>
					<div class="result-item">
						<span class="result-value">{$simulationState.avgDelayPerVehicleSec.toFixed(1)}s</span>
						<span class="result-label">Avg Delay</span>
					</div>
					<div class="result-item">
						<span class="result-value">{$simulationState.queueLengthM.toFixed(0)}m</span>
						<span class="result-label">Max Queue</span>
					</div>
				</div>
			</div>
		{/if}
	</aside>
</div>

<style>
	.simulation-page {
		display: grid;
		grid-template-columns: 1fr 380px;
		gap: 1rem;
		flex: 1;
		min-height: 0;
	}

	.sim-map {
		border-radius: 0.5rem;
		overflow: hidden;
		position: relative;
	}

	.sim-sidebar {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		overflow-y: auto;
	}

	.config-card, .results-card {
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		padding: 1rem;
	}

	.config-card h3, .results-card h3 {
		font-size: 0.85rem;
		color: #94a3b8;
		margin: 0 0 0.5rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.cfg-row {
		display: flex;
		justify-content: space-between;
		font-size: 0.85rem;
		color: #94a3b8;
		padding: 0.2rem 0;
	}

	.mono { font-family: monospace; color: #f1f5f9; }

	.hint {
		color: #64748b;
		font-size: 0.85rem;
		font-style: italic;
		margin: 0;
	}

	.hint a { color: #3b82f6; text-decoration: none; }

	.result-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
	}

	.result-item {
		text-align: center;
		padding: 0.5rem;
		background: #0f172a;
		border-radius: 0.375rem;
	}

	.result-value {
		display: block;
		font-size: 1.2rem;
		font-weight: 700;
		font-family: monospace;
		color: #f1f5f9;
	}

	.result-label {
		font-size: 0.65rem;
		text-transform: uppercase;
		color: #64748b;
		letter-spacing: 0.05em;
	}
</style>
