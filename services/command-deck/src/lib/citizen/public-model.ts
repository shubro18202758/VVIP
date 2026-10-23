/**
 * The public read-model — what /citizen and /driver are allowed to know.
 *
 * GET /api/public/events (served by the deck itself, public-api.server.ts)
 * answers with PublicFeedWire: public-kind events only, projected down to
 * the fields a citizen advisory needs — kind, lifecycle, the PRIMARY spine
 * as signage road names, phase windows, the resolved hold length with its
 * provenance, an alternate from the road graph, a plain-language tier, and
 * which of those facts could not be READ this time (`unavailable`) — so
 * "the oracle did not answer" never renders as "there is none".
 * Titles, descriptions, metadata, protection classes and contingency
 * spines never leave the server, and non-public kinds are dropped before
 * the response is written (and again here, defence in depth).
 *
 * mapPublicEvent() is the tolerant browser-side mapper (unknown/missing
 * fields degrade to null, never to invented values); fixturePublicEvents()
 * is the LABELLED offline preview used only while the feed is unreachable.
 *
 * Provenance (`source`): 'live' is a real corridor event; 'emulated' is one
 * the event emulator opened (wire `emulated: true`) — badged SIMULATED FEED
 * · EMULATED on every public surface, never LIVE, its routes never drawn as
 * live and never offered to a 108 driver as a real corridor;
 * 'offline-fixture' is the labelled preview. `later` holds public events
 * that start beyond the citizen horizon (dated heads-up rows only).
 */

import { USE_CASES } from '$lib/ops/usecases';
import {
	HOLD_SOURCES,
	isPublicKind,
	holdMinutesFromSec,
	phaseAdvanceClosureSec,
	type HoldSource,
	type PublicEventKind,
	type Stretch,
	type StripTier,
} from './holds';

/**
 * Facts the projection could not read on this pass (upstream error or
 * timeout) — distinct from "none exists": a failed /adaptive/params read is
 * 'hold', a failed k-shortest-paths (or road-name) read is 'alternate',
 * failed signage names are 'road_names'.
 */
export type UnavailableFact = 'hold' | 'alternate' | 'road_names';

const UNAVAILABLE_FACTS: readonly UnavailableFact[] = ['hold', 'alternate', 'road_names'];

// ─── Wire (snake_case, deck server → browser) ────────────────────────────────

export interface PublicEventWire {
	event_id: string;
	event_kind: PublicEventKind;
	status: 'planning' | 'approved' | 'active';
	window_start: string | null;
	window_end: string | null;
	stretches: Array<{ segment_id: number; road_name: string | null; junction_id: number | null }>;
	phases: Array<{
		label: string;
		window_start: string | null;
		window_end: string | null;
		advance_closure_sec: number | null;
	}>;
	hold: { minutes: number | null; advance_closure_sec: number | null; source: HoldSource | null };
	alternate: { road_names: string[]; segment_ids: number[]; source: 'k_shortest_paths' } | null;
	tier: StripTier | null;
	/** Facts that could not be read this pass (empty when all answered). */
	unavailable: UnavailableFact[];
	/** Opened by the event emulator (metadata.source 'event-emulator'). */
	emulated: boolean;
}

/** A public event beyond the citizen horizon — a dated heads-up only. */
export interface PublicLaterWire {
	event_id: string;
	event_kind: PublicEventKind;
	status: 'planning' | 'approved' | 'active';
	/** Earliest planned start (event or phase), ISO; null when unknown. */
	starts_at: string | null;
	window_end: string | null;
	/** Up to three signage road names of the primary spine. */
	road_names: string[];
	/**
	 * True for a protected (VVIP) movement: its route is announced inside
	 * the 2 h horizon only, so its Later row carries the date and no roads.
	 */
	roads_withheld?: boolean;
	emulated: boolean;
}

export interface PublicFeedWire {
	as_of: string;
	events: PublicEventWire[];
	/** Public events starting beyond the horizon (soonest first). */
	later: PublicLaterWire[];
	/** Worst plain-language tier across the REAL public events (null = unknown). */
	tier: StripTier | null;
	/**
	 * Contract C-M: this build labels emulated events (SIMULATED FEED ·
	 * EMULATED). `veer_up.sh --demo` refuses the living city without it.
	 */
	emulated_labelling: true;
}

