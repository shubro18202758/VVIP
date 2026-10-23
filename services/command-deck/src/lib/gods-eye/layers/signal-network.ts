/**
 * signal-network layer — city junction signal state.
 *
 * Feed: the canvas store's signals — the city registry's junction entities
 * that carry a program (GET /api/v1/city/entities), or GET /api/v1/signals/
 * state as the fallback; both served off corridor.signal_programs +
 * corridor.junctions. Each junction reads its ACTIVE PHASE ({index,
 * movements, green_sec, lost, movement_states}) at the oracle's phase clock
 * (WP-03 — the same clock the e-challan engine evaluates):
 *
 *   · a movement showing AMBER (movement_states) → an AMBER RING;
 *   · otherwise in lost time (all-red between greens) → a RED RING;
 *   · while a green runs → a GREEN ARC whose sweep is that phase's share of
 *     the cycle (green_sec / cycle_sec), drawn from 12 o'clock clockwise;
 *   · no program green at all → a dim dot only.
 *
 * Rings and arcs are pixel-sized billboards (legible at city zoom, constant
 * on screen) rendered once per (shape, 5 % bucket, colour) into a cached
 * data-URL image — identical URLs share one texture-atlas slot, so redraws
 * never grow the atlas. Colours are the band-ladder tokens read at runtime
 * (theme.ts); no hex literals. Source-labelled (osm_assumed vs calibrated)
 * in the pick description. 404 → "not deployed" chip.
 *
 * DRAPED (layers/draping.ts): the junction marker is clamped to the terrain
 * — street level, under the photoreal 3D tiles too — and never hidden by a
 * building.
 * Cesium renders a clamped point AS the entity's billboard, so one entity
 * cannot carry both: a junction with a ring / arc carries its state dot
 * INSIDE the glyph image (one billboard); a junction without a green phase
 * is the dim clamped point alone.
 *
 * Pick ids `sig:<junctionId>` → URN `junction:<id>`.
 */

import { feedError, feedHint, godsEye } from '../store.svelte';
import type { SignalStateRecord } from '../data';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor, cssVar } from '../theme';
import { clampedBillboard, clampedDot, drapeAudit, ground } from './draping';

export const SIGNAL_NETWORK_PICK_PREFIX = 'sig:';

/** On-screen size of the ring / arc glyph (CSS px). */
const GLYPH_PX = 26;
/** Arc sweep quantisation: 5 % of the cycle per bucket. */
const ARC_BUCKETS = 20;

/** The state dot inside a glyph (CSS px radius, + its canvas-ink rim). */
const DOT_R = 3.5;

/** data-URL cache: one image per (shape, bucket, stroke, track, dot). */
const glyphCache = new Map<string, string>();

/**
 * Render (once) the ring / arc glyph with the junction's state dot at its
 * centre. `fraction` is the arc's share of the full circle; the ring
 * ignores it. Null outside a DOM (never in the canvas).
 */
function glyph(
	shape: 'ring' | 'arc',
	fraction: number,
	stroke: string,
	track: string | null,
	dot: { fill: string; rim: string },
): string | null {
	if (typeof document === 'undefined') return null;
	const bucket = Math.max(1, Math.min(ARC_BUCKETS, Math.round(fraction * ARC_BUCKETS)));
	const key = `${shape}|${shape === 'arc' ? bucket : 0}|${stroke}|${track ?? ''}|${dot.fill}|${dot.rim}`;
	const hit = glyphCache.get(key);
	if (hit) return hit;
	const scale = 2; // crisp on HiDPI walls
	const canvas = document.createElement('canvas');
	canvas.width = canvas.height = GLYPH_PX * scale;
	const g = canvas.getContext('2d');
	if (!g) return null;
	g.scale(scale, scale);
	const c = GLYPH_PX / 2;
	const r = GLYPH_PX / 2 - 3;
	g.lineWidth = 3;
	g.lineCap = 'round';
	if (shape === 'arc') {
		if (track) {
			g.strokeStyle = track;
			g.beginPath();
			g.arc(c, c, r, 0, Math.PI * 2);
			g.stroke();
		}
		g.strokeStyle = stroke;
		g.beginPath();
		g.arc(c, c, r, -Math.PI / 2, -Math.PI / 2 + (Math.PI * 2 * bucket) / ARC_BUCKETS);
		g.stroke();
	} else {
		g.strokeStyle = stroke;
		g.beginPath();
		g.arc(c, c, r, 0, Math.PI * 2);
		g.stroke();
	}
	g.fillStyle = dot.fill;
	g.strokeStyle = dot.rim;
	g.lineWidth = 1;
	g.beginPath();
	g.arc(c, c, DOT_R, 0, Math.PI * 2);
	g.fill();
	g.stroke();
	const url = canvas.toDataURL('image/png');
	glyphCache.set(key, url);
	return url;
}

/** A movement shows amber at the phase clock (WP-03 movement_states). */
export function isAmber(s: SignalStateRecord): boolean {
	return Object.values(s.movementStates).includes('amber');
}

