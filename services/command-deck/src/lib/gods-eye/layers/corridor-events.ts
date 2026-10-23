/**
 * corridor-events layer — route spines + zone polygons + phase-colored
 * event markers, bound to the LIVE ops store.
 *
 * PRIMARY geometry = route spines: ops.events[].routes (labelled spines,
 * rank order) resolved against the live segment map (GET /api/v1/spatial/
 * segments through the canvas store). Zone polygons are the OPTIONAL
 * additive enhancement — this layer re-reads GET /api/v1/events/active raw
 * for `zone_geojson` (ST_AsGeoJSON of zone_geom, the C1 additive field) on
 * the board cadence and counts zones the moment the field appears; until
 * then spines alone render (documented degradation, not an error).
 *
 * Spine labels carry the plan (contract C-I via the brain's city link):
 *   'primary' of a protected movement → the RECOMMENDED corridor: a glowing
 *              spine with a RECOMMENDED tag at its midpoint — also when the
 *              server's current primary is a re-route ('reroute-N' /
 *              'plan_b', as label or C-O variant), whose tag says so;
 *   'alt-N'   → an alternate the planner ranked lower: a dashed spine;
 *   anything else (procession belts, pursuit anchors) → a solid spine.
 * A segment shared by several spines is drawn once, in its best-ranked
 * spine's style. Spines a re-route SUPERSEDED (C-O `route_history`,
 * tolerated absent) draw thin, dim and dashed, labelled 'superseded' on
 * their pick card — the old corridor is never shown as the live one.
 *
 * The RECOMMENDED tag says 'ESCORT LIVE' only while the escort really runs:
 * the movement's convoy entity (C-B GPS mirror) is in the fused registry,
 * or the brain's escort status (C-J, the shared follower) is running.
 * Once the brain reports the end it reads 'ESCORT COMPLETED' / 'ESCORT
 * STOPPED' / 'ESCORT FAILED' (its own word), otherwise 'ACTIVE' — the
 * event's lifecycle alone never claims a live escort.
 *
 * Lifecycle state colors (from the ops store's event rows):
 *   planned → data accent · active → act orange · completed/cancelled → dim.
 * Fixture rows (offline-fixture) render at reduced opacity with a PREVIEW
 * label, emulator-injected rows carry an EMULATED label, so operators can
 * tell preview and emulation from live.
 *
 * DRAPED (layers/draping.ts): spines, superseded spines and zone fills lie
 * ON the ground — terrain and the photoreal 3D tiles — as ground polylines /
 * classification fills; markers and labels are clamped to the ground and
 * never hidden by a building.
 *
 * Legibility rules:
 *   - live rows draw ABOVE fixtures: live spines take a higher ground draw
 *     order (DRAPE_Z) than fixture spines — the RECOMMENDED corridor highest
 *     of all — so a PREVIEW fixture never paints over a live corridor.
 *     Markers and labels skip the depth test, and Cesium pools billboards,
 *     so between two markers that share pixels the draw order is not
 *     guaranteed (fixtures are added first, which usually but not always
 *     puts live ones on top); fixture labels fade out first (below);
 *   - a live event's name stands on the translucent canvas plate
 *     (layers/labels.ts), so its own spine never runs through bare text;
 *   - labels declutter by camera distance (DistanceDisplayCondition +
 *     NearFarScalar; fixture labels fade out first), and markers that share
 *     an anchor fan out on a small ground ring with stacked label offsets
 *     instead of overprinting each other;
 *   - the HUD chip counts EVENTS on the canvas, not entities (one event is
 *     many spine segments + a marker + an optional zone).
 *
 * C6: event titles can name a plate (a pursuit's title does); every label
 * and pick description masks it unless a named duty officer is on console.
 *
 * Pick ids: `evt:spine-<eventId>-<segmentId>`, `evt:mark-<eventId>`,
 * `evt:zone-<eventId>` (+ its draped rim `evt:rim-<eventId>`),
 * `evt:rec-<eventId>` — eventIdFromPick() maps any of them back to the
 * event (URN `event:<id>`) so the canvas can select it.
 */

