/**
 * God's-eye canvas data client — every outbound call the canvas layers make.
 *
 * Same security contract as src/lib/ops/client.ts (contract C5): every URL
 * is a fixed constant from src/lib/ops/endpoints.ts or built from
 * `safePathId`-validated ids; nothing here accepts a caller-supplied URL.
 * 404 raises NotDeployed so a layer can render the honest "not deployed"
 * chip state instead of an error, and the path is not re-requested for
 * NOT_DEPLOYED_BACKOFF_MS (the per-path memory shared with the ops client).
 *
 * Wire shapes (traffic-oracle, verified against the live routes): every list
 * endpoint answers an ENVELOPE — {vehicles: []}, {junctions: []}, {units: []},
 * {cameras: []} — and the parsers keep accepting a bare array for older
 * builds. The pure `parse*` functions are exported for tests/contract/run.mjs.
 */

import {
	CAMERAS_API,
	INTERCEPTION_PLAN_PATH,
	SIGNALS_STATE_API,
	SPATIAL_SEGMENTS_API,
	UNITS_API,
	VEHICLE_SIGHTINGS_API,
	VEHICLE_TRACE_API,
	VEHICLES_API,
	safePathId,
} from '$lib/ops/endpoints';
import { SIGN_IN_REQUIRED, notDeployedBackoff } from '$lib/ops/client';

export class NotDeployed extends Error {
	constructor(
		public status: number,
		message: string,
	) {
		super(message);
		this.name = 'NotDeployed';
	}
}

export class Down extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'Down';
	}
}

/**
 * The oracle refused to disclose a vehicle's individual reads (WP-16 C6:
 * 404 'no lawful basis to disclose individual reads for this vehicle' —
 * the vehicle has no active earmark). Not "not deployed": the route is
 * there and answered; the path is never backed off for it.
 */
export class Withheld extends Error {
	constructor(public detail: string) {
		super(detail);
		this.name = 'Withheld';
	}
}

/** The C6 refusal sentence the oracle sends for an un-earmarked vehicle's reads. */
const NO_LAWFUL_BASIS = /no lawful basis/i;

/**
 * Concurrent identical GETs share ONE request (e.g. the traces layer and the
 * pursuit overlay asking for the same trace in the same tick): without this,
 * both are in flight before a 404 lands and the back-off cannot apply.
 */
const inFlight = new Map<string, Promise<unknown>>();

/**
 * Same-origin GET with the canvas contract: 404 → NotDeployed (and the
 * path backs off 60 s), any other failure → Down, concurrent identical
 * GETs coalesced. Exported for the city fusion client (city-api.ts) — the
 * URL must always be a fixed constant or built from validated ids.
 */
export function getJson<T>(url: string, timeoutMs = 8000, signal?: AbortSignal): Promise<T> {
	if (notDeployedBackoff.until(url)) {
		return Promise.reject(new NotDeployed(404, `endpoint not deployed (404, backing off): ${url.split('?')[0]}`));
	}
	let shared = inFlight.get(url);
	if (!shared) {
		shared = fetchJson(url, timeoutMs).finally(() => inFlight.delete(url));
		inFlight.set(url, shared);
	}
	if (!signal) return shared as Promise<T>;
	// A caller's own signal ends only ITS wait; the shared request runs on.
	return new Promise<T>((resolve, reject) => {
		const onAbort = () => reject(new Down(`aborted: ${url}`));
		if (signal.aborted) return onAbort();
		signal.addEventListener('abort', onAbort, { once: true });
		shared.then(
			(v) => {
				signal.removeEventListener('abort', onAbort);
				resolve(v as T);
			},
			(err) => {
				signal.removeEventListener('abort', onAbort);
				reject(err);
			},
		);
	});
}

