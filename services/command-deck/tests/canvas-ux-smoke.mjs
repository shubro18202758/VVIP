/**
 * Canvas UX smoke — the gate for God's-eye canvas stability, legibility and
 * wall layout, under SOFTWARE WebGL (SwiftShader, the flags the red-team
 * render proof validated: --enable-unsafe-swiftshader --use-gl=angle
 * --use-angle=swiftshader). Every assertion reads the live page:
 *
 *   idle:<route>        /gods-eye, '/' and '/ops' left idle IDLE_SEC (120 s):
 *                       the camera stays over Ahmedabad (lon 72.45–72.75,
 *                       lat 22.90–23.17), the default sensor is COMMAND
 *                       ('raw'), the canvas readback has colour (mean chroma
 *                       > 0), and the wall routes actually orbit
 *   cockpit-chips@WxH   on '/', elementFromPoint at every HUD chip centre
 *                       returns that chip (no rail / pill / dock covers it)
 *   ops-layout@WxH      on '/ops', .ge-wrap ≥ 400 px tall, no wall column
 *                       intersects the audit band, every column scrolls
 *                       internally
 *   pick                clicking an event spine selects it on the board and
 *                       opens a pick card carrying the event description;
 *                       hover turns the cursor into a pointer
 *   focus               clicking the event in the cockpit LiveRail puts the
 *                       camera within 0.02° of the spine centroid in ≤ 3 s
 *                       with the LOCKED chip shown
 *   focus-nav           a focus request made on '/' (then RELEASEd when it
 *                       locked) is never replayed by the next view's canvas:
 *                       after client-side navigation to /gods-eye and then
 *                       /ops, no lock appears and no stale 'no route
 *                       geometry' flash shows
 *   ama-clock           the AMA chip age ticks at 1 Hz (sampled 7× a
 *                       second apart: ≥ 2 +1 s steps; a drop only to a fresher
 *                       reading ≤ 10 s — the engine re-evaluates every 5 s,
 *                       the city band's reading included), and the Panel
 *                       'updated Ns ago' label advances
 *   exit                /gods-eye shows an EXIT chip; Esc navigates to /ops
 *   ion-fallback        a preview started with VEER_CESIUM_ION_TOKEN=invalid
 *                       still reaches phase 'ready' on keyless imagery
 *
 * The pick/focus checks target PURSUIT_EVENT_ID (default: the first
 * vehicle_pursuit row of GET /api/v1/events/active) and compute the spine
 * centroid independently from the raw API (routes[].segment_ids →
 * /spatial/segments), never from the canvas.
 *
 * Usage:
 *   node tests/canvas-ux-smoke.mjs [origin]           # default http://127.0.0.1:5199
 *   IDLE_SEC=120                idle window for the idle:<route> checks (the
 *                               three routes run one after another: a
 *                               background tab gets no animation frames, so
 *                               concurrent idle pages would prove nothing)
 *   CANVAS_SMOKE_ONLY=idle,pick check subset (idle, cockpit, ops, pick,
 *                               focus, focus-nav, ama, exit, ion)
 *   CANVAS_SMOKE_ION_ORIGIN=…   an already-running VEER_CESIUM_ION_TOKEN=invalid
 *                               preview; unset → spawned from build/ on
 *                               ION_PORT (default 5198) and stopped after
 *                               (a PRIVATE preview: gate off — the PIN and
 *                               the session secret never reach it — and
 *                               ORACLE_PORT/BRAIN_PORT/EMULATOR_PORT, which
 *                               default to the LIVE 8091/8092/8090)
 *   VEER_OPERATOR_PIN, else VEER_OPERATOR_PIN_FILE, else veer.env
 *                               read only when the deck under test has its
 *                               operator gate on: ONE sign-in through
 *                               tests/lib/operator-session.mjs (never
 *                               logged), the session on every API read and
 *                               every page of that deck (cookie set before
 *                               the first navigation), signed out on exit
 *   PUPPETEER_FROM=…/package.json, CHROME_PATH=…   tool resolution overrides
 *
 * The deck under test must already be serving (adapter-node build/, see
 * gods-eye-smoke.mjs for the spawn pattern). Nothing here writes: besides
 * the operator sign-in and sign-out (gate on only), every request the
 * pages make is the deck's own read polling. Exits 0 with
 * `ok <name> — <detail>` lines, 1 on any FAIL. Screenshots land in
 * tests/artifacts/canvas-ux-*.png.
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
const ARTIFACTS = path.join(HERE, 'artifacts');

const ORIGIN = (process.argv[2] ?? 'http://127.0.0.1:5199').replace(/\/$/, '');
/** The operator session on the deck under test (gate off until signIn says otherwise). */
let session = { base: ORIGIN, gate: false, officer: null, cookie: null, value: null };
const IDLE_SEC = Number(process.env.IDLE_SEC ?? 120);
const ONLY = new Set((process.env.CANVAS_SMOKE_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean));
const run = (name) => ONLY.size === 0 || ONLY.has(name);

