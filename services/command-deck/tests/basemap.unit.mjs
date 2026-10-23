/**
 * Basemap + photoreal-3d unit test — the REAL modules against a fake Cesium.
 *
 * Bundles src/lib/gods-eye/layers/basemap.ts and photoreal-3d.ts with the
 * vendored esbuild (the God's-eye store is stubbed: a plain object the test
 * flips between keyless and ion), then asserts, hermetically (no browser,
 * no network — the fake records every call):
 *
 *   1. keyless basemap: the OSM standard raster with its ODbL credit and
 *      the dark re-grade, ellipsoid terrain, id 'osm-keyless'; no ion
 *      factory touched, no CARTO host (its styles now need an API key);
 *      1b. on a build with Credit the credit rides the ON-SCREEN plate —
 *      '© OpenStreetMap contributors' spelled out, linked to the copyright
 *      page, the same html as the canvas's static credit (printed once);
 *   2. (reserved — the CARTO label overlay was dropped with CARTO)
 *   3. the ion branch: token set, asset 2 imagery + world terrain;
 *   4. photoreal-3d keyless: unconfigured, 'needs VEER_CESIUM_ION_TOKEN',
 *      zero tile requests, the globe untouched;
 *   5. photoreal-3d keyed: Google Photorealistic 3D Tiles with on-screen
 *      credits, a screen-space error of 16–24 and NO tile collision (WP-22:
 *      a tile height query reads geometry back from the GPU per marker —
 *      clamped markers stand on the terrain instead); once the initial
 *      tiles load the globe goes UNDER them — still shown (the overlays'
 *      ground), its detail coarsened — and the camera keeps a zoom floor
 *      off the mesh (nothing else stops a wheel zoom at a wall); the
 *      globe's own detail and the camera's own floor return on deactivate
 *      and on a failed load (the chip names the failure); past
 *      the 12 s watchdog a slow load is kept streaming over the visible
 *      globe, the chip says why, and late tiles still land; the generic ion
 *      factory (asset 2275207) on a build without the Google one; a load
 *      that lands after the layer was switched off is discarded;
 *   6. picks: a tile feature never opens a card (handlePick false, urnOf null);
 *   7. honest mode (WP-22): imageryModeOf / imageryChip name what is on
 *      screen — PHOTOREAL 3D · GOOGLE once the tiles cover the view (even if
 *      the imagery under them stumbles), else the offline / ion / keyless
 *      ladder — and every chip key reads in en, hi and gu.
 *
 * Run from services/command-deck:   node tests/basemap.unit.mjs
 * (bundle written to tests/artifacts/, which is gitignored)
 */

import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const ARTIFACTS = path.join(HERE, 'artifacts');
mkdirSync(ARTIFACTS, { recursive: true });

const esbuild = await import(pathToFileURL(path.join(DECK, 'node_modules/esbuild/lib/main.js')).href);

// The store module needs the Svelte compiler; the layer only reads two fields.
const stubStore = {
	name: 'stub-gods-eye-store',
	setup(build) {
		build.onResolve({ filter: /store\.svelte$/ }, () => ({ path: 'gods-eye-store-stub', namespace: 'stub' }));
		build.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
			contents: 'export const godsEye = { imageryMode: "keyless-osm", imageryToken: null };',
			loader: 'js',
		}));
	},
};

// The entry is handed to esbuild on stdin: no generated .ts lands in the tree.
const entry = [
	`export * from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/basemap.ts'))};`,
	`export * from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/photoreal-3d.ts'))};`,
	`export { godsEye } from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/store.svelte'))};`,
	`export { t } from ${JSON.stringify(path.join(DECK, 'src/lib/i18n/strings.ts'))};`,
].join('\n');
const outfile = path.join(ARTIFACTS, 'basemap-unit-bundle.mjs');
await esbuild.build({
	stdin: { contents: entry, resolveDir: DECK, loader: 'ts', sourcefile: 'basemap-unit.entry.ts' },
	absWorkingDir: DECK,
	alias: { $lib: './src/lib' },
	bundle: true,
	platform: 'node',
	format: 'esm',
	outfile,
	logLevel: 'error',
	plugins: [stubStore],
});
const M = await import(`${pathToFileURL(outfile).href}?t=${Date.now()}`);

