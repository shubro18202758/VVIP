#!/usr/bin/env node
/**
 * journeys.mjs — the judge's journeys J1–J7 (WP-15 T7), end to end through ONE deck origin: every
 * call goes through the deck's own proxy (the operator gate, the verified x-veer-officer header and
 * the loopback pinning all in the path), signed in ONCE through the shared helper
 * (scripts/lib/deck_session.mjs) as the officer 'Journey check (automated)'.
 *
 *   J1  city at rest — honest labels, no false ⚠: every fusion feed answers, cameras / units /
 *       sightings say EMULATED, the canvas shows SIMULATED FEED exactly when the oracle says the
 *       traffic is synthetic, the roads are the OSM-traced network, and no ⚠ sits on the canvas.
 *   J2  stolen vehicle: an EMULATED ANPR read → hit → PENDING R_STOLEN_SIGHTING (the penal rules
 *       withheld) → an e-challan on that plate is refused (409) and dismissed → the named officer
 *       approves the sighting → no pursuit opens from the e-challan (422) → pursuit opened from the
 *       approved sighting → interception plan → dispatch to choke #1 → approval → the unit moves →
 *       pursuit closed → the unit is released.
 *   J3  escort: a Z-class plan for the escort pair of scripts/data/route_acceptance_pairs.json → its
 *       spine on the board → the escort on EMULATED GPS → the marker moves → an AI proposal is
 *       approved by the officer, or none arrives and the approved plan holds → clear (C-F closed).
 *   J4  108 corridor: the /driver request (anonymous, ambulance only — any other agency is refused)
 *       → /ops approval by the named officer → the unit on the canvas and on the driver's own view.
 *   J5  chat: 'show me the stolen vehicle on the map' → tool call, prose and a canvas directive →
 *       the AI-suggests chip (never auto-applied) → Apply → camera on the vehicle → ONE ack.
 *   J6  citizen pages in EN / HI / GU: emulated rows badged SIMULATED FEED · EMULATED, a plan
 *       starting beyond the 2 h horizon under 'Later' with its IST date, no plate, no pursuit.
 *   J7  reboot checklist (manual): the steps, and the recorded evidence read from
 *       `journalctl --user -u veer-up` (read-only) — scripts/data/journeys/reboot_evidence.json
 *       when the journal is not on this host.
 *
 * Every row a journey creates is labelled 'JOURNEY CHECK (automated)' (titles, metadata.label — so
 * the citizen projection badges it EMULATED, never LIVE), every plate it invents carries EM, and
 * every event, dispatch, escort and earmark it opens is closed again at the end (also on failure,
 * and on SIGINT / SIGTERM); a cleanup step that cannot close its row is reported and fails the run.
 * Approvals here are the journey officer's own, on its own rows only — it never decides a row it
 * did not create, and the one e-challan approval it sends (J2, on its own stolen exercise plate)
 * must come back refused (409): no fine on a reported-stolen vehicle.
 *
 * Usage:
 *   DECK_URL=http://127.0.0.1:5315 VEER_OPERATOR_PIN_FILE=…/pin node scripts/journeys.mjs
 *   node scripts/journeys.mjs --live            # the live deck :5273 — only with the user's go-ahead
 *   --only J2,J5   --no-browser (API-level checks only)   --out <dir>
 * Env: DECK_URL / DECK_PORT (default the live :5273, refused without --live), the PIN through the
 *   helper (VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE or veer.env), JOURNEY_OFFICER (private stacks
 *   only), JOURNEY_OUT_DIR (default $VEER_LOG_DIR/journeys-<stamp>), JOURNEY_ESCORT_PAIR
 *   (escort_demo | conditional), JOURNEY_ESCORT_WATCH_SEC (90), PUPPETEER_FROM, CHROME_PATH.
 * Models: J3 (plan) and J5 (chat) call the shared decider / Gemma — one call at a time, in order.
 * Exit: 0 when J1–J6 pass (J7 is a recorded manual check) · 1 a journey failed, or a cleanup left a
 * row open · 2 usage / refusal · 130 / 143 interrupted (the rows were closed first).
 */

import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { deckFetch, puppeteerCookies, signIn, signOut } from './lib/deck_session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VVIP = path.resolve(HERE, '..');
const argv = process.argv.slice(2);
const flag = (f) => argv.includes(f);
const opt = (f) => (argv.indexOf(f) >= 0 ? argv[argv.indexOf(f) + 1] : null);
if (flag('-h') || flag('--help')) {
	console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('*/')[0]);
	process.exit(0);
}

const LIVE = flag('--live');
const BROWSER = !flag('--no-browser');
const ONLY = new Set((opt('--only') ?? '').split(',').map((s) => s.trim().toUpperCase()).filter(Boolean));
const JOURNEY_IDS = ['J1', 'J2', 'J3', 'J4', 'J5', 'J6', 'J7'];
const unknownIds = [...ONLY].filter((id) => !JOURNEY_IDS.includes(id));
if (unknownIds.length) {
	console.error(`journeys: --only ${unknownIds.join(',')}: unknown journey (one of ${JOURNEY_IDS.join(',')})`);
	process.exit(2);
}
const DECK = (process.env.DECK_URL ?? `http://127.0.0.1:${process.env.DECK_PORT ?? '5273'}`).replace(/\/+$/, '');
const LIVE_DECK_PORT = '5273';
try {
	if (!/^https?:$/.test(new URL(DECK).protocol)) throw new Error('not http(s)');
} catch {
	console.error(`journeys: DECK_URL ${DECK} is not an http(s) URL`);
	process.exit(2);
}
const OFFICER = LIVE ? 'Journey check (automated)' : (process.env.JOURNEY_OFFICER ?? 'Journey check (automated)');
const LABEL = 'JOURNEY CHECK (automated)';
const STAMP = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '');
const SUFFIX = STAMP.slice(-6);
const ESCORT_WATCH_MS = Number(process.env.JOURNEY_ESCORT_WATCH_SEC ?? 90) * 1000;

if ((new URL(DECK).port || '80') === LIVE_DECK_PORT && !LIVE) {
	console.error(
		`journeys: ${DECK} is the live deck — the journeys WRITE (labelled rows, closed again). Point DECK_URL at a ` +
			'private stack, or pass --live with the user\'s go-ahead.',
	);
	process.exit(2);
}

function logDir() {
	if (process.env.VEER_LOG_DIR) return process.env.VEER_LOG_DIR;
	const envFile = process.env.VEER_ENV_FILE ?? path.join(os.homedir(), '.config/veer/veer.env');
	const line = fs.existsSync(envFile)
		? fs.readFileSync(envFile, 'utf8').split(/\r?\n/).filter((l) => l.startsWith('VEER_LOG_DIR=')).pop()
		: null;
	return line ? line.slice('VEER_LOG_DIR='.length).replace(/^"(.*)"$/, '$1') : path.join(VVIP, '.run-models/logs');
}
const OUT = opt('--out') ?? process.env.JOURNEY_OUT_DIR ?? path.join(logDir(), `journeys-${STAMP}`);
fs.mkdirSync(OUT, { recursive: true });

// ─── reporting ───────────────────────────────────────────────────────────────

const results = [];
let cur = null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function begin(id, title) {
	cur = { id, title, status: 'pass', checks: [], notes: [], ids: {}, ms: Date.now() };
	results.push(cur);
	console.log(`\n── ${id} ${title}`);
}
function ok(name, cond, detail = '') {
	cur.checks.push({ name, ok: !!cond, detail: String(detail) });
	if (!cond) cur.status = 'fail';
	console.log(`${cond ? 'ok  ' : 'FAIL'} ${cur.id} ${name}${detail !== '' ? ` — ${detail}` : ''}`);
	return !!cond;
}
function note(text) {
	cur.notes.push(text);
	console.log(`     ${text}`);
}
const want = (id) => ONLY.size === 0 || ONLY.has(id);

// ─── the deck (one origin, one session) ──────────────────────────────────────

let session = null;

