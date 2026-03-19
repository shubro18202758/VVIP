<!-- ConvoyTracker.svelte — Real-time convoy position, status, ETA, progress, and mini-map -->
<!-- Displays convoy telemetry with route progress and active diversion count -->

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { activeConvoy, selectedRoute, activeDiversions } from '$stores/convoy';
	import type { ConvoyMovement, RouteCandidate } from '$lib/types';

	// ─── State ────────────────────────────────────────────────────────────────
	let miniMapCanvas: HTMLCanvasElement;
	let animationFrame: number | null = null;

	// ─── Derived from stores ──────────────────────────────────────────────────
	let convoy = $derived($activeConvoy);
	let route = $derived($selectedRoute);
	let diversionCount = $derived($activeDiversions.length);

	// ─── Status styling ───────────────────────────────────────────────────────
	const statusColors: Record<string, string> = {
		planning: '#3b82f6',
		approved: '#f59e0b',
		active: '#22c55e',
		completed: '#6b7280',
		cancelled: '#ef4444',
	};

	const statusIcons: Record<string, string> = {
		planning: '\u25CB',
		approved: '\u25D4',
		active: '\u25CF',
		completed: '\u2713',
		cancelled: '\u2717',
	};

	// ─── ETA formatting ───────────────────────────────────────────────────────
	let etaDisplay = $derived.by(() => {
		if (!convoy?.etaSeconds) return '--:--';
		const total = Math.max(0, convoy.etaSeconds);
		const hrs = Math.floor(total / 3600);
		const mins = Math.floor((total % 3600) / 60);
		const secs = Math.floor(total % 60);
		if (hrs > 0) return `${hrs}h ${mins.toString().padStart(2, '0')}m`;
		return `${mins}m ${secs.toString().padStart(2, '0')}s`;
	});

	// ─── Route progress ───────────────────────────────────────────────────────
	let routeProgress = $derived.by(() => {
		if (!convoy?.position || !route?.geometry || route.geometry.length < 2) return 0;
		const pos = convoy.position;
		let closestIdx = 0;
		let closestDist = Infinity;

		for (let i = 0; i < route.geometry.length; i++) {
			const [lon, lat] = route.geometry[i];
			const d = Math.hypot(lon - pos[0], lat - pos[1]);
			if (d < closestDist) {
				closestDist = d;
				closestIdx = i;
			}
		}

		return Math.min(100, (closestIdx / (route.geometry.length - 1)) * 100);
	});

	let distanceCoveredKm = $derived.by(() => {
		if (!route) return 0;
		return (route.totalDistanceM / 1000) * (routeProgress / 100);
	});

	let totalDistanceKm = $derived(route ? route.totalDistanceM / 1000 : 0);

	// ─── Mini-map rendering ───────────────────────────────────────────────────
	function drawMiniMap() {
		if (!miniMapCanvas) return;
		const ctx = miniMapCanvas.getContext('2d');
		if (!ctx) return;

		const w = miniMapCanvas.width;
		const h = miniMapCanvas.height;

		// Clear
		ctx.fillStyle = '#0f172a';
		ctx.fillRect(0, 0, w, h);

		// Border
		ctx.strokeStyle = '#334155';
		ctx.lineWidth = 1;
		ctx.strokeRect(0.5, 0.5, w - 1, h - 1);

		if (!route?.geometry || route.geometry.length < 2) {
			ctx.fillStyle = '#94a3b8';
			ctx.font = '10px sans-serif';
			ctx.textAlign = 'center';
			ctx.fillText('No route', w / 2, h / 2 + 3);
			return;
		}

		// Compute bounding box of route
		let minLon = Infinity, maxLon = -Infinity;
		let minLat = Infinity, maxLat = -Infinity;
		for (const [lon, lat] of route.geometry) {
			minLon = Math.min(minLon, lon);
			maxLon = Math.max(maxLon, lon);
			minLat = Math.min(minLat, lat);
			maxLat = Math.max(maxLat, lat);
		}

		const pad = 8;
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

		// Draw route line
		ctx.beginPath();
		ctx.strokeStyle = '#334155';
		ctx.lineWidth = 2;
		const [sx, sy] = project(route.geometry[0][0], route.geometry[0][1]);
		ctx.moveTo(sx, sy);
		for (let i = 1; i < route.geometry.length; i++) {
			const [px, py] = project(route.geometry[i][0], route.geometry[i][1]);
			ctx.lineTo(px, py);
		}
		ctx.stroke();

		// Draw traversed portion in blue
		if (routeProgress > 0) {
			const traverseIdx = Math.floor((routeProgress / 100) * (route.geometry.length - 1));
			ctx.beginPath();
			ctx.strokeStyle = '#3b82f6';
			ctx.lineWidth = 2.5;
			const [tx, ty] = project(route.geometry[0][0], route.geometry[0][1]);
			ctx.moveTo(tx, ty);
			for (let i = 1; i <= traverseIdx && i < route.geometry.length; i++) {
				const [px, py] = project(route.geometry[i][0], route.geometry[i][1]);
				ctx.lineTo(px, py);
			}
			ctx.stroke();
		}

		// Draw origin
		const [ox, oy] = project(route.geometry[0][0], route.geometry[0][1]);
		ctx.beginPath();
		ctx.fillStyle = '#3b82f6';
		ctx.arc(ox, oy, 3, 0, Math.PI * 2);
		ctx.fill();

		// Draw destination
		const lastPt = route.geometry[route.geometry.length - 1];
		const [dx, dy] = project(lastPt[0], lastPt[1]);
		ctx.beginPath();
		ctx.fillStyle = '#ef4444';
		ctx.arc(dx, dy, 3, 0, Math.PI * 2);
		ctx.fill();

		// Draw convoy position
		if (convoy?.position) {
			const [cx, cy] = project(convoy.position[0], convoy.position[1]);

			// Glow
			ctx.beginPath();
			ctx.fillStyle = 'rgba(34, 197, 94, 0.3)';
			ctx.arc(cx, cy, 8, 0, Math.PI * 2);
			ctx.fill();

			// Dot
			ctx.beginPath();
			ctx.fillStyle = '#22c55e';
			ctx.arc(cx, cy, 4, 0, Math.PI * 2);
			ctx.fill();

			// Border
			ctx.beginPath();
			ctx.strokeStyle = '#ffffff';
			ctx.lineWidth = 1.5;
			ctx.arc(cx, cy, 4, 0, Math.PI * 2);
			ctx.stroke();
		}
	}

	$effect(() => {
		// Re-draw mini-map when convoy position, route, or progress changes
		convoy?.position;
		route?.geometry;
		routeProgress;
		drawMiniMap();
	});

	onMount(() => {
		// Set canvas resolution for sharpness
		if (miniMapCanvas) {
			const dpr = window.devicePixelRatio || 1;
			const rect = miniMapCanvas.getBoundingClientRect();
			miniMapCanvas.width = rect.width * dpr;
			miniMapCanvas.height = rect.height * dpr;
			const ctx = miniMapCanvas.getContext('2d');
			ctx?.scale(dpr, dpr);
			// Reset to CSS size for drawing math
			miniMapCanvas.width = rect.width;
			miniMapCanvas.height = rect.height;
			drawMiniMap();
		}
	});

	onDestroy(() => {
		if (animationFrame) cancelAnimationFrame(animationFrame);
	});