let failures = 0;
function check(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}
const tick = () => new Promise((r) => setTimeout(r, 0));
/** Microtask turns only — usable while setTimeout is captured. */
const flush = async () => {
	for (let i = 0; i < 8; i++) await Promise.resolve();
};

// ── the fake Cesium ─────────────────────────────────────────────────────────

class FakeEvent {
	constructor() {
		this.listeners = new Set();
	}
	addEventListener(cb) {
		this.listeners.add(cb);
		return () => this.listeners.delete(cb);
	}
	raise(...args) {
		for (const cb of [...this.listeners]) cb(...args);
	}
}

function makeCesium() {
	const calls = { urlProviders: [], ionImagery: [], worldTerrain: 0, google: [], ionTileset: [] };
	const cesium = {
		calls,
		Ion: { defaultAccessToken: 'cesium-default' },
		UrlTemplateImageryProvider: class {
			constructor(opts) {
				this.opts = opts;
				this.errorEvent = new FakeEvent();
				calls.urlProviders.push(opts);
			}
		},
		SingleTileImageryProvider: class {
			constructor(opts) {
				this.opts = opts;
			}
		},
		ImageryLayer: Object.assign(
			class {
				constructor(provider) {
					this.imageryProvider = provider;
					this.readyEvent = new FakeEvent();
					this.errorEvent = new FakeEvent();
				}
			},
			{
				fromProviderAsync(promise) {
					const layer = new cesium.ImageryLayer(null);
					layer.async = promise;
					return layer;
				},
			},
		),
		IonImageryProvider: {
			fromAssetId(id) {
				calls.ionImagery.push(id);
				return Promise.resolve({ ion: id });
			},
		},
		EllipsoidTerrainProvider: class {
			constructor() {
				this.ellipsoid = true;
			}
		},
		Terrain: {
			fromWorldTerrain() {
				calls.worldTerrain++;
				return { readyEvent: new FakeEvent(), errorEvent: new FakeEvent() };
			},
		},
		Cesium3DTileset: {
			fromIonAssetId(id, opts) {
				calls.ionTileset.push({ id, opts });
				return cesium.nextTileset();
			},
		},
		createGooglePhotorealistic3DTileset(api, opts) {
			calls.google.push({ api, opts, token: cesium.Ion.defaultAccessToken });
			return cesium.nextTileset();
		},
		// the test decides each load's outcome
		pendingLoads: [],
		nextTileset() {
			return new Promise((resolve, reject) => cesium.pendingLoads.push({ resolve, reject }));
		},
	};
	return cesium;
}

function makeTileset() {
	return {
		show: true,
		destroyed: false,
		initialTilesLoaded: new FakeEvent(),
		tileFailed: new FakeEvent(),
		destroy() {
			this.destroyed = true;
		},
		isDestroyed() {
			return this.destroyed;
		},
	};
}

function makeViewer() {
	const list = [];
	const layerAdded = new FakeEvent();
	const imageryLayers = {
		get length() {
			return list.length;
		},
		get: (i) => list[i],
		indexOf: (l) => list.indexOf(l),
		add(layer, index) {
			if (index === undefined) list.push(layer);
			else list.splice(index, 0, layer);
			layerAdded.raise(layer, list.indexOf(layer));
			return layer;
		},
		remove(layer) {
			const i = list.indexOf(layer);
			if (i < 0) return false;
			list.splice(i, 1);
			return true;
		},
		removeAll() {
			list.length = 0;
		},
		raiseToTop(layer) {
			const i = list.indexOf(layer);
			if (i >= 0) {
				list.splice(i, 1);
				list.push(layer);
			}
		},
		layerAdded,
	};
	const prims = [];
	const primitives = {
		add(p) {
			prims.push(p);
			return p;
		},
		remove(p) {
			const i = prims.indexOf(p);
			if (i < 0) return false;
			prims.splice(i, 1);
			p.destroy();
			return true;
		},
		contains: (p) => prims.includes(p),
		list: prims,
	};
	const screenSpaceCameraController = { minimumZoomDistance: 1, maximumZoomDistance: 120_000 };
	return { scene: { imageryLayers, primitives, globe: { show: true, maximumScreenSpaceError: 2 }, screenSpaceCameraController }, list, prims };
}