async function fetchJson(url: string, timeoutMs: number): Promise<unknown> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const res = await fetch(url, { signal: ctrl.signal });
		if (res.status === 404) {
			const body = await res.text().catch(() => '');
			let detail = '';
			try {
				const d = (JSON.parse(body) as { detail?: unknown }).detail;
				detail = typeof d === 'string' ? d : '';
			} catch {
				// not JSON — a plain 404 page
			}
			// A C6 refusal answers from a deployed route: never backed off.
			if (NO_LAWFUL_BASIS.test(detail)) throw new Withheld(detail.slice(0, 160));
			notDeployedBackoff.remember(url);
			throw new NotDeployed(404, `endpoint not deployed: ${url}`);
		}
		if (res.status === 401 || res.status === 403) {
			// The deck's operator gate refused the read: sign in — the body
			// (the gate's own JSON) never reaches a panel note.
			await res.body?.cancel().catch(() => undefined);
			throw new Down(SIGN_IN_REQUIRED);
		}
		if (!res.ok) {
			const body = await res.text().catch(() => '');
			throw new Down(`${res.status} ${res.statusText}: ${body.slice(0, 160)}`);
		}
		return await res.json();
	} catch (err) {
		if (err instanceof NotDeployed || err instanceof Down || err instanceof Withheld) throw err;
		if (err instanceof DOMException && err.name === 'AbortError') throw new Down(`timeout: ${url}`);
		throw new Down(`${url}: ${err instanceof Error ? err.message : String(err)}`);
	} finally {
		clearTimeout(timer);
	}
}

/** Rows of an envelope ({key: [...]}) or of a bare array (older builds). */
function rowsOf(resp: unknown, key: string): Record<string, unknown>[] {
	const raw = Array.isArray(resp)
		? resp
		: resp && typeof resp === 'object' && Array.isArray((resp as Record<string, unknown>)[key])
			? ((resp as Record<string, unknown>)[key] as unknown[])
			: [];
	return raw.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object' && !Array.isArray(r));
}

/**
 * What a list endpoint actually answered, filled in by the fetchers when a
 * caller passes one: `total` rows on the wire, `dropped` rows the canvas
 * cannot place (e.g. a unit with no position yet). The store says "empty"
 * only when total is 0 — never because the parser dropped rows.
 */
export interface FeedStats {
	total: number;
	dropped: number;
}

export interface Parsed<T> {
	rows: T[];
	total: number;
}

function report<T>(parsed: Parsed<T>, stats?: FeedStats): T[] {
	if (stats) {
		stats.total = parsed.total;
		stats.dropped = parsed.total - parsed.rows.length;
	}
	return parsed.rows;
}

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length ? v : null;
}

/** ISO string (or epoch ms) → epoch ms; null when unparseable. */
function epochMs(v: unknown): number | null {
	if (typeof v === 'number' && Number.isFinite(v)) return v;
	if (typeof v === 'string') {
		const t = Date.parse(v);
		return Number.isFinite(t) ? t : null;
	}
	return null;
}

function lonLat(pos: unknown, lon: unknown, lat: unknown): [number, number] | null {
	if (Array.isArray(pos) && typeof pos[0] === 'number' && typeof pos[1] === 'number') {
		return [pos[0], pos[1]];
	}
	if (typeof lon === 'number' && typeof lat === 'number') return [lon, lat];
	return null;
}

// ─── Road segments (spine geometry, existing live endpoint) ─────────────────

export interface SegmentGeometry {
	segmentId: number;
	roadName: string | null;
	roadClass: string | null;
	/** GeoJSON LineString coordinates [lon, lat][]. */
	coordinates: Array<[number, number]>;
	/**
	 * What the drawn line is (contract C-K): 'osm_display' — traced on the
	 * real OpenStreetMap road (display geometry); 'schematic' — the seed's
	 * junction-to-junction chord, drawn dashed and said so. An oracle that
	 * predates C-K sends no basis: a segment with an osm_way_id is OSM,
	 * one without is schematic.
	 */
	geometryBasis: 'osm_display' | 'schematic';
}

/** Pure: the C-K geometry basis of one segment row (tolerates pre-C-K oracles). */
export function geometryBasisOf(row: { geometry_basis?: unknown; osm_way_id?: unknown }): 'osm_display' | 'schematic' {
	if (row.geometry_basis === 'osm_display' || row.geometry_basis === 'osm') return 'osm_display';
	if (row.geometry_basis === 'schematic') return 'schematic';
	return row.osm_way_id != null && row.osm_way_id !== '' ? 'osm_display' : 'schematic';
}