// The camera envelope the idle checks hold the canvas to (CITY_BBOX ± 0.05°).
const CITY = { minLon: 72.45, maxLon: 72.75, minLat: 22.9, maxLat: 23.17 };
const FOCUS_TOLERANCE_DEG = 0.02;
const FOCUS_BUDGET_MS = 3000;

// Puppeteer resolves from THIS repo's frontend tree first (the e2e harness
// pattern), then the shared main tree; Chrome from the puppeteer cache.
const PUPPETEER_CANDIDATES = [
	process.env.PUPPETEER_FROM,
	path.resolve(DECK, '../../frontend/package.json'),
	'/home/sandy050405/argus/veer/VVIP/frontend/package.json',
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
	process.env.CHROME_PATH,
	path.join(os.homedir(), '.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome'),
].filter(Boolean);
const executablePath = CHROME_CANDIDATES.find((p) => existsSync(p));

const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
];

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmt = (n, d = 4) => (typeof n === 'number' ? n.toFixed(d) : String(n));

/** GET JSON; a read of the deck under test carries its operator session (another origin never does). */
async function getJson(url) {
	const init = { signal: AbortSignal.timeout(10000) };
	const res = await (new URL(url).origin === new URL(ORIGIN).origin ? deckFetch(session, url, init) : fetch(url, init));
	if (!res.ok) throw new Error(`${res.status} ${url}`);
	return res.json();
}

/** The target event + its spine centroid, computed from the raw API. */
async function loadTargetEvent(origin) {
	const body = await getJson(`${origin}/api/v1/events/active`);
	const rows = Array.isArray(body?.events) ? body.events : [];
	const wanted = process.env.PURSUIT_EVENT_ID;
	const row = wanted
		? rows.find((r) => (r.event_id ?? r.id) === wanted)
		: rows.find((r) => r.event_kind === 'vehicle_pursuit') ?? rows[0];
	if (!row) return null;
	// Unique segments: routes flattened from several spines may share one.
	const segIds = [
		...new Set(
			Array.isArray(row.routes)
				? row.routes.flatMap((r) => (Array.isArray(r?.segment_ids) ? r.segment_ids : []))
				: Array.isArray(row.route_segment_ids)
					? row.route_segment_ids
					: [],
		),
	];
	const segs = await getJson(`${origin}/api/v1/spatial/segments?min_lon=72.5&min_lat=22.95&max_lon=72.7&max_lat=23.12`);
	const byId = new Map((segs?.segments ?? []).map((s) => [s.segment_id, s]));
	const pts = segIds.flatMap((id) => byId.get(id)?.geometry?.coordinates ?? []);
	const centroid = pts.length
		? {
				lon: pts.reduce((a, p) => a + p[0], 0) / pts.length,
				lat: pts.reduce((a, p) => a + p[1], 0) / pts.length,
			}
		: null;
	return {
		eventId: String(row.event_id ?? row.id),
		title: row.title ?? row.name ?? null,
		description: row.description ?? null,
		segIds,
		centroid,
	};
}

/**
 * A page on `browser` (or a browser context). It carries the operator
 * session of the deck under test unless `anonymous` — cookies are scoped by
 * host, not port, so a page meant for another preview on 127.0.0.1 is
 * opened anonymous in a context of its own.
 */
async function newPage(browser, w, h, { anonymous = false } = {}) {
	const page = await browser.newPage();
	await page.setViewport({ width: w, height: h });
	if (session.gate && !anonymous) await page.setCookie(...puppeteerCookies(session));
	page.on('pageerror', (e) => console.error(`  pageerror: ${String(e).slice(0, 200)}`));
	return page;
}