function context(cesium, viewer) {
	let notified = 0;
	return {
		ctx: { cesium, viewer, fetchJson: async () => ({}), notify: () => notified++ },
		notified: () => notified,
	};
}

// ── 1. keyless basemap ──────────────────────────────────────────────────────
{
	const cesium = makeCesium();
	const b = M.createKeylessBasemap(cesium);
	const opts = cesium.calls.urlProviders[0];
	check('keyless id osm-keyless', b.id === 'osm-keyless');
	check('keyless ground is the OSM standard raster', opts.url === 'https://tile.openstreetmap.org/{z}/{x}/{y}.png' && M.OSM_TILE_URL === opts.url, opts.url);
	check('keyless credit names OpenStreetMap (ODbL)', opts.credit === M.OSM_CREDIT && opts.credit.startsWith('© OpenStreetMap contributors (ODbL)'), opts.credit);
	check('keyless uses no CARTO host', !JSON.stringify(cesium.calls.urlProviders).includes('cartocdn'));
	check('keyless re-grade applied', b.baseLayer.brightness === M.DARK_REGRADE.brightness && b.baseLayer.saturation === M.DARK_REGRADE.saturation);
	check('keyless ellipsoid terrain', b.terrainProvider?.ellipsoid === true && !b.terrain);
	check('keyless touches no ion factory', cesium.calls.ionImagery.length === 0 && cesium.calls.worldTerrain === 0 && cesium.Ion.defaultAccessToken === 'cesium-default');
	check('provider registry keeps its ids', M.BASEMAP_PROVIDERS.map((p) => p.id).join(',') === 'osm-keyless,ion-photoreal');
}

// ── 1b. the OSM attribution ON SCREEN (a build with Credit) ─────────────────
{
	const cesium = makeCesium();
	cesium.Credit = class {
		constructor(html, showOnScreen = false) {
			this.html = html;
			this.showOnScreen = showOnScreen;
		}
	};
	M.createKeylessBasemap(cesium);
	const credit = cesium.calls.urlProviders[0].credit;
	check(
		"keyless credit rides the on-screen plate ('© OpenStreetMap contributors', spelled out)",
		credit instanceof cesium.Credit && credit.showOnScreen === true && credit.html.includes('© OpenStreetMap contributors'),
		credit?.html,
	);
	check('on-screen credit links the OSM copyright page', credit.html.includes('href="https://www.openstreetmap.org/copyright"'));
	const again = M.osmScreenCredit(cesium);
	check("the canvas's static credit shares the raster credit's html (Cesium prints one id once)", again.html === credit.html && again.showOnScreen === true);
	check('a build without Credit: no on-screen credit (the string credit stands)', M.osmScreenCredit(makeCesium()) === null);
}

// ── 3. the ion branch ───────────────────────────────────────────────────────
{
	const cesium = makeCesium();
	const b = M.createIonBasemap(cesium, 'ion-token-abc');
	check('ion id ion-photoreal', b.id === 'ion-photoreal');
	check('ion token set', cesium.Ion.defaultAccessToken === 'ion-token-abc');
	check('ion asset 2 imagery', cesium.calls.ionImagery.join(',') === '2' && !!b.imageryLayer?.async);
	check('ion world terrain', cesium.calls.worldTerrain === 1 && !!b.terrain?.errorEvent);
	check('ion aerial grade', b.imageryLayer.brightness === M.AERIAL_GRADE.brightness);
}

// ── 4. photoreal-3d keyless ─────────────────────────────────────────────────
{
	M.godsEye.imageryMode = 'keyless-osm';
	M.godsEye.imageryToken = null;
	const cesium = makeCesium();
	const viewer = makeViewer();
	const { ctx } = context(cesium, viewer);
	check('layer registered as photoreal-3d / 3D / ion / on', M.photoreal3dLayer.id === 'photoreal-3d' && M.photoreal3dLayer.label === '3D' && M.photoreal3dLayer.requiresKeyId === 'ion' && M.photoreal3dLayer.defaultOn === true);
	const inst = M.photoreal3dLayer.create(ctx);
	check('keyless: status unconfigured', (await inst.status()).configured === false);
	inst.init(ctx);
	inst.activate();
	await tick();
	const st = inst.coverageStats();
	check('keyless: hint names the token', st.hint.includes('needs VEER_CESIUM_ION_TOKEN'), st.hint);
	check('keyless: unconfigured, not an error', st.via === 'unconfigured' && st.error === null && st.count === 0 && st.detail.configured === 0);
	check('keyless: zero tile requests', cesium.calls.google.length === 0 && cesium.calls.ionTileset.length === 0 && viewer.prims.length === 0);
	check('keyless: globe untouched', viewer.scene.globe.show === true);
	check('picks: never a card', inst.handlePick('p3d:anything') === false && inst.handlePick('whatever') === false && inst.urnOf('p3d:x') === null);
	inst.destroy();
}

