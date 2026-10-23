/**
 * cameras layer — ANPR / CCTV camera REGISTRY markers (locations ONLY).
 *
 * Feed: the canvas store's cameras — the city registry's camera entities, or
 * GET /api/v1/cameras → {cameras: [...]} as the fallback (contract C-C).
 * Rows carry {camera_id, label, lon, lat, kind: anpr|cctv, source:
 * emulated|osm|registered, junction_id, heading_deg, status, reads_15m,
 * hits_15m, last_read_t} — the OSM surveillance=ANPR nodes (ODbL, the
 * keyless gev alprCameras pattern) are merged SERVER-SIDE; the canvas never
 * queries Overpass itself (canvas fetches only its own deck origin).
 *
 * A camera whose reads matched a watchlist plate in the last 15 min
 * (hits_15m > 0) PULSES in the human-act orange — someone must look; quiet
 * readers sit in the tool accent, CCTV dim. prefers-reduced-motion: the
 * pulse becomes a static enlarged marker (same information, no motion).
 *
 * ETHICS RAIL (C6, upstream gev DATA_SOURCES.md:94): this layer maps WHERE
 * plate readers are and HOW MANY reads they made — it reads NOTHING from any
 * frame, carries no plate or person fields, and badges every marker with
 * its source.
 *
 * Clickable over everything: 9–10 px dots drawn without depth test (a
 * signal ring or a spine at the same spot never hides them), and readers
 * sharing one site (the two heads of a bridge, a junction's approaches)
 * fan out on a small ring so each is its own click. An earmarked vehicle's
 * mark sits ON the camera that read it last: that camera's marker steps
 * aside (east) so the vehicle and the camera are both their own click.
 * Times print in IST.
 * An EMPTY registry is a hint ('no cameras registered'), not a fault: only
 * a feed that is down or not deployed raises the warning chip.
 *
 * DRAPED (layers/draping.ts): every marker is clamped to the terrain —
 * street level, under the photoreal 3D tiles too — so a reader at a
 * junction stands at the junction on the 3D city, and no building hides it.
 *
 * Pick ids `cam:<cameraId>` → URN `camera:<cameraId>`.
 */

import { godsEye } from '../store.svelte';
import type { CameraRecord } from '../data';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor } from '../theme';
import { istStamp } from '../hud/format';
import { clampedDot, drapeAudit, ground } from './draping';

export const CAMERAS_PICK_PREFIX = 'cam:';

/** Pulse cycle for a camera with recent watchlist hits (ms). */
const PULSE_MS = 1400;
/** Marker sizes (px): ANPR readers are the click targets; CCTV a step smaller. */
const SIZE_PX = { anpr: 10, cctv: 9 } as const;
/** Sites closer than this share one spot and fan out on FAN_RADIUS_M. */
const COLOCATED_M = 10;
const FAN_RADIUS_M = 26;
/** A camera under a vehicle mark steps this far east (≈ 6 px at the city view). */
const VEHICLE_CLEAR_M = 45;

/**
 * Display positions with co-located sites fanned out on a small ring
 * (clockwise from north, in id order) — the camera's own coordinates stay
 * in its description and dossier; only the marker moves.
 */
export function fanOut(
	cameras: CameraRecord[],
	vehicleMarks: Array<{ lon: number; lat: number }> = [],
): Map<string, { lon: number; lat: number }> {
	const out = new Map<string, { lon: number; lat: number }>();
	const groups: CameraRecord[][] = [];
	const kx = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);
	for (const c of [...cameras].sort((a, b) => a.cameraId.localeCompare(b.cameraId))) {
		const group = groups.find((g) => {
			const h = g[0];
			return Math.hypot((c.lon - h.lon) * kx(h.lat), (c.lat - h.lat) * 110_540) <= COLOCATED_M;
		});
		if (group) group.push(c);
		else groups.push([c]);
	}
	for (const g of groups) {
		if (g.length === 1) {
			out.set(g[0].cameraId, { lon: g[0].lon, lat: g[0].lat });
			continue;
		}
		g.forEach((c, i) => {
			const a = (2 * Math.PI * i) / g.length;
			out.set(c.cameraId, {
				lon: g[0].lon + (FAN_RADIUS_M * Math.sin(a)) / kx(g[0].lat),
				lat: g[0].lat + (FAN_RADIUS_M * Math.cos(a)) / 110_540,
			});
		});
	}
	for (const [id, pos] of out) {
		const under = vehicleMarks.some(
			(v) => Math.hypot((v.lon - pos.lon) * kx(pos.lat), (v.lat - pos.lat) * 110_540) <= COLOCATED_M,
		);
		if (under) out.set(id, { lon: pos.lon + VEHICLE_CLEAR_M / kx(pos.lat), lat: pos.lat });
	}
	return out;
}

/**
 * Camera-kind colours from the tokens: ANPR readers take the tool accent
 * (machine senses), CCTV sits dim — locations only, never frames (C6).
 */
