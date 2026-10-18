/**
 * corridor-events layer — route spines + zone polygons + phase-colored
 * event markers, bound to the LIVE ops store.
 *
 * PRIMARY geometry = route spines: ops.events[].routeSegmentIds resolved
 * against the live segment map (GET /api/v1/spatial/segments through the
 * canvas store). Zone polygons are the OPTIONAL additive enhancement —
 * this layer re-reads GET /api/v1/events/active raw for `zone_geojson`
 * (ST_AsGeoJSON of zone_geom, the C1 additive field owned by the
 * watchlist-anpr stream) and counts zones the moment the field appears;
 * until then spines alone render (documented degradation, not an error).
 *
 * Lifecycle state colors (from the ops store's event rows):
 *   planned → cyan · active → amber · completed → grey · cancelled → slate.
 * Fixture rows (offline-fixture) render at reduced opacity with a PREVIEW
 * label so operators can tell preview from live.
 */

import { ops } from '$lib/ops/store.svelte';
import type { CorridorEvent } from '$lib/ops/types';
import { ORACLE_V1 } from '$lib/ops/endpoints';
import { godsEye } from '../store.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssVar } from '../theme';

export const CORRIDOR_EVENTS_PICK_PREFIX = 'evt:';

// Cesium enums as verified constants (the ambient global has no enum typing).
const LABEL_FILL_AND_OUTLINE = 2;
const VERTICAL_ORIGIN_BOTTOM = -1;

/**
 * Lifecycle colours from the design tokens at runtime (theme.cssVar reads
 * the live token value; CSS colour-keyword fallbacks are last resort, never
 * a second palette): planned is the data-accent outline, active is the
 * HUMAN-ACT orange (a live corridor IS a 'your move' surface),
 * completed/cancelled sit dim ink. Returns a CSS colour string — callers
 * wrap with cesium.Color.fromCssColorString; withAlpha passes non-hex
 * values through unchanged (documented below).
 */
function stateColor(state: string): string {
	switch (state) {
		case 'active':
			return cssVar('--v-act') ?? 'orange';
		case 'completed':
		case 'cancelled':
			return cssVar('--v-ink-4') ?? 'dimgray';
		default:
			return cssVar('--v-data') ?? 'cyan';
	}
}

const ZONE_HEIGHT_BY_STATE: Record<string, number> = {
	planned: 60,
	active: 140,
	completed: 30,
	cancelled: 20,
};

/** GeoJSON Polygon ring → Cartesian3 list (Cesium polygon hierarchy input). */
function ringToPositions(cesium: CesiumGlobal, ring: unknown): unknown[] {
	const out: unknown[] = [];
	if (!Array.isArray(ring)) return out;
	for (const c of ring) {
		if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') {
			out.push(cesium.Cartesian3.fromDegrees(c[0], c[1]));
		}
	}
	return out;
}

function withAlpha(css: string, alpha: number): string {
	// Token values that are not #rrggbb (rgba()/keywords) pass through
	// unchanged — the veil runs at full token alpha instead.
	if (/^#[0-9a-f]{6}$/i.test(css)) {
		const r = parseInt(css.slice(1, 3), 16);
		const g = parseInt(css.slice(3, 5), 16);
		const b = parseInt(css.slice(5, 7), 16);
		return `rgba(${r},${g},${b},${alpha})`;
	}
	return css;
}

function createCorridorEventsLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let hint = 'loading';
	let lastSignature = '';
	let zoneCount = 0;

	/**
	 * Optional additive field (C1): absent until the events API carries
	 * zone_geojson (ST_AsGeoJSON of zone_geom). A 404/down just means no
	 * zones — never fatal. Rows are parsed defensively; a malformed ring is
	 * skipped, and an event without geometry keeps its spine.
	 */
	let zoneRows: Array<{ eventId: string; state: string; ring: unknown; color: string }> = [];

	async function pollZones() {
		if (!ctx || !active) return;
		try {
			const resp = (await ctx.fetchJson(`${ORACLE_V1}/events/active`, AbortSignal.timeout(6000))) as {
				events?: Array<Record<string, unknown>>;
			};
			const rows = Array.isArray(resp?.events) ? (resp?.events ?? []) : [];
			zoneRows = [];
			for (const r of rows) {
				const id = r.event_id ?? r.id;
				const z = r.zone_geojson;
				if (typeof id !== 'string' && typeof id !== 'number') continue;
				if (z == null || typeof z !== 'object' || (z as { type?: string }).type !== 'Polygon') continue;
				const rings = (z as { coordinates?: unknown }).coordinates;
				if (!Array.isArray(rings) || !Array.isArray(rings[0])) continue;
				const state =
					typeof r.state === 'string' ? r.state : 'planned';
				zoneRows.push({
					eventId: String(id),
					state,
					ring: rings[0],
					color: stateColor(state),
				});
			}
		} catch {
			zoneRows = [];
		}
	}

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const segmentMap = godsEye.segmentById;
		const events = ops.events;
		const sig = JSON.stringify([
			events.map((e) => [e.eventId, e.state, e.routeSegmentIds.join('.')]),
			godsEye.segments.map((s) => s.segmentId),
			zoneRows.map((z) => [z.eventId, z.state, Array.isArray(z.ring) ? z.ring.length : 0]),
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		let spineCount = 0;
		const markerCount = events.length;

		for (const ev of events) {
			const color = stateColor(ev.state);
			const live = ev.source !== 'offline-fixture';

			// Route spine: one polyline per segment (per-segment entities keep
			// pick ids stable + simple).
			for (const segId of ev.routeSegmentIds) {
				const seg = segmentMap.get(segId);
				if (!seg || seg.coordinates.length < 2) continue;
				const positions: unknown[] = [];
				for (const [lon, lat] of seg.coordinates) {
					positions.push(cesium.Cartesian3.fromDegrees(lon, lat, 4));
				}
				if (positions.length < 2) continue;
				spineCount++;
				const entity = viewer.entities.add({
					id: `${CORRIDOR_EVENTS_PICK_PREFIX}spine-${ev.eventId}-${segId}`,
					polyline: {
						positions,
						width: ev.state === 'active' ? 4 : 2,
						material: cesium.Color.fromCssColorString(
							live ? color : withAlpha(color, 0.45),
						),
					},
					description: `${ev.name} · ${ev.eventKind} · ${ev.state}${
						live ? '' : ' (offline fixture)'
					} — segment ${segId}${seg.roadName ? ` (${seg.roadName})` : ''}`,
				});
				entityIds.push(entity.id);
			}

			// Event marker at the spine midpoint.
			const anchor = anchorOf(ev, segmentMap);
			if (anchor) {
				const entity = viewer.entities.add({
					id: `${CORRIDOR_EVENTS_PICK_PREFIX}mark-${ev.eventId}`,
					position: cesium.Cartesian3.fromDegrees(anchor[0], anchor[1], 30),
					point: {
						pixelSize: ev.state === 'active' ? 12 : 8,
						color: cesium.Color.fromCssColorString(color),
						outlineColor: cesium.Color.fromCssColorString(cssVar('--v-canvas') ?? 'black'),
						outlineWidth: 2,
					},
					label: {
						text: `${ev.name}${live ? '' : ' ·PREVIEW'}`,
						font: '13px monospace',
						fillColor: cesium.Color.fromCssColorString(cssVar('--v-ink-1') ?? 'white'),
						outlineColor: cesium.Color.fromCssColorString(cssVar('--v-canvas') ?? 'black'),
						outlineWidth: 2,
						style: LABEL_FILL_AND_OUTLINE,
						verticalOrigin: VERTICAL_ORIGIN_BOTTOM,
						pixelOffset: new cesium.Cartesian2(0, 14),
					},
					description: `${ev.name} · ${ev.eventKind} · state=${ev.state} · band=${ev.band ?? '—'}${
						live ? '' : ' (offline fixture)'
					}`,
				});
				entityIds.push(entity.id);
			}
		}

		// Zone polygons (extruded) — the C1 additive enhancement. Only rows
		// whose event ALSO sits on the board render, so fixture-only zones
		// never masquerade as live closures.
		const known = new Set(events.map((e) => e.eventId));
		zoneCount = 0;
		for (const z of zoneRows) {
			if (!known.has(z.eventId)) continue;
			const positions = ringToPositions(cesium, z.ring);
			if (positions.length < 3) continue;
			zoneCount++;
			const height = ZONE_HEIGHT_BY_STATE[z.state] ?? 40;
			const entity = viewer.entities.add({
				id: `${CORRIDOR_EVENTS_PICK_PREFIX}zone-${z.eventId}`,
				polygon: {
					hierarchy: { positions },
					material: cesium.Color.fromCssColorString(withAlpha(z.color, 0.18)),
					extrudedHeight: height,
					height: 0,
					outline: true,
					outlineColor: cesium.Color.fromCssColorString(z.color),
				},
				description: `corridor zone · ${z.eventId} · state ${z.state} · extruded ${height} m`,
			});
			entityIds.push(entity.id);
		}

		const live = events.filter((e) => e.source !== 'offline-fixture').length;
		hint =
			godsEye.segmentsFeed.status === 'ok'
				? `${spineCount} spines · ${markerCount} events (${live} live) · ${zoneCount} zones`
				: `segments ${godsEye.segmentsFeed.status} — ${markerCount} events (no spines)`;
	}

	return {
		async status() {
			return { configured: true };
		},
		init(layerCtx) {
			ctx = layerCtx;
		},
		activate() {
			active = true;
			draw();
			void pollZones();
			timer = setInterval(() => draw(), 2000);
		},
		deactivate() {
			active = false;
			if (timer) clearInterval(timer);
			timer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			lastSignature = '';
		},
		refresh() {
			lastSignature = '';
			void godsEye.pollSegments(true);
			void pollZones();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(CORRIDOR_EVENTS_PICK_PREFIX);
		},
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.segmentsFeed.status === 'empty' && ops.events.length === 0,
				hint,
				error: godsEye.segmentsFeed.note,
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

function anchorOf(
	ev: CorridorEvent,
	segmentMap: Map<number, { coordinates: Array<[number, number]> }>,
): [number, number] | null {
	const segIds = ev.routeSegmentIds;
	const mid = segIds[Math.floor(segIds.length / 2)] ?? segIds[0];
	const seg = mid != null ? segmentMap.get(mid) : undefined;
	if (seg && seg.coordinates.length) {
		const c = seg.coordinates[Math.floor(seg.coordinates.length / 2)];
		return [c[0], c[1]];
	}
	return null;
}

export const corridorEventsLayer: GodsEyeLayer = {
	id: 'events',
	name: 'Corridor events (spines + zones)',
	label: 'EVENTS',
	requiresKeyId: null,
	pickPrefix: CORRIDOR_EVENTS_PICK_PREFIX,
	defaultOn: true,
	create: createCorridorEventsLayer,
};
