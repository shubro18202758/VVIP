/**
 * showcase_canvas_shots.mjs — evidence capture for the God's Eye Morning
 * flagship (VVIP/scripts/showcase_run.sh phase CANVAS).
 *
 * NOT a test: a camera. It photographs the REAL SvelteKit command-deck
 * (services/command-deck build/, served same-origin so /api/v1 proxies to
 * the live traffic-oracle) under SOFTWARE WebGL — the red-team-validated
 * SwiftShader flags — and proves each shot is non-blank at the PIXEL
 * level:
 *
 *   1. showcase_gods_eye_city.png    /gods-eye 3D city, camera framed on
 *                                    the Rath Yatra old-city belt, all
 *                                    live layers on (events, traces,
 *                                    signals, units, cameras) — taken
 *                                    while the flagship corridor event is
 *                                    ACTIVE in the running stack.
 *   2. showcase_pursuit_trace.png    /gods-eye framed on the stolen
 *                                    vehicle's REAL trace centroid
 *                                    (GET /api/v1/vehicles/{id}/trace
 *                                    through the deck origin), traces
 *                                    layer front and centre.
 *   3. showcase_citizen_advisory.png /citizen advisory rendering the LIVE
 *                                    pursuit event.
 *
 * Non-blank proof: every saved PNG is reloaded into a blank page as an
 * <img> and drawn to a 2D canvas — Chrome itself is the decoder — then
 * distinct-colour counting runs over the pixels (>= PALETTE_MIN distinct
 * colours AND not a fully-transparent frame). The two /gods-eye shots
 * ALSO use the in-live-canvas readback (the gods-eye-smoke.mjs pattern:
 * the viewer is created with preserveDrawingBuffer for exactly this).
 *
 * Usage: node showcase_canvas_shots.mjs <origin> <out-dir> <pursuit-title>
 * Env:   CHROME_PATH (defaults to the puppeteer cache path the e2e and
 *        gods-eye smoke use on this box).
 * Exit:  0 all three shots proven non-blank; 1 any failure (manifest
 *        still written with the per-shot evidence).
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// --city-only: just the 3D city shot (taken mid-chaos while the flagship
// event is active). --rest-only: pursuit trace + citizen advisory (after
// the pursuit chain exists). No flag: all three.
const MODE_FLAG = process.argv[2]?.startsWith('--') ? process.argv[2] : null;
const ARGS = MODE_FLAG ? process.argv.slice(3) : process.argv.slice(2);
const ORIGIN = ARGS[0] || 'http://127.0.0.1:5173'; // the vvip-command-deck container (the ONE deck)
const OUT = ARGS[1] || 'showcase';
const PURSUIT_TITLE = ARGS[2] || 'PURSUIT';
const CHROME = process.env.CHROME_PATH
	|| `${process.env.HOME}/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome`;

// puppeteer-core resolves from the React frontend's node_modules (the same
// tree the e2e harness uses); ESM ignores NODE_PATH, so import by file URL.
const PUPPETEER_ENTRY = path.join(
	HERE, '..', 'frontend', 'node_modules', 'puppeteer-core',
	'lib', 'cjs', 'puppeteer', 'puppeteer-core.js',
);
if (!fs.existsSync(PUPPETEER_ENTRY)) {
	console.error(`puppeteer-core not found at ${PUPPETEER_ENTRY} (run npm ci in frontend/)`);
	process.exit(2);
}
const puppeteer = await import(pathToFileURL(PUPPETEER_ENTRY).href);

// The red-team-validated software-WebGL combination (gods-eye-smoke.mjs).
const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
];

const PALETTE_MIN = 8; // distinct colours proving a rendered frame
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = { chrome: CHROME, origin: ORIGIN, pursuitTitle: PURSUIT_TITLE, shots: {} };
let failures = 0;
function record(shot, name, ok, detail = '') {
	const entry = (results.shots[shot] ||= { checks: {} });
	entry.checks[name] = { ok, detail };
	if (!ok) failures++;
}

/** Build a /gods-eye share hash (share-link.ts v=2 format). */
function godsEyeHash({ lat, lon, alt, heading = 20, pitch = -55, layers }) {
	const p = new URLSearchParams();
	p.set('v', '2');
	p.set('lat', lat.toFixed(6));
	p.set('lon', lon.toFixed(6));
	p.set('alt', alt.toFixed(6));
	p.set('heading', heading.toFixed(6));
	p.set('pitch', pitch.toFixed(6));
	p.set('roll', '0');
	p.set('style', 'raw');
	p.set('layers', layers.join(','));
	return `#${p.toString()}`;
}

/** Poll window.__GODS_EYE__.phase (reload-safe, like the smoke). */
async function waitPhase(page, timeoutMs = 60000) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const phase = await page
			.evaluate(() => window.__GODS_EYE__?.phase ?? null)
			.catch(() => null);
		if (phase && phase !== 'loading') return phase;
		await sleep(400);
	}
	return null;
}

