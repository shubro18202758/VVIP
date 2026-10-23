/**
 * Public projection (server-only) — the deck answers /citizen and /driver
 * itself, so anonymous browsers never receive operator feeds.
 *
 * GET /api/public/events reads GET /api/v1/events/active on the server,
 * drops every non-public kind (holds.ts PUBLIC_EVENT_KINDS: pursuits and
 * crime incidents carry plates, FIR numbers and officer names in their
 * titles and metadata) and returns only what an advisory needs:
 *
 *   - the PRIMARY spine as signage road names (/spatial/segments), each
 *     stretch tagged with its nearest signalised junction (/signals/state)
 *     — the green-corridor waypoint the /driver request carries;
 *   - the hold length: the phases' closure policy when the event has one,
 *     else the advance closure GET /adaptive/params publishes for the
 *     event, labelled by the oracle's OWN provenance — 'adaptive_params'
 *     only for a live adaptation (source retrieved | learned),
 *     'adaptive_baseline' for its doctrine baseline (source baseline: the
 *     120 s constant, tier-floored). Never a silent default;
 *   - an alternate from GET /graph/k-shortest-paths between the spine
 *     ends, named by the detour's signage roads (null when the graph has
 *     no distinct path — the card then says so);
 *   - a plain-language tier from the event's newest situation_log band;
 *   - `unavailable`: the facts whose read FAILED this pass (hold,
 *     alternate, road_names), so the card can say "unavailable right now"
 *     instead of "not published" / "no alternate";
 *   - `emulated`: true for an event the event emulator opened
 *     (metadata.source 'event-emulator'), for a TEST / rehearsal movement
 *     (an operator label, metadata.label) and for a movement whose escort
 *     drives on the brain's EMULATED GPS walker (GET /api/convoy/escorts
 *     gps_mode 'emulated') — a boolean only, so no metadata leaks. Citizens
 *     see it badged SIMULATED FEED · EMULATED, never LIVE or 'Holding now',
 *     it never moves the city strip (that is computed from real events
 *     only), and the feed carries `emulated_labelling: true` (C-M) so a
 *     bring-up script can check this build labels them before the living
 *     city runs.
 *
 * Horizon ("your road, next 2 hours"): an event is projected in full only
 * while it is active, inside its window, or starts within HORIZON_MS.
 * Later events go to `later` — kind, start, end and signage road names
 * only (no hold, alternate or band reads for them) — so a hold planned for
 * Friday never reads as "next" on Tuesday. A protected (VVIP) movement's
 * Later row carries its date and NO roads (`roads_withheld`): its route is
 * announced inside the horizon, as the full card, and not hours ahead.
 *
 * GET /api/public/dispatches/{id}?event= lets the /driver page follow ITS
 * OWN ambulance request (the dispatch uuid is the capability) without the
 * operator dispatch list: status + approval time + the bound emulated unit
 * (with the instant its ETA was measured, so the page can time the window
 * against the projected arrival). It reads GET /dispatches/{id}; an oracle
 * without that route falls back to the event's ambulance list.
 *
 * Every upstream call goes through the SvelteKit server `fetch` the route
 * passes in, i.e. through the deck proxy's pinned loopback rules (hooks
 * marks these as sub-requests). Static facts are cached for 10 min, the
 * whole feed for 5 s with single-flight, so N citizens cost one oracle
 * read per 5 s; the driver status reads (the dispatch, unit projection)
 * for 2 s, so N polling drivers cost one read pair per 2 s.
 */

import { NETWORK_BBOX } from '$lib/ops/endpoints';
import {
	adaptiveHoldSource,
	chooseAlternate,
	holdMinutesFromSec,
	isPublicKind,
	phaseAdvanceClosureSec,
	stripTier,
	worstTier,
	type HoldSource,
	type StripTier,
} from './holds';
import type { PublicEventWire, PublicFeedWire, PublicLaterWire, UnavailableFact } from './public-model';

/** The server-side fetch the route hands in (event.fetch). */
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

const V1 = '/api/v1';
const FEED_TTL_MS = 5_000;
const STATIC_TTL_MS = 10 * 60_000;
const PARAMS_TTL_MS = 30_000;
const DISPATCH_TTL_MS = 2_000;
/** A situation_log band older than this no longer describes "now". */
const BAND_FRESH_MS = 30 * 60_000;
/** "Your road, next 2 hours": later starts go to the dated Later list. */
export const HORIZON_MS = 2 * 3600_000;
/** Signage names a Later row carries (it is a heads-up, not a hold plan). */
const LATER_ROAD_NAMES = 3;
/** A stretch further than this from every signalised junction has no waypoint. */
const JUNCTION_RADIUS_M = 1500;
const UPSTREAM_TIMEOUT_MS = 6_000;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const LIVE_STATUSES = new Set(['planning', 'approved', 'active']);

