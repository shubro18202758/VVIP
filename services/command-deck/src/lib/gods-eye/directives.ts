/**
 * Canvas directives (contract C-D2) — the PURE validator between the agent's
 * proposals and the God's-eye canvas.
 *
 * The brain streams `{type:'directive', data:{id, verb, args, label,
 * provenance}}`; nothing reaches the camera unless it passes here AND a
 * human presses Apply. Accepted:
 *
 *   verbs     the six canvas verbs only (fly_to, track_entity, highlight,
 *             annotate, set_sensor, toggle_layer)
 *   urns      canonical `<kind>:<id>` (city-api grammar) that RESOLVE: an
 *             entity of the fused city registry, or a segment / junction id
 *             of the canvas's own network (godsEye segments / junctions)
 *   points    lon/lat inside NETWORK_BBOX (ops/endpoints.ts) — the city's
 *             road-network extent, the same box the brain's canvas_fly_to
 *             accepts (CANVAS_BBOX); the narrower CITY_BBOX boot view would
 *             drop a sighting on SP Ring Road (West) at lon 72.48
 *   text      a note of 140 characters or fewer (plates are masked at the
 *             canvas's text sinks like every other string)
 *   sensors   raw | flir-whot | flir-bhot | flir-iron;  layers: the canvas's
 *             own layer ids (+ 'photoreal-3d' when that layer is registered)
 *
 * Everything else is DROPPED and COUNTED by reason — the canvas never
 * guesses what a malformed proposal meant. `world === null` validates the
 * structure only (the agent console has no canvas network): URNs are then
 * resolved by the canvas bus when City Command mounts.
 *
 * Pure module (no Svelte runes, no DOM, no fetch): tests/canvas-bus.unit.mjs
 * bundles it with esbuild.
 */

import { NETWORK_BBOX } from '$lib/ops/endpoints';
import { parseUrn } from './city-api';

export const CANVAS_VERBS = [
	'canvas_fly_to',
	'canvas_track_entity',
	'canvas_highlight',
	'canvas_annotate',
	'canvas_set_sensor',
	'canvas_toggle_layer',
] as const;

export type CanvasVerb = (typeof CANVAS_VERBS)[number];

export const SENSOR_MODES = ['raw', 'flir-whot', 'flir-bhot', 'flir-iron'] as const;
export const ANNOTATION_SEVERITIES = ['info', 'warn', 'critical'] as const;
export const ANNOTATION_MAX_CHARS = 140;
export const HIGHLIGHT_MAX = 20;
/** Directives older than this are expired (the bus drops them). */
export const DIRECTIVE_TTL_MS = 10 * 60 * 1000;
const RANGE_M = { min: 100, max: 60_000 } as const;

export type DropReason = 'malformed' | 'verb' | 'urn' | 'bbox' | 'text' | 'args' | 'expired';

/** What the canvas can resolve (built from the registry + the canvas network). */
export interface DirectiveWorld {
	hasUrn(urn: string): boolean;
	hasSegment(id: number): boolean;
	hasJunction(id: number): boolean;
	layerIds: readonly string[];
}

export type DirectiveArgs =
	| { verb: 'canvas_fly_to'; urn: string | null; lon: number | null; lat: number | null; rangeM: number | null }
	| { verb: 'canvas_track_entity'; urn: string }
	| { verb: 'canvas_highlight'; urns: string[] }
	| { verb: 'canvas_annotate'; urn: string; text: string; severity: (typeof ANNOTATION_SEVERITIES)[number] }
	| { verb: 'canvas_set_sensor'; mode: (typeof SENSOR_MODES)[number] }
	| { verb: 'canvas_toggle_layer'; layerId: string; on: boolean };

export interface CanvasDirective {
	id: string;
	verb: CanvasVerb;
	args: DirectiveArgs;
	/** The brain's label (masked again at the canvas's text sink). */
	label: string;
	sessionId: string | null;
	tier: string | null;
	confidence: number | null;
	toolCallId: string | null;
	/** Epoch ms the deck received it (TTL clock). */
	receivedAt: number;
}

export type Validation = { ok: true; directive: CanvasDirective } | { ok: false; reason: DropReason; detail: string };

function rec(v: unknown): Record<string, unknown> {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length > 0 ? v : null;
}

