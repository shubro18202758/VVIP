<!-- TrafficHeatmap.svelte — Traffic visualization control panel with legend and statistics -->
<!-- Controls heatmap display, road network, anomaly markers, prediction horizon, and congestion legend -->

<script lang="ts">
	import {
		heatmapVisible,
		heatmapOpacity,
		roadNetworkVisible,
		anomalyMarkersVisible,
		predictionHorizonMin,
		corridorAvgCongestion,
		congestedSegmentCount,
		trafficArray,
		criticalAnomalies,
	} from '$stores/traffic';

	// ─── Derived display values ───────────────────────────────────────────────
	let segmentCount = $derived($trafficArray.length);
	let avgCongestion = $derived($corridorAvgCongestion);
	let congestedCount = $derived($congestedSegmentCount);
	let criticalCount = $derived($criticalAnomalies.length);

	// ─── Prediction horizon options ───────────────────────────────────────────
	const horizonOptions = [5, 10, 15, 30];

	// ─── Congestion legend color stops ────────────────────────────────────────
	const legendStops = [
		{ label: '0.0', color: 'rgb(34, 197, 94)' },
		{ label: '0.25', color: 'rgb(134, 188, 47)' },
		{ label: '0.5', color: 'rgb(234, 179, 8)' },
		{ label: '0.75', color: 'rgb(239, 90, 4)' },
		{ label: '1.0', color: 'rgb(239, 68, 68)' },
	];

	function congestionLevel(idx: number): string {
		if (idx < 0.3) return 'low';
		if (idx < 0.6) return 'moderate';
		if (idx < 0.8) return 'high';
		return 'critical';
	}
</script>