export interface PublicResult<T> {
	status: number;
	body: T | { detail: string };
}

class UpstreamError extends Error {
	constructor(
		public status: number,
		message: string,
		/** The upstream's own {detail} on a 404 (tells "no such row" from "no such route"). */
		public detail: string | null = null,
	) {
		super(message);
		this.name = 'UpstreamError';
	}
}

/**
 * One upstream read with a HARD deadline: SvelteKit runs same-origin
 * sub-requests in-process, where an abort signal does not interrupt the
 * proxy's own (60 s) fetch — so the deadline is raced, never trusted.
 */
async function getJson(fetchFn: FetchLike, path: string): Promise<unknown> {
	const ctrl = new AbortController();
	let timer: ReturnType<typeof setTimeout> | undefined;
	const deadline = new Promise<never>((_, reject) => {
		timer = setTimeout(() => {
			ctrl.abort();
			reject(new UpstreamError(503, `${path}: no answer within ${UPSTREAM_TIMEOUT_MS} ms`));
		}, UPSTREAM_TIMEOUT_MS);
	});
	try {
		let res: Response;
		try {
			res = await Promise.race([
				fetchFn(path, { headers: { accept: 'application/json' }, signal: ctrl.signal }),
				deadline,
			]);
		} catch (err) {
			if (err instanceof UpstreamError) throw err;
			throw new UpstreamError(503, `${path}: ${err instanceof Error ? err.message : String(err)}`);
		}
		if (res.status === 404) {
			const body = (await Promise.race([res.json().catch(() => null) as Promise<unknown>, deadline])) as {
				detail?: unknown;
			} | null;
			const detail = typeof body?.detail === 'string' ? body.detail.slice(0, 200) : null;
			throw new UpstreamError(404, `${path}: upstream 404`, detail);
		}
		if (!res.ok) {
			await res.body?.cancel().catch(() => undefined);
			throw new UpstreamError(res.status, `${path}: upstream ${res.status}`);
		}
		return await Promise.race([res.json() as Promise<unknown>, deadline]);
	} finally {
		clearTimeout(timer);
	}
}

// ─── Caches (module scope: one deck process serves every citizen) ────────────

interface CacheEntry<T> {
	at: number;
	value: Promise<T>;
}

/**
 * Keys one cache may hold. Some keys come from the public URL (a dispatch
 * uuid any caller can invent), so no cache may grow with the number of
 * ids asked: past the cap, expired entries go first, then the oldest.
 */
export const PUBLIC_CACHE_MAX_KEYS = 256;

function cached<T>(
	store: Map<string, CacheEntry<T>>,
	key: string,
	ttlMs: number,
	load: () => Promise<T>,
): Promise<T> {
	const hit = store.get(key);
	if (hit && Date.now() - hit.at < ttlMs) return hit.value;
	const value = load();
	store.delete(key); // re-inserted last: Map order is the age order the cap evicts by
	if (store.size >= PUBLIC_CACHE_MAX_KEYS) evict(store, ttlMs);
	store.set(key, { at: Date.now(), value });
	// A failed load must not be cached for the whole TTL.
	value.catch(() => {
		if (store.get(key)?.value === value) store.delete(key);
	});
	return value;
}

/** Make room under the cap: drop expired entries, then the oldest. */
function evict<T>(store: Map<string, CacheEntry<T>>, ttlMs: number): void {
	const now = Date.now();
	for (const [k, e] of store) if (now - e.at >= ttlMs) store.delete(k);
	for (const k of store.keys()) {
		if (store.size < PUBLIC_CACHE_MAX_KEYS) break;
		store.delete(k);
	}
}

interface SegmentFact {
	roadName: string | null;
	mid: [number, number] | null;
}

const segmentCache = new Map<string, CacheEntry<Map<number, SegmentFact>>>();
const junctionCache = new Map<string, CacheEntry<Array<{ id: number; lon: number; lat: number }>>>();
const kspCache = new Map<string, CacheEntry<number[][]>>();
const paramsCache = new Map<string, CacheEntry<AdaptiveClosure | null>>();
const dispatchListCache = new Map<string, CacheEntry<Timed<Array<Record<string, unknown>>>>>();
const dispatchCache = new Map<string, CacheEntry<Record<string, unknown> | null>>();
const unitsCache = new Map<string, CacheEntry<Timed<Array<Record<string, unknown>>>>>();
let feedEntry: { at: number; value: Promise<PublicResult<PublicFeedWire>> } | null = null;

/** A read and the instant it was taken (the driver times its window on it). */
interface Timed<T> {
	at: number;
	rows: T;
}

/** Test hook: how many keys the per-id caches hold (the cap is asserted). */
export function publicCacheSizes(): { dispatch: number; dispatchList: number } {
	return { dispatch: dispatchCache.size, dispatchList: dispatchListCache.size };
}