/** One call through the deck: {status, json}; status 0 when the deck did not answer. */
async function api(p, { method = 'GET', body, anon = false, timeoutMs = 30_000 } = {}) {
	const init = { method, headers: {}, signal: AbortSignal.timeout(timeoutMs) };
	if (body !== undefined) {
		init.body = JSON.stringify(body);
		init.headers['content-type'] = 'application/json';
	}
	try {
		const res = anon || !session ? await fetch(DECK + p, init) : await deckFetch(session, p, init);
		const text = await res.text();
		let json = null;
		try {
			json = text ? JSON.parse(text) : null;
		} catch {
			json = { raw: text.slice(0, 300) };
		}
		return { status: res.status, json };
	} catch (err) {
		return { status: 0, json: { detail: err instanceof Error ? err.message : String(err) } };
	}
}
const detailOf = (r) => `HTTP ${r.status}${r.json?.detail ? `: ${typeof r.json.detail === 'string' ? r.json.detail : JSON.stringify(r.json.detail)}` : ''}`.slice(0, 260);

const cleanups = [];
const defer = (label, fn) => cleanups.push({ label, fn });

function distM(a, b) {
	const rad = Math.PI / 180;
	const x = (b[0] - a[0]) * rad * Math.cos(((a[1] + b[1]) / 2) * rad);
	const y = (b[1] - a[1]) * rad;
	return Math.sqrt(x * x + y * y) * 6_371_000;
}
const iso = (ms) => new Date(ms).toISOString();

// ─── the browser (optional: --no-browser keeps every check at the API level) ──

let browser = null;
let canvas = null;
const pageErrors = [];

function resolvePuppeteer() {
	for (const from of [process.env.PUPPETEER_FROM, path.join(VVIP, 'frontend/package.json'), '/home/sandy050405/argus/veer/VVIP/frontend/package.json']) {
		if (!from) continue;
		try {
			const req = createRequire(from);
			return req('puppeteer').default ?? req('puppeteer');
		} catch {
			// next candidate
		}
	}
	return null;
}
function chromePath(puppeteer) {
	const cache = path.join(os.homedir(), '.cache/puppeteer/chrome');
	const cands = [process.env.CHROME_PATH];
	if (fs.existsSync(cache)) {
		for (const b of fs.readdirSync(cache).sort().reverse()) cands.push(path.join(cache, b, 'chrome-linux-arm64/chrome'), path.join(cache, b, 'chrome-linux64/chrome'));
	}
	const found = cands.filter(Boolean).find((p) => fs.existsSync(p));
	if (found) return found;
	try {
		return puppeteer.executablePath();
	} catch {
		return null;
	}
}
async function openBrowser() {
	if (browser || !BROWSER) return browser;
	const puppeteer = resolvePuppeteer();
	const exe = puppeteer ? chromePath(puppeteer) : null;
	if (!puppeteer || !exe) {
		console.log('     (no puppeteer / Chrome found: browser checks skipped, API checks only)');
		return null;
	}
	browser = await puppeteer.launch({
		headless: true,
		executablePath: exe,
		// SwiftShader software WebGL: the combination the deck smokes validated
		args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader'],
		defaultViewport: { width: 1920, height: 1080 },
	});
	return browser;
}