/** GET /api/v1/spatial/segments?bbox — verified live shape (segments[]). */
export async function fetchSegments(
	bbox: { minLon: number; minLat: number; maxLon: number; maxLat: number },
	signal?: AbortSignal,
): Promise<SegmentGeometry[]> {
	const resp = await getJson<{
		segments?: Array<{
			segment_id?: number;
			road_name?: string | null;
			road_class?: string | null;
			osm_way_id?: unknown;
			geometry_basis?: unknown;
			geometry?: { type?: string; coordinates?: unknown } | null;
		}>;
	}>(
		`${SPATIAL_SEGMENTS_API}?min_lon=${bbox.minLon}&min_lat=${bbox.minLat}` +
			`&max_lon=${bbox.maxLon}&max_lat=${bbox.maxLat}`,
		10000,
		signal,
	);
	const out: SegmentGeometry[] = [];
	for (const s of resp.segments ?? []) {
		if (typeof s.segment_id !== 'number') continue;
		const raw = Array.isArray(s.geometry?.coordinates) ? (s.geometry?.coordinates as unknown[]) : [];
		const coordinates: Array<[number, number]> = [];
		for (const c of raw) {
			if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') {
				coordinates.push([c[0], c[1]]);
			}
		}
		out.push({
			segmentId: s.segment_id,
			roadName: typeof s.road_name === 'string' ? s.road_name : null,
			roadClass: typeof s.road_class === 'string' ? s.road_class : null,
			coordinates,
			geometryBasis: geometryBasisOf(s),
		});
	}
	return out;
}

// ─── Vehicles (watchlist-anpr; GET /vehicles → {vehicles: [...]}) ───────────

export interface VehicleRecord {
	vehicleId: string;
	plate: string | null;
	earmarkState: 'watch' | 'flagged' | null;
	retentionUntil: string | null;
	/** The earmark's enumerated lawful basis (C6), when the source carries it. */
	lawfulBasis?: string | null;
	lastSighting: {
		/** Epoch ms of the sighting (wire: ISO string). */
		t: number | null;
		lon: number;
		lat: number;
		source: string | null;
		cameraId: string | null;
		/** stolen_vehicle | pending_fine | watchlist when the read hit a list. */
		hit: string | null;
	} | null;
}

export function parseVehicles(resp: unknown): Parsed<VehicleRecord> {
	const rows = rowsOf(resp, 'vehicles');
	const out: VehicleRecord[] = [];
	for (const r of rows) {
		const id = r.vehicle_id ?? r.id;
		if (typeof id !== 'string' && typeof id !== 'number') continue;
		const state = r.earmark_state ?? r.earmarkState;
		const sight = (r.last_sighting ?? r.lastSighting) as Record<string, unknown> | null | undefined;
		let lastSighting: VehicleRecord['lastSighting'] = null;
		if (sight && typeof sight === 'object') {
			const pos = lonLat(sight.position, sight.lon, sight.lat);
			if (pos) {
				lastSighting = {
					t: epochMs(sight.t),
					lon: pos[0],
					lat: pos[1],
					source: str(sight.source),
					cameraId: str(sight.camera_id),
					hit: str(sight.matched_plate_hit),
				};
			}
		}
		out.push({
			vehicleId: String(id),
			plate: typeof r.plate === 'string' ? r.plate : null,
			earmarkState: state === 'watch' || state === 'flagged' ? (state as 'watch' | 'flagged') : null,
			retentionUntil: typeof r.retention_until === 'string' ? r.retention_until : null,
			lastSighting,
		});
	}
	return { rows: out, total: rows.length };
}

export async function fetchVehicles(signal?: AbortSignal, stats?: FeedStats): Promise<VehicleRecord[]> {
	return report(parseVehicles(await getJson<unknown>(VEHICLES_API, 8000, signal)), stats);
}