/** Test hook: drop every cache (the guard suite runs scenarios back to back). */
export function resetPublicCaches(): void {
	segmentCache.clear();
	junctionCache.clear();
	kspCache.clear();
	paramsCache.clear();
	dispatchListCache.clear();
	dispatchCache.clear();
	unitsCache.clear();
	feedEntry = null;
}

function midpoint(coords: unknown): [number, number] | null {
	if (!Array.isArray(coords) || coords.length === 0) return null;
	const c = coords[Math.floor((coords.length - 1) / 2)];
	const d = coords[Math.ceil((coords.length - 1) / 2)];
	if (!Array.isArray(c) || !Array.isArray(d)) return null;
	const [x1, y1, x2, y2] = [c[0], c[1], d[0], d[1]];
	if ([x1, y1, x2, y2].some((v) => typeof v !== 'number')) return null;
	return [((x1 as number) + (x2 as number)) / 2, ((y1 as number) + (y2 as number)) / 2];
}

function loadSegments(fetchFn: FetchLike): Promise<Map<number, SegmentFact>> {
	return cached(segmentCache, 'city', STATIC_TTL_MS, async () => {
		// The whole road network (NETWORK_BBOX reaches the SP Ring Road west
		// of the city view): a spine on 1033 must still get its signage name.
		const q =
			`min_lon=${NETWORK_BBOX.minLon}&min_lat=${NETWORK_BBOX.minLat}` +
			`&max_lon=${NETWORK_BBOX.maxLon}&max_lat=${NETWORK_BBOX.maxLat}`;
		const body = (await getJson(fetchFn, `${V1}/spatial/segments?${q}`)) as { segments?: unknown[] };
		const out = new Map<number, SegmentFact>();
		for (const s of Array.isArray(body?.segments) ? body.segments : []) {
			const r = s as Record<string, unknown>;
			if (typeof r.segment_id !== 'number') continue;
			const geom = r.geometry as { coordinates?: unknown } | null | undefined;
			out.set(r.segment_id, {
				roadName: typeof r.road_name === 'string' && r.road_name ? r.road_name : null,
				mid: midpoint(geom?.coordinates),
			});
		}
		return out;
	});
}

function loadJunctions(fetchFn: FetchLike): Promise<Array<{ id: number; lon: number; lat: number }>> {
	return cached(junctionCache, 'signals', STATIC_TTL_MS, async () => {
		const body = (await getJson(fetchFn, `${V1}/signals/state`)) as { junctions?: unknown[] } | unknown[];
		const rows = Array.isArray(body) ? body : Array.isArray(body?.junctions) ? body.junctions : [];
		const out: Array<{ id: number; lon: number; lat: number }> = [];
		for (const j of rows) {
			const r = j as Record<string, unknown>;
			if (typeof r.junction_id === 'number' && typeof r.lon === 'number' && typeof r.lat === 'number') {
				out.push({ id: r.junction_id, lon: r.lon, lat: r.lat });
			}
		}
		return out;
	});
}

function metresBetween(a: [number, number], b: [number, number]): number {
	const rad = Math.PI / 180;
	const dLat = (b[1] - a[1]) * rad;
	const dLon = (b[0] - a[0]) * rad;
	const h =
		Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
	return 2 * 6_371_000 * Math.asin(Math.sqrt(h));
}

/** Nearest signalised junction to a point, within JUNCTION_RADIUS_M. */
export function nearestJunction(
	point: [number, number] | null,
	junctions: ReadonlyArray<{ id: number; lon: number; lat: number }>,
): number | null {
	if (!point) return null;
	let best: { id: number; d: number } | null = null;
	for (const j of junctions) {
		const d = metresBetween(point, [j.lon, j.lat]);
		if (d <= JUNCTION_RADIUS_M && (best === null || d < best.d)) best = { id: j.id, d };
	}
	return best?.id ?? null;
}

function loadKsp(fetchFn: FetchLike, source: number, target: number): Promise<number[][]> {
	return cached(kspCache, `${source}>${target}`, STATIC_TTL_MS, async () => {
		const body = (await getJson(
			fetchFn,
			`${V1}/graph/k-shortest-paths?source=${source}&target=${target}&k=4`,
		)) as { paths?: unknown[] };
		const out: number[][] = [];
		for (const path of Array.isArray(body?.paths) ? body.paths : []) {
			if (!Array.isArray(path)) continue;
			const nodes = path
				.map((step) => (step as Record<string, unknown>)?.node)
				.filter((n): n is number => typeof n === 'number');
			if (nodes.length) out.push(nodes);
		}
		return out;
	});
}

