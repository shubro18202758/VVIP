/**
 * traffic-flow layer — the restored VVIP template's signature live-congestion
 * network, drawn ON the God's-eye canvas.
 *
 * Geometry comes from the shared canvas segment feed (GET /spatial/segments,
 * cached in the gods-eye store); congestion comes from the oracle's Valkey
 * hot cache (POST /api/v1/traffic/live with the visible segment ids).
 *
 * HONEST DEGRADATION (the system's rule, kept): a segment with NO current
 * observation renders as a thin neutral hairline — never a guessed colour.
 * When the hot cache is empty (quiet window, emulator idle) the whole
 * network renders neutral and the layer hint says so; the ramp colours
 * appear the moment observations flow (signal-ingress / emulator runs).
 *
 * Ramp (design tokens at runtime — zero hex literals, theme.ts bridge):
 *   congestion ≤0.2 band-green (free) · ≤0.4 flow-light · ≤0.6 band-amber
 *   · ≤0.8 act (heavy) · >0.8 band-red (severe) — the template ladder,
 *   expressed entirely in existing token slots.
 */

import { godsEye } from '../store.svelte';
import { ORACLE_V1 } from '$lib/ops/endpoints';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssVar } from '../theme';

export const TRAFFIC_FLOW_PICK_PREFIX = 'seg:';

interface LiveObservation {
	segment_id: number;
	speed_kmh: number;
	congestion_idx: number;
	timestamp_ms: number;
	source?: string;
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

function withAlpha(css: string, alpha: number): string {
	if (/^#[0-9a-f]{6}$/i.test(css)) {
		const r = parseInt(css.slice(1, 3), 16);
		const g = parseInt(css.slice(3, 5), 16);
		const b = parseInt(css.slice(5, 7), 16);
		return `rgba(${r},${g},${b},${alpha})`;
	}
	return css;
}

function createTrafficFlowLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let drawTimer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let obsById = new Map<number, LiveObservation>();
	let obsFeed: 'ok' | 'empty' | 'down' = 'empty';
	let hint = 'loading';
	let lastSignature = '';

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
			const body = (await res.json()) as { observations?: LiveObservation[] };
			const next = new Map<number, LiveObservation>();
			for (const o of body.observations ?? []) {
				if (typeof o?.segment_id === 'number') next.set(o.segment_id, o);
			}
			obsById = next;
			obsFeed = next.size ? 'ok' : 'empty';
		} catch {
			// Leave last-good observations on the canvas (stale colouring is
			// worse than none only after the TTL window; the hint degrades).
			obsFeed = obsById.size ? 'ok' : 'down';
		}
	}

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const segments = godsEye.segments;
		const sig = JSON.stringify([
			segments.map((s) => s.segmentId),
			[...obsById.entries()].map(([id, o]) => [id, Math.round(o.congestion_idx * 20) / 20, Math.round(o.speed_kmh)]),
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		const neutral = withAlpha(cssVar('--v-ink-4') ?? 'slategray', 0.55);
		let coloured = 0;

		for (const seg of segments) {
			if (seg.coordinates.length < 2) continue;
			const positions: unknown[] = [];
			for (const [lon, lat] of seg.coordinates) {
				positions.push(cesium.Cartesian3.fromDegrees(lon, lat, 2));
			}
			if (positions.length < 2) continue;

			const obs = obsById.get(seg.segmentId);
			const width = obs ? 3.5 : 1.2;
			const material = obs
				? cesium.Color.fromCssColorString(rampColor(obs.congestion_idx))
				: cesium.Color.fromCssColorString(neutral);
			if (obs) coloured++;

			const ageSec = obs ? Math.max(0, Math.round((Date.now() - obs.timestamp_ms) / 1000)) : null;
			const description = obs
				? `${seg.roadName ?? `segment ${seg.segmentId}`} · ${obs.speed_kmh.toFixed(1)} km/h · congestion ${(obs.congestion_idx * 100).toFixed(0)}% · source ${obs.source ?? '?'} · ${ageSec}s ago`
				: `${seg.roadName ?? `segment ${seg.segmentId}`} · no live observation (neutral)`;

			const entity = viewer.entities.add({
				id: `${TRAFFIC_FLOW_PICK_PREFIX}${seg.segmentId}`,
				polyline: {
					positions,
					width,
					material,
					clampToGround: false,
				},
				description,
			});
			entityIds.push(entity.id);
		}

		hint =
			obsFeed === 'down'
				? `traffic/live unreachable — network neutral (${entityIds.length} segments)`
				: obsFeed === 'empty'
					? `no live observations — network neutral (${entityIds.length} segments)`
					: `${coloured}/${entityIds.length} segments on the live ramp`;
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
			void pollObservations();
			draw();
			pollTimer = setInterval(() => void pollObservations(), 8000);
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
		},
		refresh() {
			lastSignature = '';
			void godsEye.pollSegments(true);
			void pollObservations();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(TRAFFIC_FLOW_PICK_PREFIX);
		},
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.segmentsFeed.status === 'empty',
				hint,
				error: obsFeed === 'down' ? 'traffic/live feed down' : null,
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const trafficFlowLayer: GodsEyeLayer = {
	id: 'flow',
	name: 'Traffic flow (live congestion ramp)',
	label: 'FLOW',
	requiresKeyId: null,
	pickPrefix: TRAFFIC_FLOW_PICK_PREFIX,
	defaultOn: true,
	create: createTrafficFlowLayer,
};
