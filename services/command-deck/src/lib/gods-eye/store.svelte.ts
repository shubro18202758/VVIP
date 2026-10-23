/**
 * God's-eye canvas store — the feeds behind the Cesium layers.
 *
 * Runes-based (Svelte 5), mirroring the /ops board's per-panel degradation
 * contract (store.svelte.ts header): every rung reports honestly —
 *   status: 'ok' | 'empty' | 'not-deployed' | 'down' | 'stale'
 * — and a failed layer NEVER takes the canvas (or the board) down.
 *
 * PRIMARY SOURCE — the fused city registry (entities/registry.svelte.ts,
 * GET /api/v1/city/entities every 4 s, contract C-C). Each kind a layer
 * draws is read from it while the registry SERVES that kind (endpoint up,
 * the kind's own source ok):
 *
 *   vehicles  ← vehicle entities (active earmarks, last sighting)
 *   signals   ← junction entities that carry a signal program
 *   units     ← unit entities (dispatch projections + C-A patrols)
 *   cameras   ← camera entities (locations + 15-min counters only)
 *   traces    ← sighting entities (the last 8 reads per FLAGGED vehicle)
 *
 * FALLBACK — per kind, the WP-05 fetchers below poll ONLY while the
 * registry does not serve that kind (city API 404/down, or one dead source
 * inside it), so a healthy city picture costs one request per 4 s instead
 * of five per 8 s:
 *
 *   segments  8 s + 10 min cache   GET /api/v1/spatial/segments (always —
 *                                  the registry carries no road geometry)
 *   vehicles  8 s                  GET /api/v1/vehicles        {vehicles: []}
 *   signals   8 s                  GET /api/v1/signals/state   {junctions: []}
 *   units     8 s                  GET /api/v1/units           {units: []}
 *   cameras   8 s                  GET /api/v1/cameras         {cameras: []}
 *
 * Network hygiene: each poller is in-flight guarded, nothing is fetched while
 * the page is hidden (one catch-up round on return), and a 404 path is not
 * re-requested for 60 s (the per-path memory in $lib/ops/client).
 *
 * 'empty' means the API answered ZERO rows — never that the parser dropped
 * rows it could not place (those are counted, and the feed stays 'ok').
 * An empty feed is NOT a fault: its explanation ('no units fielded') rides
 * in `hint`, and a layer raises its warning chip (`error`, feedError()) only
 * for a feed that is 'down' or 'not-deployed'.
 *
 * Road geometry is read over the whole network extent (NETWORK_BBOX — the
 * SP Ring Road west of the city view included); CITY_BBOX stays the
 * canvas's boot view only.
 *
 * Corridor EVENTS are NOT polled here — the canvas's corridor-events layer
 * binds to the LIVE ops store (src/lib/ops/store.svelte.ts), which already
 * carries events + lifecycle state + situation band on its own cadence.
 */

import { GODS_EYE_MODE_API, NETWORK_BBOX, POLL_MS } from '$lib/ops/endpoints';
import { createPollGate } from '$lib/ops/client';
import {
	Down,
	fetchCameras,
	fetchInterceptionDetail,
	fetchSegments,
	fetchSignalsState,
	fetchUnits,
	fetchVehicles,
	NotDeployed,
	phaseFields,
	type AgencyUnitRecord,
	type CameraRecord,
	type FeedStats,
	type InterceptionDetail,
	type SegmentGeometry,
	type SignalStateRecord,
	type TraceHop,
	type VehicleRecord,
	type VehicleTrace,
} from './data';
import type { CityEntity, EntityKind } from './city-api';
import { cityRegistry } from './entities/registry.svelte';

export type LayerFeed = 'ok' | 'empty' | 'not-deployed' | 'down';
export type LayerFeedState = {
	status: LayerFeed;
	/** A fault note (down / not deployed) — what the layer chip warns about. */
	note: string | null;
	updatedAt: number;
	/** Rows the API answered (0 before the first answer). */
	total: number;
	/** Why an EMPTY feed is empty (an answer, not a fault) — the chip's hint. */
	hint?: string | null;
};

/** The layer chip's warning: only for a feed that is down or not deployed — never for an empty answer. */
export function feedError(f: LayerFeedState): string | null {
	return f.status === 'down' || f.status === 'not-deployed' ? (f.note ?? null) : null;
}

/** The empty feed's explanation, for the chip hint (null when the feed has rows). */
export function feedHint(f: LayerFeedState): string | null {
	return f.status === 'empty' && f.updatedAt > 0 ? (f.hint ?? null) : null;
}

const SEGMENT_TTL_MS = 10 * 60 * 1000;