/** The alternate in its wire shape (holds.ts chooseAlternate does the work). */
function alternateWire(
	spine: readonly number[],
	paths: readonly number[][],
	names: ReadonlyMap<number, SegmentFact>,
): PublicEventWire['alternate'] {
	const alt = chooseAlternate(spine, paths, (sid) => names.get(sid)?.roadName);
	return alt ? { road_names: alt.roadNames, segment_ids: alt.segmentIds, source: 'k_shortest_paths' } : null;
}

/** An /adaptive/params answer: the advance closure and its provenance. */
export interface AdaptiveClosure {
	sec: number;
	source: Extract<HoldSource, 'adaptive_params' | 'adaptive_baseline'>;
}

function loadAdvanceClosure(
	fetchFn: FetchLike,
	subject: string,
	protectionClass: string,
): Promise<AdaptiveClosure | null> {
	const cls = ['Z+', 'Z', 'Y', 'X', 'none'].includes(protectionClass) ? protectionClass : 'none';
	return cached(paramsCache, `${subject}|${cls}`, PARAMS_TTL_MS, async () => {
		const body = (await getJson(
			fetchFn,
			`${V1}/adaptive/params?movement_id=${encodeURIComponent(subject)}&vvip_class=${encodeURIComponent(cls)}`,
		)) as { params?: { advance_closure_sec?: unknown }; source?: unknown };
		const sec = body?.params?.advance_closure_sec;
		if (typeof sec !== 'number' || !(sec > 0)) return null;
		// The oracle labels its own answer: 'baseline' is the doctrine
		// constant (no live adaptation yet) and must be shown as such.
		return { sec, source: adaptiveHoldSource(body?.source) };
	});
}

async function loadBand(fetchFn: FetchLike, eventId: string, nowMs: number): Promise<string | null> {
	const body = (await getJson(
		fetchFn,
		`${V1}/adaptive/situation_log?event_id=${encodeURIComponent(eventId)}&limit=1`,
	)) as { entries?: Array<Record<string, unknown>> };
	const top = Array.isArray(body?.entries) ? body.entries[0] : undefined;
	if (!top || typeof top.situation_class !== 'string') return null;
	const at = typeof top.timestamp_utc === 'string' ? Date.parse(top.timestamp_utc) : Number.NaN;
	if (!Number.isFinite(at) || nowMs - at > BAND_FRESH_MS) return null;
	return top.situation_class;
}

/**
 * An event the event emulator opened (contract C-G: metadata.source
 * 'event-emulator') — projected as a boolean only, never the metadata.
 */
export function isEmulatedRow(row: Record<string, unknown>): boolean {
	const meta = row.metadata && typeof row.metadata === 'object' ? (row.metadata as Record<string, unknown>) : null;
	return meta?.source === 'event-emulator';
}

/**
 * A TEST / rehearsal movement: the operator labelled it (metadata.label —
 * 'ACCEPTANCE TEST', a rehearsal). Never a real movement on the road, so
 * never LIVE to a citizen; a boolean only.
 */
export function isRehearsalRow(row: Record<string, unknown>): boolean {
	const meta = row.metadata && typeof row.metadata === 'object' ? (row.metadata as Record<string, unknown>) : null;
	return typeof meta?.label === 'string' && meta.label.trim().length > 0;
}

/**
 * The corridor events whose escort drives on the brain's EMULATED GPS
 * walker (GET /api/convoy/escorts, gps_mode 'emulated'): a simulated
 * position is never LIVE to a citizen. Empty when the brain cannot say
 * (unreachable, or it predates the listing) — the other labels still hold.
 */
export async function loadEmulatedPositionEvents(fetchFn: FetchLike): Promise<Set<string>> {
	const out = new Set<string>();
	let body: unknown;
	try {
		body = await getJson(fetchFn, '/api/convoy/escorts');
	} catch {
		return out;
	}
	const escorts = body && typeof body === 'object' ? (body as { escorts?: unknown }).escorts : null;
	for (const e of Array.isArray(escorts) ? escorts : []) {
		if (!e || typeof e !== 'object') continue;
		const lane = e as Record<string, unknown>;
		if (lane.gps_mode === 'emulated' && typeof lane.event_id === 'string') out.add(lane.event_id);
	}
	return out;
}

/**
 * When an events/active row starts: the earliest of its planned start and
 * the starts of phases that have not ended. Null when it carries neither.
 */
export function eventStartMs(row: Record<string, unknown>, nowMs: number): number | null {
	const starts: number[] = [];
	const planned = typeof row.planned_start === 'string' ? Date.parse(row.planned_start) : Number.NaN;
	if (Number.isFinite(planned)) starts.push(planned);
	const phases = Array.isArray(row.phases) ? (row.phases as Array<Record<string, unknown>>) : [];
	for (const p of phases) {
		const s = typeof p.window_start === 'string' ? Date.parse(p.window_start) : Number.NaN;
		const e = typeof p.window_end === 'string' ? Date.parse(p.window_end) : Number.NaN;
		if (Number.isFinite(s) && !(Number.isFinite(e) && e <= nowMs)) starts.push(s);
	}
	return starts.length ? Math.min(...starts) : null;
}

