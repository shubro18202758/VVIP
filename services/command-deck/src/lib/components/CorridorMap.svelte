<!-- CorridorMap.svelte — Full-screen GIS map viewer using MapLibre GL + deck.gl -->
<!-- Renders the road network, live traffic heatmap, convoy position, route, and diversion zones -->

<script lang="ts">
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { onMount, onDestroy } from 'svelte';
	import {
		liveTraffic,
		roadSegments,
		heatmapVisible,
		heatmapOpacity,
		roadNetworkVisible,
		trafficArray,
		roadSegmentArray,
	} from '$stores/traffic';
	import {
		activeConvoy,
		selectedRoute,
		activeDiversions,
	} from '$stores/convoy';
	import { roadNetworkFeed, trafficFeed } from '$stores/connection';
	import { updateViewport } from '$lib/api/data-service';
	import type { RoadSegment, SegmentTraffic } from '$lib/types';

	// ─── Props ────────────────────────────────────────────────────────────────
	let {
		center = [77.209, 28.6139] as [number, number],
		zoom = 12 as number,
		onSegmentClick = (_segment: RoadSegment | null) => {},
	} = $props();

	// ─── State ────────────────────────────────────────────────────────────────
	let mapContainer: HTMLDivElement;
	let map: any = $state(null);
	let deckOverlay: any = $state(null);
	let loaded = $state(false);
	let mapError = $state<string | null>(null);
	let maplibregl: any = $state(null);
	let viewportDebounce: ReturnType<typeof setTimeout> | null = null;

	// ─── Viewport change → data-service ───────────────────────────────────────
	function emitViewportBbox() {
		if (!map) return;
		const bounds = map.getBounds();
		updateViewport({
			minLon: bounds.getWest(),
			minLat: bounds.getSouth(),
			maxLon: bounds.getEast(),
			maxLat: bounds.getNorth(),
		});
	}

	function handleMoveEnd() {
		if (viewportDebounce) clearTimeout(viewportDebounce);
		viewportDebounce = setTimeout(emitViewportBbox, 300);
	}

	// ─── Road class color mapping ─────────────────────────────────────────────
	const ROAD_CLASS_COLORS: Record<string, [number, number, number, number]> = {
		motorway: [59, 130, 246, 200],
		trunk: [34, 211, 238, 200],
		primary: [34, 197, 94, 200],
		secondary: [234, 179, 8, 200],
		tertiary: [249, 115, 22, 200],
		residential: [148, 163, 184, 120],
		service: [100, 116, 139, 100],
		unclassified: [100, 116, 139, 100],
	};

	function getRoadClassColor(roadClass: string): [number, number, number, number] {
		return ROAD_CLASS_COLORS[roadClass] ?? [100, 116, 139, 120];
	}

	// ─── Congestion color ramp: green (0) -> yellow (0.5) -> red (1.0) ────────
	function congestionColor(idx: number): [number, number, number, number] {
		const t = Math.max(0, Math.min(1, idx));
		if (t <= 0.5) {
			const s = t / 0.5;
			return [
				Math.round(34 + (234 - 34) * s),
				Math.round(197 - (197 - 179) * s),
				Math.round(94 - 94 * s),
				200,
			];
		} else {
			const s = (t - 0.5) / 0.5;
			return [
				Math.round(234 + (239 - 234) * s),
				Math.round(179 - 179 * s),
				Math.round(0),
				200,
			];
		}
	}

	// ─── Compute centroids for traffic scatter layer ──────────────────────────
	function getSegmentCentroid(segmentId: number): [number, number] | null {
		const segments = $roadSegments;
		const seg = segments.get(segmentId);
		if (!seg || !seg.geometry || seg.geometry.length === 0) return null;
		const coords = seg.geometry;
		const midIdx = Math.floor(coords.length / 2);
		return coords[midIdx];
	}

	// ─── Build deck.gl layers ─────────────────────────────────────────────────
	async function buildLayers() {
		const { PathLayer, ScatterplotLayer, PolygonLayer } = await import('@deck.gl/layers');

		const roadSegs = $roadSegmentArray;
		const trafficData = $trafficArray;
		const route = $selectedRoute;
		const convoy = $activeConvoy;
		const divs = $activeDiversions;
		const showHeatmap = $heatmapVisible;
		const heatOpacity = $heatmapOpacity;
		const showRoads = $roadNetworkVisible;

		const layers: any[] = [];

		// Layer 1: Road network paths
		if (showRoads && roadSegs.length > 0) {
			layers.push(
				new PathLayer({
					id: 'road-network',
					data: roadSegs,
					getPath: (d: RoadSegment) => d.geometry,
					getColor: (d: RoadSegment) => getRoadClassColor(d.roadClass),
					getWidth: (d: RoadSegment) => Math.max(1, d.lanes) * 2,
					widthMinPixels: 1,
					widthScale: 1,
					pickable: true,
					autoHighlight: true,
					highlightColor: [255, 255, 255, 80],
					onClick: (info: any) => {
						if (info.object) {
							onSegmentClick(info.object as RoadSegment);
						}
					},
				}),
			);
		}

		// Layer 2: Traffic heatmap scatter
		if (showHeatmap && trafficData.length > 0) {
			const trafficWithPositions = trafficData
				.map((t: SegmentTraffic) => {
					const pos = getSegmentCentroid(t.segmentId);
					return pos ? { ...t, position: pos } : null;
				})
				.filter(Boolean);

			layers.push(
				new ScatterplotLayer({
					id: 'traffic-heatmap',
					data: trafficWithPositions,
					getPosition: (d: any) => d.position,
					getRadius: 50,
					radiusUnits: 'meters' as const,
					getFillColor: (d: any) => congestionColor(d.congestionIdx),
					opacity: heatOpacity,
					pickable: false,
				}),
			);
		}

		// Layer 3: Selected route path
		if (route && route.geometry && route.geometry.length > 0) {
			layers.push(
				new PathLayer({
					id: 'selected-route',
					data: [route],
					getPath: (d: any) => d.geometry,
					getColor: [59, 130, 246, 220],
					getWidth: 6,
					widthMinPixels: 3,
					getDashArray: [10, 5],
					dashJustified: true,
					extensions: [],
				}),
			);
		}

		// Layer 4: Convoy position marker
		if (convoy && convoy.position) {
			layers.push(
				new ScatterplotLayer({
					id: 'convoy-position',
					data: [convoy],
					getPosition: (d: any) => d.position,
					getFillColor: [34, 197, 94, 255],
					getLineColor: [255, 255, 255, 255],
					getRadius: 80,
					radiusUnits: 'meters' as const,
					stroked: true,
					lineWidthMinPixels: 3,
					pickable: true,
					getAngle: (d: any) => d.headingDeg ?? 0,
				}),
			);
		}

		// Layer 5: Diversion zone polygons
		if (divs && divs.length > 0) {
			const diversionPolygons = divs
				.filter((d) => d.alternateRoute && d.alternateRoute.length >= 3)
				.map((d) => ({
					...d,
					polygon: d.alternateRoute,
				}));

			if (diversionPolygons.length > 0) {
				layers.push(
					new PolygonLayer({
						id: 'diversion-zones',
						data: diversionPolygons,
						getPolygon: (d: any) => d.polygon,
						getFillColor: [239, 68, 68, 80],
						getLineColor: [239, 68, 68, 200],
						getLineWidth: 2,
						lineWidthMinPixels: 1,
						pickable: true,
					}),
				);
			}
		}

		return layers;
	}

	// ─── Update deck.gl overlay ───────────────────────────────────────────────
	async function updateDeckLayers() {
		if (!deckOverlay || !loaded) return;
		try {
			const layers = await buildLayers();
			deckOverlay.setProps({ layers });
		} catch (e) {
			console.error('[CorridorMap] Failed to update deck.gl layers:', e);
		}
	}

	// ─── Reactive layer updates ───────────────────────────────────────────────
	$effect(() => {
		// Access all reactive dependencies to trigger re-run
		$roadSegmentArray;
		$trafficArray;
		$selectedRoute;
		$activeConvoy;
		$activeDiversions;
		$heatmapVisible;
		$heatmapOpacity;
		$roadNetworkVisible;

		if (loaded) {
			updateDeckLayers();
		}
	});

	// ─── Map initialization ───────────────────────────────────────────────────
	onMount(async () => {
		try {
			const mgl = await import('maplibre-gl');
			maplibregl = mgl.default ?? mgl;

			const { MapboxOverlay } = await import('@deck.gl/mapbox');

			map = new maplibregl.Map({
				container: mapContainer,
				style: 'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
				center,
				zoom,
				antialias: true,
				attributionControl: false,
			});

			map.addControl(new maplibregl.NavigationControl({ showCompass: true, showZoom: true }), 'top-right');
			map.addControl(new maplibregl.ScaleControl({ maxWidth: 200, unit: 'metric' }), 'bottom-left');
			map.addControl(
				new maplibregl.GeolocateControl({
					positionOptions: { enableHighAccuracy: true },
					trackUserLocation: false,
				}),
				'top-right',
			);
			map.addControl(
				new maplibregl.AttributionControl({ compact: true }),
				'bottom-right',
			);

			// Timeout: fail if map doesn't load within 15s
			const loadTimeout = setTimeout(() => {
				if (!loaded) {
					mapError = 'Map initialization timed out';
				}
			}, 15_000);

			map.on('load', async () => {
				clearTimeout(loadTimeout);
				deckOverlay = new MapboxOverlay({
					interleaved: true,
					layers: [],
				});
				map.addControl(deckOverlay);

				loaded = true;
				await updateDeckLayers();

				// Wire viewport-based data fetching
				map.on('moveend', handleMoveEnd);
				emitViewportBbox(); // initial fetch for current viewport
			});

			map.on('error', (e: any) => {
				console.error('[CorridorMap] Map error:', e);
			});
		} catch (err) {
			const msg = err instanceof Error ? err.message : 'Unknown error';
			console.error('[CorridorMap] Failed to initialize map:', err);
			mapError = msg;
		}
	});

	onDestroy(() => {
		if (deckOverlay) {
			try {
				map?.removeControl(deckOverlay);
			} catch {
				// Overlay already removed
			}
			deckOverlay = null;
		}
		map?.remove();
		map = null;
		loaded = false;
	});

	// ─── Public API ───────────────────────────────────────────────────────────
	export function fitBounds(bounds: [[number, number], [number, number]]) {
		map?.fitBounds(bounds, { padding: 60, duration: 1200 });
	}

	export function flyTo(targetCenter: [number, number], targetZoom?: number) {
		map?.flyTo({
			center: targetCenter,
			zoom: targetZoom ?? zoom,
			duration: 1500,
			essential: true,
		});
	}