// ─── Browser shape (camelCase) ───────────────────────────────────────────────

export interface PublicPhase {
	label: string;
	startTime: string | null;
	endTime: string | null;
	closurePolicy: { advanceClosureSec: number | null } | null;
}

export interface PublicCorridorEvent {
	eventId: string;
	eventKind: PublicEventKind;
	/** planning/approved ⇒ planned; active ⇒ active. */
	state: 'planned' | 'active';
	windowStart: string | null;
	windowEnd: string | null;
	stretches: Stretch[];
	phases: PublicPhase[];
	hold: { minutes: number | null; advanceClosureSec: number | null; source: HoldSource | null };
	alternate: { roadNames: string[]; segmentIds: number[] } | null;
	tier: StripTier | null;
	/** Which facts were unreadable this pass (vs genuinely absent). */
	unavailable: { hold: boolean; alternate: boolean; roadNames: boolean };
	/**
	 * live = a real event on the deck's public projection; emulated = an
	 * event-emulator run (SIMULATED FEED); offline-fixture = labelled preview.
	 */
	source: 'live' | 'emulated' | 'offline-fixture';
}

/** A later public event as the citizen page lists it. */
export interface PublicLaterEvent {
	eventId: string;
	eventKind: PublicEventKind;
	/** Epoch ms of the earliest planned start; null when unknown. */
	startsAt: number | null;
	endsAt: number | null;
	roadNames: string[];
	/** The route is announced closer to the start (a protected movement). */
	roadsWithheld: boolean;
	source: 'live' | 'emulated';
}

const TIERS: readonly StripTier[] = ['calm', 'watch', 'hold', 'severe'];

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length > 0 ? v : null;
}

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function pick<T extends string>(v: unknown, allowed: readonly T[]): T | null {
	return typeof v === 'string' && (allowed as readonly string[]).includes(v) ? (v as T) : null;
}

/** Tolerant mapper for one wire event; non-public kinds map to null. */
export function mapPublicEvent(raw: unknown): PublicCorridorEvent | null {
	if (!raw || typeof raw !== 'object') return null;
	const w = raw as Record<string, unknown>;
	const eventId = str(w.event_id);
	const kind = w.event_kind;
	if (!eventId || !isPublicKind(typeof kind === 'string' ? kind : null)) return null;
	const status = str(w.status);
	if (status !== 'planning' && status !== 'approved' && status !== 'active') return null;

	const stretches: Stretch[] = [];
	for (const s of Array.isArray(w.stretches) ? w.stretches : []) {
		const r = s as Record<string, unknown>;
		const segmentId = num(r?.segment_id);
		if (segmentId == null) continue;
		stretches.push({ segmentId, roadName: str(r.road_name), junctionId: num(r.junction_id) });
	}
	const phases: PublicPhase[] = (Array.isArray(w.phases) ? w.phases : []).map((p) => {
		const r = (p ?? {}) as Record<string, unknown>;
		const adv = num(r.advance_closure_sec);
		return {
			label: str(r.label) ?? 'phase',
			startTime: str(r.window_start),
			endTime: str(r.window_end),
			closurePolicy: adv == null ? null : { advanceClosureSec: adv },
		};
	});
	const hold = (w.hold ?? {}) as Record<string, unknown>;
	const alt = w.alternate && typeof w.alternate === 'object' ? (w.alternate as Record<string, unknown>) : null;
	const altNames = alt && Array.isArray(alt.road_names) ? alt.road_names.filter((n): n is string => typeof n === 'string') : [];
	const altSegs = alt && Array.isArray(alt.segment_ids) ? alt.segment_ids.filter((n): n is number => typeof n === 'number') : [];
	const missing = new Set(
		(Array.isArray(w.unavailable) ? w.unavailable : []).filter((f): f is UnavailableFact =>
			(UNAVAILABLE_FACTS as readonly unknown[]).includes(f),
		),
	);

	return {
		eventId,
		eventKind: kind as PublicEventKind,
		state: status === 'active' ? 'active' : 'planned',
		windowStart: str(w.window_start),
		windowEnd: str(w.window_end),
		stretches,
		phases,
		hold: {
			minutes: num(hold.minutes),
			advanceClosureSec: num(hold.advance_closure_sec),
			source: pick(hold.source, HOLD_SOURCES),
		},
		alternate: altNames.length ? { roadNames: altNames, segmentIds: altSegs } : null,
		tier: pick(w.tier, TIERS),
		unavailable: { hold: missing.has('hold'), alternate: missing.has('alternate'), roadNames: missing.has('road_names') },
		source: w.emulated === true ? 'emulated' : 'live',
	};
}

