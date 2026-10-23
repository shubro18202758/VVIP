/**
 * Deck data-contract tests — the parsers and stores that turn oracle JSON
 * into what the screens draw, run against RECORDED oracle payloads.
 *
 *   node tests/contract/run.mjs                   # replay fixtures (no network)
 *   node tests/contract/run.mjs --live [base]     # same core assertions, live GETs
 *   node tests/contract/run.mjs --record [base]   # re-record live fixtures (GET only)
 *
 * base defaults to http://127.0.0.1:8091 (the argus oracle). Every mode is
 * READ-ONLY against the stack: replay never touches the network (writer
 * POSTs are answered by the in-process stub and their bodies inspected);
 * --live and --record refuse any non-GET.
 *
 * How: esbuild bundles the REAL modules — gods-eye/data.ts, ops/client.ts,
 * ops/store.svelte.ts, the corridor feed and the layers' pure helpers —
 * with `$lib` aliased to src/lib (the ops-write-smoke pattern; esbuild is
 * vendored in node_modules). The .svelte.ts stores use Svelte 5 runes, which
 * only the Svelte compiler expands; the harness defines `$state` as identity
 * before importing, so the stores run as plain classes (no reactivity is
 * needed to test their logic). `fetch` is replaced by a router over
 * tests/contract/fixtures (manifest.json maps path+query → file + status;
 * unknown paths answer 404 like an undeployed route).
 *
 * Fixtures marked origin "contract" (cameras, event audit, overrides) are
 * built from contract C-C plus real rows read from the live DB, because the
 * producing routes (WP-02) are not deployed on the live oracle yet; the
 * live 404 for each is recorded too, and both paths are asserted. The
 * units, sightings and trace payloads are FROZEN the same way
 * (*.contract.json, never written by --record): WP-16 turned an
 * un-earmarked vehicle's reads into a 404 'no lawful basis', so a re-record
 * would silently change what the trace-ladder assertions mean. The 404
 * back-off and the C6 'withheld' answer are driven by net.overrides, never
 * by whatever the oracle happened to answer at recording time.
 *
 * CONTRACT_FIXTURES=<dir> points the runner (record and replay alike) at a
 * scratch copy of the fixtures — re-record into a copy, replay it, and the
 * committed fixtures are never touched.
 *
 * Exits 0 with `ok <name> — <detail>` lines (showcase style); exit 1 on any
 * assertion failure.
 */

import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..', '..');
const FIXTURES = process.env.CONTRACT_FIXTURES ? path.resolve(process.env.CONTRACT_FIXTURES) : path.join(HERE, 'fixtures');
const MANIFEST = path.join(FIXTURES, 'manifest.json');