/** The operator's God's-eye canvas, signed in (cookie set before the first goto). */
async function openCanvas() {
	if (canvas) return canvas;
	if (!(await openBrowser())) return null;
	const page = await browser.newPage();
	page.on('pageerror', (err) => pageErrors.push(err.message.slice(0, 200)));
	const cookies = puppeteerCookies(session ?? {});
	if (cookies.length) await page.setCookie(...cookies);
	await page.goto(`${DECK}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 90_000 });
	await page.waitForFunction(() => !!window.__GODS_EYE__, { timeout: 90_000 });
	const ready = await page.evaluate(() => window.__GODS_EYE__.ready);
	if (!ready?.ok) throw new Error(`canvas not ready: ${ready?.reason ?? 'unknown'}`);
	canvas = page;
	return canvas;
}
async function snap(page) {
	return page.evaluate(() => {
		const s = window.__GODS_EYE__;
		return s ? { phase: s.phase, camera: s.camera, selection: s.selection, registry: s.registry, layerStats: s.layerStats, vitals: s.vitals, agent: s.agent ?? null } : null;
	});
}
async function waitFor(fn, timeoutMs, stepMs = 500) {
	const deadline = Date.now() + timeoutMs;
	let last = null;
	while (Date.now() < deadline) {
		last = await fn();
		if (last) return last;
		await sleep(stepMs);
	}
	return null;
}
async function shot(page, name) {
	const file = path.join(OUT, `${name}.png`);
	await page.screenshot({ path: file }).catch(() => undefined);
	return file;
}

// ─── shared staging: an EMULATED stolen vehicle (J2; J5 asks about it) ───────

let stolen = null;

async function camerasOnNetwork() {
	const r = await api('/api/v1/cameras');
	return (r.json?.cameras ?? []).filter((c) => c.kind === 'anpr' && Number.isFinite(c.lon) && Number.isFinite(c.lat) && c.segment_id);
}

async function anprRead(plate, cam, { speed, tOffsetMs }) {
	return api('/api/v1/vehicles/sightings', {
		method: 'POST',
		body: {
			read_id: randomUUID(),
			plate,
			camera_id: cam.camera_id,
			t: iso(Date.now() - tOffsetMs),
			lon: cam.lon,
			lat: cam.lat,
			speed_kmh: speed,
			heading: Number.isFinite(cam.heading_deg) ? cam.heading_deg : null,
			vehicle_class: 'car',
			source: 'emulated_anpr',
		},
	});
}

// ─── J1 city at rest ─────────────────────────────────────────────────────────

async function j1() {
	begin('J1', 'city at rest — honest labels, no false ⚠');
	const sess = await api('/api/session');
	ok('the deck answers /api/session', sess.status === 200, `gate ${sess.json?.gate ? 'ON' : 'off'} · officer ${sess.json?.officer ?? '—'}`);
	if (sess.json?.gate) ok('signed in as the named journey officer', sess.json?.officer === OFFICER, sess.json?.officer ?? 'none');
	const ent = await api('/api/v1/city/entities');
	const feeds = ent.json?.feeds ?? {};
	const bad = Object.entries(feeds).filter(([, f]) => f?.status !== 'ok');
	ok('every city feed answers (no error behind a ⚠)', ent.status === 200 && Object.keys(feeds).length > 0 && bad.length === 0, bad.length ? bad.map(([k, f]) => `${k}:${f?.status}`).join(' ') : Object.keys(feeds).join(','));
	const ents = ent.json?.entities ?? [];
	const labelled = ents.filter((e) => e.source === 'live' || e.source === 'emulated').length;
	ok('every entity carries its source (live | emulated)', ents.length > 0 && labelled === ents.length, `${labelled}/${ents.length}`);
	const cams = ents.filter((e) => e.kind === 'camera');
	ok('the ANPR cameras are labelled EMULATED', cams.length > 0 && cams.every((c) => c.source === 'emulated'), `${cams.length} cameras`);
	for (const kind of ['unit', 'sighting']) {
		const rows = ents.filter((e) => e.kind === kind);
		ok(`every ${kind} on the picture is EMULATED`, rows.every((e) => e.source === 'emulated'), `${rows.length} ${kind}(s)`);
	}
	const units = await api('/api/v1/units');
	ok('/api/v1/units: source emulated, every unit badged', units.status === 200 && units.json?.source === 'emulated' && (units.json?.units ?? []).every((u) => u.emulated === true), `${units.json?.count ?? '?'} unit(s)`);
	const emu = await api('/api/emulator/health');
	ok('emulator seams probed for real (store, valkey, nats, oracle)', emu.status === 200 && Object.values(emu.json?.stack ?? {}).every(Boolean) && Object.keys(emu.json?.stack ?? {}).length >= 4, `${emu.json?.status}${emu.json?.degraded_reasons?.length ? ` — ${emu.json.degraded_reasons.join('; ')}` : ''}`);
	const feedsRoute = await api('/gods-eye/feeds');
	ok('live ingest connected', feedsRoute.json?.ingest?.connected === true, `${feedsRoute.json?.ingest?.msgs_per_min ?? '?'} msg/min`);
	const status = await api('/api/v1/adaptive/status');
	const simulatedTruth = status.json?.city_band?.simulated_feed;
	note(`oracle: the traffic feed is ${simulatedTruth === true ? 'SIMULATED (synthetic signal-ingress)' : simulatedTruth === false ? 'field data' : 'unknown'}`);
	const segs = await api('/api/v1/spatial/segments?min_lon=72.40&min_lat=22.90&max_lon=72.75&max_lat=23.20');
	const basis = {};
	for (const s of segs.json?.segments ?? []) basis[s.geometry_basis] = (basis[s.geometry_basis] ?? 0) + 1;
	ok('the roads are the OpenStreetMap network (no schematic line)', (segs.json?.segments ?? []).length > 0 && !basis.schematic, JSON.stringify(basis));

	const page = await openCanvas().catch((err) => {
		ok('the God\'s-eye canvas reaches ready', false, err.message);
		return null;
	});
	if (!page) return;
	ok('the God\'s-eye canvas reaches ready (signed in)', true, DECK);
	const loaded = await waitFor(async () => {
		const s = await snap(page);
		return s?.registry?.status === 'ok' && s.vitals ? s : null;
	}, 60_000);
	ok('the fused registry and the vitals rail answered', !!loaded, loaded ? JSON.stringify(loaded.registry.kinds) : 'no registry');
	await sleep(8000); // every layer polls at least once
	const s = await snap(page);
	const warn = await page.$eval('.warn-n', (el) => el.textContent?.trim() ?? '').catch(() => null);
	ok('no ⚠ on the canvas (no layer reports a failed feed)', warn === null, warn ?? 'none');
	if (simulatedTruth != null) {
		ok('SIMULATED FEED shown exactly when the feed is synthetic', s?.vitals?.feeds?.simulated === simulatedTruth, `vitals.feeds.simulated=${s?.vitals?.feeds?.simulated} · oracle=${simulatedTruth}`);
	}
	const roads = s?.vitals?.feeds?.roads;
	if (roads) ok('vitals: the roads read OSM-traced, none schematic', roads.traced > 0 && roads.schematic === 0, JSON.stringify(roads));
	ok('no page error on the canvas', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | ') || 'none');
	note(`screenshot ${await shot(page, 'J1-city-at-rest')}`);
}

// ─── J2 stolen vehicle → pursuit → choke dispatch → release ──────────────────

async function stageStolenVehicle() {
	if (stolen) return stolen;
	const cams = await camerasOnNetwork();
	if (!ok('an EMULATED ANPR camera on the network', cams.length > 0, `${cams.length} cameras`)) return null;
	const cam = cams.find((c) => /NEHRU/.test(c.camera_id)) ?? cams[0];
	const plate = `GJ27EM${(8000 + (Number(SUFFIX) % 1000)).toString()}`;
	const fir = `FIR J-${SUFFIX}/2026, Journey Check PS`;
	// a speeding read BEFORE the FIR: an ordinary e-challan proposal (penal, pending)
	const pre = await anprRead(plate, cam, { speed: 112, tOffsetMs: 4000 });
	const penal = (pre.json?.recommendations ?? []).find((r) => /^R_(SPD_SEGMENT|SIGNAL_VIOLATION|CLOSED_CORRIDOR_ENTRY)$/.test(r.rule_id));
	ok('a speeding read files an e-challan PROPOSAL (pending, never auto-approved)', pre.status === 200 && (!penal || penal.status === 'pending'), penal ? `${penal.rule_id} ${String(penal.recommendation_id).slice(0, 8)} ${penal.status}` : `${detailOf(pre)} — no penal rule fired`);
	const ear = await api(`/api/v1/vehicles/${plate}/earmark`, {
		method: 'POST',
		body: { state: 'flagged', reason: `STOLEN VEHICLE reported — ${fir} (${LABEL}, EMULATED plate)`, lawful_basis: 'stolen_vehicle_fir', retention_days: 1, earmarked_by: OFFICER },
	});
	const vehicleId = ear.json?.vehicle_id ?? null;
	if (!ok('the plate is earmarked: lawful basis stolen_vehicle_fir, retention 1 day', ear.status === 200 && !!vehicleId, ear.status === 200 ? `vehicle ${String(vehicleId).slice(0, 8)} · by ${ear.json?.earmarked_by ?? '?'}` : detailOf(ear))) return null;
	defer(`clear the journey earmark on ${plate}`, () => api(`/api/v1/vehicles/${vehicleId}/earmark/clear`, { method: 'POST', body: { officer: OFFICER } }));
	// the journey's own recommendations never outlive it in the decision queue (dismissed, with the reason)
	defer(`dismiss the journey's still-pending recommendations on ${plate}`, async () => {
		const left = await api('/api/v1/enforcement/recommendations?status=pending&limit=500');
		if (left.status !== 200) return left;
		let worst = left;
		for (const r of (left.json?.recommendations ?? []).filter((x) => x.vehicle_id === vehicleId)) {
			const d = await api(`/api/v1/enforcement/recommendations/${r.recommendation_id}/decision`, { method: 'POST', body: { decision: 'reject', officer: OFFICER, note: `${LABEL}: cleanup — an exercise row` } });
			if (d.status !== 200 && d.status !== 409) worst = d;
		}
		return worst;
	});
	const hit = await anprRead(plate, cam, { speed: 96, tOffsetMs: 2000 });
	const recs = hit.json?.recommendations ?? [];
	const sighting = recs.find((r) => r.rule_id === 'R_STOLEN_SIGHTING');
	ok('the ANPR read is a HIT: stolen_vehicle', hit.status === 200 && hit.json?.hit === 'stolen_vehicle', `${hit.json?.hit ?? detailOf(hit)} · source ${hit.json?.source}`);
	ok('→ ONE PENDING R_STOLEN_SIGHTING (evidence) and no e-challan', !!sighting && sighting.status === 'pending' && !recs.some((r) => r.rule_id !== 'R_STOLEN_SIGHTING'), `${recs.map((r) => `${r.rule_id}:${r.status}`).join(' ') || 'none'} · withheld ${JSON.stringify(hit.json?.withheld_rules ?? [])}`);
	stolen = { plate, cam, fir, vehicleId, penal, sighting };
	return sighting ? stolen : null;
}