function feed(status: LayerFeed, note: string | null = null, total = 0): LayerFeedState {
	return { status, note, updatedAt: Date.now(), total };
}

/** A feed that answered zero rows: no fault note, the reason in the hint. */
function emptyFeed(hint: string): LayerFeedState {
	return { status: 'empty', note: null, updatedAt: Date.now(), total: 0, hint };
}

function initialFeed(): LayerFeedState {
	return { status: 'empty', note: null, updatedAt: 0, total: 0 };
}

// ─── registry entity → layer record (pure) ───────────────────────────────────

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length ? v : null;
}

function epochMs(v: unknown): number | null {
	if (typeof v !== 'string') return null;
	const t = Date.parse(v);
	return Number.isFinite(t) ? t : null;
}

export function vehicleFromEntity(e: CityEntity): VehicleRecord {
	const p = e.props;
	const state = e.state === 'flagged' || e.state === 'watch' ? e.state : null;
	return {
		vehicleId: e.id,
		plate: str(p.plate),
		earmarkState: state,
		retentionUntil: str(p.retention_until),
		lawfulBasis: str(p.lawful_basis),
		lastSighting:
			e.lon != null && e.lat != null
				? {
						t: e.t,
						lon: e.lon,
						lat: e.lat,
						source: e.source === 'emulated' ? 'emulated_anpr' : 'anpr',
						cameraId: str(p.last_camera_id),
						hit: str(p.matched_plate_hit),
					}
				: null,
	};
}

/** Junction entities carrying a signal program (the /signals/state set). */
export function signalFromEntity(e: CityEntity): SignalStateRecord | null {
	const p = e.props;
	if (num(p.cycle_sec) == null || e.lon == null || e.lat == null) return null;
	return {
		junctionId: e.id,
		lon: e.lon,
		lat: e.lat,
		cycleSec: num(p.cycle_sec),
		offsetSec: num(p.offset_sec),
		...phaseFields(p.active_phase),
		greenRatio: num(p.green_ratio),
		source: str(p.program_source),
		name: str(p.name),
	};
}

export function unitFromEntity(e: CityEntity): AgencyUnitRecord | null {
	const p = e.props;
	if (e.lon == null || e.lat == null) return null;
	const progress = num(p.progress);
	return {
		unitId: e.id,
		agency: str(p.agency) ?? 'unknown',
		label: e.label,
		lon: e.lon,
		lat: e.lat,
		assignedEventId: str(p.assigned_event_id),
		emulated: p.emulated === true || e.source === 'emulated',
		status: e.state,
		etaSec: num(p.eta_sec),
		progress: progress == null ? null : Math.max(0, Math.min(1, progress)),
		arrived: typeof p.arrived === 'boolean' ? p.arrived : null,
		speedKmh: num(p.speed_kmh),
		headingDeg: num(p.heading),
		source: str(p.unit_source),
		dispatchId: str(p.dispatch_id),
	};
}

export function cameraFromEntity(e: CityEntity): CameraRecord | null {
	const p = e.props;
	if (e.lon == null || e.lat == null) return null;
	const src = p.camera_source;
	return {
		cameraId: e.id,
		label: e.label !== e.urn ? e.label : null,
		lon: e.lon,
		lat: e.lat,
		kind: p.camera_kind === 'cctv' ? 'cctv' : 'anpr',
		source: src === 'osm' || src === 'emulated' || src === 'registered' ? src : 'other',
		junctionId: num(p.junction_id),
		headingDeg: num(p.heading_deg),
		status: e.state,
		reads15m: Math.max(0, num(p.reads_15m) ?? 0),
		hits15m: Math.max(0, num(p.hits_15m) ?? 0),
		lastReadT: epochMs(p.last_read_t),
	};
}

/** Sighting entities of one vehicle → its trace (hops numbered oldest = 1). */
export function traceFromSightings(vehicleId: string, sightings: CityEntity[]): VehicleTrace {
	const hops: TraceHop[] = sightings
		.filter((s) => s.lon != null && s.lat != null)
		.map((s) => ({
			seq: 0,
			t: s.t,
			lon: s.lon as number,
			lat: s.lat as number,
			cameraId: str(s.props.camera_id),
			speedKmh: num(s.props.speed_kmh),
			source: str(s.props.read_source) ?? (s.source === 'emulated' ? 'emulated_anpr' : 'anpr'),
			sightingId: s.id,
		}))
		.sort((a, b) => (a.t ?? 0) - (b.t ?? 0));
	hops.forEach((h, i) => (h.seq = i + 1));
	return {
		vehicleId,
		coordinates: hops.map((h): [number, number] => [h.lon, h.lat]),
		source: hops[hops.length - 1]?.source ?? null,
		hops,
		via: hops.length ? 'city' : 'none',
		latestT: hops.length ? hops[hops.length - 1].t : null,
	};
}

