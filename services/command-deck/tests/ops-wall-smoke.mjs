/**
 * Ops-wall smoke — the /ops situation wall's human-in-the-loop gate, under
 * SOFTWARE WebGL (SwiftShader, the canvas-ux-smoke flags), at 1920×1080.
 *
 * Hermetic: EVERY /api/* request the page makes is answered by the in-test
 * fixture world below — no request reaches an oracle, brain or emulator, so
 * the smoke runs against any deck origin (a PREVIEW, or the live :5199) and
 * can never write to a city. City-writer POSTs are answered by the stub and
 * their bodies inspected (contract R8); a non-GET the world does not expect
 * is refused and fails the run. The browser runs in America/New_York, so an
 * IST assertion cannot pass by accident of the host's zone.
 *
 *   masking      no officer → no raw registration mark anywhere on the wall
 *                (queue, board, dossier, plan, audit — a raw plate a model
 *                wrote into a server row too; the audit probe's streamed
 *                answer, whose tools return raw event titles; a plate typed
 *                into a dispatch note); named officer → raw; cleared →
 *                masked again (C6)
 *   audit        within 5 s the stream is a view over the selected pursuit's
 *                GET /events/{id}/audit: ≥ 3 server rows, a human 'approved
 *                by …' row on the act rail, machine and system rows never
 *                human, EMULATED pills; CITY scope reads GET /city/feed; an
 *                officer's echo about ANOTHER event stays out of the selected
 *                event's view (it shows in the city view)
 *   lifecycle    C-F verbs from the raw status; a double click on ACTIVATE
 *                sends ONE POST {to_status, officer}, ACTIVE EVENTS +1, the
 *                inline 'activated by <officer>' and the server feed row; a
 *                409 renders inline with the moves the server allows
 *   decisions    a double click on an e-challan APPROVE sends ONE POST; a
 *                penal card on a vehicle reported stolen (FIR) offers NO
 *                approve (warning chip, reject only); an oracle 409
 *                'e-challan withheld — vehicle reported stolen' is printed
 *                verbatim and never retried; R_STOLEN_SIGHTING rows fold
 *                into ONE card per vehicle whose AUTHORISE PURSUIT approves
 *                the newest sighting (one POST)
 *   dispatch     agency registry + waypoint (choke #1 is skipped when likely
 *                passed) → POST body carries waypoint_junction; /driver 108
 *                requests lead the queue; a dispatch of a closed event is
 *                labelled and not counted; terminal dispatches older than
 *                30 min fold behind 'history (n)'
 *   pursuit      an open pursuit hides its vehicle's card; no road anchor /
 *                no approved recommendation refuse inline; an anchored card
 *                opens on the sighting's segment from its approved rec (C6)
 *   band / AMA   a null band is UNKNOWN (never GREEN); floors Z+ 180 / Z 120
 *                / Y 60; the hysteresis candidate and the held-since time
 *   posture      decider loaded / p95, Gemma, degradation tier, SIMULATED
 *                FEED, twin in-flight count, retention reaper health
 *   gate         RESTORE ALL is city-wide: the first click only arms it, the
 *                second sends ONE POST {all, officer}
 *   emulator     the emulator's own 10-tape library; flagships capped at
 *                ×10 with the sampling note; C-G 202 + progress (polled with
 *                include_tape=false), STOP, REPLAY; a synchronous build's
 *                held POST is tracked from the ledger instead of awaited;
 *                a run whose corridor event is still 'planning' says
 *                'corridor event not live yet — activation retried every
 *                5 s'; LIVING CITY start
 *   deep links   /ops#evt= selects the event, #rec= / #dsp= scroll to and
 *                highlight the card (on a fresh load: once the polls
 *                rendered it); a hostile id is ignored
 *   escort lane  officer approves every switch (GET /escorts): the escort
 *                AI's open proposal leads the /ops queue with its route, km,
 *                closures, decider tier + confidence and countdown; a double
 *                click on APPROVE sends ONE POST {decision, officer} to
 *                /escort/approvals/{id} and the card leaves a receipt
 *                ('approved by <officer>'); an answer after the deadline
 *                gets the brain's 409 (expired — current plan held), once
 *   escort (/)   the cockpit adopts a running escort; the HUD's hold line,
 *                SIMULATED FEED / EMULATED GPS provenance; 'AI PROPOSES …
 *                expires in mm:ss' locked until an officer is named, the
 *                same proposal on the planner rail; APPROVE and REJECT each
 *                send ONE POST and the HUD names the officer; an unanswered
 *                proposal turns urgent, then reads 'expired — no officer
 *                response, current plan held' with nothing sent; once the
 *                escort ends, CLEAR sends ONE POST {officer} and shows the
 *                corridor event's to_status
 *   planner (/)  a labelled (test / rehearsal) movement wears a TEST pill on
 *                the LiveRail, and only it; the planner's label rides
 *                POST /movements as the movement title; an escort the brain
 *                refuses 409 (its corridor event closed on /ops) shows the
 *                brain's own reason — never the raw HTTP body
 *   IST          the dossier's phase windows equal the board's and are the
 *                IST rendering of the wire instants
 *   hygiene      no 404, no page error, nothing passed through to a backend
 *
 * Usage:
 *   node tests/ops-wall-smoke.mjs [origin]     # default http://127.0.0.1:5199
 *
 * The deck must be serving its adapter-node build at the origin (spawned
 * from build/ on the origin's port when nothing answers). Exits 0 with
 * `ok <name> — <detail>` lines, 1 on any FAIL. Screenshots land in
 * tests/artifacts/ops-wall-*.png.
 */

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const ARTIFACTS = path.join(HERE, 'artifacts');
const ORIGIN = (process.argv[2] ?? 'http://127.0.0.1:5199').replace(/\/$/, '');

// Puppeteer is vendored once, in the repo's legacy frontend tree.
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

