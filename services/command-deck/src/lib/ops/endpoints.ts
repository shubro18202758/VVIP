/**
 * Fixed endpoint constants + outbound-URL guard for the corridor ops showcase.
 *
 * SECURITY CONTRACT (showcase stream, applies to all outbound HTTP/WS client
 * code in src/lib/ops and src/lib/citizen):
 *
 *   1. Every internal service endpoint is a FIXED CONFIG CONSTANT declared in
 *      this module. No URL is ever assembled from operator input or from data
 *      returned by an API.
 *   2. The ONLY outbound schemes permitted are http/https (ws:/wss: are the
 *      same-origin upgrade counterparts and are constructed exclusively from
 *      the page origin + the fixed path templates below).
 *   3. Any URL that originates from EXTERNAL INPUT (API payloads, operator
 *      text, query strings) MUST pass `assertSafeExternalUrl()` before a
 *      request is made. The guard rejects non-http(s) schemes and hosts that
 *      are localhost, loopback, private, or reserved — so a hostile payload
 *      can never steer the deck's fetch/WebSocket at internal surfaces.
 *      The fixed internal constants above are exempt by design (they are not
 *      external input); that exemption is exactly the "fixed internal
 *      endpoints are config constants" carve-out.
 *   4. Path parameters interpolated into the fixed templates (event ids,
 *      movement ids) are validated by `safePathId()` against a strict
 *      allowlist regex — they can never change the shape of the URL.
 */

// ─── Fixed internal endpoints (config constants) ────────────────────────────

/** traffic-oracle REST surface, served through the deck origin proxy. */
export const ORACLE_V1 = '/api/v1';

/** convoy-brain REST/NDJSON surface, served through the deck origin proxy. */
export const BRAIN_API = '/api/convoy';

/**
 * Event emulator. The PRIMARY path is the same-origin proxy prefix
 * EMULATOR_PROXY_PATH — the vite dev proxy in dev and src/hooks.server.ts in
 * the adapter-node build both forward `/api/emulator → 127.0.0.1:8090`, so
 * no browser ever needs the emulator's CORS allowlist (which covers only the
 * :5173 dev origin; the argus deck serves from :5273 and every absolute call
 * from there was CORS-blocked before falling back).
 *
 * EMULATOR_BASE (absolute loopback :8090) is a DEV-ONLY second rung, tried
 * only under the vite dev server when the proxy entry is missing. Production
 * never requests it.
 */
export const EMULATOR_BASE = 'http://localhost:8090';
export const EMULATOR_PROXY_PATH = '/api/emulator';

/** Poll cadences (ms). The audit poll is the 2 s ladder rung from the plan. */
export const POLL_MS = {
	board: 5000,
	audit: 2000,
	band: 5000,
	emulator: 10000,
	/** God's-eye canvas layers (vehicles/signals/units/cameras + segments). */
	canvas: 8000,
	/** Enforcement recommendation queue (pending e-challans). */
	enforcement: 6000,
	/** Jan Rakshak / agency dispatch lifecycle. */
	dispatch: 6000,
	/** City signal-plan writer posture (gate + last plan). */
	signals: 10000,
	/** Watchlist (earmarked vehicles + last sightings). */
	watchlist: 8000,
	/** Degradation ledger (decisions_log failure taxonomy). */
	ledger: 10000,
	/** Interception plan, only while a LIVE vehicle_pursuit is selected. */
	interception: 6000,
	/** Cockpit corridor summary (stats pill, live rail, escort HUD) + the ops feed-provenance chip. */
	corridor: 10000,
	/** City narrative (GET /city/feed) while the stream follows the city. */
	feed: 3000,
	/** Brain posture strip (GET /api/convoy/health + /adaptive/status). */
	brain: 10000,
	/** Sighting-retention reaper health (GET /vehicles/retention). */
	retention: 60000,
	/** Junction registry + segment lines (static geometry; names for waypoints). */
	geometry: 300000,
	/** Approved recommendations (the pursuit chain's human approval, C6). */
	approved: 15000,
	/** One emulator run's progress while it runs (C-G GET /runs/{id}). */
	emulatorRun: 2000,
} as const;

