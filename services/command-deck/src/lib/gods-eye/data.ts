/**
 * God's-eye canvas data client — every outbound call the canvas layers make.
 *
 * Same security contract as src/lib/ops/client.ts (contract C5): every URL
 * is a fixed constant from src/lib/ops/endpoints.ts or built from
 * `safePathId`-validated ids; nothing here accepts a caller-supplied URL.
 * 404 raises NotDeployed so a layer can render the honest "not deployed"
 * chip state instead of an error — the backend streams (vehicles, signals,
 * units, cameras) land their endpoints later and the layers light up.
 */

import {
	CAMERAS_API,
	SIGNALS_STATE_API,
	SPATIAL_SEGMENTS_API,
	UNITS_API,
	VEHICLE_TRACE_API,
	VEHICLES_API,
	safePathId,
} from '$lib/ops/endpoints';

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

async function getJson<T>(url: string, timeoutMs = 8000, signal?: AbortSignal): Promise<T> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	if (signal) signal.addEventListener('abort', () => ctrl.abort(), { once: true });
	try {
		const res = await fetch(url, { signal: ctrl.signal });
		if (res.status === 404) throw new NotDeployed(404, `endpoint not deployed: ${url}`);
		if (!res.ok) {
			const body = await res.text().catch(() => '');
			throw new Down(`${res.status} ${res.statusText}: ${body.slice(0, 160)}`);
		}
		return (await res.json()) as T;
	} catch (err) {
		if (err instanceof NotDeployed || err instanceof Down) throw err;
		if (err instanceof DOMException && err.name === 'AbortError') throw new Down(`timeout: ${url}`);
		throw new Down(`${url}: ${err instanceof Error ? err.message : String(err)}`);
	} finally {
		clearTimeout(timer);
	}
}

// ─── Road segments (spine geometry, existing live endpoint) ─────────────────

export interface SegmentGeometry {
	segmentId: number;
	roadName: string | null;
	roadClass: string | null;
	/** GeoJSON LineString coordinates [lon, lat][]. */
	coordinates: Array<[number, number]>;
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
		});
	}
	return out;
}

// ─── Vehicles (contract C1; watchlist-anpr stream; 404 until deployed) ──────

export interface VehicleRecord {
	vehicleId: string;
	plate: string | null;
	earmarkState: 'watch' | 'flagged' | null;
	retentionUntil: string | null;
	lastSighting: { t: number | null; lon: number; lat: number; source: string | null } | null;
}

export async function fetchVehicles(signal?: AbortSignal): Promise<VehicleRecord[]> {
	const resp = await getJson<Record<string, unknown>[]>(VEHICLES_API, 8000, signal);
	const rows = Array.isArray(resp) ? resp : [];
	return rows
		.map((r): VehicleRecord | null => {
			const id = r.vehicle_id ?? r.id;
			if (typeof id !== 'string' && typeof id !== 'number') return null;
			const state = r.earmark_state ?? r.earmarkState;
			const sight = (r.last_sighting ?? r.lastSighting) as Record<string, unknown> | null | undefined;
			let lastSighting: VehicleRecord['lastSighting'] = null;
			if (sight && typeof sight === 'object') {
				const pos = sight.position as unknown;
				if (Array.isArray(pos) && typeof pos[0] === 'number' && typeof pos[1] === 'number') {
					lastSighting = {
						t: typeof sight.t === 'number' ? sight.t : null,
						lon: pos[0],
						lat: pos[1],
						source: typeof sight.source === 'string' ? sight.source : null,
					};
				}
			}
			return {
				vehicleId: String(id),
				plate: typeof r.plate === 'string' ? r.plate : null,
				earmarkState:
					state === 'watch' || state === 'flagged' ? (state as 'watch' | 'flagged') : null,
				retentionUntil: typeof r.retention_until === 'string' ? r.retention_until : null,
				lastSighting,
			};
		})
		.filter((v): v is VehicleRecord => v !== null);
}

export interface VehicleTrace {
	vehicleId: string;
	coordinates: Array<[number, number]>;
	source: string | null;
}