/** CHROME_PATH, else the newest cached Chrome for Testing, else puppeteer's own. */
function findChrome() {
	if (process.env.CHROME_PATH && existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
	const cache = path.join(os.homedir(), '.cache', 'puppeteer', 'chrome');
	if (existsSync(cache)) {
		for (const b of readdirSync(cache).sort().reverse()) {
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
const T0 = Date.now();
const iso = (msAgo = 0) => new Date(T0 - msAgo).toISOString();
/** A registration mark in raw form (masked marks carry •••). */
const RAW_PLATE = /\b[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4}\b/g;
const OFFICER = 'Insp. K. Joshi (smoke)';

// ─── the fixture world ───────────────────────────────────────────────────────

const P = '5a1e0000-0000-4000-8000-00000000000a'; // pursuit — planning
const E = '5a1e0000-0000-4000-8000-00000000000b'; // procession — active, emulated, phased
const W = '5a1e0000-0000-4000-8000-00000000000c'; // work zone — approved
const GONE = '5a1e0000-0000-4000-8000-00000000000d'; // a completed event (off the board)
const NEWP = '5a1e0000-0000-4000-8000-00000000000e'; // the pursuit the smoke opens
const V = (n) => `5a1e0000-0000-4000-8000-0000000001${String(n).padStart(2, '0')}`;
const R = (n) => `5a1e0000-0000-4000-8000-0000000002${String(n).padStart(2, '0')}`;
const D = (n) => `5a1e0000-0000-4000-8000-0000000003${String(n).padStart(2, '0')}`;

/** Wire phase windows of the procession — fixed instants on another day. */
const E_PHASES = [
	{ label: 'assembly', window_start: '2026-06-27T03:30:00Z', window_end: '2026-06-27T06:15:00Z', state: 'completed' },
	{ label: 'procession', window_start: '2026-06-27T06:15:00Z', window_end: null, state: 'active' },
];

const SEGMENTS = [
	[1005, 'Relief Road', 'primary', [[72.5798, 23.0254], [72.585, 23.02], [72.59, 23.018]]],
	[1012, 'Gandhi Road', 'trunk', [[72.5798, 23.0254], [72.581, 23.028], [72.583, 23.031]]],
	[1013, 'Tilak Road', 'primary', [[72.59, 23.018], [72.595, 23.02], [72.6006, 23.0251]]],
	[1014, 'Station Road (Kalupur)', 'primary', [[72.6006, 23.0251], [72.605, 23.028], [72.61, 23.03]]],
	[1015, 'Nehru Bridge Road', 'trunk', [[72.5714, 23.0225], [72.575, 23.025], [72.5798, 23.0254]]],
	[1016, 'SG Highway (Vastrapur)', 'motorway', [[72.515, 23.03], [72.52, 23.029], [72.5293, 23.0327]]],
	[1038, 'Kankaria Road', 'secondary', [[72.59, 23.018], [72.595, 23.01], [72.6, 23.007]]],
].map(([id, name, cls, coords]) => ({
	segment_id: id,
	osm_way_id: null,
	road_name: name,
	road_class: cls,
	lanes: 2,
	speed_limit_kmh: 40,
	oneway: false,
	geometry: { type: 'LineString', coordinates: coords },
}));

const JUNCTIONS = [
	[3, 'Lal Darwaja', 72.5798, 23.0254, 'signal', true],
	[4, 'Paldi - CG Road Circle', 72.562, 23.03, 'roundabout', false],
	[5, 'Relief Road - Tilak Road - Kankaria Junction', 72.59, 23.018, 'intersection', true],
	[6, 'Riverfront West Junction', 72.565, 23.035, 'signal', true],
	[15, 'Tilak Road - Station Road (Kalupur)', 72.6006, 23.0251, 'intersection', false],
].map(([id, name, lon, lat, type, sig]) => ({
	urn: `junction:${id}`,
	kind: 'junction',
	id: String(id),
	label: name,
	lon,
	lat,
	state: null,
	band: null,
	source: 'live',
	t: null,
	age_sec: null,
	links: [],
	props: { name, name_gu: null, junction_type: type, signal_control: sig },
}));

const USE_CASES = [
	['vvip_convoy_base', 'VVIP convoy — base', 'baseline', 'Z+'],
	['rath_yatra_ahmedabad', 'Rath Yatra — Ahmedabad', 'event', 'X'],
	['rath_yatra_monsoon_ambulance', 'Rath Yatra Morning, Monsoon Burst, One Siren', 'multi_event', 'X'],
	['kumbh_scale_gathering', 'Kumbh-scale gathering', 'event', 'X'],
	['riverfront_marathon', 'Riverfront marathon', 'event', 'X'],
	['national_day_parade', 'National day parade', 'event', 'Z'],
	['motera_stadium_egress', 'Motera stadium egress', 'event', 'X'],
	['flood_evacuation_corridor', 'Flood evacuation corridor', 'weather', 'X'],
	['metro_work_zone', 'Metro work zone', 'ops', 'X'],
	['watchlist_anpr_sweep', 'Stolen-vehicle ANPR sweep', 'enforcement', 'X'],
].map(([id, name, category, cls]) => ({ id, name, category, vvip_class: cls, horizon_min: 60, seed: 7, route_segment_ids: [1005, 1013] }));

function eventRow(id, kind, title, status, extra = {}) {
	return {
		event_id: id,
		event_kind: kind,
		protection_class: 'none',
		crowd_class: 'LOW',
		title,
		description: null,
		status,
		movement_id: null,
		routes: [{ label: 'primary', segment_ids: [1005, 1013] }],
		phases: [],
		planned_start: null,
		planned_end: null,
		metadata: {},
		...extra,
	};
}

/** The oracle's own text masking (city/common.py): GJ27EM9001 → GJ27EM•••1. */
function wireMask(text) {
	return text.replace(/\b([A-Z]{2}[ -]?\d{1,2}[ -]?(?:[A-Z]{1,3}[ -]?)?)(\d{4})\b/g, (_m, head, digits) => `${head}•••${digits.slice(-1)}`);
}

function feedItem(id, msAgo, kind, actor, actorKind, text, source = 'live', urns = []) {
	return { id, t: iso(msAgo), kind, urns, actor, actor_kind: actorKind, text, source, severity: 'info', props: {} };
}

function sighting(lon, lat, msAgo, source = 'emulated_anpr') {
	return { t: iso(msAgo), position: [lon, lat], speed_kmh: 88, heading: 78, source };
}

function recommendation(id, vehicleId, plate, status, text, rule = 'R_SPD_SEGMENT', createdMsAgo = 900_000) {
	return {
		recommendation_id: id,
		sighting_id: `${id}-s`,
		vehicle_id: vehicleId,
		plate,
		rule_id: rule,
		recommendation: text,
		evidence: { source: 'emulated_anpr', camera_id: 'EM-CAM-03', plate, speed_kmh: 92, t: iso(400_000) },
		status,
		decided_by: status === 'approved' ? 'Duty Officer R. Mehta' : null,
		decided_at: status === 'approved' ? iso(880_000) : null,
		audit_tail: [],
		created_at: iso(createdMsAgo),
	};
}

function dispatchRow(id, eventId, agency, unitId, status, requestedBy, waypoint, msAgo) {
	return {
		dispatch_id: id,
		event_id: eventId,
		agency,
		agency_label: { jan_rakshak: 'Jan Rakshak', police: 'Police', ambulance: '108 Ambulance', traffic: 'Traffic', fire: 'Fire' }[agency],
		unit_id: unitId,
		waypoint_junction: waypoint,
		status,
		requested_by: requestedBy,
		decided_by: null,
		decision_note: null,
		approved_at: null,
		created_at: iso(msAgo),
		updated_at: iso(msAgo),
		audit: [{ from_status: null, to_status: 'requested', actor: requestedBy, note: null, at: iso(msAgo) }],
	};
}

const world = {
	events: [
		eventRow(P, 'vehicle_pursuit', 'PURSUIT — stolen GJ27EM9001 (FIR 4401/2026, Kalupur PS)', 'planning', {
			routes: [{ label: 'pursuit anchor', segment_ids: [1015] }],
			metadata: { vehicle_id: V(1), recommendation_id: R(0) },
		}),
		eventRow(E, 'procession', 'Rath Yatra — smoke procession (EMULATED)', 'active', {
			phases: E_PHASES,
			metadata: { source: 'event-emulator', mode: 'use_case', run_id: 39 },
		}),
		eventRow(W, 'work_zone', 'Metro work zone — Relief Road', 'approved'),
	],
	audit: {
		[P]: [
			feedItem('event_status_log:p0', 900_000, 'event', 'Duty Officer R. Mehta', 'human', 'Event opened: PURSUIT — stolen GJ27EM•••1 (FIR 4401/2026, Kalupur PS) (vehicle_pursuit) by Duty Officer R. Mehta', 'live', [`event:${P}`, `vehicle:${V(1)}`]),
			feedItem('enforcement_audit:r0', 880_000, 'enforcement', 'Duty Officer R. Mehta', 'human', 'e-challan APPROVED by Duty Officer R. Mehta (R_SPD_SEGMENT, GJ27EM•••1): approved per FIR 4401/2026', 'emulated', [`recommendation:${R(0)}`, `vehicle:${V(1)}`]),
			feedItem('anpr:s1', 300_000, 'anpr', 'EM-CAM-02', 'machine', 'ANPR hit · STOLEN VEHICLE · GJ27EM•••1 at EM-CAM-02 (112 km/h)', 'emulated', [`vehicle:${V(1)}`, 'camera:EM-CAM-02']),
			// a model wrote the RAW mark into its text — the deck must mask it
			feedItem('decisions_log:b1', 240_000, 'brain', 'convoy-brain', 'machine', 'decider: hold GJ27EM9001 at Lal Darwaja — 41 s ahead of the vehicle', 'live', [`event:${P}`]),
			feedItem('event_status_log:sys', 200_000, 'event', 'event-emulator (system)', 'system', 'Event window open (EMULATED cause)', 'emulated', [`event:${P}`]),
		],
		[E]: [feedItem('event_status_log:e0', 1_200_000, 'event', 'event-emulator (system)', 'system', 'Event opened: Rath Yatra — smoke procession (procession) by event-emulator', 'emulated', [`event:${E}`])],
		[W]: [],
	},
	cityFeed: [
		feedItem('situation_log:c1', 60_000, 'ama', 'AMA situation engine', 'machine', 'AMA band GREEN → AMBER (severity 7)', 'emulated'),
		feedItem('dispatch_events:c2', 120_000, 'dispatch', 'Duty Officer R. Mehta', 'human', '108 Ambulance 108-14 → J5: requested → approved by Duty Officer R. Mehta', 'emulated'),
	],
	vehicles: [
		{ id: V(1), plate: 'GJ27EM9001', state: 'flagged', reason: 'STOLEN VEHICLE — FIR 4401/2026, Kalupur PS', at: [72.575, 23.025] },
		{ id: V(2), plate: 'GJ01AB1234', state: 'flagged', reason: 'stolen — FIR 77/2026, Naranpura PS', at: [72.7, 23.15] },
		{ id: V(3), plate: 'GJ05CD5678', state: 'flagged', reason: 'stolen — FIR 81/2026, Kalupur PS', at: [72.585, 23.0202] },
		{ id: V(4), plate: 'GJ18EF4321', state: 'watch', reason: 'court warrant watch', at: null, basis: 'court_warrant' },
		{ id: V(5), plate: 'GJ06GH2468', state: 'flagged', reason: 'stolen — FIR 90/2026, Maninagar PS', at: [72.595, 23.0202] },
		// a warrant watch (NOT stolen): its penal e-challan may be approved
		{ id: V(6), plate: 'GJ01KL7777', state: 'watch', reason: 'court warrant watch — case 12/2026', at: null, basis: 'court_warrant' },
		// reported stolen after an e-challan was approved: that fine is never a pursuit basis (C-N)
		{ id: V(9), plate: 'GJ01TU3131', state: 'flagged', reason: 'stolen — FIR 133/2026, Navrangpura PS', at: [72.5813, 23.0264] },
		// reported stolen, no fix yet: its ANPR sightings are R_STOLEN_SIGHTING evidence
		{ id: V(8), plate: 'GJ01PQ8080', state: 'flagged', reason: 'stolen — FIR 120/2026, Vastrapur PS', at: null },
	].map((v) => ({
		vehicle_id: v.id,
		plate: v.plate,
		vehicle_class: 'car',
		earmark_state: v.state,
		earmark_reason: v.reason,
		lawful_basis: v.basis ?? 'stolen_vehicle_fir',
		retention_until: iso(-20 * 86_400_000),
		last_sighting: v.at ? sighting(v.at[0], v.at[1], 180_000) : null,
	})),
	recommendations: [
		recommendation(R(0), V(1), 'GJ27EM9001', 'approved', 'GJ27EM9001 read at 95 km/h in a 40 km/h segment'),
		recommendation(R(1), V(1), 'GJ27EM9001', 'pending', 'GJ27EM9001 read at 92 km/h in a 40 km/h segment'),
		recommendation(R(2), V(3), 'GJ05CD5678', 'pending', 'GJ05CD5678 read at 71 km/h in a 40 km/h segment'),
		// a reported-stolen vehicle is pursued on its APPROVED sighting, never on a fine (C-N)
		recommendation(R(5), V(5), 'GJ06GH2468', 'approved', 'Stolen vehicle GJ06GH2468 sighted at EM-CAM-03 (FIR 90/2026)', 'R_STOLEN_SIGHTING'),
		// not stolen (warrant watch): APPROVE is offered
		recommendation(R(3), V(6), 'GJ01KL7777', 'pending', 'GJ01KL7777 read at 78 km/h in a 40 km/h segment'),
		// a vehicle the deck's watchlist does not (yet) know as stolen: the oracle answers 409
		recommendation(R(4), V(7), 'GJ01RS4545', 'pending', 'GJ01RS4545 read at 74 km/h in a 40 km/h segment'),
		recommendation(R(8), V(9), 'GJ01TU3131', 'approved', 'GJ01TU3131 read at 68 km/h in a 40 km/h segment'),
		// an earlier approved sighting SPENT on a closed pursuit (e.g. a scripted showcase's):
		// one approval authorises ONE pursuit, so the card still offers AUTHORISE PURSUIT (C6)
		{ ...recommendation(R(9), V(8), 'GJ01PQ8080', 'approved', 'Stolen vehicle GJ01PQ8080 sighted at EM-CAM-02 (FIR 120/2026)', 'R_STOLEN_SIGHTING', 2_400_000), pursuit: { event_id: '5a1e0000-0000-4000-8000-0000000003f1', status: 'cancelled' } },
		// two ANPR sightings of a reported-stolen vehicle (evidence, never a fine)
		recommendation(R(6), V(8), 'GJ01PQ8080', 'pending', 'Stolen vehicle GJ01PQ8080 sighted at EM-CAM-04 (FIR 120/2026)', 'R_STOLEN_SIGHTING', 300_000),
		recommendation(R(7), V(8), 'GJ01PQ8080', 'pending', 'Stolen vehicle GJ01PQ8080 sighted at EM-CAM-05 (FIR 120/2026)', 'R_STOLEN_SIGHTING', 100_000),
	],
	dispatches: [
		dispatchRow(D(1), E, 'ambulance', '108-14', 'requested', '108-driver', 5, 50_000),
		// an officer typed a plate into the request note — free text, printed under the plate rule
		{
			...dispatchRow(D(2), E, 'jan_rakshak', 'JR-EM-02', 'requested', 'Duty Officer R. Mehta', 3, 70_000),
			audit: [{ from_status: null, to_status: 'requested', actor: 'Duty Officer R. Mehta', note: 'intercept GJ05CD5678 at Lal Darwaja', at: iso(70_000) }],
		},
		dispatchRow(D(3), GONE, 'jan_rakshak', 'JR-EM-01', 'on_scene', 'God’s Eye Morning showcase', 5, 9_000_000),
		// terminal and older than 30 min → behind 'history (2)'; the recent one stays a tile
		dispatchRow(D(4), E, 'traffic', 'TR-EM-03', 'completed', 'Duty Officer R. Mehta', 3, 7_200_000),
		dispatchRow(D(5), W, 'police', 'PCR-EM-02', 'cancelled', 'Duty Officer R. Mehta', null, 3_000_000),
		dispatchRow(D(6), E, 'traffic', 'TR-EM-04', 'completed', 'Duty Officer R. Mehta', 5, 300_000),
	],
	runs: [
		{ run_id: 40, use_case_id: 'watchlist_anpr_sweep', status: 'completed', clock_scale: 10, created_at: iso(3_600_000), ended_at: iso(3_000_000), progress: { frame: 600, total: 600, delivered: 600, failed: 0 } },
		{ run_id: 39, use_case_id: 'rath_yatra_ahmedabad', status: 'completed', clock_scale: 10, created_at: iso(7_200_000), ended_at: iso(6_600_000), progress: { frame: 720, total: 720, delivered: 720, failed: 0 } },
	],
	livingCity: { enabled: false, status: 'stopped', date_ist: '2026-10-22', day_seed: 1411, active_slots: [], next_causes: [{ at: iso(-600_000), kind: 'work_zone', title: 'night resurfacing — Ashram Road' }], units: [], since: null },
	posts: [],
	refused: [],
	unstubbed: [],
	runGets: 0,
	/** Query strings of the run-progress GETs (they must skip the tape). */
	runGetQueries: [],
	/** Audit-probe POSTs (a read of the brain — never a city write). */
	probes: [],
	/** The running escort the cockpit adopts (contract C-J status doc). */
	escort: null,
	/** Escort proposals the brain refuses 409 when answered: approval_id → {status, detail, reason}. */
	escortRefusals: {},
};

const MVMT = 'mvmt-smoke-01';
const PM = '5a1e0000-0000-4000-8000-00000000000f'; // the protected movement's corridor event

/** The planner's labelled movement (a rehearsal) and its corridor event. */
const PLAN_MVMT = 'mvmt-smoke-02';
const TPM = '5a1e0000-0000-4000-8000-000000000010';
const PLAN_LABEL = 'REHEARSAL smoke';
/** The brain's 409 when the movement's corridor event was closed on /ops. */
const ESCORT_REFUSAL = `corridor event ${TPM} is cancelled — an officer closed it; plan a new movement to escort again`;

/** POST /movements/{id}/plan for the planner section (contract C-I). */
function planDoc() {
	return {
		movement_id: PLAN_MVMT,
		primary_route: {
			route_id: 'mip_smoke',
			segment_ids: [1005, 1013],
			road_names: ['Relief Road', 'Tilak Road'],
			estimated_time_sec: 540,
			distance_m: 4200,
			route_tier: 'compliant',
			lane_violations: [],
		},
		alternate_routes: [],
		rejected_routes: [],
		overall_reasoning: 'Relief Road → Tilak Road: 4.2 km, 9 min at the segment limits.',
		status: 'approved',
		event_id: null,
		routing_tier: 'heuristic',
	};
}

function escortDoc() {
	return {
		movement_id: MVMT,
		event_id: PM,
		status: 'running',
		iteration: 14,
		started_at: iso(240_000),
		updated_at: new Date().toISOString(),
		position: { lon: 72.586, lat: 23.0195, speed_kmh: 31, heading: 95, t: new Date().toISOString(), source: 'emulated_gps' },
		gps: { mode: 'emulated' },
		traffic_feed: { simulated: true },
		progress: 0.42,
		eta_sec: 420,
		active_diversions: [],
		hold: { segment_ids: [1013], ordered_by: 'officer Duty Officer R. Mehta', reason: 'queue ahead on Tilak Road', peak_congestion: 0.82, since: iso(20_000), max_sec: 120 },
		// escort authority (officer approves every switch): ONE open proposal
		pending_approval: null,
		pending_approvals: [],
		approval_outcomes: [],
		approval_timeout_sec: 90,
		escort_authority: 'officer_approves_every_switch',
	};
}

/** The brain's reason for an unanswered proposal (live_escort.APPROVAL_TIMEOUT_REASON). */
const ESCORT_TIMEOUT_REASON = 'no officer response — current plan held';

/**
 * One OPEN escort proposal as the brain's C-J pending_approval carries it
 * (live_escort._approval_view): what approving it would do, the decider's
 * tier + confidence, created_at / expires_at — the AI never executes it.
 */
function escortProposal(id, kind, { ttlMs = 85_000, confidence = 0.93, rule = false } = {}) {
	const created = Date.now() - 5_000;
	const base = {
		approval_id: id,
		kind,
		status: 'pending',
		trigger: 'incident_detected',
		// no model answered: the deterministic rule stood in (labelled so)
		tier: rule ? 'deterministic' : 'decider',
		confidence: rule ? null : confidence,
		...(rule ? { checkpoint_tier: 'gemma' } : {}),
		reason: rule
			? 'strategic tier unavailable (decider degraded: model-host unreachable)'
			: `decider (decider) at confidence ${confidence.toFixed(2)} — an officer approves every switch`,
		raised_at: new Date(created).toISOString(),
		created_at: new Date(created).toISOString(),
		expires_at: new Date(created + ttlMs).toISOString(),
		timeout_sec: 90,
		segment_ids: [1013],
		peak_congestion: 0.86,
		data_quality: 'synthetic',
		simulated: true,
	};
	if (kind === 'hold') {
		return {
			...base,
			action: 'hold_position',
			segment_ids: [1005],
			summary: 'hold the convoy short of Relief Road (congestion 0.86) until it clears, up to 180 s',
			proposed: { hold_segment_ids: [1005], max_hold_sec: 180, hold_reason: 'held short of congestion 0.86 on Relief Road' },
		};
	}
	const route = [1012, 1015, 1038, 1005];
	return {
		...base,
		action: kind === 'plan_b' ? 'activate_plan_b' : 'emergency_reroute',
		summary: `${kind === 'plan_b' ? 'Plan B' : 're-route'} via Kankaria Road — 4.3 km left (now 3.4 km, 1.3x), avoiding Tilak Road (congestion 0.86)`,
		proposed: {
			route_source: kind,
			segment_ids: route,
			tail_segment_ids: route.slice(1),
			route_id: kind === 'plan_b' ? null : 'r-smoke',
			road_names: ['Gandhi Road', 'Kankaria Road', 'Relief Road'],
			via_segment_ids: [1038],
			via_road_names: ['Kankaria Road'],
			remaining_m: 3400,
			new_remaining_m: 4300,
			avoids: [1013],
			long_detour: false,
			closure_type: 'partial',
			closures_new: [1038],
			closures_lifted: [1013],
		},
		detour_segment_ids: route,
		detour_m: 4300,
		remaining_m: 3400,
	};
}

/** The escort raises a proposal (one open per escort: a newer one supersedes). */
function openEscortProposal(p) {
	const e = world.escort;
	if (e.pending_approval) closeEscortProposal(e.pending_approval.approval_id, 'superseded', 'system', 'a newer proposal replaces it — one open proposal per escort');
	e.pending_approval = p;
	e.pending_approvals = [p];
}

/** A proposal closes: decided by a named officer, expired, superseded. */
function closeEscortProposal(aid, status, by, reason = null) {
	const e = world.escort;
	if (!e) return;
	const p = e.pending_approval?.approval_id === aid ? e.pending_approval : { approval_id: aid, action: 'activate_plan_b', kind: 'plan_b', summary: null };
	e.pending_approval = e.pending_approval?.approval_id === aid ? null : e.pending_approval;
	e.pending_approvals = e.pending_approvals.filter((x) => x.approval_id !== aid);
	e.approval_outcomes = [
		...e.approval_outcomes,
		{ approval_id: aid, action: p.action, kind: p.kind, summary: p.summary, status, decided_by: by, decided_at: new Date().toISOString(), ...(reason ? { expiry_reason: reason } : {}), ...(status === 'approved' ? { applied: true } : {}) },
	].slice(-5);
}

/** GET /escorts (the brain's one read for every console's escort lane). */
function escortsDoc() {
	const e = world.escort;
	const alive = !!e && e.status === 'running';
	return {
		escorts: e
			? [
					{
						movement_id: e.movement_id,
						event_id: e.event_id,
						status: e.status,
						alive,
						officer: 'Duty Officer R. Mehta',
						started_at: e.started_at,
						updated_at: new Date().toISOString(),
						progress: e.progress,
						gps_mode: e.gps?.mode ?? null,
						traffic_simulated: e.traffic_feed?.simulated ?? null,
						pending_approval: alive ? e.pending_approval : null,
						approval_outcomes: e.approval_outcomes,
						approval_timeout_sec: e.approval_timeout_sec,
					},
				]
			: [],
		count: e ? 1 : 0,
		pending: alive && e.pending_approval ? 1 : 0,
		authority: 'officer_approves_every_switch',
		t: new Date().toISOString(),
	};
}

/**
 * What the brain could stream for the audit probe: its get_active_events
 * tool returns RAW event titles, and the model repeats them.
 */
const PROBE_NDJSON = [
	{ type: 'thought', data: { stepIndex: 0, text: 'checking the active events for the selected pursuit', tier: 'gemma' } },
	{ type: 'tool_call', data: { id: 't1', name: 'get_active_events', args: { status: 'active' } } },
	{ type: 'tool_result', data: { id: 't1', name: 'get_active_events', ok: true, summary: '3 events: PURSUIT — stolen GJ27EM9001 (FIR 4401/2026, Kalupur PS)' } },
	{ type: 'token', data: 'The pursuit of GJ27EM' },
	{ type: 'token', data: '9001 is near Lal Darwaja; band AMBER.' },
	{ type: 'done', data: { session_id: 'smoke-probe', tiers_used: ['gemma:aditi-ps18-v7h-12b'], operator_alerts: [], degraded: false } },
]
	.map((l) => JSON.stringify(l))
	.join('\n');

function vehiclesDoc() {
	return { vehicles: world.vehicles };
}

function interceptionPlan() {
	return {
		event_id: P,
		vehicle_id: V(1),
		latest_sighting: { sighting_id: 'sight-1', plate: 'GJ27EM9001', t: iso(120_000), position: [72.575, 23.025], speed_kmh: 96, heading: 78, source: 'emulated_anpr', camera_id: 'EM-CAM-02' },
		seed_segment_id: 1015,
		max_cost_sec: 600,
		interception_junctions: [
			{ junction_id: 3, junction_type: 'signal', signal_control: true, position: [72.5798, 23.0254], graph_cost_sec: 41, choke_weight: 0.9, score: 0.021, name: 'Lal Darwaja', downstream: true, likely_passed: true },
			{ junction_id: 5, junction_type: 'intersection', signal_control: true, position: [72.59, 23.018], graph_cost_sec: 170, choke_weight: 0.7, score: 0.004, name: 'Relief Road - Tilak Road - Kankaria Junction', downstream: true, likely_passed: false },
			{ junction_id: 15, junction_type: 'intersection', signal_control: false, position: [72.6006, 23.0251], graph_cost_sec: 290, choke_weight: 0.5, score: 0.0017, name: 'Tilak Road - Station Road (Kalupur)', downstream: false, likely_passed: false },
		],
		note: 'pursuits are human-initiated (C6); this plan assists the operator, it never dispatches on its own',
		sighting_age_sec: 120,
		stale: false,
		stale_after_sec: 600,
		direction_of_travel: { basis: 'oneway', towards_junction_ids: [3], segment_bearing_deg: 78, position_along_segment: 0.5, seed_segment_oneway: true, against_oneway: true, onward_segment_ids: [1005, 1012] },
	};
}

function situationLog(eventId) {
	if (eventId !== E) return { entries: [] };
	return {
		entries: [
			{ timestamp_utc: iso(600_000), situation_class: 'GREEN', observed_class: 'GREEN', transitioned: false, severity_score: 3 },
			{ timestamp_utc: iso(300_000), situation_class: 'AMBER', observed_class: 'AMBER', transitioned: true, severity_score: 7 },
			{ timestamp_utc: iso(10_000), situation_class: 'AMBER', observed_class: 'RED', transitioned: false, severity_score: 11 },
		],
	};
}

function runRow(r) {
	return {
		run_id: r.run_id,
		spec: { use_case_id: r.use_case_id, clock_scale: r.clock_scale, is_replay: !!r.is_replay, replay_of_run_id: r.replay_of ?? null },
		use_case_id: r.use_case_id,
		clock_scale: r.clock_scale,
		status: r.status,
		created_at: r.created_at,
		ended_at: r.ended_at ?? null,
		progress: r.progress,
		event_id: null,
		movement_id: null,
		event_status: r.event_status ?? null,
	};
}

/**
 * The router: (method, pathname, search, body) → {status, body, delayMs} |
 * null (not an API path). A non-GET the world does not expect is refused.
 */
function route(method, pathname, search, body) {
	const q = new URLSearchParams(search);
	const json = (b, status = 200, delayMs = 0) => ({ status, body: b, delayMs });
	let hit;

	if (pathname === '/api/session') return json({ gate: false, officer: null, expires_at: null });

	// ── emulator (C-G, via the deck's /api/emulator proxy) ────────────────
	if (pathname.startsWith('/api/emulator/')) {
		const p = pathname.slice('/api/emulator'.length);
		if (p === '/health') {
			return json({ status: 'ok', library: 10, stack: { store: true, valkey: true, nats: true, twin: true }, living_city: { enabled: world.livingCity.enabled, status: world.livingCity.status, active_slots: world.livingCity.active_slots.length } });
		}
		if (p === '/api/v1/emulator/use-cases') return json(USE_CASES);
		if (p === '/api/v1/emulator/runs' && method === 'GET') return json({ runs: world.runs.map(runRow) });
		if ((hit = p.match(/^\/api\/v1\/emulator\/runs\/(\d+)$/)) && method === 'GET') {
			world.runGetQueries.push(search);
			const r = world.runs.find((x) => x.run_id === Number(hit[1]));
			if (!r) return json({ detail: 'unknown run' }, 404);
			if (r.status === 'running' && r.progress) {
				world.runGets++;
				r.progress = { ...r.progress, frame: Math.min(r.progress.total, r.progress.frame + 37), delivered: Math.min(r.progress.total, r.progress.delivered + 37) };
			}
			return json(runRow(r));
		}
		if (p === '/api/v1/emulator/runs' && method === 'POST') {
			world.posts.push({ method, path: pathname, body });
			if (body?.use_case_id === 'metro_work_zone') {
				// A synchronous (pre-C-G) build: the ledger row exists at once
				// (the next serial id), the POST answers only when the tape ends.
				// Its row is stamped by a skewed server clock (−10 min): the panel
				// binds it by ledger id, never by comparing clocks.
				world.runs.unshift({ run_id: 44, use_case_id: 'metro_work_zone', status: 'running', clock_scale: body.clock_scale, created_at: new Date(Date.now() - 600_000).toISOString(), progress: null });
				const done = () => {
					const r = world.runs.find((x) => x.run_id === 44);
					if (r) Object.assign(r, { status: 'completed', ended_at: new Date().toISOString() });
				};
				setTimeout(done, 3400);
				return json({ run_id: 44, status: 'completed', use_case_id: 'metro_work_zone' }, 200, 3500);
			}
			// its corridor event is still 'planning' (the emulator retries activation every 5 s)
			world.runs.unshift({ run_id: 41, use_case_id: body?.use_case_id, status: 'running', clock_scale: body?.clock_scale, created_at: new Date().toISOString(), progress: { frame: 0, total: 734, delivered: 0, failed: 0 }, event_status: 'planning' });
			return json({ run_id: 41, status: 'running', use_case_id: body?.use_case_id }, 202);
		}
		if ((hit = p.match(/^\/api\/v1\/emulator\/runs\/(\d+)\/stop$/)) && method === 'POST') {
			world.posts.push({ method, path: pathname, body });
			const r = world.runs.find((x) => x.run_id === Number(hit[1]));
			if (r) Object.assign(r, { status: 'stopped', ended_at: new Date().toISOString() });
			return json({ run_id: Number(hit[1]), status: 'stopped' });
		}
		if ((hit = p.match(/^\/api\/v1\/emulator\/runs\/(\d+)\/replay$/)) && method === 'POST') {
			world.posts.push({ method, path: pathname, body });
			const src = world.runs.find((x) => x.run_id === Number(hit[1]));
			world.runs.unshift({ run_id: 43, use_case_id: src?.use_case_id ?? '—', status: 'running', clock_scale: body?.clock_scale, is_replay: true, replay_of: Number(hit[1]), created_at: new Date().toISOString(), progress: { frame: 0, total: 600, delivered: 0, failed: 0 } });
			return json({ run_id: 43, status: 'running', use_case_id: src?.use_case_id }, 202);
		}
		if (p === '/api/v1/emulator/living-city') {
			if (method === 'POST') {
				world.posts.push({ method, path: pathname, body });
				const on = body?.action === 'start';
				Object.assign(world.livingCity, {
					enabled: on,
					status: on ? 'running' : 'stopped',
					since: on ? new Date().toISOString() : null,
					active_slots: on ? [{ slot_id: 'd1-s03', kind: 'mass_gathering', title: 'Kankaria evening crowd', event_id: null, starts_at: iso(0), ends_at: iso(-3_600_000), route_segment_ids: [1038] }] : [],
				});
			}
			return json(world.livingCity);
		}
		return null;
	}

	// ── brain ─────────────────────────────────────────────────────────────
	if (pathname === '/api/convoy/chat/stream' && method === 'POST') {
		world.probes.push(body);
		return { status: 200, raw: `${PROBE_NDJSON}\n`, contentType: 'application/x-ndjson', delayMs: 0 };
	}
	if (pathname === `/api/convoy/movements/${MVMT}/escort` && method === 'GET') {
		return world.escort ? json(world.escort) : json({ detail: 'no escort for this movement' }, 404);
	}
	// escort authority: every console's lane, and a named officer's answer
	if (pathname === '/api/convoy/escorts' && method === 'GET') return json(escortsDoc());
	if ((hit = pathname.match(/^\/api\/convoy\/movements\/([^/]+)\/escort\/approvals\/([^/]+)$/)) && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		const e = world.escort;
		const aid = decodeURIComponent(hit[2]);
		if (!e || hit[1] !== e.movement_id) return json({ detail: 'no escort for this movement in this brain process — nothing to approve' }, 409);
		const refusal = world.escortRefusals[aid];
		if (refusal) {
			closeEscortProposal(aid, refusal.status, 'system', refusal.reason);
			return json({ detail: refusal.detail, status: refusal.status, approval_id: aid, decided_by: 'system', superseded_by: null }, 409, 400);
		}
		const p = e.pending_approval;
		if (!p || p.approval_id !== aid) return json({ detail: 'proposal already decided', status: 'approved', approval_id: aid }, 409);
		const status = body?.decision === 'approve' ? 'approved' : 'rejected';
		closeEscortProposal(aid, status, body?.officer ?? null);
		return json({ movement_id: e.movement_id, approval_id: aid, status, action: p.action, kind: p.kind, summary: p.summary, decided_by: body?.officer ?? null, decided_at: new Date().toISOString() }, 200, 700);
	}
	if (pathname === `/api/convoy/movements/${MVMT}/escort/approve` && method === 'POST') {
		// the older route (approval_id in the body) — a deck on a current brain never calls it
		world.posts.push({ method, path: pathname, body });
		return json({ detail: 'approval not pending' }, 409);
	}
	if (pathname === `/api/convoy/movements/${MVMT}/clear` && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		return json({ movement_id: MVMT, status: 'cleared', event_transition: { outcome: 'transitioned', to_status: 'completed' } }, 200, 500);
	}
	// the planner: a labelled movement, its plan, and the escort the brain refuses
	if (pathname === '/api/convoy/movements' && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		return json({ movement_id: PLAN_MVMT, status: 'planning' });
	}
	if (pathname === `/api/convoy/movements/${PLAN_MVMT}/plan` && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		return json(planDoc(), 200, 300);
	}
	if (pathname === `/api/convoy/movements/${PLAN_MVMT}/escort` && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		return json({ detail: ESCORT_REFUSAL, status: 'cancelled', event_id: TPM }, 409, 200);
	}
	if (pathname === '/api/convoy/health') {
		return json({
			status: 'ok',
			strategic_tier: { model_host: 'connected', model: 'pplx-decider-v1-27b', p95_decide_latency_ms: 2300, novelty_latch: 'armed', degraded: false },
			tactical_tier: { llama_server: 'connected', model: 'aditi-ps18-v7h-12b' },
		});
	}

	if (!pathname.startsWith('/api/v1/')) return null;
	const v1 = pathname.slice('/api/v1'.length);

	// ── board + lifecycle ───────────────────────────────────────────────────
	if (v1 === '/events/active') return json({ events: world.events.filter((e) => ['planning', 'approved', 'active'].includes(e.status)) });
	if (v1 === '/events' && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		world.events.push(eventRow(NEWP, 'vehicle_pursuit', body?.title ?? 'PURSUIT', 'planning', { routes: [{ label: 'pursuit anchor', segment_ids: body?.route_spines?.[0]?.segment_ids ?? [] }], metadata: body?.metadata ?? {} }));
		world.audit[NEWP] = [];
		return json({ event_id: NEWP, status: 'planning' });
	}
	if ((hit = v1.match(/^\/events\/([0-9a-f-]{36})\/status$/)) && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		const ev = world.events.find((e) => e.event_id === hit[1]);
		if (hit[1] === W) {
			return json({ detail: 'illegal transition: the event moved on meanwhile (it is completed)', from_status: 'completed', allowed: [] }, 409, 300);
		}
		const from = ev.status;
		ev.status = body.to_status;
		const verb = { approved: 'approved', active: 'activated', completed: 'completed', cancelled: 'cancelled' }[body.to_status];
		const row = feedItem(`event_status_log:${world.posts.length}`, 0, 'event', body.officer, 'human', `Event ${verb}: ${wireMask(ev.title)} (${from} → ${body.to_status}) by ${body.officer}`, 'live', [`event:${hit[1]}`]);
		row.t = new Date().toISOString();
		world.audit[hit[1]].push(row);
		return json({ event_id: hit[1], from_status: from, to_status: body.to_status, at: row.t, actor: body.officer }, 200, 400);
	}
	if ((hit = v1.match(/^\/events\/([0-9a-f-]{36})\/audit$/))) {
		const items = [...(world.audit[hit[1]] ?? [])].sort((a, b) => Date.parse(b.t) - Date.parse(a.t));
		return json({ event_id: hit[1], items });
	}
	if ((hit = v1.match(/^\/events\/([0-9a-f-]{36})\/interception-plan$/))) {
		return hit[1] === P ? json(interceptionPlan()) : json({ detail: 'not a pursuit' }, 422);
	}
	if ((hit = v1.match(/^\/events\/([0-9a-f-]{36})\/dispatch$/)) && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		const row = dispatchRow(D(9), hit[1], body.agency, body.unit_id, 'requested', body.requested_by, body.waypoint_junction ?? null, 0);
		world.dispatches.unshift(row);
		return json(row);
	}
	if (v1 === '/city/feed') return json({ as_of: iso(0), items: [...world.cityFeed, ...(world.audit[P] ?? [])].sort((a, b) => Date.parse(b.t) - Date.parse(a.t)) });
	if (v1 === '/city/entities') return json({ as_of: iso(0), entities: q.get('kinds') === 'junction' ? JUNCTIONS : [], feeds: {} });

	// ── decisions ───────────────────────────────────────────────────────────
	if (v1 === '/enforcement/recommendations') return json({ recommendations: world.recommendations.filter((r) => r.status === (q.get('status') ?? 'pending')) });
	if ((hit = v1.match(/^\/enforcement\/recommendations\/([0-9a-f-]{36})\/decision$/)) && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		if (hit[1] === R(4) && body?.decision === 'approve') {
			// C-N: the oracle withholds a fine on a vehicle reported stolen
			return json({ detail: 'e-challan withheld — vehicle reported stolen', reason: 'stolen_vehicle_fir' }, 409, 300);
		}
		const rec = world.recommendations.find((r) => r.recommendation_id === hit[1]);
		if (rec) Object.assign(rec, { status: body.decision === 'approve' ? 'approved' : 'rejected', decided_by: body.officer, decided_at: new Date().toISOString() });
		return json({ recommendation_id: hit[1], status: rec?.status ?? 'approved', decided_by: body.officer }, 200, 800);
	}
	if (v1 === '/dispatches') return json({ dispatches: world.dispatches });
	if ((hit = v1.match(/^\/dispatches\/([0-9a-f-]{36})\/(decision|status)$/)) && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		const d = world.dispatches.find((x) => x.dispatch_id === hit[1]);
		if (d) d.status = hit[2] === 'decision' ? (body.action === 'approve' ? 'approved' : 'deferred') : body.status;
		return json(d);
	}

	// ── band, AMA, signals ──────────────────────────────────────────────────
	if (v1 === '/adaptive/situation_log') return json(situationLog(q.get('event_id')));
	if (v1 === '/adaptive/overrides') {
		const rows = q.get('event_id') === E ? [{ published_at: iso(280_000), event_id: E, movement_id: null, situation_class: 'AMBER', params: { advance_closure_sec: 120 }, source: 'retrieved', shadow_eval: 'failed_closed', shadow_eval_detail: 'twin_not_calibrated' }] : [];
		return json({ overrides: rows });
	}
	if (v1 === '/adaptive/status') return json({ movements: {}, degradation: { degradation_tier: 'FULL', audit_trail: [], decider_checkpoints_paused: false }, last_shadow_eval: { status: 'passed' } });
	if (v1 === '/adaptive/module') {
		return json({
			module: 'Active Movement Adaptation',
			code: 'AMA',
			requirement: 'R9',
			summary: 'Every active corridor event is classified into a situation band; hysteresis, protocol floors and a twin shadow-eval stand between a reading and a published parameter.',
			stages: [
				{ name: 'Situation', detail: 'classify each active event' },
				{ name: 'Hysteresis', detail: 'a class change must persist' },
				{ name: 'Protocol floors', detail: 'Z+ 180 / Z 120 / Y 60 s' },
				{ name: 'Twin shadow-eval', detail: 'fail closed to baseline' },
				{ name: 'Audited persistence', detail: 'situation_log + param_overrides' },
			],
			protocol_floors_advance_closure_sec: { 'Z+': 180, Z: 120, Y: 60, X: null, none: null },
			endpoints: {},
			fail_closed: true,
		});
	}
	if (v1 === '/signals/plans') {
		return json({
			writer_enabled: false,
			writer_flag_env: 'VVIP_SIGNAL_POLICY_WRITER',
			city_band: null,
			band_subjects: 0,
			band_plan_table: [
				{ band: 'GREEN', priority_share: 0, cross_green_floor_sec: 30, writes: false },
				{ band: 'AMBER', priority_share: 0.06, cross_green_floor_sec: 18, writes: true },
				{ band: 'RED', priority_share: 0.12, cross_green_floor_sec: 14, writes: true },
				{ band: 'BLACK', priority_share: 0.2, cross_green_floor_sec: 10, writes: true },
			],
			last_plan: null,
			gate: { writer_active: false, writer_waiters: 0, twin_in_flight: 2 },
			scope: 'city',
		});
	}
	if (v1 === '/signals/restore' && method === 'POST') {
		world.posts.push({ method, path: pathname, body });
		return json({ restored: [], all: body?.all === true, officer: body?.officer ?? null }, 200, 600);
	}
	if (v1 === '/signals/history') return json({ history: [{ history_id: 7, junction_id: 3, event_id: P, kind: 'snapshot', cycle_sec: 100, source: 'osm_assumed', reason: 'first calibrated write', actor: 'Duty Officer R. Mehta', captured_at: iso(500_000) }] });
	if (v1 === '/signals/state') return json({ junctions: [] });

	// ── watchlist, retention, ledger, provenance, geometry, canvas ─────────
	if (v1 === '/vehicles') return json(vehiclesDoc());
	if (v1 === '/vehicles/retention') {
		return json({ default_retention_days: 30, earmark_retention_cap_days: 90, policy: 'reads kept 30 days; an earmark only extends retention', reaper: { healthy: true, consecutive_failures: 0, last_attempt_at: iso(30_000), last_success_at: iso(30_000), last_result: { removed: 0 } } });
	}
	if (/^\/vehicles\/[0-9a-f-]{36}\/trace$/.test(v1)) return json({ geometry: { type: 'LineString', coordinates: [] }, positions: [] });
	if (/^\/vehicles\/[0-9a-f-]{36}\/sightings$/.test(v1)) return json({ sightings: [] });
	if (v1 === '/ops/degradation-taxonomy') {
		return json({ entries: [{ cause: 'decider_timeout', count: 2, first_seen: Math.round(T0 / 1000) - 3600, last_seen: Math.round(T0 / 1000) - 600, sample_ref: 'ctx:smoke' }], contexts_scanned: 5, rows_scanned: 120, truncated: false, contexts_skipped_corrupt: 0, generated_at: iso(0) });
	}
	if (v1 === '/corridor/summary') return json({ total_segments: 47, avg_speed_kmh: 31, avg_congestion_idx: 0.4, congested_segments: 3, critical_segments: 0, quality_mix: { real: 0, synthetic: 45 }, status: 'ok', data_age_sec: 2 });
	if (v1 === '/spatial/segments') return json({ segments: SEGMENTS });
	if (v1 === '/units') return json({ units: [], count: 0, source: 'emulated_dispatch_projection' });
	if (v1 === '/cameras') return json({ cameras: [] });
	if (v1 === '/traffic/live' && method === 'POST') return json({ observations: [] }); // a read (the flow layer's hot cache)
	return null;
}

// ─── the run ─────────────────────────────────────────────────────────────────

async function originAlive(origin) {
	try {
		return (await fetch(`${origin}/ops`, { signal: AbortSignal.timeout(3000) })).ok;
	} catch {
		return false;
	}
}

/** Spawn `node build` on the origin's port when nothing answers there. */
async function ensureOrigin(origin) {
	if (await originAlive(origin)) return null;
	if (!existsSync(path.join(DECK, 'build', 'index.js'))) throw new Error('no build/ output — run `npm run build` first');
	const port = new URL(origin).port || '80';
	const proc = spawn('node', ['build'], { cwd: DECK, env: { ...process.env, PORT: port, HOST: '127.0.0.1' }, stdio: ['ignore', 'pipe', 'pipe'] });
	proc.stdout.on('data', () => {});
	proc.stderr.on('data', (d) => process.stderr.write(`[deck] ${d}`));
	for (let i = 0; i < 40; i++) {
		if (await originAlive(origin)) return proc;
		await sleep(500);
	}
	proc.kill();
	throw new Error(`deck origin did not come up on ${origin}`);
}

const executablePath = findChrome();
if (!executablePath) throw new Error('no Chrome binary found (set CHROME_PATH)');
mkdirSync(ARTIFACTS, { recursive: true });
const deckProc = await ensureOrigin(ORIGIN);
const browser = await puppeteer.launch({ executablePath, headless: true, args: SWIFTSHADER_ARGS, protocolTimeout: 180_000 });

const responses = [];
const pageErrors = [];

/** Text of the whole wall (what a reader sees). */
const wallText = (page) => page.evaluate(() => document.body.innerText);
const rawPlates = async (page) => [...new Set((await wallText(page)).match(RAW_PLATE) ?? [])];
const postsTo = (re) => world.posts.filter((p) => re.test(p.path));
async function waitFor(page, fn, arg, timeout = 15000) {
	return page.waitForFunction(fn, { timeout, polling: 150 }, arg).then(() => true).catch(() => false);
}
/** Click `sel` once the polls have rendered it (a row can trail the state that names it). */
async function clickWhenShown(page, sel, timeout = 10000) {
	const shown = await waitFor(page, (s) => !!document.querySelector(s), sel, timeout);
	const el = shown ? await page.$(sel) : null;
	if (el) await el.click();
	return !!el;
}
async function buttonIn(page, rootSel, text) {
	const handles = await page.$$(`${rootSel} button`);
	for (const h of handles) {
		const label = await h.evaluate((b) => b.textContent.replace(/\s+/g, ' ').trim());
		if (label.toLowerCase().includes(text.toLowerCase())) return h;
	}
	return null;
}

/**
 * A page whose every /api/* request is answered by the fixture world (never
 * a backend): the wall, a fresh deep-link load, the cockpit.
 */
async function hermeticPage() {
	const page = await browser.newPage();
	await page.setViewport({ width: 1920, height: 1080 });
	await page.emulateTimezone('America/New_York'); // IST must not depend on the host's zone
	page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 300)));
	page.on('response', (r) => responses.push({ status: r.status(), url: r.url() }));
	await page.setRequestInterception(true);
	page.on('request', (req) => {
		const u = new URL(req.url());
		if (u.origin !== ORIGIN || !u.pathname.startsWith('/api/')) {
			req.continue().catch(() => undefined);
			return;
		}
		let body = null;
		try {
			body = req.postData() ? JSON.parse(req.postData()) : null;
		} catch {
			body = req.postData();
		}
		const method = req.method();
		const r = route(method, u.pathname, u.search, body);
		if (!r) {
			// Never let an unknown API call reach a backend: GETs are logged and
			// answered 503 (the panel shows its degradation), writes refused.
			(method === 'GET' ? world.unstubbed : world.refused).push(`${method} ${u.pathname}${u.search}`);
			req.respond({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'ops-wall-smoke: unstubbed' }) }).catch(() => undefined);
			return;
		}
		const send = () =>
			req
				.respond(
					r.raw != null
						? { status: r.status, contentType: r.contentType ?? 'text/plain', body: r.raw }
						: { status: r.status, contentType: 'application/json', body: JSON.stringify(r.body) },
				)
				.catch(() => undefined);
		if (r.delayMs) setTimeout(send, r.delayMs);
		else send();
	});
	return page;
}