import { ops } from '$lib/ops/store.svelte';
import type { CorridorEvent } from '$lib/ops/types';
import { escortFollower } from '$lib/ops/components/template/escort.svelte';
import { cityRegistry } from '../entities/registry.svelte';
import type { SegmentGeometry } from '../data';
import { ORACLE_V1, POLL_MS } from '$lib/ops/endpoints';
import { feedError, godsEye } from '../store.svelte';
import { displayText } from '../plates';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssVar, withAlpha } from '../theme';
import {
	DRAPE_Z,
	clampedDot,
	clampedLabel,
	drapeAudit,
	drapedPolygon,
	drapedPolyline,
	ground,
	groundPath,
} from './draping';
import { labelPlate } from './labels';

export const CORRIDOR_EVENTS_PICK_PREFIX = 'evt:';

/**
 * The corridor event behind a picked entity id, or null when the id is not
 * one of this layer's (`evt:spine-<id>-<segment>`, `evt:mark-<id>`,
 * `evt:zone-<id>`, `evt:rim-<id>`, `evt:rec-<id>`). Segment ids are
 * integers, so the trailing `-<digits>` of a spine id is unambiguous even
 * for hyphenated event ids (UUIDs).
 */
export function eventIdFromPick(pickId: string): string | null {
	if (!pickId.startsWith(CORRIDOR_EVENTS_PICK_PREFIX)) return null;
	const rest = pickId.slice(CORRIDOR_EVENTS_PICK_PREFIX.length);
	const spine = /^(?:spine|old)-(.+)-(\d+)$/.exec(rest);
	if (spine) return spine[1];
	const other = /^(?:mark|zone|rim|rec)-(.+)$/.exec(rest);
	return other ? other[1] : null;
}

// Cesium enums as verified constants (the ambient global has no enum typing).
// VerticalOrigin: CENTER 0 · BOTTOM 1 · BASELINE 2 · TOP -1 — a BOTTOM-anchored
// label hangs ABOVE its point (negative y offset), a TOP-anchored one below.
const LABEL_FILL_AND_OUTLINE = 2;
const VERTICAL_ORIGIN_BOTTOM = 1;
const VERTICAL_ORIGIN_TOP = -1;

/** Ground draw order of a spine: live above fixtures, the RECOMMENDED corridor on top. */
function spineZ(style: SpineStyle, live: boolean): number {
	if (!live) return DRAPE_Z.fixtureSpine;
	return style === 'recommended' ? DRAPE_Z.recommended : style === 'alternate' ? DRAPE_Z.alternate : DRAPE_Z.spine;
}

/** Label declutter distances (m): fixture labels drop out first. */
const LABEL_FAR = { live: 40_000, fixture: 12_000 } as const;

/** Co-anchored markers fan out on rings of this radius step (m), 5 per ring. */
const FAN_STEP_M = 45;
const FAN_PER_RING = 5;
const LABEL_STACK_PX = 15;
/** Judge-view titles stay short (the pick card carries the whole title). */
const LABEL_MAX_CHARS = 32;

type SpineStyle = 'recommended' | 'alternate' | 'plain';

/**
 * Lifecycle colours from the design tokens at runtime (theme.cssVar reads
 * the live token value; CSS colour-keyword fallbacks are last resort, never
 * a second palette): planned is the data-accent outline, active is the
 * HUMAN-ACT orange (a live corridor IS a 'your move' surface),
 * completed/cancelled sit dim ink. Returns a CSS colour string — callers
 * wrap with cesium.Color.fromCssColorString.
 */
function stateColor(state: string): string {
	switch (state) {
		case 'active':
			return cssVar('--v-act') ?? 'orange';
		case 'completed':
		case 'cancelled':
			return cssVar('--v-ink-4') ?? 'dimgray';
		default:
			return cssVar('--v-data') ?? 'cyan';
	}
}

/** Zone fill opacity by state: an active closure reads strongest. */
const ZONE_ALPHA_BY_STATE: Record<string, number> = {
	planned: 0.16,
	active: 0.24,
	completed: 0.1,
	cancelled: 0.08,
};

/** GeoJSON Polygon ring → lon/lat pairs (malformed vertices dropped). */
function ringToCoords(ring: unknown): Array<[number, number]> {
	const out: Array<[number, number]> = [];
	if (!Array.isArray(ring)) return out;
	for (const c of ring) {
		if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') out.push([c[0], c[1]]);
	}
	return out;
}