/** One camera hop of a trace: where (and through which reader) it was seen. */
export interface TraceHop {
	/** 1-based, chronological (1 = oldest read in the trace). */
	seq: number;
	t: number | null;
	lon: number;
	lat: number;
	cameraId: string | null;
	speedKmh: number | null;
	source: string | null;
	/** The sighting behind the hop, when the source names it (city registry). */
	sightingId?: string | null;
}

export interface VehicleTrace {
	vehicleId: string;
	/** Polyline oldest → newest. Empty when the vehicle has no sightings. */
	coordinates: Array<[number, number]>;
	source: string | null;
	/** Camera hops in the same order as `coordinates`. */
	hops: TraceHop[];
	/**
	 * Which route answered: the city registry, the timed trace, the
	 * sightings fallback, none — or 'withheld': the oracle has no lawful
	 * basis to disclose this vehicle's individual reads (no active earmark,
	 * C6), so nothing is drawn and the hint says why.
	 */
	via: 'city' | 'trace' | 'sightings' | 'none' | 'withheld';
	/** Epoch ms of the newest point, when any. */
	latestT: number | null;
	/** The trace's own status ('tracking' | 'quiet'), when the oracle reports it. */
	status?: string | null;
	/** Why the reads may be disclosed (e.g. 'active_earmark'), when reported. */
	disclosure?: string | null;
}

/** How many recent sightings the fallback polyline uses. */
export const TRACE_FALLBACK_SIGHTINGS = 8;

function hopsFrom(rows: Record<string, unknown>[]): TraceHop[] {
	const hops: TraceHop[] = [];
	for (const r of rows) {
		const pos = lonLat(r.position, r.lon, r.lat);
		if (!pos) continue;
		hops.push({
			seq: 0,
			t: epochMs(r.t),
			lon: pos[0],
			lat: pos[1],
			cameraId: str(r.camera_id),
			speedKmh: num(r.speed_kmh),
			source: str(r.source),
		});
	}
	// Chronological (oldest first); undated rows sort first (stable sort).
	hops.sort((a, b) => (a.t ?? 0) - (b.t ?? 0));
	hops.forEach((h, i) => (h.seq = i + 1));
	return hops;
}

/** Pure parser for GET /vehicles/{id}/trace (exported for the contract tests). */
export function parseTrace(vehicleId: string, resp: unknown): VehicleTrace {
	const body = (resp ?? {}) as Record<string, unknown>;
	const geometry = (body.geometry ?? {}) as Record<string, unknown>;
	const raw = Array.isArray(geometry.coordinates) ? (geometry.coordinates as unknown[]) : [];
	const coordinates: Array<[number, number]> = [];
	for (const c of raw) {
		if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') {
			coordinates.push([c[0], c[1]]);
		}
	}
	const hops = hopsFrom(rowsOf(body.positions, 'positions'));
	return {
		vehicleId,
		coordinates,
		source: str(body.source),
		hops,
		via: coordinates.length ? 'trace' : 'none',
		latestT: hops.length ? hops[hops.length - 1].t : epochMs(body.latest_t),
		status: str(body.status),
		disclosure: str(body.disclosure),
	};
}

/** The empty trace of a vehicle whose reads the oracle may not disclose (C6). */
export function withheldTrace(vehicleId: string): VehicleTrace {
	return { vehicleId, coordinates: [], source: null, hops: [], via: 'withheld', latestT: null, status: null, disclosure: null };
}

/**
 * Pure parser for GET /vehicles/{id}/sightings (newest first on the wire):
 * the most recent `limit` reads become the polyline + numbered hops.
 */
export function parseSightingsTrace(vehicleId: string, resp: unknown, limit = TRACE_FALLBACK_SIGHTINGS): VehicleTrace {
	const body = (resp ?? {}) as Record<string, unknown>;
	const newestFirst = rowsOf(body.sightings ?? resp, 'sightings')
		.slice()
		.sort((a, b) => (epochMs(b.t) ?? 0) - (epochMs(a.t) ?? 0))
		.slice(0, Math.max(1, limit));
	const hops = hopsFrom(newestFirst);
	return {
		vehicleId,
		coordinates: hops.map((h): [number, number] => [h.lon, h.lat]),
		source: str(body.source) ?? hops[hops.length - 1]?.source ?? null,
		hops,
		via: hops.length ? 'sightings' : 'none',
		latestT: hops.length ? hops[hops.length - 1].t : null,
	};
}

