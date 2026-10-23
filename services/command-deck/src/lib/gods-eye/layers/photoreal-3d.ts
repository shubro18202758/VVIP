/**
 * photoreal-3d layer — Google Photorealistic 3D Tiles over the city (C-L).
 *
 * Keyed: when the mode endpoint hands the canvas the ion upgrade
 * (godsEye.imageryMode 'ion-photoreal' with a token — VEER_CESIUM_ION_TOKEN
 * on the deck service, withheld from anonymous LAN clients while the
 * operator gate is on), this layer streams Google Photorealistic 3D Tiles
 * (Cesium ion asset 2275207) through `createGooglePhotorealistic3DTileset()`
 * — or `Cesium3DTileset.fromIonAssetId(2275207)` on a build without it —
 * with the data credits on screen (Google's terms). Once the tiles covering
 * the first view have loaded, the city's own buildings carry the picture
 * and the globe goes UNDER them: still shown (it is the overlays' ground —
 * see below) but at a coarse screen-space error, so it costs little beneath
 * the mesh; its full detail returns when the layer is switched off or when
 * loading fails (a bad token answers 401; repeated tile failures) — and the
 * chip names why. Before the tiles cover the view nothing changes, so a
 * slow venue network costs nothing: when the first view is not covered
 * within 12 s the chip says so ('still loading … globe kept') and the tiles
 * keep streaming over the visible globe — no re-toggle needed. A load that
 * fails for a TRANSIENT reason (no answer, a dropped TLS handshake, a 5xx /
 * 429 — from this host one of api.cesium.com's CDN edges refuses every TLS
 * handshake, and a browser keeps that address for the DNS lifetime) is
 * retried on a widening schedule that outlives the DNS cache; each retry
 * asks ion afresh through the generic factory (the Google factory keeps its
 * first ion answer, a failure included, for the life of the page). A
 * refusal (401/403/404 — a bad token or a missing asset) fails at once. No
 * geocoder is used with the tiles (the viewer has none).
 *
 * Keyless: the layer reports unconfigured ('needs VEER_CESIUM_ION_TOKEN')
 * and makes no request at all — the keyless promise is zero cesium.com and
 * zero googleapis traffic.
 *
 * Picks: a tile feature is scenery, never an entity — this layer claims no
 * pick (handlePick false, urnOf null), so a click on a building never opens
 * a card (contract C-L).
 *
 * The overlays stand ON the city (layers/draping.ts): draped lines classify
 * these tiles (and the terrain), and clamped markers stand on the TERRAIN —
 * street level — kept beneath the tiles for exactly that. Tile COLLISION is
 * off (`enableCollision: false`; the Google factory defaults it on): with
 * it, every clamped marker and every camera move asks the tileset for a
 * height, and in Cesium 1.146 on WebGL2 each such query reads the candidate
 * tiles' geometry back from the GPU — measured on the demo box under
 * SwiftShader at 1920×1080: 134 queries in 30 s costing 12.3 s, frames of
 * 4.9 s. Terrain heights cost microseconds and match the street within
 * metres on Ahmedabad's flat ground; markers skip the depth test, so no
 * building hides one. Without collision nothing stops a wheel zoom at a
 * wall, so while the tiles stand the camera controller keeps a zoom floor
 * (PHOTOREAL_MIN_ZOOM_M) off the surface under the cursor and above the
 * terrain — restored when the layer goes off or fails. Detail vs load: the
 * tiles' screen-space error (below)
 * and a bounded tile cache (the deck shares unified memory with the model
 * hosts); the cache in use is on the chip's detail (memMB).
 */

import { godsEye } from '../store.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { PHOTOREAL_3D_ASSET_ID } from './basemap';

export const PHOTOREAL_PICK_PREFIX = 'p3d:';

/**
 * No initial tiles within this long → the chip reports a slow load and why
 * the globe stays shown; the tiles keep streaming (a slow network is not a
 * failure — only a rejected load or repeated tile failures are).
 */
export const PHOTOREAL_READY_TIMEOUT_MS = 12_000;
/**
 * Detail vs load (Cesium default 16). Measured on the demo box under
 * SwiftShader at 1920×1080 (judge view, every layer on): the frame is
 * fill-bound — 283 ms median at 16, 24 and 32 alike — so the error only
 * trades tiles: 24 keeps the old city's buildings as sharp as 16 does at
 * that range with ~59 MB of tiles instead of ~74 MB, and fewer tiles to
 * stream while the wall orbits.
 */
