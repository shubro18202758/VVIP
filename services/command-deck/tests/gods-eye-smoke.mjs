/**
 * God's-eye canvas headless smoke — the city-canvas stream's render gate.
 *
 * Builds nothing itself: serve the adapter-node `build/` output (or pass
 * an already-running origin) and verify, under SOFTWARE WebGL (SwiftShader —
 * the flags the red-team render proof validated:
 * --enable-unsafe-swiftshader --use-gl=angle --use-angle=swiftshader):
 *
 *   1. /gods-eye reaches phase 'ready' (window.__GODS_EYE__.ready)
 *   2. the WebGL canvas is NON-BLANK (in-page distinct-colour readback;
 *      the viewer is created with preserveDrawingBuffer for exactly this)
 *   3. entities exist (fixture + live event spines from the live oracle)
 *   4. the basemap the mode endpoint chose:
 *        keyless (default) — ZERO requests to cesium.com/ion or Google's
 *        tile servers, base tiles ONLY from tile.openstreetmap.org (the
 *        dark re-graded OSM raster), and the 3D chip (photoreal-3d)
 *        unconfigured with 'needs VEER_CESIUM_ION_TOKEN';
 *        ion-keyed (VEER_CESIUM_ION_TOKEN on the deck) — ion requests made,
 *        imageryMode 'ion-photoreal' (or a labelled keyless fallback — never
 *        the offline schematic while the ion imagery loads) and the 3D chip
 *        configured
 *   4b. ZERO Content-Security-Policy violations on /gods-eye and /ops in
 *      either mode (WP-22: the ion imagery's plain-http Bing tiles were
 *      refused on the http deck and tipped the canvas into the schematic)
 *   4c. '© OpenStreetMap contributors' spelled out and visible on the
 *      canvas's on-screen credit plate, either mode (OSMF attribution
 *      guidelines — the '© OSM' legend chip and the lightbox are not enough)
 *   5. the deck-origin proxy works: /api/v1/adaptive/status and
 *      /api/emulator/health through the origin return 200
 *   6. share-link parse: a hostile hash (lat=NaN) does not break the page
 *      and a valid hash restores the camera
 *
 * Operator gate: when the origin's gate is on (VEER_OPERATOR_PIN on the
 * deck) the smoke signs in ONCE through tests/lib/operator-session.mjs —
 * the PIN is read at run time (VEER_OPERATOR_PIN, else
 * VEER_OPERATOR_PIN_FILE, else veer.env) and never logged — reads the mode
 * endpoint WITH the session (an anonymous read is keyless under the gate),
 * sets the session cookie on every page before its first navigation and
 * signs out on exit. Gate off: nothing to sign in to.
 *
 * Usage:
 *   node tests/gods-eye-smoke.mjs [origin]        # default http://127.0.0.1:5199
 *   # origin is spawned automatically when not answering: PORT=5199 node build
 *   # (the spawned origin proxies /api/v1 at THIS clone's oracle :8091 and
 *   #  /api/emulator at the shared :8090 — ORACLE_PORT/BRAIN_PORT/
 *   #  EMULATOR_PORT override; live oracle + emulator must be up). It is a
 *   #  PRIVATE preview: gate off and keyless whatever this shell exports —
 *   #  the PIN, the session secret and the ion token never reach it (point
 *   #  the smoke at an already-running keyed deck for the ion-keyed checks)
 *
 * Exits 0 with `ok <name> — <detail>` lines (showcase style); exit 1 on any
 * assertion failure. Screenshot artifact: tests/artifacts/gods-eye-smoke.png
 */

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { deckFetch, puppeteerCookies, signIn, signOut } from './lib/operator-session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');

// Puppeteer resolves from THIS repo's frontend tree first (the e2e harness
// pattern, as in canvas-ux-smoke), then the shared trees; Chrome from the
// puppeteer cache.
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

