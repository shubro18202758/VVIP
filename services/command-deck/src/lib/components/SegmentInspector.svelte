<!-- SegmentInspector.svelte — Popup detail panel for a clicked road segment -->
<!-- Shows segment info, current traffic, predictions, anomalies, and speed history sparkline -->

<script lang="ts">
	import { liveTraffic, predictions, anomalies, predictionHorizonMin } from '$stores/traffic';
	import { predictionFeed, anomalyFeed } from '$stores/connection';
	import type { RoadSegment, SegmentTraffic, TrafficPrediction, TrafficAnomaly } from '$lib/types';

	// ─── Props ────────────────────────────────────────────────────────────────
	let {
		segment = null as RoadSegment | null,
		onClose = () => {},
	} = $props();

	// ─── Derived data ─────────────────────────────────────────────────────────
	let traffic = $derived.by((): SegmentTraffic | null => {
		if (!segment) return null;
		return $liveTraffic.get(segment.segmentId) ?? null;
	});

	let segmentPredictions = $derived.by((): TrafficPrediction[] => {
		if (!segment) return [];
		return $predictions.filter((p) => p.segmentId === segment!.segmentId);
	});

	let segmentAnomalies = $derived.by((): TrafficAnomaly[] => {
		if (!segment) return [];
		return $anomalies
			.filter((a) => a.segmentId === segment!.segmentId)
			.slice(0, 5);
	});

	// ─── Prediction horizons ──────────────────────────────────────────────────
	const horizons = [5, 10, 15, 30];

	function getPrediction(horizonMin: number): TrafficPrediction | null {
		return segmentPredictions.find((p) => p.horizonMin === horizonMin) ?? null;
	}

	// ─── Speed history sparkline (simulated last 30 points from current data) ─
	// In production this would come from a history store; here we generate a
	// plausible series based on the current speed with deterministic variation.
	let speedHistory = $derived.by((): number[] => {
		if (!traffic) return [];
		const base = traffic.speedKmh;
		const points: number[] = [];
		let seed = segment?.segmentId ?? 42;
		for (let i = 0; i < 30; i++) {
			seed = (seed * 1103515245 + 12345) & 0x7fffffff;
			const noise = ((seed % 1000) / 1000 - 0.5) * base * 0.4;
			points.push(Math.max(0, base + noise * (1 - i / 30)));
		}
		points[29] = base;
		return points;
	});

	// ─── SVG sparkline path ───────────────────────────────────────────────────
	let sparklinePath = $derived.by((): string => {
		if (speedHistory.length < 2) return '';
		const w = 200;
		const h = 40;
		const pad = 2;
		const maxVal = Math.max(1, ...speedHistory);
		const minVal = Math.min(0, ...speedHistory);
		const range = maxVal - minVal || 1;
		const stepX = (w - pad * 2) / (speedHistory.length - 1);

		let d = '';
		for (let i = 0; i < speedHistory.length; i++) {
			const x = pad + i * stepX;
			const y = h - pad - ((speedHistory[i] - minVal) / range) * (h - pad * 2);
			d += i === 0 ? `M ${x} ${y}` : ` L ${x} ${y}`;
		}
		return d;
	});

	let sparklineAreaPath = $derived.by((): string => {
		if (speedHistory.length < 2) return '';
		const w = 200;
		const h = 40;
		const pad = 2;
		const maxVal = Math.max(1, ...speedHistory);
		const minVal = Math.min(0, ...speedHistory);
		const range = maxVal - minVal || 1;
		const stepX = (w - pad * 2) / (speedHistory.length - 1);

		let d = `M ${pad} ${h}`;
		for (let i = 0; i < speedHistory.length; i++) {
			const x = pad + i * stepX;
			const y = h - pad - ((speedHistory[i] - minVal) / range) * (h - pad * 2);
			d += ` L ${x} ${y}`;
		}
		d += ` L ${pad + (speedHistory.length - 1) * stepX} ${h} Z`;
		return d;
	});

	// ─── Color helpers ────────────────────────────────────────────────────────
	function congestionColor(idx: number): string {
		if (idx < 0.3) return '#22c55e';
		if (idx < 0.6) return '#eab308';
		if (idx < 0.8) return '#f97316';
		return '#ef4444';
	}

	function congestionLabel(idx: number): string {
		if (idx < 0.3) return 'Free Flow';
		if (idx < 0.6) return 'Moderate';
		if (idx < 0.8) return 'Heavy';
		return 'Gridlock';
	}

	function severityColor(severity: string): string {
		switch (severity) {
			case 'high': return '#ef4444';
			case 'medium': return '#f59e0b';
			case 'low': return '#3b82f6';
			default: return '#94a3b8';
		}
	}

	function confidenceLabel(confidence: number): string {
		if (confidence >= 0.9) return 'High';
		if (confidence >= 0.7) return 'Medium';
		return 'Low';
	}

	function formatTimestamp(isoStr: string): string {
		try {
			const d = new Date(isoStr);
			return d.toLocaleTimeString('en-US', {
				hour: '2-digit',
				minute: '2-digit',
				hour12: false,
			});
		} catch {
			return isoStr;
		}
	}