// ── 5. photoreal-3d keyed ───────────────────────────────────────────────────
async function keyed(cesium) {
	M.godsEye.imageryMode = 'ion-photoreal';
	M.godsEye.imageryToken = 'ion-token-abc';
	const viewer = makeViewer();
	const c = context(cesium, viewer);
	const inst = M.photoreal3dLayer.create(c.ctx);
	inst.init(c.ctx);
	return { viewer, inst, c };
}

{
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	check('keyed: status configured', (await inst.status()).configured === true);
	inst.activate();
	const call = cesium.calls.google[0];
	check('keyed: Google Photorealistic 3D Tiles requested', !!call && call.api.onlyUsingWithGoogleGeocoder === true);
	check('keyed: credits on screen', call?.opts.showCreditsOnScreen === true);
	check('keyed: screen-space error 16-24', call?.opts.maximumScreenSpaceError >= 16 && call?.opts.maximumScreenSpaceError <= 24, String(call?.opts.maximumScreenSpaceError));
	check('keyed: no tile collision (markers clamp to the terrain, no GPU height read-backs)', call?.opts.enableCollision === false);
	check('keyed: the ion token is the factory key', call?.token === 'ion-token-abc');
	check('keyed: loading, globe still shown', inst.coverageStats().loading === true && viewer.scene.globe.show === true);
	const ts = makeTileset();
	cesium.pendingLoads[0].resolve(ts);
	await tick();
	check('keyed: tileset added to the scene', viewer.prims.length === 1 && inst.coverageStats().count === 1);
	ts.initialTilesLoaded.raise();
	let st = inst.coverageStats();
	check(
		'keyed: once the initial tiles load the globe goes UNDER them — shown, coarse',
		viewer.scene.globe.show === true && viewer.scene.globe.maximumScreenSpaceError === M.GLOBE_UNDER_TILES_SSE && st.detail.globeUnderTiles === 1 && st.detail.initialTilesLoaded === 1,
		st.hint,
	);
	check('keyed: ready, count 1, no error, the hint says the terrain is the overlays\' ground', st.count === 1 && st.error === null && st.via === 'ion' && st.hint.includes("overlays' ground"));
	check(
		'keyed: while the tiles stand the camera keeps a zoom floor off the mesh (no tile collision)',
		viewer.scene.screenSpaceCameraController.minimumZoomDistance === M.PHOTOREAL_MIN_ZOOM_M && st.detail.minZoomM === M.PHOTOREAL_MIN_ZOOM_M && M.PHOTOREAL_MIN_ZOOM_M >= 50 && M.PHOTOREAL_MIN_ZOOM_M <= 150,
		String(viewer.scene.screenSpaceCameraController.minimumZoomDistance),
	);
	ts.totalMemoryUsageInBytes = 58_700_000;
	check('keyed: the tile cache in use rides the detail (memMB)', inst.coverageStats().detail.memMB === 59);
	inst.deactivate();
	st = inst.coverageStats();
	check(
		'keyed: deactivate restores the globe detail and drops the tiles',
		viewer.scene.globe.show === true && viewer.scene.globe.maximumScreenSpaceError === 2 && viewer.prims.length === 0 && ts.destroyed && st.count === 0 && st.via === 'off' && st.detail.globeUnderTiles === 0,
	);
	check(
		'keyed: deactivate hands the camera its own zoom floor back',
		viewer.scene.screenSpaceCameraController.minimumZoomDistance === 1 && !('minZoomM' in st.detail),
		String(viewer.scene.screenSpaceCameraController.minimumZoomDistance),
	);
	inst.destroy();
}