/** Load a route and wait for the canvas phase to settle. */
async function openCanvas(page, route, timeoutMs = 60000) {
	await page.goto(`${ORIGIN}${route}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const phase = await page.evaluate(() => window.__GODS_EYE__?.phase ?? null).catch(() => null);
		if (phase && phase !== 'loading') return phase;
		await sleep(400);
	}
	return null;
}

const snapshot = (page) =>
	page.evaluate(() => {
		const g = window.__GODS_EYE__;
		return g
			? {
					phase: g.phase,
					sensorMode: g.sensorMode,
					camera: g.camera,
					orbiting: g.orbiting,
					stats: g.stats,
					imageryMode: g.imageryMode,
					imageryNote: g.imageryNote,
					selectedEventId: g.selectedEventId,
					subjectLockId: g.subjectLockId,
					picked: g.picked,
					amaAgeSec: g.amaAgeSec,
				}
			: null;
	});

/** In-page readback of the WebGL canvas (preserveDrawingBuffer is on). */
const readback = (page) =>
	page.evaluate(() => {
		const c = document.querySelector('.ge-canvas canvas');
		if (!c || !c.width) return null;
		const off = document.createElement('canvas');
		off.width = 320;
		off.height = Math.max(1, Math.round((320 * c.height) / c.width));
		const ctx = off.getContext('2d');
		ctx.drawImage(c, 0, 0, off.width, off.height);
		const d = ctx.getImageData(0, 0, off.width, off.height).data;
		let chroma = 0;
		let luma = 0;
		let n = 0;
		for (let i = 0; i < d.length; i += 4) {
			chroma += Math.max(d[i], d[i + 1], d[i + 2]) - Math.min(d[i], d[i + 1], d[i + 2]);
			luma += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
			n++;
		}
		return { meanChroma: chroma / n, meanLuma: luma / n };
	});

const inCity = (c) => !!c && c.lon >= CITY.minLon && c.lon <= CITY.maxLon && c.lat >= CITY.minLat && c.lat <= CITY.maxLat;

// ── idle: camera stays home, COMMAND default, colour survives ──────────────

async function idleCheck(browser, route) {
	const name = `idle:${route}`;
	const page = await newPage(browser, 1600, 900);
	try {
		const phase = await openCanvas(page, route);
		ok(`${name} ready`, phase === 'ready', `phase=${phase}`);
		if (phase !== 'ready') return;
		const first = await snapshot(page);
		ok(`${name} default sensor`, first.sensorMode === 'raw', `sensorMode=${first.sensorMode} (COMMAND)`);
		const t0 = Date.now();
		const wall = route !== '/gods-eye';
		let mid = null;
		while (Date.now() - t0 < IDLE_SEC * 1000) {
			await sleep(5000);
			const s = await snapshot(page);
			if (!inCity(s?.camera)) {
				ok(`${name} camera over the city`, false, `left the city at +${Math.round((Date.now() - t0) / 1000)} s: lon=${fmt(s?.camera?.lon)} lat=${fmt(s?.camera?.lat)}`);
				return;
			}
			if (!mid && Date.now() - t0 >= Math.min(IDLE_SEC, 75) * 1000 - 5000) mid = s;
		}
		const last = await snapshot(page);
		ok(
			`${name} camera over the city`,
			inCity(last.camera),
			`after ${IDLE_SEC} s idle lon=${fmt(last.camera?.lon)} lat=${fmt(last.camera?.lat)} alt=${Math.round(last.camera?.alt ?? 0)} m`,
		);
		if (wall && IDLE_SEC >= 45) {
			const turned = mid && last.camera ? Math.abs(((last.camera.heading - mid.camera.heading + 540) % 360) - 180) : 0;
			ok(`${name} wall orbit runs`, last.orbiting === true && turned > 5, `orbiting=${last.orbiting} heading turned ${turned.toFixed(1)}° over the last window`);
		}
		const rb = await readback(page);
		ok(`${name} colour readback`, !!rb && rb.meanChroma > 0, rb ? `mean chroma ${rb.meanChroma.toFixed(2)}, mean luma ${rb.meanLuma.toFixed(1)}` : 'no canvas');
		await page.screenshot({ path: path.join(ARTIFACTS, `canvas-ux-idle${route.replace(/\//g, '_') || '_root'}.png`) });
	} finally {
		await page.close();
	}
}

// ── cockpit: every HUD chip is hit-testable ────────────────────────────────

async function cockpitChips(browser, w, h) {
	const name = `cockpit-chips@${w}x${h}`;
	const page = await newPage(browser, w, h);
	try {
		const phase = await openCanvas(page, '/');
		if (phase !== 'ready') return ok(name, false, `phase=${phase}`);
		await sleep(3000);
		const res = await page.evaluate(() =>
			[...document.querySelectorAll('.ge-hud .chip')].map((chip) => {
				const b = chip.getBoundingClientRect();
				const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
				return {
					chip: chip.textContent.trim().replace(/\s+/g, ' ').slice(0, 32),
					ok: !!hit && (hit === chip || chip.contains(hit)),
					hit: hit ? `${hit.tagName.toLowerCase()}.${String(hit.className).split(' ')[0]}` : 'none',
					visible: b.width > 0 && b.height > 0 && b.right <= innerWidth && b.bottom <= innerHeight,
				};
			}),
		);
		const bad = res.filter((r) => !r.ok || !r.visible);
		ok(name, res.length >= 8 && bad.length === 0, bad.length ? bad.map((r) => `${r.chip} ← ${r.hit}`).join(' | ') : `${res.length} chips hit-testable`);
		await page.screenshot({ path: path.join(ARTIFACTS, `canvas-ux-cockpit-${w}.png`) });
	} finally {
		await page.close();
	}
}

// ── /ops: hero canvas size, no overlap with the audit band, inner scroll ───

async function opsLayout(browser, w, h) {
	const name = `ops-layout@${w}x${h}`;
	const page = await newPage(browser, w, h);
	try {
		const phase = await openCanvas(page, '/ops');
		if (phase !== 'ready') return ok(name, false, `phase=${phase}`);
		await sleep(4000);
		const m = await page.evaluate(() => {
			const box = (el) => {
				if (!el) return null;
				const b = el.getBoundingClientRect();
				return { x: b.x, y: b.y, w: b.width, h: b.height, r: b.right, b: b.bottom };
			};
			const audit = document.querySelector('.audit-band') ?? [...document.querySelectorAll('.wall [data-prim=panel]')].pop();
			const cols = [...document.querySelectorAll('.wall-grid > .col')].map((c) => {
				const before = c.scrollTop;
				c.scrollTop = before + 60;
				const moved = c.scrollTop !== before;
				c.scrollTop = before;
				return {
					cls: String(c.className).split(' ').filter((x) => x.startsWith('col-'))[0] ?? 'col',
					box: box(c),
					overflowY: getComputedStyle(c).overflowY,
					scrollH: c.scrollHeight,
					clientH: c.clientHeight,
					moved,
				};
			});
			return { wrap: box(document.querySelector('.ge-wrap')), audit: box(audit), cols };
		});
		ok(`${name} canvas height`, (m.wrap?.h ?? 0) >= 400, `.ge-wrap ${Math.round(m.wrap?.w ?? 0)}×${Math.round(m.wrap?.h ?? 0)} px`);
		const overlaps = m.cols.filter(
			(c) => m.audit && c.box && c.box.x < m.audit.r && c.box.r > m.audit.x && c.box.y < m.audit.b && c.box.b > m.audit.y,
		);
		ok(`${name} columns clear of the audit band`, !!m.audit && m.cols.length === 3 && overlaps.length === 0,
			overlaps.length ? overlaps.map((c) => `${c.cls} bottom ${Math.round(c.box.b)} > audit top ${Math.round(m.audit.y)}`).join(' | ') : `audit band at y=${Math.round(m.audit?.y ?? -1)}, columns end ≤ ${Math.round(Math.max(...m.cols.map((c) => c.box.b)))}`);
		const stuck = m.cols.filter((c) => !['auto', 'scroll'].includes(c.overflowY) || !(c.scrollH > c.clientH) || !c.moved);
		ok(`${name} columns scroll internally`, m.cols.length === 3 && stuck.length === 0,
			m.cols.map((c) => `${c.cls} ${c.clientH}/${c.scrollH}px ${c.overflowY}${c.moved ? '' : ' (did not scroll)'}`).join(' · '));
		await page.screenshot({ path: path.join(ARTIFACTS, `canvas-ux-ops-${w}.png`) });
	} finally {
		await page.close();
	}
}

// ── pick: spine click selects the event + pick card; hover cursor ──────────

async function pickCheck(browser, target) {
	const name = 'pick';
	const page = await newPage(browser, 1600, 900);
	try {
		const phase = await openCanvas(page, '/gods-eye');
		if (phase !== 'ready') return ok(name, false, `phase=${phase}`);
		const prefix = `evt:spine-${target.eventId}-`;
		let spots = [];
		const deadline = Date.now() + 30000;
		while (Date.now() < deadline && !spots.length) {
			spots = await page.evaluate((p) => window.__GODS_EYE__?.locate?.(p) ?? [], prefix);
			if (!spots.length) await sleep(1000);
		}
		if (!spots.length) {
			return ok(name, false, `no '${prefix}*' spine on screen — the event's route never reached the canvas (events/active → mapEventRow routes)`);
		}
		// Move the selection to another event first (when one is on screen)
		// so the pick has to change it back — the board auto-selects the
		// first event on load, which may already be the target.
		const others = await page.evaluate((id) => (window.__GODS_EYE__?.locate?.('evt:') ?? []).filter((s) => !s.id.includes(id)), target.eventId);
		let elsewhere = null;
		for (const other of others.slice(0, 6)) {
			await page.mouse.click(other.x, other.y);
			await sleep(1200);
			const s = await snapshot(page);
			if (s?.selectedEventId && s.selectedEventId !== target.eventId) {
				elsewhere = s.selectedEventId;
				break;
			}
		}
		const before = await snapshot(page);
		const spot = spots[Math.floor(spots.length / 2)];
		// Settle on the spine before clicking: the hover cursor turns into a
		// pointer only once the polyline primitive is pickable (a click in the
		// first second after 'ready' can land before it is), so hover — a
		// 1 px jiggle re-triggers the throttled hover pick — until it does.
		// Spines are DRAPED (WP-22): GroundPolylinePrimitives that Cesium
		// builds in web workers, batch by batch, so under SwiftShader on a
		// loaded host the spine turns pickable several seconds after 'ready'
		// (and after the flow line beneath it may) — wait up to 20 s for the
		// hover to land on THIS event, not merely on something.
		let cursor = '';
		let hover = null;
		const settleBy = Date.now() + 20000;
		for (let i = 0; Date.now() < settleBy; i++) {
			await page.mouse.move(spot.x + (i % 2), spot.y);
			await sleep(300);
			[cursor, hover] = await page.evaluate(() => [
				document.querySelector('.ge-canvas')?.style.cursor ?? '',
				window.__GODS_EYE__?.selection?.hover ?? null,
			]);
			if (cursor === 'pointer' && hover === `event:${target.eventId}`) break;
		}
		await page.mouse.click(spot.x, spot.y);
		await sleep(1500);
		const after = await snapshot(page);
		const card = await page.evaluate(() => document.querySelector('.ge-pick')?.textContent?.replace(/\s+/g, ' ').trim() ?? null);
		const needles = [target.title, target.description].filter(Boolean).map((t) => t.slice(0, 40));
		const carries = !!card && needles.length > 0 && needles.some((n) => card.includes(n));
		ok(`${name} selects the event`, after.selectedEventId === target.eventId && after.picked?.eventId === target.eventId,
			`selected ${before.selectedEventId ?? '—'} → ${after.selectedEventId ?? '—'}, picked ${after.picked?.id ?? '—'} (clicked ${spot.id} at ${Math.round(spot.x)},${Math.round(spot.y)}${
				elsewhere ? '' : '; no other event on screen to move the selection first'
			})`);
		ok(`${name} card carries the description`, carries, card ? `card: ${card.slice(0, 140)}` : 'no pick card');
		ok(`${name} hover highlight`, cursor === 'pointer', `cursor over the spine: '${cursor}' (hover ${hover ?? '—'})`);
		await page.screenshot({ path: path.join(ARTIFACTS, 'canvas-ux-pick.png') });
	} finally {
		await page.close();
	}
}

// ── focus: LiveRail click → camera over the centroid + LOCKED ──────────────

/** Click the target event's row in the cockpit LiveRail (true, or the rows seen). */
const clickRailEvent = (page, target) =>
	page.evaluate(
		(id) => {
			// Titles are plate-masked under C6 (a pursuit's title carries the
			// plate), so the row is matched by its event id attribute only.
			const rows = [...document.querySelectorAll('.rail-right .event')];
			const row = rows.find((b) => b.dataset.eventId === id);
			if (!row) return rows.map((r) => `${r.dataset.eventId ?? '?'} ${r.textContent.trim().slice(0, 40)}`);
			row.click();
			return true;
		},
		target.eventId,
	);

async function focusCheck(browser, target) {
	const name = 'focus';
	if (!target.centroid) return ok(name, false, `event ${target.eventId} has no resolvable spine in the API (routes → segments)`);
	const page = await newPage(browser, 1600, 900);
	try {
		const phase = await openCanvas(page, '/');
		if (phase !== 'ready') return ok(name, false, `phase=${phase}`);
		await sleep(3000);
		const clicked = await clickRailEvent(page, target);
		if (clicked !== true) return ok(name, false, `event not in the LiveRail (rows: ${JSON.stringify(clicked)})`);
		const t0 = Date.now();
		let s = null;
		let locked = false;
		let near = false;
		while (Date.now() - t0 < FOCUS_BUDGET_MS + 2000) {
			s = await snapshot(page);
			locked = await page.evaluate(() => !!document.querySelector('.ge-hud .chip.lock'));
			near = !!s?.camera && Math.abs(s.camera.lon - target.centroid.lon) <= FOCUS_TOLERANCE_DEG && Math.abs(s.camera.lat - target.centroid.lat) <= FOCUS_TOLERANCE_DEG;
			if (near && locked) break;
			await sleep(150);
		}
		const ms = Date.now() - t0;
		ok(`${name} camera on the centroid`, near && ms <= FOCUS_BUDGET_MS,
			`camera ${fmt(s?.camera?.lon)},${fmt(s?.camera?.lat)} vs centroid ${fmt(target.centroid.lon)},${fmt(target.centroid.lat)} after ${ms} ms`);
		ok(`${name} LOCKED chip`, locked && s?.subjectLockId === target.eventId, `lock=${s?.subjectLockId ?? '—'}`);
		await page.screenshot({ path: path.join(ARTIFACTS, 'canvas-ux-focus.png') });
	} finally {
		await page.close();
	}
}

// ── focus-nav: the next view's canvas never replays a focus request ───────

/**
 * Client-side navigation: SvelteKit intercepts a same-origin anchor click.
 * Resolves once the NEW canvas (a fresh __GODS_EYE__ hook) has settled.
 */
async function spaNavigate(page, route) {
	await page.evaluate((href) => {
		window.__canvasUxSpa = true; // a full reload would drop this marker
		window.__canvasUxPrevHook = window.__GODS_EYE__ ?? null;
		const a = document.createElement('a');
		a.href = href;
		a.textContent = 'canvas-ux spa nav';
		document.body.appendChild(a);
		a.click();
		a.remove();
	}, route);
	const deadline = Date.now() + 60000;
	while (Date.now() < deadline) {
		const st = await page
			.evaluate(() => ({
				path: location.pathname,
				fresh: !!window.__GODS_EYE__ && window.__GODS_EYE__ !== window.__canvasUxPrevHook,
				phase: window.__GODS_EYE__?.phase ?? null,
			}))
			.catch(() => null);
		if (st && st.path === route && st.fresh && st.phase && st.phase !== 'loading') {
			return { ok: st.phase === 'ready', detail: `phase=${st.phase}` };
		}
		await sleep(300);
	}
	return { ok: false, detail: `no fresh canvas on ${route} within 60 s` };
}

async function focusNavCheck(browser, target) {
	const name = 'focus-nav';
	const page = await newPage(browser, 1600, 900);
	try {
		const phase = await openCanvas(page, '/');
		if (phase !== 'ready') return ok(name, false, `phase=${phase}`);
		await sleep(3000);
		const clicked = await clickRailEvent(page, target);
		if (clicked !== true) return ok(name, false, `event not in the LiveRail (rows: ${JSON.stringify(clicked)})`);
		// The cockpit canvas answers first: LOCKED when its flight lands (the
		// event has a spine), else the rail reads 'no spine on canvas'.
		let onRoot = { lock: null, rail: '' };
		const t0 = Date.now();
		while (Date.now() - t0 < 6000) {
			onRoot = await page.evaluate(() => ({
				lock: window.__GODS_EYE__?.subjectLockId ?? null,
				rail: [...document.querySelectorAll('.rail-right .ev-focus')].map((e) => e.textContent.trim()).join(' | '),
			}));
			if (onRoot.lock || /no spine|unavailable/.test(onRoot.rail)) break;
			await sleep(200);
		}
		let answer = onRoot.lock ? 'locked on /' : `no lock on / (rail '${onRoot.rail || '—'}')`;
		if (onRoot.lock) {
			const released = await page.evaluate(() => {
				const chip = document.querySelector('.ge-hud .chip.lock');
				chip?.click();
				return !!chip;
			});
			await sleep(600);
			const s = await snapshot(page);
			answer = `locked then ${released && !s?.subjectLockId ? 'RELEASED' : 'NOT released'} on /`;
		}
		for (const route of ['/gods-eye', '/ops']) {
			const nav = await spaNavigate(page, route);
			if (!nav.ok) {
				ok(`${name} ${route}`, false, nav.detail);
				continue;
			}
			// Watch the fresh canvas for 7 s: a replay answers within seconds
			// of 'ready' (at once, or when the segment geometry lands) with a
			// lock or a 5 s 'no route geometry' flash.
			let lockSeen = null;
			let flashSeen = null;
			const t1 = Date.now();
			while (Date.now() - t1 < 7000) {
				const w = await page.evaluate(() => ({
					lock: window.__GODS_EYE__?.subjectLockId ?? null,
					flash: document.querySelector('.ge-hud .flash')?.textContent ?? '',
				}));
				lockSeen ??= w.lock;
				if (/no route geometry/.test(w.flash)) flashSeen = w.flash.trim();
				await sleep(250);
			}
			const s = await snapshot(page);
			ok(`${name} ${route} no replayed lock`, !lockSeen && !s?.subjectLockId,
				`lock=${lockSeen ?? s?.subjectLockId ?? '—'}, camera ${fmt(s?.camera?.lon)},${fmt(s?.camera?.lat)} (${answer})`);
			ok(`${name} ${route} no stale focus flash`, !flashSeen, flashSeen ? `flash '${flashSeen}'` : 'no focus flash');
		}
		const spa = await page.evaluate(() => window.__canvasUxSpa === true);
		ok(`${name} navigations were client-side`, spa, spa ? 'module state survived both navigations' : 'a full reload reset the page — the replay was not exercised');
	} finally {
		await page.close();
	}
}

// ── clocks: AMA chip age + Panel 'updated Ns ago' advance ──────────────────

async function clockCheck(browser) {
	const page = await newPage(browser, 1600, 900);
	try {
		const phase = await openCanvas(page, '/ops');
		if (phase !== 'ready') return ok('ama-clock', false, `phase=${phase}`);
		await sleep(6000);
		const age = () => page.evaluate(() => {
			const chip = document.querySelector('.ge-hud .chip.ama');
			const v = chip?.getAttribute('data-age-sec');
			return { chip: v === '' || v == null ? null : Number(v), snap: window.__GODS_EYE__?.amaAgeSec ?? null, text: chip?.textContent?.replace(/\s+/g, ' ').trim() ?? null };
		});
		// Sample the chip at 1 Hz. The age must TICK (+1 s steps); a drop is
		// legitimate only as a FRESHER reading (≤ 10 s): the engine re-evaluates
		// every 5 s, so the newest situation_log row, or the city band's newest
		// evaluation, restarts the clock on a re-read. A frozen chip fails.
		const samples = [];
		for (let i = 0; i < 7; i++) {
			samples.push(await age());
			if (i < 6) await sleep(1000);
		}
		let ticks = 0;
		let resets = 0;
		let bad = 0;
		for (let i = 1; i < samples.length; i++) {
			const a = samples[i - 1].chip;
			const b = samples[i].chip;
			if (a == null || b == null) bad++;
			else if (b > a && b - a <= 3) ticks++;
			else if (b < a && b <= 10) resets++;
			else if (b !== a) bad++;
		}
		const cityBand = samples.some((x) => /\[(?:city-band|city band)[^\]]*\]/.test(x.text ?? ''));
		ok(
			'ama-clock chip age ticks (a re-read restarts it at ≤ 10 s)',
			ticks >= 2 && bad === 0,
			`${samples.map((x) => x.chip ?? '—').join('→')} s · ${ticks} ticks · ${resets} fresher reading(s)${cityBand ? ' · city band' : ''} · '${samples.at(-1).text}'`,
		);
		const panels = () => page.evaluate(() => [...document.querySelectorAll('[data-prim=panel] .age')].map((el) => Number(el.getAttribute('data-age-sec'))).filter((n) => Number.isFinite(n)));
		const p1 = await panels();
		await sleep(3200);
		const p2 = await panels();
		const advanced = p1.filter((v, i) => p2[i] != null && p2[i] >= v + 2).length;
		ok('ama-clock panel ages tick on /ops', p1.length > 0 && advanced > 0, `${advanced}/${p1.length} panel ages advanced ≥ 2 s over 3.2 s (others re-polled)`);
	} finally {
		await page.close();
	}
	// Deterministic Panel clock: the design sheet's demo panel has a fixed updatedAt.
	const page2 = await newPage(browser, 1400, 900);
	try {
		await page2.goto(`${ORIGIN}/design`, { waitUntil: 'domcontentloaded' });
		await sleep(2500);
		const read = () => page2.evaluate(() => {
			const el = document.querySelector('[data-prim=panel] .age');
			return el ? { secs: Number(el.getAttribute('data-age-sec')), text: el.textContent.trim() } : null;
		});
		const x = await read();
		await sleep(3200);
		const y = await read();
		ok('ama-clock panel label advances', !!x && !!y && y.secs - x.secs >= 2, x && y ? `'${x.text}' → '${y.text}'` : 'no panel age label on /design');
	} finally {
		await page2.close();
	}
}

// ── /gods-eye exit: chip + Esc → /ops ──────────────────────────────────────

async function exitCheck(browser) {
	const page = await newPage(browser, 1600, 900);
	try {
		const phase = await openCanvas(page, '/gods-eye');
		if (phase !== 'ready') return ok('exit', false, `phase=${phase}`);
		const chip = await page.evaluate(() => {
			const el = document.querySelector('.ge-hud .chip.exit');
			if (!el) return null;
			const b = el.getBoundingClientRect();
			const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
			return { text: el.textContent.trim(), hit: !!hit && (hit === el || el.contains(hit)) };
		});
		ok('exit chip', !!chip && chip.hit, chip ? `'${chip.text}' hit-testable=${chip.hit}` : 'no EXIT chip');
		// An Esc typed in a field belongs to that field (the command search),
		// never to the canvas.
		const focused = await page.evaluate(() => {
			const input = document.createElement('input');
			input.id = 'canvas-ux-esc-field';
			input.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:99999';
			document.body.appendChild(input);
			input.focus();
			return document.activeElement === input;
		});
		await page.keyboard.press('Escape');
		await sleep(1000);
		const stayed = await page.evaluate(() => location.pathname);
		ok('exit Esc in a field stays', focused && stayed === '/gods-eye', `pathname=${stayed} after Esc in a focused input`);
		await page.evaluate(() => document.getElementById('canvas-ux-esc-field')?.remove());
		await page.keyboard.press('Escape');
		const t0 = Date.now();
		let pathname = '';
		while (Date.now() - t0 < 6000) {
			pathname = await page.evaluate(() => location.pathname).catch(() => '');
			if (pathname === '/ops') break;
			await sleep(200);
		}
		ok('exit Esc → /ops', pathname === '/ops', `pathname=${pathname} after ${Date.now() - t0} ms`);
	} finally {
		await page.close();
	}
}

// ── ion fallback: a bad token must still reach 'ready' on keyless imagery ──

async function ionCheck(browser) {
	let origin = process.env.CANVAS_SMOKE_ION_ORIGIN?.replace(/\/$/, '');
	let proc = null;
	if (!origin) {
		if (!existsSync(path.join(DECK, 'build', 'index.js'))) return ok('ion-fallback', false, 'no build/ output to spawn the invalid-token preview from');
		const port = Number(process.env.ION_PORT ?? 5198);
		origin = `http://127.0.0.1:${port}`;
		const env = {
			...process.env,
			PORT: String(port),
			HOST: '127.0.0.1',
			VEER_CESIUM_ION_TOKEN: 'invalid',
			ORACLE_PORT: process.env.ORACLE_PORT ?? '8091',
			BRAIN_PORT: process.env.BRAIN_PORT ?? '8092',
			EMULATOR_PORT: process.env.EMULATOR_PORT ?? '8090',
		};
		// The invalid-token preview is private: gate off whatever this shell
		// exported (the PIN and the session secret stay with the deck under test).
		for (const k of ['VEER_OPERATOR_PIN', 'VEER_OPERATOR_PIN_FILE', 'VEER_SESSION_SECRET', 'VEER_SESSION_SECURE']) delete env[k];
		proc = spawn('node', ['build'], {
			cwd: DECK,
			env,
			stdio: 'ignore',
		});
		const deadline = Date.now() + 20000;
		while (Date.now() < deadline) {
			try {
				const r = await fetch(`${origin}/gods-eye/mode`, { signal: AbortSignal.timeout(2000) });
				if (r.ok) break;
			} catch {
				// not up yet
			}
			await sleep(500);
		}
	}
	// Another server on the same host: its page lives in a context of its own,
	// so the deck under test's session cookie never rides along to it.
	const context = await browser.createBrowserContext();
	const page = await newPage(context, 1600, 900, { anonymous: true });
	try {
		const mode = await getJson(`${origin}/gods-eye/mode`).catch(() => null);
		ok('ion-fallback preview keyed', mode?.mode === 'ion-photoreal', `mode endpoint says ${mode?.mode ?? 'unreachable'}`);
		await page.goto(`${origin}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		let s = null;
		const deadline = Date.now() + 75000;
		while (Date.now() < deadline) {
			s = await snapshot(page).catch(() => null);
			if (s?.phase === 'unavailable') break;
			if (s?.phase === 'ready' && s.imageryMode === 'keyless-osm' && s.imageryNote) break;
			await sleep(1000);
		}
		ok('ion-fallback reaches ready', s?.phase === 'ready', `phase=${s?.phase ?? 'none'}`);
		ok('ion-fallback keyless imagery', s?.imageryMode === 'keyless-osm' && !!s?.imageryNote, `imageryMode=${s?.imageryMode} note=${s?.imageryNote ?? '—'}`);
		await sleep(4000);
		const rb = await readback(page);
		ok('ion-fallback canvas draws', !!rb && rb.meanChroma > 0, rb ? `mean chroma ${rb.meanChroma.toFixed(2)}` : 'no canvas');
		await page.screenshot({ path: path.join(ARTIFACTS, 'canvas-ux-ion-fallback.png') });
	} finally {
		await context.close();
		proc?.kill('SIGTERM');
	}
}

// ── main ───────────────────────────────────────────────────────────────────

mkdirSync(ARTIFACTS, { recursive: true });
const browser = await puppeteer.launch({
	headless: true,
	...(executablePath ? { executablePath } : {}),
	args: SWIFTSHADER_ARGS,
	protocolTimeout: 300000,
});
try {
	console.log(`canvas-ux-smoke against ${ORIGIN} (idle ${IDLE_SEC} s)`);
	session = await signIn(ORIGIN, 'Canvas UX smoke (automated check)');
	ok('operator gate', true, session.gate ? 'on — signed in' : 'off');
	const target =
		run('pick') || run('focus') || run('focus-nav') ? await loadTargetEvent(ORIGIN).catch((e) => ({ error: String(e) })) : null;
	if (target && !target.error) {
		console.log(`  target event ${target.eventId} '${target.title ?? '—'}' spine [${target.segIds.join(',')}] centroid ${target.centroid ? `${fmt(target.centroid.lon)},${fmt(target.centroid.lat)}` : 'none'}`);
	}
	if (run('idle')) for (const route of ['/gods-eye', '/', '/ops']) await idleCheck(browser, route);
	if (run('cockpit')) for (const [w, h] of [[1600, 900], [1920, 1080]]) await cockpitChips(browser, w, h);
	if (run('ops')) for (const [w, h] of [[1600, 900], [1920, 1080]]) await opsLayout(browser, w, h);
	if (run('pick')) {
		if (!target || target.error) ok('pick', false, `no target event (${target?.error ?? 'events/active empty'})`);
		else await pickCheck(browser, target);
	}
	if (run('focus')) {
		if (!target || target.error) ok('focus', false, `no target event (${target?.error ?? 'events/active empty'})`);
		else await focusCheck(browser, target);
	}
	if (run('focus-nav')) {
		if (!target || target.error) ok('focus-nav', false, `no target event (${target?.error ?? 'events/active empty'})`);
		else await focusNavCheck(browser, target);
	}
	if (run('ama')) await clockCheck(browser);
	if (run('exit')) await exitCheck(browser);
	if (run('ion')) await ionCheck(browser);
} finally {
	await signOut(session);
	await browser.close();
}

if (failures) {
	console.error(`canvas-ux-smoke: ${failures} assertion(s) failed`);
	process.exit(1);
}
console.log('canvas-ux-smoke: all assertions passed');