/** Emulator-injected rows (additive `emulated` flag on the event row). */
function isEmulated(ev: CorridorEvent): boolean {
	return 'emulated' in ev && ev.emulated === true;
}

/** Source label printed on every pick card (the C6 labelling rail). */
function sourceTag(ev: CorridorEvent): string {
	if (ev.source === 'offline-fixture') return 'PREVIEW fixture (offline, not live)';
	if (isEmulated(ev)) return 'EMULATED (event-emulator run)';
	return ev.source === 'live-movements-api' ? 'LIVE (movements feed)' : 'LIVE (events API)';
}

function labelText(ev: CorridorEvent, officer: boolean): string {
	const shown = displayText(ev.name, officer);
	const name = shown.length > LABEL_MAX_CHARS ? `${shown.slice(0, LABEL_MAX_CHARS - 1)}…` : shown;
	if (ev.source === 'offline-fixture') return `${name} ·PREVIEW`;
	return isEmulated(ev) ? `${name} ·EMULATED` : name;
}

/** Multi-line pick-card description for an event (spine or marker). */
function eventDescription(ev: CorridorEvent, detail: string | null, officer: boolean): string {
	const lines = [
		`${displayText(ev.name, officer)} — ${ev.eventKind.replace(/_/g, ' ')} · ${ev.state} · band ${ev.band ?? '—'}${
			ev.currentPhase ? ` · phase ${ev.currentPhase}` : ''
		}`,
		`source ${sourceTag(ev)}`,
	];
	if (detail) lines.push(detail);
	if (ev.description) lines.push(displayText(ev.description, officer));
	return lines.join('\n');
}

/** Offset (deg) for the k-th marker sharing an anchor: a small ground ring. */
function fanOffset(k: number, lat: number): [number, number] {
	if (k <= 0) return [0, 0];
	const ring = Math.ceil(k / FAN_PER_RING);
	const angle = (((k - 1) % FAN_PER_RING) / FAN_PER_RING) * 2 * Math.PI;
	const r = FAN_STEP_M * ring;
	const dLat = (r * Math.cos(angle)) / 111_320;
	const dLon = (r * Math.sin(angle)) / (111_320 * Math.cos((lat * Math.PI) / 180));
	return [dLon, dLat];
}

/** A re-route variant as the server names it ('reroute-2', 'plan_b'); null otherwise. */
export function rerouteVariantOf(label: string | null | undefined): string | null {
	const l = (label ?? '').trim().toLowerCase();
	return /^(reroute[-_ ]?\d+|plan[-_ ]?b)$/.test(l) ? l : null;
}

/**
 * The plan role a labelled spine plays (brain city link: 'primary',
 * 'alt-N'; C-O: the server's current primary may be a re-route — label or
 * variant 'reroute-N' / 'plan_b'; superseded spines never reach `routes`).
 */
export function spineStyleOf(ev: CorridorEvent, label: string, variant?: string | null): SpineStyle {
	const l = label.trim().toLowerCase();
	if (/^alt(\b|[-_ ]?\d)/.test(l)) return 'alternate';
	if (ev.eventKind === 'protected_movement' && (l === 'primary' || rerouteVariantOf(l) || rerouteVariantOf(variant))) {
		return 'recommended';
	}
	return 'plain';
}

/** The current primary's re-route variant, when the server switched the corridor (C-O). */
function currentVariant(ev: CorridorEvent): string | null {
	for (const r of ev.routes ?? []) {
		const v = rerouteVariantOf(r.variant) ?? rerouteVariantOf(r.label);
		if (v && spineStyleOf(ev, r.label, r.variant) === 'recommended') return v;
	}
	return null;
}

/**
 * The escort behind a protected movement, as far as the stack can tell:
 * 'live' while its convoy entity (C-B) is in the registry or the brain's
 * escort status (C-J) runs; 'stopped' once the brain says it stopped;
 * null when nothing reports an escort.
 */
export function escortStateOf(ev: CorridorEvent): 'live' | 'completed' | 'stopped' | 'failed' | null {
	const mid = ev.movementId;
	if (!mid) return null;
	const followed = escortFollower.movementId === mid ? escortFollower.status?.status : null;
	if (followed === 'running' || followed === 'waiting_for_gps') return 'live';
	if (followed === 'completed' || followed === 'stopped' || followed === 'failed') return followed;
	return cityRegistry.get(`convoy:${mid}`) ? 'live' : null;
}