/** Distinct-colour count over a live WebGL canvas (96x60 downsample). */
async function liveCanvasColours(page) {
	return page.evaluate(() => {
		const c = document.querySelector('.ge-canvas canvas');
		if (!c) return -1;
		const tmp = document.createElement('canvas');
		tmp.width = 96;
		tmp.height = 60;
		const ctx = tmp.getContext('2d');
		ctx.drawImage(c, 0, 0, 96, 60);
		const d = ctx.getImageData(0, 0, 96, 60).data;
		const colours = new Set();
		for (let i = 0; i < d.length; i += 4) colours.add(`${d[i]},${d[i + 1]},${d[i + 2]}`);
		return colours.size;
	});
}

/**
 * Pixel-level non-blank proof over a SAVED PNG: reload it into a blank
 * page (Chrome decodes), draw to a 2D canvas, count distinct colours and
 * reject fully-transparent frames.
 */
async function pngNonBlank(browser, pngPath) {
	const b64 = fs.readFileSync(pngPath).toString('base64');
	const page = await browser.newPage();
	try {
		await page.setContent(
			`<!doctype html><body style="margin:0"><img id="i" src="data:image/png;base64,${b64}"></body>`,
			{ waitUntil: 'load', timeout: 30000 },
		);
		await page.evaluate(() => document.getElementById('i').decode?.() ?? null);
		return await page.evaluate(() => {
			const img = document.getElementById('i');
			const c = document.createElement('canvas');
			c.width = 160;
			c.height = 100;
			const ctx = c.getContext('2d');
			ctx.drawImage(img, 0, 0, 160, 100);
			const d = ctx.getImageData(0, 0, 160, 100).data;
			const colours = new Set();
			let opaque = 0;
			for (let i = 0; i < d.length; i += 4) {
				colours.add(`${d[i]},${d[i + 1]},${d[i + 2]}`);
				if (d[i + 3] > 0) opaque++;
			}
			return { distinctColours: colours.size, opaqueFrac: opaque / (d.length / 4) };
		});
	} finally {
		await page.close();
	}
}

