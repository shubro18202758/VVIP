/**
 * anomalies layer — data-quality anomaly pulses on the road network (anm:).
 *
 * Feed: the city registry's anomaly entities — the latest Tier-1 anomaly per
 * segment in the last 15 minutes (traffic.anomaly_log, written by the
 * oracle's ingest from signal-ingress's corridor.traffic.anomaly docs with
 * their anomaly_flags, contract C-H). Each is a slow pulse at the segment
 * labelled with what was flagged — in the operator's words and language
 * ('Speed jump ×6', 'Stale data'; layers/labels.ts anomalyWord — the wire
 * id speed_jump / stale_data / phantom_reading … stays in the pick card) —
 * and how often it fired in the window, on the overlay labels' translucent
 * canvas plate. These flag the FEED, not the road: an anomalous row is
 * excluded from the congestion summary, and the pulse says so.
 *
 * Severity colours (tokens, never the band ladder — anomalies are not city
 * posture): high → act orange, medium → tool accent, low → dim ink.
 *
 * Provenance: the flags ride the signal-ingress feed, which is SYNTHETIC in
 * this deployment. The layer owns that answer on EVERY view (the /ops wall
 * starts no corridor readout of its own): while active it holds the shared
 * corridor-summary feed (reference-counted, one poll per page), and each
 * pulse reads, in order: EMULATED (an emulated entity), SIMULATED FEED
 * (the entity's own data_quality is 'synthetic', or the summary stands on
 * synthetic rows — the chip then carries the SIM tag), the live feed only
 * when the summary positively says so, else 'provenance unverified'. A
 * pulse is never labelled with a bare feed name, nor LIVE by default.
 * prefers-reduced-motion: a static ring of the same size.
 *
 * DRAPED (layers/draping.ts): the dot and its label are clamped to the
 * terrain (street level, under the photoreal 3D tiles too) and no building
 * hides them.
 * The pulse is a ring GLYPH (a billboard drawn once per colour into a cached
 * data URL, like the signal rings) that BREATHES — its opacity rises and
 * falls on the pulse clock — on an entity of its own: a ground ellipse
 * cannot carry an outline, re-building a ground primitive every frame for a
 * growing radius would cost the wall its frame rate, and Cesium renders a
 * clamped point AS the entity's billboard (one entity cannot carry both).
 * Size stays constant, so the canvas's hover / selection emphasis (which
 * scales a billboard and restores the number) never freezes the breath.
 *
 * Pick ids `anm:<anomalyId>` (the dot) and `anm:pulse-<anomalyId>` (its
 * ring) → URN `anomaly:<id>`.
 */

import { corridor } from '$lib/ops/components/template/corridor.svelte';
import type { CityEntity } from '../city-api';
import { cityRegistry } from '../entities/registry.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor, cssVar } from '../theme';
import { istStamp } from '../hud/format';
import { ops } from '$lib/ops/store.svelte';
import { clampedBillboard, clampedDot, clampedLabel, drapeAudit, ground } from './draping';
import { anomalyLabel, labelPlate } from './labels';

export const ANOMALIES_PICK_PREFIX = 'anm:';

const PULSE_MS = 1800;
/** On-screen size (CSS px) of the pulse ring glyph. */
const RING_PX = 34;
/** The breath: the ring's opacity swings between these over one pulse. */
const PULSE_ALPHA = { low: 0.2, high: 1 } as const;
/** A still ring (reduced motion) holds this opacity. */
const STILL_ALPHA = 0.85;

/** The anomaly id behind a pick id (`anm:<id>` or `anm:pulse-<id>`), null otherwise. */
export function anomalyIdFromPick(pickId: string): string | null {
	if (!pickId.startsWith(ANOMALIES_PICK_PREFIX)) return null;
	const id = pickId.slice(ANOMALIES_PICK_PREFIX.length).replace(/^pulse-/, '');
	return /^\d+$/.test(id) ? id : null;
}

/** data-URL cache: one ring image per stroke colour. */
const ringCache = new Map<string, string>();

