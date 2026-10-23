/**
 * traffic-flow layer — the restored VVIP template's signature congestion
 * network, drawn ON the God's-eye canvas.
 *
 * Geometry comes from the shared canvas segment feed (GET /spatial/segments,
 * cached in the gods-eye store); congestion comes from the oracle's Valkey
 * hot cache (POST /api/v1/traffic/live with the visible segment ids).
 *
 * HONEST DEGRADATION (the system's rule, kept): a segment with NO current
 * observation — or one older than STALE_OBS_SEC — renders as a thin neutral
 * hairline ('NO DATA' in its pick card), never a guessed colour. When the
 * hot cache is empty (quiet window, emulator idle) the whole network renders
 * neutral and the layer hint says so; the ramp colours appear the moment
 * observations flow (signal-ingress / emulator runs).
 *
 * PROVENANCE: every observation carries its data_quality. The integrated
 * signal-ingress feed is SYNTHETIC (data_quality 'synthetic') — such rows
 * are labelled SIMULATED FEED on the chip hint and every pick card, never
 * LIVE ('live' only ever describes system freshness, not data provenance).
 *
 * EMULATED PLANE (contract C-P): an event-emulator showcase run's traffic
 * no longer lands in `observations` — the oracle serves it beside the
 * ingest plane as `emulated_observations` (origin 'emulated', synthetic).
 * While such a row is fresh it colours its segment (the showcase's
 * congestion is what the run is for), and it says so: EMULATED · SIMULATED
 * FEED on its pick card (the ingest plane's own reading beside it) and an
 * 'n EMULATED' count in the hint. The road name falls back to the row's
 * road_name when the segment feed has none.
 *
 * GEOMETRY BASIS (contract C-K): a segment drawn on real OSM geometry —
 * its routing geometry itself ('osm', the real-road network) or its
 * OSM-traced display geometry ('osm_display') — is a SOLID line; only a
 * SCHEMATIC one (the seed's junction-to-junction chord — not the road's
 * true shape) is drawn DASHED and says so on its pick card, and the hint
 * counts both ('n OSM-traced · m schematic (dashed)') so a chord never
 * passes for a road.
 *
 * DRAPED (layers/draping.ts): every segment is a ground polyline on terrain
 * AND the photoreal 3D tiles (the keyless-era 2 m lines were buried under
 * real Ahmedabad), at the bottom of the ground draw order — spines, traces
 * and the pursuit paint over it. Widths read at 1920×1080 from the 1.5–3 km
 * judge view: a coloured segment is a 6 px line — a 3.5 px ramp-coloured
 * core inside a dark 1.25 px casing on each side (legible on aerial
 * imagery and on the dark raster alike); a
 * schematic chord's dashes alternate with the same dark casing colour; a
 * NO DATA hairline is 2 px of neutral ink.
 *
 * Network hygiene: the 8 s POST is skipped while the tab is hidden (the
 * shared poll gate, $lib/ops/client), so a background /ops tab sends none.
 *
 * Ramp (design tokens at runtime — zero hex literals, theme.ts bridge):
 *   congestion ≤0.2 band-green (free) · ≤0.4 flow-light · ≤0.6 band-amber
 *   · ≤0.8 act (heavy) · >0.8 band-red (severe) — the template ladder,
 *   expressed entirely in existing token slots.
 *
 * Pick ids `seg:<segmentId>` → URN `segment:<id>`.
 */

import { godsEye } from '../store.svelte';
import { ORACLE_V1 } from '$lib/ops/endpoints';
import { createPollGate } from '$lib/ops/client';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssVar, withAlpha } from '../theme';
import { DRAPE_Z, drapeAudit, drapedPolyline, groundPath } from './draping';

export const TRAFFIC_FLOW_PICK_PREFIX = 'seg:';

/** An observation older than this no longer colours its segment. */
export const STALE_OBS_SEC = 300;

/** Draped widths (px) — legible at 1920×1080 from the 1.5–3 km judge view. */
export const FLOW_WIDTH = { coloured: 6, casing: 1.25, noData: 2 } as const;

interface LiveObservation {
	segment_id: number;
	speed_kmh: number;
	congestion_idx: number;
	timestamp_ms: number;
	source?: string;
	/** real | synthetic | anomalous (oracle TrafficObservation.data_quality). */
	data_quality?: string;
	/** C-P: the segment's road name from the oracle's segment map (additive). */
	road_name?: string | null;
	/** C-P: 'live' (the ingest plane) | 'emulated' (an emulator showcase run). */
	origin?: string;
}

/** The rows of one /traffic/live plane, keyed by segment (malformed rows dropped). */
function planeOf(rows: unknown): Map<number, LiveObservation> {
	const out = new Map<number, LiveObservation>();
	for (const o of Array.isArray(rows) ? (rows as LiveObservation[]) : []) {
		if (typeof o?.segment_id === 'number') out.set(o.segment_id, o);
	}
	return out;
}