/**
 * Network hygiene: a GET path that answered 404 (route not deployed, or no
 * such row) is not requested again for this long — callers get the same
 * not-deployed answer from memory instead of a request per poll tick.
 */
export const NOT_DEPLOYED_BACKOFF_MS = 60_000;

/** WebSocket ladder grace before falling through to the next rung. */
export const WS_OPEN_TIMEOUT_MS = 4000;

// ─── God's-eye canvas endpoints (city-canvas stream, contract C1) ───────────
//
// All fixed path templates under the deck origin — served by the vite dev
// proxy in dev and by src/hooks.server.ts in the adapter-node build. Each
// list endpoint answers an ENVELOPE ({vehicles|junctions|units|cameras: []});
// a 404 degrades only the matching canvas layer (chip state "not deployed"),
// never the board.

/** Road-network geometry for event spines (existing live endpoint). */
export const SPATIAL_SEGMENTS_API = `${ORACLE_V1}/spatial/segments`;

/** Earmarked vehicles + last sighting: {vehicles: [...]} (watchlist-anpr). */
export const VEHICLES_API = `${ORACLE_V1}/vehicles`;

/**
 * Per-vehicle trace: GET /{id}/trace?window_sec= (404 'no sightings in
 * window' on older oracles; 200 with empty geometry once WP-03 lands).
 */
export const VEHICLE_TRACE_API = `${ORACLE_V1}/vehicles`; // /{id}/trace

/** Per-vehicle raw sightings (newest first) — the trace's fallback rung. */
export const VEHICLE_SIGHTINGS_API = `${ORACLE_V1}/vehicles`; // /{id}/sightings?limit=

/** City signal-program state: {junctions: [...], city_band} (signalling). */
export const SIGNALS_STATE_API = `${ORACLE_V1}/signals/state`;

/** Agency unit markers: {units: [...]} — dispatch projections + emulated patrols. */
export const UNITS_API = `${ORACLE_V1}/units`;

/** Camera registry {cameras: [...]} — locations + read counts ONLY, never frames (C-C). */
export const CAMERAS_API = `${ORACLE_V1}/cameras`;

/**
 * God's-eye imagery mode (same-origin): which basemap the canvas may use —
 * keyless OSM default, or the env-keyed ion upgrade when the operator set
 * VEER_CESIUM_ION_TOKEN on the gods-eye-deck service. The token itself is
 * read server-side (routes/gods-eye/mode/+server.ts) and never appears in
 * source; an ion token is a Cesium client credential by design, so handing
 * it to the same-origin page is the documented upgrade path.
 */
export const GODS_EYE_MODE_API = '/gods-eye/mode';

/**
 * The city bbox the canvas boots over (Ahmedabad seed network —
 * corridor-store seed/sample_corridor.sql). Fixed constant, not input.
 */
export const CITY_BBOX = { minLon: 72.5, minLat: 22.95, maxLon: 72.7, maxLat: 23.12 } as const;

/**
 * The city's road-network extent for geometry reads (contract C-J's city
 * bbox: lon 72.40–72.80, lat 22.90–23.20). CITY_BBOX is the canvas's boot
 * VIEW and stops at lon 72.50 — it drops the SP Ring Road (West) segment
 * at lon 72.48–72.49, so a sighting there would have no road name and no
 * pursuit anchor. Waypoints, anchors and spine names read through this.
 */
export const NETWORK_BBOX = { minLon: 72.4, minLat: 22.9, maxLon: 72.8, maxLat: 23.2 } as const;

// ─── Path parameter validation ───────────────────────────────────────────────

/**
 * Validate an id interpolated into a fixed path template. Accepts only short
 * URL-safe tokens (UUIDs, slugs like `rath_yatra_ahmedabad`). Returns the id
 * if safe, else null — callers must refuse the request when null.
 */
export function safePathId(id: string | null | undefined): string | null {
	if (typeof id !== 'string') return null;
	return /^[A-Za-z0-9_.-]{1,64}$/.test(id) ? id : null;
}

// ─── External-input URL guard ────────────────────────────────────────────────

export interface UrlGuardResult {
	ok: boolean;
	reason?: string;
}