/** The deep-link target is marked (the card's root for a queue card, the tile itself otherwise). */
const deepHit = (page, sel) =>
	waitFor(page, (s) => {
		const el = document.querySelector(s);
		return !!el && (el.classList.contains('deep-link-hit') || !!el.firstElementChild?.classList.contains('deep-link-hit'));
	}, sel, 8000);

try {
	const page = await hermeticPage();

	const tLoad = Date.now();
	await page.goto(`${ORIGIN}/ops`, { waitUntil: 'domcontentloaded', timeout: 60000 });
	await page.waitForSelector('[data-testid="event-card"]', { timeout: 30000 });

	// ── audit: a view over the selected pursuit's server rows, within 5 s ────
	const auditOk = await waitFor(page, () => {
		const v = document.querySelector('[data-testid="audit-view"]');
		return v?.dataset.view === 'event' && Number(v.dataset.serverRows) >= 3;
	}, null, 5000);
	ok('audit: ≥ 3 server rows of the selected pursuit within 5 s', auditOk, `${Date.now() - tLoad} ms`);
	const rows = await page.$$eval('[data-testid="audit-feed"] [data-prim="feed-item"]', (els) =>
		els.map((e) => ({ kind: e.dataset.actorKind, id: e.dataset.rowId ?? null, text: e.textContent.replace(/\s+/g, ' ').trim() })),
	);
	const server = rows.filter((r) => r.id);
	ok("audit: a human 'approved by …' row on the act rail", server.some((r) => r.kind === 'human' && /approved by/i.test(r.text)), `${server.length} server rows`);
	ok('audit: camera, oracle and model rows are machine, never human', server.filter((r) => /EM-CAM|convoy-brain/.test(r.text)).every((r) => r.kind === 'machine') && server.some((r) => r.kind === 'machine'));
	ok('audit: emulator bookkeeping rides the system rail', server.some((r) => r.kind === 'system' && /event-emulator/.test(r.text)));
	ok('audit: emulated server rows carry the EMULATED pill', server.some((r) => /EMULATED/.test(r.text) && /EM-CAM-02/.test(r.text)));

	// ── masking: no officer named → no raw plate anywhere ──────────────────
	await waitFor(page, () => !!document.querySelector('[data-testid="pursuit-sighting"]') && !!document.querySelector('[data-testid="queue-card"][data-kind="echallan"]'), null, 15000);
	let raw = await rawPlates(page);
	ok('masking: no raw registration mark on the wall without an officer', raw.length === 0, raw.join(',') || 'none');
	const recTitle = await page.$eval(`[data-testid="queue-card"][data-id="${R(1)}"]`, (e) => e.textContent).catch(() => '');
	ok('masking: the e-challan card prints GJ27EM•••1', recTitle.includes('GJ27EM•••1') && !recTitle.includes('GJ27EM9001'));
	// C-N: a penal card on a vehicle reported stolen — warning chip, NO approve.
	const stolenPenal = `[data-testid="queue-card"][data-kind="echallan"][data-id="${R(1)}"]`;
	ok('stolen: a penal card on a reported-stolen vehicle carries the warning chip', (await page.$eval(stolenPenal, (e) => e.dataset.stolen).catch(() => '')) === '1' && !!(await page.$(`${stolenPenal} [data-testid="stolen-warning"]`)) && !!(await page.$(`${stolenPenal} [data-testid="stolen-note"]`)));
	ok('stolen: …and offers no APPROVE (reject only)', !(await buttonIn(page, stolenPenal, 'approve')) && !!(await buttonIn(page, stolenPenal, 'reject')));
	ok('stolen: a warrant-watch (not stolen) penal card still offers APPROVE', !!(await buttonIn(page, `[data-testid="queue-card"][data-id="${R(3)}"]`, 'approve')));
	// R_STOLEN_SIGHTING rows fold into one card per vehicle.
	const stolenCard = `[data-testid="queue-card"][data-kind="stolen-sighting"][data-id="${V(8)}"]`;
	const sc = await page.$eval(stolenCard, (e) => ({ count: e.dataset.count, ids: e.dataset.recIds ?? '', text: e.textContent.replace(/\s+/g, ' ') })).catch(() => null);
	ok('stolen: two R_STOLEN_SIGHTING rows → ONE card for the vehicle', !!sc && sc.count === '2' && sc.ids.includes(R(6)) && sc.ids.includes(R(7)) && (await page.$$(`[data-testid="queue-card"][data-kind="stolen-sighting"]`)).length === 1, sc ? `${sc.count} sightings` : 'no card');
	ok("stolen: the card says 'e-challan withheld (vehicle reported stolen, FIR …)', masked", !!sc && /e-challan withheld \(vehicle reported stolen, FIR 120\/2026\)/.test(sc.text) && sc.text.includes('GJ01PQ•••0') && !sc.text.includes('GJ01PQ8080'), sc?.text.slice(0, 140));
	ok('stolen: AUTHORISE PURSUIT + DISMISS, never a fine', !!(await buttonIn(page, stolenCard, 'authorise pursuit')) && !!(await buttonIn(page, stolenCard, 'dismiss')) && !(await buttonIn(page, stolenCard, 'approve')), await page.$$eval(`${stolenCard} button`, (bs) => bs.map((b) => b.textContent.replace(/\s+/g, ' ').trim()).join(' | ')).catch(() => 'no card'));
	ok("masking: a model's raw mark in a server row is masked on the wall", rows.some((r) => r.text.includes('hold GJ27EM•••1 at Lal Darwaja')));
	const planPlate = await page.$eval('[data-testid="pursuit-sighting"]', (e) => e.textContent).catch(() => '');
	ok('masking: the interception plan prints the masked mark', planPlate.includes('GJ27EM•••1'), planPlate.replace(/\s+/g, ' ').slice(0, 60));
	const activateLocked = await page.$eval(`[data-testid="event-card"][data-event-id="${P}"] [data-verb="active"] button`, (b) => b.disabled).catch(() => null);
	ok('gate: ACTIVATE is locked until an officer is named', activateLocked === true);

	// The audit probe needs no officer — and its brain answer carries raw marks.
	await page.click('.probe-toggle');
	await page.waitForSelector('.probe button', { timeout: 8000 });
	await (await page.$$('.probe button'))[0].click();
	const answered = await waitFor(page, () => /band AMBER/.test(document.querySelector('[data-testid="audit-probe-answer"]')?.textContent ?? ''), null, 10000);
	const answer = await page.$eval('[data-testid="audit-probe-answer"]', (e) => e.textContent).catch(() => '');
	ok('masking: the audit-probe answer box prints the masked mark', answered && answer.includes('GJ27EM•••1') && !answer.includes('GJ27EM9001'), answer.trim().slice(0, 80));
	await waitFor(page, () => [...document.querySelectorAll('[data-testid="audit-feed"] [data-prim="feed-item"]')].some((r) => /get_active_events/.test(r.textContent) && /result/.test(r.textContent)), null, 6000);
	ok('masking: the probe went out once (a read — no officer needed)', world.probes.length === 1 && typeof world.probes[0]?.message === 'string', JSON.stringify(world.probes[0] ?? {}).slice(0, 120));
	// A plate typed into a dispatch note.
	await (await page.$(`[data-testid="dispatch-tile"][data-dispatch-id="${D(2)}"] .audit-toggle`)).click();
	const tail = await page.$eval(`[data-testid="dispatch-tile"][data-dispatch-id="${D(2)}"] ul.audit`, (e) => e.textContent).catch(() => '');
	ok('masking: a plate typed into a dispatch note prints masked in the tile’s audit tail', tail.includes('GJ05CD•••8') && !tail.includes('GJ05CD5678'), tail.replace(/\s+/g, ' ').trim().slice(0, 90));
	raw = await rawPlates(page);
	ok('masking: still no raw mark on the wall after the probe and the open tail', raw.length === 0, raw.join(',') || 'none');
	ok('dossier: a pursuit gets no citizen advisory preview', !!(await page.$('[data-testid="dossier-advisory-withheld"]')));

	// ── band / AMA / posture ───────────────────────────────────────────────
	ok('AMA: a band nobody reported is UNKNOWN, never GREEN', (await page.$eval('[data-testid="ama-band"]', (e) => e.dataset.band)) === 'UNKNOWN');
	const floors = await page.$$eval('[data-testid="ama-floors"] li', (els) => Object.fromEntries(els.map((e) => [e.dataset.class, e.dataset.sec])));
	ok('AMA: protocol floors Z+ 180 / Z 120 / Y 60 s', floors['Z+'] === '180' && floors.Z === '120' && floors.Y === '60', JSON.stringify(floors));
	await waitFor(page, () => document.querySelector('[data-testid="brain-posture"]')?.dataset.decider === 'loaded', null, 15000);
	const posture = await page.$eval('[data-testid="brain-posture"]', (e) => ({ ...e.dataset, text: e.textContent.replace(/\s+/g, ' ') }));
	ok('posture: decider loaded with p95 and novelty check, Gemma tactical, degradation tier', posture.decider === 'loaded' && posture.tier === 'FULL' && /p95 2\.3 s/.test(posture.text) && /novelty check on/.test(posture.text) && !/latch armed/.test(posture.text), posture.text.slice(0, 140));
	ok('posture: the synthetic traffic feed reads SIMULATED FEED', (await page.$eval('[data-testid="feed-provenance"]', (e) => e.dataset.label)) === 'simulated');
	ok('gate card: twin shadow-evals in flight shown as a count', (await page.$eval('[data-testid="gate-twin"]', (e) => e.dataset.count)) === '2');
	await waitFor(page, () => document.querySelector('[data-testid="retention-health"]')?.dataset.healthy === '1', null, 15000);
	ok('watchlist: retention reaper health shown', (await page.$eval('[data-testid="retention-health"]', (e) => e.dataset.healthy)) === '1');

	// ── queue composition ──────────────────────────────────────────────────
	const queue = await page.$$eval('[data-testid="queue-card"]', (els) => els.map((e) => ({ kind: e.dataset.kind, id: e.dataset.id, refusal: e.dataset.refusal ?? '', text: e.textContent.replace(/\s+/g, ' ') })));
	ok('queue: the /driver 108 request leads the queue', queue[0]?.kind === 'corridor-request' && queue[0].id === D(1) && /108/.test(queue[0].text), queue.map((q) => `${q.kind}:${q.id?.slice(-3)}`).join(' '));
	const pursuitCards = queue.filter((q) => q.kind === 'pursuit');
	ok('queue: an open pursuit hides its vehicle (no card for V1)', !pursuitCards.some((q) => q.id === V(1)));
	ok('queue: no road anchor → inline refusal', pursuitCards.find((q) => q.id === V(2))?.refusal === 'no-anchor');
	ok('queue: no approved recommendation → inline refusal', pursuitCards.find((q) => q.id === V(3))?.refusal === 'no-approved-recommendation');
	ok('queue: an anchored, approved vehicle offers the opening', pursuitCards.find((q) => q.id === V(5))?.refusal === '');
	ok('queue: a stolen vehicle whose only approval is an e-challan has no pursuit basis (C-N)', pursuitCards.find((q) => q.id === V(9))?.refusal === 'no-approved-recommendation', pursuitCards.find((q) => q.id === V(9))?.refusal);
	const closedTile = await page.$(`[data-testid="dispatch-tile"][data-dispatch-id="${D(3)}"] [data-testid="dispatch-event-closed"]`);
	ok('dispatch: a unit of a closed event is labelled, not projected', !!closedTile);
	ok('counters: a closed event’s unit is not counted out', (await page.$eval('[data-counter="units-dispatched"]', (e) => e.dataset.value)) === '0');

	// ── the officer names themself → plates unmask ─────────────────────────
	await page.type('[data-testid="duty-officer"]', OFFICER, { delay: 4 });
	await waitFor(page, () => /GJ27EM9001/.test(document.body.innerText), null, 8000);
	raw = await rawPlates(page);
	ok('masking: a named officer sees the raw mark', raw.includes('GJ27EM9001'), raw.join(','));
	await page.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-officer.png') });

	// ── lifecycle: double-click ACTIVATE → one POST, ACTIVE +1, feed row ───
	const active0 = Number(await page.$eval('[data-counter="active-events"]', (e) => e.dataset.value));
	const act = await page.$(`[data-testid="event-card"][data-event-id="${P}"] [data-verb="active"] button`);
	await act.click({ count: 2 });
	await act.click().catch(() => undefined);
	const activated = await waitFor(page, (id) => document.querySelector(`[data-testid="event-card"][data-event-id="${id}"]`)?.dataset.status === 'active', P, 12000);
	const statusPosts = postsTo(new RegExp(`/events/${P}/status$`));
	ok('lifecycle: a double click on ACTIVATE sends ONE POST', activated && statusPosts.length === 1, `${statusPosts.length} POST`);
	ok('lifecycle: the POST carries {to_status:active, officer}', statusPosts[0]?.body?.to_status === 'active' && statusPosts[0]?.body?.officer === OFFICER, JSON.stringify(statusPosts[0]?.body));
	const active1 = Number(await page.$eval('[data-counter="active-events"]', (e) => e.dataset.value));
	ok('lifecycle: ACTIVE EVENTS follows the server (+1)', active1 === active0 + 1, `${active0} → ${active1}`);
	// the board re-poll can re-render the card a beat after the counter moved
	await waitFor(page, (id) => !!document.querySelector(`[data-testid="event-card"][data-event-id="${id}"] [data-testid="event-outcome"]`), P, 5000);
	const outcome = await page.$eval(`[data-testid="event-card"][data-event-id="${P}"] [data-testid="event-outcome"]`, (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
	ok("lifecycle: inline 'activated by <officer>'", outcome.includes(`activated by ${OFFICER}`), outcome);
	const feedRow = await waitFor(page, (who) => [...document.querySelectorAll('[data-testid="audit-feed"] [data-prim="feed-item"]')].some((r) => r.dataset.actorKind === 'human' && r.dataset.rowId && r.textContent.includes('Event activated') && r.textContent.includes(who)), OFFICER, 10000);
	ok("lifecycle: the server's 'activated … by <officer>' row lands on the human rail", feedRow);

	// ── e-challan: double-click APPROVE → one POST (a vehicle NOT reported stolen) ──
	const approve = await buttonIn(page, `[data-testid="queue-card"][data-id="${R(3)}"]`, 'approve');
	await approve.click({ count: 2 });
	await approve.click().catch(() => undefined);
	await sleep(2500);
	const decisionPosts = postsTo(new RegExp(`/recommendations/${R(3)}/decision$`));
	ok('decision: a double click on APPROVE sends ONE POST', decisionPosts.length === 1, `${decisionPosts.length} POST · ${JSON.stringify(decisionPosts[0]?.body)}`);
	ok('decision: the POST names the officer', decisionPosts[0]?.body?.decision === 'approve' && decisionPosts[0]?.body?.officer === OFFICER);
	ok('stolen: no decision was ever sent for the stolen penal card', postsTo(new RegExp(`/recommendations/${R(1)}/decision$`)).length === 0);

	// ── C-N: the oracle's 409 is printed verbatim and never retried ─────────
	const refusedCard = `[data-testid="queue-card"][data-id="${R(4)}"]`;
	await (await buttonIn(page, refusedCard, 'approve')).click();
	const refusedShown = await waitFor(page, (sel) => !!document.querySelector(`${sel} [data-testid="decision-refusal"]`), refusedCard, 8000);
	const refusalLine = await page.$eval(`${refusedCard} [data-testid="decision-refusal"]`, (e) => e.textContent.trim()).catch(() => '');
	ok("stolen: the oracle's 409 'e-challan withheld — vehicle reported stolen' shows verbatim", refusedShown && refusalLine === 'e-challan withheld — vehicle reported stolen', refusalLine);
	ok('stolen: …the refused card no longer offers APPROVE', !(await buttonIn(page, refusedCard, 'approve')) && (await page.$eval(refusedCard, (e) => e.dataset.refused)) === '1');
	await sleep(2500);
	ok('stolen: …and the refused decision is never retried (ONE POST)', postsTo(new RegExp(`/recommendations/${R(4)}/decision$`)).length === 1, `${postsTo(new RegExp(`/recommendations/${R(4)}/decision$`)).length} POST`);

	// ── R_STOLEN_SIGHTING: AUTHORISE PURSUIT approves the newest sighting ───
	const authorise = await buttonIn(page, stolenCard, 'authorise pursuit');
	await authorise.click({ count: 2 });
	await authorise.click().catch(() => undefined);
	const authorised = await waitFor(page, (sel) => !!document.querySelector(`${sel} [data-testid="stolen-authorised"]`), stolenCard, 10000);
	const authPosts = postsTo(/\/recommendations\/[0-9a-f-]{36}\/decision$/).filter((x) => x.path.includes(R(6)) || x.path.includes(R(7)));
	ok('stolen: AUTHORISE PURSUIT sends ONE POST approving the NEWEST sighting', authPosts.length === 1 && authPosts[0].path.includes(R(7)) && authPosts[0].body?.decision === 'approve' && authPosts[0].body?.officer === OFFICER, JSON.stringify(authPosts.map((x) => [x.path.slice(-50), x.body])));
	ok("stolen: …the card then reads 'pursuit authorised by <officer>' with DISMISS only", authorised && !(await buttonIn(page, stolenCard, 'authorise pursuit')) && !!(await buttonIn(page, stolenCard, 'dismiss')) && (await page.$eval(`${stolenCard} [data-testid="stolen-authorised"]`, (e) => e.textContent)).includes(OFFICER));

	// ── escort lane: the AI PROPOSES, a named officer decides (officer approves every switch) ──
	world.events.push(eventRow(PM, 'protected_movement', 'VVIP movement — smoke escort', 'active', { movement_id: MVMT, protection_class: 'Z' }));
	world.audit[PM] = [];
	world.escort = escortDoc();
	openEscortProposal(escortProposal('esc-ops-1', 'plan_b'));
	const laneSel = '[data-testid="queue-card"][data-kind="escort-proposal"]';
	const laneShown = await waitFor(page, (s) => !!document.querySelector(`${s}[data-id="esc-ops-1"]`), laneSel, 10000);
	const lane = await page
		.$eval(`${laneSel}[data-id="esc-ops-1"]`, (e) => ({ first: e === document.querySelector('[data-testid="queue-card"]'), text: e.textContent.replace(/\s+/g, ' ') }))
		.catch(() => null);
	ok("escort lane: the escort AI's open proposal leads the /ops decision queue", laneShown && !!lane?.first, lane?.text.slice(0, 120) ?? 'no card');
	ok(
		"escort lane: it reads the route, the km, what it avoids, the closures, the decider's tier + confidence and the countdown",
		!!lane && /Plan B via Kankaria Road ·/.test(lane.text) && /4\.3 km left \(now 3\.4 km\)/.test(lane.text) && /avoids Tilak Road \(congestion 86%\)/.test(lane.text) && /closures: \+1 new · 1 lifted/.test(lane.text) && /decider · strategic tier · confidence 0\.93/.test(lane.text) && /expires in 01:\d\d/.test(lane.text),
		lane?.text.slice(0, 260),
	);
	ok('escort lane: SIMULATED FEED + EMULATED GPS provenance on the card', !!lane && /SIMULATED FEED/.test(lane.text) && /EMULATED GPS/.test(lane.text));
	// the board's next poll brings the escort's corridor event (its name)
	const laneNamed = await waitFor(page, (s) => /escort · VVIP movement — smoke escort/.test(document.querySelector(s)?.textContent ?? ''), `${laneSel}[data-id="esc-ops-1"]`, 10000);
	ok('escort lane: the escort is named by its corridor event', laneNamed);
	const laneApprove = await buttonIn(page, `${laneSel}[data-id="esc-ops-1"]`, 'approve');
	await laneApprove?.click({ count: 2 });
	await laneApprove?.click().catch(() => undefined);
	const laneReceipt = await waitFor(page, () => /approved by/.test(document.querySelector('[data-testid="escort-outcome"][data-approval-id="esc-ops-1"]')?.textContent ?? ''), null, 10000);
	await sleep(1500);
	const lanePosts = postsTo(/\/escort\/approvals\/esc-ops-1$/);
	ok(
		'escort lane: a double click on APPROVE sends ONE POST {decision, officer} to /escort/approvals/{id}',
		lanePosts.length === 1 && lanePosts[0].path === `/api/convoy/movements/${MVMT}/escort/approvals/esc-ops-1` && JSON.stringify(lanePosts[0].body) === JSON.stringify({ decision: 'approve', officer: OFFICER }),
		JSON.stringify(lanePosts.map((x) => x.body)),
	);
	const receiptText = await page.$eval('[data-testid="escort-outcome"][data-approval-id="esc-ops-1"]', (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
	ok("escort lane: the card leaves the queue; its receipt reads 'approved by <officer>'", laneReceipt && !(await page.$(`${laneSel}[data-id="esc-ops-1"]`)) && receiptText.includes(`approved by ${OFFICER}`), receiptText);
	ok('escort lane: no POST ever went to the older /escort/approve', postsTo(/\/escort\/approve$/).length === 0);
	// A stale answer: the proposal ran out meanwhile — the brain's 409 (the
	// convoy held its current plan) is shown, never retried.
	world.escortRefusals['esc-ops-2'] = { status: 'expired', detail: `proposal expired — ${ESCORT_TIMEOUT_REASON}`, reason: ESCORT_TIMEOUT_REASON };
	openEscortProposal(escortProposal('esc-ops-2', 'hold', { ttlMs: 6_000 }));
	await waitFor(page, (s) => !!document.querySelector(`${s}[data-id="esc-ops-2"]`), laneSel, 10000);
	const holdText = await page.$eval(`${laneSel}[data-id="esc-ops-2"]`, (e) => e.textContent.replace(/\s+/g, ' ')).catch(() => '');
	ok('escort lane: a hold proposal says where and for how long', /hold the convoy short of Relief Road \(congestion 86%\) until it clears · up to 180 s/.test(holdText), holdText.slice(0, 160));
	await (await buttonIn(page, `${laneSel}[data-id="esc-ops-2"]`, 'approve'))?.click();
	const staleShown = await waitFor(page, () => {
		const r = document.querySelector('[data-testid="escort-outcome"][data-approval-id="esc-ops-2"]');
		return r?.dataset.status === 'expired' && r.dataset.refused === '1';
	}, null, 10000);
	const staleText = await page.$eval('[data-testid="escort-outcome"][data-approval-id="esc-ops-2"]', (e) => ({ text: e.textContent.replace(/\s+/g, ' ').trim(), title: e.getAttribute('title') ?? '' })).catch(() => null);
	await sleep(2500);
	ok(
		"escort lane: an answer after the deadline gets the brain's 409 — 'expired — no officer response, current plan held' — sent ONCE",
		staleShown && staleText?.text.includes('expired — no officer response, current plan held') && staleText.title === `proposal expired — ${ESCORT_TIMEOUT_REASON}` && postsTo(/\/escort\/approvals\/esc-ops-2$/).length === 1,
		`${JSON.stringify(staleText)} · ${postsTo(/\/escort\/approvals\/esc-ops-2$/).length} POST`,
	);
	await page.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-escort-lane.png') });
	world.escort = null; // the escort ends; the cockpit section below starts its own

	// ── gate: RESTORE ALL is city-wide — the first click only arms it ──────
	const restoreAll = () => page.$('button:has([data-testid="gate-restore-all"])');
	await (await restoreAll()).click();
	await sleep(400);
	const armed = await page.$eval('[data-testid="gate-restore-all"]', (e) => e.dataset.armed);
	ok('gate: one click on RESTORE ALL only arms it (no POST)', armed === '1' && postsTo(/\/signals\/restore$/).length === 0);
	await (await restoreAll()).click({ count: 2 });
	await waitFor(page, () => document.querySelector('[data-testid="gate-restore-all"]')?.dataset.armed === '0', null, 6000);
	await sleep(600);
	const restorePosts = postsTo(/\/signals\/restore$/);
	ok('gate: a double click on the armed RESTORE ALL sends ONE POST {all, officer}', restorePosts.length === 1 && restorePosts[0].body?.all === true && restorePosts[0].body?.officer === OFFICER && !('event_id' in (restorePosts[0].body ?? {})), JSON.stringify(restorePosts.map((x) => x.body)));

	// ── dispatch: agency + waypoint → POST body ─────────────────────────────
	await page.select('[data-testid="dispatch-agency"]', 'police');
	await sleep(300);
	const unit = await page.$eval('[data-testid="dispatch-unit"]', (e) => e.value);
	ok('dispatch: the agency registry pre-fills its unit', unit === 'PCR-EM-01', unit);
	await waitFor(page, () => document.querySelectorAll('[data-testid="dispatch-waypoint"] option').length > 1, null, 10000);
	const wp = await page.$eval('[data-testid="dispatch-waypoint"]', (s) => ({ value: s.value, text: s.selectedOptions[0]?.textContent ?? '' }));
	ok('dispatch: the default waypoint skips a likely-passed choke #1', wp.value === '5' && /choke #2/.test(wp.text), wp.text);
	const reqBtn = await page.$('[data-testid="dispatch-request"] button:has([data-testid="dispatch-request-btn"])');
	await reqBtn.click();
	await waitFor(page, (id) => !!document.querySelector(`[data-testid="dispatch-tile"][data-dispatch-id="${id}"]`), D(9), 10000);
	const dispatchPost = postsTo(new RegExp(`/events/${P}/dispatch$`))[0];
	ok('dispatch: POST carries agency, unit and waypoint_junction', dispatchPost?.body?.agency === 'police' && dispatchPost.body.unit_id === 'PCR-EM-01' && dispatchPost.body.waypoint_junction === 5 && dispatchPost.body.requested_by === OFFICER, JSON.stringify(dispatchPost?.body));
	ok('dispatch: the new tile shows its waypoint', (await page.$eval(`[data-testid="dispatch-tile"][data-dispatch-id="${D(9)}"]`, (e) => e.dataset.waypoint).catch(() => '')) === '5');
	// Terminal dispatches older than 30 min fold behind 'history (n)'.
	const hist = await page.$eval('[data-testid="dispatch-history"]', (e) => ({ count: e.dataset.count, text: e.textContent.replace(/\s+/g, ' ').trim() })).catch(() => null);
	const tileIds = () => page.$$eval('[data-testid="dispatch-tile"]', (els) => els.map((e) => e.dataset.dispatchId));
	const before = await tileIds();
	ok("dispatch: old terminal dispatches fold behind 'history (2)'", hist?.count === '2' && hist.text === 'history (2)' && !before.includes(D(4)) && !before.includes(D(5)) && before.includes(D(6)), `${hist?.text ?? 'no toggle'} · tiles ${before.length}`);
	await page.click('[data-testid="dispatch-history"]');
	await sleep(300);
	const after = await tileIds();
	ok('dispatch: …and unfold on demand', after.includes(D(4)) && after.includes(D(5)), `${after.length} tiles`);
	await page.click('[data-testid="dispatch-history"]');

	// ── lifecycle refusal (409) renders inline with the allowed moves ──────
	const cancel = await page.$(`[data-testid="event-card"][data-event-id="${W}"] [data-verb="cancelled"] button`);
	await cancel.click();
	await page.waitForSelector(`[data-testid="event-card"][data-event-id="${W}"] .cancel-row input`, { timeout: 8000 });
	await page.type(`[data-testid="event-card"][data-event-id="${W}"] .cancel-row input`, 'resurfacing deferred (smoke)');
	await (await page.$(`[data-testid="event-card"][data-event-id="${W}"] [data-verb="cancelled"] button`)).click();
	const refused = await waitFor(page, (id) => {
		const o = document.querySelector(`[data-testid="event-card"][data-event-id="${id}"] [data-testid="event-outcome"]`);
		return o?.dataset.ok === 'false';
	}, W, 10000);
	const refusalText = await page.$eval(`[data-testid="event-card"][data-event-id="${W}"] [data-testid="event-outcome"]`, (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
	const cancelPost = postsTo(new RegExp(`/events/${W}/status$`))[0];
	ok('lifecycle: CANCEL confirms first, carries the reason', cancelPost?.body?.to_status === 'cancelled' && cancelPost.body.note === 'resurfacing deferred (smoke)', JSON.stringify(cancelPost?.body));
	ok('lifecycle: a 409 renders inline with the moves the server allows', refused && /409/.test(refusalText) && /allowed from here/.test(refusalText), refusalText);
	// That CANCEL was about the work zone, not the selected pursuit: its echo and
	// refusal stay out of the pursuit's view and show in the city's.
	const feedTexts = () => page.$$eval('[data-testid="audit-feed"] [data-prim="feed-item"]', (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ')));
	const inEventView = (await feedTexts()).filter((t) => /Metro work zone|cancelled refused/.test(t));
	ok('audit: an echo about another event stays out of the selected event’s view', (await page.$eval('[data-testid="audit-view"]', (e) => e.dataset.view)) === 'event' && inEventView.length === 0, inEventView.join(' | ').slice(0, 120) || 'none');
	await page.click('[data-testid="audit-scope-city"]');
	const cityHas = await waitFor(page, () => document.querySelector('[data-testid="audit-view"]')?.dataset.view === 'city' && [...document.querySelectorAll('[data-testid="audit-feed"] [data-prim="feed-item"]')].some((r) => /CANCEL Metro work zone/.test(r.textContent)), null, 8000);
	ok('audit: …and shows in the city view', cityHas);
	await page.click('[data-testid="audit-scope-event"]');
	await waitFor(page, () => document.querySelector('[data-testid="audit-view"]')?.dataset.view === 'event', null, 8000);

	// ── IST: the procession's windows, board == dossier == IST ─────────────
	await (await page.$(`[data-testid="event-card"][data-event-id="${E}"] .select-btn`)).click();
	await waitFor(page, () => document.querySelectorAll('[data-testid="dossier-phase-window"]').length === 2, null, 10000);
	const ist = (s) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(Date.parse(s));
	const istDay = (s) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' }).format(Date.parse(s));
	const expected = E_PHASES.map((p) => `${istDay(p.window_start)} ${ist(p.window_start)} → ${p.window_end ? `${istDay(p.window_end)} ${ist(p.window_end)}` : 'open'}`);
	const board = await page.$$eval(`[data-testid="event-card"][data-event-id="${E}"] [data-testid="phase-window"]`, (els) => els.map((e) => e.textContent.trim()));
	const dossier = await page.$$eval('[data-testid="dossier-phase-window"]', (els) => els.map((e) => e.textContent.trim()));
	ok('IST: the dossier prints the board’s windows', JSON.stringify(board) === JSON.stringify(dossier), JSON.stringify(dossier));
	ok('IST: windows are the IST rendering (browser in America/New_York)', JSON.stringify(board) === JSON.stringify(expected), `${JSON.stringify(board)} vs ${JSON.stringify(expected)}`);
	const bandOk = await waitFor(page, () => document.querySelector('[data-testid="band-row"]')?.dataset.band === 'AMBER' && !!document.querySelector('[data-testid="band-candidate"]'), null, 12000);
	ok('band: the event log’s committed band + hysteresis candidate', bandOk, await page.$eval('[data-testid="band-row"]', (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => ''));
	ok('band: held-since from the transitioned flag', !!(await page.$('[data-testid="band-held"]')));

	// ── RESTORE is disabled while a preview fixture is selected ────────────
	await page.click('[data-testid="preview-toggle"]');
	await waitFor(page, () => !!document.querySelector('[data-testid="event-card"].fixture'), null, 10000);
	await (await page.$('[data-testid="event-card"].fixture .select-btn')).click();
	await sleep(600);
	const restoreDisabled = await page.$eval('[data-testid="gate-restore"]', (s) => s.closest('button')?.disabled ?? null);
	ok('gate card: RESTORE disabled while a preview fixture is selected', restoreDisabled === true);
	await page.click('[data-testid="preview-toggle"]');
	await waitFor(page, () => !document.querySelector('[data-testid="event-card"].fixture'), null, 10000);

	// ── emulator control plane (C-G + the synchronous fallback) ────────────
	await waitFor(page, () => Number(document.querySelector('[data-testid="emu-library-count"]')?.dataset.count) === 10, null, 15000);
	ok('emulator: the panel lists the emulator’s 10 tapes', (await page.$$eval('[data-testid="emu-use-case"] option', (os) => os.length)) === 10);
	await page.select('[data-testid="emu-use-case"]', 'rath_yatra_monsoon_ambulance');
	await sleep(300);
	const clocks = await page.$$eval('[data-testid="emu-clock"] option', (os) => os.map((o) => `${o.value}${o.disabled ? '!' : ''}`));
	ok('emulator: a flagship is capped at ×10', clocks.join(' ') === '1 10 60!', clocks.join(' '));
	const sampling = await page.$eval('[data-testid="emu-sampling"]', (e) => e.textContent.replace(/\s+/g, ' '));
	ok('emulator: the effective sampling is stated', /1 decision \/ 50 sim-s/.test(sampling), sampling.trim());
	await (await page.$('button:has([data-testid="emu-launch-btn"])')).click();
	const cg = await waitFor(page, () => {
		const s = document.querySelector('[data-testid="emu-launch-state"]');
		return s?.dataset.contract === 'c-g' && s.dataset.runId === '41';
	}, null, 8000);
	const launchPost = postsTo(/\/emulator\/runs$/)[0];
	ok('emulator: LAUNCH posts {use_case_id, clock_scale:10} and tracks the 202 run', cg && launchPost?.body?.use_case_id === 'rath_yatra_monsoon_ambulance' && launchPost.body.clock_scale === 10, JSON.stringify(launchPost?.body));
	const progressed = await waitFor(page, () => Number(document.querySelector('[data-testid="emu-launch-state"] [role="progressbar"]')?.getAttribute('aria-valuenow') ?? 0) > 0, null, 12000);
	ok('emulator: C-G progress is shown', progressed && world.runGets > 0);
	const pendingShown = await waitFor(page, () => !!document.querySelector('[data-testid="emu-event-pending"]'), null, 8000);
	const pendingText = await page.$eval('[data-testid="emu-event-pending"]', (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
	ok("emulator: a run whose event is still 'planning' says so", pendingShown && pendingText === 'corridor event not live yet — activation retried every 5 s', pendingText);
	await clickWhenShown(page, '[data-testid="emu-run"][data-run-id="41"] button:has([data-testid="emu-stop"])');
	const stopped = await waitFor(page, () => document.querySelector('[data-testid="emu-run"][data-run-id="41"]')?.dataset.status === 'stopped', null, 10000);
	ok('emulator: STOP ends the run (one POST)', stopped && postsTo(/\/runs\/41\/stop$/).length === 1);
	ok('emulator: progress polls skip the tape (include_tape=false)', world.runGetQueries.length > 0 && world.runGetQueries.every((q) => /include_tape=false/.test(q)), `${world.runGetQueries.length} polls · ${world.runGetQueries[0] ?? '—'}`);
	await clickWhenShown(page, '[data-testid="emu-run"][data-run-id="40"] button:has([data-testid="emu-replay"])');
	const replayed = await waitFor(page, () => document.querySelector('[data-testid="emu-launch-state"]')?.dataset.runId === '43', null, 8000);
	ok('emulator: REPLAY re-runs a recorded tape (202 tracked)', replayed && postsTo(/\/runs\/40\/replay$/).length === 1);
	// The replayed run's row trails its launch state by a runs poll: wait for it, then STOP.
	const stop43 = await clickWhenShown(page, '[data-testid="emu-run"][data-run-id="43"] button:has([data-testid="emu-stop"])');
	const stopped43 = stop43 && (await waitFor(page, () => document.querySelector('[data-testid="emu-run"][data-run-id="43"]')?.dataset.status === 'stopped', null, 10000));
	ok('emulator: the replayed run can be stopped from its row', stopped43);
	await page.select('[data-testid="emu-use-case"]', 'metro_work_zone');
	await sleep(300);
	const t1 = Date.now();
	await (await page.$('button:has([data-testid="emu-launch-btn"])')).click();
	const sync = await waitFor(page, () => {
		const s = document.querySelector('[data-testid="emu-launch-state"]');
		return s?.dataset.contract === 'sync' && s.dataset.phase === 'running';
	}, null, 6000);
	ok('emulator: a synchronous build’s held POST is tracked from the ledger, not awaited', sync && Date.now() - t1 < 3500, `${Date.now() - t1} ms`);
	const bound = await waitFor(page, () => {
		const s = document.querySelector('[data-testid="emu-launch-state"]');
		return s?.dataset.contract === 'sync' && s.dataset.phase === 'running' && s.dataset.runId === '44';
	}, null, 3000);
	ok('emulator: …bound to its ledger row by run id while the POST is still held (server clock skewed 10 min)', bound && Date.now() - t1 < 3500, `${Date.now() - t1} ms`);
	const syncDone = await waitFor(page, () => document.querySelector('[data-testid="emu-launch-state"]')?.dataset.phase === 'completed', null, 12000);
	ok('emulator: …and it completes when the run ends', syncDone);
	const lcSupported = await page.$eval('[data-testid="living-city"]', (e) => e.dataset.supported);
	await (await page.$('button:has([data-testid="living-city-toggle"])')).click();
	const lcRunning = await waitFor(page, () => document.querySelector('[data-testid="living-city"]')?.dataset.status === 'running', null, 8000);
	ok('emulator: LIVING CITY status + start', lcSupported === '1' && lcRunning && postsTo(/\/living-city$/)[0]?.body?.action === 'start');

	// ── pursuit opening from an anchored, approved vehicle (C6) ────────────
	const openBtn = await buttonIn(page, `[data-testid="queue-card"][data-kind="pursuit"][data-id="${V(5)}"]`, 'open pursuit');
	await openBtn.click();
	await waitFor(page, (id) => !!document.querySelector(`[data-testid="event-card"][data-event-id="${id}"]`), NEWP, 10000);
	const openPost = postsTo(/\/api\/v1\/events$/)[0];
	ok('pursuit: opens on the sighting’s segment from its approved recommendation', openPost?.body?.event_kind === 'vehicle_pursuit' && openPost.body.route_spines?.[0]?.segment_ids?.[0] === 1013 && openPost.body.metadata?.vehicle_id === V(5) && openPost.body.metadata?.recommendation_id === R(5) && openPost.body.officer === OFFICER, JSON.stringify(openPost?.body).slice(0, 220));

	// ── deep links on the open wall (hashchange) ────────────────────────────
	await page.evaluate((id) => (location.hash = `evt=${id}`), W);
	const evtSel = await waitFor(page, (id) => !!document.querySelector(`[data-testid="event-card"][data-event-id="${id}"].selected`), W, 8000);
	ok('deep link: #evt= selects the event', evtSel);
	const recSel = `[data-testid="queue-card"][data-id="${R(2)}"]`;
	await page.evaluate((id) => (location.hash = `rec=${id}`), R(2));
	ok('deep link: #rec= highlights the e-challan card', await deepHit(page, recSel));
	const dspSel = `[data-testid="queue-card"][data-kind="corridor-request"][data-id="${D(1)}"]`;
	await page.evaluate((id) => (location.hash = `dsp=${id}`), D(1));
	ok('deep link: #dsp= highlights the 108 request', await deepHit(page, dspSel));
	// A hostile id (path traversal) is never resolved: nothing is marked, the selection holds.
	await waitFor(page, () => document.querySelectorAll('.deep-link-hit').length === 0, null, 6000);
	const errs0 = pageErrors.length;
	await page.evaluate(() => (location.hash = 'rec=..%2F..%2Fetc%2Fpasswd&evt=%3Cimg%20src%3Dx%3E'));
	await sleep(1500);
	const hostileMarked = (await page.$$('.deep-link-hit')).length;
	const stillW = !!(await page.$(`[data-testid="event-card"][data-event-id="${W}"].selected`));
	ok('deep link: a hostile id is ignored (nothing marked, selection kept, no error)', hostileMarked === 0 && stillW && pageErrors.length === errs0, `${hostileMarked} marked · selected W ${stillW}`);
	await page.evaluate(() => history.replaceState(null, '', location.pathname));

	// ── clearing the officer masks again ────────────────────────────────────
	await page.click('[data-testid="duty-officer"]', { count: 3 });
	await page.keyboard.press('Backspace');
	await waitFor(page, () => !document.querySelector('[data-testid="duty-officer"]')?.value, null, 4000);
	await sleep(800);
	raw = await rawPlates(page);
	ok('masking: clearing the officer masks every mark again', raw.length === 0, raw.join(',') || 'none');
	await page.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-final.png') });

	// ── deep link on a fresh load: the card is marked once the polls render it ──
	const page2 = await hermeticPage();
	try {
		await page2.goto(`${ORIGIN}/ops#rec=${R(6)}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		ok('deep link: a fresh /ops#rec=<stolen sighting> load marks the vehicle’s card', await deepHit(page2, `[data-testid="queue-card"][data-kind="stolen-sighting"][data-id="${V(8)}"]`));
	} finally {
		await page2.close();
	}

	// ── the cockpit ('/'): an escort already running is adopted; its proposals + CLEAR ──
	if (!world.events.some((e) => e.event_id === PM)) {
		world.events.push(eventRow(PM, 'protected_movement', 'VVIP movement — smoke escort', 'active', { movement_id: MVMT, protection_class: 'Z' }));
	}
	world.audit[PM] = world.audit[PM] ?? [];
	world.escort = escortDoc();
	openEscortProposal(escortProposal('apr-smoke-1', 'reroute'));
	const page3 = await hermeticPage();
	try {
		await page3.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		const adopted = await waitFor(page3, () => document.querySelector('[data-template="convoy-hud"]')?.dataset.phase === 'escorting', null, 20000);
		ok('escort: the cockpit adopts the escort already running for the active movement', adopted);
		// The cockpit rail's EMULATED marker sits on the kind line, never inside the cut name.
		const emuMark = await page3
			.$eval(`.rail [data-event-id="${E}"] [data-testid="liverail-emulated"]`, (e) => {
				const r = e.getBoundingClientRect();
				const row = e.closest('button').getBoundingClientRect();
				return { text: e.textContent.trim(), shown: r.width > 0 && r.right <= row.right + 1 };
			})
			.catch(() => null);
		ok("cockpit: the LiveRail's EMULATED marker shows in full (outside the cut event name)", !!emuMark && emuMark.text === 'EMULATED' && emuMark.shown, JSON.stringify(emuMark));
		// Escort authority: the AI only PROPOSES — 'AI PROPOSES: <route> ·
		// decider <tier> <confidence> · expires in mm:ss', locked until a duty
		// officer is named.
		const propSel = '[data-testid="convoy-hud-proposal"]';
		const outSel = '[data-testid="convoy-hud-proposal-outcome"]';
		const propShown = await waitFor(page3, (s) => document.querySelector(s)?.dataset.approvalId === 'apr-smoke-1', propSel, 10000);
		const prop = await page3
			.$eval(propSel, (e) => ({ text: e.textContent.replace(/\s+/g, ' ').trim(), kind: e.dataset.kind, left: Number(e.dataset.expiresSec) }))
			.catch(() => null);
		ok(
			"escort: the HUD shows 'AI PROPOSES: <route, km, closures> · decider <tier> <confidence> · expires in mm:ss'",
			propShown && !!prop && prop.kind === 'reroute' && /AI PROPOSES/.test(prop.text) && /Re-route via Kankaria Road ·/.test(prop.text) && /4\.3 km left \(now 3\.4 km\)/.test(prop.text) && /closures: \+1 new · 1 lifted/.test(prop.text) && /decider · strategic tier · confidence 0\.93/.test(prop.text) && /expires in 01:\d\d/.test(prop.text) && prop.left > 60 && prop.left <= 90,
			prop?.text.slice(0, 260),
		);
		ok("escort: a confident decider's own sentence is not printed twice (the decider line + authority note say it)", !(await page3.$('[data-testid="convoy-hud-proposal-why"]')));
		const lockedBefore = await page3.$$eval(`${propSel} .ap-actions button`, (bs) => bs.map((b) => b.disabled)).catch(() => []);
		ok('escort: APPROVE / REJECT are locked until a duty officer is named (and the HUD says so)', lockedBefore.length === 2 && lockedBefore.every(Boolean) && !!(await page3.$('[data-testid="convoy-hud-proposal-officer"]')), JSON.stringify(lockedBefore));
		ok('escort: the planner rail carries the same proposal', (await page3.$eval('[data-testid="planner-proposal"]', (e) => e.dataset.approvalId).catch(() => null)) === 'apr-smoke-1');
		ok("escort: an older brain's referred-actions list is not shown beside it", !(await page3.$('[data-testid="convoy-hud-approvals"]')));
		await page3.type('[data-testid="planner-officer"]', OFFICER, { delay: 4 });
		const unlocked = await waitFor(page3, (s) => {
			const bs = [...document.querySelectorAll(`${s} .ap-actions button`)];
			return bs.length === 2 && bs.every((b) => !b.disabled);
		}, propSel, 8000);
		ok('escort: …and unlock once the officer is named', unlocked);
		const hold = await page3.$eval('[data-testid="convoy-hud-hold"]', (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
		ok("escort: the HUD says 'HOLDING on <road> (<ordered by>)' — an officer's approved hold", /HOLDING on Tilak Road \(officer Duty Officer R\. Mehta\)/.test(hold), hold);
		ok('escort: SIMULATED FEED and EMULATED GPS provenance on the HUD', !!(await page3.$('[data-testid="convoy-hud-simulated"]')) && !!(await page3.$('[data-testid="convoy-hud-emulated"]')));
		await page3.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-cockpit-proposal.png') });
		// APPROVE (a double click): ONE POST; the HUD then names the officer.
		const apBtn = await page3.$(`${propSel} button:has([data-testid="convoy-hud-proposal-approve"])`);
		await apBtn?.click({ count: 2 });
		await apBtn?.click().catch(() => undefined);
		const approvedShown = await waitFor(page3, (s) => document.querySelector(s)?.dataset.status === 'approved', outSel, 10000);
		const approvedText = await page3.$eval(outSel, (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
		await sleep(1200);
		const ap1 = postsTo(/\/escort\/approvals\/apr-smoke-1$/);
		ok(
			"escort: APPROVE sends ONE POST {decision, officer}; the HUD then reads 'approved by <officer>'",
			approvedShown && ap1.length === 1 && JSON.stringify(ap1[0].body) === JSON.stringify({ decision: 'approve', officer: OFFICER }) && approvedText === `approved by ${OFFICER}`,
			`${ap1.length} POST · ${JSON.stringify(ap1[0]?.body)} · '${approvedText}'`,
		);
		// The next proposal (a hold) came from the deterministic rule (no model
		// answered): labelled RULE PROPOSES, never AI; REJECT keeps the plan.
		openEscortProposal(escortProposal('apr-smoke-2', 'hold', { rule: true }));
		await waitFor(page3, (s) => document.querySelector(s)?.dataset.approvalId === 'apr-smoke-2', propSel, 10000);
		const ruleTag = await page3.$eval('[data-testid="convoy-hud-proposal-tag"]', (e) => e.textContent.trim()).catch(() => '');
		const ruleLine = await page3.$eval('[data-testid="convoy-hud-proposal-decider"]', (e) => e.textContent.trim()).catch(() => '');
		const ruleWhy = await page3.$eval('[data-testid="convoy-hud-proposal-why"]', (e) => e.textContent.trim()).catch(() => '');
		ok(
			"escort: a rule's proposal reads 'RULE PROPOSES · deterministic rule — no model' with why a model did not answer",
			ruleTag === 'RULE PROPOSES' && ruleLine === 'deterministic rule — no model' && ruleWhy.startsWith('strategic tier unavailable'),
			`${ruleTag} · ${ruleLine} · ${ruleWhy.slice(0, 60)}`,
		);
		await sleep(600);
		const rjBtn = await page3.$(`${propSel} button:has([data-testid="convoy-hud-proposal-reject"])`);
		await rjBtn?.click({ count: 2 });
		await rjBtn?.click().catch(() => undefined);
		const rejectedShown = await waitFor(page3, (s) => document.querySelector(s)?.dataset.status === 'rejected', outSel, 10000);
		const rejectedText = await page3.$eval(outSel, (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
		await sleep(1200);
		const ap2 = postsTo(/\/escort\/approvals\/apr-smoke-2$/);
		ok(
			"escort: REJECT sends ONE POST {decision, officer}; 'rejected by <officer> — current plan kept'",
			rejectedShown && ap2.length === 1 && JSON.stringify(ap2[0].body) === JSON.stringify({ decision: 'reject', officer: OFFICER }) && rejectedText === `rejected by ${OFFICER} — current plan kept`,
			`${ap2.length} POST · '${rejectedText}'`,
		);
		// A proposal nobody answers: the countdown turns urgent, then the brain
		// expires it — the convoy holds its current approved plan.
		const p3 = escortProposal('apr-smoke-3', 'plan_b', { ttlMs: 12_000 });
		openEscortProposal(p3);
		await waitFor(page3, (s) => document.querySelector(s)?.dataset.approvalId === 'apr-smoke-3', propSel, 10000);
		const urgent = await page3.$eval(`${propSel} [data-testid="convoy-hud-proposal-countdown"]`, (e) => e.classList.contains('urgent')).catch(() => false);
		ok('escort: under 20 s the countdown turns urgent', urgent);
		setTimeout(() => closeEscortProposal('apr-smoke-3', 'expired', 'system', ESCORT_TIMEOUT_REASON), Math.max(0, Date.parse(p3.expires_at) - Date.now()));
		const expiredShown = await waitFor(page3, (s) => document.querySelector(s)?.dataset.status === 'expired', outSel, 20000);
		const expiredText = await page3.$eval(outSel, (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
		ok(
			"escort: unanswered, it expires — 'expired — no officer response, current plan held' — and nothing was sent",
			expiredShown && expiredText === 'expired — no officer response, current plan held' && postsTo(/\/escort\/approvals\/apr-smoke-3$/).length === 0,
			`'${expiredText}'`,
		);
		ok('escort: every answer went to /escort/approvals/{id} (never the older /escort/approve)', postsTo(/\/escort\/approve$/).length === 0);
		await page3.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-cockpit-expired.png') });
		// The convoy arrives: the brain reports the escort completed.
		world.escort = { ...world.escort, status: 'completed', hold: null, pending_approval: null, pending_approvals: [], progress: 1, updated_at: new Date().toISOString() };
		const ended = await waitFor(page3, () => document.querySelector('[data-template="convoy-hud"]')?.dataset.ended === 'completed', null, 10000);
		ok('escort: the HUD follows the brain to COMPLETED', ended);
		const clearBtn = await page3.$('button:has([data-testid="planner-clear"])');
		ok('escort: CLEAR is offered once the escort ended', !!clearBtn);
		if (clearBtn) {
			await clearBtn.click({ count: 2 });
			await clearBtn.click().catch(() => undefined);
		}
		const cleared = await waitFor(page3, () => document.querySelector('[data-testid="planner-clear-outcome"]')?.dataset.toStatus === 'completed', null, 10000);
		const clearPosts = postsTo(new RegExp(`/movements/${MVMT}/clear$`));
		ok('escort: CLEAR sends ONE POST {officer} and shows the event’s to_status', cleared && clearPosts.length === 1 && JSON.stringify(clearPosts[0].body) === JSON.stringify({ officer: OFFICER }), `${clearPosts.length} POST · ${JSON.stringify(clearPosts[0]?.body)}`);
		await page3.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-cockpit-escort.png') });
	} finally {
		await page3.close();
	}

	// ── the planner ('/'): a labelled movement's TEST pill; a refused escort ──
	world.escort = null;
	for (const ev of world.events) if (ev.event_id === PM) ev.status = 'completed'; // the CLEAR closed it
	world.events.push(
		eventRow(TPM, 'protected_movement', `${PLAN_LABEL} · Z protected movement · Relief Road → Tilak Road`, 'planning', {
			protection_class: 'Z',
			metadata: { source: 'convoy-brain', label: PLAN_LABEL },
		}),
	);
	world.audit[TPM] = [];
	const page4 = await hermeticPage();
	try {
		await page4.goto(`${ORIGIN}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		const pillSel = `.rail [data-event-id="${TPM}"] [data-testid="liverail-test"]`;
		await waitFor(page4, (sel) => !!document.querySelector(sel), pillSel, 20000);
		const pill = await page4
			.$eval(pillSel, (e) => {
				const r = e.getBoundingClientRect();
				const row = e.closest('button').getBoundingClientRect();
				return { text: e.textContent.trim(), title: e.getAttribute('title') ?? '', shown: r.width > 0 && r.right <= row.right + 1 };
			})
			.catch(() => null);
		ok('planner: a labelled (test / rehearsal) movement wears a TEST pill on the LiveRail', !!pill && pill.text === 'TEST' && pill.shown && pill.title.includes(PLAN_LABEL), JSON.stringify(pill));
		const pills = await page4.$$eval('.rail [data-testid="liverail-test"]', (els) => els.length);
		ok('planner: …and only that row (an unlabelled event wears none)', pills === 1, `${pills} TEST pill(s)`);
		await page4.type('[data-testid="planner-label"]', PLAN_LABEL, { delay: 2 });
		await page4.type('[data-testid="planner-officer"]', OFFICER, { delay: 2 });
		const planBtn = await buttonIn(page4, '.form', 'plan corridor');
		await planBtn?.click();
		const planned = await waitFor(page4, () => !!document.querySelector('[data-testid="planner-candidates"]'), null, 15000);
		const mv = postsTo(/\/api\/convoy\/movements$/);
		ok('planner: the label rides POST /movements as the movement title', planned && mv.length === 1 && mv[0].body?.title === PLAN_LABEL, JSON.stringify(mv[0]?.body ?? null));
		const startBtn = await buttonIn(page4, '.form', 'start escort');
		await startBtn?.click();
		const refused = await waitFor(
			page4,
			(want) => [...document.querySelectorAll('.form .error')].some((e) => e.textContent.includes(want)),
			'an officer closed it',
			10000,
		);
		const errText = await page4.$eval('.form .error', (e) => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => '');
		ok("planner: an escort refused 409 shows the brain's own reason, never the raw HTTP body", refused && errText === ESCORT_REFUSAL, errText.slice(0, 160));
		const esc = postsTo(new RegExp(`/movements/${PLAN_MVMT}/escort$`));
		ok('planner: ONE escort POST, the named officer on it', esc.length === 1 && esc[0].body?.officer === OFFICER, JSON.stringify(esc.map((x) => x.body)));
		await page4.screenshot({ path: path.join(ARTIFACTS, 'ops-wall-cockpit-planner.png') });
	} finally {
		await page4.close();
	}

	// ── hygiene ─────────────────────────────────────────────────────────────
	const notFound = responses.filter((r) => r.status === 404).map((r) => r.url);
	ok('hygiene: no 404 response during the run', notFound.length === 0, notFound.slice(0, 4).join(' ') || `${responses.length} responses`);
	ok('hygiene: no page error', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '));
	ok('hygiene: every API call answered by the fixture world (none reached a backend)', world.unstubbed.length === 0, world.unstubbed.slice(0, 4).join(' ') || 'none');
	ok('R8: no unexpected write attempted', world.refused.length === 0, world.refused.slice(0, 4).join(' ') || `${world.posts.length} stubbed writes`);
} finally {
	await browser.close();
	deckProc?.kill();
}

if (failures > 0) {
	console.error(`ops-wall-smoke: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   ops-wall-smoke — all assertions passed (every write stubbed, nothing reached a backend)');
process.exit(0);