<div class="traffic-panel">
	<div class="panel-header">
		<h3 class="panel-title">Traffic Visualization</h3>
		<div class="stat-pills">
			<span class="pill">{segmentCount} segments</span>
			{#if criticalCount > 0}
				<span class="pill critical">{criticalCount} alerts</span>
			{/if}
		</div>
	</div>

	<!-- Toggle Controls -->
	<div class="control-group">
		<label class="toggle-row">
			<span class="toggle-switch">
				<input type="checkbox" bind:checked={$heatmapVisible} />
				<span class="toggle-slider"></span>
			</span>
			<span class="toggle-label">Traffic Heatmap</span>
		</label>

		{#if $heatmapVisible}
			<div class="slider-row">
				<span class="slider-label">Opacity</span>
				<input
					type="range"
					min="0"
					max="1"
					step="0.05"
					bind:value={$heatmapOpacity}
					class="range-input"
				/>
				<span class="slider-value">{($heatmapOpacity * 100).toFixed(0)}%</span>
			</div>
		{/if}

		<label class="toggle-row">
			<span class="toggle-switch">
				<input type="checkbox" bind:checked={$roadNetworkVisible} />
				<span class="toggle-slider"></span>
			</span>
			<span class="toggle-label">Road Network</span>
		</label>

		<label class="toggle-row">
			<span class="toggle-switch">
				<input type="checkbox" bind:checked={$anomalyMarkersVisible} />
				<span class="toggle-slider"></span>
			</span>
			<span class="toggle-label">Anomaly Markers</span>
			{#if criticalCount > 0}
				<span class="anomaly-badge">{criticalCount}</span>
			{/if}
		</label>
	</div>

	<!-- Prediction Horizon Selector -->
	<div class="control-group">
		<span class="group-label">Prediction Horizon</span>
		<div class="horizon-selector">
			{#each horizonOptions as h}
				<button
					class="horizon-btn"
					class:active={$predictionHorizonMin === h}
					onclick={() => $predictionHorizonMin = h}
				>
					{h}m
				</button>
			{/each}
		</div>
	</div>

	<!-- Congestion Statistics -->
	<div class="stats-section">
		<div class="stat-row">
			<span class="stat-label">Avg Congestion</span>
			<span class="stat-value" class:low={avgCongestion < 0.3} class:moderate={avgCongestion >= 0.3 && avgCongestion < 0.6} class:high={avgCongestion >= 0.6 && avgCongestion < 0.8} class:critical={avgCongestion >= 0.8}>
				{avgCongestion.toFixed(3)}
			</span>
		</div>
		<div class="congestion-bar">
			<div
				class="congestion-fill"
				style="width: {Math.min(100, avgCongestion * 100)}%; background: {avgCongestion < 0.3 ? '#22c55e' : avgCongestion < 0.6 ? '#eab308' : avgCongestion < 0.8 ? '#f97316' : '#ef4444'}"
			></div>
		</div>
		<div class="stat-row">
			<span class="stat-label">Congested Segments</span>
			<span class="stat-value congested">{congestedCount}</span>
		</div>
		<div class="stat-row">
			<span class="stat-label">Level</span>
			<span class="stat-value level-{congestionLevel(avgCongestion)}">{congestionLevel(avgCongestion).toUpperCase()}</span>
		</div>
	</div>

	<!-- Congestion Color Legend -->
	<div class="legend-section">
		<span class="group-label">Congestion Scale</span>
		<div class="legend-gradient">
			<div class="gradient-bar"></div>
			<div class="gradient-labels">
				{#each legendStops as stop}
					<span class="gradient-label">{stop.label}</span>
				{/each}
			</div>
		</div>
		<div class="legend-items">
			<div class="legend-item">
				<span class="legend-dot" style="background: #22c55e"></span>
				<span>Free flow</span>
			</div>
			<div class="legend-item">
				<span class="legend-dot" style="background: #eab308"></span>
				<span>Moderate</span>
			</div>
			<div class="legend-item">
				<span class="legend-dot" style="background: #f97316"></span>
				<span>Heavy</span>
			</div>
			<div class="legend-item">
				<span class="legend-dot" style="background: #ef4444"></span>
				<span>Gridlock</span>
			</div>
		</div>
	</div>
</div>

<style>
	.traffic-panel {
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		background: #0f172a;
		color: #f1f5f9;
		font-size: 0.8125rem;
	}

	.panel-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}

	.panel-title {
		margin: 0;
		font-size: 0.875rem;
		font-weight: 600;
		color: #f1f5f9;
	}

	.stat-pills {
		display: flex;
		gap: 0.375rem;
	}

	.pill {
		padding: 0.125rem 0.5rem;
		border-radius: 9999px;
		background: #1e293b;
		color: #94a3b8;
		font-size: 0.6875rem;
		font-weight: 500;
	}

	.pill.critical {
		background: rgba(239, 68, 68, 0.2);
		color: #ef4444;
	}

	/* ─── Toggle controls ──────────────────────────────────────────────── */
	.control-group {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.625rem;
		background: #1e293b;
		border-radius: 0.375rem;
		border: 1px solid #334155;
	}

	.group-label {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #94a3b8;
	}

	.toggle-row {
		display: flex;
		align-items: center;
		gap: 0.625rem;
		cursor: pointer;
		user-select: none;
	}

	.toggle-switch {
		position: relative;
		width: 2rem;
		height: 1.125rem;
		flex-shrink: 0;
	}

	.toggle-switch input {
		opacity: 0;
		width: 0;
		height: 0;
		position: absolute;
	}

	.toggle-slider {
		position: absolute;
		inset: 0;
		background: #334155;
		border-radius: 9999px;
		transition: background 0.2s ease;
	}

	.toggle-slider::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 0.8125rem;
		height: 0.8125rem;
		background: #94a3b8;
		border-radius: 50%;
		transition: transform 0.2s ease, background 0.2s ease;
	}

	.toggle-switch input:checked + .toggle-slider {
		background: #1d4ed8;
	}

	.toggle-switch input:checked + .toggle-slider::after {
		transform: translateX(0.875rem);
		background: #f1f5f9;
	}

	.toggle-label {
		font-size: 0.8125rem;
		color: #f1f5f9;
		flex: 1;
	}

	.anomaly-badge {
		padding: 0.0625rem 0.375rem;
		border-radius: 9999px;
		background: rgba(239, 68, 68, 0.2);
		color: #ef4444;
		font-size: 0.6875rem;
		font-weight: 600;
	}

	/* ─── Slider ───────────────────────────────────────────────────────── */
	.slider-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding-left: 2.625rem;
	}

	.slider-label {
		font-size: 0.75rem;
		color: #94a3b8;
		min-width: 3rem;
	}

	.range-input {
		flex: 1;
		height: 4px;
		-webkit-appearance: none;
		appearance: none;
		background: #334155;
		border-radius: 2px;
		outline: none;
	}

	.range-input::-webkit-slider-thumb {
		-webkit-appearance: none;
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: #3b82f6;
		cursor: pointer;
		border: 2px solid #1e293b;
	}

	.range-input::-moz-range-thumb {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: #3b82f6;
		cursor: pointer;
		border: 2px solid #1e293b;
	}

	.slider-value {
		font-size: 0.75rem;
		color: #94a3b8;
		min-width: 2.5rem;
		text-align: right;
		font-family: monospace;
	}

	/* ─── Horizon buttons ──────────────────────────────────────────────── */
	.horizon-selector {
		display: flex;
		gap: 0.25rem;
	}

	.horizon-btn {
		flex: 1;
		padding: 0.375rem 0.5rem;
		border: 1px solid #334155;
		border-radius: 0.25rem;
		background: transparent;
		color: #94a3b8;
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.horizon-btn:hover {
		background: #334155;
		color: #f1f5f9;
	}

	.horizon-btn.active {
		background: #1d4ed8;
		border-color: #3b82f6;
		color: #f1f5f9;
	}

	/* ─── Statistics ────────────────────────────────────────────────────── */
	.stats-section {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		padding: 0.625rem;
		background: #1e293b;
		border-radius: 0.375rem;
		border: 1px solid #334155;
	}

	.stat-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.stat-label {
		font-size: 0.75rem;
		color: #94a3b8;
	}

	.stat-value {
		font-size: 0.8125rem;
		font-weight: 600;
		font-family: monospace;
		color: #f1f5f9;
	}

	.stat-value.low { color: #22c55e; }
	.stat-value.moderate { color: #eab308; }
	.stat-value.high { color: #f97316; }
	.stat-value.critical { color: #ef4444; }
	.stat-value.congested { color: #f97316; }

	.level-low { color: #22c55e; }
	.level-moderate { color: #eab308; }
	.level-high { color: #f97316; }
	.level-critical { color: #ef4444; }

	.congestion-bar {
		width: 100%;
		height: 4px;
		background: #334155;
		border-radius: 2px;
		overflow: hidden;
	}

	.congestion-fill {
		height: 100%;
		border-radius: 2px;
		transition: width 0.5s ease, background 0.5s ease;
	}

	/* ─── Legend ────────────────────────────────────────────────────────── */
	.legend-section {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.625rem;
		background: #1e293b;
		border-radius: 0.375rem;
		border: 1px solid #334155;
	}

	.legend-gradient {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.gradient-bar {
		height: 8px;
		border-radius: 4px;
		background: linear-gradient(to right, #22c55e, #86bc2f, #eab308, #f97316, #ef4444);
	}

	.gradient-labels {
		display: flex;
		justify-content: space-between;
	}

	.gradient-label {
		font-size: 0.625rem;
		color: #64748b;
		font-family: monospace;
	}

	.legend-items {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.25rem 0.75rem;
	}

	.legend-item {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.6875rem;
		color: #94a3b8;
	}

	.legend-dot {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		flex-shrink: 0;
	}
</style>