/**
 * Inside the citizen horizon: active, inside its window, starting within
 * HORIZON_MS — or carrying no start at all (nothing to defer it by).
 */
export function withinHorizon(row: Record<string, unknown>, nowMs: number): boolean {
	if (isLiveNow(row, nowMs)) return true;
	const start = eventStartMs(row, nowMs);
	return start === null || start <= nowMs + HORIZON_MS;
}

/** Whether a live row is "on the road now" (status or an open window). */
export function isLiveNow(row: Record<string, unknown>, nowMs: number): boolean {
	if (row.status === 'active') return true;
	const inWindow = (start: unknown, end: unknown) => {
		const s = typeof start === 'string' ? Date.parse(start) : Number.NaN;
		const e = typeof end === 'string' ? Date.parse(end) : Number.NaN;
		return Number.isFinite(s) && s <= nowMs && (!Number.isFinite(e) || nowMs < e);
	};
	if (inWindow(row.planned_start, row.planned_end)) return true;
	const phases = Array.isArray(row.phases) ? (row.phases as Array<Record<string, unknown>>) : [];
	return phases.some((p) => inWindow(p.window_start, p.window_end));
}

interface EventFacts {
	segments: ReadonlyMap<number, SegmentFact>;
	junctions: ReadonlyArray<{ id: number; lon: number; lat: number }>;
	/** The adaptive layer's advance closure (null = none published / not asked). */
	advanceFromParams: AdaptiveClosure | null;
	alternate: PublicEventWire['alternate'];
	band: string | null;
	/** Reads that FAILED this pass (never conflated with "none exists"). */
	unavailable?: readonly UnavailableFact[];
	/** Its escort drives on the brain's EMULATED GPS walker (never LIVE). */
	emulatedPosition?: boolean;
}

/** The primary spine of an events/active row (routes[0]; legacy flat field). */
export function primarySpine(row: Record<string, unknown>): number[] {
	const routes = Array.isArray(row.routes) ? (row.routes as Array<Record<string, unknown>>) : [];
	const first = routes[0]?.segment_ids ?? row.route_segment_ids;
	return Array.isArray(first) ? first.filter((n): n is number => typeof n === 'number') : [];
}

/** The phases of an events/active row in wire shape (closure policy → seconds). */
function wirePhases(row: Record<string, unknown>): PublicEventWire['phases'] {
	const rawPhases = Array.isArray(row.phases) ? (row.phases as Array<Record<string, unknown>>) : [];
	return rawPhases.map((p) => {
		const policy = p.closure_policy && typeof p.closure_policy === 'object'
			? (p.closure_policy as Record<string, unknown>)
			: null;
		const adv = policy && typeof policy.advance_closure_sec === 'number' ? policy.advance_closure_sec : null;
		return {
			label: typeof p.label === 'string' ? p.label : 'phase',
			window_start: typeof p.window_start === 'string' ? p.window_start : null,
			window_end: typeof p.window_end === 'string' ? p.window_end : null,
			advance_closure_sec: adv,
		};
	});
}

/** The event's own closure-plan advance closure (largest across phases), or null. */
export function policyAdvanceSec(row: Record<string, unknown>): number | null {
	return phaseAdvanceClosureSec(
		wirePhases(row).map((p) => ({ closurePolicy: { advanceClosureSec: p.advance_closure_sec } })),
	);
}

/**
 * Pure projection of one events/active row onto the public wire shape.
 * Null for any non-public kind or non-live status — the allowlist is
 * enforced here, before anything is serialised for a browser.
 */
