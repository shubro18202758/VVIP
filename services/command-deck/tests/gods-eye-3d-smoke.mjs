/**
 * God's-eye 3D smoke — the photoreal city with every overlay ON it (WP-22).
 *
 * Runs against an ion-KEYED deck origin (VEER_CESIUM_ION_TOKEN on the deck;
 * the token is never printed: the mode answer is read only for its `mode`
 * and asset id), under SOFTWARE WebGL (SwiftShader — the gods-eye-smoke
 * flags), 1920×1080.
 *
 * Operator gate: the judged deck runs with the PIN gate ON, and there the
 * deck hands the ion upgrade ONLY to a signed-in operator (an anonymous
 * mode read is keyless by design). Like gods-eye-smoke, this smoke signs in
 * ONCE through tests/lib/operator-session.mjs — the PIN is read at run time
 * (VEER_OPERATOR_PIN, else VEER_OPERATOR_PIN_FILE, else veer.env) and never
 * logged — reads the mode endpoint, the active events and the interception
 * plan WITH the session, sets the session cookie on every page before its
 * first navigation (fresh browser contexts and the City Command home
 * included) and signs out on exit. Gate off (a private preview): nothing to
 * sign in to, the same checks run.
 *
 *   1. the mode endpoint answers ion-photoreal (else: not a keyed deck —
 *      this smoke needs one; gods-eye-smoke covers the keyless canvas);
 *   2. /gods-eye on the old-city judge view reaches phase 'ready', the
 *      photoreal-3d layer reports its tiles covering the view (Google
 *      Photorealistic 3D Tiles, ion asset 2275207 — allow ~2 minutes under
 *      SwiftShader) with the globe kept beneath as the overlays' ground, and
 *      the snapshot's imageryMode is 'ion-photoreal' — never the offline
 *      schematic;
 *   3. ZERO Content-Security-Policy violations (console + the
 *      securitypolicyviolation event) and zero page errors — on /gods-eye,
 *      '/' and /ops;
 *   4. every overlay layer with entities on the canvas reports them DRAPED
 *      (ground polylines / fills) or CLAMPED (markers on the terrain, depth
 *      test off) and floating 0 (layerStats.<id>.{draped,clamped,floating},
 *      the drape audit reading the entities back); the flow network draped
 *      segment for segment;
 *   5. the draped flow network is VISIBLE on the 3D tiles: canvas pixels in
 *      the congestion-ramp colours (read from the live design tokens) with
 *      the flow layer on outnumber the same view with it off;
 *   6. no stall: no frame over 3 s in a 10 s window on the judge view;
 *   7. the data credits (Google, Cesium ion, '© OpenStreetMap contributors'
 *      spelled out) are on screen and not covered by a panel on the '/'
 *      wall — the WHOLE bar, its right end included: it wraps before the
 *      cockpit's tool dock instead of running under it (asserted with
 *      GODS_EYE_3D_MERGED=1, reported otherwise);
 *   8. the pursuit overlay: drawn (entities > 0) whenever the oracle holds a
 *      live interception plan with a sighting (read through the deck's
 *      proxy — a GET), else reported;
 *   9. screenshots: tests/artifacts/gods-eye-3d-smoke{,-root,-home}.png (or
 *      the directory in GODS_EYE_3D_OUT).
 *
 * MERGED canvas (GODS_EYE_3D_MERGED=1, or the older
 * GODS_EYE_3D_CITY_COMMAND_CREDITS=assert) — once WP-22's GodsEyeCanvas,
 * enforcement and replay changes are merged, these are asserted instead of
 * reported: the City Command credits clear of the fusion rail; the chip
 * reads PHOTOREAL 3D · GOOGLE; the e-challan pins and the pursuit overlay
 * publish their drape audit (floating 0); a FRESH /gods-eye (no share hash)
 * lands on the judge-grade home — pitch −35…−45°, camera 1.5–3 km up.
 *
 * SOAK (GODS_EYE_3D_SOAK_MIN=10): after the checks, the '/' wall orbits on
 * the keyed city for that many minutes, sampled every 20 s (JS heap, tile
 * memory, frames, camera altitude, entities, page errors): no page error,
 * no frame over 3 s, the orbit's altitude constant (±50 m), the tiles
 * within their cache bound and the heap without a runaway (the last third's
 * median within 1.5× + 40 MB of the first third's).
 *
 * Environment fault, said and retried: from the demo box one of
 * api.cesium.com's CDN addresses refuses every TLS handshake and a browser
 * context keeps its pick, so a load whose ion requests fail at the TLS layer
 * (and whose tiles therefore never arrive) is retried in a fresh browser
 * context — own DNS cache and sockets — at most twice per page.
 *
 * Usage:   node tests/gods-eye-3d-smoke.mjs [origin]   # default http://127.0.0.1:5223
 *          VEER_OPERATOR_PIN_FILE=<0600 file> node tests/gods-eye-3d-smoke.mjs <gate-on origin>
 *          (a sandbox deck's throwaway PIN; without it the helper reads veer.env)
 * Exits 0 with `ok <name> — <detail>` lines; exit 1 on any failure.
 */

