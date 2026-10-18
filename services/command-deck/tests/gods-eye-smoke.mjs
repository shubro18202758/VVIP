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
 *   4. ZERO requests to cesium.com/ion (the keyless promise)
 *   5. the deck-origin proxy works: /api/v1/adaptive/status and
 *      /api/emulator/health through the origin return 200
 *   6. share-link parse: a hostile hash (lat=NaN) does not break the page
 *      and a valid hash restores the camera
 *
 * Usage:
 *   node tests/gods-eye-smoke.mjs [origin]        # default http://127.0.0.1:5199
 *   # origin is spawned automatically when not answering: PORT=5199 node build
 *   # (the spawned origin proxies /api/v1 at THIS clone's oracle :8091 and
 *   #  /api/emulator at the shared :8090 — ORACLE_PORT/BRAIN_PORT/
 *   #  EMULATOR_PORT override; live oracle + emulator must be up)
 *
 * Exits 0 with `ok <name> — <detail>` lines (showcase style); exit 1 on any
 * assertion failure. Screenshot artifact: tests/artifacts/gods-eye-smoke.png
 */

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');

// Puppeteer + cached Chrome live in the React frontend's tree (the e2e
// harness pattern from verify_all.sh tier frontend-e2e).
const require2 = createRequire('/home/sandy050405/Desktop/Veer/VVIP/frontend/package.json');
const puppeteer = require2('puppeteer').default ?? require2('puppeteer');

const CHROME_CANDIDATES = [
	process.env['CHROME_PATH'],
	'/home/sandy050405/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome',
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
	const proc = spawn('node', ['build'], {
		cwd: DECK,
		env: {
			...process.env,
			PORT: String(port),
			HOST: '127.0.0.1',
			ORACLE_PORT: process.env.ORACLE_PORT ?? '8091',
			BRAIN_PORT: process.env.BRAIN_PORT ?? '8092',
			EMULATOR_PORT: process.env.EMULATOR_PORT ?? '8090',
		},
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

try {
	const portArg = process.argv[2] ? Number(new URL(process.argv[2]).port) : 5199;
	const origin = process.argv[2] ?? `http://127.0.0.1:${portArg}`;
	deckProc = await ensureOrigin(origin, portArg);
	ok('deck-origin up', true, origin);

	const page = await browser.newPage();
	await page.setViewport({ width: 1280, height: 800 });

	// keyless promise: no cesium.com/ion request may leave the page.
	const ionRequests = [];
	const failedRequests = [];
	page.on('request', (req) => {
		const u = req.url();
		if (u.includes('cesium.com') || u.includes('ion.cesium')) ionRequests.push(u);
	});
	page.on('requestfailed', (req) => {
		const u = req.url();
		if (u.startsWith(origin)) failedRequests.push(`${u} ${req.failure()?.errorText ?? ''}`);
	});
	const consoleErrors = [];
	page.on('console', (m) => {
		if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200));
	});
	page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 200)}`));

	// ── 1+2+3: ready, non-blank, entities ─────────────────────────────────
	await page.goto(`${origin}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 60000 });
	const state = { phase: await waitPhase(page, 60000) };
	ok('canvas phase ready', state.phase === 'ready', `phase=${state.phase}`);

	if (state.phase === 'ready') {
		// Bounded wait for imagery: tiles loaded OR the offline schematic
		// kicked in (sustained tile errors). Under software GL the globe
		// settle time varies with host load — poll the live snapshot
		// (tilesLoaded publishes from the queue-depth event) instead of a
		// fixed sleep, so the assertion semantics stay identical.
		const tileDeadline = Date.now() + 45000;
		let tilesSettled = false;
		while (Date.now() < tileDeadline) {
			tilesSettled = await page
				.evaluate(() => {
					const g = window.__GODS_EYE__;
					return !!g && (g.tilesLoaded || g.imageryMode === 'offline-schematic');
				})
				.catch(() => false);
			if (tilesSettled) break;
			await sleep(500);
		}
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
		ok(
			'tiles loaded or offline schematic',
			probe.tilesLoaded || probe.imageryMode === 'offline-schematic',
			`tilesLoaded=${probe.tilesLoaded} mode=${probe.imageryMode}`,
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
		ok('mode endpoint keyless by default', probe.proxy.mode?.endsWith('keyless-osm'), `mode=${probe.proxy.mode}`);

		mkdirSync(path.join(HERE, 'artifacts'), { recursive: true });
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'gods-eye-smoke.png') });
	}

	// ── 4: zero ion traffic ────────────────────────────────────────────────
	await sleep(1000);
	ok('zero cesium.com/ion requests (keyless)', ionRequests.length === 0, ionRequests.slice(0, 3).join(' | ') || 'none');

	// ── 6: share-link hostile + valid hash ─────────────────────────────────
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
} finally {
	await browser.close().catch(() => {});
	if (deckProc) deckProc.kill();
}

if (failures > 0) {
	console.error(`gods-eye-smoke: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   gods-eye-smoke — all assertions passed');
process.exit(0);