/**
 * The recent movement polyline for one vehicle.
 *
 * Ladder: GET /vehicles/{id}/trace?window_sec= first. A 404 ('no sightings
 * for vehicle' on today's oracle) or a 200 with empty geometry (WP-03's
 * no-sightings-in-window answer) is NOT "not deployed" — the vehicle simply
 * has no reads in that window — so the ladder falls back to the last
 * TRACE_FALLBACK_SIGHTINGS reads from GET /vehicles/{id}/sightings for the
 * polyline and its camera hops. Both empty → an empty trace (never thrown).
 * Only a transport failure on both rungs throws Down.
 */
export async function fetchVehicleTrace(
	vehicleId: string,
	windowSec = 900,
	signal?: AbortSignal,
): Promise<VehicleTrace> {
	const id = safePathId(vehicleId);
	if (!id) throw new Down(`invalid vehicle id: ${vehicleId}`);
	let traceErr: unknown = null;
	let quiet: VehicleTrace | null = null;
	try {
		const trace = parseTrace(
			id,
			await getJson<unknown>(
				`${VEHICLE_TRACE_API}/${encodeURIComponent(id)}/trace?window_sec=${windowSec}`,
				8000,
				signal,
			),
		);
		if (trace.coordinates.length >= 2) return trace;
		quiet = trace; // an earmarked vehicle quiet in the window: keep its status/disclosure
	} catch (err) {
		// C6: no lawful basis to disclose this vehicle's reads — the sightings
		// route answers the same; nothing to fall back to, nothing backed off.
		if (err instanceof Withheld) return withheldTrace(id);
		if (!(err instanceof NotDeployed)) traceErr = err;
	}
	try {
		const fallback = parseSightingsTrace(
			id,
			await getJson<unknown>(
				`${VEHICLE_SIGHTINGS_API}/${encodeURIComponent(id)}/sightings?limit=${TRACE_FALLBACK_SIGHTINGS}`,
				8000,
				signal,
			),
		);
		return quiet ? { ...fallback, status: quiet.status, disclosure: quiet.disclosure } : fallback;
	} catch (err) {
		if (err instanceof Withheld) return withheldTrace(id);
		if (err instanceof NotDeployed) {
			return quiet ?? { vehicleId: id, coordinates: [], source: null, hops: [], via: 'none', latestT: null };
		}
		throw traceErr ?? err;
	}
}

// ─── Signals (signalling; GET /signals/state → {junctions: [...]}) ──────────

export interface SignalStateRecord {
	junctionId: string;
	lon: number;
	lat: number;
	cycleSec: number | null;
	offsetSec: number | null;
	/**
	 * Phase label: 'φ{index} {movements}' while a green runs, prefixed
	 * 'lost' while the cycle sits in amber/all-red lost time; null when the
	 * program has no green at all.
	 */
	activePhase: string | null;
	phaseIndex: number | null;
	phaseMovements: string[];
	/** True while the cycle position is in lost time (amber/all-red). */
	phaseLost: boolean | null;
	/** Green seconds of the active phase (its share of the cycle). */
	greenSec: number | null;
	greenRatio: number | null;
	source: string | null;
	/**
	 * Per-movement light at the oracle's phase clock (WP-03 additive
	 * `active_phase.movement_states`: through/cross/ramp → green|amber|red);
	 * empty on older oracles. An amber movement draws the amber ring.
	 */
	movementStates: Record<string, 'green' | 'amber' | 'red'>;
	/** Seconds left in the running phase, when the oracle reports it. */
	remainingSec: number | null;
	/** Junction name (migration 012), when known. */
	name: string | null;
}

function phaseLabel(index: number | null, movements: string[], lost: boolean | null): string | null {
	if (index == null && !movements.length) return lost ? 'lost' : null;
	const head = `φ${index ?? '?'}${movements.length ? ` ${movements.join('+')}` : ''}`;
	return lost ? `lost · after ${head}` : head;
}