class GodsEyeStore {
	// ─── imagery mode (server-side token presence) ────────────────────────────
	imageryMode = $state<'keyless-osm' | 'ion-photoreal' | 'unknown'>('unknown');
	imageryNote = $state<string | null>(null);
	/**
	 * The ion token when the upgrade is active — handed to the same-origin
	 * page by routes/gods-eye/mode/+server.ts (env-only server-side; an ion
	 * access token is a Cesium client credential by design). Never logged.
	 */
	imageryToken = $state<string | null>(null);

	// ─── feeds ────────────────────────────────────────────────────────────────
	segments = $state<SegmentGeometry[]>([]);
	segmentsFeed = $state<LayerFeedState>(initialFeed());
	private segmentsFetchedAt = 0;

	/** WP-05 fallback rows (used only while the registry does not serve the kind). */
	private polledVehicles = $state<VehicleRecord[]>([]);
	private polledVehiclesFeed = $state<LayerFeedState>(initialFeed());
	private polledSignals = $state<SignalStateRecord[]>([]);
	private polledSignalsFeed = $state<LayerFeedState>(initialFeed());
	private polledUnits = $state<AgencyUnitRecord[]>([]);
	private polledUnitsFeed = $state<LayerFeedState>(initialFeed());
	private polledCameras = $state<CameraRecord[]>([]);
	private polledCamerasFeed = $state<LayerFeedState>(initialFeed());

	/**
	 * The selected pursuit's interception plan, read for its honesty fields
	 * (stale, likely-passed chokes, direction of travel) — refreshed when the
	 * board's plan changes (the canvas pursuit effect drives it). Plain
	 * `$state` (not `.raw`): the record is small, and the contract harness
	 * (tests/contract/run.mjs) runs this store with an identity `$state` stub.
	 */
	interception = $state<InterceptionDetail | null>(null);
	interceptionNote = $state<string | null>(null);
	private interceptionToken = 0;

	// ─── subject follow (wall mode) ───────────────────────────────────────────
	/**
	 * When true (the /ops wall default), selecting an event flies the camera
	 * to its spine and holds it there (auto-orbit suspended) until released
	 * from the HUD. /gods-eye keeps it false — the cinematic route is a free
	 * camera unless the hash says otherwise.
	 */
	followSelection = $state(false);
	/** Set while the camera is held on a subject (HUD lock chip). */
	subjectLockId = $state<string | null>(null);

	// ─── internals ────────────────────────────────────────────────────────────
	private timers: ReturnType<typeof setInterval>[] = [];
	private kick: ReturnType<typeof setTimeout> | null = null;
	private started = false;
	private gate = createPollGate();
	private onVisibility: (() => void) | null = null;

	/** segmentId → geometry, rebuilt when the segment fetch lands. */
	get segmentById(): Map<number, SegmentGeometry> {
		const m = new Map<number, SegmentGeometry>();
		for (const s of this.segments) m.set(s.segmentId, s);
		return m;
	}

	// ─── registry-first reads ─────────────────────────────────────────────────

	/** Where a kind's rows come from right now. */
	via(kind: EntityKind): 'city-api' | 'fallback' {
		return cityRegistry.served(kind) ? 'city-api' : 'fallback';
	}

	/**
	 * Registry rows when it serves the kind; else the fallback poller's rows
	 * once it has answered; else the registry's last-good rows (a degraded
	 * registry keeps drawing until the fallback lands).
	 */
	private pick<T>(kind: EntityKind, map: (e: CityEntity) => T | null, polled: T[], polledFeed: LayerFeedState): T[] {
		if (cityRegistry.served(kind) || (polledFeed.updatedAt === 0 && cityRegistry.byKind(kind).length)) {
			const out: T[] = [];
			for (const e of cityRegistry.byKind(kind)) {
				const r = map(e);
				if (r) out.push(r);
			}
			return out;
		}
		return polled;
	}

	private pickFeed(kind: EntityKind, polledFeed: LayerFeedState, emptyHint: string): LayerFeedState {
		if (!cityRegistry.served(kind)) return polledFeed;
		const f = cityRegistry.feed(kind);
		const total = f?.count ?? 0;
		return {
			status: total > 0 ? 'ok' : 'empty',
			note: null,
			hint: total > 0 ? null : emptyHint,
			updatedAt: cityRegistry.updatedAt,
			total: total + (f?.unlocated ?? 0),
		};
	}