async function j2() {
	begin('J2', 'ANPR hit → PENDING R_STOLEN_SIGHTING → named approval → pursuit → choke dispatch → unit moves → closed → released');
	const st = await stageStolenVehicle();
	if (!st) return;
	cur.ids = { plate: st.plate, vehicle_id: st.vehicleId };
	const pending = await api('/api/v1/enforcement/recommendations?status=pending&limit=500');
	ok('the sighting waits in the decision queue (PENDING)', (pending.json?.recommendations ?? []).some((r) => r.recommendation_id === st.sighting.recommendation_id && r.status === 'pending'), String(st.sighting.recommendation_id).slice(0, 8));
	if (st.penal) {
		const refused = await api(`/api/v1/enforcement/recommendations/${st.penal.recommendation_id}/decision`, {
			method: 'POST',
			body: { decision: 'approve', officer: OFFICER, note: `${LABEL}: C-N check` },
		});
		ok('approving an e-challan on the stolen plate is refused (409, owner is the victim)', refused.status === 409 && /withheld/.test(String(refused.json?.detail)), detailOf(refused));
		const dismissed = await api(`/api/v1/enforcement/recommendations/${st.penal.recommendation_id}/decision`, {
			method: 'POST',
			body: { decision: 'reject', officer: OFFICER, note: `e-challan withheld — vehicle reported stolen (${st.fir}); ${LABEL}` },
		});
		ok('…and dismissed with a reason by the named officer', dismissed.status === 200 && dismissed.json?.status === 'rejected', detailOf(dismissed));
	} else {
		note('no penal rule fired on the speeding read: the 409 check needs one (see the WARN above)');
	}
	const approve = await api(`/api/v1/enforcement/recommendations/${st.sighting.recommendation_id}/decision`, {
		method: 'POST',
		body: { decision: 'approve', officer: OFFICER, note: 'pursuit authorised on a reported-stolen vehicle (evidence, no fine)' },
	});
	const lastAudit = (approve.json?.audit_tail ?? []).slice(-1)[0] ?? {};
	ok('the named officer approves the sighting (the pursuit authorisation)', approve.status === 200 && approve.json?.status === 'approved' && approve.json?.decided_by === OFFICER, `${approve.json?.decided_by ?? detailOf(approve)} · ${lastAudit.actor_source ?? 'body'}`);
	if (session?.gate) ok('…recorded from the verified session (x-veer-officer), not a typed name', lastAudit.actor_source === 'x-veer-officer', lastAudit.actor_source ?? 'none');

	const now = Date.now();
	if (st.penal) {
		// a fine is never a pursuit basis: the dismissed e-challan cannot open one (C6 — an APPROVED e-challan of a
		// vehicle reported stolen is refused the same way, C-N: traffic-oracle's own tests cover that row, which only
		// an approved fine creates — the journeys never approve one)
		const fromFine = await api('/api/v1/events', {
			method: 'POST',
			timeoutMs: 20_000,
			body: {
				event_kind: 'vehicle_pursuit',
				protection_class: 'none',
				crowd_class: 'LOW',
				title: `PURSUIT refusal check — ${st.plate} (${LABEL})`,
				officer: OFFICER,
				route_spines: [{ label: 'pursuit anchor', segment_ids: [st.cam.segment_id] }],
				phases: [{ label: 'pursuit', window_start: iso(now), window_end: iso(now + 5 * 60_000), closure_policy: {} }],
				metadata: { vehicle_id: st.vehicleId, recommendation_id: st.penal.recommendation_id, label: LABEL },
			},
		});
		if (fromFine.json?.event_id) {
			const wrong = fromFine.json.event_id;
			defer(`cancel the pursuit wrongly opened from the e-challan ${String(wrong).slice(0, 8)}`, () => api(`/api/v1/events/${wrong}/status`, { method: 'POST', body: { to_status: 'cancelled', officer: OFFICER, note: `${LABEL}: cleanup` } }));
		}
		ok('…and no pursuit opens from the dismissed e-challan (422): only an approved sighting authorises one', fromFine.status === 422, detailOf(fromFine));
	}
	const open = await api('/api/v1/events', {
		method: 'POST',
		timeoutMs: 20_000,
		body: {
			event_kind: 'vehicle_pursuit',
			protection_class: 'none',
			crowd_class: 'LOW',
			title: `PURSUIT — ${st.plate} (${LABEL})`,
			description: `Opened by ${OFFICER} from the approved sighting ${st.sighting.recommendation_id} — ${LABEL}.`,
			officer: OFFICER,
			route_spines: [{ label: 'pursuit anchor', segment_ids: [st.cam.segment_id] }],
			phases: [{ label: 'pursuit', window_start: iso(now), window_end: iso(now + 45 * 60_000), closure_policy: {} }],
			metadata: { vehicle_id: st.vehicleId, recommendation_id: st.sighting.recommendation_id, label: LABEL },
		},
	});
	const eventId = open.json?.event_id;
	if (!ok('pursuit opened FROM the approved sighting (human-initiated, C6)', open.status === 200 && !!eventId, open.status === 200 ? `${String(eventId).slice(0, 8)} by ${open.json?.actor}` : detailOf(open))) return;
	cur.ids.pursuit_event_id = eventId;
	let closed = false;
	defer(`close the journey pursuit ${String(eventId).slice(0, 8)}`, async () =>
		closed ? null : api(`/api/v1/events/${eventId}/status`, { method: 'POST', body: { to_status: 'cancelled', officer: OFFICER, note: `${LABEL}: cleanup` } }),
	);
	const act = await api(`/api/v1/events/${eventId}/status`, { method: 'POST', body: { to_status: 'active', officer: OFFICER, note: `pursuit under way — ${LABEL}` } });
	ok('the pursuit goes active (C-F, named officer)', act.status === 200 && act.json?.to_status === 'active', act.json?.actor ?? detailOf(act));
	const plan = await api(`/api/v1/events/${eventId}/interception-plan`);
	const chokes = plan.json?.interception_junctions ?? [];
	const choke = chokes.find((j) => !j.likely_passed) ?? chokes[0];
	if (!ok('the interception plan ranks choke junctions ahead of the sighting', plan.status === 200 && !!choke, choke ? `#1 J${choke.junction_id} (${choke.graph_cost_sec} s, ${chokes.length} ranked)` : detailOf(plan))) return;
	const unitId = `JR-JOURNEY-${SUFFIX}`;
	const req = await api(`/api/v1/events/${eventId}/dispatch`, {
		method: 'POST',
		body: { agency: 'jan_rakshak', unit_id: unitId, waypoint_junction: choke.junction_id, requested_by: OFFICER, note: `dispatch to choke #1 — ${LABEL}` },
	});
	const did = req.json?.dispatch_id;
	if (!ok('a Jan Rakshak unit is REQUESTED to choke #1', req.status === 200 && req.json?.status === 'requested', did ? `${unitId} → J${choke.junction_id}` : detailOf(req))) return;
	cur.ids.dispatch_id = did;
	let units = await api('/api/v1/units');
	ok('…and does not move before a human approves it', !(units.json?.units ?? []).some((u) => u.unit_id === unitId), 'not on /units');
	const dec = await api(`/api/v1/dispatches/${did}/decision`, { method: 'POST', body: { action: 'approve', officer: OFFICER, note: LABEL } });
	ok('the named officer approves the dispatch', dec.status === 200 && dec.json?.status === 'approved' && dec.json?.decided_by === OFFICER, dec.json?.decided_by ?? detailOf(dec));
	const enr = await api(`/api/v1/dispatches/${did}/status`, { method: 'POST', body: { status: 'en_route', officer: OFFICER } });
	ok('…en route', enr.status === 200 && enr.json?.status === 'en_route', detailOf(enr));
	units = await api('/api/v1/units');
	const u1 = (units.json?.units ?? []).find((u) => u.unit_id === unitId);
	ok('the unit is on /api/v1/units — EMULATED projection, assigned to the pursuit', !!u1 && u1.emulated === true && u1.assigned_event_id === eventId, u1 ? `${u1.source} @ ${u1.position?.map((v) => v.toFixed(5)).join(',')} · eta ${u1.eta_sec ?? '—'} s` : 'absent');
	await sleep(9000);
	units = await api('/api/v1/units');
	const u2 = (units.json?.units ?? []).find((u) => u.unit_id === unitId);
	const moved = u1 && u2 ? distM(u1.position, u2.position) : 0;
	ok('the unit MOVES toward the choke (9 s later)', !!u2 && (moved > 1 || (u2.progress ?? 0) > (u1?.progress ?? 0) || u2.arrived), `${moved.toFixed(0)} m · progress ${u1?.progress ?? '—'} → ${u2?.progress ?? '—'}`);
	const done = await api(`/api/v1/events/${eventId}/status`, { method: 'POST', body: { to_status: 'completed', officer: OFFICER, note: `pursuit concluded — ${LABEL}` } });
	closed = done.status === 200;
	const walked = (done.json?.closed_dispatches ?? []).find((d) => d.dispatch_id === did);
	ok('the pursuit is closed (C-F) and its open dispatch walked shut with it', done.status === 200 && done.json?.to_status === 'completed' && !!walked, walked ? `${walked.from_status} → ${walked.to_status}` : detailOf(done));
	units = await api('/api/v1/units');
	const disp = await api(`/api/v1/dispatches/${did}`);
	ok('the unit is RELEASED (off /api/v1/units)', !(units.json?.units ?? []).some((u) => u.unit_id === unitId), `${units.json?.count ?? '?'} unit(s) left`);
	ok('…its audit tail names the system walk, never a person', (disp.json?.audit ?? []).some((a) => a.actor === 'system:event_closed'), (disp.json?.audit ?? []).map((a) => `${a.to_status}:${a.actor}`).join(' ← '));
}

// ─── J3 Z-class escort ───────────────────────────────────────────────────────