function movementStatesOf(v: unknown): Record<string, 'green' | 'amber' | 'red'> {
	const out: Record<string, 'green' | 'amber' | 'red'> = {};
	if (!v || typeof v !== 'object' || Array.isArray(v)) return out;
	for (const [k, s] of Object.entries(v as Record<string, unknown>)) {
		if (s === 'green' || s === 'amber' || s === 'red') out[k] = s;
	}
	return out;
}

/**
 * One junction's phase block (wire `active_phase`, the /signals/state and
 * city-entity junction shape alike) → the record's phase fields.
 */
export function phaseFields(ap: unknown): Pick<
	SignalStateRecord,
	'activePhase' | 'phaseIndex' | 'phaseMovements' | 'phaseLost' | 'greenSec' | 'movementStates' | 'remainingSec'
> {
	if (ap && typeof ap === 'object' && !Array.isArray(ap)) {
		const p = ap as Record<string, unknown>;
		const phaseIndex = num(p.index);
		const phaseMovements = Array.isArray(p.movements)
			? p.movements.filter((m): m is string => typeof m === 'string')
			: [];
		const phaseLost = typeof p.lost === 'boolean' ? p.lost : null;
		return {
			activePhase: phaseLabel(phaseIndex, phaseMovements, phaseLost),
			phaseIndex,
			phaseMovements,
			phaseLost,
			greenSec: num(p.green_sec),
			movementStates: movementStatesOf(p.movement_states),
			remainingSec: num(p.remaining_sec),
		};
	}
	return {
		activePhase: typeof ap === 'string' ? ap : null, // legacy string label
		phaseIndex: null,
		phaseMovements: [],
		phaseLost: null,
		greenSec: null,
		movementStates: {},
		remainingSec: null,
	};
}

export function parseSignalsState(resp: unknown): Parsed<SignalStateRecord> {
	const rows = rowsOf(resp, 'junctions');
	const out: SignalStateRecord[] = [];
	for (const r of rows) {
		const id = r.junction_id ?? r.id;
		const lon = r.lon ?? r.lng;
		const lat = r.lat;
		if (typeof id !== 'string' && typeof id !== 'number') continue;
		if (typeof lon !== 'number' || typeof lat !== 'number') continue;
		out.push({
			junctionId: String(id),
			lon,
			lat,
			cycleSec: num(r.cycle_sec),
			offsetSec: num(r.offset_sec),
			...phaseFields(r.active_phase),
			greenRatio: num(r.green_ratio),
			source: str(r.source),
			name: str(r.name),
		});
	}
	return { rows: out, total: rows.length };
}

export async function fetchSignalsState(signal?: AbortSignal, stats?: FeedStats): Promise<SignalStateRecord[]> {
	return report(parseSignalsState(await getJson<unknown>(SIGNALS_STATE_API, 8000, signal)), stats);
}

// ─── Agency units (GET /units → {units: [...]}) ─────────────────────────────
//
// Two producers share the route: dispatch projections (units/router.py —
// position [lon, lat], status, eta_sec, progress, arrived, source
// 'emulated_dispatch_projection') and, with contract C-A, emulated patrols
// from Valkey (lon/lat, status patrol|available|…, source 'emulated_patrol').
// Every unit today is emulated; the badge is never optional (C6).

export interface AgencyUnitRecord {
	unitId: string;
	agency: string;
	label: string | null;
	lon: number;
	lat: number;
	assignedEventId: string | null;
	emulated: boolean;
	/** Lifecycle: requested | approved | en_route | on_scene | patrol | … */
	status: string | null;
	/** Seconds to the waypoint (0 once arrived), when projected. */
	etaSec: number | null;
	/** 0..1 along the projected path, when projected. */
	progress: number | null;
	arrived: boolean | null;
	speedKmh: number | null;
	headingDeg: number | null;
	source: string | null;
	dispatchId: string | null;
}