/**
 * Guard for URLs that come from EXTERNAL INPUT. Rejects:
 *   - any scheme other than http/https (blocks file:, ws:, data:, …)
 *   - credentials embedded in the URL (user:pass@host)
 *   - localhost and *.localhost names
 *   - IPv4 loopback (127.0.0.0/8), private (10/8, 172.16/12, 192.168/16),
 *     link-local (169.254/16), reserved (0/8, 100.64/10, 192.0.0/24,
 *     198.18/15, 224/4 multicast, 240/4, 255.255.255.255)
 *   - IPv6 loopback/unspecified/link-local/unique-local (::1, ::, fe80::/10,
 *     fc00::/7), and IPv4-mapped IPv6 loopbacks
 *
 * Fixed internal endpoints (EMULATOR_BASE etc.) do NOT flow through this guard
 * — they are constants defined above, never external input.
 */
export function assertSafeExternalUrl(raw: string): UrlGuardResult {
	let url: URL;
	try {
		url = new URL(raw);
	} catch {
		return { ok: false, reason: 'malformed URL' };
	}

	if (url.protocol !== 'http:' && url.protocol !== 'https:') {
		return { ok: false, reason: `scheme "${url.protocol}" not permitted (http/https only)` };
	}
	if (url.username || url.password) {
		return { ok: false, reason: 'embedded credentials are not permitted' };
	}

	const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, '');

	if (host === 'localhost' || host.endsWith('.localhost')) {
		return { ok: false, reason: 'localhost is not permitted for external URLs' };
	}

	if (host.includes(':')) {
		// IPv6 literal
		if (
			host === '::1' ||
			host === '::' ||
			host.startsWith('fe8') ||
			host.startsWith('fe9') ||
			host.startsWith('fea') ||
			host.startsWith('feb') ||
			host.startsWith('fc') ||
			host.startsWith('fd')
		) {
			return { ok: false, reason: `IPv6 address "${host}" is loopback/link-local/unique-local` };
		}
		// IPv4-mapped IPv6, dotted form (::ffff:127.0.0.1) or the hex form the
		// URL parser canonicalizes to (::ffff:7f00:1) — both must reduce to the
		// IPv4 check or a mapped loopback/private address slips through.
		const v4mappedDotted = host.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
		if (v4mappedDotted) return checkIpv4(v4mappedDotted[1]);
		const v4mappedHex = host.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
		if (v4mappedHex) {
			const hi = parseInt(v4mappedHex[1], 16);
			const lo = parseInt(v4mappedHex[2], 16);
			if (Number.isFinite(hi) && Number.isFinite(lo)) {
				return checkIpv4(`${hi >> 8}.${hi & 0xff}.${lo >> 8}.${lo & 0xff}`);
			}
		}
		return { ok: true };
	}

	if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
		return checkIpv4(host);
	}

	// Bare IPv6 without colons can't happen; anything else is a DNS name.
	return { ok: true };
}

function checkIpv4(host: string): UrlGuardResult {
	const parts = host.split('.').map((p) => parseInt(p, 10));
	if (parts.length !== 4 || parts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) {
		return { ok: false, reason: `malformed IPv4 "${host}"` };
	}
	const [a, b] = parts;
	const reject = (why: string): UrlGuardResult => ({ ok: false, reason: `IPv4 ${host}: ${why}` });
	if (a === 0) return reject('reserved 0.0.0.0/8');
	if (a === 10) return reject('private 10/8');
	if (a === 127) return reject('loopback 127/8');
	if (a === 169 && b === 254) return reject('link-local 169.254/16');
	if (a === 172 && b >= 16 && b <= 31) return reject('private 172.16/12');
	if (a === 192 && b === 168) return reject('private 192.168/16');
	if (a === 100 && b >= 64 && b <= 127) return reject('CGNAT 100.64/10');
	if (a === 192 && b === 0) return reject('reserved 192.0.0/24');
	if (a === 198 && (b === 18 || b === 19)) return reject('benchmark 198.18/15');
	if (a >= 224) return reject('multicast/reserved 224/4+');
	return { ok: true };
}