async function j3() {
	begin('J3', 'Z plan → spine → sim-GPS escort → moving marker → approval or hold → clear');
	const pairs = JSON.parse(fs.readFileSync(path.join(VVIP, 'scripts/data/route_acceptance_pairs.json'), 'utf8'));
	const pairName = process.env.JOURNEY_ESCORT_PAIR ?? 'escort_demo';
	const pair = pairs[pairName];
	if (!ok(`the ${pairName} pair from route_acceptance_pairs.json`, !!pair?.origin && !!pair?.destination, pair ? `${pair.origin.name} → ${pair.destination.name} (${pairs.vvip_class})` : 'missing')) return;
	const origin = [pair.origin.lon, pair.origin.lat];
	const destination = [pair.destination.lon, pair.destination.lat];
	const departure = iso(Date.now() + 15 * 60_000);
	const vvip = pairs.vvip_class ?? 'Z+';
	const mv = await api('/api/convoy/movements', {
		method: 'POST',
		body: { origin, destination, vvip_class: vvip, planned_departure: departure, event_kind: 'protected_movement', crowd_class: 'LOW', title: LABEL },
	});
	const mid = mv.json?.movement_id;
	if (!ok('a protected movement is registered (labelled a journey check)', mv.status === 200 && !!mid, mid ?? detailOf(mv))) return;
	cur.ids.movement_id = mid;
	let cleared = false;
	defer(`clear the journey movement ${String(mid).slice(0, 8)}`, async () =>
		cleared ? null : api(`/api/convoy/movements/${mid}/clear`, { method: 'POST', timeoutMs: 120_000, body: { officer: OFFICER, max_recovery_sec: 5 } }),
	);
	const t0 = Date.now();
	const planned = await api(`/api/convoy/movements/${mid}/plan`, { method: 'POST', timeoutMs: 300_000, body: { origin, destination, vvip_class: vvip, planned_departure: departure } });
	const plan = planned.json ?? {};
	const spine = plan.primary_route?.segment_ids ?? [];
	if (!ok('the brain plans it (decider + Gemma tiers) with a primary spine', planned.status === 200 && spine.length > 0, planned.status === 200 ? `${plan.status} · ${spine.join('→')} · ${plan.primary_route?.route_tier ?? '?'} · ${((Date.now() - t0) / 1000).toFixed(0)} s` : detailOf(planned))) return;
	note(`plan: ${String(plan.overall_reasoning ?? '').slice(0, 220)}`);
	ok('the spine runs from the pair\'s origin road to its destination road', spine[0] === pair.origin_segment && spine[spine.length - 1] === pair.destination_segment, `${spine[0]}…${spine[spine.length - 1]} vs ${pair.origin_segment}…${pair.destination_segment}`);
	const eventId = plan.event_id;
	cur.ids.event_id = eventId;
	const board = await api('/api/v1/events/active');
	const ev = (board.json?.events ?? []).find((e) => e.event_id === eventId);
	ok('the spine is on the board as the corridor event\'s primary route', !!ev && JSON.stringify(ev.routes?.[0]?.segment_ids) === JSON.stringify(spine), ev ? `${ev.status} · ${ev.title.slice(0, 70)}` : `event ${eventId ?? 'none'} not live`);
	const start = await api(`/api/convoy/movements/${mid}/escort`, { method: 'POST', body: { officer: OFFICER } });
	if (!ok('the escort starts (202) under the named officer', start.status === 202, detailOf(start))) return;
	const seen = [];
	let proposal = null;
	let decided = null;
	const deadline = Date.now() + ESCORT_WATCH_MS;
	while (Date.now() < deadline) {
		const doc = (await api(`/api/convoy/movements/${mid}/escort`)).json ?? {};
		if (doc.position) seen.push({ t: Date.now(), lon: doc.position.lon, lat: doc.position.lat, source: doc.position.source, progress: doc.progress });
		if (doc.pending_approval && !proposal) {
			proposal = doc.pending_approval;
			const a = await api(`/api/convoy/movements/${mid}/escort/approvals/${proposal.approval_id}`, { method: 'POST', body: { decision: 'approve', officer: OFFICER } });
			decided = a;
		}
		if (seen.length >= 6 && (proposal ? decided : Date.now() > deadline - 1000)) break;
		if (['completed', 'stopped', 'failed'].includes(doc.status)) break;
		await sleep(3000);
	}
	const sim = seen.filter((p) => p.source === 'emulated_gps');
	const travelled = sim.length >= 2 ? distM([sim[0].lon, sim[0].lat], [sim[sim.length - 1].lon, sim[sim.length - 1].lat]) : 0;
	ok('the convoy marker MOVES on emulated GPS (labelled emulated_gps)', sim.length >= 2 && travelled > 20, `${sim.length} fixes · ${travelled.toFixed(0)} m · progress ${sim[0]?.progress ?? '—'} → ${sim[sim.length - 1]?.progress ?? '—'}`);
	const convoy = await api('/api/v1/city/entities?kinds=convoy');
	const ce = (convoy.json?.entities ?? []).find((e) => e.id === mid || String(e.urn).endsWith(mid));
	ok('the city picture carries the convoy as EMULATED', !!ce && ce.source === 'emulated', ce ? `${ce.urn} ${ce.source}` : 'absent');
	if (canvas) {
		const onCanvas = await waitFor(async () => ((await snap(canvas))?.registry?.convoys ?? []).find((c) => c.id === mid) ?? null, 15_000);
		ok('…and the God\'s-eye canvas draws it', !!onCanvas, onCanvas ? `${onCanvas.lon.toFixed(5)},${onCanvas.lat.toFixed(5)} ${onCanvas.source}` : 'not drawn');
	}
	const escorts = await api('/api/convoy/escorts');
	ok('authority: the AI only proposes — the officer approves every switch', escorts.json?.authority === 'officer_approves_every_switch', escorts.json?.authority ?? detailOf(escorts));
	const doc = (await api(`/api/convoy/movements/${mid}/escort`)).json ?? {};
	if (proposal) {
		const outcome = (doc.approval_outcomes ?? []).find((o) => o.approval_id === proposal.approval_id);
		ok(`an AI proposal (${proposal.kind ?? proposal.action ?? 'switch'}) is APPROVED by the named officer`, decided?.status === 200, `${detailOf(decided)} · outcome ${outcome ? `${outcome.outcome ?? outcome.status} by ${outcome.decided_by ?? outcome.officer ?? '?'}` : 'pending apply'}`);
	} else {
		const route = doc.route?.segment_ids ?? doc.route?.route_segment_ids ?? null;
		ok('no AI proposal in the window — the approved plan HOLDS (nothing switched on its own)', !route || JSON.stringify(route) === JSON.stringify(spine), `route ${route ? route.join('→') : '(plan)'} · ${doc.status}`);
	}
	if (canvas) note(`screenshot ${await shot(canvas, 'J3-escort')}`);
	const clear = await api(`/api/convoy/movements/${mid}/clear`, { method: 'POST', timeoutMs: 180_000, body: { officer: OFFICER, max_recovery_sec: 10 } });
	cleared = clear.status === 200;
	const tr = clear.json?.event_transition ?? {};
	ok('CLEAR: the escort stops and the corridor event closes (C-F)', clear.status === 200 && ['completed', 'cancelled'].includes(tr.to_status) && ['transitioned', 'already'].includes(tr.outcome), `${tr.outcome ?? '?'} → ${tr.to_status ?? detailOf(clear)}`);
	const after = await api('/api/v1/events/active');
	ok('…off the board', !(after.json?.events ?? []).some((e) => e.event_id === eventId), 'not live');
}

// ─── J4 108 corridor request ─────────────────────────────────────────────────

async function labelledEvent(kind, title, startMs, endMs, spine) {
	const r = await api('/api/v1/events', {
		method: 'POST',
		body: {
			event_kind: kind,
			protection_class: 'none',
			crowd_class: 'MEDIUM',
			title: `${title} — ${LABEL}`,
			description: `${LABEL}: an exercise row, closed again by scripts/journeys.mjs.`,
			officer: OFFICER,
			planned_start: iso(startMs),
			planned_end: iso(endMs),
			route_spines: [{ label: 'primary', segment_ids: spine }],
			phases: [{ label: 'live', window_start: iso(startMs), window_end: iso(endMs), closure_policy: { advance_closure_sec: 120 } }],
			metadata: { label: LABEL },
		},
	});
	const id = r.json?.event_id ?? null;
	if (id) {
		let closedOnce = false;
		defer(`cancel the journey event ${id.slice(0, 8)} (409 = already closed by its journey)`, async () => {
			if (closedOnce) return null;
			closedOnce = true;
			return api(`/api/v1/events/${id}/status`, { method: 'POST', body: { to_status: 'cancelled', officer: OFFICER, note: `${LABEL}: cleanup` } });
		});
	}
	return { r, id };
}

