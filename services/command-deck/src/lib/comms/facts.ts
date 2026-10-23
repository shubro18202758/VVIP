/**
 * Advisory fact resolver for the operator desk (/comms).
 *
 * Resolves an ops-board CorridorEvent into the AdvisoryFacts the writer
 * renders — the SAME derivation the public projection runs server-side
 * (public-api.server.ts), so a citizen draft on /comms matches /citizen:
 *
 *   kind      the oracle's RAW event_kind (GET /events/active), not the
 *             board mapper's: the mapper files an unknown kind under
 *             protected_movement, a public kind, so a new tactical kind
 *             would otherwise get a citizen draft. Unknown ⇒ withheld.
 *   hold      the phases' closure policy (max advance closure), else the
 *             advance closure GET /api/v1/adaptive/params publishes for
 *             the event (movement id when it has one), labelled by the
 *             oracle's own provenance (live adaptation vs doctrine
 *             baseline) — never a default
 *   stretch   the spine's first segment, named from /spatial/segments
 *   alternate GET /api/v1/graph/k-shortest-paths between the spine ends,
 *             named by the detour's signage roads (holds.chooseAlternate)
 *
 * Read-only GETs through the deck proxy's fixed /api/v1 prefix; segment
 * names cache for the page's life, paths for 10 min, params for 30 s, raw
 * kinds for 10 s. A read that fails degrades its fact to null AND is
 * flagged `unavailable`, so the draft says "unavailable right now" rather
 * than "not published" / "no alternate".
 */

import type { CorridorEvent } from '$lib/ops/types';
import { NETWORK_BBOX, ORACLE_V1 } from '$lib/ops/endpoints';
import {
	adaptiveHoldSource,
	chooseAlternate,
	holdAnchorMs,
	holdMinutesFromSec,
	phaseAdvanceClosureSec,
	type HoldSource,
} from '$lib/citizen/holds';
import type { AdvisoryFacts } from './advisory';

const PATHS_TTL_MS = 10 * 60_000;
const PARAMS_TTL_MS = 30_000;
const KINDS_TTL_MS = 10_000;

async function getJson(url: string): Promise<unknown> {
	const res = await fetch(url, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(8000) });
	if (!res.ok) throw new Error(`${res.status} ${url}`);
	return res.json();
}

let namesPromise: Promise<Map<number, string>> | null = null;
const pathsCache = new Map<string, { at: number; value: Promise<number[][]> }>();
const paramsCache = new Map<string, { at: number; value: Promise<AdaptiveClosure | null> }>();
const kindsCache = new Map<string, { at: number; value: Promise<Map<string, string>> }>();

/** An /adaptive/params answer: the advance closure and its provenance. */
interface AdaptiveClosure {
	sec: number;
	source: Extract<HoldSource, 'adaptive_params' | 'adaptive_baseline'>;
}

function roadNames(): Promise<Map<number, string>> {
	if (!namesPromise) {
		// The whole road network: a spine on the SP Ring Road (west of the
		// city view's CITY_BBOX) still gets its signage name in a draft.
		const q =
			`min_lon=${NETWORK_BBOX.minLon}&min_lat=${NETWORK_BBOX.minLat}` +
			`&max_lon=${NETWORK_BBOX.maxLon}&max_lat=${NETWORK_BBOX.maxLat}`;
		namesPromise = getJson(`${ORACLE_V1}/spatial/segments?${q}`).then((body) => {
			const out = new Map<number, string>();
			const rows = (body as { segments?: unknown[] })?.segments;
			for (const s of Array.isArray(rows) ? rows : []) {
				const r = s as Record<string, unknown>;
				if (typeof r.segment_id === 'number' && typeof r.road_name === 'string' && r.road_name) {
					out.set(r.segment_id, r.road_name);
				}
			}
			return out;
		});
		namesPromise.catch(() => {
			namesPromise = null;
		});
	}
	return namesPromise;
}

function memo<T>(
	store: Map<string, { at: number; value: Promise<T> }>,
	key: string,
	ttl: number,
	load: () => Promise<T>,
): Promise<T> {
	const hit = store.get(key);
	if (hit && Date.now() - hit.at < ttl) return hit.value;
	const value = load();
	store.set(key, { at: Date.now(), value });
	value.catch(() => {
		if (store.get(key)?.value === value) store.delete(key);
	});
	return value;
}