/** The RECOMMENDED tag's text: never 'ESCORT LIVE' unless an escort reports running. */
export function recommendedTag(ev: CorridorEvent): string {
	const variant = currentVariant(ev);
	const head = variant ? `RECOMMENDED · ${variant.replace(/[-_]/g, ' ').toUpperCase()}` : 'RECOMMENDED';
	if (ev.state !== 'active') return head;
	const escort = escortStateOf(ev);
	return `${head} · ${escort === 'live' ? 'ESCORT LIVE' : escort ? `ESCORT ${escort.toUpperCase()}` : 'ACTIVE'}`;
}

/** segmentId → {style, label}: each segment once, in its best-ranked spine's style. */
function segmentStyles(ev: CorridorEvent): Map<number, { style: SpineStyle; label: string }> {
	const out = new Map<number, { style: SpineStyle; label: string }>();
	const spines = ev.routes?.length ? ev.routes : [{ label: 'spine', segmentIds: ev.routeSegmentIds, variant: null }];
	for (const spine of spines) {
		const style = spineStyleOf(ev, spine.label, spine.variant);
		for (const id of spine.segmentIds) if (!out.has(id)) out.set(id, { style, label: spine.label });
	}
	return out;
}

function createCorridorEventsLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	let zoneTimer: ReturnType<typeof setInterval> | null = null;
	let zonesInFlight = false;
	const entityIds: string[] = [];
	let hint = 'loading';
	let lastSignature = '';
	let zoneCount = 0;
	let eventsDrawn = 0;
	let detail: Record<string, number> = {};

	/**
	 * Optional additive field (C1): absent until the events API carries
	 * zone_geojson (ST_AsGeoJSON of zone_geom). A 404/down just means no
	 * zones — never fatal. Rows are parsed defensively; a malformed ring is
	 * skipped, and an event without geometry keeps its spine.
	 */
	let zoneRows: Array<{ eventId: string; state: string; ring: unknown; color: string }> = [];

	async function pollZones() {
		if (!ctx || !active || zonesInFlight) return;
		if (typeof document !== 'undefined' && document.hidden) return;
		zonesInFlight = true;
		try {
			const resp = (await ctx.fetchJson(`${ORACLE_V1}/events/active`, AbortSignal.timeout(6000))) as {
				events?: Array<Record<string, unknown>>;
			};
			const rows = Array.isArray(resp?.events) ? (resp?.events ?? []) : [];
			const next: typeof zoneRows = [];
			for (const r of rows) {
				const id = r.event_id ?? r.id;
				const z = r.zone_geojson;
				if (typeof id !== 'string' && typeof id !== 'number') continue;
				if (z == null || typeof z !== 'object' || (z as { type?: string }).type !== 'Polygon') continue;
				const rings = (z as { coordinates?: unknown }).coordinates;
				if (!Array.isArray(rings) || !Array.isArray(rings[0])) continue;
				const rawState = typeof r.state === 'string' ? r.state : typeof r.status === 'string' ? r.status : 'planned';
				const state = rawState === 'planning' || rawState === 'approved' ? 'planned' : rawState;
				next.push({
					eventId: String(id),
					state,
					ring: rings[0],
					color: stateColor(state),
				});
			}
			zoneRows = next;
		} catch {
			zoneRows = [];
		} finally {
			zonesInFlight = false;
		}
	}

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const segmentMap = godsEye.segmentById;
		const officer = ops.officerOn;
		// One row per event id (a live row wins over a fixture with the same
		// id): every entity id below derives from the event id, and Cesium
		// throws on a duplicate entity id.
		const byEvent = new Map<string, CorridorEvent>();
		for (const e of ops.events) {
			const seen = byEvent.get(e.eventId);
			if (!seen || (seen.source === 'offline-fixture' && e.source !== 'offline-fixture')) byEvent.set(e.eventId, e);
		}
		// Fixtures first, live last: later adds win depth ties, and live
		// geometry also rides higher — PREVIEW never paints over live.
		const events = [...byEvent.values()].sort(
			(a, b) => Number(a.source !== 'offline-fixture') - Number(b.source !== 'offline-fixture'),
		);
		const sig = JSON.stringify([
			events.map((e) => [
				e.eventId,
				e.state,
				e.routeSegmentIds.join('.'),
				(e.routes ?? []).map((r) => `${r.label}:${r.segmentIds.join('.')}`),
				e.name,
				e.band,
				e.source,
				e.currentPhase,
				isEmulated(e),
				e.description?.length ?? 0,
				(e.routeHistory ?? []).map((r) => `${r.label}:${r.variant ?? ''}:${r.segmentIds.join('.')}`),
				e.eventKind === 'protected_movement' ? recommendedTag(e) : null,
			]),
			godsEye.segments.map((s) => s.segmentId),
			zoneRows.map((z) => [z.eventId, z.state, Array.isArray(z.ring) ? z.ring.length : 0]),
			officer,
		]);
		if (sig === lastSignature) return;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		try {
			drawEvents(cesium, viewer, events, segmentMap, officer);
			lastSignature = sig; // only a complete draw is cached
		} catch (err) {
			// The ids drawn so far are tracked, so the next tick clears the
			// partial layer and redraws it instead of leaving it half-drawn.
			lastSignature = '';
			hint = `draw failed — ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	function spineMaterial(cesium: CesiumGlobal, style: SpineStyle, color: string, live: boolean): unknown {
		if (!live) return cesium.Color.fromCssColorString(withAlpha(color, 0.45));
		if (style === 'recommended') {
			return new cesium.PolylineGlowMaterialProperty({
				color: cesium.Color.fromCssColorString(color),
				glowPower: 0.22,
				taperPower: 1,
			});
		}
		if (style === 'alternate') {
			return new cesium.PolylineDashMaterialProperty({
				color: cesium.Color.fromCssColorString(withAlpha(color, 0.85)),
				dashLength: 14,
			});
		}
		return cesium.Color.fromCssColorString(color);
	}

	function spineWidth(style: SpineStyle, state: string, live: boolean): number {
		if (!live) return state === 'active' ? 3 : 2;
		if (style === 'recommended') return state === 'active' ? 13 : 11;
		if (style === 'alternate') return 3;
		return state === 'active' ? 5 : 3.5;
	}

	function drawEvents(
		cesium: CesiumGlobal,
		viewer: CesiumViewer,
		events: CorridorEvent[],
		segmentMap: Map<number, SegmentGeometry>,
		officer: boolean,
	) {
		let spineCount = 0;
		let recommended = 0;
		let alternates = 0;
		const drawn = new Set<string>();
		// Fan-out slots per shared anchor, allocated LIVE-FIRST: a live event
		// keeps the true anchor and fixtures step aside onto the ring.
		const fanSlot = new Map<string, number>();
		const anchorUse = new Map<string, number>();
		const anchored: Array<{ id: string; lon: number }> = [];
		for (const ev of [...events].reverse()) {
			const anchor = anchorOf(ev, segmentMap);
			if (!anchor) continue;
			const key = `${anchor[0].toFixed(4)},${anchor[1].toFixed(4)}`;
			const k = anchorUse.get(key) ?? 0;
			anchorUse.set(key, k + 1);
			fanSlot.set(ev.eventId, k);
			anchored.push({ id: ev.eventId, lon: anchor[0] });
		}
		// Neighbours west→east alternate one label line lower, so two markers a
		// few hundred px apart never print their titles over each other.
		const lowered = new Set(
			anchored
				.sort((a, b) => a.lon - b.lon)
				.filter((_, i) => i % 2 === 1)
				.map((a) => a.id),
		);
		const canvasInk = cesium.Color.fromCssColorString(cssVar('--v-canvas') ?? 'black');
		const labelInk = cesium.Color.fromCssColorString(cssVar('--v-ink-1') ?? 'white');
		const actInk = cesium.Color.fromCssColorString(cssVar('--v-act-text') ?? 'orange');

		for (const ev of events) {
			const color = stateColor(ev.state);
			const live = ev.source !== 'offline-fixture';
			const tier = live ? 'live' : 'fixture';

			// Route spines: one polyline per segment (per-segment entities keep
			// pick ids stable + simple), styled by the spine's plan role.
			const styles = segmentStyles(ev);
			const segCount = styles.size;
			const primaryIds: number[] = [];
			for (const [segId, { style, label }] of styles) {
				const seg = segmentMap.get(segId);
				if (!seg || seg.coordinates.length < 2) continue;
				const positions = groundPath(cesium, seg.coordinates);
				spineCount++;
				if (style === 'recommended') {
					recommended++;
					primaryIds.push(segId);
				} else if (style === 'alternate') {
					alternates++;
				}
				drawn.add(ev.eventId);
				const role =
					style === 'recommended'
						? 'RECOMMENDED primary route'
						: style === 'alternate'
							? `alternate route ${label} (ranked lower by the planner)`
							: `route spine '${label}'`;
				const entity = viewer.entities.add({
					id: `${CORRIDOR_EVENTS_PICK_PREFIX}spine-${ev.eventId}-${segId}`,
					polyline: drapedPolyline(cesium, {
						positions,
						width: spineWidth(style, ev.state, live),
						material: spineMaterial(cesium, style, color, live),
						z: spineZ(style, live),
					}),
					description: eventDescription(
						ev,
						`${role} · segment ${segId}${seg.roadName ? ` (${seg.roadName})` : ''} · ${segCount} segments on the event`,
						officer,
					),
				});
				entityIds.push(entity.id);
			}

			// Superseded spines (C-O route_history): thin, dim, dashed — the
			// corridor the escort no longer takes, said so on the pick card.
			const current = new Set(styles.keys());
			const oldSeen = new Set<number>();
			for (const old of live ? (ev.routeHistory ?? []) : []) {
				for (const segId of old.segmentIds) {
					if (current.has(segId) || oldSeen.has(segId)) continue;
					const seg = segmentMap.get(segId);
					if (!seg || seg.coordinates.length < 2) continue;
					oldSeen.add(segId);
					const entity = viewer.entities.add({
						id: `${CORRIDOR_EVENTS_PICK_PREFIX}old-${ev.eventId}-${segId}`,
						polyline: drapedPolyline(cesium, {
							positions: groundPath(cesium, seg.coordinates),
							width: 2,
							material: new cesium.PolylineDashMaterialProperty({
								color: cesium.Color.fromCssColorString(withAlpha(cssVar('--v-ink-3') ?? 'gray', 0.55)),
								dashLength: 10,
							}),
							z: DRAPE_Z.superseded,
						}),
						description: eventDescription(
							ev,
							`superseded route '${old.variant ?? old.label}'${old.supersededAt ? ` · replaced ${old.supersededAt}` : ''} · segment ${segId}${
								seg.roadName ? ` (${seg.roadName})` : ''
							} · not the live corridor`,
							officer,
						),
					});
					entityIds.push(entity.id);
				}
			}

			// The RECOMMENDED tag rides the middle of the primary spine.
			if (live && primaryIds.length) {
				const mid = segmentMap.get(primaryIds[Math.floor(primaryIds.length / 2)]);
				const c = mid?.coordinates[Math.floor((mid?.coordinates.length ?? 1) / 2)];
				if (c) {
					const entity = viewer.entities.add({
						id: `${CORRIDOR_EVENTS_PICK_PREFIX}rec-${ev.eventId}`,
						position: ground(cesium, c[0], c[1]),
						label: clampedLabel(cesium, {
							text: recommendedTag(ev),
							font: '600 13px monospace',
							fillColor: actInk,
							outlineColor: canvasInk,
							outlineWidth: 3,
							style: LABEL_FILL_AND_OUTLINE,
							showBackground: true,
							backgroundColor: cesium.Color.fromCssColorString(withAlpha(cssVar('--v-canvas') ?? 'black', 0.72)),
							backgroundPadding: new cesium.Cartesian2(5, 3),
							// ABOVE the marker (which sits on the same midpoint): the plate
							// never covers the dot or takes its clicks
							verticalOrigin: VERTICAL_ORIGIN_BOTTOM,
							pixelOffset: new cesium.Cartesian2(0, -12),
							distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, LABEL_FAR.live),
						}),
						description: eventDescription(ev, `RECOMMENDED primary route · ${primaryIds.length} segments`, officer),
					});
					entityIds.push(entity.id);
				}
			}

			// Event marker at the spine midpoint; co-anchored markers fan out.
			const anchor = anchorOf(ev, segmentMap);
			if (anchor) {
				const k = fanSlot.get(ev.eventId) ?? 0;
				const [dLon, dLat] = fanOffset(k, anchor[1]);
				drawn.add(ev.eventId);
				const entity = viewer.entities.add({
					id: `${CORRIDOR_EVENTS_PICK_PREFIX}mark-${ev.eventId}`,
					position: ground(cesium, anchor[0] + dLon, anchor[1] + dLat),
					billboard: clampedDot(cesium, {
						pixelSize: live ? (ev.state === 'active' ? 13 : 10) : ev.state === 'active' ? 9 : 7,
						color: cesium.Color.fromCssColorString(live ? color : withAlpha(color, 0.6)),
						outlineColor: canvasInk,
						outlineWidth: 2,
					}),
					label: clampedLabel(cesium, {
						text: labelText(ev, officer),
						font: live ? '13px monospace' : '12px monospace',
						fillColor: labelInk,
						outlineColor: canvasInk,
						outlineWidth: 2,
						style: LABEL_FILL_AND_OUTLINE,
						// a live event's name stands on the canvas plate: legible over the
						// photoreal city with its own spine running through it (PREVIEW
						// fixtures stay plate-less and fade first)
						...(live ? labelPlate(cesium, 5, 3) : {}),
						// below the marker; co-anchored markers stack, neighbours stagger
						verticalOrigin: VERTICAL_ORIGIN_TOP,
						pixelOffset: new cesium.Cartesian2(0, 14 + (k + (lowered.has(ev.eventId) ? 1 : 0)) * LABEL_STACK_PX),
						distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, LABEL_FAR[tier]),
						scaleByDistance: new cesium.NearFarScalar(1_500, 1.0, LABEL_FAR[tier], live ? 0.8 : 0.7),
						...(live ? {} : { translucencyByDistance: new cesium.NearFarScalar(2_000, 1.0, LABEL_FAR.fixture, 0.45) }),
					}),
					description: eventDescription(
						ev,
						`marker · ${segCount} spine segments${ev.windowStart ? ` · window ${ev.windowStart}` : ''}`,
						officer,
					),
				});
				entityIds.push(entity.id);
			}
		}

		// Zone polygons — the C1 additive enhancement, DRAPED: a state-tinted
		// fill on the ground (terrain and the 3D tiles) under a draped rim in
		// the state colour (a ground polygon has no outline of its own). Only
		// rows whose event ALSO sits on the board render, so fixture-only
		// zones never masquerade as live closures.
		const byId = new Map(events.map((e) => [e.eventId, e]));
		const zoned = new Set<string>();
		zoneCount = 0;
		for (const z of zoneRows) {
			const ev = byId.get(z.eventId);
			if (!ev || zoned.has(z.eventId)) continue;
			zoned.add(z.eventId);
			const coords = ringToCoords(z.ring);
			if (coords.length < 3) continue;
			zoneCount++;
			drawn.add(z.eventId);
			const alpha = ZONE_ALPHA_BY_STATE[z.state] ?? 0.14;
			const description = eventDescription(ev, `corridor zone · state ${z.state} · draped on the ground`, officer);
			const fill = viewer.entities.add({
				id: `${CORRIDOR_EVENTS_PICK_PREFIX}zone-${z.eventId}`,
				polygon: drapedPolygon(cesium, {
					positions: groundPath(cesium, coords),
					material: cesium.Color.fromCssColorString(withAlpha(z.color, alpha)),
					z: DRAPE_Z.zone,
				}),
				description,
			});
			entityIds.push(fill.id);
			const closed = coords[0][0] === coords[coords.length - 1][0] && coords[0][1] === coords[coords.length - 1][1];
			const rim = viewer.entities.add({
				id: `${CORRIDOR_EVENTS_PICK_PREFIX}rim-${z.eventId}`,
				polyline: drapedPolyline(cesium, {
					positions: groundPath(cesium, closed ? coords : [...coords, coords[0]]),
					width: 2.5,
					material: cesium.Color.fromCssColorString(z.color),
					z: DRAPE_Z.zone,
				}),
				description,
			});
			entityIds.push(rim.id);
		}

		eventsDrawn = drawn.size;
		const drawnRows = [...drawn].map((id) => byId.get(id)).filter((e): e is CorridorEvent => !!e);
		const liveDrawn = drawnRows.filter((e) => e.source !== 'offline-fixture').length;
		detail = {
			live: liveDrawn,
			preview: eventsDrawn - liveDrawn,
			emulated: drawnRows.filter((e) => e.source !== 'offline-fixture' && isEmulated(e)).length,
			active: drawnRows.filter((e) => e.source !== 'offline-fixture' && e.state === 'active').length,
			spines: spineCount,
			recommended,
			alternates,
		};
		const offCanvas = events.length - eventsDrawn;
		hint =
			godsEye.segmentsFeed.status === 'ok'
				? `${eventsDrawn} events on canvas (${liveDrawn} live) · ${spineCount} spine segments${
						recommended ? ` · RECOMMENDED corridor ${recommended} segs, ${alternates} alternate segs` : ''
					} · ${zoneCount} zones${offCanvas > 0 ? ` · ${offCanvas} without geometry` : ''}`
				: `segments ${godsEye.segmentsFeed.status} — ${events.length} events (no spines)`;
	}

	return {
		async status() {
			return { configured: true };
		},
		init(layerCtx) {
			ctx = layerCtx;
		},
		activate() {
			active = true;
			draw();
			void pollZones();
			timer = setInterval(() => draw(), 2000);
			zoneTimer = setInterval(() => void pollZones(), POLL_MS.board);
		},
		deactivate() {
			active = false;
			if (timer) clearInterval(timer);
			if (zoneTimer) clearInterval(zoneTimer);
			timer = zoneTimer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			lastSignature = '';
			eventsDrawn = 0;
			detail = {};
		},
		refresh() {
			lastSignature = '';
			void godsEye.pollSegments(true);
			void pollZones();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(CORRIDOR_EVENTS_PICK_PREFIX);
		},
		urnOf(pickId) {
			const id = eventIdFromPick(pickId);
			return id ? `event:${id}` : null;
		},
		coverageStats() {
			return {
				count: eventsDrawn,
				loading: godsEye.segmentsFeed.status === 'empty' && ops.events.length === 0,
				hint,
				error: feedError(godsEye.segmentsFeed),
				detail: ctx && eventsDrawn ? { ...detail, ...drapeAudit(ctx.viewer, entityIds) } : detail,
				via: 'ops-board',
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

function anchorOf(
	ev: CorridorEvent,
	segmentMap: Map<number, { coordinates: Array<[number, number]> }>,
): [number, number] | null {
	const segIds = ev.routeSegmentIds;
	const mid = segIds[Math.floor(segIds.length / 2)] ?? segIds[0];
	const seg = mid != null ? segmentMap.get(mid) : undefined;
	if (seg && seg.coordinates.length) {
		const c = seg.coordinates[Math.floor(seg.coordinates.length / 2)];
		return [c[0], c[1]];
	}
	return null;
}

/**
 * Spine centroid (mean of every resolved segment vertex) + bounding radius
 * in metres — the camera target for subject-follow, focus requests and the
 * guided tour. Null when no spine segment resolves (no geometry to fly to).
 */
export function eventCentroid(
	ev: CorridorEvent,
	segmentMap: Map<number, { coordinates: Array<[number, number]> }>,
): { lon: number; lat: number; radiusM: number } | null {
	const coords = [...new Set(ev.routeSegmentIds)]
		.map((id) => segmentMap.get(id))
		.filter((s): s is NonNullable<typeof s> => Boolean(s))
		.flatMap((s) => s.coordinates);
	if (!coords.length) return null;
	let lon = 0;
	let lat = 0;
	for (const [x, y] of coords) {
		lon += x;
		lat += y;
	}
	lon /= coords.length;
	lat /= coords.length;
	const mPerDegLat = 111_320;
	const mPerDegLon = 111_320 * Math.cos((lat * Math.PI) / 180);
	let radiusM = 0;
	for (const [x, y] of coords) {
		radiusM = Math.max(radiusM, Math.hypot((x - lon) * mPerDegLon, (y - lat) * mPerDegLat));
	}
	return { lon, lat, radiusM };
}

export const corridorEventsLayer: GodsEyeLayer = {
	id: 'events',
	name: 'Corridor events (spines + zones)',
	label: 'EVENTS',
	requiresKeyId: null,
	pickPrefix: CORRIDOR_EVENTS_PICK_PREFIX,
	defaultOn: true,
	create: createCorridorEventsLayer,
};