import { createRequire } from 'node:module';
import { existsSync, mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { deckFetch, puppeteerCookies, signIn, signOut } from './lib/operator-session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const OUT = process.env['GODS_EYE_3D_OUT'] ?? path.join(HERE, 'artifacts');

const PUPPETEER_CANDIDATES = [
	process.env['PUPPETEER_FROM'],
	path.resolve(DECK, '../../frontend/package.json'),
	'/home/sandy050405/argus/veer/VVIP/frontend/package.json',
	'/home/sandy050405/Desktop/Veer/VVIP/frontend/package.json',
].filter(Boolean);
let puppeteer = null;
for (const from of PUPPETEER_CANDIDATES) {
	try {
		const req = createRequire(from);
		puppeteer = req('puppeteer').default ?? req('puppeteer');
		break;
	} catch {
		// next candidate
	}
}
if (!puppeteer) throw new Error(`puppeteer not resolvable from ${PUPPETEER_CANDIDATES.join(', ')}`);

const CHROME_CANDIDATES = [
	process.env['CHROME_PATH'],
	path.join(os.homedir(), '.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome'),
	path.join(os.homedir(), '.cache/puppeteer/chrome/linux_arm-146.0.7680.76/chrome-linux-arm64/chrome'),
].filter(Boolean);
const executablePath = CHROME_CANDIDATES.find(existsSync);
if (!executablePath) throw new Error('no Chrome binary found (set CHROME_PATH)');

const ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
	'--ignore-gpu-blocklist',
	'--window-size=1920,1080',
	// performance.memory un-bucketed and live (the soak samples the JS heap)
	'--enable-precise-memory-info',
];

/** The old-city judge view: SSW of Teen Darwaza at ~2 km, looking NNE over the Sabarmati. */
const JUDGE_HASH = '#v=2&lat=23.00366&lon=72.57482&alt=2057&heading=20&pitch=-40&style=raw';
/** Overlay layers that must stand on the city (photoreal-3d is the city itself). */
const OVERLAYS = ['events', 'flow', 'traces', 'enforcement', 'signals', 'units', 'dispatch', 'convoy', 'cameras', 'anomalies'];
const TILE_WAIT_MS = Number(process.env['GODS_EYE_3D_TILE_WAIT_MS'] ?? 150_000);
/** The WP-22 canvas / enforcement / replay changes are merged: their checks are asserted. */
const MERGED = process.env['GODS_EYE_3D_MERGED'] === '1' || process.env['GODS_EYE_3D_CITY_COMMAND_CREDITS'] === 'assert';
/** The judge-grade City Command home on the photoreal city (task 3). */
const HOME_3D = { minPitch: -45.5, maxPitch: -34.5, minAlt: 1500, maxAlt: 3000 };
const CHIP_PHOTOREAL = 'PHOTOREAL 3D · GOOGLE';
const SOAK_MIN = Number(process.env['GODS_EYE_3D_SOAK_MIN'] ?? 0);
/** Tile cache bound of layers/photoreal-3d.ts (cacheBytes + maximumCacheOverflowBytes), MB. */
const TILE_CACHE_MB = 768;