{
	// a bad token: the factory rejects (ion answers 401)
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	inst.activate();
	cesium.pendingLoads[0].reject(new Error('Request has failed. Status Code: 401'));
	await tick();
	const st = inst.coverageStats();
	check('failed load: globe shown at full detail, the chip names the failure', viewer.scene.globe.show === true && viewer.scene.globe.maximumScreenSpaceError === 2 && st.error?.includes('401') && st.hint.includes('the globe carries the view') && st.count === 0, st.hint);
	check('failed load: the camera keeps its own zoom floor', viewer.scene.screenSpaceCameraController.minimumZoomDistance === 1);
	inst.destroy();
}

{
	// tiles stream but never cover the view: early tile failures give up
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	inst.activate();
	const ts = makeTileset();
	cesium.pendingLoads[0].resolve(ts);
	await tick();
	for (let i = 0; i < 3; i++) ts.tileFailed.raise({ message: 'HTTP 403' });
	const st = inst.coverageStats();
	check('failing tiles: tileset dropped, globe shown', ts.destroyed && viewer.prims.length === 0 && viewer.scene.globe.show === true && st.error?.includes('403'), st.hint);
	inst.destroy();
}

// the 12 s watchdog (setTimeout captured and fired by hand): slow is not failed
async function withCapturedTimers(run) {
	const realSetTimeout = globalThis.setTimeout;
	const timers = [];
	globalThis.setTimeout = (fn, ms) => {
		timers.push({ fn, ms });
		return timers.length;
	};
	try {
		return await run(timers, realSetTimeout);
	} finally {
		globalThis.setTimeout = realSetTimeout;
	}
}

{
	// tiles streaming but the first view not covered in 12 s: kept, globe shown,
	// the chip says why — and the globe hides once they do arrive (no re-toggle)
	const cesium = makeCesium();
	const { viewer, inst, c } = await keyed(cesium);
	const timers = await withCapturedTimers(async (t) => {
		inst.activate();
		return t;
	});
	const ts = makeTileset();
	cesium.pendingLoads[0].resolve(ts);
	await tick();
	const watchdog = timers.find((t) => t.ms === M.PHOTOREAL_READY_TIMEOUT_MS);
	check('watchdog armed for 12 s', !!watchdog && M.PHOTOREAL_READY_TIMEOUT_MS === 12000);
	const before = c.notified();
	watchdog?.fn();
	let st = inst.coverageStats();
	check('slow: the streaming tileset is kept', !ts.destroyed && viewer.prims.length === 1 && st.count === 1);
	check('slow: globe shown, still loading, not an error', viewer.scene.globe.show === true && st.loading === true && st.error === null && st.via === 'ion' && st.detail.slow === 1, st.hint);
	check('slow: the chip says why the city is still flat', st.hint.includes('12 s') && st.hint.includes('the globe carries the view') && c.notified() > before, st.hint);
	ts.initialTilesLoaded.raise();
	st = inst.coverageStats();
	check('slow: late tiles still land — globe under them, ready', viewer.scene.globe.maximumScreenSpaceError === M.GLOBE_UNDER_TILES_SSE && st.count === 1 && st.via === 'ion' && st.loading === false && st.detail.slow === 0, st.hint);
	inst.deactivate();
	check('slow: deactivate restores the globe detail', viewer.scene.globe.maximumScreenSpaceError === 2 && ts.destroyed);
	inst.destroy();
}

{
	// ion never answers within 12 s: still loading (nothing to drop), globe shown;
	// a load that lands after the layer was switched off is discarded
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	const timers = await withCapturedTimers(async (t) => {
		inst.activate();
		return t;
	});
	timers.find((t) => t.ms === M.PHOTOREAL_READY_TIMEOUT_MS)?.fn();
	let st = inst.coverageStats();
	check('slow ion: loading, no tileset, globe shown', st.loading === true && st.count === 0 && viewer.scene.globe.show === true && st.detail.slow === 1, st.hint);
	inst.deactivate();
	const ts = makeTileset();
	cesium.pendingLoads[0].resolve(ts);
	await tick();
	st = inst.coverageStats();
	check('slow ion: switched off, the late tileset is discarded', ts.destroyed && viewer.prims.length === 0 && viewer.scene.globe.show === true && st.via === 'off');
	inst.destroy();
}