async function j4() {
	begin('J4', '108 request → /ops approval → unit on the canvas');
	const now = Date.now();
	const { r, id: eventId } = await labelledEvent('procession', 'Corridor test procession', now - 60_000, now + 60 * 60_000, [1005, 1015, 1001]);
	if (!ok('a public corridor event is live (labelled: the public feed badges it EMULATED)', !!eventId, eventId ? eventId.slice(0, 8) : detailOf(r))) return;
	cur.ids.event_id = eventId;
	await api(`/api/v1/events/${eventId}/status`, { method: 'POST', body: { to_status: 'active', officer: OFFICER, note: LABEL } });
	const feed = await waitFor(async () => {
		const f = await api('/api/public/events', { anon: true });
		return (f.json?.events ?? []).find((e) => e.event_id === eventId) ?? null;
	}, 15_000, 1500);
	const waypoint = feed?.stretches?.find((s) => s.junction_id)?.junction_id ?? null;
	ok('the anonymous public feed carries it, emulated: true, with a signalised waypoint', !!feed && feed.emulated === true && !!waypoint, feed ? `stretches ${feed.stretches.map((s) => `${s.segment_id}/J${s.junction_id ?? '-'}`).join(' ')}` : 'absent');
	if (!waypoint) return;
	const gate = (await api('/api/session', { anon: true })).json?.gate;
	if (gate) {
		const police = await api(`/api/v1/events/${eventId}/dispatch`, { method: 'POST', anon: true, body: { agency: 'police', unit_id: 'PCR-JOURNEY', requested_by: '108-driver' } });
		ok('an anonymous request for any other agency is refused by the gate (403)', police.status === 403, detailOf(police));
	}
	const unitId = `108-JOURNEY-${SUFFIX}`;
	const req = await api(`/api/v1/events/${eventId}/dispatch`, {
		method: 'POST',
		anon: true,
		body: { agency: 'ambulance', unit_id: unitId, waypoint_junction: waypoint, requested_by: '108-driver', note: `green corridor request — ${LABEL}` },
	});
	const did = req.json?.dispatch_id;
	if (!ok('the 108 driver\'s request goes through anonymously (ambulance only)', req.status === 200 && req.json?.status === 'requested' && req.json?.requested_by === '108-driver', did ? `${unitId} → J${waypoint}` : detailOf(req))) return;
	cur.ids.dispatch_id = did;
	let mine = await api(`/api/public/dispatches/${did}?event=${eventId}`, { anon: true });
	ok('the driver sees "awaiting duty officer" (requested) — the driver cannot approve', mine.status === 200 && mine.json?.dispatch?.status === 'requested', mine.json?.dispatch?.status ?? detailOf(mine));
	const dec = await api(`/api/v1/dispatches/${did}/decision`, { method: 'POST', body: { action: 'approve', officer: OFFICER, note: `108 corridor approved — ${LABEL}` } });
	ok('/ops: the named officer APPROVES it', dec.status === 200 && dec.json?.status === 'approved' && dec.json?.decided_by === OFFICER, dec.json?.decided_by ?? detailOf(dec));
	await api(`/api/v1/dispatches/${did}/status`, { method: 'POST', body: { status: 'en_route', officer: OFFICER } });
	const units = await api('/api/v1/units');
	const u = (units.json?.units ?? []).find((x) => x.unit_id === unitId);
	ok('the ambulance is on /api/v1/units — EMULATED projection toward the waypoint', !!u && u.emulated === true && u.agency === 'ambulance', u ? `${u.source} · eta ${u.eta_sec ?? '—'} s · J${u.waypoint_junction ?? waypoint}` : 'absent');
	const ent = await api('/api/v1/city/entities?kinds=unit');
	ok('…and in the fused city picture the canvas draws', (ent.json?.entities ?? []).some((e) => e.id === unitId || String(e.urn).endsWith(unitId)), `${(ent.json?.entities ?? []).length} unit entities`);
	if (canvas) {
		const drawn = await waitFor(async () => {
			const s = await snap(canvas);
			const k = Object.keys(s?.layerStats ?? {}).find((id) => /unit/.test(id));
			return (s?.registry?.kinds?.unit ?? 0) > 0 && k && (s.layerStats[k].count ?? 0) > 0 ? { k, n: s.layerStats[k].count } : null;
		}, 25_000, 1000);
		ok('the God\'s-eye canvas draws the unit', !!drawn, drawn ? `${drawn.k}: ${drawn.n}` : 'not drawn');
		note(`screenshot ${await shot(canvas, 'J4-108-unit')}`);
	}
	mine = await api(`/api/public/dispatches/${did}?event=${eventId}`, { anon: true });
	ok('the driver\'s own view follows: approved, unit bound', mine.status === 200 && ['approved', 'en_route'].includes(mine.json?.dispatch?.status), `${mine.json?.dispatch?.status} · unit ${mine.json?.unit ? 'bound' : 'none'}`);
	const close = await api(`/api/v1/events/${eventId}/status`, { method: 'POST', body: { to_status: 'completed', officer: OFFICER, note: `${LABEL}: done` } });
	ok('the event closes and releases the ambulance', close.status === 200 && (close.json?.closed_dispatches ?? []).some((d) => d.dispatch_id === did), detailOf(close));
}

// ─── J5 chat → directive → chip → Apply → ack ────────────────────────────────

const QUESTION = 'show me the stolen vehicle on the map';

function parseNdjson(text) {
	const out = [];
	for (const line of text.split('\n')) {
		if (!line.trim()) continue;
		try {
			out.push(JSON.parse(line));
		} catch {
			// a torn line: ignored
		}
	}
	return out;
}