export function parseUnits(resp: unknown): Parsed<AgencyUnitRecord> {
	const rows = rowsOf(resp, 'units');
	const out: AgencyUnitRecord[] = [];
	for (const r of rows) {
		const id = r.unit_id ?? r.id;
		if (typeof id !== 'string' && typeof id !== 'number') continue;
		// Units without a position (no waypoint/approval yet) are real but
		// cannot be placed; they count in `total`, not in the drawn rows.
		const pos = lonLat(r.position, r.lon, r.lat);
		if (!pos) continue;
		const progress = num(r.progress);
		out.push({
			unitId: String(id),
			agency: typeof r.agency === 'string' ? r.agency : 'unknown',
			label: typeof r.label === 'string' ? r.label : null,
			lon: pos[0],
			lat: pos[1],
			assignedEventId: typeof r.assigned_event_id === 'string' ? r.assigned_event_id : null,
			emulated:
				r.emulated === true ||
				(typeof r.source === 'string' && r.source.startsWith('emulated')),
			status: str(r.status),
			etaSec: num(r.eta_sec),
			progress: progress == null ? null : Math.max(0, Math.min(1, progress)),
			arrived: typeof r.arrived === 'boolean' ? r.arrived : null,
			speedKmh: num(r.speed_kmh),
			headingDeg: num(r.heading ?? r.heading_deg),
			source: str(r.source),
			dispatchId: str(r.dispatch_id),
		});
	}
	return { rows: out, total: rows.length };
}

export async function fetchUnits(signal?: AbortSignal, stats?: FeedStats): Promise<AgencyUnitRecord[]> {
	return report(parseUnits(await getJson<unknown>(UNITS_API, 8000, signal)), stats);
}

// ─── Cameras (C-C; GET /cameras → {cameras: [...]}) ─────────────────────────
//
// Locations and read COUNTS only — the camera registry never carries frame,
// image or plate data (ethics rail C6); the UI badges every marker with its
// source.

export interface CameraRecord {
	cameraId: string;
	/** Registry label (e.g. 'Relief Road: east head'), when the registry names it. */
	label: string | null;
	lon: number;
	lat: number;
	kind: 'anpr' | 'cctv';
	source: 'osm' | 'emulated' | 'registered' | 'other';
	junctionId: number | null;
	headingDeg: number | null;
	status: string | null;
	/** ANPR reads in the last 15 min (0 when the producer sends none). */
	reads15m: number;
	/** Watchlist hits in the last 15 min — a hit pulses the marker. */
	hits15m: number;
	lastReadT: number | null;
}

export function parseCameras(resp: unknown): Parsed<CameraRecord> {
	const rows = rowsOf(resp, 'cameras');
	const out: CameraRecord[] = [];
	for (const r of rows) {
		const id = r.camera_id ?? r.id;
		const lon = r.lon ?? r.lng;
		const lat = r.lat;
		if (typeof id !== 'string' && typeof id !== 'number') continue;
		if (typeof lon !== 'number' || typeof lat !== 'number') continue;
		const kind = r.kind === 'cctv' ? 'cctv' : 'anpr';
		const source: CameraRecord['source'] =
			r.source === 'osm'
				? 'osm'
				: r.source === 'emulated'
					? 'emulated'
					: r.source === 'registered'
						? 'registered'
						: 'other';
		out.push({
			cameraId: String(id),
			label: str(r.label),
			lon,
			lat,
			kind,
			source,
			junctionId: num(r.junction_id),
			headingDeg: num(r.heading_deg),
			status: str(r.status),
			reads15m: Math.max(0, num(r.reads_15m) ?? 0),
			hits15m: Math.max(0, num(r.hits_15m) ?? 0),
			lastReadT: epochMs(r.last_read_t),
		});
	}
	return { rows: out, total: rows.length };
}

export async function fetchCameras(signal?: AbortSignal, stats?: FeedStats): Promise<CameraRecord[]> {
	return report(parseCameras(await getJson<unknown>(CAMERAS_API, 8000, signal)), stats);
}

// ─── Interception detail (GET /events/{id}/interception-plan, WP-03) ────────
//
// The ops store keeps the ranked chokes it needs for the board; the canvas
// and the dossier also need the plan's HONESTY fields — whether the latest
// sighting is stale, which junctions the vehicle has likely already passed,
// and how the direction of travel was decided (heading / one-way / unknown,
// observed against a one-way) with the legal onward movements. Read-only,
// fetched only when the board's plan changes.