async function shootGodsEye(browser, name, hash, file, extraAsserts) {
	const page = await browser.newPage();
	await page.setViewport({ width: 1440, height: 900 });
	try {
		await page.goto(`${ORIGIN}/gods-eye${hash}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		const phase = await waitPhase(page);
		record(name, 'phase-ready', phase === 'ready', `phase=${phase}`);
		if (phase !== 'ready') return;
		await sleep(9000); // tiles + first layer draws under software GL
		const probe = await page.evaluate(async () => {
			const g = window.__GODS_EYE__;
			let feed = null;
			try {
				const r = await fetch('/api/v1/vehicles');
				feed = (await r.json())?.vehicles?.length ?? null;
			} catch { /* feed evidence only */ }
			return { ...g, liveVehicles: feed };
		});
		record(name, 'entities-rendered', (probe.entityCount ?? 0) > 0,
			`entities=${probe.entityCount} layers=[${probe.distinctLayerIds?.join(',')}]`);
		record(name, 'live-layer-on', (probe.distinctLayerIds ?? []).includes(extraAsserts.layer),
			`layer=${extraAsserts.layer} liveVehicles=${probe.liveVehicles}`);
		if (extraAsserts?.vehiclesFeed) {
			record(name, 'vehicles-feed-live', (probe.liveVehicles ?? 0) > 0, `vehicles=${probe.liveVehicles}`);
		}
		const live = await liveCanvasColours(page);
		record(name, 'live-canvas-nonblank', live >= PALETTE_MIN, `distinct colours=${live}`);
		const shot = path.join(OUT, file);
		await page.screenshot({ path: shot, captureBeyondViewport: false });
		const png = await pngNonBlank(browser, shot);
		record(name, 'png-nonblank', png.distinctColours >= PALETTE_MIN && png.opaqueFrac > 0.9,
			`png colours=${png.distinctColours} opaque=${(png.opaqueFrac * 100).toFixed(1)}%`);
		results.shots[name].file = file;
	} finally {
		await page.close();
	}
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: 'new',
		args: SWIFTSHADER_ARGS,
	});
	try {
		// ── 1. the 3D city mid-chaos (all live layers, old-city belt) ──────
		if (MODE_FLAG !== '--rest-only') {
		await shootGodsEye(browser, 'city3d',
			godsEyeHash({ lat: 23.0205, lon: 72.582, alt: 9000, layers: ['events', 'traces', 'signals', 'units', 'cameras'] }),
			'showcase_gods_eye_city.png',
			{ layer: 'events', vehiclesFeed: true });
		}

		if (MODE_FLAG !== '--city-only') {

		// ── 2. the pursuit trace view, framed on the REAL trace centroid ──
		// (centroid + point count read through the deck origin's own proxy;
		//  the page must be ON the origin first — about:blank cannot fetch
		//  relative URLs)
		const tracePage = await browser.newPage();
		await tracePage.goto(`${ORIGIN}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 })
			.catch(() => tracePage.goto(ORIGIN, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {}));
		const trace = await tracePage.evaluate(async () => {
			const r = await fetch('/api/v1/vehicles');
			const j = await r.json();
			const flagged = (j.vehicles || []).find((v) => v.earmark_state === 'flagged');
			if (!flagged?.vehicle_id) return { found: false, vehicles: (j.vehicles || []).length };
			const t = await fetch(`/api/v1/vehicles/${flagged.vehicle_id}/trace?window_sec=86400`);
			if (!t.ok) return { found: true, traceOk: false, vehicleId: flagged.vehicle_id };
			const tj = await t.json();
			return {
				found: true, traceOk: true, vehicleId: flagged.vehicle_id,
				points: tj.positions?.length ?? 0, source: tj.source,
				centroid: tj.geometry?.coordinates?.length
					? [
						tj.geometry.coordinates.reduce((s, c) => s + c[0], 0) / tj.geometry.coordinates.length,
						tj.geometry.coordinates.reduce((s, c) => s + c[1], 0) / tj.geometry.coordinates.length,
					]
					: null,
			};
		}).catch((e) => ({ found: false, error: String(e) }));
		await tracePage.close();
		results.pursuitFeed = trace;
		record('pursuit', 'flagged-vehicle-live', trace.found === true,
			`vehicles feed=${trace.vehicles ?? '?'} vehicleId=${trace.vehicleId ?? '-'}`);
		record('pursuit', 'trace-positions', (trace.points ?? 0) >= 2 && trace.traceOk === true,
			`points=${trace.points} source=${trace.source}`);
		const c = trace.centroid ?? [72.582, 23.0205];
		await shootGodsEye(browser, 'pursuitTrace',
			godsEyeHash({ lat: c[1], lon: c[0], alt: 2600, pitch: -48, layers: ['traces', 'events', 'signals', 'units'] }),
			'showcase_pursuit_trace.png',
			{ layer: 'traces', vehiclesFeed: true });

		// ── 3. the citizen advisory over the LIVE pursuit event ────────────
		const citizen = await browser.newPage();
		await citizen.setViewport({ width: 1440, height: 900 });
		try {
			await citizen.goto(`${ORIGIN}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 });
			await sleep(6500); // board poll rung (5 s) lands the live events
			// The interface-revamp advisory (CitizenAdvisory.svelte): assert
			// the rebuilt surface by its stable hooks — data-screen, the
			// plain-language band strip, and LIVE-source event cards (the
			// card badge is .badge.v-live only for real feed rows; demo
			// fixtures render .badge.v-fixture, honestly labelled).
			const p = await citizen.evaluate(() => ({
				screen: document.querySelectorAll('[data-screen="citizen"]').length,
				kicker: document.querySelector('.kicker')?.textContent?.trim() ?? '',
				strip: document.querySelector('[data-strip]')?.getAttribute('data-strip') ?? null,
				cards: document.querySelectorAll('.event-card').length,
				liveCards: document.querySelectorAll('.event-card .badge.v-live').length,
				bodyChars: document.body.innerText.trim().length,
			}));
			record('citizen', 'advisory-rendered',
				p.screen === 1 && p.kicker.length > 0 && !!p.strip,
				`kicker="${p.kicker}" strip=${p.strip}`);
			record('citizen', 'live-restriction-shown', p.liveCards > 0,
				p.liveCards > 0
					? `${p.liveCards} live-source event card(s) of ${p.cards} (live corridor restriction rendered)`
					: `no live-source event card (${p.cards} cards, all fixture)`);
			record('citizen', 'content-present', p.bodyChars > 400, `body text ${p.bodyChars} chars`);
			const shot = path.join(OUT, 'showcase_citizen_advisory.png');
			await citizen.screenshot({ path: shot, captureBeyondViewport: false });
			const png = await pngNonBlank(browser, shot);
			record('citizen', 'png-nonblank', png.distinctColours >= PALETTE_MIN && png.opaqueFrac > 0.9,
				`png colours=${png.distinctColours} opaque=${(png.opaqueFrac * 100).toFixed(1)}%`);
			results.shots.citizen ||= { checks: {} };
			results.shots.citizen.file = 'showcase_citizen_advisory.png';
		} finally {
			await citizen.close();
		}
		} // end --rest-only block
	} finally {
		await browser.close();
		fs.writeFileSync(path.join(OUT, 'shots_manifest.json'), JSON.stringify(results, null, 2));
	}
	console.log(JSON.stringify(results, null, 2));
	console.log(failures === 0 ? 'CANVAS SHOTS: OK' : `CANVAS SHOTS: ${failures} check(s) failed`);
	process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
	console.error('HARNESS ERROR', e);
	fs.writeFileSync(path.join(OUT, 'shots_manifest.json'), JSON.stringify({ ...results, error: String(e) }, null, 2));
	process.exit(2);
});