</script>

<div class="corridor-map-wrapper">
	<div bind:this={mapContainer} class="corridor-map"></div>

	{#if mapError}
		<div class="map-loading">
			<span class="error-icon">!</span>
			<span class="loading-text" style="color: #fca5a5">Map failed to load</span>
			<span class="error-detail">{mapError}</span>
		</div>
	{:else if !loaded}
		<div class="map-loading">
			<div class="loading-spinner"></div>
			<span class="loading-text">Initializing map...</span>
		</div>
	{/if}

	<div class="map-status-bar">
		<span class="status-dot" class:online={loaded && !mapError} class:error={!!mapError}></span>
		<span class="status-text">
			{#if mapError}
				Error
			{:else if !loaded}
				Loading...
			{:else}
				Map Ready
			{/if}
		</span>
		{#if loaded && $roadNetworkFeed.state === 'loading'}
			<span class="feed-badge loading">Roads</span>
		{/if}
		{#if loaded && $trafficFeed.state === 'loading'}
			<span class="feed-badge loading">Traffic</span>
		{/if}
		{#if $roadNetworkFeed.state === 'error'}
			<span class="feed-badge error">Roads</span>
		{/if}
		{#if $trafficFeed.state === 'error'}
			<span class="feed-badge error">Traffic</span>
		{/if}
	</div>
</div>

<style>
	.corridor-map-wrapper {
		position: relative;
		width: 100%;
		height: 100%;
		min-height: 400px;
		background: #0f172a;
		border-radius: 0.5rem;
		overflow: hidden;
	}

	.corridor-map {
		width: 100%;
		height: 100%;
		min-height: 400px;
	}

	/* MapLibre control overrides for dashboard theme */
	.corridor-map :global(.maplibregl-ctrl-group) {
		background: rgba(15, 23, 42, 0.9);
		border: 1px solid #334155;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
	}

	.corridor-map :global(.maplibregl-ctrl-group button) {
		background-color: transparent;
		border-color: #334155;
		color: #f1f5f9;
	}

	.corridor-map :global(.maplibregl-ctrl-group button:hover) {
		background-color: #334155;
	}

	.corridor-map :global(.maplibregl-ctrl-group button span) {
		filter: invert(1);
	}

	.corridor-map :global(.maplibregl-ctrl-scale) {
		background: rgba(15, 23, 42, 0.85);
		color: #f1f5f9;
		border-color: #94a3b8;
		font-size: 0.75rem;
	}

	.corridor-map :global(.maplibregl-ctrl-attrib) {
		background: rgba(15, 23, 42, 0.7);
		color: #94a3b8;
		font-size: 0.625rem;
	}

	.corridor-map :global(.maplibregl-ctrl-attrib a) {
		color: #94a3b8;
	}

	/* Ensure map canvas is interactive */
	.corridor-map :global(.maplibregl-canvas) {
		cursor: grab;
	}

	.corridor-map :global(.maplibregl-canvas:active) {
		cursor: grabbing;
	}

	.map-loading {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 1rem;
		background: rgba(15, 23, 42, 0.9);
		z-index: 10;
	}

	.loading-spinner {
		width: 2.5rem;
		height: 2.5rem;
		border: 3px solid #334155;
		border-top-color: #3b82f6;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.loading-text {
		color: #94a3b8;
		font-size: 0.875rem;
		font-weight: 500;
	}

	.map-status-bar {
		position: absolute;
		top: 0.5rem;
		left: 0.5rem;
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.25rem 0.625rem;
		background: rgba(15, 23, 42, 0.85);
		border: 1px solid #334155;
		border-radius: 0.375rem;
		z-index: 5;
	}

	.status-dot {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		background: #ef4444;
		transition: background 0.3s ease;
	}

	.status-dot.online {
		background: #22c55e;
		box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
	}

	.status-text {
		color: #94a3b8;
		font-size: 0.6875rem;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.status-dot.error {
		background: #ef4444;
		box-shadow: 0 0 6px rgba(239, 68, 68, 0.5);
	}

	.error-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.5rem;
		height: 2.5rem;
		border-radius: 50%;
		background: rgba(239, 68, 68, 0.15);
		color: #fca5a5;
		font-size: 1.25rem;
		font-weight: 700;
	}

	.error-detail {
		color: #64748b;
		font-size: 0.75rem;
		max-width: 300px;
		text-align: center;
	}

	.feed-badge {
		font-size: 0.5625rem;
		font-weight: 600;
		text-transform: uppercase;
		padding: 0.0625rem 0.375rem;
		border-radius: 0.1875rem;
		letter-spacing: 0.03em;
	}

	.feed-badge.loading {
		background: rgba(59, 130, 246, 0.15);
		color: #93c5fd;
		animation: pulse-badge 1.5s ease-in-out infinite;
	}

	.feed-badge.error {
		background: rgba(239, 68, 68, 0.15);
		color: #fca5a5;
	}

	@keyframes pulse-badge {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}
</style>