export const PHOTOREAL_MAX_SCREEN_SPACE_ERROR = 24;
/**
 * The globe's screen-space error while it lies under the tiles (Cesium
 * default 2): coarse terrain is invisible beneath the mesh, still the
 * overlays' ground, and far cheaper to keep.
 */
export const GLOBE_UNDER_TILES_SSE = 8;
/**
 * How close the operator's own camera may come to the city (m) while the
 * tiles stand: tile COLLISION is off (header), so without a floor a wheel
 * zoom would carry the camera into the mesh. Cesium's camera controller
 * keeps this distance from the surface under the cursor while zooming (a
 * depth pick — the tiles included) and above the terrain at all times.
 * Every flight frames from ≥ 1 km, and Ahmedabad's old city stands well
 * under 80 m.
 */
export const PHOTOREAL_MIN_ZOOM_M = 80;
/** Tile cache bound — the deck shares unified memory with the model hosts. */
const CACHE_BYTES = 512 * 1024 * 1024;
const CACHE_OVERFLOW_BYTES = 256 * 1024 * 1024;
/** Tile failures before the first view counts as a failed load. */
const MAX_EARLY_TILE_FAILURES = 3;
/**
 * Retry schedule (ms) for a transient load failure — the later steps land
 * after a browser's DNS cache (CloudFront: 60 s) lets it pick another edge.
 */
export const PHOTOREAL_RETRY_MS: ReadonlyArray<number> = Object.freeze([5_000, 20_000, 65_000, 130_000]);

const NEEDS_TOKEN = 'needs VEER_CESIUM_ION_TOKEN — Google Photorealistic 3D Tiles load only on the env-keyed deck';

type Phase = 'idle' | 'unconfigured' | 'loading' | 'ready' | 'failed' | 'off';

function configured(): boolean {
	return godsEye.imageryMode === 'ion-photoreal' && !!godsEye.imageryToken;
}

function why(err: unknown): string {
	const text = err instanceof Error ? err.message : typeof err === 'string' ? err : JSON.stringify(err);
	return (text ?? 'unknown error').slice(0, 120);
}

/**
 * A failure worth retrying: no HTTP status at all (no answer, a refused TLS
 * handshake — Cesium says 'Request has failed.'), or a 5xx / 429. A 4xx
 * refusal (bad token, missing asset) is final.
 */
export function transientLoadFailure(err: unknown): boolean {
	const status = (err as { statusCode?: unknown })?.statusCode;
	const code = typeof status === 'number' ? status : Number(/Status Code:\s*(\d{3})/.exec(why(err))?.[1] ?? NaN);
	if (!Number.isFinite(code)) return true;
	return code >= 500 || code === 429;
}

/**
 * The tileset, from the best factory this Cesium build has — the generic
 * ion factory on a retry (`fresh`): the Google factory caches its first ion
 * answer for the page's life, a failed one included.
 */
function loadTileset(cesium: CesiumGlobal, fresh = false): Promise<CesiumTileset> {
	const options = {
		showCreditsOnScreen: true,
		maximumScreenSpaceError: PHOTOREAL_MAX_SCREEN_SPACE_ERROR,
		cacheBytes: CACHE_BYTES,
		maximumCacheOverflowBytes: CACHE_OVERFLOW_BYTES,
		// no tile height queries: markers clamp to the terrain beneath (header)
		enableCollision: false,
	};
	if (!fresh && typeof cesium.createGooglePhotorealistic3DTileset === 'function') {
		return cesium.createGooglePhotorealistic3DTileset({ onlyUsingWithGoogleGeocoder: true }, options);
	}
	return cesium.Cesium3DTileset.fromIonAssetId(PHOTOREAL_3D_ASSET_ID, options);
}