function kShortest(source: number, target: number): Promise<number[][]> {
	return memo(pathsCache, `${source}>${target}`, PATHS_TTL_MS, async () => {
		const body = (await getJson(
			`${ORACLE_V1}/graph/k-shortest-paths?source=${source}&target=${target}&k=4`,
		)) as { paths?: unknown[] };
		return (Array.isArray(body?.paths) ? body.paths : [])
			.filter(Array.isArray)
			.map((p) =>
				(p as unknown[])
					.map((step) => (step as Record<string, unknown>)?.node)
					.filter((n): n is number => typeof n === 'number'),
			)
			.filter((nodes) => nodes.length > 0);
	});
}

function advanceClosure(subject: string, protectionClass: string): Promise<AdaptiveClosure | null> {
	return memo(paramsCache, `${subject}|${protectionClass}`, PARAMS_TTL_MS, async () => {
		const body = (await getJson(
			`${ORACLE_V1}/adaptive/params?movement_id=${encodeURIComponent(subject)}` +
				`&vvip_class=${encodeURIComponent(protectionClass)}`,
		)) as { params?: { advance_closure_sec?: unknown }; source?: unknown };
		const sec = body?.params?.advance_closure_sec;
		if (typeof sec !== 'number' || !(sec > 0)) return null;
		return { sec, source: adaptiveHoldSource(body?.source) };
	});
}

/** event_id → the oracle's raw event_kind, off GET /events/active. */
function rawKinds(): Promise<Map<string, string>> {
	return memo(kindsCache, 'active', KINDS_TTL_MS, async () => {
		const body = (await getJson(`${ORACLE_V1}/events/active`)) as { events?: unknown[] } | unknown[];
		const rows = Array.isArray(body) ? body : Array.isArray(body?.events) ? body.events : [];
		const out = new Map<string, string>();
		for (const r of rows as Array<Record<string, unknown>>) {
			const id = r?.event_id;
			if ((typeof id === 'string' || typeof id === 'number') && typeof r.event_kind === 'string') {
				out.set(String(id), r.event_kind);
			}
		}
		return out;
	});
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

/** The movement id an event carries (additive field on the events API). */
export function movementIdOf(ev: CorridorEvent): string | null {
	const v = (ev as CorridorEvent & { movementId?: unknown }).movementId;
	return typeof v === 'string' && v ? v : null;
}

/** A stable key for "the facts of this event would not change". */
export function factsKey(ev: CorridorEvent): string {
	const adv = ev.phases.map((p) => p.closurePolicy?.advanceClosureSec ?? '').join(',');
	return `${ev.eventId}|${ev.eventKind}|${ev.routeSegmentIds.join(',')}|${adv}|${ev.windowStart ?? ''}`;
}

/** Resolve one ops-board event into the writer's facts (never throws). */
export async function resolveFacts(ev: CorridorEvent, nowMs = Date.now()): Promise<AdvisoryFacts> {
	const spine = ev.routeSegmentIds;
	const fromPolicy = phaseAdvanceClosureSec(ev.phases);
	const [names, params, paths, kinds] = await Promise.all([
		attempt(roadNames()),
		fromPolicy == null
			? attempt(advanceClosure(movementIdOf(ev) ?? ev.eventId, ev.protectionClass))
			: Promise.resolve<Attempt<AdaptiveClosure | null>>({ ok: true, value: null }),
		spine.length >= 2
			? attempt(kShortest(spine[0], spine[spine.length - 1]))
			: Promise.resolve<Attempt<number[][]>>({ ok: true, value: [] }),
		attempt(rawKinds()),
	]);
	const nameOf = (sid: number) => (names.ok ? names.value.get(sid) : undefined);
	const adaptive = params.ok ? params.value : null;
	const advance = fromPolicy ?? adaptive?.sec ?? null;
	const source: HoldSource | null = fromPolicy != null ? 'closure_policy' : (adaptive?.source ?? null);
	const first = spine[0] ?? null;
	return {
		eventId: ev.eventId,
		// The raw kind when the oracle answered (an unknown kind is then
		// withheld from citizens); the board's kind only as a fallback.
		eventKind: (kinds.ok ? kinds.value.get(ev.eventId) : undefined) ?? ev.eventKind,
		stretch: { segmentId: first, roadName: first == null ? null : (nameOf(first) ?? null) },
		holdMinutes: holdMinutesFromSec(advance),
		holdSource: source,
		alternate: chooseAlternate(spine, paths.ok ? paths.value : [], nameOf)?.roadNames ?? null,
		anchorMs: holdAnchorMs(ev, nowMs),
		unavailable: {
			hold: fromPolicy == null && !params.ok,
			alternate: spine.length >= 2 && (!paths.ok || !names.ok),
		},
		emulated: ev.emulated === true,
	};
}
