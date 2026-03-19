<!-- RouteComparator.svelte — Side-by-side comparison of route candidates with visual charts -->
<!-- Displays route cards with bar charts for disruption/security/time and route geometry thumbnails -->

<script lang="ts">
	import { onMount } from 'svelte';
	import { routeCandidates, selectedRouteId, selectRoute } from '$stores/convoy';
	import type { RouteCandidate } from '$lib/types';

	// ─── Derived from stores ──────────────────────────────────────────────────
	let routes = $derived($routeCandidates);
	let currentSelectedId = $derived($selectedRouteId);

	// ─── Recommendation (lowest composite score) ──────────────────────────────
	let recommendedRouteId = $derived.by(() => {
		if (routes.length === 0) return null;
		const sorted = [...routes].sort((a, b) => a.compositeScore - b.compositeScore);
		return sorted[0].routeId;
	});

	// ─── Max values for normalizing bar charts ────────────────────────────────
	let maxDisruption = $derived(Math.max(1, ...routes.map((r) => r.disruptionScore)));
	let maxSecurity = $derived(Math.max(1, ...routes.map((r) => r.securityScore)));
	let maxTime = $derived(Math.max(1, ...routes.map((r) => r.estimatedTimeSec)));
	let maxDistance = $derived(Math.max(1, ...routes.map((r) => r.totalDistanceM)));

	// ─── Canvas refs for route thumbnails ─────────────────────────────────────
	let canvasRefs = $state<Record<string, HTMLCanvasElement | null>>({});

	// ─── Draw route polyline thumbnail ────────────────────────────────────────
	function drawRouteThumbnail(canvas: HTMLCanvasElement, route: RouteCandidate, isSelected: boolean, isRecommended: boolean) {
		const ctx = canvas.getContext('2d');
		if (!ctx || !route.geometry || route.geometry.length < 2) return;

		const w = canvas.width;
		const h = canvas.height;

		ctx.clearRect(0, 0, w, h);

		// Background
		ctx.fillStyle = '#0f172a';
		ctx.fillRect(0, 0, w, h);

		// Compute bounding box
		let minLon = Infinity, maxLon = -Infinity;
		let minLat = Infinity, maxLat = -Infinity;
		for (const [lon, lat] of route.geometry) {
			minLon = Math.min(minLon, lon);
			maxLon = Math.max(maxLon, lon);
			minLat = Math.min(minLat, lat);
			maxLat = Math.max(maxLat, lat);
		}

		const pad = 6;
		const rangeX = maxLon - minLon || 0.001;
		const rangeY = maxLat - minLat || 0.001;
		const scaleX = (w - pad * 2) / rangeX;
		const scaleY = (h - pad * 2) / rangeY;
		const scale = Math.min(scaleX, scaleY);
		const offsetX = pad + ((w - pad * 2) - rangeX * scale) / 2;
		const offsetY = pad + ((h - pad * 2) - rangeY * scale) / 2;

		function project(lon: number, lat: number): [number, number] {
			return [
				offsetX + (lon - minLon) * scale,
				h - (offsetY + (lat - minLat) * scale),
			];
		}

		// Draw route polyline
		ctx.beginPath();
		ctx.strokeStyle = isSelected ? '#3b82f6' : isRecommended ? '#22c55e' : '#475569';
		ctx.lineWidth = isSelected ? 2.5 : 1.5;
		ctx.lineJoin = 'round';
		ctx.lineCap = 'round';

		const [sx, sy] = project(route.geometry[0][0], route.geometry[0][1]);
		ctx.moveTo(sx, sy);
		for (let i = 1; i < route.geometry.length; i++) {
			const [px, py] = project(route.geometry[i][0], route.geometry[i][1]);
			ctx.lineTo(px, py);
		}
		ctx.stroke();

		// Origin dot
		const [ox, oy] = project(route.geometry[0][0], route.geometry[0][1]);
		ctx.beginPath();
		ctx.fillStyle = '#3b82f6';
		ctx.arc(ox, oy, 3, 0, Math.PI * 2);
		ctx.fill();

		// Destination dot
		const lastPt = route.geometry[route.geometry.length - 1];
		const [dx, dy] = project(lastPt[0], lastPt[1]);
		ctx.beginPath();
		ctx.fillStyle = '#ef4444';
		ctx.arc(dx, dy, 3, 0, Math.PI * 2);
		ctx.fill();
	}

	// ─── Re-draw all thumbnails when routes change ────────────────────────────
	$effect(() => {
		routes;
		currentSelectedId;
		recommendedRouteId;

		// Defer to next tick so canvases are rendered
		requestAnimationFrame(() => {
			for (const route of routes) {
				const canvas = canvasRefs[route.routeId];
				if (canvas) {
					drawRouteThumbnail(
						canvas,
						route,
						route.routeId === currentSelectedId,
						route.routeId === recommendedRouteId,
					);
				}
			}
		});
	});

	// ─── Helpers ──────────────────────────────────────────────────────────────
	function formatTime(sec: number): string {
		const mins = Math.ceil(sec / 60);
		if (mins >= 60) {
			const h = Math.floor(mins / 60);
			const m = mins % 60;
			return `${h}h ${m}m`;
		}
		return `${mins} min`;
	}

	function formatDistance(meters: number): string {
		return `${(meters / 1000).toFixed(1)} km`;
	}

	function barColor(score: number, max: number, invert: boolean = false): string {
		const pct = score / max;
		if (invert) {
			// Higher is better (security)
			if (pct > 0.7) return '#22c55e';
			if (pct > 0.4) return '#eab308';
			return '#ef4444';
		}
		// Lower is better (disruption, time)
		if (pct < 0.3) return '#22c55e';
		if (pct < 0.6) return '#eab308';
		return '#ef4444';
	}