function createPhotoreal3dLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let phase: Phase = 'idle';
	let reason: string | null = null;
	let tileset: CesiumTileset | null = null;
	/** The globe's own screen-space error while it lies under the tiles (null: not under). */
	let globeSseBefore: number | null = null;
	/** The camera controller's own zoom floor while the tiles stand (null: not raised). */
	let zoomFloorBefore: number | null = null;
	let initialTilesLoaded = false;
	/** Past the 12 s mark without the first view covered (still streaming). */
	let slow = false;
	let earlyFailures = 0;
	/** Load attempts that failed for a transient reason so far (this activation). */
	let retries = 0;
	/** The last transient failure, while a retry is pending (the chip says why). */
	let retryReason: string | null = null;
	/** Bumped on every activate/deactivate: a stale async load never lands. */
	let generation = 0;
	let watchdog: ReturnType<typeof setTimeout> | null = null;
	let retryTimer: ReturnType<typeof setTimeout> | null = null;
	let listeners: Array<() => void> = [];

	function clearWatch() {
		if (watchdog) clearTimeout(watchdog);
		watchdog = null;
		if (retryTimer) clearTimeout(retryTimer);
		retryTimer = null;
		for (const off of listeners) off();
		listeners = [];
	}

	/** The tiles carry the picture: the globe stays as the ground, coarse; the camera keeps off the mesh. */
	function sinkGlobe() {
		if (!ctx || globeSseBefore != null) return;
		const globe = ctx.viewer.scene.globe;
		globeSseBefore = globe.maximumScreenSpaceError ?? 2;
		globe.maximumScreenSpaceError = GLOBE_UNDER_TILES_SSE;
		const sscc = ctx.viewer.scene.screenSpaceCameraController;
		if (sscc && zoomFloorBefore == null) {
			zoomFloorBefore = sscc.minimumZoomDistance ?? 1;
			sscc.minimumZoomDistance = Math.max(zoomFloorBefore, PHOTOREAL_MIN_ZOOM_M);
		}
	}

	function restoreGlobe() {
		if (!ctx) return;
		const sscc = ctx.viewer.scene.screenSpaceCameraController;
		if (sscc && zoomFloorBefore != null) sscc.minimumZoomDistance = zoomFloorBefore;
		zoomFloorBefore = null;
		if (globeSseBefore == null) return;
		ctx.viewer.scene.globe.maximumScreenSpaceError = globeSseBefore;
		globeSseBefore = null;
	}

	function dropTileset() {
		if (ctx && tileset) {
			const primitives = ctx.viewer.scene.primitives;
			if (primitives.contains(tileset)) primitives.remove(tileset); // destroys it
			else if (!tileset.isDestroyed()) tileset.destroy();
		}
		tileset = null;
		initialTilesLoaded = false;
		slow = false;
		earlyFailures = 0;
	}

	function fail(message: string, gen: number) {
		if (gen !== generation) return;
		clearWatch();
		dropTileset();
		restoreGlobe();
		phase = 'failed';
		reason = message;
		ctx?.notify();
	}

	/** A transient failure retries on the schedule; the last one (or a refusal) fails. */
	function retryOrFail(err: unknown, gen: number) {
		if (gen !== generation || !ctx) return;
		const delay = PHOTOREAL_RETRY_MS[retries];
		if (!transientLoadFailure(err) || delay === undefined) {
			fail(`3D tiles unavailable (${why(err)})${retries ? ` after ${retries + 1} attempts` : ''}`, gen);
			return;
		}
		retries++;
		retryReason = why(err);
		retryTimer = setTimeout(() => {
			retryTimer = null;
			if (gen === generation) load(gen, true);
		}, delay);
		ctx.notify();
	}

	function start() {
		if (!ctx) return;
		const gen = ++generation;
		phase = 'loading';
		reason = null;
		slow = false;
		retries = 0;
		retryReason = null;
		// the layer is self-sufficient on the key (createIonBasemap sets the same token)
		ctx.cesium.Ion.defaultAccessToken = godsEye.imageryToken ?? ctx.cesium.Ion.defaultAccessToken;
		// Slow is not failed: the globe still carries the view, so the 12 s mark only
		// tells the operator why the city is still flat — the load goes on.
		watchdog = setTimeout(() => {
			watchdog = null;
			if (gen !== generation || !ctx || initialTilesLoaded) return;
			slow = true;
			ctx.notify();
		}, PHOTOREAL_READY_TIMEOUT_MS);
		load(gen, false);
		ctx.notify();
	}

	function load(gen: number, fresh: boolean) {
		if (!ctx) return;
		const { cesium, viewer } = ctx;
		let pending: Promise<CesiumTileset>;
		try {
			pending = loadTileset(cesium, fresh);
		} catch (err) {
			fail(`3D tiles unavailable (${why(err)})`, gen);
			return;
		}
		pending.then(
			(ts) => {
				if (gen !== generation || !ctx) {
					if (!ts.isDestroyed()) ts.destroy(); // switched off meanwhile
					return;
				}
				retryReason = null;
				tileset = viewer.scene.primitives.add(ts);
				listeners.push(
					ts.initialTilesLoaded.addEventListener(() => {
						if (gen !== generation || !ctx) return;
						if (watchdog) clearTimeout(watchdog);
						watchdog = null;
						initialTilesLoaded = true;
						slow = false;
						sinkGlobe();
						phase = 'ready';
						ctx.notify();
					}),
					ts.tileFailed.addEventListener((err) => {
						if (initialTilesLoaded) return; // a stray tile later is not a failed layer
						earlyFailures++;
						if (earlyFailures >= MAX_EARLY_TILE_FAILURES) {
							fail(`3D tiles failing (${why((err as { message?: unknown })?.message ?? err)})`, gen);
						}
					}),
				);
				ctx.notify();
			},
			(err) => retryOrFail(err, gen),
		);
	}

	return {
		async status() {
			return { configured: configured() };
		},
		init(c) {
			ctx = c;
		},
		activate() {
			if (!ctx) return;
			if (!configured()) {
				phase = 'unconfigured';
				reason = null;
				ctx.notify();
				return;
			}
			start();
		},
		deactivate() {
			generation++;
			clearWatch();
			dropTileset();
			restoreGlobe();
			phase = configured() ? 'off' : 'unconfigured';
			reason = null;
			retries = 0;
			retryReason = null;
		},
		refresh() {
			// nothing to poll: the tiles stream on the camera
		},
		handlePick() {
			return false; // tile features are scenery — never a card (C-L)
		},
		urnOf() {
			return null;
		},
		coverageStats() {
			const bytes = tileset && !tileset.isDestroyed() ? tileset.totalMemoryUsageInBytes : undefined;
			const detail = {
				configured: configured() ? 1 : 0,
				globeUnderTiles: globeSseBefore != null ? 1 : 0,
				initialTilesLoaded: initialTilesLoaded ? 1 : 0,
				slow: slow ? 1 : 0,
				retries,
				...(zoomFloorBefore != null ? { minZoomM: PHOTOREAL_MIN_ZOOM_M } : {}),
				...(typeof bytes === 'number' && Number.isFinite(bytes) ? { memMB: Math.round(bytes / 1e6) } : {}),
			};
			switch (phase) {
				case 'loading':
					return {
						count: tileset ? 1 : 0,
						loading: true,
						hint: retryReason
							? `Cesium ion did not answer (${retryReason}) — retry ${retries}/${PHOTOREAL_RETRY_MS.length} scheduled; the globe carries the view`
							: slow
								? `still loading Google Photorealistic 3D Tiles after ${Math.round(PHOTOREAL_READY_TIMEOUT_MS / 1000)} s (slow network) — the globe carries the view until they cover it`
								: `loading Google Photorealistic 3D Tiles (ion asset ${PHOTOREAL_3D_ASSET_ID})…`,
						error: null,
						detail,
						via: 'ion',
					};
				case 'ready':
					return {
						count: 1,
						loading: false,
						hint: `Google Photorealistic 3D Tiles (ion asset ${PHOTOREAL_3D_ASSET_ID}) · terrain kept beneath as the overlays' ground · credits on screen`,
						error: null,
						detail,
						via: 'ion',
					};
				case 'failed':
					return {
						count: 0,
						loading: false,
						hint: `3D off — ${reason ?? 'load failed'}; the globe carries the view`,
						error: reason ?? '3D tiles failed',
						detail,
						via: 'failed',
					};
				case 'off':
					return {
						count: 0,
						loading: false,
						hint: '3D tiles off — the globe and the basemap are shown',
						error: null,
						detail,
						via: 'off',
					};
				default:
					return {
						count: 0,
						loading: false,
						hint: configured() ? '3D tiles off' : NEEDS_TOKEN,
						error: null,
						detail,
						via: configured() ? 'off' : 'unconfigured',
					};
			}
		},
		destroy() {
			this.deactivate();
			ctx = null;
		},
	};
}

export const photoreal3dLayer: GodsEyeLayer = {
	id: 'photoreal-3d',
	name: 'Photorealistic 3D city (Google, via Cesium ion)',
	label: '3D',
	requiresKeyId: 'ion',
	pickPrefix: PHOTOREAL_PICK_PREFIX,
	defaultOn: true,
	create: createPhotoreal3dLayer,
};
