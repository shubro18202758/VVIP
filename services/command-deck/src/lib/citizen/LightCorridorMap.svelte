<script lang="ts">
	/**
	 * LightCorridorMap — the public corridor map (MapLibre on Daylight
	 * tokens). Keyless inline raster style re-graded from tokens (bright,
	 * desaturated paper ground), corridor spines drawn from the live
	 * /api/v1/spatial/segments geometry; live event routes render solid,
	 * fixture previews render dashed — the same honesty the operator map
	 * uses. One highlighted stretch (the granted green-corridor window)
	 * renders in the act accent, and the driver's tracked unit (emulated
	 * projection, labelled by the page) as a point.
	 *
	 * Layers are REACTIVE: routes / highlight / unit props re-render on
	 * change after load (live feeds arrive after the map does).
	 *
	 * Degradation, by MapLibre error class:
	 *   - tile/source errors (a blocked or missing OSM tile) degrade to the
	 *     paper ground — counted, never fatal;
	 *   - any other error before 'load' (an invalid style, a dead worker)
	 *     ⇒ EmptyState, never a silent blank box;
	 *   - segments API down ⇒ the map stays up with the honest note;
	 *   - MapLibre itself failing (no WebGL) ⇒ EmptyState.
	 *
	 * Test hook: window.__CITIZEN_MAP__ = { ready, segmentCount, routeCount,
	 * liveRouteCount, highlight, unit, tileRequests, tileErrors, error } —
	 * liveRouteCount is how many route stretches draw SOLID (live); fixture
	 * and emulated stretches always draw dashed.
	 */
	import { onMount } from 'svelte';
	import type { GeoJSONSource, Map as MlMap } from 'maplibre-gl';
	import { CITY_BBOX, NETWORK_BBOX } from '$lib/ops/endpoints';
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
		/** The tracked unit's position (the page labels it emulated). */
		unit?: { lon: number; lat: number } | null;
		/** Compact height for inline cards. */
		compact?: boolean;
	}

	let { lang, highlightSegmentId = null, routes = [], unit = null, compact = false }: Props = $props();

	type Feature = GeoJSON.Feature<GeoJSON.Geometry, Record<string, unknown>>;
	const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };

	let container: HTMLDivElement | undefined = $state();
	let failed = $state(false);
	let failure = $state<string | null>(null);
	let geometryNote = $state<string | null>(null);
	let ready = $state(false);
	/** Never proxy a MapLibre instance or the geometry index. */
	let map = $state.raw<MlMap | null>(null);
	let byId = $state.raw<Map<number, SegmentGeometry>>(new Map());
	let tileRequests = 0;
	let tileErrors = 0;
	let fittedKey = '';

	const cx = (CITY_BBOX.minLon + CITY_BBOX.maxLon) / 2;
	const cy = (CITY_BBOX.minLat + CITY_BBOX.maxLat) / 2;

	function publishHook() {
		(window as unknown as Record<string, unknown>).__CITIZEN_MAP__ = {
			ready,
			segmentCount: byId.size,
			routeCount: routes.length,
			liveRouteCount: routes.filter((r) => r.live).length,
			highlight: highlightSegmentId,
			unit: unit !== null,
			tileRequests,
			tileErrors,
			error: failure,
		};
	}

	function line(seg: SegmentGeometry, props: Record<string, unknown>): Feature {
		return { type: 'Feature', properties: props, geometry: { type: 'LineString', coordinates: seg.coordinates } };
	}

	function setData(id: string, data: GeoJSON.FeatureCollection) {
		(map?.getSource(id) as GeoJSONSource | undefined)?.setData(data);
	}

	function boundsOf(coords: Array<[number, number]>): [[number, number], [number, number]] | null {
		if (!coords.length) return null;
		let [minLon, minLat, maxLon, maxLat] = [180, 90, -180, -90];
		for (const [lon, lat] of coords) {
			minLon = Math.min(minLon, lon);
			maxLon = Math.max(maxLon, lon);
			minLat = Math.min(minLat, lat);
			maxLat = Math.max(maxLat, lat);
		}
		return [
			[minLon, minLat],
			[maxLon, maxLat],
		];
	}

	onMount(() => {
		const el = container;
		let disposed = false;

		void (async () => {
			if (!el) {
				failed = true;
				return;
			}
			try {
				const mod = await import('maplibre-gl');
				if (disposed) return;
				const maplibregl = (mod.default ?? mod) as typeof import('maplibre-gl');

				const m = new maplibregl.Map({
					container: el,
					center: [cx, cy],
					zoom: 12,
					attributionControl: false,
					transformRequest: (url, resourceType) => {
						if (String(resourceType) === 'Tile') tileRequests += 1;
						return { url };
					},
					style: {
						version: 8,
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
				map = m;
				m.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

				m.on('error', (e) => {
					const ev = e as { error?: { message?: string }; sourceId?: string; tile?: unknown };
					if (ev.sourceId || ev.tile) {
						// Tile/source failures degrade to the paper ground.
						tileErrors += 1;
						publishHook();
						return;
					}
					if (!ready) {
						// A style/worker failure before 'load' would otherwise
						// leave a blank box forever: say so instead.
						failure = ev.error?.message ?? 'map error';
						failed = true;
						publishHook();
					}
				});

				m.on('load', async () => {
					const act = cssVar('--v-act', 'daylight');
					const ink3 = cssVar('--v-ink-3', 'daylight');
					const line1 = cssVar('--v-line-1', 'daylight');
					const data = cssVar('--v-data', 'daylight');
					const paper = cssVar('--v-paper', 'daylight');
					for (const id of ['spine', 'routes', 'granted', 'unit']) {
						m.addSource(id, { type: 'geojson', data: EMPTY });
					}
					// Quiet ground spine (context, never the hero).
					m.addLayer({ id: 'spine-line', type: 'line', source: 'spine', paint: { 'line-color': line1, 'line-width': 2.5 } });
					// Event routes: live solid; fixture and emulated dashed (never live).
					m.addLayer({
						id: 'routes-line',
						type: 'line',
						source: 'routes',
						paint: {
							'line-color': ink3,
							'line-width': ['case', ['get', 'live'], 4, 3],
							'line-dasharray': ['case', ['get', 'live'], ['literal', [1, 0]], ['literal', [2, 1.5]]]
						}
					});
					// The granted stretch — act accent.
					m.addLayer({ id: 'granted-arc', type: 'line', source: 'granted', paint: { 'line-color': act, 'line-width': 5, 'line-opacity': 0.85 } });
					// The tracked unit.
					m.addLayer({
						id: 'unit-dot',
						type: 'circle',
						source: 'unit',
						paint: { 'circle-radius': 6, 'circle-color': data, 'circle-stroke-color': paper, 'circle-stroke-width': 2 }
					});
					ready = true;
					publishHook();

					// Corridor spine geometry — live endpoint, honest fallback. The
					// whole network (NETWORK_BBOX reaches the SP Ring Road west of
					// the city view); CITY_BBOX stays the map's centre only.
					try {
						const segments = await fetchSegments(NETWORK_BBOX);
						if (disposed) return;
						byId = new Map(segments.map((sg) => [sg.segmentId, sg]));
						geometryNote = null;
						setData('spine', {
							type: 'FeatureCollection',
							features: segments.map((sg) => line(sg, { sid: sg.segmentId }))
						});
					} catch {
						geometryNote = 'map_note_fallback';
					}
					publishHook();
				});
			} catch (err) {
				failure = err instanceof Error ? err.message : String(err);
				failed = true;
				publishHook();
			}
		})();

		return () => {
			disposed = true;
			(window as unknown as Record<string, unknown>).__CITIZEN_MAP__ = undefined;
			map?.remove();
			map = null;
		};
	});

	// Routes / highlight / unit follow their props after load.
	$effect(() => {
		if (!ready || !map) return;
		const index = byId;
		const routeFeatures: Feature[] = [];
		for (const r of routes) {
			const seg = index.get(r.segmentId);
			if (seg) routeFeatures.push(line(seg, { sid: seg.segmentId, live: r.live }));
		}
		setData('routes', { type: 'FeatureCollection', features: routeFeatures });

		const granted = highlightSegmentId != null ? index.get(highlightSegmentId) : undefined;
		setData('granted', { type: 'FeatureCollection', features: granted ? [line(granted, {})] : [] });

		setData(
			'unit',
			unit
				? {
						type: 'FeatureCollection',
						features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [unit.lon, unit.lat] } }]
					}
				: EMPTY
		);

		// Frame what matters once per distinct set: the granted stretch,
		// else the event routes.
		const focus = granted ? granted.coordinates : routeFeatures.flatMap((f) => (f.geometry as GeoJSON.LineString).coordinates as Array<[number, number]>);
		const key = granted ? `g${granted.segmentId}` : routeFeatures.map((f) => f.properties.sid).join(',');
		const bounds = boundsOf(focus);
		if (bounds && key !== fittedKey) {
			fittedKey = key;
			map.fitBounds(bounds, { padding: 40, duration: 600, maxZoom: 14 });
		}
		publishHook();
	});
</script>

<div class="light-map" class:compact data-prim="light-corridor-map">
	{#if failed}
		<div data-map-ready="failed" data-map-error={failure ?? ''}>
			<EmptyState title={citizenT(lang, 'map_unavailable')} hint={citizenT(lang, 'map_unavailable_hint')} />
		</div>
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