/** Render (once per colour) the pulse ring glyph; null outside a DOM. */
function ringGlyph(stroke: string): string | null {
	if (typeof document === 'undefined') return null;
	const hit = ringCache.get(stroke);
	if (hit) return hit;
	const scale = 2; // crisp on HiDPI walls
	const canvas = document.createElement('canvas');
	canvas.width = canvas.height = RING_PX * scale;
	const g = canvas.getContext('2d');
	if (!g) return null;
	g.scale(scale, scale);
	g.lineWidth = 2.5;
	g.strokeStyle = stroke;
	g.beginPath();
	g.arc(RING_PX / 2, RING_PX / 2, RING_PX / 2 - 2, 0, Math.PI * 2);
	g.stroke();
	const url = canvas.toDataURL('image/png');
	ringCache.set(stroke, url);
	return url;
}

function reducedMotion(): boolean {
	if (typeof matchMedia !== 'function') return false;
	return matchMedia('(prefers-reduced-motion: reduce)').matches;
}

type Provenance = 'emulated' | 'simulated' | 'live' | 'unverified';

function provenanceOf(a: CityEntity): Provenance {
	if (a.source === 'emulated') return 'emulated';
	if (a.props.data_quality === 'synthetic') return 'simulated';
	return corridor.provenance;
}

const PROVENANCE_TEXT: Record<Provenance, string> = {
	emulated: 'EMULATED',
	simulated: 'SIMULATED FEED (synthetic signal-ingress rows)',
	live: 'live signal-ingress rows (per the corridor summary)',
	unverified: 'provenance unverified (the corridor summary has not reported it yet)',
};

function severityToken(severity: string | null): string {
	if (severity === 'high' || severity === 'critical') return '--v-act';
	if (severity === 'medium') return '--v-tool';
	return '--v-ink-3';
}

/** The label's ink: the severity hue where it reads as text, else the canvas's secondary ink. */
function severityTextToken(severity: string | null): string {
	if (severity === 'high' || severity === 'critical') return '--v-act-text';
	if (severity === 'medium') return '--v-tool';
	return '--v-ink-2';
}

function createAnomaliesLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';
	/** Drawn pulses per provenance (the chip's SIM tag + hint). */
	let byProvenance: Record<Provenance, number> = { emulated: 0, simulated: 0, live: 0, unverified: 0 };
	/** This instance holds the shared corridor-summary feed (released exactly once). */
	let holdingCorridor = false;

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const rows = cityRegistry.byKind('anomaly').filter((a) => a.lon != null && a.lat != null);
		const lang = ops.lang;
		const sig = JSON.stringify([
			rows.map((a) => [a.id, a.state, a.lon, a.lat, a.props.count_15m, a.props.anomaly_type, provenanceOf(a)]),
			lang,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		byProvenance = { emulated: 0, simulated: 0, live: 0, unverified: 0 };
		const still = reducedMotion();
		const canvasInk = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);

		for (const a of rows) {
			const color = cssColor(cesium, severityToken(a.state), cesium.Color.ORANGE);
			const labelInk = cssColor(cesium, severityTextToken(a.state), cesium.Color.WHITE);
			const ring = ringGlyph(cssVar(severityToken(a.state)) ?? 'orange');
			const type = typeof a.props.anomaly_type === 'string' ? a.props.anomaly_type : 'anomaly';
			const flags = Array.isArray(a.props.anomaly_flags) ? (a.props.anomaly_flags as unknown[]).filter((f) => typeof f === 'string') : [];
			const count = typeof a.props.count_15m === 'number' ? a.props.count_15m : null;
			const provenance = provenanceOf(a);
			byProvenance[provenance]++;
			const description = `feed anomaly · ${type} · severity ${a.state ?? '—'}${
				flags.length ? ` · flags ${flags.join(', ')}` : ''
			} · ${count ?? '—'} in 15 min on ${a.props.road_name ?? `segment ${a.props.segment_id ?? '—'}`} · ${
				a.t != null ? `${istStamp(a.t)} IST` : '—'
			} · flagged rows are excluded from the congestion summary · source ${PROVENANCE_TEXT[provenance]}`;
			if (ring) {
				// the breath: a sine on the pulse clock, as the billboard's tint
				// (white × alpha — the glyph already carries the severity colour)
				const white = cesium.Color.WHITE as { withAlpha?: (a: number) => unknown } | undefined;
				const breath = white?.withAlpha
					? still
						? white.withAlpha(STILL_ALPHA)
						: new cesium.CallbackProperty(() => {
								const p = (performance.now() % PULSE_MS) / PULSE_MS;
								const a01 = 0.5 - 0.5 * Math.cos(p * 2 * Math.PI);
								return white.withAlpha!(PULSE_ALPHA.low + (PULSE_ALPHA.high - PULSE_ALPHA.low) * a01);
							}, false)
					: undefined;
				const pulse = viewer.entities.add({
					id: `${ANOMALIES_PICK_PREFIX}pulse-${a.id}`,
					position: ground(cesium, a.lon as number, a.lat as number),
					billboard: clampedBillboard(cesium, {
						image: ring,
						width: RING_PX,
						height: RING_PX,
						...(breath ? { color: breath } : {}),
						scaleByDistance: new cesium.NearFarScalar(2_000, 1.0, 16_000, 0.55),
					}),
					description,
				});
				entityIds.push(pulse.id);
			}
			const entity = viewer.entities.add({
				id: `${ANOMALIES_PICK_PREFIX}${a.id}`,
				position: ground(cesium, a.lon as number, a.lat as number),
				billboard: clampedDot(cesium, {
					pixelSize: 6,
					color,
					outlineColor: canvasInk,
					outlineWidth: 1,
				}),
				label: clampedLabel(cesium, {
					// the operator's word for the flag (the wire id stays in the pick card)
					text: anomalyLabel(type, count, lang),
					font: '12px monospace',
					// the severity's TEXT ink on the canvas plate: legible on the
					// photoreal city and over the draped flow lines through it
					fillColor: labelInk,
					outlineColor: canvasInk,
					outlineWidth: 2,
					style: 2,
					...labelPlate(cesium),
					verticalOrigin: -1,
					// below the pulse ring (RING_PX across), never inside it
					pixelOffset: new cesium.Cartesian2(0, RING_PX / 2 + 3),
					distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, 6000),
				}),
				description,
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
			if (!holdingCorridor) {
				holdingCorridor = true;
				corridor.start();
			}
			draw();
			timer = setInterval(draw, 2000);
		},
		deactivate() {
			active = false;
			if (holdingCorridor) {
				holdingCorridor = false;
				corridor.stop();
			}
			if (timer) clearInterval(timer);
			timer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			byProvenance = { emulated: 0, simulated: 0, live: 0, unverified: 0 };
			lastSignature = '';
		},
		refresh() {
			lastSignature = '';
			void cityRegistry.refresh();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(ANOMALIES_PICK_PREFIX);
		},
		urnOf(pickId) {
			const id = anomalyIdFromPick(pickId);
			return id ? `anomaly:${id}` : null;
		},
		coverageStats() {
			const served = cityRegistry.served('anomaly');
			const down = cityRegistry.status !== 'idle' && !served;
			// pulses, not entities: each pulse is a dot (+ its ring)
			const n = byProvenance.emulated + byProvenance.simulated + byProvenance.live + byProvenance.unverified;
			const tag =
				byProvenance.simulated > 0
					? byProvenance.simulated === n
						? ' · SIMULATED FEED'
						: ` · ${byProvenance.simulated} SIMULATED FEED`
					: byProvenance.unverified > 0
						? ' · provenance unverified'
						: '';
			return {
				count: n,
				loading: cityRegistry.status === 'idle',
				hint: down
					? 'city fusion API unavailable — anomaly pulses not shown'
					: `${n} segments with feed anomalies in the last 15 min (Tier-1 flags, C-H)${tag}${
							byProvenance.emulated ? ` · ${byProvenance.emulated} EMULATED` : ''
						}`,
				error: down ? (cityRegistry.note ?? 'anomaly source not ok') : null,
				detail: {
					anomalies: n,
					simulated: byProvenance.simulated,
					emulated: byProvenance.emulated,
					unverified: byProvenance.unverified,
					...(ctx && n ? drapeAudit(ctx.viewer, entityIds) : {}),
				},
				via: 'city-api',
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const anomaliesLayer: GodsEyeLayer = {
	id: 'anomalies',
	name: 'Feed anomalies (Tier-1 flags)',
	label: 'ANOMALY',
	requiresKeyId: null,
	pickPrefix: ANOMALIES_PICK_PREFIX,
	defaultOn: true,
	create: createAnomaliesLayer,
};
