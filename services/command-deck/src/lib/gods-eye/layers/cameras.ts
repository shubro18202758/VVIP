/**
 * cameras layer — ANPR / CCTV camera REGISTRY markers (locations ONLY).
 *
 * Feed: GET /api/v1/cameras (canvas store; contract C1). Rows carry
 * {camera_id, lon, lat, kind: anpr|cctv, source: osm|emulated} — the
 * OSM surveillance=ANPR nodes (ODbL, the keyless gev alprCameras pattern)
 * are merged SERVER-SIDE by the watchlist-anpr stream; the canvas never
 * queries Overpass itself (canvas fetches only its own deck origin).
 *
 * ETHICS RAIL (C6, upstream gev DATA_SOURCES.md:94): this layer maps WHERE
 * plate readers are — it reads NOTHING from any frame, carries no plate or
 * person fields, and badges every marker with its source.
 */

import { godsEye } from '../store.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor } from '../theme';

export const CAMERAS_PICK_PREFIX = 'cam:';

/**
 * Camera-kind colours from the tokens: ANPR readers take the tool accent
 * (machine senses), CCTV sits dim — locations only, never frames (C6).
 */
function kindColor(kind: 'anpr' | 'cctv', cesium: CesiumGlobal): unknown {
	return kind === 'anpr'
		? cssColor(cesium, '--v-tool', cesium.Color.CYAN)
		: cssColor(cesium, '--v-ink-3', cesium.Color.LIGHTGRAY);
}

function createCamerasLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const cameras = godsEye.cameras;
		const sig = JSON.stringify([
			cameras.map((c) => [c.cameraId, c.kind, c.source, c.lon, c.lat]),
			godsEye.camerasFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		for (const c of cameras) {
			const entity = viewer.entities.add({
				id: `${CAMERAS_PICK_PREFIX}${c.cameraId}`,
				position: cesium.Cartesian3.fromDegrees(c.lon, c.lat, 6),
				point: {
					pixelSize: c.kind === 'anpr' ? 7 : 5,
					color: kindColor(c.kind, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: 1,
				},
				description: `camera ${c.cameraId} · ${c.kind.toUpperCase()} · source ${
					c.source
				} · location only (no frame data)`,
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
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.camerasFeed.status === 'empty' && godsEye.camerasFeed.updatedAt === 0,
				hint:
					godsEye.camerasFeed.status === 'not-deployed'
						? '/api/v1/cameras 404 — watchlist-anpr stream pending'
						: `${godsEye.cameras.filter((c) => c.source === 'emulated').length} emulated / ${
								godsEye.cameras.length
							} total`,
				error: godsEye.camerasFeed.note,
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