function movementLine(s: SignalStateRecord): string {
	const entries = Object.entries(s.movementStates);
	return entries.length ? ` · ${entries.map(([m, st]) => `${m} ${st}`).join(', ')}` : '';
}

/** Share of the cycle the running phase holds (0..1), null when unknown. */
export function greenShare(s: SignalStateRecord): number | null {
	if (s.greenSec == null || s.cycleSec == null || s.cycleSec <= 0) return null;
	return Math.max(0, Math.min(1, s.greenSec / s.cycleSec));
}

function createSignalNetworkLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';
	let lostCount = 0;
	let amberCount = 0;
	let greenCount = 0;

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const signals = godsEye.signals;
		const sig = JSON.stringify([
			signals.map((s) => [
				s.junctionId,
				s.phaseIndex,
				s.phaseLost,
				s.greenSec,
				s.cycleSec,
				s.activePhase,
				Object.values(s.movementStates).join(''),
			]),
			godsEye.signalsFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		lostCount = 0;
		amberCount = 0;
		greenCount = 0;

		const red = cssVar('--v-band-red') ?? 'red';
		const amberInk = cssVar('--v-band-amber') ?? 'orange';
		const green = cssVar('--v-band-green') ?? 'lime';
		const track = cssVar('--v-line-2');
		const rim = cssVar('--v-canvas') ?? 'black';

		for (const s of signals) {
			const share = greenShare(s);
			const amber = isAmber(s);
			const lost = !amber && s.phaseLost === true;
			if (amber) amberCount++;
			else if (lost) lostCount++;
			const noGreen = s.phaseIndex == null && !s.phaseMovements.length;
			if (!noGreen && !amber && !lost) greenCount++;
			const ink = amber ? amberInk : lost ? red : green;
			const image = noGreen
				? null
				: amber || lost
					? glyph('ring', 1, ink, null, { fill: ink, rim })
					: share != null
						? glyph('arc', share, green, track, { fill: green, rim })
						: null;
			const entity = viewer.entities.add({
				id: `${SIGNAL_NETWORK_PICK_PREFIX}${s.junctionId}`,
				position: ground(cesium, s.lon, s.lat),
				// the glyph carries the state dot; without one, the clamped point
				...(image
					? {
							billboard: clampedBillboard(cesium, {
								image,
								width: GLYPH_PX,
								height: GLYPH_PX,
							}),
						}
					: {
							billboard: clampedDot(cesium, {
								pixelSize: noGreen ? 5 : 7,
								color: noGreen ? cssColor(cesium, '--v-ink-4', cesium.Color.GRAY) : cssColor(cesium, '--v-band-green', cesium.Color.LIME),
								outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
								outlineWidth: 1,
							}),
						}),
				description: `${s.name ? `${s.name} · ` : ''}junction ${s.junctionId} · ${
					amber ? 'AMBER' : lost ? 'ALL-RED (lost time)' : noGreen ? 'no green phase' : 'GREEN'
				}${movementLine(s)} · phase ${s.activePhase ?? '—'}${
					s.remainingSec != null ? ` (${s.remainingSec}s left)` : ''
				} · green ${s.greenSec ?? '—'}s of ${s.cycleSec ?? '—'}s cycle${
					share != null ? ` (${Math.round(share * 100)}%)` : ''
				} · offset ${s.offsetSec ?? '—'}s · green_ratio ${
					s.greenRatio != null ? s.greenRatio.toFixed(2) : '—'
				} · source ${s.source ?? '—'}`,
			});
			entityIds.push(entity.id);
		}
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
			timer = setInterval(draw, 2000);
		},
		deactivate() {
			active = false;
			if (timer) clearInterval(timer);
			timer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			lastSignature = '';
		},
		refresh() {
			lastSignature = '';
			void godsEye.pollSignals();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(SIGNAL_NETWORK_PICK_PREFIX);
		},
		urnOf(pickId) {
			const id = pickId.slice(SIGNAL_NETWORK_PICK_PREFIX.length);
			return /^\d+$/.test(id) ? `junction:${id}` : null;
		},
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.signalsFeed.status === 'empty' && godsEye.signalsFeed.updatedAt === 0,
				hint:
					godsEye.signalsFeed.status === 'not-deployed'
						? '/api/v1/signals/state 404 — signalling stream pending'
						: (feedHint(godsEye.signalsFeed) ??
							`${godsEye.signals.length} signal programs · ${greenCount} green · ${amberCount} amber · ${lostCount} all-red (program clock)`),
				error: feedError(godsEye.signalsFeed),
				detail: {
					programs: godsEye.signals.length,
					green: greenCount,
					amber: amberCount,
					red: lostCount,
					...(ctx && entityIds.length ? drapeAudit(ctx.viewer, entityIds) : {}),
				},
				via: godsEye.via('junction'),
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const signalNetworkLayer: GodsEyeLayer = {
	id: 'signals',
	name: 'Signal network state',
	label: 'SIGNALS',
	requiresKeyId: null,
	pickPrefix: SIGNAL_NETWORK_PICK_PREFIX,
	defaultOn: true,
	create: createSignalNetworkLayer,
};
