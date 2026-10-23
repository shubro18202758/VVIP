/**
 * Public advisory derivation — the shared read-model behind /citizen, the
 * /driver stretch picker and the comms advisory writer.
 *
 * Everything here is PURE: the facts arrive already resolved (the public
 * projection in public-api.server.ts for /citizen and /driver, the comms
 * fact resolver in comms/facts.ts for the operator desk), and these
 * functions only turn them into hold rows, clocks and plain-language
 * tiers. Nothing here invents a number: a hold length that no closure
 * policy or adaptive parameter backs stays null and renders as "not
 * published yet"; an alternate the road graph does not offer stays null.
 * Every hold carries its provenance, and the adaptive layer's own label is
 * kept: its doctrine baseline (source 'baseline') is never presented as a
 * live adaptation.
 *
 * PUBLIC_EVENT_KINDS is the ONE allowlist of event kinds a public surface
 * (or a citizen-audience draft) may carry. vehicle_pursuit and
 * crime_incident are tactical police operations: publishing them could tip
 * off a suspect, so they never leave the operator surfaces.
 */

import { localizeNumber } from '$lib/i18n/strings';

/** Event kinds a public surface may show (everything else is operator-only). */
export const PUBLIC_EVENT_KINDS = [
	'protected_movement',
	'procession',
	'mass_gathering',
	'race',
	'ceremonial',
	'egress_surge',
	'emergency_evacuation',
	'work_zone',
] as const;

export type PublicEventKind = (typeof PUBLIC_EVENT_KINDS)[number];

export function isPublicKind(kind: string | null | undefined): kind is PublicEventKind {
	return typeof kind === 'string' && (PUBLIC_EVENT_KINDS as readonly string[]).includes(kind);
}

/**
 * Where a hold length came from — always rendered next to the minutes.
 *   closure_policy     the event's own phase closure plan
 *   adaptive_params    GET /adaptive/params adapted it to the live situation
 *                      (oracle source 'retrieved' | 'learned')
 *   adaptive_baseline  GET /adaptive/params answered its doctrine baseline
 *                      (oracle source 'baseline': no live adaptation for
 *                      this event yet — the 120 s constant, tier-floored)
 */
export type HoldSource = 'closure_policy' | 'adaptive_params' | 'adaptive_baseline';

export const HOLD_SOURCES: readonly HoldSource[] = ['closure_policy', 'adaptive_params', 'adaptive_baseline'];

/**
 * The provenance of an /adaptive/params answer from its own `source`
 * field. Only 'retrieved' and 'learned' are live adaptations; 'baseline',
 * a missing field or any unknown value is reported as the baseline —
 * never upgraded to "live".
 */
export function adaptiveHoldSource(source: unknown): Extract<HoldSource, 'adaptive_params' | 'adaptive_baseline'> {
	return source === 'retrieved' || source === 'learned' ? 'adaptive_params' : 'adaptive_baseline';
}

/** One stretch of an event spine as a citizen reads it (signage name first). */
export interface Stretch {
	segmentId: number;
	/** Road name as on city signage; null when the graph has none. */
	roadName: string | null;
	/** Nearest signalised junction (the green-corridor waypoint), if known. */
	junctionId: number | null;
}

/**
 * Largest advance closure (seconds) across an event's phase closure
 * policies — the legacy advisory's hold model. Null when no phase carries
 * one (the caller then asks the adaptive layer, never a default).
 */
export function phaseAdvanceClosureSec(
	phases: ReadonlyArray<{ closurePolicy?: { advanceClosureSec: number | null } | null }>,
): number | null {
	let best: number | null = null;
	for (const p of phases) {
		const sec = p.closurePolicy?.advanceClosureSec;
		if (typeof sec === 'number' && sec > 0 && (best === null || sec > best)) best = sec;
	}
	return best;
}

/** Hold minutes for an advance closure (≥ 1 min); unknown stays null. */
export function holdMinutesFromSec(sec: number | null | undefined): number | null {
	if (typeof sec !== 'number' || !(sec > 0)) return null;
	return Math.max(1, Math.round(sec / 60));
}

/** The city's clock: every public time is India Standard Time, whatever the phone's zone. */
const CITY_ZONE = 'Asia/Kolkata';