async function j5() {
	begin('J5', 'chat tool call + directive → chip → Apply → ack');
	if (!stolen) await stageStolenVehicle();
	const veh = await api('/api/v1/city/entities?kinds=vehicle');
	const vehicles = veh.json?.entities ?? [];
	ok('a stolen vehicle is in the city picture to ask about', vehicles.length > 0, `${vehicles.length} vehicle(s)`);
	const page = canvas;
	let events = null;
	if (page) {
		const streams = [];
		const acks = [];
		page.on('requestfinished', async (req) => {
			const u = req.url();
			const res = req.response();
			if (!res) return;
			// a streamed body is readable once the request finished (when DevTools kept it)
			if (u.endsWith('/api/convoy/chat/stream')) streams.push(await res.text().catch(() => ''));
			if (u.endsWith('/api/convoy/chat/directives/ack')) acks.push({ status: res.status(), body: await res.json().catch(() => ({})) });
		});
		await page.keyboard.down('Control');
		await page.keyboard.press('KeyK');
		await page.keyboard.up('Control');
		const palette = await page.waitForSelector('[data-testid="palette-input"]', { visible: true, timeout: 8000 }).then(() => true).catch(() => false);
		ok('the command palette opens (Ctrl+K)', palette);
		if (palette) {
			await page.type('[data-testid="palette-input"]', `ask ${QUESTION}`, { delay: 5 });
			await sleep(400);
			await page.keyboard.press('Enter');
		}
		const chip = await waitFor(async () => ((await page.$$('[data-testid="agent-chip"]')).length > 0 ? true : null), 240_000, 1000);
		const meta = chip ? await page.$eval('[data-testid="agent-chip"]', (el) => ({ verb: el.dataset.verb, text: (el.textContent ?? '').replace(/\s+/g, ' ').trim() })) : null;
		ok('the streamed directive shows as an AI-suggests chip', !!chip && /AI suggests/.test(meta?.text ?? ''), meta ? `${meta.verb}: ${meta.text.slice(0, 110)}` : 'no chip');
		// Esc on City Command is layered (palette → dossier → EXIT): only ever with a palette open
		if (await page.$('[data-testid="palette-input"]')) await page.keyboard.press('Escape').catch(() => undefined);
		const before = await snap(page);
		await sleep(1500);
		const still = await snap(page);
		ok('never auto-applied: the camera waits for the human', !!before?.camera && !!still?.camera && Math.abs(before.camera.lon - still.camera.lon) < 1e-6);
		await waitFor(async () => (streams.length ? true : null), 30_000, 500);
		events = streams.length ? parseNdjson(streams[0]) : null;
		if (events && !events.some((e) => e.type === 'done')) events = null; // body not kept: ask at the API
		if (chip) {
			const target = events?.find((e) => e.type === 'directive')?.data?.args?.urn ?? null;
			const want = vehicles.find((v) => v.urn === target) ?? null;
			await page.click('[data-testid="agent-apply"]');
			const landed = await waitFor(async () => {
				const s = await snap(page);
				if (!s?.camera || s.selection?.flyOutcome !== 'landed') return null;
				return !want || (Math.abs(s.camera.lon - want.lon) <= 0.01 && Math.abs(s.camera.lat - want.lat) <= 0.01) ? s : null;
			}, 15_000);
			ok('Apply flies the camera to the vehicle and selects it', !!landed, landed ? `${landed.selection.urn} · camera ${landed.camera.lon.toFixed(4)},${landed.camera.lat.toFixed(4)}` : 'did not land');
			await waitFor(async () => (acks.length ? true : null), 10_000, 500);
			ok('exactly ONE ack, recorded under the signed-in officer', acks.length === 1 && acks[0].status === 200 && (!session?.gate || acks[0].body?.officer === OFFICER), acks.map((a) => `${a.status} ${a.body?.decision ?? ''} by ${a.body?.officer ?? '—'} → ${a.body?.recorded_in ?? ''}`).join(' | ') || 'none');
			note(`screenshot ${await shot(page, 'J5-directive-applied')}`);
		}
	}
	let where = 'the palette turn';
	if (!events) {
		// API level (no browser, or the stream body was not readable): the same call the palette makes
		where = 'the same question at the API';
		const r = await fetch(`${DECK}/api/convoy/chat/stream`, {
			method: 'POST',
			headers: { 'content-type': 'application/json', ...(session?.cookie ? { cookie: session.cookie } : {}) },
			body: JSON.stringify({ message: QUESTION }),
			signal: AbortSignal.timeout(240_000),
		}).catch((err) => ({ ok: false, status: 0, text: async () => err.message }));
		events = r.ok ? parseNdjson(await r.text()) : [];
		if (!page) {
			const d = events.find((e) => e.type === 'directive')?.data;
			const sid = events.find((e) => e.type === 'done')?.data?.session_id;
			if (d && sid) {
				const ack = await api('/api/convoy/chat/directives/ack', { method: 'POST', body: { session_id: sid, directive_id: d.id, decision: 'applied', officer: OFFICER } });
				ok('Apply → ONE ack, recorded under the signed-in officer (API level)', ack.status === 200 && (!session?.gate || ack.json?.officer === OFFICER), `${detailOf(ack)} · ${ack.json?.recorded_in ?? ''}`);
			}
		}
	}
	const types = events.map((e) => e.type);
	const tool = events.find((e) => e.type === 'tool_call')?.data;
	const prose = events.filter((e) => e.type === 'token').map((e) => e.data).join('');
	const directive = events.find((e) => e.type === 'directive')?.data;
	note(`stream checks on ${where}: ${types.length} NDJSON events`);
	ok('the turn calls a city tool', !!tool, tool ? `${tool.name}(${JSON.stringify(tool.args).slice(0, 80)})` : types.join(','));
	ok('…answers in prose (never a raw JSON envelope)', prose.trim().length > 0 && !/^\s*\{/.test(prose), prose.replace(/\s+/g, ' ').slice(0, 140));
	ok('…and proposes a canvas directive with its provenance', !!directive && /^canvas_/.test(directive.verb) && !!directive.provenance?.session_id, directive ? `${directive.verb} ${JSON.stringify(directive.args)} · ${directive.provenance?.tier}` : 'none');
	// C6: a full plate may reach only a NAMED officer (a signed-in session); unnamed, every plate reads GJ27EM•••1
	const fullPlates = prose.match(new RegExp(PLATE_RE.source, 'g')) ?? [];
	const named = !!session?.gate && !!session?.officer;
	ok('a full plate in the answer only for a named officer (masked otherwise)', fullPlates.length === 0 || named, fullPlates.length ? `${fullPlates.length} full plate(s), shown to the named officer '${OFFICER}'` : 'every plate masked in the answer');
}

// ─── J6 citizen pages EN / HI / GU ───────────────────────────────────────────

const EMU_BADGE = { en: 'SIMULATED FEED · EMULATED', hi: 'सिम्युलेटेड फ़ीड · EMULATED', gu: 'સિમ્યુલેટેડ ફીડ · EMULATED' };
const LATER_TITLE = { en: 'Later — beyond the next 2 hours', hi: 'बाद में — अगले २ घंटों के बाद', gu: 'પછીથી — આગામી ૨ કલાક પછી' };
const PLATE_RE = /\bGJ\s?\d{2}\s?[A-Z]{1,3}\s?\d{4}\b/;

async function j6() {
	begin('J6', 'citizen pages EN / HI / GU — EMULATED labels and the 2 h horizon');
	const now = Date.now();
	const inH = await labelledEvent('work_zone', 'Corridor test work zone', now - 60_000, now + 90 * 60_000, [1012, 1049]);
	const later = await labelledEvent('procession', 'Planned test procession', now + 3 * 3600_000, now + 4 * 3600_000, [1005, 1015]);
	if (!ok('two labelled public events: one now, one starting in 3 h', !!inH.id && !!later.id, `${inH.id?.slice(0, 8) ?? detailOf(inH.r)} · ${later.id?.slice(0, 8) ?? detailOf(later.r)}`)) return;
	cur.ids = { now_event_id: inH.id, later_event_id: later.id };
	await api(`/api/v1/events/${inH.id}/status`, { method: 'POST', body: { to_status: 'active', officer: OFFICER, note: LABEL } });
	const feed = await waitFor(async () => {
		const f = await api('/api/public/events', { anon: true });
		return (f.json?.events ?? []).some((e) => e.event_id === inH.id) && (f.json?.later ?? []).some((e) => e.event_id === later.id) ? f.json : null;
	}, 20_000, 1500);
	ok('the public feed labels emulated rows (emulated_labelling, C-M)', feed?.emulated_labelling === true, String(feed?.emulated_labelling));
	const row = feed?.events?.find((e) => e.event_id === inH.id);
	const lrow = feed?.later?.find((e) => e.event_id === later.id);
	ok('the event inside the horizon is a full card, emulated: true', !!row && row.emulated === true, row ? `${row.event_kind} ${row.status}` : 'absent');
	ok('the event beyond 2 h is a dated Later row only, emulated: true', !!lrow && lrow.emulated === true && !!lrow.starts_at, lrow ? `${lrow.starts_at}` : 'absent');
	ok('no pursuit or crime incident on the public feed', ![...(feed?.events ?? []), ...(feed?.later ?? [])].some((e) => ['vehicle_pursuit', 'crime_incident'].includes(e.event_kind)));
	ok('no plate anywhere in the public feed', !PLATE_RE.test(JSON.stringify(feed ?? {})));
	if (!(await openBrowser())) return;
	const ctx = await browser.createBrowserContext(); // anonymous: no operator cookie
	try {
		const page = await ctx.newPage();
		await page.setViewport({ width: 1280, height: 900 });
		for (const code of ['en', 'hi', 'gu']) {
			await page.setCookie({ name: 'veer_lang', value: code, url: DECK });
			await page.goto(`${DECK}/citizen`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
			const card = `.event-card[data-event="${inH.id}"]`;
			const shown = await page.waitForSelector(card, { timeout: 25_000 }).then(() => true).catch(() => false);
			const htmlLang = await page.evaluate(() => document.documentElement.lang);
			const badge = shown ? await page.$eval(`${card} [data-source-badge="emulated"]`, (e) => e.textContent ?? '').catch(() => '') : '';
			const laterRow = await page.$eval(`[data-testid="citizen-later"] .later-row[data-event="${later.id}"]`, (e) => ({ when: e.querySelector('.later-when')?.textContent?.trim() ?? '' })).catch(() => null);
			const body = await page.evaluate(() => document.body.innerText);
			ok(`/citizen ${code}: <html lang> = ${code}`, htmlLang === code, htmlLang);
			ok(`/citizen ${code}: the card is badged '${EMU_BADGE[code]}', never LIVE`, badge.includes(EMU_BADGE[code]) && !(await page.$(`${card} [data-source-badge="live"]`)), badge.trim() || 'no badge');
			ok(`/citizen ${code}: the 3 h plan sits under '${LATER_TITLE[code]}' with its date`, !!laterRow && body.includes(LATER_TITLE[code]) && laterRow.when.length > 0, laterRow?.when ?? 'no Later row');
			ok(`/citizen ${code}: no plate on the page`, !PLATE_RE.test(body));
			await shot(page, `J6-citizen-${code}`);
		}
		note(`screenshots ${path.join(OUT, 'J6-citizen-{en,hi,gu}.png')}`);
	} finally {
		await ctx.close();
	}
}

// ─── J7 reboot checklist (manual, recorded) ──────────────────────────────────

function journalReboots() {
	try {
		const text = execFileSync('journalctl', ['--user', '-u', 'veer-up', '--no-pager', '-o', 'short-iso-precise'], { encoding: 'utf8', timeout: 15_000, stdio: ['ignore', 'pipe', 'ignore'] });
		const runs = [];
		let curRun = null;
		for (const line of text.split('\n')) {
			const t = line.split(' ')[0];
			if (/Starting veer-up\.service/.test(line)) {
				curRun = { service_start: t, ready: null, readiness: null };
				runs.push(curRun);
			} else if (curRun && /: READY \d+\/\d+$/.test(line)) {
				curRun.ready = t;
				curRun.readiness = line.match(/READY \d+\/\d+$/)[0];
			}
		}
		return runs.filter((r) => r.ready).map((r) => ({ ...r, seconds_to_ready: Math.round((Date.parse(r.ready) - Date.parse(r.service_start)) / 100) / 10 }));
	} catch {
		return null;
	}
}

async function j7() {
	begin('J7', 'reboot checklist (manual) — the recorded evidence');
	cur.status = 'manual';
	const steps = [
		'1. Power-cycle the GB10 (sudo reboot). Nobody logs in: the user manager lingers (loginctl Linger=yes).',
		'2. veer-up.service (WantedBy veer.target, enabled) brings the stack up: compose, migrations, readiness —',
		'   follow it with `journalctl --user -u veer-up -f` until READY 11/11.',
		'3. On the GB10 browser open http://127.0.0.1:5273 → sign in as the duty officer (name + PIN).',
		'4. /ops, /gods-eye and /citizen load; `bash scripts/demo_reset.sh` (dry-run) shows the inventory.',
	];
	for (const s of steps) note(s);
	const fixture = JSON.parse(fs.readFileSync(path.join(HERE, 'data/journeys/reboot_evidence.json'), 'utf8'));
	for (const r of fixture.reboots) {
		note(`recorded: boot ${r.boot_id.slice(0, 8)} — veer-up ${r.service_start.slice(11, 19)} → ${r.readiness} ${r.ready.slice(11, 19)} IST (${Math.floor(r.seconds_to_ready / 60)} min ${Math.round(r.seconds_to_ready % 60)} s), no manual command`);
	}
	const live = journalReboots();
	if (live?.length) {
		for (const r of live.slice(-3)) note(`journal: veer-up ${r.service_start} → ${r.readiness} in ${Math.floor(r.seconds_to_ready / 60)} min ${Math.round(r.seconds_to_ready % 60)} s`);
	} else {
		note('journal: no veer-up runs on this host (the recorded evidence above stands)');
	}
	const first = fixture.reboots[0];
	ok('recorded evidence: a real reboot reached READY 11/11 with no manual command', first.readiness === 'READY 11/11' && first.manual_command === false, `2026-10-23 · ${Math.floor(first.seconds_to_ready / 60)} min ${Math.round(first.seconds_to_ready % 60)} s`);
	cur.status = cur.status === 'fail' ? 'fail' : 'manual';
}

// ─── run ─────────────────────────────────────────────────────────────────────

/**
 * Close every row the journeys opened, newest first — once, also on SIGINT / SIGTERM. A step that answers
 * 2xx, 404 (already gone) or 409 (already closed / decided) is done; anything else leaves a labelled row open
 * on the stack, so it is reported (and fails the run) with what to close by hand.
 */
const cleanupLog = [];
let cleaning = null;
function runCleanups() {
	cleaning ??= (async () => {
		const steps = cleanups.splice(0).reverse();
		if (steps.length) console.log('\n── cleanup (every row a journey opened is closed again)');
		for (const c of steps) {
			let status = null;
			let note = '';
			try {
				const r = await c.fn();
				status = r?.status ?? null;
				if (status !== null && !(status >= 200 && status < 300) && status !== 404 && status !== 409) note = detailOf(r);
			} catch (err) {
				note = err instanceof Error ? err.message : String(err);
			}
			cleanupLog.push({ label: c.label, status, ok: !note, note: note || undefined });
			console.log(`${note ? 'WARN' : 'ok  '} ${c.label}${status !== null ? ` — HTTP ${status}` : ''}${note ? ` — ${note.slice(0, 200)} — LEFT OPEN: close it by hand` : ''}`);
		}
	})();
	return cleaning;
}
for (const sig of ['SIGINT', 'SIGTERM']) {
	process.once(sig, () => {
		console.error(`\njourneys: ${sig} — closing every row the journeys opened before exiting (again to abort)`);
		void runCleanups()
			.then(() => (browser ? browser.close().catch(() => undefined) : undefined))
			.then(() => (session ? signOut(session) : undefined))
			.finally(() => process.exit(sig === 'SIGINT' ? 130 : 143));
	});
}

console.log(`journeys → ${DECK} as '${OFFICER}'${LIVE ? ' (LIVE — with the user\'s go-ahead)' : ''} · artifacts ${OUT}`);
let exitCode = 0;
try {
	session = await signIn(DECK, OFFICER);
	console.log(session.gate ? `signed in (operator gate ON) as '${OFFICER}'` : 'operator gate OFF on this deck (writes carry the body officer)');
	const plan = [['J1', j1], ['J2', j2], ['J3', j3], ['J4', j4], ['J5', j5], ['J6', j6], ['J7', j7]];
	for (const [id, fn] of plan) {
		if (!want(id)) continue;
		try {
			await fn();
		} catch (err) {
			if (!cur || cur.id !== id) begin(id, '(aborted)');
			ok('journey ran to its end', false, err instanceof Error ? `${err.message}`.slice(0, 300) : String(err));
		}
		cur.ms = Date.now() - cur.ms;
	}
} catch (err) {
	console.error(`journeys: ${err instanceof Error ? err.message : String(err)}`);
	exitCode = 2;
} finally {
	await runCleanups();
	if (browser) await browser.close().catch(() => undefined);
	if (session) await signOut(session);
}

console.log('\n── summary');
for (const r of results) {
	const failed = r.checks.filter((c) => !c.ok).length;
	console.log(`${r.status.toUpperCase().padEnd(6)} ${r.id} ${r.title} — ${r.checks.length - failed}/${r.checks.length} checks · ${(r.ms / 1000).toFixed(0)} s`);
}
const leftOpen = cleanupLog.filter((c) => !c.ok);
if (leftOpen.length) console.log(`FAIL   cleanup — ${leftOpen.length} step(s) left a labelled row open (WARN lines above)`);
fs.writeFileSync(path.join(OUT, 'journeys-results.json'), JSON.stringify({ deck: DECK, officer: OFFICER, live: LIVE, at: new Date().toISOString(), results, cleanup: cleanupLog }, null, 1));
console.log(`results ${path.join(OUT, 'journeys-results.json')}`);
if (exitCode === 0 && (results.some((r) => r.status === 'fail') || leftOpen.length)) exitCode = 1;
process.exit(exitCode);