function kindColor(kind: 'anpr' | 'cctv', cesium: CesiumGlobal): unknown {
	return kind === 'anpr'
		? cssColor(cesium, '--v-tool', cesium.Color.CYAN)
		: cssColor(cesium, '--v-ink-3', cesium.Color.LIGHTGRAY);
}

function reducedMotion(): boolean {
	if (typeof matchMedia !== 'function') return false;
	return matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function describe(c: CameraRecord): string {
	const last = c.lastReadT != null ? `${istStamp(c.lastReadT)} IST` : '—';
	return `${c.label ? `${c.label} · ` : ''}camera ${c.cameraId} · ${c.kind.toUpperCase()} · source ${c.source}${
		c.source === 'emulated' ? ' (EMULATED)' : ''
	}${c.junctionId != null ? ` · junction ${c.junctionId}` : ''} · reads 15m ${c.reads15m} · watchlist hits 15m ${
		c.hits15m
	} · last read ${last}${c.status ? ` · ${c.status}` : ''} · location + counts only (no frame data)`;
}

function createCamerasLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';
	let hitCount = 0;

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const cameras = godsEye.cameras;
		const marks = godsEye.vehicles
			.filter((v) => v.lastSighting)
			.map((v) => ({ lon: v.lastSighting!.lon, lat: v.lastSighting!.lat }));
		const sig = JSON.stringify([
			cameras.map((c) => [c.cameraId, c.kind, c.source, c.lon, c.lat, c.reads15m, c.hits15m]),
			marks,
			godsEye.camerasFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		hitCount = 0;

		const still = reducedMotion();
		const at = fanOut(cameras, marks);
		for (const c of cameras) {
			const hot = c.hits15m > 0;
			if (hot) hitCount++;
			const base = SIZE_PX[c.kind];
			const pos = at.get(c.cameraId) ?? { lon: c.lon, lat: c.lat };
			// Hot readers pulse (base → base+6 px, sine); reduced motion holds
			// the enlarged size instead.
			// The dot is drawn at the pulse's peak size; the pulse scales it
			// (base → base+6 px, sine). Reduced motion holds the enlarged size.
			const peak = base + 6;
			const pulse =
				hot && !still
					? new cesium.CallbackProperty(
							() => (base + 3 + 3 * Math.sin((2 * Math.PI * (performance.now() % PULSE_MS)) / PULSE_MS)) / peak,
							false,
						)
					: undefined;
			const entity = viewer.entities.add({
				id: `${CAMERAS_PICK_PREFIX}${c.cameraId}`,
				position: ground(cesium, pos.lon, pos.lat),
				// never hidden under a spine, a signal ring, terrain or a building
				billboard: clampedDot(cesium, {
					pixelSize: hot ? (still ? base + 5 : peak) : base,
					color: hot ? cssColor(cesium, '--v-act', cesium.Color.ORANGE) : kindColor(c.kind, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: hot ? 2 : 1.5,
					pulse,
				}),
				description: describe(c),
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
			void godsEye.pollCameras();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(CAMERAS_PICK_PREFIX);
		},
		urnOf(pickId) {
			const id = pickId.slice(CAMERAS_PICK_PREFIX.length);
			return id ? `camera:${id}` : null;
		},
		coverageStats() {
			const cams = godsEye.cameras;
			const reads = cams.reduce((n, c) => n + c.reads15m, 0);
			const reporting = cams.filter((c) => c.reads15m > 0).length;
			const feed = godsEye.camerasFeed;
			// A fault is a feed that is down or not deployed; an empty
			// registry is a state the hint states (not the red chip).
			const fault = feed.status === 'down' || feed.status === 'not-deployed';
			return {
				count: entityIds.length,
				loading: feed.status === 'empty' && feed.updatedAt === 0,
				hint:
					feed.status === 'not-deployed'
						? '/api/v1/cameras 404 — camera registry pending'
						: feed.status === 'empty' && feed.updatedAt > 0
							? `${feed.note ?? 'no cameras registered'} · locations + counts only`
							: `${cams.filter((c) => c.source === 'emulated').length} emulated / ${cams.length} total · ${hitCount} with watchlist hits (15 min) · ${reads} reads · locations + counts only`,
				error: fault ? (feed.note ?? `camera feed ${feed.status}`) : null,
				detail: {
					cameras: cams.length,
					withHits: hitCount,
					reporting,
					reads15m: reads,
					...(ctx && entityIds.length ? drapeAudit(ctx.viewer, entityIds) : {}),
				},
				via: godsEye.via('camera'),
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const camerasLayer: GodsEyeLayer = {
	id: 'cameras',
	name: 'Camera registry (ANPR/CCTV locations)',
	label: 'CAMERAS',
	requiresKeyId: null,
	pickPrefix: CAMERAS_PICK_PREFIX,
	defaultOn: true,
	create: createCamerasLayer,
};