	get vehicles(): VehicleRecord[] {
		return this.pick('vehicle', vehicleFromEntity, this.polledVehicles, this.polledVehiclesFeed);
	}

	get vehiclesFeed(): LayerFeedState {
		return this.pickFeed('vehicle', this.polledVehiclesFeed, 'no earmarked vehicles');
	}

	get signals(): SignalStateRecord[] {
		return this.pick('junction', signalFromEntity, this.polledSignals, this.polledSignalsFeed);
	}

	get signalsFeed(): LayerFeedState {
		if (!cityRegistry.served('junction')) return this.polledSignalsFeed;
		const n = this.signals.length;
		return { status: n ? 'ok' : 'empty', note: null, hint: n ? null : 'no signal programs', updatedAt: cityRegistry.updatedAt, total: n };
	}

	get units(): AgencyUnitRecord[] {
		return this.pick('unit', unitFromEntity, this.polledUnits, this.polledUnitsFeed);
	}

	get unitsFeed(): LayerFeedState {
		return this.pickFeed('unit', this.polledUnitsFeed, 'no units fielded');
	}

	get cameras(): CameraRecord[] {
		return this.pick('camera', cameraFromEntity, this.polledCameras, this.polledCamerasFeed);
	}

	get camerasFeed(): LayerFeedState {
		return this.pickFeed('camera', this.polledCamerasFeed, 'no cameras registered');
	}

	/**
	 * The registry's camera hops for one vehicle (FLAGGED vehicles only —
	 * the snapshot carries their last 8 reads), or null when the registry
	 * does not serve sightings (the traces layer then reads /trace).
	 */
	registryTrace(vehicleId: string): VehicleTrace | null {
		if (!cityRegistry.served('sighting')) return null;
		const own = cityRegistry.byKind('sighting').filter((s) => s.props.vehicle_id === vehicleId);
		return own.length ? traceFromSightings(vehicleId, own) : null;
	}

	// ─── lifecycle ────────────────────────────────────────────────────────────

	start() {
		if (this.started) return;
		this.started = true;
		cityRegistry.start();
		void this.pollImageryMode();
		this.pollAll();
		// The registry answers its first poll within ~1 s; whatever it does
		// not serve then is fetched by the fallback at once, not a tick later.
		this.kick = setTimeout(() => this.pollAll(), 2500);
		this.timers.push(setInterval(() => this.pollAll(), POLL_MS.canvas));
		if (typeof document !== 'undefined') {
			// Back from a hidden tab: one catch-up round instead of waiting a tick.
			this.onVisibility = () => {
				if (!document.hidden) this.pollAll();
			};
			document.addEventListener('visibilitychange', this.onVisibility);
		}
	}

	stop() {
		if (!this.started) return;
		for (const t of this.timers) clearInterval(t);
		this.timers = [];
		if (this.kick) clearTimeout(this.kick);
		this.kick = null;
		if (this.onVisibility && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', this.onVisibility);
		}
		this.onVisibility = null;
		this.started = false;
		cityRegistry.stop();
	}

	/**
	 * One gated round: segments always; each fallback feed only while the
	 * registry has answered and does not serve its kind (skipped while the
	 * registry's first answer is still pending).
	 */
	private pollAll() {
		void this.gate('segments', () => this.pollSegments());
		if (cityRegistry.status === 'idle') return;
		if (!cityRegistry.served('vehicle')) void this.gate('vehicles', () => this.pollVehicles());
		if (!cityRegistry.served('junction')) void this.gate('signals', () => this.pollSignals());
		if (!cityRegistry.served('unit')) void this.gate('units', () => this.pollUnits());
		if (!cityRegistry.served('camera')) void this.gate('cameras', () => this.pollCameras());
	}

	// ─── imagery mode ─────────────────────────────────────────────────────────

	async pollImageryMode() {
		try {
			const res = await fetch(GODS_EYE_MODE_API, { signal: AbortSignal.timeout(6000) });
			if (!res.ok) throw new Error(`${res.status}`);
			const body = (await res.json()) as { mode?: string; note?: string; token?: string };
			if (body.mode === 'ion-photoreal') {
				this.imageryMode = 'ion-photoreal';
				this.imageryToken = typeof body.token === 'string' && body.token ? body.token : null;
			} else if (body.mode === 'keyless-osm') {
				this.imageryMode = 'keyless-osm';
				this.imageryToken = null;
			}
			this.imageryNote = typeof body.note === 'string' ? body.note : null;
		} catch (err) {
			this.imageryNote = `mode endpoint unavailable (${
				err instanceof Error ? err.message : String(err)
			}) — keyless default`;
			this.imageryMode = 'keyless-osm';
		}
	}

