/**
 * God's-eye canvas store — the pollers + caches behind the Cesium layers.
 *
 * Runes-based (Svelte 5), mirroring the /ops board's per-panel degradation
 * contract (store.svelte.ts header): every rung reports honestly —
 *   status: 'ok' | 'empty' | 'not-deployed' | 'down' | 'stale'
 * — and a failed layer NEVER takes the canvas (or the board) down.
 *
 *   segments  8 s + 10 min cache   GET /api/v1/spatial/segments (live today)
 *   vehicles  8 s                  GET /api/v1/vehicles (404 → not-deployed)
 *   signals   8 s                  GET /api/v1/signals/state (404 until C1)
 *   units     8 s                  GET /api/v1/units (404 until C1)
 *   cameras   8 s                  GET /api/v1/cameras (404 until C1)
 *
 * Corridor EVENTS are NOT polled here — the canvas's corridor-events layer
 * binds to the LIVE ops store (src/lib/ops/store.svelte.ts), which already
 * carries events + lifecycle state + situation band on its own cadence.
 */

import { CITY_BBOX, GODS_EYE_MODE_API, POLL_MS } from '$lib/ops/endpoints';
import {
	Down,
	fetchCameras,
	fetchSegments,
	fetchSignalsState,
	fetchUnits,
	fetchVehicles,
	NotDeployed,
	type AgencyUnitRecord,
	type CameraRecord,
	type SegmentGeometry,
	type SignalStateRecord,
	type VehicleRecord,
} from './data';

export type LayerFeed = 'ok' | 'empty' | 'not-deployed' | 'down';
export type LayerFeedState = { status: LayerFeed; note: string | null; updatedAt: number };

const SEGMENT_TTL_MS = 10 * 60 * 1000;

function feed(status: LayerFeed, note: string | null = null): LayerFeedState {
	return { status, note, updatedAt: Date.now() };
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
	segmentsFeed = $state<LayerFeedState>({ status: 'empty', note: null, updatedAt: 0 });
	private segmentsFetchedAt = 0;

	vehicles = $state<VehicleRecord[]>([]);
	vehiclesFeed = $state<LayerFeedState>({ status: 'empty', note: null, updatedAt: 0 });

	signals = $state<SignalStateRecord[]>([]);
	signalsFeed = $state<LayerFeedState>({ status: 'empty', note: null, updatedAt: 0 });

	units = $state<AgencyUnitRecord[]>([]);
	unitsFeed = $state<LayerFeedState>({ status: 'empty', note: null, updatedAt: 0 });

	cameras = $state<CameraRecord[]>([]);
	camerasFeed = $state<LayerFeedState>({ status: 'empty', note: null, updatedAt: 0 });

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
	private started = false;

	/** segmentId → geometry, rebuilt when the segment fetch lands. */
	get segmentById(): Map<number, SegmentGeometry> {
		const m = new Map<number, SegmentGeometry>();
		for (const s of this.segments) m.set(s.segmentId, s);
		return m;
	}

	// ─── lifecycle ────────────────────────────────────────────────────────────

	start() {
		if (this.started) return;
		this.started = true;
		void this.pollImageryMode();
		void this.pollSegments();
		void this.pollVehicles();
		void this.pollSignals();
		void this.pollUnits();
		void this.pollCameras();
		this.timers.push(setInterval(() => void this.pollSegments(), POLL_MS.canvas));
		this.timers.push(setInterval(() => void this.pollVehicles(), POLL_MS.canvas));
		this.timers.push(setInterval(() => void this.pollSignals(), POLL_MS.canvas));
		this.timers.push(setInterval(() => void this.pollUnits(), POLL_MS.canvas));
		this.timers.push(setInterval(() => void this.pollCameras(), POLL_MS.canvas));
	}

	stop() {
		for (const t of this.timers) clearInterval(t);
		this.timers = [];
		this.started = false;
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
			const rows = await fetchSegments(CITY_BBOX);
			this.segmentsFetchedAt = Date.now();
			this.segments = rows;
			this.segmentsFeed = rows.length
				? feed('ok')
				: feed('empty', 'no segments in city bbox');
		} catch (err) {
			this.segmentsFeed = feed('down', errMessage(err));
		}
	}

	// ─── vehicles / signals / units / cameras ─────────────────────────────────

	async pollVehicles() {
		try {
			const rows = await fetchVehicles();
			this.vehicles = rows;
			this.vehiclesFeed = rows.length
				? feed('ok')
				: feed('empty', 'no earmarked vehicles');
		} catch (err) {
			this.vehiclesFeed = notDeployedAware(err, this.vehiclesFeed);
		}
	}

	async pollSignals() {
		try {
			const rows = await fetchSignalsState();
			this.signals = rows;
			this.signalsFeed = rows.length ? feed('ok') : feed('empty', 'no signal programs');
		} catch (err) {
			this.signalsFeed = notDeployedAware(err, this.signalsFeed);
		}
	}

	async pollUnits() {
		try {
			const rows = await fetchUnits();
			this.units = rows;
			this.unitsFeed = rows.length ? feed('ok') : feed('empty', 'no units fielded');
		} catch (err) {
			this.unitsFeed = notDeployedAware(err, this.unitsFeed);
		}
	}

	async pollCameras() {
		try {
			const rows = await fetchCameras();
			this.cameras = rows;
			this.camerasFeed = rows.length
				? feed('ok')
				: feed('empty', 'no cameras registered');
		} catch (err) {
			this.camerasFeed = notDeployedAware(err, this.camerasFeed);
		}
	}
}

function errMessage(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}

/**
 * 404 = the backend stream has not landed its endpoint yet (expected today);
 * anything else is a real failure. Both keep last-good rows (stale data with
 * an honest note beats a blank canvas).
 */
function notDeployedAware(err: unknown, prev: LayerFeedState): LayerFeedState {
	if (err instanceof NotDeployed) return feed('not-deployed', 'endpoint 404 — backend stream pending');
	if (err instanceof Down || prev.status === 'ok' || prev.status === 'empty') {
		return feed('down', `${errMessage(err)} — showing last-good`);
	}
	return prev;
}

export const godsEye = new GodsEyeStore();