const argv = process.argv.slice(2);
const MODE = argv.includes('--record') ? 'record' : argv.includes('--live') ? 'live' : 'replay';
const BASE = (argv.find((a) => /^https?:\/\//.test(a)) ?? 'http://127.0.0.1:8091').replace(/\/$/, '');

const PURSUIT = '4508d86d-566b-438f-8414-b3d67fd7e6e4';
const VEHICLE = '1b94e9d2-7e11-4174-a735-7dd3be3189cf';
const RATH_YATRA = '3ef08290-66d8-511b-a25a-3d2967c79985';

/**
 * The live GETs the deck's parsers consume (recorded verbatim). Units,
 * sightings and the trace are NOT here: they are frozen *.contract.json
 * fixtures (see the header).
 */
const RECORDED = [
	['/api/v1/vehicles', 'vehicles.json'],
	['/api/v1/signals/state', 'signals_state.json'],
	['/api/v1/cameras', 'cameras.live.json'],
	['/api/v1/events/active', 'events_active.json'],
	['/api/v1/signals/plans', 'signals_plans.json'],
	[`/api/v1/adaptive/situation_log?event_id=${PURSUIT}&limit=120`, 'situation_log_pursuit.json'],
	[`/api/v1/adaptive/situation_log?event_id=${RATH_YATRA}&limit=120`, 'situation_log_rath_yatra.json'],
	['/api/v1/corridor/summary', 'corridor_summary.json'],
	[`/api/v1/events/${PURSUIT}/interception-plan?max_cost_sec=600`, 'interception_plan.json'],
	[`/api/v1/events/${PURSUIT}/audit?limit=50`, 'event_audit.live.json'],
	['/api/v1/adaptive/overrides?limit=50', 'overrides.live.json'],
	['/api/v1/signals/history?limit=20', 'signals_history.json'],
	['/api/v1/movements/active', 'movements_active.json'],
	['/api/v1/adaptive/status', 'adaptive_status.json'],
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

// ─── --record: live GETs → fixtures ─────────────────────────────────────────

if (MODE === 'record') {
	mkdirSync(FIXTURES, { recursive: true });
	const prev = JSON.parse(readFileSync(MANIFEST, 'utf8'));
	const fixtures = { ...prev.fixtures };
	for (const [route, file] of RECORDED) {
		const res = await fetch(`${BASE}${route}`, { signal: AbortSignal.timeout(15000) });
		const text = await res.text();
		let body;
		try {
			body = JSON.parse(text);
		} catch {
			body = { detail: text.slice(0, 200) };
		}
		if (file.endsWith('.contract.json')) throw new Error(`--record must never write a frozen fixture (${file})`);
		writeFileSync(path.join(FIXTURES, file), `${JSON.stringify(body, null, '\t')}\n`);
		fixtures[route] = { file, status: res.status, origin: 'live' };
		console.log(`rec  ${res.status} ${route} → ${file}`);
	}
	writeFileSync(
		MANIFEST,
		`${JSON.stringify({ ...prev, recorded_at: new Date().toISOString(), recorded_from: BASE, fixtures }, null, '\t')}\n`,
	);
	process.exit(0);
}

// ─── bundle the real modules ────────────────────────────────────────────────

const require2 = createRequire(path.join(DECK, 'package.json'));
const esbuild = require2('esbuild');
const OUT = path.join(DECK, 'tests', 'artifacts', 'contract-bundle.mjs');
const ENTRY = `
export * as data from './src/lib/gods-eye/data';
export * as client from './src/lib/ops/client';
export * as opsStore from './src/lib/ops/store.svelte';
export * as corridorFeed from './src/lib/ops/components/template/corridor.svelte';
export * as unitsLayer from './src/lib/gods-eye/layers/agency-units';
export * as signalsLayer from './src/lib/gods-eye/layers/signal-network';
export * as cityApi from './src/lib/gods-eye/city-api';
export * as usecases from './src/lib/ops/usecases';
export * as opsStrings from './src/lib/i18n/strings.ops';
export * as registryMod from './src/lib/gods-eye/entities/registry.svelte';
export * as fusionFeedMod from './src/lib/gods-eye/hud/feed.svelte';
`;
{
	const result = await esbuild.build({
		stdin: { contents: ENTRY, resolveDir: DECK, loader: 'ts', sourcefile: 'contract-entry.ts' },
		absWorkingDir: DECK,
		alias: { $lib: './src/lib' },
		bundle: true,
		format: 'esm',
		platform: 'node',
		outfile: OUT,
		logLevel: 'silent',
	});
	ok('contract bundle builds (esbuild, $lib aliased)', result.errors.length === 0, `${result.errors.length} errors`);
}

// Svelte 5 runes are compiler macros: identity stubs let the stores run as plain classes.
globalThis.$state = (v) => v;
globalThis.$state.raw = (v) => v;

// ─── fetch router (replay) / live GET passthrough ───────────────────────────

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
const fixture = (file) => JSON.parse(readFileSync(path.join(FIXTURES, file), 'utf8'));

const net = {
	log: [],
	overrides: new Map(), // route → {status, body} set by a scenario
	posts: [],
	inflight: 0,
};

function respond(status, body) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function routeOf(url) {
	return url.startsWith('http') ? url.slice(url.indexOf('/', url.indexOf('//') + 2)) : url;
}

async function replayFetch(input, init = {}) {
	const url = typeof input === 'string' ? input : input.url;
	const method = (init.method ?? 'GET').toUpperCase();
	const route = routeOf(url);
	net.log.push({ method, url, route, body: init.body ?? null });
	net.inflight++;
	try {
		await sleep(1); // a real async boundary, as on the wire
		if (url.startsWith('http://localhost:8090')) throw new TypeError('fetch failed (absolute emulator URL)');
		if (method !== 'GET') {
			net.posts.push({ route, body: init.body ? JSON.parse(String(init.body)) : null });
			const canned = net.overrides.get(`${method} ${route}`);
			return canned ? respond(canned.status, canned.body) : respond(200, { ok: true });
		}
		const override = net.overrides.get(route) ?? net.overrides.get(route.split('?')[0]);
		if (override) return respond(override.status, override.body);
		const hit = manifest.fixtures[route] ?? manifest.fixtures[route.split('?')[0]];
		if (hit) return respond(hit.status, fixture(hit.file));
		return respond(404, { detail: 'Not Found' });
	} finally {
		net.inflight--;
	}
}

const realFetch = globalThis.fetch;
async function liveFetch(input, init = {}) {
	const url = typeof input === 'string' ? input : input.url;
	const method = (init.method ?? 'GET').toUpperCase();
	if (method !== 'GET') throw new Error(`contract --live refuses ${method} ${url} (read-only)`);
	const route = routeOf(url);
	net.log.push({ method, url, route, body: null });
	if (route.startsWith('/api/emulator')) return respond(503, { detail: 'emulator not probed in --live' });
	return realFetch(`${BASE}${route}`, init);
}

globalThis.fetch = MODE === 'live' ? liveFetch : replayFetch;

/** Wait until every request the code under test started has answered. */
async function settle() {
	for (let i = 0; i < 400; i++) {
		await sleep(5);
		if (net.inflight === 0) {
			await sleep(5);
			if (net.inflight === 0) return;
		}
	}
}

const { data, client, opsStore, corridorFeed, unitsLayer, signalsLayer, cityApi, usecases, opsStrings, registryMod, fusionFeedMod } = await import(
	`${pathToFileURL(OUT).href}?t=${Date.now()}`
);

// ─── 1. canvas feeds: envelopes, phases, timestamps ─────────────────────────

{
	const stats = { total: 0, dropped: 0 };
	const rows = await data.fetchVehicles(undefined, stats);
	const gj = rows.find((v) => v.plate === 'GJ27EM9001');
	ok('fetchVehicles unwraps {vehicles: [...]}', rows.length >= 1 && stats.total === rows.length, `${rows.length} row(s), total ${stats.total}`);
	if (MODE === 'replay') ok('fetchVehicles returns exactly the recorded row', rows.length === 1, rows.map((v) => v.plate).join(','));
	ok('GJ27EM9001 present, flagged', !!gj && gj.earmarkState === 'flagged', gj ? `${gj.vehicleId} ${gj.earmarkState}` : 'missing');
	ok(
		'lastSighting.t is a NUMBER (Date.parse of the ISO wire string)',
		!!gj?.lastSighting && typeof gj.lastSighting.t === 'number' && Number.isFinite(gj.lastSighting.t),
		gj?.lastSighting ? `${gj.lastSighting.t} = ${new Date(gj.lastSighting.t).toISOString()}` : 'no sighting',
	);
	ok('lastSighting carries camera + hit', !!gj?.lastSighting?.cameraId && !!gj?.lastSighting?.hit, `${gj?.lastSighting?.cameraId} ${gj?.lastSighting?.hit}`);
}

{
	const stats = { total: 0, dropped: 0 };
	const rows = await data.fetchSignalsState(undefined, stats);
	ok('fetchSignalsState unwraps {junctions: [...]}', rows.length >= 11, `${rows.length} rows (total ${stats.total})`);
	if (MODE === 'replay') ok('fetchSignalsState returns the 11 recorded junctions', rows.length === 11);
	ok('every junction has an activePhase label', rows.every((s) => typeof s.activePhase === 'string' && s.activePhase.length > 0), rows.slice(0, 3).map((s) => `${s.junctionId}:${s.activePhase}`).join(' | '));
	ok('phaseLost is boolean and greenSec/cycleSec numeric on every row', rows.every((s) => typeof s.phaseLost === 'boolean' && typeof s.greenSec === 'number' && typeof s.cycleSec === 'number'));
	if (MODE === 'replay') {
		// Phases are computed at request time, so check every row against the
		// raw recorded active_phase object rather than fixed junction ids.
		const raw = new Map(fixture('signals_state.json').junctions.map((j) => [String(j.junction_id), j]));
		const label = (ap) => `${ap.lost ? 'lost · after ' : ''}φ${ap.index} ${ap.movements.join('+')}`;
		const mismatched = rows.filter((s) => {
			const ap = raw.get(s.junctionId).active_phase;
			return s.activePhase !== label(ap) || s.phaseLost !== ap.lost || s.greenSec !== ap.green_sec || s.phaseIndex !== ap.index;
		});
		ok('active_phase {index, movements, green_sec, lost} → label + phaseLost + greenSec on every row', mismatched.length === 0, mismatched.map((s) => s.junctionId).join(',') || `${rows.filter((s) => s.phaseLost).length} lost / ${rows.filter((s) => !s.phaseLost).length} green`);
		const green = rows.find((s) => s.phaseLost === false);
		const lost = rows.find((s) => s.phaseLost === true);
		if (green) ok(`green row label (${green.junctionId}: ${green.activePhase})`, /^φ\d+ \S/.test(green.activePhase));
		if (lost) ok(`lost-time row label (${lost.junctionId}: ${lost.activePhase})`, lost.activePhase.startsWith('lost · after φ'));
		ok('green arc share = greenSec / cycleSec', rows.every((s) => Math.abs(signalsLayer.greenShare(s) - s.greenSec / s.cycleSec) < 1e-9));
	}
	const legacy = data.parseSignalsState([{ junction_id: 9, lon: 72.5, lat: 23, cycle_sec: 90, active_phase: 'green' }]);
	ok('bare-array + legacy string phase still parse', legacy.rows.length === 1 && legacy.rows[0].activePhase === 'green');
}

{
	const stats = { total: 0, dropped: 0 };
	const rows = await data.fetchUnits(undefined, stats);
	ok('fetchUnits unwraps {units: [...]}', stats.total >= rows.length && (stats.total === 0 || rows.length >= 1), `${rows.length} drawn of ${stats.total}`);
	if (MODE === 'replay') {
		const u = rows[0];
		ok('unit lifecycle parsed (JR-EM-01 on_scene, eta 0, progress 1, emulated)', u?.unitId === 'JR-EM-01' && u.status === 'on_scene' && u.etaSec === 0 && u.progress === 1 && u.emulated === true, JSON.stringify(u && { s: u.status, e: u.etaSec, p: u.progress }));
		const line = u ? unitsLayer.unitStatusLine(u) : null;
		ok('unit label line reads status · ETA · progress', line === 'ON SCENE · ARRIVED · 100%', String(line));
	}
	const mixed = data.parseUnits({
		units: [
			{ unit_id: 'T1', agency: 'jan_rakshak', lon: 72.58, lat: 23.02, status: 'patrol', source: 'emulated_patrol' },
			{ unit_id: 'JR-2', agency: 'police', position: null, status: 'requested', emulated: true },
			{ unit_id: 'JR-3', agency: 'traffic', position: [72.6, 23.03], status: 'en_route', eta_sec: 125, progress: 0.45, arrived: false, emulated: true },
		],
	});
	ok('C-A patrol (lon/lat) parses as emulated; unpositioned unit counted, not drawn', mixed.rows.length === 2 && mixed.total === 3 && mixed.rows[0].emulated === true && mixed.rows[0].status === 'patrol');
	ok('en-route label', unitsLayer.unitStatusLine(mixed.rows[1]) === 'EN ROUTE · ETA 2m 05s · 45%', unitsLayer.unitStatusLine(mixed.rows[1]));
}

{
	client.notDeployedBackoff.reset();
	let threw = null;
	const before = net.log.filter((r) => r.route.startsWith('/api/v1/cameras')).length;
	try {
		await data.fetchCameras();
	} catch (err) {
		threw = err;
	}
	if (threw) {
		ok('cameras 404 → NotDeployed (honest chip), never a crash', threw instanceof data.NotDeployed, threw.message);
		try {
			await data.fetchCameras();
		} catch {
			// expected: answered from the 60 s back-off memory
		}
		const after = net.log.filter((r) => r.route.startsWith('/api/v1/cameras')).length;
		ok('a 404 path is not re-requested inside the back-off window', after - before === 1, `${after - before} request(s) for 2 calls`);
	} else {
		ok('cameras route answered (WP-02 deployed)', true);
	}
	const contract = data.parseCameras(fixture('cameras.contract.json'));
	ok('C-C cameras parse with reads/hits counts', contract.rows.length === 4 && contract.rows.every((c) => Number.isInteger(c.reads15m) && Number.isInteger(c.hits15m) && c.kind === 'anpr' && c.source === 'emulated'), contract.rows.map((c) => c.cameraId).join(','));
	const banned = contract.rows.flatMap((c) => Object.keys(c)).filter((k) => /plate|frame|image|person|face/i.test(k));
	ok('camera records carry no plate/frame/image/person keys (C6)', banned.length === 0, banned.join(','));
	const hot = data.parseCameras({ cameras: [{ camera_id: 'X', lon: 1, lat: 2, kind: 'anpr', source: 'registered', reads_15m: 7, hits_15m: 2 }] });
	ok('hits_15m > 0 parsed for the pulse', hot.rows[0].hits15m === 2 && hot.rows[0].reads15m === 7 && hot.rows[0].source === 'registered');
}

// ─── 2. vehicle trace ladder (trace → last 8 sightings → empty) ─────────────

{
	client.notDeployedBackoff.reset();
	const t0 = net.log.length;
	const trace = await data.fetchVehicleTrace(VEHICLE, 900);
	const tried = net.log.slice(t0).map((r) => r.route.split('?')[0].split('/').pop());
	ok('trace resolves (never NotDeployed)', !!trace && Array.isArray(trace.coordinates), `via ${trace.via}, ${trace.coordinates.length} points, rungs ${tried.join('→')}`);
	if (MODE === 'replay') {
		ok('404 trace falls back to /sightings (last 8)', trace.via === 'sightings' && trace.coordinates.length === 4);
		ok('camera hops numbered oldest → newest', trace.hops.map((h) => `${h.seq}:${h.cameraId}`).join(' ') === '1:EM-CAM-03 2:EM-CAM-04 3:EM-CAM-01 4:EM-CAM-02', trace.hops.map((h) => `${h.seq}:${h.cameraId}`).join(' '));
		ok('polyline ends at the newest sighting', JSON.stringify(trace.coordinates.at(-1)) === JSON.stringify([72.573251, 23.02397]));
		// The back-off is driven by an explicit 404 (never by what was recorded).
		net.overrides.set(`/api/v1/vehicles/${VEHICLE}/trace`, { status: 404, body: { detail: 'Not Found' } });
		client.notDeployedBackoff.reset();
		await data.fetchVehicleTrace(VEHICLE, 900);
		const t1 = net.log.length;
		await data.fetchVehicleTrace(VEHICLE, 900);
		const second = net.log.slice(t1).map((r) => r.route.split('?')[0].split('/').pop());
		ok('the 404 trace path is backed off on the next call', second.join(',') === 'sightings', second.join(','));
		net.overrides.delete(`/api/v1/vehicles/${VEHICLE}/trace`);
		// C6 (WP-16): no lawful basis to disclose an un-earmarked vehicle's reads —
		// 'withheld', nothing drawn, and the route is NOT backed off as undeployed.
		client.notDeployedBackoff.reset();
		const NO_BASIS = { status: 404, body: { detail: 'no lawful basis to disclose individual reads for this vehicle' } };
		net.overrides.set(`/api/v1/vehicles/${VEHICLE}/trace`, NO_BASIS);
		net.overrides.set(`/api/v1/vehicles/${VEHICLE}/sightings`, NO_BASIS);
		const withheld = await data.fetchVehicleTrace(VEHICLE, 900);
		ok("C6 'no lawful basis' 404 → via 'withheld', no polyline", withheld.via === 'withheld' && withheld.coordinates.length === 0, withheld.via);
		ok('…and the trace path is not backed off as not-deployed', client.notDeployedBackoff.until(`/api/v1/vehicles/${VEHICLE}/trace`) === 0);
		net.overrides.clear();
		const disclosed = data.parseTrace(VEHICLE, { geometry: { type: 'LineString', coordinates: [] }, positions: [], status: 'quiet', disclosure: 'active_earmark' });
		ok('trace status + disclosure parsed (quiet · active_earmark)', disclosed.status === 'quiet' && disclosed.disclosure === 'active_earmark');
		net.overrides.set(`/api/v1/vehicles/${VEHICLE}/trace`, { status: 200, body: { vehicle_id: VEHICLE, window_sec: 900, geometry: { type: 'LineString', coordinates: [] }, positions: [], latest_t: '2026-10-18T15:19:59.301446+00:00' } });
		client.notDeployedBackoff.reset();
		const empty200 = await data.fetchVehicleTrace(VEHICLE, 900);
		ok('WP-03 200-with-empty-geometry also falls back to sightings', empty200.via === 'sightings' && empty200.coordinates.length === 4);
		net.overrides.set(`/api/v1/vehicles/${VEHICLE}/sightings`, { status: 200, body: { vehicle_id: VEHICLE, sightings: [] } });
		const none = await data.fetchVehicleTrace(VEHICLE, 900);
		ok('no sightings at all → empty trace, not an error', none.via === 'none' && none.coordinates.length === 0);
		net.overrides.clear();
		const timed = data.parseTrace(VEHICLE, { geometry: { type: 'LineString', coordinates: [[1, 2], [3, 4]] }, positions: [{ t: '2026-10-18T15:00:00Z', position: [1, 2], camera_id: 'A' }, { t: '2026-10-18T15:01:00Z', position: [3, 4], camera_id: 'B' }], source: 'emulated_anpr' });
		ok('a timed 200 trace keeps its own hops', timed.via === 'trace' && timed.hops.map((h) => h.cameraId).join('') === 'AB');
		// Two consumers (traces layer + pursuit overlay) in the same tick → one request per rung.
		client.notDeployedBackoff.reset();
		const t2 = net.log.length;
		const [a, b] = await Promise.all([
			data.fetchVehicleTrace(VEHICLE, 900, AbortSignal.timeout(6000)),
			data.fetchVehicleTrace(VEHICLE, 900, AbortSignal.timeout(6000)),
		]);
		const rungs = net.log.slice(t2).map((r) => r.route.split('?')[0].split('/').pop());
		ok('concurrent identical GETs share one request (no 404 twice)', rungs.join(',') === 'trace,sightings' && a.coordinates.length === 4 && b.coordinates.length === 4, rungs.join(','));
		const t3 = net.log.length;
		await Promise.all([client.fetchActiveEvents(), client.fetchActiveEvents()]);
		ok('ops client GETs coalesce too', net.log.slice(t3).length === 1);
	}
}

// ─── 3. events board mapping ────────────────────────────────────────────────

{
	const events = await client.fetchActiveEvents();
	const pursuit = events.find((e) => e.eventKind === 'vehicle_pursuit');
	ok('fetchActiveEvents maps the live board', events.length >= 1, events.map((e) => `${e.name.slice(0, 24)}…`).join(' | '));
	ok("mapEventRow(pursuit).name ← title (starts with 'PURSUIT')", !!pursuit && pursuit.name.startsWith('PURSUIT'), pursuit?.name);
	ok('routeSegmentIds ← flatten(routes[].segment_ids) = [1012,1015,1005,1013]', JSON.stringify(pursuit?.routeSegmentIds) === '[1012,1015,1005,1013]', JSON.stringify(pursuit?.routeSegmentIds));
	ok('routes keep their labels', pursuit?.routes?.[0]?.label === 'old-city sweep belt', pursuit?.routes?.[0]?.label);
	ok('status planning → state planned (raw status kept)', pursuit?.state === 'planned' && pursuit?.status === 'planning', `${pursuit?.state}/${pursuit?.status}`);
	ok('metadata carried (vehicle_id, recommendation_id)', pursuit?.metadata?.vehicleId === VEHICLE && pursuit?.metadata?.recommendationId === 'e91d4591-390a-4c55-bcb0-ffd10c769c20');
	ok('operator-opened pursuit is not badged emulated', pursuit?.emulated === false);

	const row = {
		event_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
		event_kind: 'procession',
		protection_class: 'X',
		crowd_class: 'MEDIUM',
		title: 'Rath Yatra Morning',
		status: 'approved',
		movement_id: '5e44d9f6-941d-5e08-80a9-0251127216ba',
		planned_start: '2026-10-23T01:30:00+00:00',
		planned_end: '2026-10-23T03:30:00+00:00',
		metadata: { source: 'event-emulator', run_id: 21, use_case_id: 'rath_yatra_monsoon_ambulance', is_replay: false },
		routes: [
			{ label: 'primary', segment_ids: [1005, 1015, 1006] },
			{ label: 'alt-1', segment_ids: [1006, 1001] },
		],
		phases: [{ label: 'procession', window_start: '2026-10-23T01:30:00+00:00', window_end: '2026-10-23T03:00:00+00:00', closure_policy: { closure_type: 'signal_hold', advance_closure_sec: 45 } }],
	};
	const ev = client.mapEventRow(row, 'live-events-api');
	ok('crowd MEDIUM → MODERATE', ev.crowdClass === 'MODERATE');
	ok('approved → planned', ev.state === 'planned' && ev.status === 'approved');
	ok('spines flattened in rank order, shared segment once', JSON.stringify(ev.routeSegmentIds) === '[1005,1015,1006,1001]' && ev.routes.length === 2, JSON.stringify(ev.routeSegmentIds));
	ok('phase times ← window_start/window_end', ev.phases[0].startTime === row.phases[0].window_start && ev.phases[0].endTime === row.phases[0].window_end && ev.phases[0].closurePolicy?.closureType === 'signal_hold');
	ok('windowStart/End ← planned_start/planned_end', ev.windowStart === row.planned_start && ev.windowEnd === row.planned_end);
	ok("emulated = metadata.source === 'event-emulator' (+ run/use-case carried)", ev.emulated === true && ev.metadata.runId === '21' && ev.metadata.useCaseId === 'rath_yatra_monsoon_ambulance');
	ok('movementId carried', ev.movementId === row.movement_id);
	ok('an unlabelled row carries no label (no TEST pill)', ev.metadata.label === null);
	// A brain movement created with a title (an acceptance test, a rehearsal)
	// carries it as metadata.label: the LiveRail TEST pill reads it.
	const labelled = client.mapEventRow({ ...row, metadata: { source: 'convoy-brain', label: '  ACCEPTANCE TEST escort ' } }, 'live-events-api');
	ok('metadata.label carried (trimmed) for the TEST pill', labelled.metadata.label === 'ACCEPTANCE TEST escort' && labelled.emulated === false, labelled.metadata.label);
	const legacy = client.mapEventRow({ event_id: 'm1', name: 'convoy', state: 'active', route_segment_ids: [1, 2] }, 'live-movements-api');
	ok('legacy movement rows still map', legacy.name === 'convoy' && legacy.state === 'active' && legacy.routeSegmentIds.join() === '1,2');
}

// ─── 4. signal gate shapes ──────────────────────────────────────────────────

{
	const doc = await client.fetchSignalPlans();
	ok('band plan table: writes boolean → writes/hold label', doc.bandPlanTable.length === 4 && doc.bandPlanTable.every((r) => (r.writes === 'writes') === (r.writesSignals === true) && (r.writes === 'hold') === (r.writesSignals === false)), doc.bandPlanTable.map((r) => `${r.band}:${r.writes}`).join(' '));
	ok('gate.twin_in_flight is an integer count', Number.isInteger(doc.gate.twinInFlight), String(doc.gate.twinInFlight));
	ok('city band read', ['GREEN', 'AMBER', 'RED', 'BLACK'].includes(doc.cityBand), doc.cityBand);
	const t0 = net.log.length;
	const history = await client.fetchSignalHistory(20);
	const req = net.log.slice(t0).map((r) => r.route);
	ok('HISTORY reads GET /api/v1/signals/history', req.length === 1 && req[0].startsWith('/api/v1/signals/history?'), req.join(','));
	ok('history parses to an array', Array.isArray(history), `${history.length} rows`);
}

// ─── 5. situation log + hysteresis candidate ────────────────────────────────

{
	const log = await client.fetchSituationLog(RATH_YATRA);
	ok('situation_log rows parse oldest → newest with server timestamps', log.length > 0 && log.every((p, i) => Number.isFinite(p.ts) && (i === 0 || log[i - 1].ts <= p.ts)), `${log.length} rows`);
	if (MODE === 'replay') {
		const newest = log.at(-1);
		ok('newest committed band + ts from timestamp_utc', newest.band === 'RED' && newest.ts === Date.parse('2026-10-19T09:57:43.787039+00:00'));
		ok('transitioned flags carried', log.some((p) => p.transitioned));
		ok('no observed_class on the wire → no candidate (nothing pending is visible)', opsStore.bandCandidateOf(log) === null);
	}
	const synth = client.parseSituationLog({
		entries: [
			{ situation_class: 'AMBER', observed_class: 'RED', transitioned: false, timestamp_utc: '2026-10-19T09:53:43Z' },
			{ situation_class: 'AMBER', observed_class: 'RED', transitioned: false, timestamp_utc: '2026-10-19T09:53:38Z' },
			{ situation_class: 'AMBER', observed_class: 'AMBER', transitioned: true, timestamp_utc: '2026-10-19T09:53:33Z' },
			{ situation_class: 'GREEN', transitioned: false, timestamp_utc: 'not-a-time' },
		],
	});
	const cand = opsStore.bandCandidateOf(synth);
	ok('rows without a server timestamp are dropped', synth.length === 3);
	ok('observed_class ≠ committed → candidate RED held 2/2 (escalation)', cand?.band === 'RED' && cand.ticks === 2 && cand.required === 2, JSON.stringify(cand));
	let refused = false;
	try {
		await client.fetchSituationLog('');
	} catch {
		refused = true;
	}
	ok('an unscoped situation_log read is refused client-side', refused);
}

// ─── 6. audit poll rung (C-C FeedItem rows) ─────────────────────────────────

{
	const rows = client.parseAuditRows(fixture('event_audit.contract.json'));
	ok('C-C audit rows parse oldest → newest', rows.length === 3 && rows[0].ts <= rows[1].ts && rows[1].ts <= rows[2].ts);
	ok('actor_kind carried (human decision row)', rows.some((r) => r.actorKind === 'human' && r.actor?.startsWith('Duty Officer')));
	const snap = await client.pollAuditTail(null);
	ok('no subject → adaptive status rung (no per-event route)', snap.source === 'adaptive-status');
}

// ─── 7. corridor summary honesty ────────────────────────────────────────────

{
	const live = await (async () => {
		const res = await fetch('/api/v1/corridor/summary');
		return res.json();
	})();
	const row = corridorFeed.normaliseCorridorSummary({
		totalSegments: live.total_segments,
		avgSpeedKmh: live.avg_speed_kmh,
		avgCongestionIdx: live.avg_congestion_idx,
		congestedSegments: live.congested_segments,
		criticalSegments: live.critical_segments,
		status: live.status,
	});
	if (live.total_segments === 0 || live.status === 'no_data') {
		ok('0 segments / no_data → NO LIVE FEED (never GREEN)', row.status === 'no_data', `wire ${live.status}/${live.total_segments}`);
	} else {
		ok('live summary keeps its band', row.status === live.status, `${live.status}/${live.total_segments}`);
	}
	const fed = corridorFeed.normaliseCorridorSummary({ totalSegments: 31, avgSpeedKmh: 24.2, avgCongestionIdx: 0.41, congestedSegments: 3, criticalSegments: 0, status: 'amber' });
	ok('a fed corridor keeps amber', fed.status === 'amber');
	const wp01 = corridorFeed.normaliseCorridorSummary({ totalSegments: 31, avgSpeedKmh: 0, avgCongestionIdx: 0, congestedSegments: 0, criticalSegments: 0, status: 'no_data' });
	ok("WP-01 'no_data' (stale rows) → NO LIVE FEED", wp01.status === 'no_data');
}

if (MODE === 'live') {
	console.log(`\n${failures === 0 ? 'PASS' : 'FAIL'} contract (live ${BASE}) — ${failures} failure(s)`);
	process.exit(failures === 0 ? 0 : 1);
}

// ─── 8. ops store: band truth, selection, pursuit, fixtures, writers ───────

const { ops } = opsStore;

function requested(pred) {
	return net.log.filter(pred).map((r) => `${r.method} ${r.route}`);
}

{
	client.notDeployedBackoff.reset();
	net.log.length = 0;
	ops.boardLoaded = false;
	await ops.pollBand(); // before the first board load: must not read anything
	ok('band poll waits for the first board load', requested((r) => r.route.includes('situation_log')).length === 0);
	await ops.pollBoard();
	await settle();
	ok('auto-selection prefers the live vehicle_pursuit', ops.selectedEventId === PURSUIT, ops.selectedEventId);
	ok('healthy events API → no preview fixtures on the board', ops.events.every((e) => e.source !== 'offline-fixture') && ops.boardStatus === 'live', `${ops.events.length} events, ${ops.boardStatus}`);
	ok('event counts count events', ops.eventCounts.total === ops.events.length && ops.eventCounts.preview === 0);
	const unscoped = requested((r) => r.route.includes('/adaptive/situation_log') && !r.route.includes('event_id='));
	ok('situation_log is never read unscoped', unscoped.length === 0, unscoped.join(','));
	// What the band must be follows the RECORDED pursuit log (a re-record from
	// a stack whose engine has logged the pursuit changes the expectation,
	// never the rule): an empty log → the city band; a non-empty one → its own.
	const pursuitLog = client.parseSituationLog(fixture('situation_log_pursuit.json'));
	if (pursuitLog.length === 0) {
		ok("empty event log → band = city band (bandSourceKind 'city-band')", ops.band === 'GREEN' && ops.bandSourceKind === 'city-band', `${ops.band} via ${ops.bandSourceKind} · '${ops.bandSource}'`);
		// The city band's newest reading is the engine's last evaluation (else
		// the read) — never a completed event's band; how long it has HELD is
		// its entry time (city_band.since), on the held-since line.
		const cityBandSince = fixture('adaptive_status.json')?.city_band?.since ?? null;
		ok(
			'city band: newest reading = the engine’s last evaluation (else the read); held since = city_band.since',
			ops.bandUpdated > 0 && Date.now() - ops.bandUpdated < 60_000 && ops.bandCommittedAt === (cityBandSince ? Date.parse(cityBandSince) : null),
			`reading ${Math.round((Date.now() - ops.bandUpdated) / 1000)} s ago · held since ${cityBandSince ?? '— (not served)'}`,
		);
	} else {
		const newest = pursuitLog.at(-1);
		ok("the pursuit's own log → its committed band (bandSourceKind 'situation_log')", ops.band === newest.band && ops.bandSourceKind === 'situation_log', `${ops.band} via ${ops.bandSourceKind}`);
		ok("band age = the newest entry's timestamp_utc (never the read)", ops.bandUpdated === newest.ts, new Date(ops.bandUpdated).toISOString());
	}
	ok('interception plan loaded on auto-select (no click)', ops.interception?.eventId === PURSUIT && ops.interception.chokes.length > 0 && ops.interception.latestSighting?.plate === 'GJ27EM•••1');
	ops.setDutyOfficer('Duty Officer A');
	ok('a named officer sees the raw plate; clearing masks it again (C6)', ops.interception?.latestSighting?.plate === 'GJ27EM9001' && (ops.setDutyOfficer(''), ops.interception?.latestSighting?.plate === 'GJ27EM•••1'));
	ok('audit for a live event goes via the 2 s poll rung (no WS ladder in production)', ops.audit.some((a) => a.text.includes('via the 2 s poll rung')));
	ok('no request to the absolute emulator URL', requested((r) => r.url.startsWith('http://localhost:8090')).length === 0);

	// Same plan on the next poll → the object is kept (no overlay re-animation).
	const before = ops.interception;
	await ops.pollInterception();
	ok('unchanged plan keeps its object (canvas overlay does not re-animate)', ops.interception === before && ops.interceptionUpdated > 0);

	// Stale answers are dropped: a slow plan for the pursuit lands after the operator moved on.
	const slow = (async () => {
		const p = ops.loadInterception(PURSUIT);
		return p;
	})();
	ops.selectEvent('missing-event-id');
	await slow;
	await settle();
	ok('stale interception answer dropped after the selection moved', ops.interception === null, String(ops.interception?.eventId));
	ops.selectEvent(PURSUIT);
	await settle();

	// The audit rung with WP-02's per-event route answering.
	net.overrides.set(`/api/v1/events/${PURSUIT}/audit`, { status: 200, body: fixture('event_audit.contract.json') });
	client.notDeployedBackoff.reset();
	await ops.pollAudit();
	await ops.pollAudit();
	const serverRows = ops.audit.filter((a) => a.serverId);
	ok('server audit rows appended once each (de-duplicated by id)', serverRows.length === 3, `${serverRows.length} rows`);
	ok('human/machine attribution kept on server rows', serverRows.some((a) => a.actorKind === 'human') && serverRows.some((a) => a.actorKind === 'machine'));
	net.overrides.clear();

	// Overrides evidence (C-C) without ever calling the side-effectful /adaptive/params.
	net.overrides.set('/api/v1/adaptive/overrides', { status: 200, body: fixture('overrides.contract.json') });
	client.notDeployedBackoff.reset();
	await ops.pollBand();
	await settle();
	ok('override rows → band evidence (shadow_eval)', ops.overrides.length === 2 && ops.bandEvidence?.shadowEval === 'unavailable' && ops.bandEvidence.failClosedReason === null);
	ok('never polls GET /adaptive/params (it writes a param_overrides row per call)', requested((r) => r.route.startsWith('/api/v1/adaptive/params')).length === 0);
	net.overrides.clear();
}

{
	// A selected live event WITH its own log: band from situation_log, age from the row.
	const board = fixture('events_active.json');
	const procession = {
		...board.events[0],
		event_id: RATH_YATRA,
		event_kind: 'procession',
		title: 'Rath Yatra Morning, Monsoon Burst, One Siren',
		status: 'active',
		metadata: { source: 'event-emulator' },
		routes: [{ label: 'spine', segment_ids: [1005, 1015, 1006, 1001] }],
	};
	net.overrides.set('/api/v1/events/active', { status: 200, body: { events: [...board.events, procession] } });
	await ops.pollBoard();
	ok('pursuit still preferred over an active procession', ops.selectedEventId === PURSUIT);
	ops.selectEvent(RATH_YATRA);
	await settle();
	ok("non-empty event log → its band (bandSourceKind 'situation_log')", ops.band === 'RED' && ops.bandSourceKind === 'situation_log');
	ok('the printed band source is scoped to the selected event', /selected event/.test(ops.bandSource), ops.bandSource);
	ok('bandUpdated = newest entry timestamp_utc (not Date.now())', ops.bandUpdated === Date.parse('2026-10-19T09:57:43.787039+00:00'), new Date(ops.bandUpdated).toISOString());
	ok('commit time from the newest transitioned row', ops.bandCommittedAt === Date.parse('2026-10-19T09:53:48.607812+00:00'));
	ok('non-pursuit selection never requests a plan', ops.interception === null && requested((r) => r.route.includes(`${RATH_YATRA}/interception-plan`)).length === 0);
	net.overrides.clear();
	await ops.pollBoard();
	await settle();
	ops.selectEvent(PURSUIT);
	await settle();
}

{
	// PREVIEW toggle + fixtures selected: no plan, no per-event audit, restore refused.
	ops.setPreviewFixtures(true);
	await settle();
	ok('PREVIEW toggle shows labelled fixtures beside live rows', ops.eventCounts.preview === 8 && ops.eventCounts.live >= 1 && ops.boardStatus === 'mixed');
	net.log.length = 0;
	ops.selectEvent('rath_yatra_ahmedabad');
	await settle();
	ok('fixture selected → no interception-plan request (422 today)', requested((r) => r.route.includes('interception-plan')).length === 0);
	ok('fixture selected → no per-event audit request', requested((r) => r.route.includes('rath_yatra_ahmedabad/audit')).length === 0);
	ok('fixture selected → city band', ops.bandSourceKind === 'city-band');
	ops.setDutyOfficer('Duty Officer A');
	net.posts.length = 0;
	await ops.restorePrograms(false);
	ok('RESTORE with a fixture selected is refused before any request', net.posts.length === 0 && ops.audit.at(-1)?.text.includes('RESTORE refused'));
	await ops.restorePrograms(true);
	ok('RESTORE ALL sends {all, officer}', net.posts.at(-1)?.route === '/api/v1/signals/restore' && net.posts.at(-1).body.all === true && net.posts.at(-1).body.officer === 'Duty Officer A' && !('event_id' in net.posts.at(-1).body), JSON.stringify(net.posts.at(-1)?.body));
	net.overrides.set('POST /api/v1/signals/plans/apply', { status: 409, body: { detail: { reason: 'twin_not_calibrated' } } });
	await ops.applyCityPlan();
	const apply = net.posts.at(-1);
	ok('APPLY carries the officer and no fixture slug', apply?.route === '/api/v1/signals/plans/apply' && apply.body.officer === 'Duty Officer A' && !('event_id' in apply.body), JSON.stringify(apply?.body));
	ok('409 refusal is a held-at-baseline outcome', ops.signalApply?.applied === false && ops.signalApply.refusalReason === 'twin_not_calibrated');
	ops.selectEvent(PURSUIT);
	await settle();
	await ops.restorePrograms(false);
	const restore = net.posts.at(-1);
	ok('RESTORE for a live uuid event sends {event_id, officer}', restore?.body.event_id === PURSUIT && restore.body.officer === 'Duty Officer A', JSON.stringify(restore?.body));
	await ops.clearWatch(VEHICLE);
	const clear = net.posts.at(-1);
	ok('earmark CLEAR sends {officer}', clear?.route === `/api/v1/vehicles/${VEHICLE}/earmark/clear` && clear.body.officer === 'Duty Officer A', JSON.stringify(clear?.body));
	ops.setDutyOfficer(`Duty   Officer ${'B'.repeat(90)}`);
	await ops.restorePrograms(true);
	const long = net.posts.at(-1)?.body.officer ?? '';
	ok('officer normalised to the 64-char contract (C-F/signals/dispatch)', long.length === 64 && long.startsWith('Duty Officer B'), `${long.length} chars`);
	ops.setDutyOfficer('Duty Officer A');
	ops.setPreviewFixtures(false);
	await settle();
	ok('PREVIEW off → fixtures leave a healthy board', ops.eventCounts.preview === 0);
	net.overrides.clear();
}

{
	// AMA band honesty (C-Q): the adaptive city band — entered-since, simulated
	// feed, stale. A stale band is UNKNOWN with since when, never GREEN.
	const since = '2026-10-22T20:17:14.878Z';
	const status = (cityBand, engineLoop = { last_tick_age_sec: 2.5 }) => ({
		status: 200,
		body: { ...fixture('adaptive_status.json'), city_band: cityBand, ...(engineLoop ? { engine_loop: engineLoop } : {}) },
	});
	ops.setPreviewFixtures(true);
	await settle();
	ops.selectEvent('rath_yatra_ahmedabad'); // a fixture: the subject is the city
	await settle();
	net.overrides.set('/api/v1/adaptive/status', status({ band: 'AMBER', since, subjects: 1, simulated_feed: true, stale: false, stale_since: null }));
	await ops.pollCityBand();
	await ops.pollBand();
	await settle();
	ok('city band from /adaptive/status city_band', ops.band === 'AMBER' && ops.bandSourceKind === 'city-band', `${ops.band} via ${ops.bandSourceKind}`);
	// A band held for hours is not a stale reading: the newest reading is the
	// engine's last tick (the canvas AMA chip calls > 10 min STALE); `since`
	// is how long it has held, and a re-read never resets it.
	const tickAge = (Date.now() - ops.bandUpdated) / 1000;
	ok('newest reading = the engine’s last evaluation (now − last_tick_age_sec)', tickAge >= 2 && tickAge < 6, `${tickAge.toFixed(1)} s ago`);
	ok('held since = Date.parse(city_band.since)', ops.bandCommittedAt === Date.parse(since), ops.bandCommittedAt ? new Date(ops.bandCommittedAt).toISOString() : 'null');
	net.overrides.set('/api/v1/adaptive/status', status({ band: 'AMBER', since, subjects: 1, simulated_feed: true, stale: false, stale_since: null }, null));
	await ops.pollCityBand();
	await ops.pollBand();
	await settle();
	ok('…an oracle without the engine loop block: the reading is the read time; held since unchanged', Date.now() - ops.bandUpdated < 3000 && ops.bandCommittedAt === Date.parse(since));
	ok('simulated_feed → the band source says SIMULATED FEED', ops.bandSimulated === true && /SIMULATED FEED/.test(ops.bandSource), ops.bandSource);
	net.overrides.set('/api/v1/adaptive/status', status({ band: 'GREEN', since, subjects: 1, simulated_feed: true, stale: true, stale_since: '2026-10-22T20:30:00.000Z' }));
	await ops.pollCityBand();
	await ops.pollBand();
	await settle();
	ok('a STALE city band is UNKNOWN (band null), never GREEN', ops.band === null && ops.bandStaleSince === Date.parse('2026-10-22T20:30:00.000Z'), String(ops.band));
	ok("…its source reads 'UNKNOWN — engine poll failing since <IST>'", /^UNKNOWN — engine poll failing since \d{2}:\d{2} IST$/.test(ops.bandSource), ops.bandSource);
	// A stale doc without stale_since is dated by the first read that saw it
	// stale (it began no later) — never by the moment the band was entered.
	net.overrides.set('/api/v1/adaptive/status', status({ band: 'GREEN', since, subjects: 1, simulated_feed: true, stale: false, stale_since: null }));
	await ops.pollCityBand();
	const firstSeen = Date.now();
	net.overrides.set('/api/v1/adaptive/status', status({ band: 'GREEN', since, subjects: 1, simulated_feed: true, stale: true }));
	await ops.pollCityBand();
	await ops.pollBand();
	await settle();
	const seenStale = ops.bandStaleSince;
	ok('a stale band without stale_since: failing since the first read that saw it, not since the band was entered', ops.band === null && seenStale != null && seenStale >= firstSeen && seenStale !== Date.parse(since), seenStale ? new Date(seenStale).toISOString() : 'null');
	await ops.pollCityBand();
	await ops.pollBand();
	await settle();
	ok('…and a re-read keeps that first sighting', ops.bandStaleSince === seenStale);
	net.overrides.clear();
	ops.setPreviewFixtures(false);
	await settle();
	ops.selectEvent(PURSUIT);
	await settle();
	// adaptive status: the band is effective_class (situation_code is a composite code)
	net.overrides.set('/api/v1/adaptive/status', status(null));
	net.overrides.set('/api/v1/adaptive/status', {
		status: 200,
		body: { movements: { [PURSUIT]: { effective_class: 'AMBER', situation_code: 'Z:FREE_FLOW:CLEAR:LOW', band_since: since, simulated_feed: true } }, degradation: {}, last_shadow_eval: {} },
	});
	const snap = await client.fetchAdaptiveStatus();
	ok('fetchAdaptiveStatus picks effective_class + band_since (not the composite situation_code)', snap.movements[0]?.situationCode === 'AMBER' && snap.movements[0]?.bandSince === Date.parse(since) && snap.movements[0]?.simulatedFeed === true);
	net.overrides.clear();
}

{
	// Events API not deployed → movements fallback + labelled fixtures (and the 404 is remembered).
	client.notDeployedBackoff.reset();
	net.overrides.set('/api/v1/events/active', { status: 404, body: { detail: 'Not Found' } });
	net.log.length = 0;
	await ops.pollBoard();
	await ops.pollBoard();
	await settle();
	ok('events API 404 → fixtures stand in, board offline-labelled', ops.eventCounts.preview === 8 && ops.boardStatus === 'offline' && /not deployed/.test(ops.boardNote ?? ''));
	ok('…and the 404 route is asked once per back-off window', requested((r) => r.route === '/api/v1/events/active').length === 1);
	ok('selection falls to a fixture (no plan request)', ops.selectedEvent?.source === 'offline-fixture' && requested((r) => r.route.includes('interception-plan')).length === 0);
	net.overrides.clear();
	client.notDeployedBackoff.reset();
}

{
	// Emulator transport: same-origin only in the production build.
	net.log.length = 0;
	await ops.pollEmulator();
	const emu = requested((r) => r.route.includes('emulator') || r.url.includes('8090'));
	ok('emulator reached through /api/emulator only', emu.length > 0 && emu.every((r) => r.includes(' /api/emulator/')), emu.join(','));
}

{
	// Poll gate: in-flight guard + hidden pause.
	const gate = client.createPollGate();
	let runs = 0;
	let release;
	const blocker = new Promise((r) => (release = r));
	const first = gate('x', async () => {
		runs++;
		await blocker;
	});
	const second = await gate('x', async () => {
		runs++;
	});
	release();
	await first;
	ok('a poller never overlaps itself', runs === 1 && second === false);
	globalThis.document = { hidden: true };
	const hidden = await gate('y', async () => {
		runs++;
	});
	delete globalThis.document;
	ok('nothing runs while the page is hidden', hidden === false && runs === 1);
}

// ─── 9. escort authority: a followed escort's own status document (C-J) ────

{
	// "Officer approves every switch": the brain's escort status carries the
	// ONE open proposal (pending_approval) and the closed ones
	// (approval_outcomes). A console following one escort shows them even
	// when GET /api/convoy/escorts is not deployed, failing or not read yet.
	const MID = 'mv-escort-1';
	const EVT = '7a1c0c39-5d0e-4c43-9a3f-1f6f2a1c9b10';
	const t0 = Date.now();
	const iso = (ms) => new Date(ms).toISOString();
	const pending = {
		approval_id: 'ap-2',
		action: 'activate_plan_b',
		kind: 'plan_b',
		status: 'pending',
		summary: 'Plan B via Kankaria Road — 4.3 km left (now 3.4 km, 1.3x)',
		reason: 'decider confidence 0.62 — below the floor',
		trigger: 'incident_detected',
		tier: 'decider',
		confidence: 0.62,
		created_at: iso(t0 - 20_000),
		raised_at: iso(t0 - 20_000),
		expires_at: iso(t0 + 70_000),
		timeout_sec: 90,
		segment_ids: [1013],
		proposed: { route_source: 'plan_b', segment_ids: [1005, 1038], closures_new: [1038], closures_lifted: [1013] },
		data_quality: 'synthetic',
	};
	const doc = {
		movement_id: MID,
		event_id: EVT,
		status: 'running',
		iteration: 12,
		escort_authority: 'officer_approves_every_switch',
		approval_timeout_sec: 90,
		pending_approval: pending,
		pending_approvals: [pending],
		approval_outcomes: [
			{ approval_id: 'ap-1', action: 'hold_position', kind: 'hold', status: 'expired', decided_by: 'system', decided_at: iso(t0 - 60_000), expiry_reason: 'no officer response — current plan held' },
		],
	};
	const st = cityApi.parseEscortStatus(MID, doc);
	ok(
		'escort doc: pending_approval → the ONE open proposal (C-J)',
		st.proposal?.approvalId === 'ap-2' && st.proposal.kind === 'plan_b' && st.proposal.movementId === MID && st.proposal.eventId === EVT && st.proposal.expiresAt === Date.parse(pending.expires_at) && st.proposal.simulated === true && st.proposal.proposed?.closuresNew.join() === '1038',
		`${st.proposal?.approvalId} ${st.proposal?.kind}`,
	);
	ok('escort doc: approval_outcomes → closed proposals, newest last', st.outcomes.length === 1 && st.outcomes[0].status === 'expired' && /current plan held/.test(st.outcomes[0].expiryReason ?? ''));
	ok('escort doc: authority reported, timeout carried', st.proposalsReported === true && st.authority === 'officer_approves_every_switch' && st.approvalTimeoutSec === 90);
	const ended = cityApi.parseEscortStatus(MID, { ...doc, status: 'stopped' });
	ok('an ended escort answers nothing: no open proposal, outcomes kept', ended.proposal === null && ended.outcomes.length === 1);
	const legacy = cityApi.parseEscortStatus(MID, {
		movement_id: MID,
		status: 'running',
		pending_approvals: [{ approval_id: 'old-1', action: 'hold_position', status: 'pending', raised_at: iso(t0) }],
	});
	ok('a brain that predates escort authority: its legacy list only', legacy.proposalsReported === false && legacy.proposal === null && legacy.outcomes.length === 0 && legacy.pendingApprovals.length === 1);

	// The store answers from the /escorts lanes when they answer, else from
	// the followed escort's own document.
	const saved = { escorts: ops.escorts, deployed: ops.escortsDeployed, note: ops.escortsNote };
	const lane = {
		movementId: MID,
		eventId: EVT,
		status: 'running',
		alive: true,
		officer: 'Duty Officer A',
		gpsMode: 'emulated',
		trafficSimulated: true,
		proposal: client.parseEscortProposal({ ...pending, approval_id: 'ap-lane' }, MID, EVT),
		outcomes: [],
		timeoutSec: 90,
	};
	ops.escorts = [lane];
	ops.escortsDeployed = true;
	ops.escortsNote = null;
	ok('fresh /escorts lanes win over the followed document', ops.escortProposalFor(MID, st)?.approvalId === 'ap-lane' && ops.escortOutcomeFor(MID, st) === null);
	ops.escorts = [];
	ops.escortsDeployed = false;
	ok('no /escorts (404): the followed escort shows its own proposal and outcome', ops.escortProposalFor(MID, st)?.approvalId === 'ap-2' && ops.escortOutcomeFor(MID, st)?.approvalId === 'ap-1');
	ok('…for the movement it follows only', ops.escortProposalFor('mv-other', st) === null && ops.escortOutcomeFor('mv-other', st) === null);
	ops.escortsDeployed = null;
	ok('/escorts not answered yet: the followed document', ops.escortProposalFor(MID, st)?.approvalId === 'ap-2');
	ops.escorts = [lane];
	ops.escortsDeployed = true;
	ops.escortsNote = 'brain HTTP 502';
	ok('stale lanes (their last read failed): the fresher followed document wins', ops.escortProposalFor(MID, st)?.approvalId === 'ap-2');
	ok('…and a followed document with no open proposal clears it', ops.escortProposalFor(MID, cityApi.parseEscortStatus(MID, { ...doc, pending_approval: null })) === null);
	ok('…while a legacy document never stands in for the lanes', ops.escortProposalFor(MID, legacy)?.approvalId === 'ap-lane');
	ok('no followed document: the lanes as last read', ops.escortProposalFor(MID)?.approvalId === 'ap-lane');
	ops.escorts = saved.escorts;
	ops.escortsDeployed = saved.deployed;
	ops.escortsNote = saved.note;
}

// ─── 10. use-case spines mirror the emulator library (real network) ────────

{
	// The board's preview fixtures, the citizen offline fixtures and the twin
	// compare fallback draw these spines: each must be the emulator's own
	// (and the twin's scenarios.py) chain — legal on the OpenStreetMap network.
	const libPath = path.resolve(DECK, '..', 'event-emulator', 'src', 'event_emulator', 'use_case_library.json');
	const lib = JSON.parse(readFileSync(libPath, 'utf8'));
	const byId = new Map(lib.map((u) => [u.id, u.scenarioConfig]));
	const drift = usecases.USE_CASES.filter((u) => JSON.stringify(u.routeSegmentIds) !== JSON.stringify(byId.get(u.id)?.route_segment_ids));
	ok(
		"every deck use-case spine = the emulator library's",
		usecases.USE_CASES.length >= 8 && drift.length === 0,
		drift.map((u) => `${u.id} ${JSON.stringify(u.routeSegmentIds)} ≠ ${JSON.stringify(byId.get(u.id)?.route_segment_ids)}`).join(' | ') || `${usecases.USE_CASES.length} spines`,
	);
	const stale = usecases.USE_CASES.filter((u) => /Tilak Road|CTM Road|Jamalpur approach/.test(u.description));
	ok('no schematic-era road names in the use-case copy', stale.length === 0, stale.map((u) => u.id).join(','));
}

// ─── 11. emulator runs: the scenario premise (C-N) ─────────────────────────

{
	const row = client.parseEmulatorRun({
		run_id: 41,
		status: 'running',
		use_case_id: 'watchlist_anpr_sweep',
		spec: {
			use_case_id: 'watchlist_anpr_sweep',
			scenario_precondition: [
				{ plate: 'GJ27EM9001', lawful_basis: 'stolen_vehicle_fir', fir_ref: 'FIR 4401/2026, Kalupur PS', result: 'delivered', earmark: 'created', earmark_id: 'e1' },
			],
		},
	});
	ok(
		'run spec.scenario_precondition → its premise (earmark created)',
		row?.premises?.length === 1 && row.premises[0].result === 'delivered' && row.premises[0].earmark === 'created' && row.premises[0].firRef === 'FIR 4401/2026, Kalupur PS' && row.premises[0].plate === 'GJ27EM9001',
	);
	const failed = client.parseRunPremises([{ plate: 'GJ27EM9001', result: 'failed', reason: 'HTTPStatusError: 503' }, { result: '' }, 'junk', null]);
	ok('a failed premise keeps its reason; junk entries are dropped', failed.length === 1 && failed[0].result === 'failed' && failed[0].reason === 'HTTPStatusError: 503');
	ok('a run without a premise parses to []', client.parseEmulatorRun({ run_id: 7, status: 'completed', spec: {} })?.premises?.length === 0 && client.parseRunPremises(undefined).length === 0);
	const keys = ['emuPremiseTitle', 'emuPremise_created', 'emuPremise_already_active', 'emuPremise_failed', 'emuPremise_skipped'];
	const missing = ['en', 'hi', 'gu'].flatMap((lang) =>
		keys.filter((k) => {
			const text = opsStrings.tOps(lang, k, 'GJ27EM•••1', 'FIR 4401/2026, Kalupur PS');
			return text === k || (k !== 'emuPremiseTitle' && !text.includes('GJ27EM•••1'));
		}).map((k) => `${lang}:${k}`),
	);
	ok('the premise line ships in EN / HI / GU (plate shown as given — masked upstream)', missing.length === 0, missing.join(','));
}

// ─── 12. a stolen vehicle is pursued on its sighting, never on a fine (C-N) ─

{
	const saved = { watchlist: ops.watchlist, approved: ops.approvedRecommendations };
	const vid = '00000000-0000-4000-8000-0000000000c1';
	const watch = (lawfulBasis) => ({
		vehicleId: vid, plate: 'GJ27EM9001', earmarkState: 'flagged', earmarkReason: 'test', lawfulBasis, retentionUntil: null,
		lastSighting: { t: new Date().toISOString(), lon: 72.5813, lat: 23.0264, speedKmh: 30, heading: 90, source: 'emulated_anpr' },
	});
	const rec = (id, ruleId, decidedAt) => ({
		recommendationId: id, sightingId: null, vehicleId: vid, plate: 'GJ27EM9001', ruleId, recommendation: null, evidence: null,
		status: 'approved', decidedBy: 'Duty Officer A', decidedAt, createdAt: decidedAt,
	});
	const candidateRec = () => ops.pursuitCandidates.find((c) => c.vehicle.vehicleId === vid)?.recommendationId ?? null;
	const challan = rec('00000000-0000-4000-8000-00000000c0a1', 'R_SPD_SEGMENT', '2026-10-23T10:00:00Z');
	const sighting = rec('00000000-0000-4000-8000-00000000c0a2', 'R_STOLEN_SIGHTING', '2026-10-23T09:00:00Z');
	ops.watchlist = [watch('stolen_vehicle_fir')];
	ops.approvedRecommendations = [challan];
	ok('a stolen vehicle with only an approved e-challan has no pursuit basis', candidateRec() === null, candidateRec());
	ops.approvedRecommendations = [challan, sighting];
	ok('…its approved R_STOLEN_SIGHTING is the basis, even when the e-challan is newer', candidateRec() === sighting.recommendationId, candidateRec());
	ops.watchlist = [watch('court_warrant')];
	ops.approvedRecommendations = [challan];
	ok('another lawful basis keeps the newest human-approved recommendation (C6)', candidateRec() === challan.recommendationId, candidateRec());
	ops.watchlist = saved.watchlist;
	ops.approvedRecommendations = saved.approved;
}

// ─── 14. a single failed read is a BLIP, not an outage ──────────────────────

{
	// The board: a 503 right after a good read keeps the last-good LIVE rows.
	net.overrides.clear();
	client.notDeployedBackoff.reset();
	await ops.pollBoard();
	await settle();
	const liveIds = ops.events.map((e) => e.eventId).join(',');
	const updated = ops.boardUpdated;
	ok('blip setup: a healthy live board', ops.boardStatus === 'live' && ops.eventCounts.preview === 0 && ops.boardLiveAt > 0, `${ops.events.length} events`);
	net.overrides.set('/api/v1/events/active', { status: 503, body: { detail: 'oracle restarting' } });
	net.log.length = 0;
	await ops.pollBoard();
	await settle();
	ok(
		'a blip (503 right after a good read) keeps the last-good LIVE board — no PREVIEW fixtures, no movements fallback',
		ops.eventCounts.preview === 0 && ops.events.map((e) => e.eventId).join(',') === liveIds && ops.boardStatus === 'live' &&
			requested((r) => r.route.includes('/movements/active')).length === 0,
		`${ops.events.length} events · ${ops.boardStatus} · ${ops.boardNote}`,
	);
	ok('…said in the note, its age left to grow (stale)', /blip/.test(ops.boardNote ?? '') && ops.boardUpdated === updated, ops.boardNote);
	ops.boardLiveAt = Date.now() - client.BLIP_GRACE_MS - 1000; // the outage outlived the grace
	await ops.pollBoard();
	await settle();
	ok('past the blip grace (an outage) → movements fallback + labelled PREVIEW fixtures, as before', ops.eventCounts.preview === 8 && /unreachable/.test(ops.boardNote ?? ''), ops.boardNote);
	net.overrides.set('/api/v1/events/active', { status: 404, body: { detail: 'Not Found' } });
	client.notDeployedBackoff.reset();
	ops.boardLiveAt = Date.now();
	await ops.pollBoard();
	await settle();
	ok('a 404 (not deployed) is never a blip — fixtures stand in at once', ops.eventCounts.preview === 8 && /not deployed/.test(ops.boardNote ?? ''), ops.boardNote);
	net.overrides.clear();
	client.notDeployedBackoff.reset();
	await ops.pollBoard();
	await settle();
	ok('recovery → the live board again, no fixtures', ops.boardStatus === 'live' && ops.eventCounts.preview === 0);

	// The fused registry (City Command's LIVE / NO DATA pill and every layer).
	const { cityRegistry } = registryMod;
	const snapshot = { as_of: new Date().toISOString(), took_ms: 3, entities: [], feeds: { event: { count: 0, status: 'ok' } } };
	net.overrides.set('/api/v1/city/entities', { status: 200, body: snapshot });
	await cityRegistry.refresh();
	ok('registry: a good snapshot → ok', cityRegistry.status === 'ok' && cityRegistry.note === null, cityRegistry.status);
	net.overrides.set('/api/v1/city/entities', { status: 503, body: { detail: 'oracle restarting' } });
	await cityRegistry.refresh();
	ok('registry: a blip keeps ok on the last-good snapshot (no NO DATA), the note says so', cityRegistry.status === 'ok' && /blip/.test(cityRegistry.note ?? '') && cityRegistry.served('event'), cityRegistry.note);
	cityRegistry.updatedAt = Date.now() - client.BLIP_GRACE_MS - 1000;
	await cityRegistry.refresh();
	ok('registry: past the grace → down (NO DATA)', cityRegistry.status === 'down' && /unreachable/.test(cityRegistry.note ?? ''), cityRegistry.note);
	net.overrides.set('/api/v1/city/entities', { status: 200, body: snapshot });
	await cityRegistry.refresh();
	ok('registry: recovery → ok, note cleared', cityRegistry.status === 'ok' && cityRegistry.note === null);
	net.overrides.set('/api/v1/city/entities', { status: 404, body: { detail: 'Not Found' } });
	await cityRegistry.refresh();
	ok('registry: a 404 is never a blip', cityRegistry.status === 'not-deployed');

	// The fusion rail's feed (a GET path that 404'd earlier is backed off: forget it).
	client.notDeployedBackoff.reset();
	const { fusionFeed } = fusionFeedMod;
	net.overrides.set('/api/v1/city/feed', { status: 200, body: { items: [] } });
	await fusionFeed.poll();
	ok('feed: a good read → ok', fusionFeed.status === 'ok', fusionFeed.status);
	net.overrides.set('/api/v1/city/feed', { status: 503, body: { detail: 'oracle restarting' } });
	await fusionFeed.poll();
	ok("feed: a blip keeps the rail 'ok' (never 'down' for one dropped poll)", fusionFeed.status === 'ok' && /blip/.test(fusionFeed.note ?? ''), fusionFeed.note);
	fusionFeed.updatedAt = Date.now() - client.BLIP_GRACE_MS - 1000;
	await fusionFeed.poll();
	ok("feed: past the grace → 'down'", fusionFeed.status === 'down', fusionFeed.note);
	net.overrides.clear();
	client.notDeployedBackoff.reset();
	ok('withinBlipGrace: never before a first good answer; within / past the grace',
		!client.withinBlipGrace(0) && client.withinBlipGrace(Date.now() - 1000) && !client.withinBlipGrace(Date.now() - client.BLIP_GRACE_MS - 1));
}

// ─── 13. one officer approval authorises ONE pursuit (C6) ──────────────────

{
	const spent = client.approvalSpent;
	ok('an approval whose pursuit closed is spent; an open or absent pursuit is not',
		spent({ pursuit: { eventId: 'e1', status: 'cancelled' } }) && spent({ pursuit: { eventId: 'e1', status: 'completed' } }) &&
		!spent({ pursuit: { eventId: 'e1', status: 'active' } }) && !spent({ pursuit: null }) && !spent({}));
	const parsed = client.mapRecommendationForTest
		? client.mapRecommendationForTest({ recommendation_id: 'r1', status: 'approved', pursuit: { event_id: 'e9', status: 'cancelled' } })
		: null;
	if (parsed) ok("the oracle's pursuit ref is parsed", parsed.pursuit?.eventId === 'e9' && parsed.pursuit?.status === 'cancelled');
	const saved = { watchlist: ops.watchlist, approved: ops.approvedRecommendations };
	const vid = '00000000-0000-4000-8000-0000000000d1';
	const watch = {
		vehicleId: vid, plate: 'GJ27EM9001', earmarkState: 'flagged', earmarkReason: 'test', lawfulBasis: 'stolen_vehicle_fir', retentionUntil: null,
		lastSighting: { t: new Date().toISOString(), lon: 72.5813, lat: 23.0264, speedKmh: 30, heading: 90, source: 'emulated_anpr' },
	};
	const sight = (id, decidedAt, pursuit) => ({
		recommendationId: id, sightingId: null, vehicleId: vid, plate: 'GJ27EM9001', ruleId: 'R_STOLEN_SIGHTING', recommendation: null,
		evidence: null, status: 'approved', decidedBy: 'Scripted demo officer (showcase script)', decidedAt, createdAt: decidedAt, pursuit,
	});
	const basis = () => ops.pursuitCandidates.find((c) => c.vehicle.vehicleId === vid)?.recommendationId ?? null;
	ops.watchlist = [watch];
	ops.approvedRecommendations = [sight('00000000-0000-4000-8000-00000000d0a1', '2026-10-23T10:00:00Z', { eventId: 'p1', status: 'cancelled' })];
	ok("a scripted approval spent on a closed pursuit is no basis for the next one", basis() === null, basis());
	ops.approvedRecommendations.push(sight('00000000-0000-4000-8000-00000000d0a2', '2026-10-23T09:00:00Z', null));
	ops.approvedRecommendations = [...ops.approvedRecommendations];
	ok('…a fresh human approval is (even when older than the spent one)', basis() === '00000000-0000-4000-8000-00000000d0a2', basis());
	ops.watchlist = saved.watchlist;
	ops.approvedRecommendations = saved.approved;
}

console.log(`\n${failures === 0 ? 'PASS' : 'FAIL'} contract (replay, fixtures recorded ${manifest.recorded_at} from ${manifest.recorded_from}) — ${failures} failure(s)`);
process.exit(failures === 0 ? 0 : 1);