export function projectEvent(
	row: Record<string, unknown>,
	facts: EventFacts,
	nowMs: number,
): PublicEventWire | null {
	const eventId = typeof row.event_id === 'string' ? row.event_id : null;
	const kind = typeof row.event_kind === 'string' ? row.event_kind : null;
	const status = typeof row.status === 'string' ? row.status : null;
	if (!eventId || !isPublicKind(kind) || !status || !LIVE_STATUSES.has(status)) return null;

	const phases = wirePhases(row);
	const fromPolicy = policyAdvanceSec(row);
	let advance: number | null = null;
	let source: HoldSource | null = null;
	if (fromPolicy != null) {
		advance = fromPolicy;
		source = 'closure_policy';
	} else if (facts.advanceFromParams != null) {
		advance = facts.advanceFromParams.sec;
		source = facts.advanceFromParams.source;
	}
	// A policy hold never depended on the params read, so its failure
	// does not make the hold "unavailable".
	const unavailable = (facts.unavailable ?? []).filter((f) => f !== 'hold' || fromPolicy == null);

	const stretches = primarySpine(row).map((sid) => {
		const seg = facts.segments.get(sid);
		return {
			segment_id: sid,
			road_name: seg?.roadName ?? null,
			junction_id: nearestJunction(seg?.mid ?? null, facts.junctions),
		};
	});

	const tier: StripTier | null = stripTier(facts.band, isLiveNow(row, nowMs));
	// emulator-opened, a TEST / rehearsal label, or an emulated position:
	// labelled SIMULATED FEED · EMULATED, never LIVE / 'Holding now'
	const emulated = isEmulatedRow(row) || isRehearsalRow(row) || facts.emulatedPosition === true;

	return {
		event_id: eventId,
		event_kind: kind,
		status: status as PublicEventWire['status'],
		window_start: typeof row.planned_start === 'string' ? row.planned_start : null,
		window_end: typeof row.planned_end === 'string' ? row.planned_end : null,
		stretches,
		phases,
		hold: { minutes: holdMinutesFromSec(advance), advance_closure_sec: advance, source },
		alternate: facts.alternate,
		tier,
		unavailable,
		emulated,
	};
}

/**
 * A public event beyond the horizon, projected for the dated Later list:
 * kind, lifecycle, start/end and signage road names — nothing else (no
 * hold, alternate or band was read for it). Null for a non-public kind or
 * a non-live status.
 */
export function projectLater(
	row: Record<string, unknown>,
	segments: ReadonlyMap<number, SegmentFact>,
	nowMs: number,
): PublicLaterWire | null {
	const eventId = typeof row.event_id === 'string' ? row.event_id : null;
	const kind = typeof row.event_kind === 'string' ? row.event_kind : null;
	const status = typeof row.status === 'string' ? row.status : null;
	if (!eventId || !isPublicKind(kind) || !status || !LIVE_STATUSES.has(status)) return null;
	const start = eventStartMs(row, nowMs);
	// A VVIP route is announced inside the horizon only — never hours ahead.
	const roadsWithheld = kind === 'protected_movement';
	const roadNames: string[] = [];
	for (const sid of roadsWithheld ? [] : primarySpine(row)) {
		const name = segments.get(sid)?.roadName;
		if (name && !roadNames.includes(name)) roadNames.push(name);
		if (roadNames.length >= LATER_ROAD_NAMES) break;
	}
	return {
		event_id: eventId,
		event_kind: kind,
		status: status as PublicLaterWire['status'],
		starts_at: start === null ? null : new Date(start).toISOString(),
		window_end: typeof row.planned_end === 'string' ? row.planned_end : null,
		road_names: roadNames,
		roads_withheld: roadsWithheld,
		emulated: isEmulatedRow(row) || isRehearsalRow(row),
	};
}

/** A settled read: the value, or a failure the caller must label. */
type Attempt<T> = { ok: true; value: T } | { ok: false };

async function attempt<T>(p: Promise<T>): Promise<Attempt<T>> {
	try {
		return { ok: true, value: await p };
	} catch {
		return { ok: false };
	}
}

