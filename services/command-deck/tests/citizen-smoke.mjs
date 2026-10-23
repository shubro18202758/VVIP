/**
 * Citizen-screens headless smoke — the public / comms / chat render gate.
 *
 * Serves nothing itself: point it at a running deck origin (or let it spawn
 * the adapter-node `build/` output on the smoke port) and it verifies under
 * SwiftShader software WebGL:
 *
 *   0. EDGE (plain HTTP): /citizen carries CSP (frame-ancestors 'none'),
 *      nosniff, referrer-policy and x-frame-options; exactly one <title>
 *      (an unknown path's error page too); the server render is already
 *      daylight (no dark flash) and /driver is lang="gu"; /favicon.png
 *      answers; a WebSocket upgrade on the proxy is answered 501 in under
 *      0.2 s.
 *   1. /citizen against the origin's REAL public feed: the corridor map
 *      reaches ready with tile requests; no raw `kind_` / `consequence_`
 *      key in EN, HI or GU; no pursuit or crime card; the page makes NO
 *      operator API call (its only API traffic is /api/public/* and the
 *      road geometry); the citizen bar shows no band code and no SHARE;
 *      <html lang> follows the language switch. Then, with a STUBBED feed:
 *      hold rows + provenance (closure plan; the oracle's baseline labelled
 *      as the standard length, never as live), "unavailable right now"
 *      for unreadable facts, the graph alternate, a planned event never
 *      "holding now", and a pursuit smuggled into the feed is still
 *      dropped by the browser mapper.
 *  1c. /citizen with a STUBBED feed carrying an event-emulator row and a
 *      Later row (contract C-M): the row is badged SIMULATED FEED ·
 *      EMULATED in EN / HI / GU, the hero never reads LIVE over it, its
 *      routes never draw solid (live), the event starting beyond 2 h sits
 *      under 'Later' with its IST date, timeline clocks are IST in a
 *      browser set to America/New_York, and phase labels are localized.
 *      Plain HTTP: GET /api/public/events says emulated_labelling: true.
 *   2. /driver live flow (stubbed writes by default): unit + stretch →
 *      the real request body (ambulance, 108 unit, junction waypoint,
 *      requested_by 108-driver) → "awaiting duty officer" with NO approve
 *      button → the officer's approval (stubbed status) primes the window,
 *      which opens one window-length before the unit's projected arrival
 *      and counts down to it, unit tracked on the map; the request UI
 *      survives the event leaving the public feed. The scripted fallback
 *      (no live event) is labelled, sends nothing, and is not hijacked by
 *      a live event appearing mid-script. An EMULATED corridor event is
 *      never offered to the driver (the withheld count is said), and after
 *      the officer cancels a request exactly ONE request button remains.
 *      CITIZEN_SMOKE_LIVE_WRITES=1 instead files a REAL request against
 *      the origin's stack (sandbox only) and reads it back.
 *   3. /chat with a mocked C-D NDJSON stream: the agent bubble shows the
 *      streamed prose, one tool card, the decider + Gemma tier chips and
 *      the session; the follow-up carries the session id; a legacy raw
 *      JSON envelope renders as "no prose answer", never as an answer;
 *      the unreachable stream shows the labelled degradation.
 *   4. /comms with a stubbed board: the event select is initialised, the
 *      three language columns carry identical facts, the facts line shows
 *      the hold's provenance, a pursuit gets no citizen draft and no
 *      all-clear, a kind the oracle reports but the board mapper does not
 *      know is withheld too, the all-clear is trilingual, the dispatch
 *      ladder mirrors the backend (completed / cancelled chips, all-events
 *      view), and the false "logged for the audit record" claim is gone.
 *   5. shell: client navigation from an operator page drops its SHARE VIEW
 *      and band chip; the public bar never inherits them.
 *   6. grep gate: no $lib/stores or data-service import under the
 *      citizen/comms paths (the stream's import boundary).
 *
 * Usage:
 *   node tests/citizen-smoke.mjs [origin]        # default http://127.0.0.1:5199
 *
 * Exits 0 with `ok <name> — <detail>` lines (showcase style); exit 1 on any
 * assertion failure. Screenshot artifacts: tests/artifacts/citizen-*.png
 */

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import http from 'node:http';
import { existsSync, mkdirSync, readdirSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');

// Puppeteer is vendored once, in the repo's legacy frontend tree.
const PUPPETEER_HOME = process.env.PUPPETEER_HOME ?? '/home/sandy050405/argus/veer/VVIP/frontend';
const require2 = createRequire(path.join(PUPPETEER_HOME, 'package.json'));
const puppeteer = require2('puppeteer').default ?? require2('puppeteer');

/** CHROME_PATH, else the newest cached Chrome for Testing, else puppeteer's own. */
function findChrome() {
	if (process.env.CHROME_PATH && existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
	const cache = path.join(os.homedir(), '.cache', 'puppeteer', 'chrome');
	if (existsSync(cache)) {
		const builds = readdirSync(cache).sort().reverse();
		for (const b of builds) {
			for (const sub of ['chrome-linux-arm64', 'chrome-linux64', 'chrome-linux']) {
				const p = path.join(cache, b, sub, 'chrome');
				if (existsSync(p)) return p;
			}
		}
	}
	try {
		return puppeteer.executablePath();
	} catch {
		return null;
	}
}
const executablePath = findChrome();
if (!executablePath) throw new Error('no Chrome binary found (set CHROME_PATH)');

const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
];

const LIVE_WRITES = process.env.CITIZEN_SMOKE_LIVE_WRITES === '1';

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
const GU = /[઀-૿]/;
const HI = /[ऀ-ॿ]/;
const RAW_KEY = /\b(kind|consequence)_[a-z_]+/;
const BAND_CODE = /\b(GREEN|AMBER|RED|BLACK)\b/;
/** The only API a public page may call: its own projection + road geometry. */
const PUBLIC_API_OK = /^\/api\/(public\/|v1\/spatial\/segments)/;

async function originAlive(origin) {
	try {
		const res = await fetch(`${origin}/citizen`, { signal: AbortSignal.timeout(3000) });
		return res.ok;
	} catch {
		return false;
	}
}

/** Spawn `node build` on the smoke port (adapter-node output). */
async function ensureOrigin(origin, port) {
	if (await originAlive(origin)) return null;
	if (!existsSync(path.join(DECK, 'build', 'index.js'))) {
		throw new Error('no build/ output — run `npm run build` first');
	}
	const proc = spawn('node', ['build'], {
		cwd: DECK,
		env: { ...process.env, PORT: String(port), HOST: '127.0.0.1' },
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

/** Gate 6 — the stream's import boundary, checked on the source tree. */
function importBoundaryGate() {
	const roots = [
		'src/lib/citizen',
		'src/lib/comms',
		'src/routes/(public)',
		'src/routes/(deck)/chat',
		'src/routes/(deck)/comms',
	];
	const offenders = [];
	for (const root of roots) {
		const abs = path.join(DECK, root);
		if (!existsSync(abs)) continue;
		const walk = (dir) => {
			for (const entry of readdirSync(dir, { withFileTypes: true })) {
				const p = path.join(dir, entry.name);
				if (entry.isDirectory()) walk(p);
				else if (/\.(svelte|ts)$/.test(entry.name)) {
					const src = readFileSync(p, 'utf8');
					if (src.includes('$lib/stores') || src.includes('$lib/api/data-service')) {
						offenders.push(path.relative(DECK, p));
					}
				}
			}
		};
		walk(abs);
	}
	ok('import boundary (no $lib/stores, no data-service)', offenders.length === 0, offenders.join(', ') || 'clean');
}

async function text(page, selector) {
	return page.$eval(selector, (el) => el.textContent ?? '').catch(() => '');
}

/** Raw HTTP upgrade attempt: status + seconds (fetch cannot send Upgrade). */
function upgradeProbe(origin, pathname) {
	return new Promise((resolve) => {
		const u = new URL(pathname, origin);
		const t0 = process.hrtime.bigint();
		const req = http.request({
			host: u.hostname,
			port: u.port,
			path: u.pathname,
			headers: {
				Connection: 'Upgrade',
				Upgrade: 'websocket',
				'Sec-WebSocket-Version': '13',
				'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
			},
			timeout: 5000,
		});
		const done = (status) => resolve({ status, seconds: Number(process.hrtime.bigint() - t0) / 1e9 });
		req.on('response', (res) => {
			res.resume();
			done(res.statusCode);
		});
		req.on('upgrade', (res, socket) => {
			socket.destroy();
			done(res.statusCode);
		});
		req.on('timeout', () => {
			req.destroy();
			done('timeout');
		});
		req.on('error', () => done('error'));
		req.end();
	});
}

/**
 * Route table interception: `routes` maps a matcher to a handler that
 * returns {status, body, contentType}, {abort: true} (network failure) or
 * null to let the request through. Every request lands in `log`.
 */
async function intercept(page, routes, log) {
	await page.setRequestInterception(true);
	page.on('request', (req) => {
		const u = new URL(req.url());
		log.push({ method: req.method(), path: u.pathname + u.search, origin: u.origin, body: req.postData() ?? null });
		for (const [match, handler] of routes) {
			if (match(req, u)) {
				const r = handler(req, u);
				if (r?.abort) {
					req.abort('failed').catch(() => undefined);
					return;
				}
				if (r) {
					req
						.respond({
							status: r.status ?? 200,
							contentType: r.contentType ?? 'application/json',
							body: typeof r.body === 'string' ? r.body : JSON.stringify(r.body),
						})
						.catch(() => undefined);
					return;
				}
			}
		}
		req.continue().catch(() => undefined);
	});
}

async function newPage(browser) {
	const page = await browser.newPage();
	await page.setViewport({ width: 1280, height: 900, isMobile: false });
	return page;
}

/**
 * Forget the persisted language choice (contract C-M: the veer_lang cookie
 * a language button writes survives reloads and carries across pages), so
 * a page shows its own route default — /driver Gujarati, the rest English.
 */
async function forgetLang(page, origin) {
	await page.deleteCookie({ name: 'veer_lang', url: origin }).catch(() => undefined);
}

const browser = await puppeteer.launch({ executablePath, headless: true, args: SWIFTSHADER_ARGS });
let deckProc = null;
mkdirSync(path.join(HERE, 'artifacts'), { recursive: true });

try {
	const portArg = process.argv[2] ? Number(new URL(process.argv[2]).port) : 5199;
	const origin = process.argv[2] ?? `http://127.0.0.1:${portArg}`;
	deckProc = await ensureOrigin(origin, portArg);
	ok('deck-origin up', true, origin);

	// ── 0. Edge: headers, documents, favicon, WS upgrade ─────────────────
	{
		const head = await fetch(`${origin}/citizen`, { method: 'HEAD' });
		const csp = head.headers.get('content-security-policy') ?? '';
		ok('edge: CSP on /citizen', csp.includes("default-src 'self'"), csp.slice(0, 60));
		ok(
			'edge: no framing (frame-ancestors none / x-frame-options)',
			csp.includes("frame-ancestors 'none'") || head.headers.get('x-frame-options') === 'DENY',
		);
		ok('edge: nosniff', head.headers.get('x-content-type-options') === 'nosniff');
		ok('edge: referrer-policy', !!head.headers.get('referrer-policy'), head.headers.get('referrer-policy') ?? '');
		ok('edge: public CSP has no eval', !csp.includes("'unsafe-eval'"));

		const html = await (await fetch(`${origin}/citizen`)).text();
		ok('edge: exactly one <title> on /citizen', (html.match(/<title>/g) ?? []).length === 1);
		ok('edge: /citizen server-renders daylight (no dark flash)', /<html[^>]*data-theme="daylight"/.test(html));
		const driverHtml = await (await fetch(`${origin}/driver`)).text();
		ok('edge: exactly one <title> on /driver', (driverHtml.match(/<title>/g) ?? []).length === 1);
		ok('edge: /driver server-renders lang="gu"', /<html[^>]*lang="gu"/.test(driverHtml));
		ok('edge: inline scripts carry the CSP nonce', /<script nonce="/.test(html) && !/<script>/.test(html));

		const missingHtml = await (await fetch(`${origin}/no-such-page-smoke`)).text();
		ok('edge: an unknown path still ships exactly one <title>', (missingHtml.match(/<title>/g) ?? []).length === 1);

		const fav = await fetch(`${origin}/favicon.png`);
		ok('edge: /favicon.png 200', fav.status === 200 && (fav.headers.get('content-type') ?? '').includes('png'));

		const ws = await upgradeProbe(origin, '/api/convoy/ws/event/x');
		ok('edge: WS upgrade → 501 in < 0.2 s', ws.status === 501 && ws.seconds < 0.2, `${ws.status} in ${ws.seconds.toFixed(3)} s`);
	}

	// ── 1. /citizen — the REAL public feed ───────────────────────────────
	{
		const page = await newPage(browser);
		const log = [];
		await intercept(page, [], log);
		await page.goto(`${origin}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="citizen"]', { timeout: 30000 });
		ok('/citizen advisory mounts', true);

		const theme = await page.evaluate(() => document.documentElement.dataset.theme ?? '');
		ok('/citizen daylight theme', theme === 'daylight', `data-theme=${theme}`);

		const t0 = Date.now();
		const ready = await page
			.waitForSelector('[data-map-ready="yes"]', { timeout: 15000 })
			.then(() => true)
			.catch(() => false);
		const hook = await page.evaluate(() => window.__CITIZEN_MAP__ ?? null);
		ok('/citizen map ready within 15 s', ready && hook?.ready === true, `${Date.now() - t0} ms · ${JSON.stringify(hook)}`);
		await sleep(2500);
		const tileReqs = log.filter((r) => r.origin.includes('tile.openstreetmap.org')).length;
		ok('/citizen map requested tiles', tileReqs > 0, `${tileReqs} tile requests`);

		await page.waitForFunction(() => document.querySelector('[data-screen="citizen"]')?.dataset.feed !== 'connecting', { timeout: 20000 });
		const feedState = await page.$eval('[data-screen="citizen"]', (el) => el.dataset.feed);
		const kinds = await page.$$eval('.event-card', (els) => els.map((e) => e.getAttribute('data-kind')));
		ok('/citizen feed answered', feedState === 'live' || feedState === 'stale', `feed=${feedState} · ${kinds.length} card(s): ${kinds.join(',') || 'none'}`);
		ok('/citizen no pursuit / crime card', !kinds.some((k) => k === 'vehicle_pursuit' || k === 'crime_incident'));

		const bar = await text(page, '[data-shell="citizen-bar"]');
		ok('/citizen bar: no band code, no SHARE', !BAND_CODE.test(bar) && !/share/i.test(bar), bar.trim().slice(0, 80));
		ok('/citizen bar: no operator search', !(await page.$('[data-shell="command-search"]')));
		ok('/citizen no /ops link', !(await page.$('a[href="/ops"]')));

		for (const [code, script] of [['en', null], ['hi', HI], ['gu', GU]]) {
			await page.click(`[data-shell="citizen-bar"] .lang button[lang="${code}"]`);
			await sleep(500);
			const body = await page.evaluate(() => document.body.innerText);
			const htmlLang = await page.evaluate(() => document.documentElement.lang);
			const raw = body.match(RAW_KEY);
			ok(`/citizen ${code}: no raw kind_/consequence_ key`, !raw, raw ? raw[0] : 'clean');
			ok(`/citizen ${code}: <html lang> follows`, htmlLang === code, htmlLang);
			if (script) ok(`/citizen ${code}: script on the page`, script.test(await text(page, '.kicker')));
		}
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'citizen-gu.png') });

		const apiCalls = log.filter((r) => r.path.startsWith('/api/'));
		const operator = log.filter((r) => /\/api\/v1\/(vehicles|enforcement|dispatches|city)/.test(r.path));
		const offList = apiCalls.filter((r) => !PUBLIC_API_OK.test(r.path));
		ok('/citizen makes no /vehicles, /enforcement, /dispatches call', operator.length === 0, `${operator.length} operator calls`);
		ok('/citizen API traffic is public-only', offList.length === 0, offList.slice(0, 3).map((r) => r.path).join(' | ') || `${apiCalls.length} public calls`);
		ok('/citizen never contacts the emulator', !log.some((r) => r.origin.includes(':8090') || r.path.startsWith('/api/emulator')));
		await page.close();
	}

	// ── 1b. /citizen — STUBBED feed: holds, provenance, alternate, defence ─
	const STUB_EVENT = {
		event_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'procession',
		status: 'active',
		window_start: new Date(Date.now() - 600_000).toISOString(),
		window_end: new Date(Date.now() + 7_200_000).toISOString(),
		stretches: [
			{ segment_id: 1003, road_name: 'CG Road (West)', junction_id: 6 },
			{ segment_id: 1004, road_name: 'CG Road (East)', junction_id: 6 },
		],
		phases: [{ label: 'procession window', window_start: null, window_end: null, advance_closure_sec: 150 }],
		hold: { minutes: 3, advance_closure_sec: 150, source: 'closure_policy' },
		alternate: { road_names: ['Ellis Bridge Road'], segment_ids: [1039], source: 'k_shortest_paths' },
		tier: 'watch',
		unavailable: [],
	};
	// A PLANNED convoy whose window has started, on the oracle's doctrine
	// baseline (GET /adaptive/params source 'baseline'), graph read failed.
	const BASELINE_EVENT = {
		...STUB_EVENT,
		event_id: 'aaaaaaaa-cccc-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'protected_movement',
		status: 'planning',
		stretches: [{ segment_id: 1026, road_name: 'Ashram Road', junction_id: 4 }],
		phases: [],
		hold: { minutes: 2, advance_closure_sec: 120, source: 'adaptive_baseline' },
		alternate: null,
		unavailable: ['alternate'],
	};
	// A gathering whose params read failed: hold unavailable, not "not published".
	const UNREADABLE_EVENT = {
		...STUB_EVENT,
		event_id: 'aaaaaaaa-dddd-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'mass_gathering',
		phases: [],
		hold: { minutes: null, advance_closure_sec: null, source: null },
		alternate: null,
		unavailable: ['hold'],
	};
	const feedBody = (events) => ({ as_of: new Date().toISOString(), events, tier: events.length ? 'watch' : 'calm' });
	{
		const page = await newPage(browser);
		const log = [];
		const smuggled = { ...STUB_EVENT, event_id: 'bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee', event_kind: 'vehicle_pursuit' };
		await intercept(page, [[(_r, u) => u.pathname === '/api/public/events', () => ({ body: feedBody([STUB_EVENT, smuggled, BASELINE_EVENT, UNREADABLE_EVENT]) })]], log);
		await page.goto(`${origin}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('.event-card', { timeout: 20000 });
		await page.click('[data-shell="citizen-bar"] .lang button[lang="en"]');
		await sleep(300);
		const kinds = await page.$$eval('.event-card', (els) => els.map((e) => e.getAttribute('data-kind')));
		ok('/citizen drops a pursuit even if a feed carried one', kinds.join(',') === 'procession,protected_movement,mass_gathering', kinds.join(','));
		const holds = await page.$$eval(`.event-card[data-event="${STUB_EVENT.event_id}"] .hold`, (els) => els.map((e) => e.textContent?.replace(/\s+/g, ' ').trim()));
		ok('/citizen hold rows with signage names', holds.length === 2 && holds[0].includes('CG Road (West)') && holds[0].includes('3'), holds[0] ?? '');
		ok('/citizen hold provenance shown', (await page.$('[data-hold-source="closure_policy"]')) !== null);
		ok('/citizen graph alternate shown', (await text(page, '[data-alternate="graph"]')).includes('Ellis Bridge Road'));
		const baselineCard = `.event-card[data-event="${BASELINE_EVENT.event_id}"]`;
		const baselineTxt = await text(page, `${baselineCard} [data-hold-source="adaptive_baseline"]`);
		ok('/citizen oracle baseline labelled as the standard length, never "live"', /standard hold length/i.test(baselineTxt) && /not yet adapted/i.test(baselineTxt), baselineTxt.trim());
		ok('/citizen a failed graph read says "unavailable", not "no alternate"', (await page.$(`${baselineCard} [data-alternate="unavailable"]`)) !== null && (await page.$(`${baselineCard} [data-alternate="none"]`)) === null);
		const plannedStates = await page.$$eval(`${baselineCard} .hold`, (els) => els.map((e) => e.getAttribute('data-hold-state')));
		ok('/citizen a planned event whose window started never reads "holding now"', plannedStates.length === 1 && !plannedStates.includes('now'), plannedStates.join(','));
		ok('/citizen a failed hold read says "unavailable", not "not published"', (await page.$(`.event-card[data-event="${UNREADABLE_EVENT.event_id}"] [data-hold-state="unavailable"]`)) !== null);
		const activeStates = await page.$$eval(`.event-card[data-event="${STUB_EVENT.event_id}"] .hold`, (els) => els.map((e) => e.getAttribute('data-hold-state')));
		ok('/citizen an active event holds now', activeStates[0] === 'now', activeStates.join(','));
		ok('/citizen bar tier is plain language', (await page.$eval('[data-shell="citizen-bar"] [data-tier]', (el) => el.getAttribute('data-tier'))) === 'watch');
		await page.type('[data-testid="subscribe-phone"]', '9876543210');
		await page.select('[data-testid="subscribe-lang"]', 'gu');
		await page.click('.subscribe [data-prim="button"]');
		await page.waitForSelector('[data-testid="subscribe-preview"]', { timeout: 5000 });
		const preview = await text(page, '[data-testid="subscribe-preview"] p');
		ok('/citizen subscribe preview in the chosen alert language', GU.test(preview) && preview.includes('Ellis Bridge Road'), preview.slice(0, 60));
		await page.close();
	}

	// ── 1c. /citizen — EMULATED rows, the Later list, IST clocks, phase labels ─
	const EMU_EVENT = {
		...STUB_EVENT,
		event_id: 'aaaaaaaa-eeee-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'procession',
		status: 'active',
		emulated: true,
		phases: [
			{ label: 'assembly', window_start: new Date(Date.now() - 1_800_000).toISOString(), window_end: new Date(Date.now() + 1_800_000).toISOString(), advance_closure_sec: 150 },
			{ label: 'Procession', window_start: new Date(Date.now() + 1_800_000).toISOString(), window_end: null, advance_closure_sec: 150 },
		],
	};
	const LATER_START = Date.now() + 26 * 3600_000; // tomorrow in IST, whatever the hour
	const LATER_EVENT = {
		event_id: 'aaaaaaaa-ffff-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'work_zone',
		status: 'approved',
		starts_at: new Date(LATER_START).toISOString(),
		window_end: new Date(LATER_START + 4 * 3600_000).toISOString(),
		road_names: ['Ashram Road', 'Relief Road'],
		emulated: false,
	};
	// A protected (VVIP) movement beyond the horizon: date only, its roads withheld.
	const LATER_VVIP = {
		event_id: 'aaaaaaaa-9999-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'protected_movement',
		status: 'approved',
		starts_at: new Date(LATER_START + 3600_000).toISOString(),
		window_end: null,
		road_names: [],
		roads_withheld: true,
		emulated: false,
	};
	const IST_HM = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
	const IST_DAY = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short' });
	{
		const page = await newPage(browser);
		await page.emulateTimezone('America/New_York'); // IST must not depend on the browser's zone
		const log = [];
		await intercept(
			page,
			[[(_r, u) => u.pathname === '/api/public/events', () => ({ body: { as_of: new Date().toISOString(), events: [EMU_EVENT], later: [LATER_EVENT, LATER_VVIP], tier: 'calm', emulated_labelling: true } })]],
			log,
		);
		await page.goto(`${origin}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		const card = `.event-card[data-event="${EMU_EVENT.event_id}"]`;
		await page.waitForSelector(card, { timeout: 20000 });
		const EMU_BADGE = { en: 'SIMULATED FEED · EMULATED', hi: 'सिम्युलेटेड फ़ीड · EMULATED', gu: 'સિમ્યુલેટેડ ફીડ · EMULATED' };
		for (const code of ['en', 'hi', 'gu']) {
			await page.click(`[data-shell="citizen-bar"] .lang button[lang="${code}"]`);
			await sleep(400);
			const badge = await text(page, `${card} [data-source-badge="emulated"]`);
			ok(`/citizen ${code}: an event-emulator row is badged '${EMU_BADGE[code]}'`, badge.includes(EMU_BADGE[code]) && (await page.$eval(card, (e) => e.dataset.source)) === 'emulated', badge.trim());
			const hero = await page.$eval('.hero', (e) => ({ emu: e.querySelector('[data-source-badge="emulated"]')?.textContent ?? '', live: !!e.querySelector('[data-source-badge="live"]') }));
			ok(`/citizen ${code}: the hero is never LIVE over emulated rows`, hero.emu.includes(EMU_BADGE[code]) && !hero.live, hero.emu.trim());
			ok(`/citizen ${code}: the emulated note is shown`, !!(await page.$('[data-feed-note="emulated"]')));
		}
		await page.click('.accordion .acc-head');
		await sleep(300);
		const labelsGu = await page.$$eval('.tl-label', (els) => els.map((e) => ({ key: e.getAttribute('data-phase-label'), text: e.textContent?.replace(/\s+/g, ' ').trim() ?? '' })));
		const assemblyGu = labelsGu.find((l) => l.key === 'assembly')?.text ?? '';
		const processionGu = labelsGu.find((l) => l.key === 'Procession')?.text ?? '';
		ok('/citizen gu: phase labels are localized (no raw English label)', assemblyGu.includes('એકત્રીકરણ') && !/assembly/i.test(assemblyGu) && processionGu.includes('સરઘસ') && !/procession/i.test(processionGu), `${assemblyGu} | ${processionGu}`);
		await page.click('[data-shell="citizen-bar"] .lang button[lang="en"]');
		await sleep(400);
		const clocks = await page.$$eval('.tl-clock[data-ts]', (els) => els.map((e) => ({ ts: e.getAttribute('data-ts'), text: e.textContent?.trim() ?? '' })));
		const istOk = clocks.length === 2 && clocks.every((c) => c.text.endsWith(IST_HM.format(Date.parse(c.ts))));
		ok('/citizen timeline clocks are IST (browser in America/New_York)', istOk, clocks.map((c) => `${c.text} ← ${c.ts}`).join(' · '));
		const laterRow = await page.$eval(`[data-testid="citizen-later"] .later-row[data-event="${LATER_EVENT.event_id}"]`, (e) => ({ starts: Number(e.dataset.starts), when: e.querySelector('.later-when')?.textContent?.trim() ?? '', text: e.textContent?.replace(/\s+/g, ' ').trim() ?? '' })).catch(() => null);
		ok("/citizen an event starting beyond 2 h is listed under 'Later' with its IST date", !!laterRow && laterRow.starts === Date.parse(LATER_EVENT.starts_at) && laterRow.when.includes(IST_DAY.format(LATER_START)) && laterRow.when.endsWith(IST_HM.format(LATER_START)) && laterRow.text.includes('Ashram Road / Relief Road'), laterRow ? `${laterRow.when} · ${laterRow.text.slice(0, 80)}` : 'no Later row');
		ok('/citizen …and it is not a hold card', !(await page.$(`.event-card[data-event="${LATER_EVENT.event_id}"]`)));
		const vvipRow = await page.$eval(`[data-testid="citizen-later"] .later-row[data-event="${LATER_VVIP.event_id}"]`, (e) => ({ withheld: !!e.querySelector('[data-roads-withheld="true"]'), text: e.textContent?.replace(/\s+/g, ' ').trim() ?? '' })).catch(() => null);
		ok('/citizen a VVIP movement in Later shows its date, and says its route comes within 2 h (no roads)', !!vvipRow && vvipRow.withheld && /route announced within 2 hours/.test(vvipRow.text), vvipRow?.text.slice(0, 90) ?? 'no VVIP Later row');
		const mapped = await page.waitForFunction(() => (window.__CITIZEN_MAP__?.routeCount ?? 0) > 0, { timeout: 15000 }).then(() => true).catch(() => false);
		const hook = await page.evaluate(() => window.__CITIZEN_MAP__ ?? null);
		ok('/citizen an emulated event’s routes never draw as live', mapped && hook?.liveRouteCount === 0, JSON.stringify({ routeCount: hook?.routeCount, liveRouteCount: hook?.liveRouteCount }));
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'citizen-emulated.png') });
		await forgetLang(page, origin);
		await page.close();
	}
	{
		const res = await fetch(`${origin}/api/public/events`);
		const body = await res.json().catch(() => null);
		ok('/api/public/events says emulated_labelling: true (C-M)', body?.emulated_labelling === true && Array.isArray(body?.later) && (body?.events ?? []).every((e) => typeof e.emulated === 'boolean'), `HTTP ${res.status} · ${(body?.events ?? []).length} event(s) · ${(body?.later ?? []).length} later`);
	}

	// ── 1d. phone widths: nothing prints over a road name (360 / 412 px, EN · HI · GU) ─
	// The daylight type floor (13–14 px) and the longer state words must
	// WRAP a hold row — the window and state drop below the road name —
	// never squeeze the name to nothing under them.
	const PHONE_EVENT = {
		...STUB_EVENT,
		event_id: 'aaaaaaaa-abab-4ccc-8ddd-eeeeeeeeeeee',
		stretches: [
			{ segment_id: 1026, road_name: 'Relief Road', junction_id: 4 },
			{ segment_id: 1033, road_name: 'Sardar Patel Ring Road (West)', junction_id: 9 },
		],
	};
	/** Each hold row as laid out: the road name's box vs its text, and what overlaps it. */
	const holdLayout = () =>
		[...document.querySelectorAll('.hold')].map((row) => {
			const seg = row.querySelector('.seg');
			const r = seg.getBoundingClientRect();
			const overlaps = [...row.querySelectorAll('.window, .state')].filter((el) => {
				const o = el.getBoundingClientRect();
				return Math.min(r.right, o.right) - Math.max(r.left, o.left) > 1 && Math.min(r.bottom, o.bottom) - Math.max(r.top, o.top) > 1;
			}).length;
			const box = row.getBoundingClientRect();
			const outside = [...row.children].filter((c) => c.getBoundingClientRect().right > box.right + 1).length;
			return { name: seg.textContent.trim(), box: Math.round(seg.clientWidth), text: seg.scrollWidth, overlaps, outside };
		});
	{
		// One page, resized: a CSS-pixel phone viewport (no isMobile flip — that
		// forces a reload puppeteer waits out on its own clock).
		const page = await browser.newPage();
		await intercept(page, [[(_r, u) => u.pathname === '/api/public/events', () => ({ body: { as_of: new Date().toISOString(), events: [PHONE_EVENT, EMU_EVENT, BASELINE_EVENT], later: [LATER_EVENT, LATER_VVIP], tier: 'watch', emulated_labelling: true } })]], []);
		for (const [w, h] of [[360, 780], [412, 915]]) {
			await page.setViewport({ width: w, height: h });
			await page.goto(`${origin}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60000 });
			await page.waitForSelector('.hold', { timeout: 20000 });
			for (const code of ['en', 'hi', 'gu']) {
				await page.click(`[data-shell="citizen-bar"] .lang button[lang="${code}"]`);
				await sleep(400);
				const rows = await page.evaluate(holdLayout);
				const bad = rows.filter((r) => r.text > r.box + 1 || r.overlaps > 0 || r.outside > 0 || r.box < 48);
				const spill = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
				ok(
					`/citizen ${w} px ${code}: every hold row keeps its road name readable (no overlap, no clip, no sideways scroll)`,
					rows.length >= 5 && bad.length === 0 && spill <= 1,
					bad.length ? JSON.stringify(bad[0]) : `${rows.length} rows · narrowest name box ${Math.min(...rows.map((r) => r.box))} px · spill ${spill} px`,
				);
				// The SIMULATED FEED · EMULATED badge wraps under the title — never past its card's edge.
				const badges = await page.$$eval('.hero .source-badge, .event-card .source-badge, .later-row .source-badge', (els) =>
					els.map((b) => {
						const box = b.closest('.hero, .event-card, .later')?.getBoundingClientRect();
						return { inside: !!box && b.getBoundingClientRect().right <= box.right + 1, text: b.textContent.trim() };
					}),
				);
				const outBadge = badges.find((b) => !b.inside);
				// …and the emulated note beside the badge reads at full width under it, never as a sliver.
				const noteW = await page.$eval('[data-feed-note="emulated"] .note-text', (e) => Math.round(e.getBoundingClientRect().width)).catch(() => 0);
				ok(
					`/citizen ${w} px ${code}: every source badge stays inside its card; the emulated note reads at full width`,
					badges.length >= 3 && !outBadge && noteW >= 200,
					outBadge ? `'${outBadge.text}' past the card edge` : `${badges.length} badges inside · note ${noteW} px wide`,
				);
				if (code === 'gu') await page.screenshot({ path: path.join(HERE, 'artifacts', `citizen-phone-${w}-gu.png`) });
			}
			// /driver on the same phone: the stretch picker (its options carry
			// the longest road names) stays inside its card — nothing sent.
			await page.goto(`${origin}/driver`, { waitUntil: 'domcontentloaded', timeout: 60000 });
			const picker = await page.waitForSelector('[data-testid="stretch-select"]', { timeout: 20000 }).then(() => true).catch(() => false);
			for (const code of ['gu', 'en']) {
				await page.click(`[data-shell="citizen-bar"] .lang button[lang="${code}"]`);
				await sleep(400);
				const fit = await page.evaluate(() => {
					const sel = document.querySelector('[data-testid="stretch-select"]');
					const card = sel?.closest('.request-card');
					if (!sel || !card) return null;
					return { select: Math.round(sel.getBoundingClientRect().right), card: Math.round(card.getBoundingClientRect().right), spill: document.documentElement.scrollWidth - window.innerWidth };
				});
				ok(`/driver ${w} px ${code}: the stretch picker stays inside its card`, picker && !!fit && fit.select <= fit.card + 1 && fit.spill <= 1, JSON.stringify(fit));
			}
		}
		await forgetLang(page, origin);
		await page.close();
	}

	// ── 2. /driver — live request → human approval → window ─────────────
	if (!LIVE_WRITES) {
		const page = await newPage(browser);
		const log = [];
		const D = 'dddddddd-1111-4222-8333-944455556666';
		let polls = 0;
		let posted = null;
		let feedEvents = [STUB_EVENT];
		// The officer approves on the 3rd poll; the emulated unit is then
		// 155 s from the junction, so the 150 s window opens ~5 s later.
		let approvedAt = null;
		let arrivalAt = null;
		await intercept(
			page,
			[
				[(_r, u) => u.pathname === '/api/public/events', () => ({ body: feedBody(feedEvents) })],
				[
					(r, u) => r.method() === 'POST' && u.pathname === `/api/v1/events/${STUB_EVENT.event_id}/dispatch`,
					(r) => {
						posted = JSON.parse(r.postData() ?? '{}');
						return {
							body: { dispatch_id: D, event_id: STUB_EVENT.event_id, agency: 'ambulance', unit_id: posted.unit_id, status: 'requested' },
						};
					},
				],
				[
					(_r, u) => u.pathname === `/api/public/dispatches/${D}`,
					() => {
						polls += 1;
						const now = Date.now();
						if (polls >= 3 && approvedAt === null) {
							approvedAt = now - 2000;
							arrivalAt = now + 155_000;
						}
						const approved = approvedAt !== null;
						return {
							body: {
								as_of: new Date(now).toISOString(),
								dispatch: {
									dispatch_id: D,
									event_id: STUB_EVENT.event_id,
									status: approved ? 'approved' : 'requested',
									unit_id: '108-14',
									approved_at: approved ? new Date(approvedAt).toISOString() : null,
								},
								unit: approved
									? {
											status: 'approved',
											eta_sec: Math.max(0, (arrivalAt - now) / 1000),
											eta_at: new Date(now).toISOString(),
											arrived: false,
											position: [72.556, 23.031],
											emulated: true,
											source: 'emulated_dispatch_projection',
										}
									: null,
							},
						};
					},
				],
			],
			log,
		);
		await forgetLang(page, origin);
		await page.goto(`${origin}/driver`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="driver"][data-mode="live"]', { timeout: 20000 });
		ok('/driver live mode with a live public event', true);
		const driverTheme = await page.evaluate(() => document.documentElement.dataset.theme ?? '');
		ok('/driver daylight theme', driverTheme === 'daylight', `data-theme=${driverTheme}`);
		ok('/driver Gujarati first', GU.test(await text(page, '.head h1')));

		const tick1 = Number(await page.$eval('[data-golden-seconds]', (el) => el.getAttribute('data-golden-seconds')));
		await sleep(2300);
		const tick2 = Number(await page.$eval('[data-golden-seconds]', (el) => el.getAttribute('data-golden-seconds')));
		ok('/driver golden-hour ticks', tick2 > tick1, `${tick1} → ${tick2}`);

		const options = await page.$$eval('[data-testid="stretch-select"] option', (els) => els.map((o) => ({ v: o.value, t: o.textContent })));
		ok('/driver stretches labelled with road names', options.some((o) => o.t?.includes('CG Road (West)')), options.map((o) => o.t).join(' | '));
		await page.select('[data-testid="stretch-select"]', `${STUB_EVENT.event_id}|1003`);
		await page.type('[data-testid="unit-input"]', '108-14');
		await sleep(250);
		await page.click('.request-row [data-prim="button"]');

		await page.waitForSelector('[data-testid="awaiting-officer"]', { timeout: 10000 });
		ok('/driver request body: ambulance · 108 unit · junction · 108-driver',
			posted?.agency === 'ambulance' && posted?.unit_id === '108-14' && posted?.waypoint_junction === 6 && posted?.requested_by === '108-driver',
			JSON.stringify(posted));
		const awaitingTxt = await text(page, '[data-testid="awaiting-officer"]');
		ok('/driver shows awaiting duty officer', awaitingTxt.length > 0 && (await page.$eval('[data-screen="driver"]', (el) => el.dataset.phase)) === 'requested', awaitingTxt.trim().slice(0, 60));
		const approveButtons = await page.$$eval('[data-screen="driver"] button', (els) =>
			els.filter((b) => /approve|મંજૂર|मंज़ूर/i.test(b.textContent ?? '')).length,
		);
		ok('/driver has NO approve button (the officer decides on /ops)', approveButtons === 0, `${approveButtons} approve buttons`);
		ok('/driver awaiting card offers no action', (await page.$$('[data-testid="awaiting-officer"] button')).length === 0);

		await page.waitForSelector('[data-testid="window-primed"]', { timeout: 15000 });
		const opensIn = Number(await page.$eval('[data-opens-in]', (el) => el.getAttribute('data-opens-in')));
		const primedSrc = await page.$eval('[data-testid="window-primed"] .window-source', (el) => ({ src: el.getAttribute('data-window-source'), anchor: el.getAttribute('data-window-anchor') }));
		ok('/driver approval primes the window: opens one window-length before the projected arrival', opensIn >= 1 && opensIn <= 6 && primedSrc.anchor === 'eta', `opens in ${opensIn} s · ${JSON.stringify(primedSrc)} · after ${polls} polls`);
		ok('/driver window length provenance = the event closure plan', primedSrc.src === 'closure_policy');
		const primedMap = await page.waitForFunction(() => window.__CITIZEN_MAP__?.highlight === 1003 && window.__CITIZEN_MAP__?.unit === true, { timeout: 15000 }).then(() => true).catch(() => false);
		ok('/driver approved: granted stretch + approaching unit already on the map', primedMap);

		await page.waitForSelector('[data-testid="window-open"]', { timeout: 15000 });
		const cd1 = Number(await page.$eval('[data-countdown]', (el) => el.getAttribute('data-countdown')));
		await sleep(2300);
		const cd2 = Number(await page.$eval('[data-countdown]', (el) => el.getAttribute('data-countdown')));
		ok('/driver window counts down to the unit reaching the junction', cd1 <= 150 && cd1 >= 140 && cd2 < cd1, `${cd1} → ${cd2} (150 s window closing on arrival)`);
		ok('/driver unit labelled EMULATED', (await text(page, '[data-testid="window-open"] .unit')).includes('EMULATED'));
		const mapHook = await page.waitForFunction(() => window.__CITIZEN_MAP__?.highlight === 1003 && window.__CITIZEN_MAP__?.unit === true, { timeout: 15000 }).then(() => true).catch(() => false);
		ok('/driver granted stretch + tracked unit on the map', mapHook);
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'driver-open.png') });

		// The event leaves the public feed mid-window: the request UI stays.
		feedEvents = [];
		const feedPolled = log.filter((r) => r.path.startsWith('/api/public/events')).length;
		await sleep(11_000); // one public-feed poll (10 s)
		const polledAgain = log.filter((r) => r.path.startsWith('/api/public/events')).length > feedPolled;
		const stillOpen = (await page.$('[data-testid="window-open"]')) !== null;
		const mode = await page.$eval('[data-screen="driver"]', (el) => el.dataset.mode);
		ok('/driver the request survives its event leaving the feed', polledAgain && stillOpen && mode === 'live', `feed re-polled=${polledAgain} · open=${stillOpen} · mode=${mode}`);
		ok('/driver made no operator read', !log.some((r) => /\/api\/v1\/(vehicles|enforcement|dispatches|city)/.test(r.path)));
		await page.close();
	} else {
		// Sandbox acceptance: a REAL request against the origin's stack.
		const page = await newPage(browser);
		const log = [];
		await intercept(page, [], log);
		await forgetLang(page, origin);
		await page.goto(`${origin}/driver`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="driver"][data-mode="live"]', { timeout: 20000 });
		const first = await page.$eval('[data-testid="stretch-select"]', (el) => [...el.querySelectorAll('option')].find((o) => o.value.includes('|'))?.value ?? '');
		ok('/driver (live writes) live stretch available', first !== '', first);
		await page.select('[data-testid="stretch-select"]', first);
		await page.type('[data-testid="unit-input"]', '108-77');
		await sleep(250);
		await page.click('.request-row [data-prim="button"]');
		await page.waitForSelector('[data-testid="awaiting-officer"]', { timeout: 15000 });
		const post = log.find((r) => r.method === 'POST' && /\/api\/v1\/events\/[^/]+\/dispatch$/.test(r.path));
		ok('/driver (live writes) POSTed the dispatch request', !!post, post?.path ?? '');
		const approveButtons = await page.$$eval('[data-screen="driver"] button', (els) => els.filter((b) => /approve|મંજૂર|मंज़ूर/i.test(b.textContent ?? '')).length);
		ok('/driver (live writes) awaiting duty officer, no approve button', approveButtons === 0);
		await sleep(4000);
		const statusPoll = log.filter((r) => r.path.startsWith('/api/public/dispatches/'));
		ok('/driver (live writes) follows its own request', statusPoll.length > 0, `${statusPoll.length} status polls`);
		await page.close();
	}

	// ── 2b. /driver — scripted fallback only without a live event ────────
	{
		const page = await newPage(browser);
		const log = [];
		let feedEvents = [];
		await intercept(page, [[(_r, u) => u.pathname === '/api/public/events', () => ({ body: feedBody(feedEvents) })]], log);
		await forgetLang(page, origin);
		await page.goto(`${origin}/driver`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="driver"][data-mode="scripted"]', { timeout: 20000 });
		ok('/driver scripted fallback when no live event', !!(await page.$('[data-testid="scripted-note"]')));
		const first = await page.$eval('[data-testid="stretch-select"]', (el) => [...el.querySelectorAll('option')].find((o) => o.value.includes('|'))?.value ?? '');
		await page.select('[data-testid="stretch-select"]', first);
		await sleep(250);
		await page.click('.request-row [data-prim="button"]');
		await page.waitForSelector('[data-testid="proposal"]', { timeout: 10000 });
		ok('/driver scripted proposal has no approve button', (await page.$$('[data-testid="proposal"] button')).length === 0);
		await page.waitForSelector('[data-testid="window-open"]', { timeout: 10000 });
		ok('/driver scripted window opens (labelled, nothing sent)', !log.some((r) => r.method === 'POST'), `${log.filter((r) => r.method === 'POST').length} POSTs`);
		// A live event appearing mid-script must not hijack the running script.
		feedEvents = [STUB_EVENT];
		const before = log.filter((r) => r.path.startsWith('/api/public/events')).length;
		await sleep(11_000);
		const repolled = log.filter((r) => r.path.startsWith('/api/public/events')).length > before;
		const mode = await page.$eval('[data-screen="driver"]', (el) => el.dataset.mode);
		ok('/driver a live event mid-script does not hijack the scripted run', repolled && mode === 'scripted' && (await page.$('[data-testid="window-open"]')) !== null, `re-polled=${repolled} · mode=${mode}`);
		await page.close();
	}

	// ── 2c. /driver — EMULATED never offered; ONE request button after a cancel ─
	{
		const page = await newPage(browser);
		const log = [];
		const D2 = 'dddddddd-2222-4222-8333-944455556666';
		let polls = 0;
		await intercept(
			page,
			[
				[(_r, u) => u.pathname === '/api/public/events', () => ({ body: { as_of: new Date().toISOString(), events: [STUB_EVENT, EMU_EVENT], later: [], tier: 'watch', emulated_labelling: true } })],
				[
					(r, u) => r.method() === 'POST' && u.pathname === `/api/v1/events/${STUB_EVENT.event_id}/dispatch`,
					(r) => {
						const b = JSON.parse(r.postData() ?? '{}');
						return { body: { dispatch_id: D2, event_id: STUB_EVENT.event_id, agency: 'ambulance', unit_id: b.unit_id, status: 'requested' } };
					},
				],
				[
					(_r, u) => u.pathname === `/api/public/dispatches/${D2}`,
					() => {
						polls += 1;
						// the duty officer cancels the request on the 2nd poll
						return { body: { as_of: new Date().toISOString(), dispatch: { dispatch_id: D2, event_id: STUB_EVENT.event_id, status: polls >= 2 ? 'cancelled' : 'requested', unit_id: '108-15', approved_at: null }, unit: null } };
					},
				],
			],
			log,
		);
		await forgetLang(page, origin);
		await page.goto(`${origin}/driver`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="driver"][data-mode="live"]', { timeout: 20000 });
		const opts = await page.$$eval('[data-testid="stretch-select"] option', (els) => els.map((o) => o.value));
		ok('/driver an EMULATED corridor event is never offered', opts.some((v) => v.startsWith(STUB_EVENT.event_id)) && !opts.some((v) => v.startsWith(EMU_EVENT.event_id)), opts.filter(Boolean).join(' | '));
		const withheld = await page.$eval('[data-testid="driver-emulated-withheld"]', (e) => ({ count: e.dataset.count, text: e.textContent?.replace(/\s+/g, ' ').trim() ?? '' })).catch(() => null);
		ok('/driver …and says so (SIMULATED FEED · EMULATED + count, in Gujarati)', withheld?.count === '1' && withheld.text.includes('સિમ્યુલેટેડ ફીડ · EMULATED') && GU.test(withheld.text), withheld?.text.slice(0, 90) ?? 'no note');
		await page.select('[data-testid="stretch-select"]', `${STUB_EVENT.event_id}|1003`);
		await page.type('[data-testid="unit-input"]', '108-15');
		await sleep(250);
		await page.click('.request-row [data-prim="button"]');
		const cancelled = await page.waitForSelector('[data-screen="driver"][data-phase="cancelled"]', { timeout: 15000 }).then(() => true).catch(() => false);
		const requestButtons = await page.$$eval('[data-screen="driver"] button', (els) => els.filter((b) => /કોરિડોર સમયની વિનંતી|નવી વિનંતી/.test(b.textContent ?? '')).map((b) => b.textContent?.trim()));
		ok('/driver after the officer cancels, exactly ONE request button remains', cancelled && requestButtons.length === 1, `${requestButtons.length}: ${requestButtons.join(' | ')}`);
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'driver-cancelled.png') });
		await page.close();
	}

	// ── 3. /chat — mocked C-D stream, session, legacy envelope, degraded ──
	{
		const page = await newPage(browser);
		const log = [];
		const bodies = [];
		let mode = 'cd';
		const ndjson = (lines) => lines.map((l) => JSON.stringify(l)).join('\n') + '\n';
		const BOARD = {
			events: [
				{
					event_id: 'cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee',
					event_kind: 'protected_movement',
					protection_class: 'Z',
					title: 'Z convoy — smoke',
					name: 'Z convoy — smoke',
					status: 'active',
					movement_id: 'mv-smoke-1',
					routes: [{ label: 'primary', segment_ids: [1026, 1025] }],
					route_segment_ids: [1026, 1025],
					phases: [],
				},
			],
		};
		await intercept(
			page,
			[
				[(_r, u) => u.pathname === '/api/v1/events/active', () => ({ body: BOARD })],
				[
					(_r, u) => u.pathname === '/api/convoy/chat/stream',
					(r) => {
						bodies.push(JSON.parse(r.postData() ?? '{}'));
						if (mode === 'abort') return { abort: true };
						if (mode === 'legacy')
							return {
								contentType: 'application/x-ndjson',
								body: ndjson([
									{ type: 'thought', data: { stepIndex: 1, text: 'Reasoning (round 1)...' } },
									{ type: 'token', data: '{"action":"call_tool","tool":"get_live_traffic",' },
									{ type: 'token', data: '"parameters":{}}' },
									{ type: 'done' },
								]),
							};
						return {
							contentType: 'application/x-ndjson',
							body: ndjson([
								{ type: 'thought', data: { stepIndex: 1, text: 'route_strategy → get_live_traffic', tier: 'decider' } },
								{ type: 'thought', data: { stepIndex: 2, text: 'arguments for get_live_traffic', tier: 'gemma' } },
								{ type: 'tool_call', data: { id: 'c1', name: 'get_live_traffic', args: { segment_ids: [1026] } } },
								{ type: 'tool_result', data: { id: 'c1', name: 'get_live_traffic', ok: true, summary: '2 segments · avg 31 km/h' } },
								{ type: 'token', data: 'HELLO_FROM_' },
								{ type: 'token', data: 'MOCK_STREAM — the corridor is flowing.' },
								{ type: 'done', data: { session_id: 'sess-smoke-123', tiers_used: ['decider:pplx-decider-v1-27b', 'gemma:aditi-ps18-v7h-12b'], operator_alerts: [], degraded: false } },
							]),
						};
					},
				],
			],
			log,
		);
		await page.goto(`${origin}/chat`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="chat"]', { timeout: 30000 });
		ok('/chat ChatConsole mounts', true);
		// WP-14: the header names the event (kind · name) and keeps the id
		// in the title of [data-testid=chat-context-event] — wait on that node
		const STUB_EVENT_ID = 'cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee';
		await page.waitForSelector('[data-testid="chat-context-event"]', { timeout: 20000 }).catch(() => undefined);
		const ctxEvent = await page
			.$eval('[data-testid="chat-context-event"]', (el) => ({ title: el.getAttribute('title') ?? '', text: el.textContent ?? '' }))
			.catch(() => ({ title: '', text: '' }));
		const ctxTxt = await text(page, '[data-testid="chat-context"]');
		ok(
			'/chat context header names the event (its id in the title)',
			ctxEvent.title === STUB_EVENT_ID && ctxTxt.includes('Z convoy — smoke'),
			`title ${ctxEvent.title || '—'} · ${ctxTxt.trim().slice(0, 70)}`,
		);

		await page.type('[data-testid="chat-input"]', 'status of corridor');
		await page.keyboard.press('Enter');
		await page.waitForSelector('[data-testid="chat-tiers"]', { timeout: 15000 });
		const agent = await page.$$eval('[data-msg-role="agent"]', (els) => els.map((e) => e.textContent?.replace(/\s+/g, ' ').trim()));
		const answer = await text(page, '[data-testid="chat-answer"]');
		const cards = await page.$$eval('[data-testid="chat-tool-card"]', (els) => els.length);
		const tiers = await page.$$eval('[data-testid="chat-tiers"] [data-tier]', (els) => els.map((e) => e.getAttribute('data-tier')));
		ok('/chat agent bubble shows the streamed text', answer.includes('HELLO_FROM_MOCK_STREAM'), (agent[0] ?? '').slice(0, 90));
		ok('/chat exactly 1 tool card', cards === 1, `${cards} cards`);
		ok('/chat tier chips (decider, Gemma)', tiers.includes('decider') && tiers.includes('gemma'), tiers.join(','));
		ok('/chat session shown', (await text(page, '[data-testid="chat-session"]')).includes('sess-smo'));
		ok('/chat request scoped to the event (vvip_class)', bodies[0]?.vvip_class === 'Z', JSON.stringify(bodies[0]));
		ok(
			'/chat movement_id sent when the board maps one',
			bodies[0]?.movement_id === 'mv-smoke-1' || bodies[0]?.movement_id === null,
			`movement_id=${bodies[0]?.movement_id} (null until the events mapper carries movementId)`,
		);

		await page.type('[data-testid="chat-input"]', 'and the alternate?');
		await page.keyboard.press('Enter');
		await sleep(1500);
		ok('/chat follow-up carries the session id', bodies[1]?.session_id === 'sess-smoke-123', JSON.stringify(bodies[1] ?? {}));

		mode = 'legacy';
		await page.type('[data-testid="chat-input"]', 'legacy brain?');
		await page.keyboard.press('Enter');
		await page.waitForFunction(() => document.querySelectorAll('[data-testid="chat-no-prose"]').length > 0, { timeout: 10000 }).catch(() => undefined);
		const noProse = await page.$$('[data-testid="chat-no-prose"]');
		const answers = await page.$$eval('[data-testid="chat-answer"]', (els) => els.map((e) => e.textContent ?? ''));
		ok('/chat raw JSON envelope is never shown as the answer', noProse.length === 1 && !answers.some((a) => a.trim().startsWith('{')), `${noProse.length} no-prose notes`);
		const raw = (await page.evaluate(() => document.body.innerText)).match(RAW_KEY);
		ok('/chat no raw kind_ key', !raw, raw ? raw[0] : 'clean');
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'chat-stream.png') });

		mode = 'abort';
		await page.type('[data-testid="chat-input"]', 'are you there?');
		await page.keyboard.press('Enter');
		await page.waitForSelector('[data-testid="chat-degraded"]', { timeout: 15000 });
		ok('/chat labelled degradation when the stream is down', true);
		await page.close();
	}

	// ── 4. /comms — stubbed board + dispatch feed ─────────────────────────
	{
		const page = await newPage(browser);
		const log = [];
		const PROC = 'eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee';
		const PURS = 'ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee';
		const NEWKIND = '99999999-bbbb-4ccc-8ddd-eeeeeeeeeeee';
		// Every stub event carries a closure plan, so /comms never asks the
		// oracle's /adaptive/params for these fake ids (that GET persists an
		// adaptive.param_overrides row on the origin's oracle).
		const SMOKE_PHASE = { label: 'w', start_time: null, end_time: null, window_start: null, window_end: null, closure_policy: { advance_closure_sec: 120 } };
		const BOARD = {
			events: [
				{
					event_id: PROC,
					event_kind: 'procession',
					protection_class: 'none',
					title: 'Rath Yatra — smoke',
					name: 'Rath Yatra — smoke',
					status: 'active',
					routes: [{ label: 'route', segment_ids: [1003, 1004, 1001, 1015] }],
					route_segment_ids: [1003, 1004, 1001, 1015],
					phases: [{ label: 'w', start_time: null, end_time: null, window_start: null, window_end: null, closure_policy: { advance_closure_sec: 150 } }],
				},
				{
					event_id: PURS,
					event_kind: 'vehicle_pursuit',
					protection_class: 'none',
					title: 'PURSUIT — smoke',
					name: 'PURSUIT — smoke',
					status: 'active',
					routes: [{ label: 'belt', segment_ids: [1012, 1015] }],
					route_segment_ids: [1012, 1015],
					phases: [SMOKE_PHASE],
				},
				{
					// A kind the oracle reports but the board mapper does not
					// know (it files it under protected_movement).
					event_id: NEWKIND,
					event_kind: 'hostage_rescue',
					protection_class: 'none',
					title: 'NEW KIND — smoke',
					name: 'NEW KIND — smoke',
					status: 'active',
					routes: [{ label: 'r', segment_ids: [1012, 1015] }],
					route_segment_ids: [1012, 1015],
					phases: [SMOKE_PHASE],
				},
			],
		};
		const audit = (steps) => steps.map((to, i) => ({ from_status: i ? steps[i - 1] : null, to_status: to, actor: 'smoke', note: null, at: new Date().toISOString() })).reverse();
		const DISPATCHES = {
			dispatches: [
				{ dispatch_id: '11111111-1111-4111-8111-111111111111', event_id: PROC, agency: 'ambulance', agency_label: '108 Ambulance', unit_id: '108-14', status: 'completed', audit: audit(['requested', 'approved', 'en_route', 'on_scene', 'completed']) },
				{ dispatch_id: '22222222-2222-4222-8222-222222222222', event_id: 'other-event', agency: 'jan_rakshak', agency_label: 'Jan Rakshak', unit_id: 'JR-EM-01', status: 'cancelled', audit: audit(['requested', 'approved', 'cancelled']) },
			],
			count: 2,
		};
		await intercept(
			page,
			[
				[(_r, u) => u.pathname === '/api/v1/events/active', () => ({ body: BOARD })],
				[(_r, u) => u.pathname === '/api/v1/dispatches', () => ({ body: DISPATCHES })],
			],
			log,
		);
		await page.goto(`${origin}/comms`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-screen="comms"]', { timeout: 30000 });
		ok('/comms desk mounts', true);
		const commsTheme = await page.evaluate(() => document.documentElement.dataset.theme ?? '');
		ok('/comms command theme', commsTheme === 'command', `data-theme=${commsTheme}`);

		await page.waitForFunction(() => document.querySelector('[data-testid="comms-event-select"]')?.value, { timeout: 20000 });
		const selVal = await page.$eval('[data-testid="comms-event-select"]', (el) => el.value);
		ok('/comms event select initialised (never blank)', selVal === PROC, selVal);

		await page.waitForSelector('[data-testid="comms-trilingual"][data-ready="yes"]', { timeout: 20000 });
		const cols = await page.$$eval('[data-adv-lang]', (els) =>
			els.map((el) => ({ lang: el.closest('[data-lang]')?.dataset.lang ?? '', text: el.textContent ?? '', facts: el.closest('[data-lang]')?.dataset.facts ?? '' })),
		);
		const [en, hi, gu] = ['en', 'hi', 'gu'].map((l) => cols.find((c) => c.lang === l));
		ok('/comms three language columns', !!en?.text.trim() && !!hi?.text.trim() && !!gu?.text.trim(), cols.map((c) => c.text.trim().length).join('/'));
		ok('/comms HI column Devanagari', !!hi && HI.test(hi.text));
		ok('/comms GU column Gujarati', !!gu && GU.test(gu.text));
		ok('/comms identical facts across languages', !!en?.facts && en.facts === hi?.facts && en.facts === gu?.facts, en?.facts.slice(0, 120));
		ok('/comms citizen draft uses the policy hold (3 min)', JSON.parse(en?.facts || '{}').minutes === 3);
		const factsLine = await page.$eval('[data-testid="comms-facts"]', (el) => ({ src: el.getAttribute('data-hold-source'), text: el.textContent?.replace(/\s+/g, ' ').trim() })).catch(() => null);
		ok('/comms facts line shows the hold provenance', factsLine?.src === 'closure_policy', JSON.stringify(factsLine));

		const genBtn = await page.evaluateHandle(() => [...document.querySelectorAll('.gen-row [data-prim="button"]')].find((b) => !b.disabled));
		if (genBtn && genBtn.asElement()) await genBtn.asElement().click();
		await sleep(400);
		const clearBtn = await page.evaluateHandle(() => [...document.querySelectorAll('.gen-row [data-prim="button"]')][1]);
		if (clearBtn && clearBtn.asElement()) await clearBtn.asElement().click();
		await sleep(400);
		const logLangs = await page.$$eval('.log-langs', (els) => els.map((e) => e.textContent?.trim()));
		ok('/comms generate + all-clear logged in en · hi · gu', logLangs.length === 2 && logLangs.every((l) => l === 'en · hi · gu'), logLangs.join(' | '));
		const pageText = await page.evaluate(() => document.body.innerText);
		ok('/comms drafted-not-delivered honesty', pageText.toUpperCase().includes('NOT DELIVERED'));
		ok('/comms no false "logged for the audit record" claim', !/logged for the audit record|echoed into the decision audit/i.test(pageText));

		const steps = await page.$$eval('.thread .th-step.reached', (els) => els.length);
		ok('/comms ladder mirrors the backend (completed reaches 5 steps)', steps === 5, `${steps} reached`);
		await page.click('.scope button[data-scope="all"]');
		await sleep(300);
		const exits = await page.$$eval('.th-exit', (els) => els.map((e) => e.getAttribute('data-step')));
		ok('/comms all-events view shows the cancelled exit chip', exits.includes('cancelled'), exits.join(','));

		await page.select('[data-testid="comms-event-select"]', PURS);
		await page.waitForFunction(() => document.querySelector('[data-lang="en"]')?.getAttribute('data-withheld') === 'yes', { timeout: 15000 }).catch(() => undefined);
		const withheld = await page.$$eval('[data-lang]', (els) => els.map((e) => e.getAttribute('data-withheld')));
		ok('/comms pursuit: no citizen draft in any language', withheld.length === 3 && withheld.every((w) => w === 'yes'), withheld.join(','));
		const allClearDisabled = await page.evaluate(() => [...document.querySelectorAll('.gen-row [data-prim="button"]')][1]?.disabled === true);
		ok('/comms pursuit: no citizen all-clear (button disabled)', allClearDisabled);

		await page.select('[data-testid="comms-event-select"]', NEWKIND);
		const newKindWithheld = await page
			.waitForFunction(() => [...document.querySelectorAll('[data-lang]')].length === 3 && [...document.querySelectorAll('[data-lang]')].every((e) => e.getAttribute('data-withheld') === 'yes'), { timeout: 15000 })
			.then(() => true)
			.catch(() => false);
		ok('/comms a kind unknown to the board mapper is withheld (raw oracle kind)', newKindWithheld);
		const raw = (await page.evaluate(() => document.body.innerText)).match(RAW_KEY);
		ok('/comms no raw kind_ key', !raw, raw ? raw[0] : 'clean');
		await page.screenshot({ path: path.join(HERE, 'artifacts', 'comms-trilingual.png') });
		await page.close();
	}

	// ── 5. Shell — no stale operator chrome across navigation ─────────────
	{
		const page = await newPage(browser);
		await page.goto(`${origin}/ops`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		await page.waitForSelector('[data-shell="command-bar"] .share', { timeout: 30000 });
		ok('/ops sets SHARE VIEW (baseline for the leak check)', true);
		ok('nav: the design sheet is a dev-only entry (not in a production build)', !(await page.$('nav.rail a[href="/design"]')));
		// Client-side navigation to the (unlisted) design sheet: SvelteKit's
		// router takes any same-origin anchor click.
		await page.evaluate(() => {
			const a = document.createElement('a');
			a.href = '/design';
			a.textContent = 'design';
			document.body.appendChild(a);
			a.click();
		});
		await page.waitForFunction(() => location.pathname === '/design', { timeout: 15000 });
		await sleep(800);
		ok('/ops → /design: SHARE VIEW cleared', !(await page.$('[data-shell="command-bar"] .share')));
		ok('/ops → /design: band chip reset', !!(await page.$('[data-shell="command-bar"] [data-band="pending"]')));
		await page.click('nav.rail a[href="/citizen"]');
		await page.waitForSelector('[data-shell="citizen-bar"]', { timeout: 15000 });
		await sleep(800);
		const bar = await text(page, '[data-shell="citizen-bar"]');
		ok('deck → /citizen: public bar has no band code / SHARE', !BAND_CODE.test(bar) && !/share/i.test(bar), bar.trim().slice(0, 80));
		ok('deck → /citizen: daylight applied', (await page.evaluate(() => document.documentElement.dataset.theme)) === 'daylight');
		await page.close();
	}

	// ── 6. Import boundary (source gate) ─────────────────────────────────
	importBoundaryGate();
} catch (err) {
	failures++;
	console.error(`FAIL harness — ${String(err?.stack ?? err).slice(0, 600)}`);
} finally {
	await browser.close().catch(() => undefined);
	if (deckProc) deckProc.kill();
}

if (failures > 0) {
	console.error(`\ncitizen-smoke: ${failures} failure(s)`);
	process.exit(1);
}
console.log('\ncitizen-smoke: all green');
process.exit(0);
