/**
 * vehicle-traces layer — earmarked vehicles with traces, camera hops and badges.
 *
 * Feed: the canvas store's vehicles (city registry vehicle entities, or
 * GET /api/v1/vehicles as the fallback). Traces come from the registry's
 * sighting entities — the snapshot carries the last 8 reads of every
 * FLAGGED vehicle — so a healthy city picture costs no per-vehicle request;
 * only when the registry does not serve sightings does the layer fall back
 * to fetchVehicleTrace (data.ts: the timed trace, else the last 8
 * sightings). Each trace draws as a polyline plus NUMBERED CAMERA HOPS
 * (1 = oldest read), each hop naming the ANPR reader that saw the plate —
 * the path an operator can follow.
 *
 * Earmark colors: watch → data accent, flagged → act orange, unearmarked →
 * dim ink. Every emulated read carries its source ('emulated_anpr' today)
 * and the marker label shows an EMULATED badge — the ethics rail (C6):
 * synthetic reads are always labelled; vehicles only, never persons; the
 * plate is MASKED (GJ27EM•••1) unless a named duty officer is on console.
 *
 * Pick ids: `veh:mark-<vehicleId>`, `veh:trace-<vehicleId>` → URN
 * `vehicle:<id>`; `veh:hop-<vehicleId>-<n>` → the hop's `sighting:<id>`
 * when the source names it, else the vehicle.
 *
 * Every time reads in IST (the city's clock, never the browser's zone or
 * an ISO-Z string). A vehicle without an active earmark has no lawful
 * basis for its individual reads (C6): the oracle withholds them, nothing
 * is drawn, and the chip hint says 'reads withheld: no active earmark
 * (C6)'. The pursuit overlay already labels the pursued vehicle's newest
 * sighting, so its marker here keeps the dot but drops the duplicate
 * plate label (two labels on one spot read as noise).
 *
 * DRAPED (layers/draping.ts): each trace is a ground polyline on terrain and
 * the photoreal 3D tiles, above the corridor spines; marks, hops and their
 * labels are clamped to the ground and never hidden by a building. Every
 * label stands on the translucent canvas plate; a hop label is the first to
 * give way in the canvas's declutter (after the vehicle's own mark label,
 * and under any e-challan / sighting pin at the same camera site).
 */

import { fetchVehicleTrace, type TraceHop, type VehicleRecord, type VehicleTrace } from '../data';
import { feedError, feedHint, godsEye } from '../store.svelte';
import { istDayTime, istStamp } from '../hud/format';
import { ops } from '$lib/ops/store.svelte';
import { pageHidden } from '$lib/ops/client';
import { displayPlate } from '../plates';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssVar, withAlpha } from '../theme';
import { DRAPE_Z, clampedDot, clampedLabel, drapeAudit, drapedPolyline, ground, groundPath } from './draping';

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

/** A read's time as the control room says it: HH:MM:SS IST today, '22 Oct 21:40 IST' before. */
function ist(t: number | null): string {
	return t != null ? `${istStamp(t)} IST` : '—';
}

/** An ISO date-time (retention_until) in IST, day included. */
function istIso(v: string | null): string {
	if (!v) return '—';
	const t = Date.parse(v);
	return Number.isFinite(t) ? `${istDayTime(t)} IST` : v;
}

function hopDescription(plate: string, h: TraceHop, of: number): string {
	return `hop ${h.seq}/${of} · ${plate} · camera ${h.cameraId ?? '—'} · ${ist(h.t)}${
		h.speedKmh != null ? ` · ${Math.round(h.speedKmh)} km/h` : ''
	} · source ${h.source ?? '—'}${h.source === 'emulated_anpr' ? ' (EMULATED)' : ''}`;
}

function createVehicleTracesLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	/** pick id → URN (hops name their sighting when the source does). */
	const urns = new Map<string, string>();
	let lastSignature = '';
	/** Bumped per redraw: an older async draw stops before adding anything. */
	let generation = 0;
	let tracedCount = 0;
	let hopCount = 0;
	let traceVia = 'none';
	/** Vehicles whose individual reads the oracle withheld (no active earmark, C6). */
	let withheldCount = 0;

	function clearEntities() {
		if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
		entityIds.length = 0;
		urns.clear();
	}

	function track(id: string, urn: string) {
		entityIds.push(id);
		urns.set(id, urn);
	}

	async function draw(): Promise<void> {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const vehicles = godsEye.vehicles;
		const officer = ops.officerOn;
		const registryTraces = new Map<string, VehicleTrace>();
		for (const v of vehicles) {
			const t = godsEye.registryTrace(v.vehicleId);
			if (t) registryTraces.set(v.vehicleId, t);
		}
		const sig = JSON.stringify([
			vehicles.map((v) => [
				v.vehicleId,
				v.earmarkState,
				v.lastSighting?.lon,
				v.lastSighting?.lat,
				v.lastSighting?.t,
				registryTraces.get(v.vehicleId)?.hops.map((h) => h.sightingId ?? h.t) ?? null,
			]),
			godsEye.vehiclesFeed.status,
			officer,
			ops.interception?.latestSighting ? ops.interception.vehicleId : null,
		]);
		if (sig === lastSignature) return;
		// Trace reads wait for a visible page; the next tick retries.
		if (pageHidden()) return;
		lastSignature = sig;
		const gen = ++generation;

		clearEntities();
		tracedCount = 0;
		hopCount = 0;
		withheldCount = 0;
		traceVia = registryTraces.size ? 'city' : 'none';
		const canvasInk = cesium.Color.fromCssColorString(cssVar('--v-canvas') ?? 'black');
		// The pursuit overlay labels this vehicle's newest sighting itself.
		const pursued = ops.interception?.latestSighting ? ops.interception.vehicleId : null;
		// A translucent canvas plate behind labels: legible over any basemap.
		const labelPlate = cesium.Color.fromCssColorString(withAlpha(cssVar('--v-canvas') ?? 'black', 0.72));

		for (const v of vehicles) {
			const color = earmarkColor(v.earmarkState);
			const badge = v.earmarkState === 'watch' ? 'WATCH' : v.earmarkState === 'flagged' ? 'FLAGGED' : '';
			const emulated = v.lastSighting?.source === 'emulated_anpr';
			const plate = displayPlate(v.plate, officer) ?? v.vehicleId.slice(0, 8);

			if (v.lastSighting) {
				const labelled = v.vehicleId !== pursued;
				const entity = viewer.entities.add({
					id: `${VEHICLE_TRACES_PICK_PREFIX}mark-${v.vehicleId}`,
					position: ground(cesium, v.lastSighting.lon, v.lastSighting.lat),
					billboard: clampedDot(cesium, {
						pixelSize: v.earmarkState === 'flagged' ? 12 : 8,
						color: cesium.Color.fromCssColorString(color),
						outlineColor: canvasInk,
						outlineWidth: 2,
					}),
					...(labelled
						? {
								label: clampedLabel(cesium, {
									text: `${plate}${badge ? ` ·${badge}` : ''}${emulated ? ' ·EMULATED' : ''}`,
									font: '13px monospace',
									fillColor: cesium.Color.fromCssColorString(cssVar('--v-ink-1') ?? 'white'),
									outlineColor: canvasInk,
									outlineWidth: 2,
									style: 2,
									showBackground: true,
									backgroundColor: labelPlate,
									backgroundPadding: new cesium.Cartesian2(5, 3),
									verticalOrigin: -1,
									pixelOffset: new cesium.Cartesian2(0, 12),
									// declutter: a watch plate reads up close, a flagged one from the city view
									distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, v.earmarkState === 'flagged' ? 30_000 : 12_000),
								}),
							}
						: {}),
					description: `vehicle ${plate} · earmark ${v.earmarkState ?? 'none'}${
						v.lawfulBasis ? ` · lawful basis ${v.lawfulBasis}` : ''
					} · retention until ${istIso(v.retentionUntil)} · last seen ${ist(v.lastSighting.t)} at camera ${
						v.lastSighting.cameraId ?? '—'
					}${v.lastSighting.hit ? ` · HIT ${v.lastSighting.hit}` : ''} · source ${v.lastSighting.source ?? '—'}${
						emulated ? ' (EMULATED)' : ''
					}${officer ? '' : ' · plate masked (no named duty officer)'}`,
				});
				track(entity.id, `vehicle:${v.vehicleId}`);
			}
		}

		// Traces + numbered camera hops for the most recent vehicles (bounded).
		const traced = vehicles
			.filter((v) => v.lastSighting)
			.sort((a, b) => (b.lastSighting?.t ?? 0) - (a.lastSighting?.t ?? 0))
			.slice(0, MAX_TRACES);
		for (const v of traced) {
			let trace = registryTraces.get(v.vehicleId) ?? null;
			if (!trace) {
				// The registry serves hops for FLAGGED vehicles only; a watch
				// vehicle (or a degraded registry) reads its own trace.
				try {
					trace = await fetchVehicleTrace(v.vehicleId, TRACE_WINDOW_SEC, AbortSignal.timeout(6000));
				} catch {
					continue; // trace + sightings both down — the marker stands alone
				}
				if (!ctx || !active || gen !== generation) return; // superseded mid-flight
				if (trace.via === 'withheld') {
					withheldCount++;
					continue; // C6: no lawful basis to disclose its reads — nothing to draw
				}
				if (traceVia === 'none') traceVia = trace.via;
			}
			if (trace.coordinates.length < 2) continue;
			const plate = displayPlate(v.plate, officer) ?? v.vehicleId.slice(0, 8);
			const color = cesium.Color.fromCssColorString(earmarkColor(v.earmarkState));
			const line = viewer.entities.add({
				id: `${VEHICLE_TRACES_PICK_PREFIX}trace-${v.vehicleId}`,
				polyline: drapedPolyline(cesium, {
					positions: groundPath(cesium, trace.coordinates),
					width: 3,
					material: color,
					z: DRAPE_Z.trace,
				}),
				description: `trace ${plate} · ${
					trace.via === 'trace' ? `window ${TRACE_WINDOW_SEC}s` : `last ${trace.hops.length} sightings`
				} · ${trace.hops.length} camera hops · newest ${ist(trace.latestT)}${trace.status ? ` · ${trace.status}` : ''}${
					trace.disclosure ? ` · disclosed under ${trace.disclosure.replace(/_/g, ' ')}` : ''
				} · source ${trace.source ?? '—'}${
					trace.source === 'emulated_anpr' ? ' (EMULATED)' : ''
				}`,
			});
			track(line.id, `vehicle:${v.vehicleId}`);
			for (const h of trace.hops) {
				const hop = viewer.entities.add({
					id: `${VEHICLE_TRACES_PICK_PREFIX}hop-${v.vehicleId}-${h.seq}`,
					position: ground(cesium, h.lon, h.lat),
					billboard: clampedDot(cesium, {
						pixelSize: 7,
						color,
						outlineColor: canvasInk,
						outlineWidth: 1,
					}),
					label: clampedLabel(cesium, {
						text: `${h.seq}${h.cameraId ? ` ${h.cameraId}` : ''}`,
						font: '12px monospace',
						fillColor: cesium.Color.fromCssColorString(cssVar('--v-ink-1') ?? 'white'),
						outlineColor: canvasInk,
						outlineWidth: 2,
						style: 2,
						// on the canvas plate (legible over the city and the lines through
						// it); the canvas's declutter hides it under an e-challan / sighting
						// pin standing on the same camera site — the pin names the site
						showBackground: true,
						backgroundColor: labelPlate,
						backgroundPadding: new cesium.Cartesian2(4, 2),
						verticalOrigin: 1, // label above the hop dot
						pixelOffset: new cesium.Cartesian2(0, -8),
						// hop numbers up close only (the overview keeps the trace line)
						distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, 4500),
					}),
					description: hopDescription(plate, h, trace.hops.length),
				});
				track(hop.id, h.sightingId ? `sighting:${h.sightingId}` : `vehicle:${v.vehicleId}`);
				hopCount++;
			}
			tracedCount++;
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
			generation++;
			if (timer) clearInterval(timer);
			timer = null;
			clearEntities();
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
		urnOf(pickId) {
			return urns.get(pickId) ?? null;
		},
		coverageStats() {
			return {
				// The chip counts VEHICLES on the canvas (each with its hops).
				count: godsEye.vehicles.filter((v) => v.lastSighting).length,
				loading: godsEye.vehiclesFeed.status === 'empty' && godsEye.vehiclesFeed.updatedAt === 0,
				hint:
					godsEye.vehiclesFeed.status === 'not-deployed'
						? '/api/v1/vehicles 404 — watchlist-anpr stream pending'
						: (feedHint(godsEye.vehiclesFeed) ??
							`${godsEye.vehicles.length} earmarked vehicles · ${tracedCount} traced · ${hopCount} camera hops (${traceVia})${
								withheldCount ? ` · ${withheldCount} reads withheld: no active earmark (C6)` : ''
							} · plates ${ops.officerOn ? 'shown to the duty officer' : 'masked'}`),
				error: feedError(godsEye.vehiclesFeed),
				detail: {
					vehicles: godsEye.vehicles.length,
					traced: tracedCount,
					hops: hopCount,
					withheld: withheldCount,
					...(ctx && entityIds.length ? drapeAudit(ctx.viewer, entityIds) : {}),
				},
				via: godsEye.via('vehicle'),
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