{
	// the watchdog after the tiles loaded (or after a deactivate) changes nothing
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	const timers = await withCapturedTimers(async (t) => {
		inst.activate();
		return t;
	});
	const ts = makeTileset();
	cesium.pendingLoads[0].resolve(ts);
	await tick();
	ts.initialTilesLoaded.raise();
	timers.find((t) => t.ms === M.PHOTOREAL_READY_TIMEOUT_MS)?.fn();
	const st = inst.coverageStats();
	check('a stale watchdog never marks a ready layer slow', st.loading === false && st.detail.slow === 0 && st.detail.globeUnderTiles === 1, st.hint);
	inst.destroy();
}

{
	// a transient failure (no answer / a refused TLS handshake): retried on the
	// schedule through the generic factory (the Google one caches its failure)
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	const timers = await withCapturedTimers(async (t) => {
		inst.activate();
		return t;
	});
	check('retry: the schedule outlives a 60 s DNS cache', M.PHOTOREAL_RETRY_MS.length >= 3 && M.PHOTOREAL_RETRY_MS.at(-1) > 60_000);
	check('retry: transient = no status / 5xx / 429; refusals are final', M.transientLoadFailure(new Error('Request has failed.')) && M.transientLoadFailure(new Error('Request has failed. Status Code: 503')) && M.transientLoadFailure({ statusCode: 429 }) && !M.transientLoadFailure(new Error('Request has failed. Status Code: 401')) && !M.transientLoadFailure({ statusCode: 404 }));
	const retried = await withCapturedTimers(async (t) => {
		cesium.pendingLoads[0].reject(new Error('Request has failed.'));
		await flush();
		return t;
	});
	let st = inst.coverageStats();
	const retry = retried.find((t) => t.ms === M.PHOTOREAL_RETRY_MS[0]);
	check('retry: a dropped ion answer schedules a retry — loading, not failed, the chip says why', !!retry && st.loading === true && st.via === 'ion' && st.detail.retries === 1 && /retry 1\//.test(st.hint) && st.error === null, st.hint);
	retry.fn();
	check('retry: the retry asks ion afresh through the generic factory', cesium.calls.ionTileset.length === 1 && cesium.calls.ionTileset[0].id === 2275207 && cesium.calls.google.length === 1);
	const ts = makeTileset();
	cesium.pendingLoads[1].resolve(ts);
	await tick();
	ts.initialTilesLoaded.raise();
	st = inst.coverageStats();
	check('retry: the retried tiles land — ready, globe under them', st.via === 'ion' && st.count === 1 && st.detail.globeUnderTiles === 1 && viewer.prims.length === 1, st.hint);
	inst.destroy();
	void timers;
}

{
	// a refusal is final; a transient failure that never clears fails after the schedule
	const cesium = makeCesium();
	const { inst } = await keyed(cesium);
	await withCapturedTimers(async (t) => {
		inst.activate();
		return t;
	});
	await withCapturedTimers(async (t) => {
		cesium.pendingLoads[0].reject(new Error('Request has failed.'));
		await flush();
		for (let i = 1; i <= M.PHOTOREAL_RETRY_MS.length; i++) {
			t.find((x) => x.ms === M.PHOTOREAL_RETRY_MS[i - 1])?.fn();
			cesium.pendingLoads[i]?.reject(new Error('Request has failed.'));
			await flush();
		}
		return t;
	});
	const st = inst.coverageStats();
	check('retry: exhausted → failed, the chip counts the attempts', st.via === 'failed' && /after \d+ attempts/.test(st.error ?? '') && cesium.pendingLoads.length === M.PHOTOREAL_RETRY_MS.length + 1, st.error ?? st.hint);
	inst.destroy();
}

{
	// a build without the Google factory: the generic ion factory, asset 2275207
	const cesium = makeCesium();
	delete cesium.createGooglePhotorealistic3DTileset;
	const { inst } = await keyed(cesium);
	inst.activate();
	const call = cesium.calls.ionTileset[0];
	check('fallback factory: Cesium3DTileset.fromIonAssetId(2275207)', call?.id === 2275207 && call?.opts.showCreditsOnScreen === true && call?.opts.enableCollision === false);
	inst.destroy();
}

{
	// switched off before the load lands: the late tileset is discarded
	const cesium = makeCesium();
	const { viewer, inst } = await keyed(cesium);
	inst.activate();
	inst.deactivate();
	const ts = makeTileset();
	cesium.pendingLoads[0].resolve(ts);
	await tick();
	check('stale load discarded', ts.destroyed && viewer.prims.length === 0 && viewer.scene.globe.show === true);
	inst.destroy();
}

// ── 7. honest mode: what the canvas shows, as the snapshot and the chip say ──
{
	const ready = { via: 'ion', detail: { initialTilesLoaded: 1 } };
	const loading = { via: 'ion', detail: { initialTilesLoaded: 0 } };
	const mode = (offline, basemapId, photoreal) => M.imageryModeOf({ offline, basemapId, photoreal });
	check('mode: tiles covering the view → ion-photoreal', mode(false, 'ion-photoreal', ready) === 'ion-photoreal');
	check('mode: the 3D city stays ion-photoreal when the imagery under it stumbles', mode(true, 'ion-photoreal', ready) === 'ion-photoreal');
	check('mode: offline imagery without the tiles → offline-schematic', mode(true, 'ion-photoreal', loading) === 'offline-schematic' && mode(true, 'osm-keyless', null) === 'offline-schematic');
	check('mode: ion before the tiles → ion-photoreal; keyless → keyless-osm', mode(false, 'ion-photoreal', loading) === 'ion-photoreal' && mode(false, 'osm-keyless', null) === 'keyless-osm');
	check('mode: the 3D city under a keyless imagery fallback still reads ion-photoreal (the note says why)', mode(false, 'osm-keyless', ready) === 'ion-photoreal');
	check('mode: a keyless canvas without the tiles stays keyless', mode(false, 'osm-keyless', { via: 'unconfigured', detail: { initialTilesLoaded: 0 } }) === 'keyless-osm');
	const chip = (offline, basemapId, photoreal, fallbackNote = null) => M.imageryChip({ offline, basemapId, photoreal, fallbackNote });
	check('chip: PHOTOREAL 3D · GOOGLE once the tiles cover the view', M.t('en', chip(false, 'ion-photoreal', ready)) === 'PHOTOREAL 3D · GOOGLE');
	check('chip: …and never OFFLINE SCHEMATIC while they do', chip(true, 'ion-photoreal', ready) === 'ge_imagery_photoreal');
	check('chip: loading tiles say so; ion without the layer says ION AERIAL', chip(false, 'ion-photoreal', loading) === 'ge_imagery_photoreal_loading' && chip(false, 'ion-photoreal', null) === 'ge_imagery_ion');
	check(
		'chip: the keyless ladder is unchanged (OSM · DARK, the fallback, offline)',
		M.t('en', chip(false, 'osm-keyless', null)) === 'OSM · DARK' && M.t('en', chip(false, 'osm-keyless', null, 'ion upgrade unavailable')) === 'OSM · DARK · FALLBACK' && M.t('en', chip(true, 'osm-keyless', null)) === 'OFFLINE SCHEMATIC MODE',
	);
	const keys = ['ge_imagery_photoreal', 'ge_imagery_photoreal_loading', 'ge_imagery_ion', 'ge_imagery_osm', 'ge_imagery_osm_fallback', 'ge_imagery_offline'];
	const missing = [];
	for (const lang of ['en', 'hi', 'gu']) {
		for (const k of keys) {
			const v = M.t(lang, k);
			if (v === k || (lang !== 'en' && v === M.t('en', k) && !/^OSM · /.test(v))) missing.push(`${lang}:${k}`);
		}
	}
	check('chip: every key reads in en, hi and gu (operational codes stay Latin)', missing.length === 0, missing.join(', ') || `${keys.length} keys × 3`);
	check('chip: GOOGLE named in every language (attribution)', ['en', 'hi', 'gu'].every((l) => M.t(l, 'ge_imagery_photoreal').includes('GOOGLE')));
}

if (failures > 0) {
	console.error(`basemap.unit: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   basemap.unit — all assertions passed');