// ─── City-writer + decision surfaces (ops-screens stream) ────────────────────
//
// Every endpoint below is a FIXED path template under the deck origin, same
// contract as the block above. The POST halves are CITY-WRITER endpoints:
// they change shared city state and are therefore (a) gated on a named human
// actor client-side (CommitRefused when the officer identity is blank — the
// C6 ethics gate) and (b) STUBBED in every smoke test (tests never fire them
// against the fleet; see tests/ops-write-smoke.mjs).

/** Enforcement recommendations (e-challan queue). Prefix for GET /recommendations. */
export const ENFORCEMENT_API = `${ORACLE_V1}/enforcement`;

/** Agency dispatch lifecycle. GET /dispatches, POST /dispatches/{id}/decision|/status. */
export const DISPATCHES_API = `${ORACLE_V1}/dispatches`;

/** Corridor events (board reads /events/active here already; POST creates). */
export const EVENTS_API = `${ORACLE_V1}/events`;

/** City signal plans: GET /plans, POST /plans/apply. */
export const SIGNALS_PLANS_API = `${ORACLE_V1}/signals/plans`;

/** Signal program snapshots (applies + restores): GET /signals/history. */
export const SIGNALS_HISTORY_API = `${ORACLE_V1}/signals/history`;

/** Interception choke ranking: GET /events/{id}/interception-plan (read-only). */
export const INTERCEPTION_PLAN_PATH = (eventId: string) =>
	`${EVENTS_API}/${eventId}/interception-plan`;

/** Event lifecycle (contract C-F): POST /events/{id}/status — CITY WRITER. */
export const EVENT_STATUS_PATH = (eventId: string) => `${EVENTS_API}/${eventId}/status`;

/** One event's audit narrative (contract C-C): GET /events/{id}/audit. */
export const EVENT_AUDIT_PATH = (eventId: string) => `${EVENTS_API}/${eventId}/audit`;

/** The city narrative (contract C-C): GET /city/feed?since=&limit=&kinds=. */
export const CITY_FEED_API = `${ORACLE_V1}/city/feed`;

/** The fused city picture (contract C-C) — the junction registry is read from it. */
export const CITY_ENTITIES_API = `${ORACLE_V1}/city/entities`;

/** Sighting-retention policy + reaper health (C6 / DPDP, WP-03). */
export const VEHICLE_RETENTION_API = `${ORACLE_V1}/vehicles/retention`;

/** Corridor summary — its quality_mix says whether the traffic feed is simulated. */
export const CORRIDOR_SUMMARY_API = `${ORACLE_V1}/corridor/summary`;

/** Adaptive degradation + per-subject situation (read-only). */
export const ADAPTIVE_STATUS_API = `${ORACLE_V1}/adaptive/status`;

/** convoy-brain health: decider tier (model host, p95, latch) + Gemma tier. */
export const BRAIN_HEALTH_API = `${BRAIN_API}/health`;

/**
 * Event lifecycle transitions (contract C-F, mirrored from
 * traffic_oracle/events EVENT_TRANSITIONS): the verb rail offers only these.
 */
export const EVENT_TRANSITIONS: Record<string, readonly ('approved' | 'active' | 'completed' | 'cancelled')[]> = {
	planning: ['approved', 'active', 'cancelled'],
	approved: ['active', 'cancelled'],
	active: ['completed', 'cancelled'],
	completed: [],
	cancelled: [],
};

/**
 * Sighting → road attribution tolerance: the oracle's interception plan
 * seeds from the nearest segment within this distance
 * (vehicles/pursuit.py SEGMENT_ATTRIBUTION_TOL_M) — a pursuit anchors the
 * same way, or not at all.
 */
export const PURSUIT_ANCHOR_TOL_M = 300;

// ─── Emulator control plane (contract C-G, via the /api/emulator proxy) ─────

/** Clock compressions the panel offers. */
export const EMULATOR_CLOCKS = [1, 10, 60] as const;

/**
 * Flagship ('hero') tapes are capped at ×10: at ×60 one 5 s engine tick
 * spans 5 simulated minutes and the flagship's 300-s green-corridor windows
 * alias away (audit: clock-compression aliasing).
 */
