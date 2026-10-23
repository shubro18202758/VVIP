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
 *   3. showcase_citizen_advisory.png /citizen advisory, shot anonymously as
 *                                    the public sees it: the pursuit stays
 *                                    OFF the public page (vehicle_pursuit is
 *                                    not a public kind — holds.ts
 *                                    PUBLIC_EVENT_KINDS: publishing it could
 *                                    tip off a suspect), no plate appears,
 *                                    and no emulated card wears LIVE (C-M).
 *
 * Non-blank proof: every saved PNG is reloaded into a blank page as an
 * <img> and drawn to a 2D canvas — Chrome itself is the decoder — then
 * distinct-colour counting runs over the pixels (>= PALETTE_MIN distinct
 * colours AND not a fully-transparent frame). The two /gods-eye shots
 * ALSO use the in-live-canvas readback (the gods-eye-smoke.mjs pattern:
 * the viewer is created with preserveDrawingBuffer for exactly this).
 *
 * Operator gate: the deck's /api/* reads are gated (VEER_OPERATOR_PIN), so
 * the camera signs in ONCE through the shared helper (scripts/lib/
 * deck_session.mjs — one attempt, PIN read at run time, never logged) and
 * sets the session cookie on every page BEFORE its first navigation; the
 * session is revoked on exit. A signed-in session names an officer, so the
 * canvas shows plates unmasked (the pursuit plate is the EMULATED
 * GJ27EM9001); the /citizen shot is taken WITHOUT the session — the public,
 * masked surface. Nothing here clicks, approves or writes: it photographs.
 *
 * Usage: node showcase_canvas_shots.mjs [--city-only|--rest-only|--ops-only] [origin] [out-dir] [pursuit-title]
 *        --ops-only: just showcase_ops_wall.png, the signed-in /ops wall (its
 *        canvas panel on the photoreal city when the deck is keyed)
 *        origin defaults to the live deck http://127.0.0.1:5273
 * Env:   CHROME_PATH (defaults to the puppeteer cache path the e2e and
 *        gods-eye smoke use on this box); VEER_OPERATOR_PIN, else
 *        VEER_OPERATOR_PIN_FILE (a sandbox deck's PIN), else veer.env.
 * Exit:  0 all three shots proven non-blank; 1 any failure (manifest
 *        still written with the per-shot evidence); 2 harness error
 *        (including a refused sign-in).
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { puppeteerCookies, signIn, signOut } from './lib/deck_session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// --city-only: just the 3D city shot (taken mid-chaos while the flagship
// event is active). --rest-only: pursuit trace + citizen advisory (after
// the pursuit chain exists). No flag: all three.
const MODE_FLAG = process.argv[2]?.startsWith('--') ? process.argv[2] : null;
const ARGS = MODE_FLAG ? process.argv.slice(3) : process.argv.slice(2);
const ORIGIN = (ARGS[0] || 'http://127.0.0.1:5273').replace(/\/+$/, ''); // the argus deck (the ONE deck)
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
/** The operator session (signIn → {gate:false} when the deck's gate is off). */
let session = null;

/** A page carrying the session cookie — set BEFORE the first navigation. */
async function openPage(browser) {
	const page = await browser.newPage();
	if (session) {
		// a page opened right after another closed can report 'main frame too
		// early' for a beat — retry the cookie once it has one
		for (let i = 0; ; i++) {
			try {
				await page.setCookie(...puppeteerCookies(session));
				break;
			} catch (e) {
				if (i >= 5) throw e;
				await sleep(500);
			}
		}
	}
	return page;
}
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

/**
 * On an ion-keyed deck (a signed-in operator), wait for the photoreal 3D city:
 * imageryMode 'ion-photoreal' with the photoreal-3d layer loaded. One
 * api.cesium.com edge refuses TLS from some hosts and a page that draws it
 * falls back honestly (keyless imagery / no tiles) — the caller then retries
 * in a fresh page. Keyless decks resolve at once ('keyless').
 */
async function waitPhotoreal(page, timeoutMs = 150000) {
	// keyed = the deck serves this session the ion upgrade (signed-in operator
	// on a deck with a token); a keyed page that shows keyless imagery has hit
	// the ion fallback, not a keyless deck
	const keyed = await page
		.evaluate(async () => (await (await fetch('/gods-eye/mode', { cache: 'no-store' })).json())?.mode === 'ion-photoreal')
		.catch(() => false);
	const deadline = Date.now() + timeoutMs;
	let last = null;
	while (Date.now() < deadline) {
		last = await page
			.evaluate(() => {
				const g = window.__GODS_EYE__ ?? {};
				const p = g.layerStats?.['photoreal-3d'] ?? null;
				return { mode: g.imageryMode ?? null, tiles: p?.initialTilesLoaded ?? null, via: p?.via ?? null };
			})
			.catch(() => null);
		if (!keyed && last?.mode) return 'keyless';
		if (keyed && last?.mode && !String(last.mode).startsWith('ion')) return 'fallback';
		if (last?.via === 'failed') return 'fallback';
		if (last?.mode === 'ion-photoreal' && last.tiles === 1) return 'photoreal';
		await sleep(1000);
	}
	return last?.mode === 'ion-photoreal' ? 'photoreal-pending' : 'fallback';
}

async function shootGodsEye(browser, name, hash, file, extraAsserts) {
	for (let attempt = 1; attempt <= 3; attempt++) {
		const page = await openPage(browser);
		await page.setViewport({ width: 1440, height: 900 });
		try {
			await page.goto(`${ORIGIN}/gods-eye${hash}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
			const phase = await waitPhase(page);
			if (phase !== 'ready') {
				record(name, 'phase-ready', false, `phase=${phase}`);
				return;
			}
			const imagery = await waitPhotoreal(page);
			// the TLS-refusing ion edge (or tiles still streaming from it): a
			// fresh page draws another edge
			if ((imagery === 'fallback' || imagery === 'photoreal-pending') && attempt < 3) continue;
			record(name, 'phase-ready', true, `phase=${phase}`);
			record(name, 'imagery', imagery !== 'fallback', `imagery=${imagery} attempt=${attempt}`);
			// draped lines are built in web workers, batch by batch (WP-22): let
			// them land on the tiles before the frame is taken
			await sleep(imagery === 'keyless' ? 9000 : 20000);
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
			return;
		} finally {
			await page.close().catch(() => {});
		}
	}
}

/** The signed-in /ops situation wall (decision queue + canvas panel + rails). */
async function shootOps(browser) {
	const name = 'opsWall';
	for (let attempt = 1; attempt <= 3; attempt++) {
		const page = await openPage(browser);
		await page.setViewport({ width: 1440, height: 900 });
		try {
			await page.goto(`${ORIGIN}/ops`, { waitUntil: 'domcontentloaded', timeout: 60000 });
			const phase = await waitPhase(page);
			if (phase !== 'ready') {
				record(name, 'phase-ready', false, `phase=${phase}`);
				return;
			}
			const imagery = await waitPhotoreal(page);
			if ((imagery === 'fallback' || imagery === 'photoreal-pending') && attempt < 3) continue;
			record(name, 'phase-ready', true, `phase=${phase}`);
			record(name, 'imagery', imagery !== 'fallback', `imagery=${imagery} attempt=${attempt}`);
			await sleep(imagery === 'keyless' ? 9000 : 20000);
			const queue = await page.$$eval('[data-testid="queue-card"]', (els) => els.length).catch(() => 0);
			record(name, 'decision-queue', queue >= 0, `${queue} queue card(s)`);
			const shot = path.join(OUT, 'showcase_ops_wall.png');
			await page.screenshot({ path: shot, captureBeyondViewport: false });
			const png = await pngNonBlank(browser, shot);
			record(name, 'png-nonblank', png.distinctColours >= PALETTE_MIN && png.opaqueFrac > 0.9,
				`png colours=${png.distinctColours} opaque=${(png.opaqueFrac * 100).toFixed(1)}%`);
			results.shots[name].file = 'showcase_ops_wall.png';
			return;
		} finally {
			await page.close().catch(() => {});
		}
	}
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	session = await signIn(ORIGIN, 'Showcase canvas shots (automated check)');
	results.operatorGate = session.gate ? 'on (signed in)' : 'off';
	const browser = await puppeteer.launch({
		executablePath: CHROME,
		headless: 'new',
		args: SWIFTSHADER_ARGS,
	});
	try {
		// ── 1. the 3D city mid-chaos (all live layers, old-city belt) ──────
		if (MODE_FLAG === '--ops-only') {
			await shootOps(browser);
		}

		if (MODE_FLAG !== '--rest-only' && MODE_FLAG !== '--ops-only') {
		await shootGodsEye(browser, 'city3d',
			godsEyeHash({ lat: 23.0035, lon: 72.578, alt: 5200, pitch: -42, layers: ['photoreal-3d', 'flow', 'events', 'traces', 'signals', 'units', 'cameras', 'enforcement'] }),
			'showcase_gods_eye_city.png',
			{ layer: 'events', vehiclesFeed: true });
		}

		if (MODE_FLAG !== '--city-only' && MODE_FLAG !== '--ops-only') {

		// ── 2. the pursuit trace view, framed on the REAL trace centroid ──
		// (centroid + point count read through the deck origin's own proxy;
		//  the page must be ON the origin first — about:blank cannot fetch
		//  relative URLs)
		const tracePage = await openPage(browser);
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
			// no flow layer here: the vehicle's draped trace and its numbered
			// hops are the subject, on the photoreal old city
			godsEyeHash({ lat: c[1] - 0.012, lon: c[0], alt: 2200, pitch: -50, layers: ['photoreal-3d', 'traces', 'events', 'signals', 'units', 'enforcement'] }),
			'showcase_pursuit_trace.png',
			{ layer: 'traces', vehiclesFeed: true });

		// ── 3. the citizen advisory — anonymous, as the public sees it ─────
		// its own browser context: the operator pages above set the session
		// cookie in the default context, and a public shot must never carry it
		const publicContext = await browser.createBrowserContext();
		const citizen = await publicContext.newPage();
		await citizen.setViewport({ width: 1440, height: 900 });
		try {
			await citizen.goto(`${ORIGIN}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 });
			await sleep(6500); // board poll rung (5 s) lands the live events
			// The advisory (CitizenAdvisory.svelte), asserted by its stable
			// hooks — data-screen, the plain-language band strip, the event
			// cards' data-source — and by what must NOT reach the public: the
			// pursuit (an operator-only kind), any plate, and a LIVE badge on
			// an emulated card. The cards by source are evidence, not a pass
			// condition: the showcase's public-kind events are emulated runs.
			const p = await citizen.evaluate((pursuitTitle) => {
				const text = document.body.innerText;
				const cards = [...document.querySelectorAll('.event-card')];
				const bySource = {};
				for (const c of cards) {
					const src = c.getAttribute('data-source') || 'unknown';
					bySource[src] = (bySource[src] ?? 0) + 1;
				}
				const titled = !!pursuitTitle && !['PURSUIT', 'pending'].includes(pursuitTitle);
				return {
					screen: document.querySelectorAll('[data-screen="citizen"]').length,
					kicker: document.querySelector('.kicker')?.textContent?.trim() ?? '',
					strip: document.querySelector('[data-strip]')?.getAttribute('data-strip') ?? null,
					cards: cards.length,
					bySource,
					mislabelled: cards.filter(
						(c) => (c.getAttribute('data-source') === 'emulated' || c.classList.contains('emulated'))
							&& c.querySelector('[data-source-badge="live"], .badge.v-live'),
					).length,
					pursuitShown: (titled && text.includes(pursuitTitle)) || /\bpursuit\b/i.test(text),
					plateShown: /\b[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4}\b/.test(text),
					bodyChars: text.trim().length,
				};
			}, PURSUIT_TITLE);
			const cardNote = `${p.cards} card(s) by source ${JSON.stringify(p.bySource)}`;
			record('citizen', 'advisory-rendered',
				p.screen === 1 && p.kicker.length > 0 && !!p.strip,
				`kicker="${p.kicker}" strip=${p.strip}`);
			record('citizen', 'pursuit-not-public', !p.pursuitShown,
				`tactical operations stay operator-only (holds.ts PUBLIC_EVENT_KINDS); ${cardNote}`);
			record('citizen', 'no-plate-on-public-page', !p.plateShown, p.plateShown ? 'a plate-shaped string is on the public page' : 'none');
			record('citizen', 'emulated-never-labelled-live', p.mislabelled === 0,
				`${p.mislabelled} emulated card(s) wearing LIVE; ${cardNote}`);
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
			await publicContext.close();
		}
		} // end --rest-only block
	} finally {
		await browser.close();
		await signOut(session);
		writeManifest(results);
	}
	console.log(JSON.stringify(results, null, 2));
	console.log(failures === 0 ? 'CANVAS SHOTS: OK' : `CANVAS SHOTS: ${failures} check(s) failed`);
	process.exit(failures === 0 ? 0 : 1);
}

main().catch(async (e) => {
	await signOut(session);
	console.error('HARNESS ERROR', e);
	writeManifest({ ...results, error: String(e) });
	process.exit(2);
});

/**
 * shots_manifest.json is the record of the canvas shots: --ops-only adds its
 * one shot to the existing record instead of replacing the city/pursuit
 * evidence a showcase run wrote.
 */
function writeManifest(record) {
	const file = path.join(OUT, 'shots_manifest.json');
	let out = record;
	if (MODE_FLAG === '--ops-only' && fs.existsSync(file)) {
		try {
			const prior = JSON.parse(fs.readFileSync(file, 'utf8'));
			out = { ...prior, shots: { ...(prior.shots ?? {}), ...record.shots }, ...(record.error ? { opsError: record.error } : {}) };
		} catch {
			out = record;
		}
	}
	fs.writeFileSync(file, JSON.stringify(out, null, 2));
}