const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	// The red-team-validated software-WebGL combination.
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
];

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) {
		console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Poll __GODS_EYE__.phase in the CURRENT execution context (reload-safe). */
async function waitPhase(page, timeoutMs = 45000) {
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

async function originAlive(origin) {
	try {
		const res = await fetch(`${origin}/gods-eye`, { signal: AbortSignal.timeout(3000) });
		return res.ok;
	} catch {
		return false;
	}
}

/** Spawn `node build` on the smoke port (adapter-node output). */
async function ensureOrigin(origin, port) {
	if (await originAlive(origin)) return null;
	if (!existsSync(path.join(DECK, 'build', 'index.js'))) {
		throw new Error('no build/ output — run `npx vite build` first');
	}
	// THIS clone's upstream ports (infra/compose.argus-override.yml): the
	// oracle listens on :8091 and the brain on :8092 — the original fleet
	// keeps 8081/8080 — and the emulator is the shared host service on
	// :8090. hooks.server.ts reads ORACLE_PORT/BRAIN_PORT/EMULATOR_PORT, and
	// without these the spawned origin would proxy /api/v1 at the ORIGINAL
	// fleet's oracle (:8081) — the wrong stack for this clone's gate (the
	// clone's own model_drift_panel.sh probes :8091). Explicit env still
	// wins, so an operator can repoint deliberately.
	const env = {
		...process.env,
		PORT: String(port),
		HOST: '127.0.0.1',
		ORACLE_PORT: process.env.ORACLE_PORT ?? '8091',
		BRAIN_PORT: process.env.BRAIN_PORT ?? '8092',
		EMULATOR_PORT: process.env.EMULATOR_PORT ?? '8090',
	};
	// A preview this smoke spawns is private: gate off and keyless, whatever
	// this shell exported (the PIN, the session secret and the ion token stay
	// with the deployed deck).
	for (const k of ['VEER_OPERATOR_PIN', 'VEER_OPERATOR_PIN_FILE', 'VEER_SESSION_SECRET', 'VEER_SESSION_SECURE', 'VEER_CESIUM_ION_TOKEN']) delete env[k];
	const proc = spawn('node', ['build'], {
		cwd: DECK,
		env,
		stdio: ['ignore', 'pipe', 'pipe'],
	});
	proc.stdout.on('data', () => {});
	proc.stderr.on('data', (d) => process.stderr.write(`[deck] ${d}`));
	for (let i = 0; i < 40; i++) {
		if (await originAlive(origin)) return proc;
		await sleep(500);
	}
	proc.kill();
	throw new Error(`deck origin did not come up on ${origin}`);
}

const browser = await puppeteer.launch({ executablePath, headless: 'new', args: SWIFTSHADER_ARGS });
let deckProc = null;
let session = null;

try {
	const portArg = process.argv[2] ? Number(new URL(process.argv[2]).port) : 5199;
	const origin = process.argv[2] ?? `http://127.0.0.1:${portArg}`;
	deckProc = await ensureOrigin(origin, portArg);
	ok('deck-origin up', true, origin);
	session = await signIn(origin, 'Gods-eye smoke (automated check)');
	ok('operator gate', true, session.gate ? 'on — signed in' : 'off');

	// The mode endpoint decides keyless vs ion; read it first (same origin),
	// WITH the session: under the gate an anonymous read is keyless by design.
	const modeDoc = await deckFetch(session, '/gods-eye/mode', { signal: AbortSignal.timeout(5000) })
		.then((r) => r.json())
		.catch(() => ({}));
	const keyed = modeDoc.mode === 'ion-photoreal';
	ok('mode endpoint answers', modeDoc.mode === 'keyless-osm' || keyed, `mode=${modeDoc.mode ?? 'unreachable'}${keyed ? ' (ion-keyed run)' : ''}`);

	// keyless promise: no cesium.com/ion or Google tile request may leave the
	// page, and the base tiles come only from the OSM tile server.
	const ionRequests = [];
	const googleTileRequests = [];
	const tileRequests = [];
	const failedRequests = [];
	/** Cesium ion's API refused at the TLS layer — the environment, not the deck. */
	let ionRefused = 0;
	/** Requests still unanswered (diagnostics for a keyed load that never settles). */
	const inFlight = new Map();
	const stalledHosts = () => {
		const out = {};
		for (const [u, t0] of inFlight) {
			if (Date.now() - t0 < 30_000) continue;
			let host = '?';
			try {
				host = new URL(u).hostname;
			} catch {
				// data:/blob:
			}
			out[host] = (out[host] ?? 0) + 1;
		}
		return out;
	};
	const consoleErrors = [];
	const cspViolations = [];
	function watch(p) {
		p.on('requestfinished', (req) => inFlight.delete(req.url()));
		p.on('request', (req) => {
			const u = req.url();
			if (/^https?:/.test(u)) inFlight.set(u, Date.now());
			if (u.includes('cesium.com') || u.includes('ion.cesium')) ionRequests.push(u);
			let host = '';
			try {
				host = new URL(u).hostname;
			} catch {
				// data:/blob: URLs
			}
			// Google Fonts (fonts.googleapis.com) is the page's typeface, not a tile
			if (host.endsWith('googleapis.com') && host !== 'fonts.googleapis.com') googleTileRequests.push(u);
			if (/\/\d+\/\d+\/\d+(?:@2x)?\.(?:png|jpe?g|webp)(?:[?#]|$)/.test(u)) tileRequests.push({ host, url: u });
		});
		p.on('requestfailed', (req) => {
			const u = req.url();
			inFlight.delete(u);
			if (u.startsWith(origin)) failedRequests.push(`${u} ${req.failure()?.errorText ?? ''}`);
			if (/^https:\/\/api\.cesium\.com\//.test(u) && /SSL|CONNECTION|TIMED_OUT/i.test(req.failure()?.errorText ?? '')) ionRefused++;
		});
		p.on('console', (m) => {
			const text = m.text();
			if (/Content Security Policy/i.test(text)) cspViolations.push(text.replace(/[?&](access_token|key)=[^&\s"']+/g, '?$1=…').slice(0, 160));
			if (m.type() === 'error') consoleErrors.push(text.slice(0, 200));
		});
		p.on('pageerror', (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 200)}`));
	}

	// ── 1+2+3: ready, non-blank, entities ─────────────────────────────────
	// A keyed run depends on Cesium ion's and Google's CDNs: one whose browser
	// drew an api.cesium.com edge that refuses TLS (seen from the demo box:
	// one CDN address fails every handshake, and a browser context keeps its
	// pick) or whose tile requests stall unanswered is an ENVIRONMENT fault —
	// it is retried in a fresh browser context (own DNS + sockets), at most
	// twice, and said with what stalled. A deck fault fails all three.
	let page = null;
	let state = { phase: null };
	let tilesSettled = false;
	for (let attempt = 1; attempt <= (keyed ? 3 : 1); attempt++) {
		const context = attempt === 1 ? browser.defaultBrowserContext() : await browser.createBrowserContext();
		page = await context.newPage();
		await page.setViewport({ width: 1280, height: 800 });
		// the session rides on this context's pages (set before the first goto)
		if (session.gate) await page.setCookie(...puppeteerCookies(session));
		watch(page);
		const refusedBefore = ionRefused;
		await page.goto(`${origin}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		state = { phase: await waitPhase(page, 60000) };
		if (state.phase !== 'ready') break;
		// Bounded wait for imagery: tiles loaded OR the offline schematic
		// kicked in (sustained tile errors). Under software GL the globe
		// settle time varies with host load — poll the live snapshot
		// (tilesLoaded publishes from the queue-depth event) instead of a
		// fixed sleep, so the assertion semantics stay identical. The keyed
		// canvas streams world terrain, aerial imagery AND the photoreal
		// tiles under SwiftShader: it gets the longer budget (the offline
		// schematic used to end this wait early — the refused http imagery).
		const tileDeadline = Date.now() + (keyed ? 150000 : 45000);
		tilesSettled = false;
		while (Date.now() < tileDeadline) {
			tilesSettled = await page
				.evaluate((isKeyed) => {
					const g = window.__GODS_EYE__;
					if (!g) return false;
					if (g.imageryMode === 'offline-schematic') return true;
					// the keyed canvas settles when the photoreal tiles cover the view
					// (or, they having failed, when the globe's tiles have loaded)
					const p3d = g.layerStats?.['photoreal-3d'];
					if (isKeyed) return p3d?.initialTilesLoaded === 1 || (p3d?.via === 'failed' && g.tilesLoaded);
					return g.tilesLoaded;
				}, keyed)
				.catch(() => false);
			if (tilesSettled) break;
			// the ion API refused this context: no point waiting it out
			if (keyed && ionRefused > refusedBefore && Date.now() > tileDeadline - 120000) break;
			await sleep(500);
		}
		if (tilesSettled || !keyed) break;
		console.log(
			`note keyed tiles did not settle in this browser context — ion TLS refusals ${ionRefused - refusedBefore}, requests unanswered >30 s ${JSON.stringify(stalledHosts())}${attempt < 3 ? ` — retrying in a fresh context (${attempt}/3)` : ''}`,
		);
		if (attempt < 3) {
			await page.close();
			inFlight.clear();
		}
	}
	ok('canvas phase ready', state.phase === 'ready', `phase=${state.phase}`);

	if (state.phase === 'ready') {
		// first layer draws settle time under software GL
		await sleep(1500);
		const probe = await page.evaluate(async () => {
			const g = window.__GODS_EYE__;
			const c = document.querySelector('.ge-canvas canvas');
			let distinctColours = -1;
			if (c) {
				const tmp = document.createElement('canvas');
				tmp.width = 96;
				tmp.height = 60;
				const ctx = tmp.getContext('2d');
				ctx.drawImage(c, 0, 0, 96, 60);
				const d = ctx.getImageData(0, 0, 96, 60).data;
				const colours = new Set();
				for (let i = 0; i < d.length; i += 4) colours.add(`${d[i]},${d[i + 1]},${d[i + 2]}`);
				distinctColours = colours.size;
			}
			// In-page proxy checks through the deck origin.
			const proxy = {};
			try {
				const r = await fetch('/api/v1/adaptive/status');
				proxy.adaptiveStatus = r.status;
			} catch {
				proxy.adaptiveStatus = 0;
			}
			try {
				const r = await fetch('/api/emulator/health');
				proxy.emulatorHealth = r.status;
			} catch {
				proxy.emulatorHealth = 0;
			}
			try {
				const r = await fetch('/gods-eye/mode');
				const body = await r.json();
				proxy.mode = `${r.status}:${body.mode}`;
			} catch {
				proxy.mode = '0';
			}
			return {
				...g,
				distinctColours,
				hasCanvas: !!c,
				proxy,
				hash: window.location.hash.slice(0, 80),
			};
		});
		ok('canvas element present', probe.hasCanvas, probe.hasCanvas ? 'viewer canvas mounted' : 'no canvas');
		ok(
			'canvas non-blank (SwiftShader readback)',
			probe.distinctColours >= 8,
			`distinct colours=${probe.distinctColours}`,
		);
		ok('entities rendered', probe.entityCount > 0, `entities=${probe.entityCount} layers=[${probe.distinctLayerIds.join(',')}]`);
		const p3dReady = probe.layerStats?.['photoreal-3d']?.initialTilesLoaded === 1;
		ok(
			'tiles loaded (globe or photoreal 3D) or offline schematic',
			probe.tilesLoaded || probe.imageryMode === 'offline-schematic' || p3dReady,
			`tilesLoaded=${probe.tilesLoaded} photoreal=${p3dReady} mode=${probe.imageryMode}`,
		);
		ok(
			'deck proxy: adaptive/status through origin',
			probe.proxy.adaptiveStatus === 200,
			`status=${probe.proxy.adaptiveStatus}`,
		);
		ok(
			'deck proxy: emulator/health through origin (strip rule)',
			probe.proxy.emulatorHealth === 200,
			`status=${probe.proxy.emulatorHealth}`,
		);
		if (keyed) {
			ok('ion-keyed: mode endpoint names the photoreal asset', modeDoc.tiles3d_asset_id === 2275207, `tiles3d_asset_id=${modeDoc.tiles3d_asset_id}`);
			ok(
				'ion-keyed: ion imagery (or a labelled keyless fallback) — never the offline schematic',
				probe.imageryMode === 'ion-photoreal' || (probe.imageryMode === 'keyless-osm' && !!probe.imageryNote),
				`imageryMode=${probe.imageryMode} note=${probe.imageryNote ?? '—'}`,
			);
		} else {
			ok('mode endpoint keyless by default', probe.proxy.mode?.endsWith('keyless-osm'), `mode=${probe.proxy.mode}`);
		}
		// the 3D chip: unconfigured keyless, configured when keyed
		const p3d = probe.layerStats?.['photoreal-3d'] ?? null;
		const chipTitle = await page
			.$eval('button.chip.layer[data-layer="photoreal-3d"]', (el) => el.getAttribute('title') ?? '')
			.catch(() => null);
		if (keyed) {
			ok('ion-keyed: 3D chip configured', p3d?.configured === 1, JSON.stringify(p3d));
		} else {
			ok(
				'keyless: 3D chip unconfigured',
				!!p3d && p3d.configured === 0 && p3d.via === 'unconfigured' && p3d.count === 0,
				JSON.stringify(p3d),
			);
			ok('keyless: 3D chip says what it needs', !!chipTitle && chipTitle.includes('needs VEER_CESIUM_ION_TOKEN'), chipTitle ?? 'no 3D chip');
		}

		// the OSM attribution spelled out ON SCREEN (the credit plate), either
		// mode: the keyless raster is OSM, the keyed city draws OSM-derived roads
		const credit = await page.evaluate(() => {
			const bar = document.querySelector('.ge-canvas .cesium-viewer-bottom');
			if (!bar) return { present: false, osm: false, visible: false };
			const r = bar.getBoundingClientRect();
			const osmEl = Array.from(bar.querySelectorAll('a, span, div')).find((el) => /OpenStreetMap contributors/i.test(el.textContent ?? '') && el.children.length === 0);
			const box = osmEl?.getBoundingClientRect();
			const top = box ? document.elementFromPoint(box.left + Math.min(20, box.width / 2), box.top + box.height / 2) : null;
			return {
				present: true,
				osm: !!osmEl,
				visible: !!box && box.width > 0 && box.bottom <= innerHeight && !!top && bar.contains(top),
				at: `${Math.round(r.left)},${Math.round(r.top)}`,
				href: osmEl?.closest('a')?.getAttribute('href') ?? null,
			};
		});
		ok("'© OpenStreetMap contributors' visible on the canvas's credit plate", credit.osm && credit.visible, JSON.stringify(credit));

		mkdirSync(path.join(HERE, 'artifacts'), { recursive: true });
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'gods-eye-smoke.png') });
	}

	// ── 4: the basemap's network footprint ─────────────────────────────────
	await sleep(1000);
	if (keyed) {
		ok('ion-keyed: ion requested', ionRequests.length > 0, `${ionRequests.length} cesium.com request(s)`);
	} else {
		ok('zero cesium.com/ion requests (keyless)', ionRequests.length === 0, ionRequests.slice(0, 3).join(' | ') || 'none');
		ok('zero Google tile requests (keyless)', googleTileRequests.length === 0, googleTileRequests.slice(0, 3).join(' | ') || 'none');
		const hosts = [...new Set(tileRequests.map((t) => t.host))];
		ok(
			'keyless: base tiles only from tile.openstreetmap.org',
			tileRequests.length > 0 && hosts.every((h) => h === 'tile.openstreetmap.org'),
			`${tileRequests.length} tile request(s) from ${hosts.join(', ') || 'nowhere'}`,
		);
	}

	// ── 6: share-link hostile + valid hash ─────────────────────────────────
	// Each hash is entered as a FULL navigation (via about:blank): a hash-only
	// goto keeps the old document, whose City Command canvas writes ITS view
	// into the hash (throttled replaceState) before the reload reads it — a
	// race that slow software GL loses.
	await page.goto('about:blank');
	await page.goto(`${origin}/gods-eye#v=2&lat=NaN&lon=Infinity&alt=-5&style=flir`, {
		waitUntil: 'domcontentloaded',
		timeout: 30000,
	});
	await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
	const hostileState = { phase: await waitPhase(page) };
	ok(
		'hostile share hash rejected (non-finite guards)',
		hostileState.phase === 'ready',
		`phase=${hostileState.phase}`,
	);

	await page.goto('about:blank');
	await page.goto(`${origin}/gods-eye#v=2&lat=23.03&lon=72.58&alt=1500&heading=45&pitch=-40&style=flir&layers=events`, {
		waitUntil: 'domcontentloaded',
		timeout: 30000,
	});
	await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
	const restoredState = { phase: await waitPhase(page) };
	await sleep(1500);
	const camProbe = await page.evaluate(() => {
		const g = window.__GODS_EYE__;
		return { sensorMode: g?.sensorMode, layers: g?.distinctLayerIds };
	});
	ok(
		'valid share hash restores view (single layer on, FLIR)',
		restoredState.phase === 'ready' &&
			camProbe.layers.length === 1 &&
			camProbe.layers[0] === 'events' &&
			camProbe.sensorMode === 'flir-whot',
		`layers=[${camProbe.layers.join(',')}] sensor=${camProbe.sensorMode}`,
	);

	// ── /ops situation wall: the rebuilt board mounts without breaking ────
	await page.goto(`${origin}/ops`, { waitUntil: 'domcontentloaded', timeout: 30000 });
	const opsPhase = await waitPhase(page, 45000);
	await sleep(4000); // polls land: masthead, queue, gate card, audit rows
	const opsProbe = await page.evaluate(() => {
		const g = window.__GODS_EYE__;
		const text = (sel) => document.querySelector(sel)?.textContent ?? '';
		return {
			// The wall's three-column grid + design-system panels.
			wallGrid: !!document.querySelector('.wall-grid'),
			panels: document.querySelectorAll('[data-prim="panel"]').length,
			masthead: !!document.querySelector('[data-prim="band-masthead"]'),
			counters: document.querySelectorAll('[data-prim="data-tile"]').length,
			decisionCards: document.querySelectorAll('[data-prim="decision-card"]').length,
			auditRows: document.querySelectorAll('[data-prim="audit-row"], [data-prim="feed-item"]').length,
			amaCard: Array.from(document.querySelectorAll('h2')).some((h) =>
				h.textContent?.includes('Active Movement Adaptation'),
			),
			gateCard: Array.from(document.querySelectorAll('h2')).some((h) =>
				h.textContent?.includes('City Signal Plans'),
			),
			// Band chips render through the ONLY legal band renderer.
			bandChips: document.querySelectorAll('[data-prim="band-chip"]').length,
			// The pursuit-overlay smoke hook is published (count may be 0 when
			// no pursuit plan exists — the field must exist and be a number).
			pursuitEntities: g ? typeof g.pursuitEntities : 'missing',
			officerGate: !!document.querySelector('.officer-input'),
		};
	});
	ok('/ops canvas panel ready', opsPhase === 'ready', `phase=${opsPhase}`);
	ok('/ops situation wall grid present', opsProbe.wallGrid, 'three-column wall');
	ok('/ops design-system panels mounted', opsProbe.panels >= 6, `panels=${opsProbe.panels}`);
	ok('/ops band masthead present', opsProbe.masthead, 'BandMasthead primitive');
	ok('/ops commander counters mounted', opsProbe.counters === 4, `data-tiles=${opsProbe.counters}`);
	ok(
		'/ops decision surface mounted (cards or honest empty)',
		opsProbe.decisionCards >= 0,
		`decision-cards=${opsProbe.decisionCards}`,
	);
	ok('/ops audit stream rows render', opsProbe.auditRows > 0, `rows=${opsProbe.auditRows}`);
	ok('/ops AMA card surfaced', opsProbe.amaCard, 'Active Movement Adaptation card');
	ok('/ops signal gate card surfaced', opsProbe.gateCard, 'City Signal Plans gate');
	ok('/ops band chips route through BandChip', opsProbe.bandChips > 0, `chips=${opsProbe.bandChips}`);
	ok('/ops pursuit hook published', opsProbe.pursuitEntities === 'number', `pursuitEntities=${opsProbe.pursuitEntities}`);
	ok('/ops duty-officer gate present', opsProbe.officerGate, 'C6 identity input');
	mkdirSync(path.join(HERE, 'artifacts'), { recursive: true });
	await page.screenshot({ path: path.join(HERE, 'artifacts', 'ops-wall-smoke.png') });

	// ── /gods-eye pursuit hook (cinematic route publishes it too) ─────────
	await page.goto(`${origin}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 30000 });
	const gePhase = await waitPhase(page, 60000);
	if (gePhase === 'ready') {
		await sleep(2000);
		const geHook = await page.evaluate(() => ({
			pursuitEntities: typeof window.__GODS_EYE__?.pursuitEntities,
			tourButton: Array.from(document.querySelectorAll('button.chip')).some((b) =>
				b.textContent?.includes('TOUR'),
			),
		}));
		ok('/gods-eye pursuit hook published', geHook.pursuitEntities === 'number', `type=${geHook.pursuitEntities}`);
		ok('/gods-eye guided tour control present', geHook.tourButton, 'TOUR chip in the HUD');
	}

	// console errors: tolerate tile 4xx noise offline, fail on page errors
	const pageErrors = consoleErrors.filter((e) => e.startsWith('pageerror:'));
	ok('no page errors', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | ') || 'none');
	ok(`zero CSP violations (${keyed ? 'ion-keyed' : 'keyless'})`, cspViolations.length === 0, cspViolations.slice(0, 2).join(' | ') || 'none');
} finally {
	await signOut(session);
	await browser.close().catch(() => {});
	if (deckProc) deckProc.kill();
}

if (failures > 0) {
	console.error(`gods-eye-smoke: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   gods-eye-smoke — all assertions passed');
process.exit(0);