function finite(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

const ID_RE = /^[A-Za-z0-9_.:-]{1,64}$/;

function drop(reason: DropReason, detail: string): Validation {
	return { ok: false, reason, detail };
}

/** True when `lon,lat` lies inside the city's road-network extent (NETWORK_BBOX). */
export function inNetworkBbox(lon: number, lat: number): boolean {
	return (
		lon >= NETWORK_BBOX.minLon && lon <= NETWORK_BBOX.maxLon && lat >= NETWORK_BBOX.minLat && lat <= NETWORK_BBOX.maxLat
	);
}

/** A URN's grammar + (with a world) whether the canvas can resolve it; the canonical URN or null. */
export function resolveUrn(value: unknown, world: DirectiveWorld | null): { urn: string | null; resolved: boolean } {
	const parsed = typeof value === 'string' ? parseUrn(value.trim()) : null;
	if (!parsed) return { urn: null, resolved: false };
	const urn = `${parsed.kind}:${parsed.id}`;
	if (!world) return { urn, resolved: true };
	if (world.hasUrn(urn)) return { urn, resolved: true };
	if (parsed.kind === 'segment') return { urn, resolved: world.hasSegment(Number(parsed.id)) };
	if (parsed.kind === 'junction') return { urn, resolved: world.hasJunction(Number(parsed.id)) };
	return { urn, resolved: false };
}

/**
 * Validate one streamed directive (the client's ChatDirective shape, or the
 * raw wire data). `world` null = structural check only.
 */
export function validateDirective(raw: unknown, world: DirectiveWorld | null, now = Date.now()): Validation {
	const d = rec(raw);
	const id = str(d.id);
	const verb = str(d.verb);
	if (!id || !ID_RE.test(id) || !verb) return drop('malformed', 'directive without a usable id or verb');
	if (!(CANVAS_VERBS as readonly string[]).includes(verb)) return drop('verb', `verb ${verb.slice(0, 40)} is not a canvas verb`);
	const a = rec(d.args);
	const prov = rec(d.provenance);
	const base = {
		id,
		verb: verb as CanvasVerb,
		label: (str(d.label) ?? verb).slice(0, 200),
		sessionId: str(prov.sessionId) ?? str(prov.session_id),
		tier: str(prov.tier),
		confidence: finite(prov.confidence),
		toolCallId: str(prov.toolCallId) ?? str(prov.tool_call_id),
		receivedAt: now,
	};
	const needUrn = (value: unknown): { urn: string } | Validation => {
		const r = resolveUrn(value, world);
		if (!r.urn) return drop('urn', `not a canonical URN: ${String(value).slice(0, 60)}`);
		if (!r.resolved) return drop('urn', `${r.urn} is not on the canvas`);
		return { urn: r.urn };
	};
	switch (verb as CanvasVerb) {
		case 'canvas_fly_to': {
			const rangeRaw = finite(a.range_m ?? a.rangeM);
			const rangeM = rangeRaw == null ? null : Math.min(RANGE_M.max, Math.max(RANGE_M.min, rangeRaw));
			if (a.urn != null && a.urn !== '') {
				const u = needUrn(a.urn);
				if ('ok' in u) return u;
				return { ok: true, directive: { ...base, args: { verb: 'canvas_fly_to', urn: u.urn, lon: null, lat: null, rangeM } } };
			}
			const lon = finite(a.lon);
			const lat = finite(a.lat);
			if (lon == null || lat == null) return drop('args', 'fly_to needs an entity urn or lon + lat');
			if (!inNetworkBbox(lon, lat)) return drop('bbox', `(${lon}, ${lat}) is outside the city road network`);
			return { ok: true, directive: { ...base, args: { verb: 'canvas_fly_to', urn: null, lon, lat, rangeM } } };
		}
		case 'canvas_track_entity': {
			const u = needUrn(a.urn);
			if ('ok' in u) return u;
			return { ok: true, directive: { ...base, args: { verb: 'canvas_track_entity', urn: u.urn } } };
		}
		case 'canvas_highlight': {
			const list = Array.isArray(a.urns) ? a.urns : [];
			if (list.length < 1 || list.length > HIGHLIGHT_MAX) return drop('args', `highlight needs 1..${HIGHLIGHT_MAX} URNs`);
			const urns: string[] = [];
			for (const value of list) {
				const u = needUrn(value);
				if ('ok' in u) return u;
				if (!urns.includes(u.urn)) urns.push(u.urn);
			}
			return { ok: true, directive: { ...base, args: { verb: 'canvas_highlight', urns } } };
		}
		case 'canvas_annotate': {
			const text = typeof a.text === 'string' ? a.text.replace(/\s+/g, ' ').trim() : '';
			if (!text) return drop('text', 'a note needs text');
			if ([...text].length > ANNOTATION_MAX_CHARS) return drop('text', `note over ${ANNOTATION_MAX_CHARS} characters`);
			const severity = (str(a.severity) ?? 'info') as (typeof ANNOTATION_SEVERITIES)[number];
			if (!(ANNOTATION_SEVERITIES as readonly string[]).includes(severity)) return drop('args', `severity ${severity} unknown`);
			const u = needUrn(a.urn);
			if ('ok' in u) return u;
			return { ok: true, directive: { ...base, args: { verb: 'canvas_annotate', urn: u.urn, text, severity } } };
		}
		case 'canvas_set_sensor': {
			const mode = str(a.mode);
			if (!mode || !(SENSOR_MODES as readonly string[]).includes(mode)) return drop('args', `sensor ${String(mode)} unknown`);
			return { ok: true, directive: { ...base, args: { verb: 'canvas_set_sensor', mode: mode as (typeof SENSOR_MODES)[number] } } };
		}
		case 'canvas_toggle_layer': {
			const layerId = str(a.layer_id ?? a.layerId);
			const on = a.on;
			if (!layerId || typeof on !== 'boolean') return drop('args', 'toggle_layer needs layer_id and on');
			if (world && !world.layerIds.includes(layerId)) return drop('args', `layer ${layerId} is not on this canvas`);
			return { ok: true, directive: { ...base, args: { verb: 'canvas_toggle_layer', layerId, on } } };
		}
	}
	return drop('verb', 'unreachable');
}

/** Every URN a directive points at (the bus re-resolves them on the canvas). */
export function directiveUrns(d: CanvasDirective): string[] {
	const a = d.args;
	switch (a.verb) {
		case 'canvas_fly_to':
			return a.urn ? [a.urn] : [];
		case 'canvas_track_entity':
		case 'canvas_annotate':
			return [a.urn];
		case 'canvas_highlight':
			return [...a.urns];
		default:
			return [];
	}
}

/** Drop counters by reason (the bus publishes them; tests assert them). */
export type DropTally = Record<DropReason, number> & { total: number };

export function emptyTally(): DropTally {
	return { malformed: 0, verb: 0, urn: 0, bbox: 0, text: 0, args: 0, expired: 0, total: 0 };
}

export function countDrop(tally: DropTally, reason: DropReason): DropTally {
	return { ...tally, [reason]: tally[reason] + 1, total: tally.total + 1 };
}