</script>

<div class="route-comparator">
	<div class="comparator-header">
		<h3 class="comparator-title">Route Candidates</h3>
		<span class="route-count">{routes.length} route{routes.length !== 1 ? 's' : ''}</span>
	</div>

	{#if routes.length === 0}
		<div class="empty-state">
			<div class="empty-icon">{'\u2B21'}</div>
			<span>No routes computed yet</span>
			<span class="empty-sub">Plan a convoy movement to generate route options</span>
		</div>
	{:else}
		<div class="route-list">
			{#each routes as route, i}
				{@const isSelected = route.routeId === currentSelectedId}
				{@const isRecommended = route.routeId === recommendedRouteId}

				<button
					class="route-card"
					class:selected={isSelected}
					class:recommended={isRecommended && !isSelected}
					onclick={() => selectRoute(route.routeId)}
				>
					<!-- Card header -->
					<div class="card-header">
						<div class="card-rank">
							<span class="rank-number">#{i + 1}</span>
							{#if isRecommended}
								<span class="recommended-badge">BEST</span>
							{/if}
						</div>
						<div class="composite-score">
							<span class="score-label">Score</span>
							<span class="score-value">{route.compositeScore.toFixed(1)}</span>
						</div>
					</div>

					<!-- Route geometry thumbnail -->
					<div class="route-thumbnail">
						<canvas
							bind:this={canvasRefs[route.routeId]}
							width="180"
							height="72"
							class="thumbnail-canvas"
						></canvas>
					</div>

					<!-- Metrics summary -->
					<div class="metrics-summary">
						<div class="metric-item">
							<span class="metric-label">Distance</span>
							<span class="metric-val">{formatDistance(route.totalDistanceM)}</span>
						</div>
						<div class="metric-item">
							<span class="metric-label">ETA</span>
							<span class="metric-val">{formatTime(route.estimatedTimeSec)}</span>
						</div>
					</div>

					<!-- Comparison bar charts -->
					<div class="chart-section">
						<!-- Disruption -->
						<div class="bar-row">
							<span class="bar-label">Disruption</span>
							<div class="bar-track">
								<div
									class="bar-fill"
									style="width: {(route.disruptionScore / maxDisruption) * 100}%; background: {barColor(route.disruptionScore, maxDisruption)}"
								></div>
							</div>
							<span class="bar-value">{route.disruptionScore.toFixed(0)}</span>
						</div>

						<!-- Security -->
						<div class="bar-row">
							<span class="bar-label">Security</span>
							<div class="bar-track">
								<div
									class="bar-fill"
									style="width: {(route.securityScore / maxSecurity) * 100}%; background: {barColor(route.securityScore, maxSecurity, true)}"
								></div>
							</div>
							<span class="bar-value">{route.securityScore.toFixed(0)}</span>
						</div>

						<!-- Time -->
						<div class="bar-row">
							<span class="bar-label">Time</span>
							<div class="bar-track">
								<div
									class="bar-fill"
									style="width: {(route.estimatedTimeSec / maxTime) * 100}%; background: {barColor(route.estimatedTimeSec, maxTime)}"
								></div>
							</div>
							<span class="bar-value">{formatTime(route.estimatedTimeSec)}</span>
						</div>

						<!-- Distance -->
						<div class="bar-row">
							<span class="bar-label">Distance</span>
							<div class="bar-track">
								<div
									class="bar-fill"
									style="width: {(route.totalDistanceM / maxDistance) * 100}%; background: {barColor(route.totalDistanceM, maxDistance)}"
								></div>
							</div>
							<span class="bar-value">{formatDistance(route.totalDistanceM)}</span>
						</div>
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.route-comparator {
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
		background: #0f172a;
		color: #f1f5f9;
		font-size: 0.8125rem;
	}

	.comparator-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.comparator-title {
		margin: 0;
		font-size: 0.875rem;
		font-weight: 600;
		color: #f1f5f9;
	}

	.route-count {
		font-size: 0.6875rem;
		color: #94a3b8;
		padding: 0.125rem 0.5rem;
		background: #1e293b;
		border-radius: 9999px;
	}

	/* ─── Empty state ──────────────────────────────────────────────────── */
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.375rem;
		padding: 1.5rem;
		color: #94a3b8;
		text-align: center;
	}

	.empty-icon {
		font-size: 1.5rem;
		opacity: 0.5;
	}

	.empty-sub {
		font-size: 0.6875rem;
		color: #64748b;
	}

	/* ─── Route list ───────────────────────────────────────────────────── */
	.route-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	/* ─── Route card ───────────────────────────────────────────────────── */
	.route-card {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.75rem;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		background: #1e293b;
		color: inherit;
		cursor: pointer;
		width: 100%;
		text-align: left;
		transition: all 0.15s ease;
		font-family: inherit;
		font-size: inherit;
	}

	.route-card:hover {
		border-color: #475569;
		background: #243047;
	}

	.route-card.selected {
		border-color: #3b82f6;
		background: #172554;
		box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.3);
	}

	.route-card.recommended {
		border-color: #166534;
		box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.15);
	}

	/* ─── Card header ──────────────────────────────────────────────────── */
	.card-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.card-rank {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.rank-number {
		font-size: 1.125rem;
		font-weight: 700;
		color: #f1f5f9;
		min-width: 1.75rem;
	}

	.recommended-badge {
		padding: 0.1rem 0.4rem;
		border-radius: 0.2rem;
		background: rgba(34, 197, 94, 0.2);
		color: #22c55e;
		font-size: 0.5625rem;
		font-weight: 700;
		letter-spacing: 0.05em;
	}

	.composite-score {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
	}

	.score-label {
		font-size: 0.5625rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #64748b;
	}

	.score-value {
		font-size: 1.25rem;
		font-weight: 700;
		color: #22c55e;
		font-family: monospace;
	}

	/* ─── Route thumbnail ──────────────────────────────────────────────── */
	.route-thumbnail {
		overflow: hidden;
		border-radius: 0.25rem;
		border: 1px solid #334155;
	}

	.thumbnail-canvas {
		width: 100%;
		height: 72px;
		display: block;
		background: #0f172a;
	}

	/* ─── Metrics summary ──────────────────────────────────────────────── */
	.metrics-summary {
		display: flex;
		gap: 0.75rem;
	}

	.metric-item {
		display: flex;
		flex-direction: column;
		gap: 0.0625rem;
	}

	.metric-label {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #64748b;
	}

	.metric-val {
		font-size: 0.8125rem;
		font-weight: 600;
		font-family: monospace;
		color: #f1f5f9;
	}

	/* ─── Bar charts ───────────────────────────────────────────────────── */
	.chart-section {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	.bar-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.bar-label {
		font-size: 0.6875rem;
		color: #94a3b8;
		min-width: 4.5rem;
	}

	.bar-track {
		flex: 1;
		height: 6px;
		background: #334155;
		border-radius: 3px;
		overflow: hidden;
	}

	.bar-fill {
		height: 100%;
		border-radius: 3px;
		transition: width 0.3s ease;
		min-width: 2px;
	}

	.bar-value {
		font-size: 0.6875rem;
		font-family: monospace;
		color: #94a3b8;
		min-width: 3rem;
		text-align: right;
	}
</style>