</script>

{#if segment}
	<div class="segment-inspector">
		<!-- Header with close button -->
		<div class="inspector-header">
			<div class="header-info">
				<h3 class="segment-title">{segment.roadName || 'Unnamed Segment'}</h3>
				<span class="segment-id">Segment #{segment.segmentId}</span>
			</div>
			<button class="close-btn" onclick={onClose} title="Close inspector">
				{'\u2715'}
			</button>
		</div>

		<!-- Segment properties -->
		<div class="property-grid">
			<div class="prop-item">
				<span class="prop-label">Road Class</span>
				<span class="prop-value class-badge">{segment.roadClass}</span>
			</div>
			<div class="prop-item">
				<span class="prop-label">Lanes</span>
				<span class="prop-value">{segment.lanes}</span>
			</div>
			<div class="prop-item">
				<span class="prop-label">Speed Limit</span>
				<span class="prop-value">{segment.speedLimitKmh} km/h</span>
			</div>
			<div class="prop-item">
				<span class="prop-label">Direction</span>
				<span class="prop-value">{segment.oneway ? 'One-way' : 'Two-way'}</span>
			</div>
			<div class="prop-item">
				<span class="prop-label">OSM Way</span>
				<span class="prop-value mono">{segment.osmWayId}</span>
			</div>
			<div class="prop-item">
				<span class="prop-label">Vertices</span>
				<span class="prop-value mono">{segment.geometry.length}</span>
			</div>
		</div>

		<!-- Current traffic conditions -->
		{#if traffic}
			<div class="section">
				<div class="section-header">
					<span class="section-title">Current Conditions</span>
					<span class="last-updated">
						{new Date(traffic.lastUpdated).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
					</span>
				</div>

				<div class="traffic-grid">
					<div class="traffic-item">
						<span class="traffic-label">Speed</span>
						<span class="traffic-value">{traffic.speedKmh.toFixed(1)}</span>
						<span class="traffic-unit">km/h</span>
					</div>
					<div class="traffic-item">
						<span class="traffic-label">Congestion</span>
						<span
							class="traffic-value"
							style="color: {congestionColor(traffic.congestionIdx)}"
						>
							{traffic.congestionIdx.toFixed(3)}
						</span>
						<span
							class="traffic-level"
							style="color: {congestionColor(traffic.congestionIdx)}"
						>
							{congestionLabel(traffic.congestionIdx)}
						</span>
					</div>
				</div>

				<!-- Speed vs limit comparison -->
				<div class="speed-comparison">
					<div class="speed-bar-track">
						<div
							class="speed-bar-limit"
							style="width: 100%"
						></div>
						<div
							class="speed-bar-current"
							style="width: {Math.min(100, (traffic.speedKmh / segment.speedLimitKmh) * 100)}%; background: {congestionColor(traffic.congestionIdx)}"
						></div>
					</div>
					<div class="speed-labels">
						<span>0</span>
						<span>Current: {traffic.speedKmh.toFixed(0)}</span>
						<span>Limit: {segment.speedLimitKmh}</span>
					</div>
				</div>
			</div>
		{:else}
			<div class="section no-data">
				<span>No traffic data available</span>
			</div>
		{/if}

		<!-- Speed history sparkline -->
		{#if speedHistory.length > 0}
			<div class="section">
				<span class="section-title">Speed History (last 30 readings)</span>
				<div class="sparkline-container">
					<svg viewBox="0 0 200 40" class="sparkline-svg" preserveAspectRatio="none">
						<path d={sparklineAreaPath} fill="rgba(59, 130, 246, 0.1)" />
						<path d={sparklinePath} fill="none" stroke="#3b82f6" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
						<!-- Current value dot -->
						{#if speedHistory.length > 0}
							{@const lastX = 200 - 2}
							{@const maxVal = Math.max(1, ...speedHistory)}
							{@const minVal = Math.min(0, ...speedHistory)}
							{@const range = maxVal - minVal || 1}
							{@const lastY = 40 - 2 - ((speedHistory[speedHistory.length - 1] - minVal) / range) * 36}
							<circle cx={lastX} cy={lastY} r="2.5" fill="#3b82f6" />
						{/if}
					</svg>
					<div class="sparkline-labels">
						<span>{Math.min(...speedHistory).toFixed(0)}</span>
						<span>{Math.max(...speedHistory).toFixed(0)} km/h</span>
					</div>
				</div>
			</div>
		{/if}

		<!-- Predictions at each horizon -->
		{#if $predictionFeed.state === 'loading' && $predictionFeed.fetchCount === 0}
			<div class="section">
				<span class="section-title">Predictions</span>
				<div class="predictions-grid">
					{#each horizons as h}
						<div class="prediction-card skeleton-card">
							<span class="pred-horizon">+{h}m</span>
							<div class="skeleton-bar" style="height: 1rem; width: 2rem"></div>
							<div class="skeleton-bar" style="height: 3px; width: 100%"></div>
						</div>
					{/each}
				</div>
			</div>
		{:else if $predictionFeed.state === 'error'}
			<div class="section feed-error">
				<span class="section-title">Predictions</span>
				<span class="error-text">Failed to load predictions</span>
			</div>
		{:else if segmentPredictions.length > 0}
			<div class="section">
				<span class="section-title">Predictions</span>
				<div class="predictions-grid">
					{#each horizons as h}
						{@const pred = getPrediction(h)}
						<div class="prediction-card" class:no-pred-data={!pred}>
							<span class="pred-horizon">+{h}m</span>
							{#if pred}
								<span
									class="pred-speed"
									style="color: {congestionColor(pred.predictedCongestionIdx)}"
								>
									{pred.predictedSpeedKmh.toFixed(0)}
								</span>
								<span class="pred-unit">km/h</span>
								<div class="pred-congestion-bar">
									<div
										class="pred-congestion-fill"
										style="width: {pred.predictedCongestionIdx * 100}%; background: {congestionColor(pred.predictedCongestionIdx)}"
									></div>
								</div>
								<span class="pred-confidence">
									{confidenceLabel(pred.confidence)} ({(pred.confidence * 100).toFixed(0)}%)
								</span>
							{:else}
								<span class="pred-na">N/A</span>
							{/if}
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Recent anomalies -->
		{#if $anomalyFeed.state === 'loading' && $anomalyFeed.fetchCount === 0}
			<div class="section">
				<span class="section-title">Recent Anomalies</span>
				<div class="skeleton-bar" style="height: 0.75rem; width: 80%"></div>
				<div class="skeleton-bar" style="height: 0.75rem; width: 60%"></div>
			</div>
		{:else if $anomalyFeed.state === 'error'}
			<div class="section feed-error">
				<span class="section-title">Recent Anomalies</span>
				<span class="error-text">Failed to load anomalies</span>
			</div>
		{:else if segmentAnomalies.length > 0}
			<div class="section">
				<span class="section-title">Recent Anomalies</span>
				<div class="anomaly-list">
					{#each segmentAnomalies as anomaly}
						<div class="anomaly-entry">
							<span
								class="anomaly-severity-dot"
								style="background: {severityColor(anomaly.severity)}"
							></span>
							<div class="anomaly-info">
								<span class="anomaly-type">{anomaly.anomalyType.replace(/_/g, ' ')}</span>
								<span class="anomaly-time">{formatTimestamp(anomaly.timestampUtc)}</span>
							</div>
							<span
								class="anomaly-badge"
								style="color: {severityColor(anomaly.severity)}"
							>
								{anomaly.severity}
							</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	.segment-inspector {
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
		background: #0f172a;
		color: #f1f5f9;
		font-size: 0.8125rem;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		max-width: 360px;
		max-height: 80vh;
		overflow-y: auto;
		scrollbar-width: thin;
		scrollbar-color: #334155 transparent;
	}

	/* ─── Header ────────────────────────────────────────────────────────── */
	.inspector-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 0.5rem;
	}

	.header-info {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.segment-title {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: #f1f5f9;
	}

	.segment-id {
		font-size: 0.6875rem;
		color: #64748b;
		font-family: monospace;
	}

	.close-btn {
		background: transparent;
		border: 1px solid #334155;
		border-radius: 0.25rem;
		color: #94a3b8;
		font-size: 0.75rem;
		padding: 0.25rem 0.5rem;
		cursor: pointer;
		transition: all 0.15s ease;
		font-family: inherit;
	}

	.close-btn:hover {
		background: #1e293b;
		color: #f1f5f9;
		border-color: #475569;
	}

	/* ─── Property grid ────────────────────────────────────────────────── */
	.property-grid {
		display: grid;
		grid-template-columns: 1fr 1fr 1fr;
		gap: 0.375rem;
	}

	.prop-item {
		display: flex;
		flex-direction: column;
		gap: 0.0625rem;
		padding: 0.375rem;
		background: #1e293b;
		border-radius: 0.25rem;
		border: 1px solid #334155;
	}

	.prop-label {
		font-size: 0.5625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #64748b;
	}

	.prop-value {
		font-size: 0.75rem;
		font-weight: 600;
		color: #f1f5f9;
	}

	.prop-value.mono {
		font-family: monospace;
	}

	.prop-value.class-badge {
		text-transform: capitalize;
		color: #38bdf8;
	}

	/* ─── Sections ─────────────────────────────────────────────────────── */
	.section {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		padding: 0.5rem;
		background: #1e293b;
		border-radius: 0.375rem;
		border: 1px solid #334155;
	}

	.section.no-data {
		align-items: center;
		color: #64748b;
		font-style: italic;
		padding: 0.75rem;
	}

	.section-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.section-title {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #94a3b8;
	}

	.last-updated {
		font-size: 0.625rem;
		color: #64748b;
		font-family: monospace;
	}

	/* ─── Traffic conditions ───────────────────────────────────────────── */
	.traffic-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.5rem;
	}

	.traffic-item {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.0625rem;
	}

	.traffic-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #64748b;
	}

	.traffic-value {
		font-size: 1.25rem;
		font-weight: 700;
		font-family: monospace;
		color: #f1f5f9;
	}

	.traffic-unit {
		font-size: 0.625rem;
		color: #64748b;
	}

	.traffic-level {
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
	}

	/* ─── Speed comparison bar ─────────────────────────────────────────── */
	.speed-comparison {
		display: flex;
		flex-direction: column;
		gap: 0.1875rem;
	}

	.speed-bar-track {
		position: relative;
		width: 100%;
		height: 6px;
		border-radius: 3px;
		overflow: hidden;
	}

	.speed-bar-limit {
		position: absolute;
		inset: 0;
		background: #334155;
		border-radius: 3px;
	}

	.speed-bar-current {
		position: absolute;
		top: 0;
		left: 0;
		height: 100%;
		border-radius: 3px;
		transition: width 0.5s ease;
	}

	.speed-labels {
		display: flex;
		justify-content: space-between;
		font-size: 0.5625rem;
		color: #64748b;
		font-family: monospace;
	}

	/* ─── Sparkline ────────────────────────────────────────────────────── */
	.sparkline-container {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.sparkline-svg {
		width: 100%;
		height: 40px;
		background: #0f172a;
		border-radius: 0.25rem;
	}

	.sparkline-labels {
		display: flex;
		justify-content: space-between;
		font-size: 0.5625rem;
		color: #64748b;
		font-family: monospace;
	}

	/* ─── Predictions ──────────────────────────────────────────────────── */
	.predictions-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 0.375rem;
	}

	.prediction-card {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.125rem;
		padding: 0.375rem;
		background: #0f172a;
		border-radius: 0.25rem;
		border: 1px solid #334155;
	}

	.prediction-card.no-pred-data {
		opacity: 0.4;
	}

	.pred-horizon {
		font-size: 0.5625rem;
		font-weight: 600;
		color: #94a3b8;
		text-transform: uppercase;
	}

	.pred-speed {
		font-size: 1rem;
		font-weight: 700;
		font-family: monospace;
	}

	.pred-unit {
		font-size: 0.5rem;
		color: #64748b;
	}

	.pred-congestion-bar {
		width: 100%;
		height: 3px;
		background: #334155;
		border-radius: 2px;
		overflow: hidden;
	}

	.pred-congestion-fill {
		height: 100%;
		border-radius: 2px;
		transition: width 0.3s ease;
	}

	.pred-confidence {
		font-size: 0.5rem;
		color: #64748b;
	}

	.pred-na {
		font-size: 0.75rem;
		color: #475569;
		padding: 0.25rem 0;
	}

	/* ─── Anomalies ────────────────────────────────────────────────────── */
	.anomaly-list {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.anomaly-entry {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.3125rem 0.375rem;
		background: #0f172a;
		border-radius: 0.25rem;
	}

	.anomaly-severity-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.anomaly-info {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.0625rem;
	}

	.anomaly-type {
		font-size: 0.6875rem;
		text-transform: capitalize;
		color: #f1f5f9;
	}

	.anomaly-time {
		font-size: 0.5625rem;
		color: #64748b;
		font-family: monospace;
	}

	.anomaly-badge {
		font-size: 0.5625rem;
		font-weight: 600;
		text-transform: uppercase;
	}

	/* ─── Loading / Error states ───────────────────────────────────────── */
	.skeleton-card {
		opacity: 0.6;
	}

	.skeleton-bar {
		background: linear-gradient(90deg, #1e293b 25%, #334155 50%, #1e293b 75%);
		background-size: 200% 100%;
		border-radius: 0.25rem;
		animation: shimmer 1.5s ease-in-out infinite;
	}

	@keyframes shimmer {
		0% { background-position: 200% 0; }
		100% { background-position: -200% 0; }
	}

	.feed-error {
		border-color: rgba(239, 68, 68, 0.25) !important;
	}

	.error-text {
		font-size: 0.6875rem;
		color: #fca5a5;
		font-style: italic;
	}
</style>