</script>

<div class="convoy-tracker">
	<!-- Header -->
	<div class="tracker-header">
		<div class="header-left">
			{#if convoy}
				<span
					class="vvip-badge"
					style="background: {statusColors[convoy.status] ?? '#6b7280'}"
				>
					{convoy.vvipClass}
				</span>
				<span class="movement-id">{convoy.movementId}</span>
			{:else}
				<span class="vvip-badge" style="background: #6b7280">--</span>
				<span class="movement-id">No active convoy</span>
			{/if}
		</div>
		<div class="header-right">
			{#if convoy}
				<span class="status-indicator" style="color: {statusColors[convoy.status] ?? '#6b7280'}">
					{statusIcons[convoy.status] ?? '\u25CB'}
					{convoy.status.toUpperCase()}
				</span>
			{/if}
			{#if diversionCount > 0}
				<span class="diversion-badge" title="{diversionCount} active diversion(s)">
					{diversionCount}
				</span>
			{/if}
		</div>
	</div>

	{#if convoy}
		<!-- Telemetry Grid -->
		<div class="telemetry-grid">
			<div class="telemetry-item">
				<span class="tel-label">Position</span>
				<span class="tel-value mono">
					{#if convoy.position}
						{convoy.position[1].toFixed(5)}, {convoy.position[0].toFixed(5)}
					{:else}
						Awaiting GPS
					{/if}
				</span>
			</div>
			<div class="telemetry-item">
				<span class="tel-label">Speed</span>
				<span class="tel-value mono">{convoy.speedKmh.toFixed(0)} km/h</span>
			</div>
			<div class="telemetry-item">
				<span class="tel-label">Heading</span>
				<span class="tel-value mono">{convoy.headingDeg.toFixed(0)}&deg;</span>
			</div>
			<div class="telemetry-item">
				<span class="tel-label">ETA</span>
				<span class="tel-value eta">{etaDisplay}</span>
			</div>
		</div>

		<!-- Route Progress -->
		{#if route}
			<div class="progress-section">
				<div class="progress-header">
					<span class="progress-label">Route Progress</span>
					<span class="progress-pct">{routeProgress.toFixed(1)}%</span>
				</div>
				<div class="progress-bar-track">
					<div
						class="progress-bar-fill"
						style="width: {routeProgress}%"
					></div>
					<div
						class="progress-bar-marker"
						style="left: {routeProgress}%"
					></div>
				</div>
				<div class="progress-distance">
					<span>{distanceCoveredKm.toFixed(1)} km</span>
					<span class="progress-sep">/</span>
					<span>{totalDistanceKm.toFixed(1)} km</span>
				</div>
			</div>
		{/if}

		<!-- Mini-Map -->
		<div class="minimap-section">
			<div class="minimap-header">
				<span class="minimap-label">Corridor Overview</span>
			</div>
			<canvas
				bind:this={miniMapCanvas}
				class="minimap-canvas"
				width="240"
				height="120"
			></canvas>
			<div class="minimap-legend">
				<span class="legend-dot origin"></span><span>Origin</span>
				<span class="legend-dot convoy-dot"></span><span>Convoy</span>
				<span class="legend-dot dest"></span><span>Destination</span>
			</div>
		</div>
	{:else}
		<div class="no-convoy">
			<div class="no-convoy-icon">{'\u26A0'}</div>
			<span>No active convoy movement</span>
			<span class="no-convoy-sub">Start a new movement to see tracking data</span>
		</div>
	{/if}
</div>

<style>
	.convoy-tracker {
		padding: 0.75rem;
		border-radius: 0.5rem;
		background: #0f172a;
		color: #f1f5f9;
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
		font-size: 0.8125rem;
	}

	/* ─── Header ────────────────────────────────────────────────────────── */
	.tracker-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}

	.header-left {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.header-right {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.vvip-badge {
		padding: 0.2rem 0.5rem;
		border-radius: 0.25rem;
		font-weight: 700;
		font-size: 0.8125rem;
		color: #fff;
		min-width: 1.75rem;
		text-align: center;
	}

	.movement-id {
		font-size: 0.75rem;
		color: #94a3b8;
		font-family: monospace;
	}

	.status-indicator {
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.03em;
	}

	.diversion-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 1.25rem;
		height: 1.25rem;
		padding: 0 0.25rem;
		border-radius: 9999px;
		background: rgba(239, 68, 68, 0.2);
		color: #ef4444;
		font-size: 0.6875rem;
		font-weight: 700;
	}

	/* ─── Telemetry Grid ───────────────────────────────────────────────── */
	.telemetry-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.375rem;
	}

	.telemetry-item {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
		padding: 0.5rem;
		background: #1e293b;
		border-radius: 0.375rem;
		border: 1px solid #334155;
	}

	.tel-label {
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #94a3b8;
	}

	.tel-value {
		font-size: 0.8125rem;
		font-weight: 600;
		color: #f1f5f9;
	}

	.tel-value.mono {
		font-family: monospace;
	}

	.tel-value.eta {
		color: #3b82f6;
		font-family: monospace;
		font-size: 0.875rem;
	}

	/* ─── Route Progress ───────────────────────────────────────────────── */
	.progress-section {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		padding: 0.5rem;
		background: #1e293b;
		border-radius: 0.375rem;
		border: 1px solid #334155;
	}

	.progress-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.progress-label {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #94a3b8;
	}

	.progress-pct {
		font-size: 0.8125rem;
		font-weight: 700;
		color: #3b82f6;
		font-family: monospace;
	}

	.progress-bar-track {
		position: relative;
		width: 100%;
		height: 6px;
		background: #334155;
		border-radius: 3px;
		overflow: visible;
	}

	.progress-bar-fill {
		height: 100%;
		border-radius: 3px;
		background: linear-gradient(90deg, #1d4ed8, #3b82f6);
		transition: width 0.5s ease;
	}

	.progress-bar-marker {
		position: absolute;
		top: 50%;
		transform: translate(-50%, -50%);
		width: 10px;
		height: 10px;
		border-radius: 50%;
		background: #22c55e;
		border: 2px solid #0f172a;
		box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
		transition: left 0.5s ease;
	}

	.progress-distance {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.6875rem;
		color: #94a3b8;
		font-family: monospace;
	}

	.progress-sep {
		color: #475569;
	}

	/* ─── Mini-Map ─────────────────────────────────────────────────────── */
	.minimap-section {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	.minimap-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.minimap-label {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: #94a3b8;
	}

	.minimap-canvas {
		width: 100%;
		height: 120px;
		border-radius: 0.375rem;
		border: 1px solid #334155;
		background: #0f172a;
	}

	.minimap-legend {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.625rem;
		color: #64748b;
	}

	.legend-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		margin-left: 0.375rem;
	}

	.legend-dot:first-child {
		margin-left: 0;
	}

	.legend-dot.origin { background: #3b82f6; }
	.legend-dot.convoy-dot { background: #22c55e; }
	.legend-dot.dest { background: #ef4444; }

	/* ─── No Convoy State ──────────────────────────────────────────────── */
	.no-convoy {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.375rem;
		padding: 1.5rem;
		color: #94a3b8;
		text-align: center;
	}

	.no-convoy-icon {
		font-size: 1.5rem;
		opacity: 0.5;
	}

	.no-convoy-sub {
		font-size: 0.6875rem;
		color: #64748b;
	}
</style>