async function buildFeedOnce(fetchFn: FetchLike, nowMs: number): Promise<PublicResult<PublicFeedWire>> {
	let rows: Array<Record<string, unknown>>;
	try {
		const body = (await getJson(fetchFn, `${V1}/events/active`)) as { events?: unknown[] } | unknown[];
		const list = Array.isArray(body) ? body : Array.isArray(body?.events) ? body.events : [];
		rows = list.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object');
	} catch (err) {
		const status = err instanceof UpstreamError && err.status === 404 ? 404 : 503;
		return {
			status,
			body: {
				detail:
					status === 404
						? 'public feed: events API not deployed'
						: `public feed: events API unreachable (${err instanceof Error ? err.message : String(err)})`,
			},
		};
	}

	const publicRows = rows.filter(
		(r) => isPublicKind(typeof r.event_kind === 'string' ? r.event_kind : null) && LIVE_STATUSES.has(String(r.status)),
	);
	const candidates = publicRows.filter((r) => withinHorizon(r, nowMs));
	const beyond = publicRows.filter((r) => !withinHorizon(r, nowMs));
	if (publicRows.length === 0) {
		return {
			status: 200,
			body: { as_of: new Date(nowMs).toISOString(), events: [], later: [], tier: 'calm', emulated_labelling: true },
		};
	}

	const [segmentsRead, junctionsRead, emulatedPositions] = await Promise.all([
		attempt(loadSegments(fetchFn)),
		candidates.length ? attempt(loadJunctions(fetchFn)) : Promise.resolve<Attempt<Array<{ id: number; lon: number; lat: number }>>>({ ok: true, value: [] }),
		candidates.some((r) => r.event_kind === 'protected_movement')
			? loadEmulatedPositionEvents(fetchFn)
			: Promise.resolve(new Set<string>()),
	]);
	const segments = segmentsRead.ok ? segmentsRead.value : new Map<number, SegmentFact>();
	const junctions = junctionsRead.ok ? junctionsRead.value : [];
	const later = beyond
		.map((row) => projectLater(row, segments, nowMs))
		.filter((e): e is PublicLaterWire => e !== null)
		.sort((a, b) => Date.parse(a.starts_at ?? '') - Date.parse(b.starts_at ?? ''));

	const events = await Promise.all(
		candidates.map(async (row) => {
			const eventId = String(row.event_id);
			const spine = primarySpine(row);
			const subject = typeof row.movement_id === 'string' && row.movement_id ? row.movement_id : eventId;
			// The adaptive layer is asked only when the event's own closure
			// plan is silent (every /adaptive/params read is also an audited
			// publish on the oracle — no reads it does not need).
			const [params, paths, band] = await Promise.all([
				policyAdvanceSec(row) == null
					? attempt(loadAdvanceClosure(fetchFn, subject, String(row.protection_class ?? 'none')))
					: Promise.resolve<Attempt<AdaptiveClosure | null>>({ ok: true, value: null }),
				spine.length >= 2
					? attempt(loadKsp(fetchFn, spine[0], spine[spine.length - 1]))
					: Promise.resolve<Attempt<number[][]>>({ ok: true, value: [] }),
				attempt(loadBand(fetchFn, eventId, nowMs)),
			]);
			const unavailable: UnavailableFact[] = [];
			if (!params.ok) unavailable.push('hold');
			// The alternate needs the paths AND the names that make it readable.
			if (spine.length >= 2 && (!paths.ok || !segmentsRead.ok)) unavailable.push('alternate');
			if (!segmentsRead.ok && spine.length > 0) unavailable.push('road_names');
			return projectEvent(
				row,
				{
					segments,
					junctions,
					advanceFromParams: params.ok ? params.value : null,
					alternate: alternateWire(spine, paths.ok ? paths.value : [], segments),
					band: band.ok ? band.value : null,
					unavailable,
					emulatedPosition: emulatedPositions.has(eventId),
				},
				nowMs,
			);
		}),
	);
	const projected = events.filter((e): e is PublicEventWire => e !== null);
	return {
		status: 200,
		body: {
			as_of: new Date(nowMs).toISOString(),
			events: projected,
			later,
			// The city strip speaks for REAL roads: an emulated event is shown
			// on its own badged card but never turns the strip to "holds".
			tier: worstTier(projected.filter((e) => !e.emulated).map((e) => e.tier)) ?? 'calm',
			emulated_labelling: true,
		},
	};
}

/** GET /api/public/events — 5 s cache, single-flight across citizens. */
export function buildPublicFeed(fetchFn: FetchLike, nowMs = Date.now()): Promise<PublicResult<PublicFeedWire>> {
	if (feedEntry && nowMs - feedEntry.at < FEED_TTL_MS) return feedEntry.value;
	const value = buildFeedOnce(fetchFn, nowMs);
	feedEntry = { at: nowMs, value };
	value.then(
		(r) => {
			// Errors are answered but not cached: the next citizen retries.
			if (r.status !== 200 && feedEntry?.value === value) feedEntry = null;
		},
		() => {
			if (feedEntry?.value === value) feedEntry = null;
		},
	);
	return value;
}

export interface PublicDispatchWire {
	/** When this answer was written (the page corrects its clock skew on it). */
	as_of: string;
	dispatch: {
		dispatch_id: string;
		event_id: string;
		status: string;
		unit_id: string | null;
		waypoint_junction: number | null;
		approved_at: string | null;
		created_at: string | null;
		updated_at: string | null;
	};
	unit: {
		status: string | null;
		eta_sec: number | null;
		/** The instant eta_sec was measured (projected arrival = eta_at + eta_sec). */
		eta_at: string;
		progress: number | null;
		arrived: boolean;
		position: [number, number] | null;
		emulated: boolean;
		source: string | null;
	} | null;
}

/**
 * GET /dispatches/{id} — one dispatch, 2 s cache per id. Null when the
 * oracle answers 404 'dispatch not found' (no such request); an oracle
 * without the route (any other 404) throws, so the caller can fall back to
 * the per-event list read.
 */
function loadDispatch(fetchFn: FetchLike, dispatchId: string): Promise<Record<string, unknown> | null> {
	return cached(dispatchCache, dispatchId, DISPATCH_TTL_MS, async () => {
		try {
			const row = await getJson(fetchFn, `${V1}/dispatches/${dispatchId}`);
			return row && typeof row === 'object' && !Array.isArray(row) ? (row as Record<string, unknown>) : null;
		} catch (err) {
			if (err instanceof UpstreamError && err.status === 404 && /dispatch not found/i.test(err.detail ?? '')) return null;
			throw err;
		}
	});
}

