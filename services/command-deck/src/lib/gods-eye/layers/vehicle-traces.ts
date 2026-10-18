/**
 * vehicle-traces layer — earmarked vehicles with traces and badges.
 *
 * Feed: GET /api/v1/vehicles (canvas store) + per-vehicle
 * GET /api/v1/vehicles/{id}/trace for the recent movement polyline.
 * Earmark colors: watch → amber, flagged → red, unearmarked → slate.
 * Every emulated trace carries its source ('emulated_anpr' today) and the
 * marker label shows an EMULATED badge — the ethics rail (C6): synthetic
 * reads are always labelled. Endpoint 404 → honest "not deployed" chip.
 */

import { fetchVehicleTrace } from '../data';
import { godsEye } from '../store.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssVar } from '../theme';

export const VEHICLE_TRACES_PICK_PREFIX = 'veh:';

/**
 * Earmark colours from the tokens (CSS colour strings for
 * fromCssColorString call sites): a FLAGGED plate is a human-must-act
 * surface (act orange); WATCH sits at the data accent — neither competes
 * with the band ladder's amber. No hex: theme.cssVar reads live tokens,
 * CSS colour-keyword fallbacks only.
 */
function earmarkColor(state: 'watch' | 'flagged' | null): string {
	if (state === 'flagged') return cssVar('--v-act') ?? 'orange';
	if (state === 'watch') return cssVar('--v-data') ?? 'cyan';
	return cssVar('--v-ink-3') ?? 'lightgray';
}

const MAX_TRACES = 12; // trace fetches per refresh — bounded on purpose
const TRACE_WINDOW_SEC = 900;

function createVehicleTracesLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';

	async function draw(): Promise<void> {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const vehicles = godsEye.vehicles;
		const sig = JSON.stringify([
			vehicles.map((v) => [v.vehicleId, v.earmarkState, v.lastSighting?.lon, v.lastSighting?.lat]),
			godsEye.vehiclesFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		for (const v of vehicles) {
			const color = earmarkColor(v.earmarkState);
			const badge = v.earmarkState === 'watch' ? 'WATCH' : v.earmarkState === 'flagged' ? 'FLAGGED' : '';
			const emulated = v.lastSighting?.source === 'emulated_anpr';

			if (v.lastSighting) {
				const entity = viewer.entities.add({
					id: `${VEHICLE_TRACES_PICK_PREFIX}mark-${v.vehicleId}`,
					position: cesium.Cartesian3.fromDegrees(v.lastSighting.lon, v.lastSighting.lat, 10),
					point: {
						pixelSize: v.earmarkState === 'flagged' ? 11 : 8,
						color: cesium.Color.fromCssColorString(color),
						outlineColor: cesium.Color.fromCssColorString(cssVar('--v-canvas') ?? 'black'),
						outlineWidth: 2,
					},
					label: {
						text: `${v.plate ?? v.vehicleId}${badge ? ` ·${badge}` : ''}${emulated ? ' ·EMULATED' : ''}`,
						font: '12px monospace',
						fillColor: cesium.Color.fromCssColorString(cssVar('--v-ink-1') ?? 'white'),
						outlineColor: cesium.Color.fromCssColorString(cssVar('--v-canvas') ?? 'black'),
						outlineWidth: 2,
						style: 2,
						verticalOrigin: -1,
						pixelOffset: new cesium.Cartesian2(0, 12),
					},
					description: `vehicle ${v.vehicleId} · plate ${v.plate ?? '—'} · earmark ${
						v.earmarkState ?? 'none'
					} · retention ${v.retentionUntil ?? '—'} · source ${v.lastSighting.source ?? '—'}`,
				});
				entityIds.push(entity.id);
			}
		}

		// Traces for the most recent vehicles (bounded).
		const traced = vehicles
			.filter((v) => v.lastSighting)
			.sort((a, b) => (b.lastSighting?.t ?? 0) - (a.lastSighting?.t ?? 0))
			.slice(0, MAX_TRACES);
		for (const v of traced) {
			try {
				const trace = await fetchVehicleTrace(v.vehicleId, TRACE_WINDOW_SEC, AbortSignal.timeout(6000));
				if (!ctx || !active || trace.coordinates.length < 2) continue;
				const positions: unknown[] = [];
				for (const [lon, lat] of trace.coordinates) {
					positions.push(cesium.Cartesian3.fromDegrees(lon, lat, 6));
				}
				const entity = viewer.entities.add({
					id: `${VEHICLE_TRACES_PICK_PREFIX}trace-${v.vehicleId}`,
					polyline: {
						positions,
						width: 2,
						material: cesium.Color.fromCssColorString(earmarkColor(v.earmarkState)),
					},
					description: `trace ${v.vehicleId} · window ${TRACE_WINDOW_SEC}s · source ${
						trace.source ?? '—'
					}${trace.source === 'emulated_anpr' ? ' (EMULATED)' : ''}`,
				});
				entityIds.push(entity.id);
			} catch {
				// trace endpoint absent (404) or down — markers stand alone
			}
		}
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
			void draw();
			timer = setInterval(() => void draw(), 4000);
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
			void godsEye.pollVehicles();
			void draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(VEHICLE_TRACES_PICK_PREFIX);
		},
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.vehiclesFeed.status === 'empty' && godsEye.vehiclesFeed.updatedAt === 0,
				hint:
					godsEye.vehiclesFeed.status === 'not-deployed'
						? '/api/v1/vehicles 404 — watchlist-anpr stream pending'
						: `${godsEye.vehicles.length} vehicles`,
				error: godsEye.vehiclesFeed.note,
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const vehicleTracesLayer: GodsEyeLayer = {
	id: 'traces',
	name: 'Vehicle traces + earmarks',
	label: 'TRACES',
	requiresKeyId: null,
	pickPrefix: VEHICLE_TRACES_PICK_PREFIX,
	defaultOn: true,
	create: createVehicleTracesLayer,
};