/** Tolerant mapper for one Later row; non-public kinds map to null. */
export function mapLaterEvent(raw: unknown): PublicLaterEvent | null {
	if (!raw || typeof raw !== 'object') return null;
	const w = raw as Record<string, unknown>;
	const eventId = str(w.event_id);
	const kind = w.event_kind;
	if (!eventId || !isPublicKind(typeof kind === 'string' ? kind : null)) return null;
	const startsAt = str(w.starts_at) ? Date.parse(w.starts_at as string) : Number.NaN;
	const endsAt = str(w.window_end) ? Date.parse(w.window_end as string) : Number.NaN;
	const roadsWithheld = w.roads_withheld === true;
	return {
		eventId,
		eventKind: kind as PublicEventKind,
		startsAt: Number.isFinite(startsAt) ? startsAt : null,
		endsAt: Number.isFinite(endsAt) ? endsAt : null,
		roadNames: roadsWithheld
			? []
			: (Array.isArray(w.road_names) ? w.road_names : []).filter((n): n is string => typeof n === 'string').slice(0, 3),
		roadsWithheld,
		source: w.emulated === true ? 'emulated' : 'live',
	};
}

/** Map a whole feed body; returns null when the body is not a feed. */
export function mapPublicFeed(
	raw: unknown,
): { asOf: number; events: PublicCorridorEvent[]; later: PublicLaterEvent[]; tier: StripTier | null } | null {
	if (!raw || typeof raw !== 'object' || !Array.isArray((raw as Record<string, unknown>).events)) return null;
	const body = raw as Record<string, unknown>;
	const asOf = typeof body.as_of === 'string' ? Date.parse(body.as_of) : Number.NaN;
	return {
		asOf: Number.isFinite(asOf) ? asOf : Date.now(),
		events: (body.events as unknown[])
			.map(mapPublicEvent)
			.filter((e): e is PublicCorridorEvent => e !== null),
		later: (Array.isArray(body.later) ? body.later : [])
			.map(mapLaterEvent)
			.filter((e): e is PublicLaterEvent => e !== null),
		tier: pick(body.tier, TIERS),
	};
}

/**
 * The LABELLED offline preview: the use-case library's public archetypes,
 * shown only while the public feed is unreachable (never mixed with live
 * rows). Road names are unknown offline, so stretches carry ids only.
 */
export function fixturePublicEvents(): PublicCorridorEvent[] {
	return USE_CASES.filter((u) => isPublicKind(u.eventKind))
		.slice(0, 2)
		.map((u) => {
			const advance = phaseAdvanceClosureSec(u.phases);
			return {
				eventId: u.id,
				eventKind: u.eventKind as PublicEventKind,
				state: 'planned' as const,
				windowStart: u.departureTime,
				windowEnd: null,
				stretches: u.routeSegmentIds.map((segmentId) => ({ segmentId, roadName: null, junctionId: null })),
				phases: u.phases.map((p) => ({
					label: p.label,
					startTime: p.startTime,
					endTime: p.endTime,
					closurePolicy: p.closurePolicy ? { advanceClosureSec: p.closurePolicy.advanceClosureSec } : null,
				})),
				hold: {
					minutes: holdMinutesFromSec(advance),
					advanceClosureSec: advance,
					source: advance == null ? null : ('closure_policy' as const),
				},
				alternate: null,
				tier: null,
				unavailable: { hold: false, alternate: false, roadNames: false },
				source: 'offline-fixture' as const,
			};
		});
}