/** GET /dispatches?event_id=&agency=ambulance — 2 s cache per event (older oracles). */
function loadAmbulanceDispatches(fetchFn: FetchLike, eventId: string): Promise<Timed<Array<Record<string, unknown>>>> {
	return cached(dispatchListCache, eventId, DISPATCH_TTL_MS, async () => {
		const body = (await getJson(
			fetchFn,
			`${V1}/dispatches?event_id=${eventId}&agency=ambulance&limit=200`,
		)) as { dispatches?: unknown[] };
		const rows = (Array.isArray(body?.dispatches) ? body.dispatches : []) as Array<Record<string, unknown>>;
		return { at: Date.now(), rows };
	});
}

/** GET /units — the emulated projection of approved dispatches; 2 s cache. */
function loadUnits(fetchFn: FetchLike): Promise<Timed<Array<Record<string, unknown>>>> {
	return cached(unitsCache, 'units', DISPATCH_TTL_MS, async () => {
		const b = await getJson(fetchFn, `${V1}/units`);
		const rows = Array.isArray(b) ? b : (b as { units?: unknown[] })?.units;
		return { at: Date.now(), rows: (Array.isArray(rows) ? rows : []) as Array<Record<string, unknown>> };
	});
}

/**
 * GET /api/public/dispatches/{id}?event= — one AMBULANCE dispatch's
 * status for the requesting driver. Only the 108 agency is readable here
 * (police / Jan Rakshak dispatches never are), and no officer names,
 * notes or audit tails are returned. Upstream reads are shared for 2 s
 * across every polling driver.
 */
export async function publicDispatchStatus(
	fetchFn: FetchLike,
	dispatchId: string,
	eventId: string,
	nowMs?: number,
): Promise<PublicResult<PublicDispatchWire>> {
	if (!UUID_RE.test(dispatchId) || !UUID_RE.test(eventId)) {
		return { status: 400, body: { detail: 'dispatch and event must be uuids' } };
	}
	const NO_SUCH = { status: 404, body: { detail: 'no such ambulance request for this event' } };
	let row: Record<string, unknown> | null = null;
	try {
		row = await loadDispatch(fetchFn, dispatchId);
		if (row === null) return NO_SUCH;
	} catch (err) {
		if (!(err instanceof UpstreamError && err.status === 404)) {
			return { status: 503, body: { detail: 'dispatch feed unreachable' } };
		}
		// An oracle without GET /dispatches/{id}: the event's ambulance list.
		try {
			const list = (await loadAmbulanceDispatches(fetchFn, eventId)).rows;
			row = list.find((d) => d.dispatch_id === dispatchId) ?? null;
		} catch (listErr) {
			const status = listErr instanceof UpstreamError && listErr.status === 404 ? 404 : 503;
			return { status, body: { detail: `dispatch feed ${status === 404 ? 'not deployed' : 'unreachable'}` } };
		}
	}
	// The uuid is the capability, but only for ITS event and only for 108:
	// a police or Jan Rakshak dispatch is never readable here.
	if (!row || row.event_id !== eventId || row.agency !== 'ambulance') return NO_SUCH;
	const d = row;

	const unitsRead = await attempt(loadUnits(fetchFn));
	const units = unitsRead.ok ? unitsRead.value.rows : [];
	const u =
		units.find((x) => x.dispatch_id === dispatchId) ??
		units.find((x) => x.unit_id === d.unit_id && x.assigned_event_id === eventId) ??
		null;
	const pos = u && Array.isArray(u.position) && u.position.length >= 2 ? u.position : null;
	const lonLat: [number, number] | null =
		u && typeof u.lon === 'number' && typeof u.lat === 'number'
			? [u.lon, u.lat]
			: pos && typeof pos[0] === 'number' && typeof pos[1] === 'number'
				? [pos[0], pos[1]]
				: null;
	const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
	const s = (v: unknown) => (typeof v === 'string' ? v : null);

	return {
		status: 200,
		body: {
			as_of: new Date(nowMs ?? Date.now()).toISOString(),
			dispatch: {
				dispatch_id: dispatchId,
				event_id: eventId,
				status: s(d.status) ?? 'requested',
				unit_id: s(d.unit_id),
				waypoint_junction: n(d.waypoint_junction),
				approved_at: s(d.approved_at),
				created_at: s(d.created_at),
				updated_at: s(d.updated_at),
			},
			unit:
				u && unitsRead.ok
					? {
							status: s(u.status),
							eta_sec: n(u.eta_sec),
							eta_at: new Date(unitsRead.value.at).toISOString(),
							progress: n(u.progress),
							arrived: u.arrived === true,
							position: lonLat,
							emulated: u.emulated !== false,
							source: s(u.source),
						}
					: null,
		},
	};
}