	// ─── segments (spine geometry) ────────────────────────────────────────────

	async pollSegments(force = false) {
		if (!force && Date.now() - this.segmentsFetchedAt < SEGMENT_TTL_MS) return;
		try {
			const rows = await fetchSegments(NETWORK_BBOX);
			this.segmentsFetchedAt = Date.now();
			this.segments = rows;
			this.segmentsFeed = rows.length ? feed('ok', null, rows.length) : emptyFeed('no segments in the network extent');
		} catch (err) {
			this.segmentsFeed = notDeployedAware(err, this.segmentsFeed);
		}
	}

	// ─── fallback pollers: vehicles / signals / units / cameras ───────────────

	async pollVehicles() {
		if (cityRegistry.served('vehicle')) return void cityRegistry.refresh();
		const stats: FeedStats = { total: 0, dropped: 0 };
		try {
			this.polledVehicles = await fetchVehicles(undefined, stats);
			this.polledVehiclesFeed = answered(stats, 'no earmarked vehicles');
		} catch (err) {
			this.polledVehiclesFeed = notDeployedAware(err, this.polledVehiclesFeed);
		}
	}

	async pollSignals() {
		if (cityRegistry.served('junction')) return void cityRegistry.refresh();
		const stats: FeedStats = { total: 0, dropped: 0 };
		try {
			this.polledSignals = await fetchSignalsState(undefined, stats);
			this.polledSignalsFeed = answered(stats, 'no signal programs');
		} catch (err) {
			this.polledSignalsFeed = notDeployedAware(err, this.polledSignalsFeed);
		}
	}

	async pollUnits() {
		if (cityRegistry.served('unit')) return void cityRegistry.refresh();
		const stats: FeedStats = { total: 0, dropped: 0 };
		try {
			this.polledUnits = await fetchUnits(undefined, stats);
			this.polledUnitsFeed = answered(stats, 'no units fielded');
		} catch (err) {
			this.polledUnitsFeed = notDeployedAware(err, this.polledUnitsFeed);
		}
	}

	async pollCameras() {
		if (cityRegistry.served('camera')) return void cityRegistry.refresh();
		const stats: FeedStats = { total: 0, dropped: 0 };
		try {
			this.polledCameras = await fetchCameras(undefined, stats);
			this.polledCamerasFeed = answered(stats, 'no cameras registered');
		} catch (err) {
			this.polledCamerasFeed = notDeployedAware(err, this.polledCamerasFeed);
		}
	}

	// ─── interception detail (pursuit overlay + dossier) ──────────────────────

	/** Read the plan's honesty fields for `eventId` (null clears). Newest request wins. */
	async loadInterception(eventId: string | null): Promise<InterceptionDetail | null> {
		const token = ++this.interceptionToken;
		if (!eventId) {
			this.interception = null;
			this.interceptionNote = null;
			return null;
		}
		try {
			const detail = await fetchInterceptionDetail(eventId, AbortSignal.timeout(12_000));
			if (token !== this.interceptionToken) return null;
			this.interception = detail;
			this.interceptionNote = null;
			return detail;
		} catch (err) {
			if (token !== this.interceptionToken) return null;
			this.interceptionNote =
				err instanceof NotDeployed
					? 'no interception plan for this event (404)'
					: `interception plan unavailable (${errMessage(err)})`;
			if (this.interception?.eventId !== eventId) this.interception = null;
			return null;
		}
	}
}

function errMessage(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}

/**
 * The feed state for a successful answer: 'empty' (with its note) only when
 * the API returned zero rows; otherwise 'ok' with no warning — rows the
 * canvas cannot place are counted in `total`, not reported as missing.
 */
function answered(stats: FeedStats, emptyHint: string): LayerFeedState {
	return stats.total > 0 ? feed('ok', null, stats.total) : emptyFeed(emptyHint);
}

/**
 * 404 = the backend has not landed this endpoint (an honest chip state, not
 * an error); anything else is a real failure that keeps last-good rows (stale
 * data with an honest note beats a blank canvas).
 */
function notDeployedAware(err: unknown, prev: LayerFeedState): LayerFeedState {
	if (err instanceof NotDeployed) return feed('not-deployed', 'endpoint 404 — backend stream pending');
	if (err instanceof Down || prev.status === 'ok' || prev.status === 'empty') {
		return { ...feed('down', `${errMessage(err)} — showing last-good`), total: prev.total };
	}
	return prev;
}

export const godsEye = new GodsEyeStore();