/** GET /api/v1/vehicles/{id}/trace?window_sec= — GeoJSON LineString + times. */
export async function fetchVehicleTrace(
	vehicleId: string,
	windowSec = 900,
	signal?: AbortSignal,
): Promise<VehicleTrace> {
	const id = safePathId(vehicleId);
	if (!id) throw new Down(`invalid vehicle id: ${vehicleId}`);
	const resp = await getJson<Record<string, unknown>>(
		`${VEHICLE_TRACE_API}/${encodeURIComponent(id)}/trace?window_sec=${windowSec}`,
		8000,
		signal,
	);
	const geometry = (resp.geometry ?? {}) as Record<string, unknown>;
	const raw = Array.isArray(geometry.coordinates) ? (geometry.coordinates as unknown[]) : [];
	const coordinates: Array<[number, number]> = [];
	for (const c of raw) {
		if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') {
			coordinates.push([c[0], c[1]]);
		}
	}
	return {
		vehicleId: id,
		coordinates,
		source: typeof resp.source === 'string' ? resp.source : null,
	};
}

// ─── Signals (contract C1; signalling stream; 404 until deployed) ───────────

export interface SignalStateRecord {
	junctionId: string;
	lon: number;
	lat: number;
	cycleSec: number | null;
	activePhase: string | null;
	greenRatio: number | null;
	source: string | null;
}

export async function fetchSignalsState(signal?: AbortSignal): Promise<SignalStateRecord[]> {
	const resp = await getJson<Record<string, unknown>[]>(SIGNALS_STATE_API, 8000, signal);
	const rows = Array.isArray(resp) ? resp : [];
	return rows
		.map((r): SignalStateRecord | null => {
			const id = r.junction_id ?? r.id;
			const lon = r.lon ?? r.lng;
			const lat = r.lat;
			if (typeof id !== 'string' && typeof id !== 'number') return null;
			if (typeof lon !== 'number' || typeof lat !== 'number') return null;
			return {
				junctionId: String(id),
				lon,
				lat,
				cycleSec: typeof r.cycle_sec === 'number' ? r.cycle_sec : null,
				activePhase: typeof r.active_phase === 'string' ? r.active_phase : null,
				greenRatio: typeof r.green_ratio === 'number' ? r.green_ratio : null,
				source: typeof r.source === 'string' ? r.source : null,
			};
		})
		.filter((s): s is SignalStateRecord => s !== null);
}

// ─── Agency units (contract C1; signalling stream; 404 until deployed) ──────

export interface AgencyUnitRecord {
	unitId: string;
	agency: string;
	label: string | null;
	lon: number;
	lat: number;
	assignedEventId: string | null;
	emulated: boolean;
}

export async function fetchUnits(signal?: AbortSignal): Promise<AgencyUnitRecord[]> {
	const resp = await getJson<Record<string, unknown>[]>(UNITS_API, 8000, signal);
	const rows = Array.isArray(resp) ? resp : [];
	return rows
		.map((r): AgencyUnitRecord | null => {
			const id = r.unit_id ?? r.id;
			const pos = r.position as unknown;
			const lon = Array.isArray(pos) ? pos[0] : r.lon;
			const lat = Array.isArray(pos) ? pos[1] : r.lat;
			if (typeof id !== 'string' && typeof id !== 'number') return null;
			if (typeof lon !== 'number' || typeof lat !== 'number') return null;
			return {
				unitId: String(id),
				agency: typeof r.agency === 'string' ? r.agency : 'unknown',
				label: typeof r.label === 'string' ? r.label : null,
				lon,
				lat,
				assignedEventId:
					typeof r.assigned_event_id === 'string' ? r.assigned_event_id : null,
				emulated: r.emulated === true,
			};
		})
		.filter((u): u is AgencyUnitRecord => u !== null);
}

// ─── Cameras (contract C1; watchlist-anpr stream; 404 until deployed) ───────
//
// Locations ONLY — the camera registry never carries frame data (ethics
// rail C6); the UI badges every marker with its source.

export interface CameraRecord {
	cameraId: string;
	lon: number;
	lat: number;
	kind: 'anpr' | 'cctv';
	source: 'osm' | 'emulated' | 'other';
}

export async function fetchCameras(signal?: AbortSignal): Promise<CameraRecord[]> {
	const resp = await getJson<Record<string, unknown>[]>(CAMERAS_API, 8000, signal);
	const rows = Array.isArray(resp) ? resp : [];
	return rows
		.map((r): CameraRecord | null => {
			const id = r.camera_id ?? r.id;
			const lon = r.lon ?? r.lng;
			const lat = r.lat;
			if (typeof id !== 'string' && typeof id !== 'number') return null;
			if (typeof lon !== 'number' || typeof lat !== 'number') return null;
			const kind = r.kind === 'cctv' ? 'cctv' : 'anpr';
			const source: CameraRecord['source'] =
				r.source === 'osm' ? 'osm' : r.source === 'emulated' ? 'emulated' : 'other';
			return { cameraId: String(id), lon, lat, kind, source };
		})
		.filter((c): c is CameraRecord => c !== null);
}
