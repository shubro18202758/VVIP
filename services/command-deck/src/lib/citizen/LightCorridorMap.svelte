<script lang="ts">
	/**
	 * LightCorridorMap — the public corridor map (MapLibre on Daylight
	 * tokens). Keyless inline raster style re-graded from tokens (bright,
	 * desaturated paper ground), corridor spines drawn from the live
	 * /api/v1/spatial/segments geometry; live event routes render solid,
	 * fixture previews render dashed — the same honesty the operator map
	 * uses. One highlighted stretch (the granted green-corridor window)
	 * renders in the act accent with a marching-dash future arc.
	 *
	 * Degradation: segments API down or not deployed ⇒ the map stays up
	 * with the honest fallback note (never invented geometry); MapLibre
	 * itself failing (no WebGL) ⇒ EmptyState.
	 */
	import { onMount } from 'svelte';
	import { CITY_BBOX } from '$lib/ops/endpoints';
	import { fetchSegments, type SegmentGeometry } from '$lib/gods-eye/data';
	import { cssVar } from '$lib/design/tokens';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import EmptyState from '$lib/primitives/EmptyState.svelte';

	interface Props {
		lang: string;
		/** Segment id to highlight (the granted stretch on /driver). */
		highlightSegmentId?: number | null;
		/** Corridor event routes to draw: segment id ⇒ live|fixture. */
		routes?: Array<{ segmentId: number; live: boolean }>;
		/** Compact height for inline cards. */
		compact?: boolean;
	}

	let { lang, highlightSegmentId = null, routes = [], compact = false }: Props = $props();

	let container: HTMLDivElement | undefined = $state();
	let failed = $state(false);
	let geometryNote = $state<string | null>(null);
	let ready = $state(false);

	const cx = (CITY_BBOX.minLon + CITY_BBOX.maxLon) / 2;
	const cy = (CITY_BBOX.minLat + CITY_BBOX.maxLat) / 2;

	onMount(() => {
		const el = container;
		let map: import('maplibre-gl').Map | null = null;
		let disposed = false;

		void (async () => {
			if (!el) {
				failed = true;
				return;
			}
			let maplibregl: typeof import('maplibre-gl') | null = null;
			try {
			const mod = await import('maplibre-gl');
			if (disposed) return;
			maplibregl = (mod.default ?? mod) as typeof import('maplibre-gl');

			const act = cssVar('--v-act', 'daylight');
			const ink3 = cssVar('--v-ink-3', 'daylight');
			const line1 = cssVar('--v-line-1', 'daylight');

			map = new maplibregl.Map({
				container: el,
				center: [cx, cy],
				zoom: 12,
				attributionControl: false,
				style: {
					version: 8,
					glyphs: undefined,
					sources: {
						osm: {
							type: 'raster',
							tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
							tileSize: 256,
							maxzoom: 19,
							attribution: '© OpenStreetMap contributors'
						}
					},
					layers: [
						{
							id: 'paper',
							type: 'background',
							paint: { 'background-color': cssVar('--v-paper', 'daylight') }
						},
						{
							id: 'osm-tiles',
							type: 'raster',
							source: 'osm',
							paint: {
								'raster-brightness-max': 0.96,
								'raster-saturation': -0.45,
								'raster-contrast': 0.05,
								'raster-opacity': 0.9
							}
						}
					]
				}
			});
			map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

			map.on('error', () => {
				/* tile failures degrade to the paper ground silently */
			});

			map.on('load', async () => {
				ready = true;
				// Corridor spine geometry — live endpoint, honest fallback.
				let segments: SegmentGeometry[] = [];
				try {
					segments = await fetchSegments(CITY_BBOX);
					geometryNote = null;
				} catch {
					geometryNote = 'map_note_fallback';
				}

				const byId = new Map(segments.map((s) => [s.segmentId, s]));

				// Quiet ground spine (context, never the hero).
				if (segments.length) {
					map!.addSource('spine', {
						type: 'geojson',
						data: {
							type: 'FeatureCollection',
							features: segments.map((s) => ({
								type: 'Feature',
								properties: { sid: s.segmentId },
								geometry: { type: 'LineString', coordinates: s.coordinates }
							}))
						}
					});
					map!.addLayer({
						id: 'spine-line',
						type: 'line',
						source: 'spine',
						paint: { 'line-color': line1, 'line-width': 2.5 }
					});
				}

				// Event routes: live solid, fixture dashed.
				const routeFeatures = routes
					.map((r) => byId.get(r.segmentId))
					.filter((s): s is SegmentGeometry => !!s)
					.map((s) => ({
						type: 'Feature' as const,
						properties: { sid: s.segmentId, live: routes.find((r) => r.segmentId === s.segmentId)?.live ?? false },
						geometry: { type: 'LineString' as const, coordinates: s.coordinates }
					}));
				if (routeFeatures.length) {
					map!.addSource('routes', {
						type: 'geojson',
						data: { type: 'FeatureCollection', features: routeFeatures }
					});
					map!.addLayer({
						id: 'routes-line',
						type: 'line',
						source: 'routes',
						paint: {
							'line-color': ink3,
							'line-width': ['case', ['get', 'live'], 4, 3],
							'line-dasharray': ['case', ['get', 'live'], ['literal', [1, 0]], ['literal', [2, 1.5]]]
						}
					});
				}

				// The granted stretch — act accent, marching dashes (future arc).
				const granted = highlightSegmentId != null ? byId.get(highlightSegmentId) : null;
				if (granted) {
					map!.addSource('granted', {
						type: 'geojson',
						data: {
							type: 'FeatureCollection',
							features: [
								{
									type: 'Feature',
									properties: {},
									geometry: { type: 'LineString', coordinates: granted.coordinates }
								}
							]
						}
					});
					map!.addLayer({
						id: 'granted-arc',
						type: 'line',
						source: 'granted',
						paint: { 'line-color': act, 'line-width': 5, 'line-opacity': 0.85 }
					});
					const bounds = granted.coordinates.reduce(
						(acc, c) => ({
							minLon: Math.min(acc.minLon, c[0]),
							maxLon: Math.max(acc.maxLon, c[0]),
							minLat: Math.min(acc.minLat, c[1]),
							maxLat: Math.max(acc.maxLat, c[1])
						}),
						{ minLon: 180, maxLon: -180, minLat: 90, maxLat: -90 }
					);
					map!.fitBounds(
						[
							[bounds.minLon, bounds.minLat],
							[bounds.maxLon, bounds.maxLat]
						],
						{ padding: 48, duration: 600, maxZoom: 14 }
					);
				}

				// Test hook (gods-eye window.__GODS_EYE__ lineage).
				(window as unknown as Record<string, unknown>).__CITIZEN_MAP__ = {
					ready: true,
					segmentCount: segments.length,
					highlight: highlightSegmentId
				};
			});
			} catch {
				failed = true;
			}
		})();

		return () => {
			disposed = true;
			(window as unknown as Record<string, unknown>).__CITIZEN_MAP__ = undefined;
			map?.remove();
		};
	});
</script>

<div class="light-map" class:compact data-prim="light-corridor-map">
	{#if failed}
		<EmptyState title="Map unavailable" hint="WebGL could not start on this device." />
	{:else}
		<div bind:this={container} class="canvas" data-map-ready={ready ? 'yes' : 'no'}></div>
		{#if geometryNote}
			<p class="note" data-map-note={geometryNote}>{citizenT(lang, geometryNote)}</p>
		{/if}
	{/if}
</div>

<style>
	.light-map {
		position: relative;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		overflow: hidden;
		background: var(--v-paper);
		height: 320px;
	}

	.light-map.compact {
		height: 220px;
	}

	.canvas {
		position: absolute;
		inset: 0;
	}

	.note {
		position: absolute;
		left: var(--v-space-2);
		bottom: var(--v-space-2);
		margin: 0;
		padding: 2px var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		background: var(--v-surface-1);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
	}

	.light-map :global(.maplibregl-ctrl-attrib) {
		font-size: var(--v-text-2xs);
		background: var(--v-surface-1);
	}
</style>