const HM_IST = new Intl.DateTimeFormat('en-GB', { timeZone: CITY_ZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const YMD_IST = new Intl.DateTimeFormat('en-CA', { timeZone: CITY_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' });
/** Day-and-month in each citizen language ('24 Oct' · '24 अक्टू॰' · '24 ઑક્ટો'). */
const DAY_FMT: Record<string, Intl.DateTimeFormat> = {
	en: new Intl.DateTimeFormat('en-GB', { timeZone: CITY_ZONE, weekday: 'short', day: 'numeric', month: 'short' }),
	hi: new Intl.DateTimeFormat('hi-IN', { timeZone: CITY_ZONE, weekday: 'short', day: 'numeric', month: 'short' }),
	gu: new Intl.DateTimeFormat('gu-IN', { timeZone: CITY_ZONE, weekday: 'short', day: 'numeric', month: 'short' }),
};

/** The IST calendar day of an instant (YYYY-MM-DD). */
export function istDayKey(ms: number): string {
	return YMD_IST.format(ms);
}

/** Localized IST date ('Fri, 24 Oct'), without the time. */
export function dayLabel(ms: number, lang: string): string {
	return localizeNumber((DAY_FMT[lang] ?? DAY_FMT.en).format(ms), lang);
}

/**
 * Localized HH:MM IST clock for an epoch-ms instant, prefixed with the
 * localized IST date when that day is not today's — a hold planned for
 * Friday never reads as this evening.
 */
export function clockAtMs(ms: number, lang: string, nowMs = Date.now()): string {
	if (!Number.isFinite(ms)) return '—';
	const hm = localizeNumber(HM_IST.format(ms), lang);
	return istDayKey(ms) === istDayKey(nowMs) ? hm : `${dayLabel(ms, lang)} ${hm}`;
}

/** Localized HH:MM clock for `offsetSec` seconds from now. */
export function clockAt(offsetSec: number, lang: string): string {
	return clockAtMs(Date.now() + offsetSec * 1000, lang);
}

/**
 * When the first stretch holds: the start of the earliest phase that has
 * not ended (or the event's planned start), never earlier than now — an
 * event planned for 18:00 holds at 18:00, not at "now".
 */
export function holdAnchorMs(
	event: {
		windowStart: string | null;
		phases: ReadonlyArray<{ startTime: string | null; endTime: string | null }>;
	},
	nowMs = Date.now(),
): number {
	const starts = event.phases
		.filter((p) => !p.endTime || Date.parse(p.endTime) > nowMs)
		.map((p) => (p.startTime ? Date.parse(p.startTime) : Number.NaN))
		.filter((t) => Number.isFinite(t));
	const planned = event.windowStart ? Date.parse(event.windowStart) : Number.NaN;
	const first = starts.length ? Math.min(...starts) : Number.isFinite(planned) ? planned : nowMs;
	return Math.max(nowMs, first);
}

export interface StretchHold {
	segmentId: number;
	roadName: string | null;
	/** Minutes the stretch is expected to hold. */
	minutes: number;
	/** Wall-clock start of the hold window, localized per language. */
	clock: string;
	state: 'now' | 'next' | 'later';
}

/** A hold is "next" only inside the citizen horizon ("your road, next 2 hours"). */
export const NEXT_HORIZON_MS = 2 * 3600_000;

/**
 * Stretch-by-stretch rolling holds (the documented rolling-hold model):
 * the first stretch holds at the anchor, each later stretch one hold
 * length after the previous. Unknown hold length ⇒ no rows (the card says
 * the length is not published rather than inventing one). Only an ACTIVE
 * event holds "now": a planned (planning/approved) event whose window has
 * started is still waiting for an officer to activate it, so its first
 * stretch reads "next", never "holding now" — and only when it starts
 * within NEXT_HORIZON_MS; a later start reads "later" with its dated clock.
 */
export function stretchHolds(
	stretches: readonly Stretch[],
	minutes: number | null,
	anchorMs: number,
	lang: string,
	active: boolean,
	nowMs = Date.now(),
): StretchHold[] {
	if (minutes == null) return [];
	const startsNow = active && anchorMs <= nowMs + 60_000;
	const soon = anchorMs <= nowMs + NEXT_HORIZON_MS;
	return stretches.slice(0, 6).map((s, i) => ({
		segmentId: s.segmentId,
		roadName: s.roadName,
		minutes,
		clock: clockAtMs(anchorMs + i * minutes * 60_000, lang, nowMs),
		state: i === 0 ? (startsNow ? 'now' : soon ? 'next' : 'later') : i === 1 && startsNow ? 'next' : 'later',
	}));
}

// ─── /driver: the green-corridor window ──────────────────────────────────────

/** Doctrine bounds of a green-corridor window (seconds). */
export const WINDOW_MIN_SEC = 60;
export const WINDOW_MAX_SEC = 180;

/** The event's advance closure held to the 60–180 s doctrine; unknown stays null. */
export function doctrineWindowSec(advanceClosureSec: number | null | undefined): number | null {
	if (typeof advanceClosureSec !== 'number' || !(advanceClosureSec > 0)) return null;
	return Math.min(WINDOW_MAX_SEC, Math.max(WINDOW_MIN_SEC, Math.round(advanceClosureSec)));
}

export interface WindowTiming {
	/** Server-clock epoch ms the corridor opens / closes. */
	opensAt: number;
	closesAt: number;
	/** 'eta': timed to the unit's projected arrival; 'approval': no projection. */
	anchor: 'eta' | 'approval';
}

/**
 * When an APPROVED request's window runs, on the server clock. Doctrine:
 * the corridor opens `windowSec` before the ambulance reaches the junction
 * and closes as it arrives. With a projected arrival (the oracle's
 * emulated unit, whose journey starts at approved_at) that is
 * [max(approved_at, arrival − window), arrival] — so the window never
 * closes before the ambulance gets there, and opens immediately when it is
 * already closer than one window. Without a projection (no waypoint, no
 * graph path, or an arrival not after the approval) the window runs
 * `windowSec` from approved_at. Unknown approval or length ⇒ null.
 */
export function windowTiming(
	approvedAtMs: number | null,
	arrivalAtMs: number | null,
	windowSec: number | null,
): WindowTiming | null {
	if (approvedAtMs == null || windowSec == null) return null;
	if (arrivalAtMs != null && arrivalAtMs > approvedAtMs) {
		return { opensAt: Math.max(approvedAtMs, arrivalAtMs - windowSec * 1000), closesAt: arrivalAtMs, anchor: 'eta' };
	}
	return { opensAt: approvedAtMs, closesAt: approvedAtMs + windowSec * 1000, anchor: 'approval' };
}

/** Where a timed window stands at `serverNowMs`. */
export function windowState(timing: WindowTiming, serverNowMs: number): 'primed' | 'open' | 'closed' {
	if (serverNowMs >= timing.closesAt) return 'closed';
	return serverNowMs >= timing.opensAt ? 'open' : 'primed';
}

/** An alternate route as citizens read it: signage names + the detour ids. */
export interface Alternate {
	roadNames: string[];
	segmentIds: number[];
}

/**
 * The alternate around a spine: among the k shortest paths between the
 * spine's two ends (GET /graph/k-shortest-paths — road_graph nodes are
 * segment ids), the one sharing the fewest INNER spine segments, named by
 * its detour's signage roads. Null when the graph offers no distinct,
 * named detour — the card then says so instead of naming a road.
 */
export function chooseAlternate(
	spine: readonly number[],
	paths: readonly (readonly number[])[],
	roadNameOf: (segmentId: number) => string | null | undefined,
): Alternate | null {
	if (spine.length < 2) return null;
	const spineKey = spine.join(',');
	const spineSet = new Set(spine);
	const inner = new Set(spine.slice(1, -1));
	let best: { detour: number[]; overlap: number; length: number } | null = null;
	for (const nodes of paths) {
		if (nodes.join(',') === spineKey) continue;
		const detour = nodes.filter((n) => !spineSet.has(n));
		if (detour.length === 0) continue;
		const overlap = nodes.filter((n) => inner.has(n)).length;
		if (best === null || overlap < best.overlap || (overlap === best.overlap && nodes.length < best.length)) {
			best = { detour, overlap, length: nodes.length };
		}
	}
	if (!best) return null;
	const roadNames: string[] = [];
	for (const sid of best.detour) {
		const name = roadNameOf(sid);
		if (name && !roadNames.includes(name)) roadNames.push(name);
	}
	return roadNames.length ? { roadNames: roadNames.slice(0, 2), segmentIds: best.detour } : null;
}

/** Plain-language strip tier for the citizen band strip (never the code). */
export type StripTier = 'calm' | 'watch' | 'hold' | 'severe';

/**
 * Citizens never see GREEN/AMBER/RED/BLACK — the ops band maps onto four
 * plain-language postures. No active public event ⇒ calm. With one
 * active: AMBER or an unreported/GREEN band ⇒ watch (a route is live, so
 * short holds are possible), RED ⇒ hold, BLACK ⇒ "priority corridor
 * traffic" wording — never the internal code.
 */
export function stripTier(band: string | null | undefined, anyActive: boolean): StripTier {
	if (!anyActive) return 'calm';
	switch (band) {
		case 'BLACK':
			return 'severe';
		case 'RED':
			return 'hold';
		default:
			return 'watch';
	}
}

const TIER_RANK: Record<StripTier, number> = { calm: 0, watch: 1, hold: 2, severe: 3 };

/** The most severe tier across events (the citizen bar's single posture). */
export function worstTier(tiers: ReadonlyArray<StripTier | null>): StripTier | null {
	let worst: StripTier | null = null;
	for (const t of tiers) {
		if (t && (worst === null || TIER_RANK[t] > TIER_RANK[worst])) worst = t;
	}
	return worst;
}