export interface InterceptionChokeDetail {
	rank: number;
	junctionId: number;
	name: string | null;
	junctionType: string;
	signalControl: boolean;
	position: [number, number];
	graphCostSec: number | null;
	/** The vehicle would already have reached it (graph time < sighting age). */
	likelyPassed: boolean;
	/** One of the junctions the sighted segment leads to. */
	downstream: boolean;
	score: number | null;
}

export interface InterceptionDetail {
	eventId: string;
	vehicleId: string | null;
	sightingId: string | null;
	cameraId: string | null;
	sightingAgeSec: number | null;
	/** Older than staleAfterSec: the vehicle may be beyond every junction listed. */
	stale: boolean;
	staleAfterSec: number | null;
	seedSegmentId: number | null;
	direction: {
		/** heading | oneway | unknown | dead_end */
		basis: string | null;
		towardsJunctionIds: number[];
		segmentBearingDeg: number | null;
		againstOneway: boolean;
		onwardSegmentIds: number[];
	} | null;
	chokes: InterceptionChokeDetail[];
	note: string | null;
}

/** Pure parser (exported for the contract tests). */
export function parseInterceptionDetail(eventId: string, resp: unknown): InterceptionDetail {
	const body = (resp ?? {}) as Record<string, unknown>;
	const sight = (body.latest_sighting ?? null) as Record<string, unknown> | null;
	const dir = body.direction_of_travel as Record<string, unknown> | null | undefined;
	const ints = (v: unknown): number[] =>
		Array.isArray(v) ? v.filter((n): n is number => typeof n === 'number' && Number.isFinite(n)) : [];
	const chokes: InterceptionChokeDetail[] = [];
	const rows = Array.isArray(body.interception_junctions) ? (body.interception_junctions as unknown[]) : [];
	rows.forEach((j, i) => {
		const r = (j ?? {}) as Record<string, unknown>;
		const pos = lonLat(r.position, r.lon, r.lat);
		if (typeof r.junction_id !== 'number' || !pos) return;
		chokes.push({
			rank: i + 1,
			junctionId: r.junction_id,
			name: str(r.name),
			junctionType: str(r.junction_type) ?? 'junction',
			signalControl: r.signal_control === true,
			position: pos,
			graphCostSec: num(r.graph_cost_sec),
			likelyPassed: r.likely_passed === true,
			downstream: r.downstream === true,
			score: num(r.score),
		});
	});
	return {
		eventId,
		vehicleId: str(body.vehicle_id),
		sightingId: sight ? str(sight.sighting_id) : null,
		cameraId: sight ? str(sight.camera_id) : null,
		sightingAgeSec: num(body.sighting_age_sec),
		stale: body.stale === true,
		staleAfterSec: num(body.stale_after_sec),
		seedSegmentId: num(body.seed_segment_id),
		direction:
			dir && typeof dir === 'object'
				? {
						basis: str(dir.basis),
						towardsJunctionIds: ints(dir.towards_junction_ids),
						segmentBearingDeg: num(dir.segment_bearing_deg),
						againstOneway: dir.against_oneway === true,
						onwardSegmentIds: ints(dir.onward_segment_ids),
					}
				: null,
		chokes,
		note: str(body.note),
	};
}

/**
 * GET /api/v1/events/{id}/interception-plan — the same plan the board
 * polls (same horizon), read for its honesty fields. 404 (not a pursuit,
 * or no sightings) → NotDeployed; never thrown past the caller's catch.
 */
export async function fetchInterceptionDetail(eventId: string, signal?: AbortSignal): Promise<InterceptionDetail> {
	const id = safePathId(eventId);
	if (!id) throw new Down(`invalid event id: ${eventId}`);
	return parseInterceptionDetail(
		id,
		await getJson<unknown>(`${INTERCEPTION_PLAN_PATH(id)}?max_cost_sec=600`, 12000, signal),
	);
}