/** Where the canvas's data credits are, and whether a panel covers them. */
async function creditsOnScreen(page) {
	return page.evaluate(() => {
		const credits = document.querySelector('.ge-canvas .cesium-viewer-bottom');
		if (!credits) return { present: false, visible: false, google: false, cesium: false };
		const r = credits.getBoundingClientRect();
		let text = credits.textContent ?? '';
		text += ` ${Array.from(credits.querySelectorAll('img, a')).map((el) => el.getAttribute('title') ?? el.getAttribute('alt') ?? el.getAttribute('href') ?? '').join(' ')}`;
		const y = r.top + r.height / 2;
		const mine = (x) => {
			const top = document.elementFromPoint(x, y);
			return !!top && credits.contains(top);
		};
		const inView = r.width > 0 && r.height > 0 && r.bottom <= innerHeight;
		return {
			present: true,
			visible: inView && mine(r.left + Math.min(40, r.width / 2)),
			// the whole bar, its right end included (the data providers' names), not under a panel
			clear: inView && mine(r.left + Math.min(40, r.width / 2)) && mine(r.right - Math.min(40, r.width / 2)),
			google: /google/i.test(text),
			cesium: /cesium/i.test(text),
			// the OSM attribution spelled out on the plate (OSMF guidelines), not only '© OSM'
			osm: /openstreetmap contributors/i.test(text),
			at: `${Math.round(r.left)},${Math.round(r.top)}`,
			w: Math.round(r.width),
		};
	});
}

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const redact = (text) => String(text).replace(/([?&](?:access_token|key)=)[^&\s"']+/g, '$1…');

const origin = (process.argv[2] ?? 'http://127.0.0.1:5223').replace(/\/+$/, '');
/** The operator session (signed in once; `gate:false` on a gate-off preview). */
let session = null;

/**
 * A page that records CSP violations (console + DOM event), page errors and
 * ion TLS refusals — carrying the operator session from its first navigation
 * (every caller navigates after this returns; a fresh browser context has
 * no cookie of its own).
 */
async function openPage(context) {
	const page = await context.newPage();
	await page.setViewport({ width: 1920, height: 1080 });
	if (session?.gate) await page.setCookie(...puppeteerCookies(session));
	const log = { csp: [], errors: [], ionRefused: 0 };
	page.on('console', (m) => {
		const text = m.text();
		if (/Content Security Policy/i.test(text)) log.csp.push(redact(text).slice(0, 180));
	});
	page.on('pageerror', (e) => log.errors.push(redact(e.message).slice(0, 180)));
	page.on('requestfailed', (req) => {
		if (/^https:\/\/api\.cesium\.com\//.test(req.url()) && /SSL|CONNECTION|TIMED_OUT/i.test(req.failure()?.errorText ?? '')) log.ionRefused++;
	});
	await page.evaluateOnNewDocument(() => {
		window.__CSP_EVENTS__ = [];
		document.addEventListener('securitypolicyviolation', (e) => {
			window.__CSP_EVENTS__.push(`${e.violatedDirective} ${String(e.blockedURI).replace(/([?&](?:access_token|key)=)[^&]+/g, '$1…').slice(0, 120)}`);
		});
	});
	return { page, log };
}

async function waitReady(page, timeoutMs = 90_000) {
	return page
		.waitForFunction(() => window.__GODS_EYE__?.phase && window.__GODS_EYE__.phase !== 'loading', { timeout: timeoutMs })
		.then(() => page.evaluate(() => window.__GODS_EYE__.phase))
		.catch(() => null);
}

/** Wait until the photoreal tiles cover the view (or the layer gives up, or ion refused this context). */
async function waitTiles(page, timeoutMs, log = null) {
	const deadline = Date.now() + timeoutMs;
	const start = Date.now();
	let last = null;
	while (Date.now() < deadline) {
		last = await page.evaluate(() => window.__GODS_EYE__?.layerStats?.['photoreal-3d'] ?? null).catch(() => null);
		if (last && (last.initialTilesLoaded === 1 || last.via === 'failed')) return last;
		if (log?.ionRefused && Date.now() - start > 30_000) return last; // the environment refused this context
		await sleep(2000);
	}
	return last;
}

/**
 * Open `route` until its photoreal tiles land: a context whose ion requests
 * the CDN refused (environment) is retried in a fresh one, at most twice.
 */
async function openWithTiles(browser, route) {
	let opened = null;
	for (let attempt = 1; attempt <= 3; attempt++) {
		const context = attempt === 1 ? browser.defaultBrowserContext() : await browser.createBrowserContext();
		const { page, log } = await openPage(context);
		await page.goto(`${origin}${route}`, { waitUntil: 'domcontentloaded', timeout: 90_000 });
		const phase = await waitReady(page);
		const tiles = phase === 'ready' ? await waitTiles(page, TILE_WAIT_MS, log) : null;
		opened = { page, log, phase, tiles, attempt };
		if (tiles?.initialTilesLoaded === 1 || !log.ionRefused || attempt === 3) break;
		console.log(`note ${route}: api.cesium.com refused TLS in this browser context (${log.ionRefused}×, environment) — retrying in a fresh context (${attempt}/3)`);
		await page.close();
	}
	return opened;
}

/** Canvas pixels within `tol` (RGB distance) of the congestion-ramp token colours. */
async function rampPixels(page, tol = 34) {
	return page.evaluate((tolerance) => {
		const css = getComputedStyle(document.documentElement);
		const probe = document.createElement('canvas').getContext('2d');
		const rgb = (value) => {
			probe.fillStyle = '#000';
			probe.fillStyle = value;
			const hex = probe.fillStyle;
			if (!/^#[0-9a-f]{6}$/i.test(hex)) return null;
			return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
		};
		const ramp = ['--v-band-green', '--v-flow-light', '--v-band-amber', '--v-act', '--v-band-red']
			.map((t) => css.getPropertyValue(t).trim())
			.filter(Boolean)
			.map(rgb)
			.filter(Boolean);
		const c = document.querySelector('.ge-canvas canvas');
		if (!c || !ramp.length) return { ramp: ramp.length, count: -1 };
		const w = 960;
		const h = 540;
		const tmp = document.createElement('canvas');
		tmp.width = w;
		tmp.height = h;
		const g = tmp.getContext('2d');
		g.drawImage(c, 0, 0, w, h);
		const d = g.getImageData(0, 0, w, h).data;
		let count = 0;
		for (let i = 0; i < d.length; i += 4) {
			for (const [r, gg, b] of ramp) {
				if (Math.abs(d[i] - r) + Math.abs(d[i + 1] - gg) + Math.abs(d[i + 2] - b) <= tolerance) {
					count++;
					break;
				}
			}
		}
		return { ramp: ramp.length, count };
	}, tol);
}

/**
 * A live interception plan with a sighting — the pursuit overlay must then
 * draw. Read through the deck's proxy like the canvas does (GETs only), with
 * the operator session: under the gate an anonymous read is refused.
 */
async function livePursuitPlan() {
	const get = (route, ms) =>
		deckFetch(session, route, { signal: AbortSignal.timeout(ms) })
			.then((r) => (r.ok ? r.json() : null))
			.catch(() => null);
	const body = await get('/api/v1/events/active', 8000);
	const rows = Array.isArray(body?.events) ? body.events : [];
	for (const row of rows) {
		const id = String(row.event_id ?? row.id ?? '');
		if (row.event_kind !== 'vehicle_pursuit' || !/^[\w-]+$/.test(id)) continue;
		const plan = await get(`/api/v1/events/${id}/interception-plan?max_cost_sec=600`, 12_000);
		if (plan && typeof plan.vehicle_id === 'string' && Array.isArray(plan.latest_sighting?.position)) {
			return { eventId: id, chokes: Array.isArray(plan.interception_junctions) ? plan.interception_junctions.length : 0 };
		}
	}
	return null;
}

/** The camera once it stopped moving (two reads `gapMs` apart within 5 m / 0.2°), or the last read. */
async function settledCamera(page, timeoutMs, gapMs = 1500) {
	const deadline = Date.now() + timeoutMs;
	let last = await page.evaluate(() => window.__GODS_EYE__?.camera ?? null).catch(() => null);
	while (Date.now() < deadline) {
		await sleep(gapMs);
		const now = await page.evaluate(() => window.__GODS_EYE__?.camera ?? null).catch(() => null);
		if (last && now && Math.abs(now.alt - last.alt) < 5 && Math.abs(now.pitch - last.pitch) < 0.2 && Math.abs(now.heading - last.heading) < 0.2) return now;
		last = now;
	}
	return last;
}

/** SOAK: the '/' wall orbiting on the keyed city for `minutes`, sampled every 20 s. */
async function soak(minutes) {
	const { page, log, phase, tiles } = await openWithTiles(browser, '/');
	ok(`soak: '/' phase ready on the photoreal city`, phase === 'ready' && tiles?.initialTilesLoaded === 1, `phase=${phase} tiles=${tiles?.initialTilesLoaded}`);
	const samples = [];
	const t0 = Date.now();
	while (Date.now() - t0 < minutes * 60_000) {
		await sleep(20_000);
		const s = await page
			.evaluate(
				() =>
					new Promise((resolve) => {
						const d = [];
						let last = performance.now();
						const start = last;
						function step(now) {
							d.push(now - last);
							last = now;
							if (now - start < 2000) requestAnimationFrame(step);
							else {
								const sorted = [...d].sort((a, b) => a - b);
								const g = window.__GODS_EYE__;
								resolve({
									heapMB: Math.round((performance.memory?.usedJSHeapSize ?? 0) / 1e6),
									tileMB: g?.layerStats?.['photoreal-3d']?.memMB ?? null,
									alt: Math.round(g?.camera?.alt ?? NaN),
									orbiting: !!g?.orbiting,
									entities: g?.entityCount ?? null,
									median: Math.round(sorted[Math.floor(sorted.length / 2)] ?? NaN),
									max: Math.round(sorted[sorted.length - 1] ?? NaN),
								});
							}
						}
						requestAnimationFrame(step);
					}),
			)
			.catch(() => null);
		if (!s) continue;
		samples.push({ min: +((Date.now() - t0) / 60_000).toFixed(1), ...s });
		console.log(`soak ${JSON.stringify(samples[samples.length - 1])}`);
	}
	const third = Math.max(1, Math.floor(samples.length / 3));
	const med = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] ?? NaN;
	const heapFirst = med(samples.slice(0, third).map((s) => s.heapMB));
	const heapLast = med(samples.slice(-third).map((s) => s.heapMB));
	const orbit = samples.filter((s) => s.orbiting).map((s) => s.alt);
	const tileMax = Math.max(0, ...samples.map((s) => s.tileMB ?? 0));
	const frameMax = Math.max(0, ...samples.map((s) => s.max));
	// a sample every ~22 s (20 s apart + a 2 s frame window): at least one per 30 s, and the window covered
	ok(
		`soak: sampled across the whole ${minutes} min`,
		samples.length >= Math.floor((minutes * 60) / 30) && (samples.at(-1)?.min ?? 0) >= minutes - 0.5,
		`${samples.length} samples · last at ${samples.at(-1)?.min ?? '—'} min`,
	);
	ok('soak: zero page errors', log.errors.length === 0, log.errors.slice(0, 2).join(' | ') || 'none');
	ok('soak: no frame over 3 s', frameMax < 3000, `max frame ${frameMax} ms · medians ${samples.map((s) => s.median).join('/')} ms`);
	ok('soak: the orbit holds its altitude (±50 m)', orbit.length > 0 && Math.max(...orbit) - Math.min(...orbit) <= 100, `orbiting in ${orbit.length}/${samples.length} samples · alt ${Math.min(...orbit)}–${Math.max(...orbit)} m`);
	ok('soak: tiles within their cache bound', tileMax <= TILE_CACHE_MB, `tile memory peak ${tileMax} MB (bound ${TILE_CACHE_MB} MB)`);
	ok('soak: no heap runaway', heapLast <= heapFirst * 1.5 + 40, `JS heap median ${heapFirst} MB (first third) → ${heapLast} MB (last third)`);
	await page.close();
}

const browser = await puppeteer.launch({ executablePath, headless: 'new', args: ARGS });
try {
	// ── 0. the operator session (gate on: the ion upgrade is an operator's) ──
	session = await signIn(origin, 'Gods-eye 3D smoke (automated check)');
	ok('operator gate', true, session.gate ? 'on — signed in' : 'off — a private preview, nothing to sign in to');

	// ── 1. a keyed deck ─────────────────────────────────────────────────────
	// Read WITH the session: under the gate an anonymous read is keyless by design.
	const modeDoc = await deckFetch(session, '/gods-eye/mode', { signal: AbortSignal.timeout(5000) })
		.then((r) => r.json())
		.catch(() => ({}));
	ok('mode endpoint answers ion-photoreal (a keyed deck)', modeDoc.mode === 'ion-photoreal', `mode=${modeDoc.mode ?? 'unreachable'}${modeDoc.mode === 'keyless-osm' ? ' — run against the VEER_CESIUM_ION_TOKEN preview' : ''}`);
	ok('mode endpoint names the photoreal asset', modeDoc.tiles3d_asset_id === 2275207, `tiles3d_asset_id=${modeDoc.tiles3d_asset_id}`);
	if (modeDoc.mode !== 'ion-photoreal') throw new Error('not an ion-keyed deck');

	// ── 2. the judge view on the photoreal city ─────────────────────────────
	const { page, log, phase, tiles: p3d, attempt } = await openWithTiles(browser, `/gods-eye${JUDGE_HASH}`);
	ok('canvas phase ready', phase === 'ready', `phase=${phase}${attempt > 1 ? ` (attempt ${attempt}: ion TLS refusals retried)` : ''}`);
	ok(
		'photoreal-3d: Google Photorealistic 3D Tiles cover the view',
		p3d?.initialTilesLoaded === 1 && p3d.via === 'ion' && p3d.count === 1,
		JSON.stringify(p3d),
	);
	ok('photoreal-3d: the globe kept beneath the tiles (the overlays\' ground)', p3d?.globeUnderTiles === 1, `globeUnderTiles=${p3d?.globeUnderTiles}`);
	await sleep(8000); // the layers' draws settle on the landed view
	const snap = await page.evaluate(() => {
		const g = window.__GODS_EYE__;
		return {
			imageryMode: g.imageryMode,
			imageryNote: g.imageryNote,
			offline: g.offlineDetected,
			layerStats: g.layerStats,
			pursuit: g.pursuitEntities,
			entities: g.entityCount,
			chips: Array.from(document.querySelectorAll('.ge-hud .chip.mode')).map((c) => c.textContent.trim()),
		};
	});
	ok('imageryMode ion-photoreal — never the offline schematic', snap.imageryMode === 'ion-photoreal' && snap.offline === false, `imageryMode=${snap.imageryMode} offline=${snap.offline} note=${snap.imageryNote ?? '—'}`);
	ok('no OFFLINE SCHEMATIC chip', !snap.chips.some((c) => /OFFLINE/i.test(c)), snap.chips.join(' | ') || 'no mode chip');
	const chipSays = snap.chips.includes(CHIP_PHOTOREAL);
	if (MERGED) ok(`chip reads '${CHIP_PHOTOREAL}'`, chipSays, snap.chips.join(' | ') || 'no mode chip');
	else console.log(`note chip: ${snap.chips.join(' | ') || 'no mode chip'} ('${CHIP_PHOTOREAL}' asserted with GODS_EYE_3D_MERGED=1)`);

	// ── 4. every overlay draped or clamped (+ the pursuit overlay) ─────────
	const audited = [];
	for (const id of [...OVERLAYS, 'pursuit']) {
		const st = snap.layerStats?.[id];
		if (id === 'pursuit' && !st) {
			// the overlay publishes its audit through the merged canvas; before that only its count
			if (MERGED && snap.pursuit > 0) ok('pursuit: drape audit published', false, `${snap.pursuit} entities, no layerStats.pursuit`);
			else console.log(`note pursuit: ${snap.pursuit} entities${snap.pursuit > 0 ? ', drape audit published by the merged canvas' : ''}`);
			continue;
		}
		if (!st) {
			ok(`${id}: layer on the canvas`, false, 'no layerStats');
			continue;
		}
		if (!('floating' in st)) {
			// nothing drawn (no audit to publish), or a layer that publishes none
			// (enforcement before the WP-22 change to it is merged): reported —
			// asserted once it is merged
			if (MERGED && st.count > 0) ok(`${id}: drape audit published`, false, `${st.count} on the canvas, no draped/clamped/floating counts`);
			else
				console.log(
					st.count > 0
						? `note ${id}: ${st.count} on the canvas, no drape audit published by this build`
						: `ok   ${id}: nothing to drape right now (count 0)`,
				);
			continue;
		}
		const shown = (st.draped ?? 0) + (st.clamped ?? 0) + (st.floating ?? 0);
		if (!shown) {
			console.log(`ok   ${id}: nothing to drape right now (count ${st.count})`);
			continue;
		}
		audited.push(id);
		ok(`${id}: every entity draped or clamped (floating 0)`, st.floating === 0, `draped ${st.draped} · clamped ${st.clamped} · floating ${st.floating}`);
	}
	const flow = snap.layerStats?.flow;
	ok('flow: the network draped segment for segment', !!flow && flow.segments > 0 && flow.draped === flow.segments, `segments ${flow?.segments} · draped ${flow?.draped}`);
	ok('overlays audited on the 3D city', audited.length >= 3, audited.join(', '));
	const plan = await livePursuitPlan();
	if (plan) {
		ok('pursuit overlay drawn on the live interception plan', snap.pursuit > 0, `entities ${snap.pursuit} · plan for event ${plan.eventId} (${plan.chokes} chokes)`);
	} else {
		console.log(`note pursuit: no live interception plan with a sighting right now — entities ${snap.pursuit} (not asserted)`);
	}

	// ── 6. no stall on the judge view ───────────────────────────────────────
	const frames = await page.evaluate(
		() =>
			new Promise((resolve) => {
				const d = [];
				let last = performance.now();
				const t0 = last;
				function step(now) {
					d.push(now - last);
					last = now;
					if (now - t0 < 10_000) requestAnimationFrame(step);
					else {
						const s = [...d].sort((a, b) => a - b);
						resolve({ n: d.length, median: Math.round(s[Math.floor(s.length / 2)]), max: Math.round(s[s.length - 1]) });
					}
				}
				requestAnimationFrame(step);
			}),
	);
	ok('no stall: no frame over 3 s (SwiftShader, 1920×1080)', frames.max < 3000, `${frames.n} frames · median ${frames.median} ms · max ${frames.max} ms`);

	const ccCredits = await creditsOnScreen(page);
	if (MERGED) {
		ok('City Command: Google + Cesium ion credits on screen, clear of the fusion rail', ccCredits.visible && ccCredits.clear && ccCredits.google && ccCredits.cesium, JSON.stringify(ccCredits));
		ok("City Command: '© OpenStreetMap contributors' on the credit plate (OSM-derived roads on the 3D city)", ccCredits.osm === true, JSON.stringify(ccCredits));
	} else {
		console.log(`note City Command credits: ${JSON.stringify(ccCredits)} (asserted with GODS_EYE_3D_MERGED=1)`);
	}

	mkdirSync(OUT, { recursive: true });
	await page.screenshot({ path: path.join(OUT, 'gods-eye-3d-smoke.png') });

	// ── 5. the flow network visible on the tiles ────────────────────────────
	const on = await rampPixels(page);
	await page.evaluate(() => document.querySelector('[data-layer="flow"]')?.click());
	await sleep(6000);
	const off = await rampPixels(page);
	await page.evaluate(() => document.querySelector('[data-layer="flow"]')?.click());
	ok(
		'the draped flow network is visible on the 3D tiles (ramp pixels: flow on ≫ off)',
		on.count > 0 && on.count >= Math.max(400, off.count * 3),
		`ramp colours ${on.ramp} · flow on ${on.count} px · flow off ${off.count} px (960×540 sample)`,
	);
	const cspEvents = await page.evaluate(() => window.__CSP_EVENTS__ ?? []);
	ok('/gods-eye: zero CSP violations', log.csp.length === 0 && cspEvents.length === 0, [...log.csp, ...cspEvents].slice(0, 2).join(' | ') || 'none');
	ok('/gods-eye: zero page errors', log.errors.length === 0, log.errors.slice(0, 2).join(' | ') || 'none');
	await page.close();

	// ── 3 + 7. '/' and /ops: CSP, photoreal, credits on screen ─────────────
	for (const route of ['/', '/ops']) {
		const { page: p, log: l, phase: ph, tiles } = await openWithTiles(browser, route);
		await sleep(5000);
		const s = {
			mode: await p.evaluate(() => window.__GODS_EYE__?.imageryMode),
			credits: await creditsOnScreen(p),
		};
		const ev = await p.evaluate(() => window.__CSP_EVENTS__ ?? []);
		ok(`${route}: phase ready, photoreal tiles in view, imageryMode ion-photoreal`, ph === 'ready' && tiles?.initialTilesLoaded === 1 && s.mode === 'ion-photoreal', `phase=${ph} tiles=${tiles?.initialTilesLoaded} mode=${s.mode}`);
		ok(`${route}: zero CSP violations`, l.csp.length === 0 && ev.length === 0, [...l.csp, ...ev].slice(0, 2).join(' | ') || 'none');
		ok(`${route}: zero page errors`, l.errors.length === 0, l.errors.slice(0, 2).join(' | ') || 'none');
		if (route === '/') {
			ok("'/': Google + Cesium ion credits on screen, not under a panel", s.credits.visible && s.credits.google && s.credits.cesium, JSON.stringify(s.credits));
			// The cockpit caps the bar before its bottom-centre tool dock (it wraps
			// instead) and its left rail ends at its panels: the WHOLE bar — the
			// providers' tail and 'Data attribution' included — must be clear.
			const whole = `${s.credits.w} px wide at ${s.credits.at} · osm ${s.credits.osm}`;
			if (MERGED) {
				ok("'/': the whole credit bar clear of the tool dock (right end too)", s.credits.clear === true, whole);
				ok("'/': '© OpenStreetMap contributors' on the credit plate", s.credits.osm === true, whole);
			} else {
				console.log(`note '/': whole credit bar clear (right end too): ${s.credits.clear} — ${whole} (asserted with GODS_EYE_3D_MERGED=1)`);
			}
			await p.screenshot({ path: path.join(OUT, 'gods-eye-3d-smoke-root.png') });
		}
		await p.close();
	}

	// ── 10. City Command's own home: a fresh /gods-eye, no share hash ──────
	{
		const { page: p, log: l } = await openPage(browser.defaultBrowserContext());
		await p.goto(`${origin}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 90_000 });
		const ph = await waitReady(p);
		// the home lands at once; a late fused picture re-frames it once within 12 s (+ a 1.6 s flight)
		await sleep(15_000);
		const cam = ph === 'ready' ? await settledCamera(p, 20_000) : null;
		const inBand = !!cam && cam.pitch >= HOME_3D.minPitch && cam.pitch <= HOME_3D.maxPitch && cam.alt >= HOME_3D.minAlt && cam.alt <= HOME_3D.maxAlt;
		const detail = cam
			? `pitch ${cam.pitch.toFixed(1)}° · alt ${Math.round(cam.alt)} m · heading ${cam.heading.toFixed(0)}° · at ${cam.lon.toFixed(4)},${cam.lat.toFixed(4)}`
			: `phase=${ph} · no camera`;
		if (MERGED) ok('City Command home: judge-grade oblique (pitch −35…−45°, 1.5–3 km up)', inBand, detail);
		else console.log(`note City Command home: ${detail} (asserted with GODS_EYE_3D_MERGED=1)`);
		await sleep(10_000); // let the tiles under the landed home stream before the picture
		await p.screenshot({ path: path.join(OUT, 'gods-eye-3d-smoke-home.png') });
		ok('City Command home: zero page errors', l.errors.length === 0, l.errors.slice(0, 2).join(' | ') || 'none');
		await p.close();
	}

	if (SOAK_MIN > 0) await soak(SOAK_MIN);
} catch (err) {
	ok('3d smoke ran to the end', false, err instanceof Error ? err.message : String(err));
} finally {
	await signOut(session);
	await browser.close().catch(() => {});
}

if (failures > 0) {
	console.error(`gods-eye-3d-smoke: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   gods-eye-3d-smoke — all assertions passed');
process.exit(0);