export const EMULATOR_HERO_CLOCK_CAP = 10;
export const EMULATOR_HERO_CATEGORIES: readonly string[] = ['multi_event', 'enforcement'];
export const EMULATOR_HERO_USE_CASES: readonly string[] = ['rath_yatra_monsoon_ambulance', 'watchlist_anpr_sweep'];

/** Suggested unit id per agency for a new request (emulated fleet labels). */
export const DEFAULT_UNIT_ID: Record<string, string> = {
	jan_rakshak: 'JR-EM-01',
	police: 'PCR-EM-01',
	traffic: 'TP-EM-01',
	ambulance: '108-EM-01',
	fire: 'FIRE-EM-01',
};

/**
 * Degradation ledger: GET /ops/degradation-taxonomy — the recurring-failure
 * taxonomy convoy-brain's decisions_log actually carries (latch firings,
 * tier-0 misses, tactical fallbacks, operator alerts, failed turns), mined
 * by the oracle out of the shared Valkey. Empty log → honest empty ledger.
 */
export const DEGRADATION_TAXONOMY_API = `${ORACLE_V1}/ops/degradation-taxonomy`;

/**
 * The closed agency registry (migration 011 CHECK values mirrored from
 * traffic_oracle/units/agencies.py). Kept as a fixed constant so the UI
 * never offers an agency the backend would 400 on.
 */
export const AGENCIES = {
	jan_rakshak: { label: 'Jan Rakshak', labelNative: 'જન રક્ષક', role: 'citizen volunteer coordination' },
	police: { label: 'Police Control', labelNative: 'પોલીસ કંટ્રોલ', role: 'law & order' },
	traffic: { label: 'Traffic Police', labelNative: 'ટ્રાફિક પોલીસ', role: 'traffic management' },
	ambulance: { label: '108 Ambulance', labelNative: '૧૦૮ એમ્બ્યુલાન્સ', role: 'emergency medical' },
	fire: { label: 'Fire & Emergency', labelNative: 'ફાયર અને ઇમરજન્સી', role: 'fire & rescue' },
} as const;

export type AgencyCode = keyof typeof AGENCIES;

/**
 * Lawful-basis codes for vehicle earmarks (C6: NOT NULL, enumerated —
 * mirrored from traffic_oracle/vehicles/__init__.py LAWFUL_BASIS_CODES).
 */
export const LAWFUL_BASIS_CODES = [
	'mva_1988_s177',
	'mva_1988_s183',
	'mva_1988_s207',
	'bnss_2023_s94',
	'stolen_vehicle_fir',
] as const;

export type LawfulBasisCode = (typeof LAWFUL_BASIS_CODES)[number];

/** Earmark states the writer accepts (watch | flagged). */
export const EARMARK_STATES = ['watch', 'flagged'] as const;

/** Dispatch lifecycle states (mirrors DISPATCH_TRANSITIONS server-side). */
export const DISPATCH_STATES = [
	'requested',
	'approved',
	'en_route',
	'on_scene',
	'completed',
	'cancelled',
	'deferred',
] as const;

export type DispatchState = (typeof DISPATCH_STATES)[number];

/** Legal dispatch transitions for the tiles' verb rail (server mirror). */
export const DISPATCH_TRANSITIONS: Record<DispatchState, readonly DispatchState[]> = {
	requested: ['approved', 'deferred', 'cancelled'],
	approved: ['en_route', 'cancelled'],
	en_route: ['on_scene', 'cancelled'],
	on_scene: ['completed', 'cancelled'],
	deferred: [],
	completed: [],
	cancelled: [],
};

/**
 * Every city-writer POST path this stream can fire. tests/ops-write-smoke.mjs
 * asserts each is (1) reached only through a commit-gated client function and
 * (2) never called by a smoke. Fixed templates only — no dynamic hosts.
 */
export const CITY_WRITER_POST_PATHS = [
	'/api/v1/enforcement/recommendations/', // + {id}/decision
	'/api/v1/events/', // + {event_id}/dispatch | {event_id}/status (C-F), and POST /api/v1/events itself
	'/api/v1/dispatches/', // + {id}/decision | /status
	'/api/v1/signals/plans/apply',
	'/api/v1/signals/restore',
	'/api/v1/vehicles/', // + {plate}/earmark | {vehicle_id}/earmark/clear
] as const;