/** The 5-stop congestion ramp from live tokens (mirrors flowColor() in
 * tokens.ts; canvas code reaches tokens through theme.ts, not design/). */
function rampColor(idx: number): string {
	const stop =
		idx <= 0.2
			? '--v-band-green'
			: idx <= 0.4
				? '--v-flow-light'
				: idx <= 0.6
					? '--v-band-amber'
					: idx <= 0.8
						? '--v-act'
						: '--v-band-red';
	return cssVar(stop) ?? 'cornflowerblue';
}

function createTrafficFlowLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let drawTimer: ReturnType<typeof setInterval> | null = null;
	const gate = createPollGate();
	const entityIds: string[] = [];
	let obsById = new Map<number, LiveObservation>();
	/** The emulated plane (C-P emulated_observations): showcase-run rows. */
	let emuById = new Map<number, LiveObservation>();
	let obsFeed: 'ok' | 'empty' | 'down' = 'empty';
	let hint = 'loading';
	let lastSignature = '';
	let detail: Record<string, number> = {};

	/** Fixed-endpoint POST to the oracle hot cache (ids come from the
	 * shared segment store — never operator input). */
	async function pollObservations() {
		if (!ctx || !active) return;
		const ids = godsEye.segments.map((s) => s.segmentId);
		if (!ids.length) return;
		try {
			const res = await fetch(`${ORACLE_V1}/traffic/live`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ segment_ids: ids }),
				signal: AbortSignal.timeout(6000),
			});
			if (!res.ok) throw new Error(`${res.status}`);
			const body = (await res.json()) as { observations?: unknown; emulated_observations?: unknown };
			obsById = planeOf(body.observations);
			// an oracle before C-P has no emulated plane: nothing to draw from it
			emuById = planeOf(body.emulated_observations);
			obsFeed = obsById.size || emuById.size ? 'ok' : 'empty';
		} catch {
			// Leave last-good observations on the canvas: each one greys out by
			// itself once it is older than STALE_OBS_SEC; the hint degrades.
			obsFeed = obsById.size || emuById.size ? 'ok' : 'down';
		}
	}

	/** Gated: never overlaps itself, never runs while the tab is hidden. */
	const poll = () => void gate('flow', pollObservations);

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const segments = godsEye.segments;
		const now = Date.now();
		const fresh = (o: LiveObservation | undefined) =>
			!!o && Number.isFinite(o.timestamp_ms) && (now - o.timestamp_ms) / 1000 <= STALE_OBS_SEC;
		const planeSig = (plane: Map<number, LiveObservation>) =>
			[...plane.entries()].map(([id, o]) => [
				id,
				Math.round(o.congestion_idx * 20) / 20,
				Math.round(o.speed_kmh),
				o.data_quality ?? '',
				fresh(o),
			]);
		const sig = JSON.stringify([
			segments.map((s) => `${s.segmentId}${s.geometryBasis === 'schematic' ? 's' : ''}`),
			planeSig(obsById),
			planeSig(emuById),
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		const neutral = withAlpha(cssVar('--v-ink-4') ?? 'slategray', 0.75);
		// the dark casing / dash gap: the canvas ink, slightly translucent
		const casing = cesium.Color.fromCssColorString(withAlpha(cssVar('--v-canvas') ?? 'black', 0.85));
		let coloured = 0;
		let simulated = 0;
		let emulatedCount = 0;
		let stale = 0;
		let osmCount = 0;
		let schematicCount = 0;

		for (const seg of segments) {
			if (seg.coordinates.length < 2) continue;
			// ground positions: the draped line takes its height from the surface
			const positions = groundPath(cesium, seg.coordinates);

			const liveRaw = obsById.get(seg.segmentId);
			const emuRaw = emuById.get(seg.segmentId);
			const live = fresh(liveRaw) ? liveRaw : undefined;
			const emu = fresh(emuRaw) ? emuRaw : undefined;
			// A fresh showcase row colours its segment (badged EMULATED);
			// otherwise the ingest plane's row does.
			const obs = emu ?? live;
			const emulated = !!emu;
			const raw = obs ?? liveRaw ?? emuRaw;
			if (raw && !obs) stale++;
			const synthetic = obs?.data_quality === 'synthetic' || emulated;
			const schematic = seg.geometryBasis === 'schematic';
			if (schematic) schematicCount++;
			else osmCount++;
			const width = obs ? FLOW_WIDTH.coloured : FLOW_WIDTH.noData;
			const colour = cesium.Color.fromCssColorString(obs ? rampColor(obs.congestion_idx) : neutral);
			// A schematic chord is not the road's true shape: dashed, never solid
			// (its gaps carry the dark casing so the rhythm reads on aerial
			// imagery); a coloured real road wears the casing as an outline.
			const material = schematic
				? new cesium.PolylineDashMaterialProperty({ color: colour, gapColor: obs ? casing : undefined, dashLength: 14 })
				: obs && cesium.PolylineOutlineMaterialProperty
					? new cesium.PolylineOutlineMaterialProperty({ color: colour, outlineColor: casing, outlineWidth: FLOW_WIDTH.casing })
					: colour;
			if (obs) coloured++;
			if (synthetic) simulated++;
			if (emulated) emulatedCount++;

			const name = seg.roadName ?? raw?.road_name ?? liveRaw?.road_name ?? emuRaw?.road_name ?? `segment ${seg.segmentId}`;
			const road = `${name}${schematic ? ' · SCHEMATIC geometry (straight chord between junctions, not the road’s true shape)' : ' · OSM-traced'}`;
			const ageSec = raw ? Math.max(0, Math.round((now - raw.timestamp_ms) / 1000)) : null;
			const planeProvenance = (o: LiveObservation | undefined) =>
				o?.data_quality === 'synthetic'
					? 'SIMULATED FEED (synthetic signal-ingress rows)'
					: o?.data_quality === 'anomalous'
						? 'flagged anomalous'
						: `source ${o?.source ?? '?'}`;
			const provenance = emulated
				? 'EMULATED · SIMULATED FEED (event-emulator showcase run)'
				: planeProvenance(obs);
			// the ingest plane's own reading stays on the card under a showcase row
			const besides =
				emulated && live
					? ` · ingest plane: ${live.speed_kmh.toFixed(1)} km/h, ${(live.congestion_idx * 100).toFixed(0)}% (${planeProvenance(live)})`
					: '';
			const description = obs
				? `${road} · ${obs.speed_kmh.toFixed(1)} km/h · congestion ${(obs.congestion_idx * 100).toFixed(0)}% · ${provenance} · ${ageSec}s ago${besides}`
				: raw
					? `${road} · NO DATA — last observation ${ageSec}s ago (older than ${STALE_OBS_SEC}s, neutral)`
					: `${road} · NO DATA — no observation in the hot cache (neutral)`;

			const entity = viewer.entities.add({
				id: `${TRAFFIC_FLOW_PICK_PREFIX}${seg.segmentId}`,
				polyline: drapedPolyline(cesium, { positions, width, material, z: DRAPE_Z.flow }),
				description,
			});
			entityIds.push(entity.id);
		}

		detail = {
			coloured,
			segments: entityIds.length,
			simulated,
			emulated: emulatedCount,
			stale,
			osm: osmCount,
			schematic: schematicCount,
		};
		const tag =
			(simulated > 0 && simulated === coloured ? ' · SIMULATED FEED' : simulated > 0 ? ` · ${simulated} SIMULATED` : '') +
			(emulatedCount > 0 ? ` · ${emulatedCount} EMULATED` : '');
		const basis = schematicCount ? ` · ${osmCount} OSM-traced · ${schematicCount} schematic (dashed)` : osmCount ? ` · ${osmCount} OSM-traced` : '';
		hint =
			obsFeed === 'down'
				? `traffic/live unreachable — network neutral (${entityIds.length} segments)${basis}`
				: obsFeed === 'empty'
					? `no observations — network neutral (${entityIds.length} segments)${basis}`
					: `${coloured}/${entityIds.length} segments on the congestion ramp${tag}${stale ? ` · ${stale} stale (NO DATA)` : ''}${basis}`;
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
			poll();
			draw();
			pollTimer = setInterval(poll, 8000);
			drawTimer = setInterval(() => draw(), 2000);
		},
		deactivate() {
			active = false;
			if (pollTimer) clearInterval(pollTimer);
			if (drawTimer) clearInterval(drawTimer);
			pollTimer = drawTimer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			lastSignature = '';
			detail = {};
		},
		refresh() {
			lastSignature = '';
			void godsEye.pollSegments(true);
			poll();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(TRAFFIC_FLOW_PICK_PREFIX);
		},
		urnOf(pickId) {
			const id = pickId.slice(TRAFFIC_FLOW_PICK_PREFIX.length);
			return /^\d+$/.test(id) ? `segment:${id}` : null;
		},
		coverageStats() {
			return {
				// The chip counts COLOURED segments (a hairline is an absence of data).
				count: detail.coloured ?? 0,
				loading: godsEye.segmentsFeed.status === 'empty',
				hint,
				error: obsFeed === 'down' ? 'traffic/live feed down' : null,
				detail: ctx ? { ...detail, ...drapeAudit(ctx.viewer, entityIds) } : detail,
				via: 'traffic/live',
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const trafficFlowLayer: GodsEyeLayer = {
	id: 'flow',
	name: 'Traffic flow (congestion ramp)',
	label: 'FLOW',
	requiresKeyId: null,
	pickPrefix: TRAFFIC_FLOW_PICK_PREFIX,
	defaultOn: true,
	create: createTrafficFlowLayer,
};
