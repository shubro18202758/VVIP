/**
 * Canvas layer unit test — the REAL draping helpers, pursuit overlay and
 * traffic-flow / camera / signal layers against a fake Cesium.
 *
 * Bundles src/lib/gods-eye/layers/draping.ts, pursuit.ts, layers/
 * traffic-flow.ts, layers/cameras.ts and layers/signal-network.ts with the
 * vendored esbuild (`$lib` aliased to src/lib; the God's-eye store stubbed:
 * a plain object the test fills; `fetch` stubbed), then asserts,
 * hermetically (no browser, no network):
 *
 *   1. draping helpers (WP-22 — overlays ON the photoreal 3D city): a draped
 *      polyline is a ground polyline (clampToGround, classificationType BOTH,
 *      the DRAPE_Z ground order, the enum values of the vendored 1.146 build
 *      when the global lacks them); markers are clamped to the terrain with
 *      the depth test off at every distance, labels nudged toward the camera;
 *      a DOT is billboard graphics with an explicit size (never a clamped
 *      `point`: Cesium pools that billboard and never resets its size); a
 *      ground ellipse has no height and no outline; a ring is a closed loop
 *      at its radius; the drape audit reads draped / clamped / floating
 *      graphics back from plain and Property-wrapped entities;
 *   2. pursuit: the 45 m sighting ring is a RING — a draped rim in the full
 *      data colour over a 15 % veil filled on the ground — so the vehicle
 *      mark and the selection halo inside stay visible; a STALE sighting's
 *      rim and veil go dim (ink); a Cesium build without Color.withAlpha /
 *      TRANSPARENT draws the rim alone — never an opaque disc; the growing
 *      rim and the head-first trace land on constant positions; the future
 *      arc marches through a time-varying dash length INSIDE its material;
 *      every pursuit graphic is draped or clamped, and the overlay's own
 *      audit() (published by the canvas as layerStats.pursuit) says so;
 *   3. traffic-flow (contract C-P): /traffic/live `emulated_observations`
 *      colour their segment while fresh, badged EMULATED · SIMULATED FEED
 *      on the pick card with the ingest plane's own reading beside it, and
 *      the hint / detail count them ('n EMULATED'); a stale showcase row
 *      hands the segment back to the ingest plane; a row's road_name names
 *      a segment the segment feed left unnamed; an oracle without the
 *      emulated plane draws exactly as before; every segment is DRAPED at
 *      the bottom of the ground order — real OSM geometry ('osm',
 *      'osm_display') solid, only 'schematic' dashed — and the layer reports
 *      floating 0;
 *   4. cameras + signal network: every marker is clamped billboard graphics
 *      (no entity carries a `point`, none carries two billboards), drape
 *      audit floating 0;
 *   5. overlay labels (layers/labels.ts): every overlay label's translucent
 *      canvas plate; a feed anomaly in the operator's words in EN / HI / GU
 *      ('Speed jump ×6', never 'speed_jump') and an unknown id humanised,
 *      never snake_case; a site label folds identical lines into one with a
 *      count ('STOLEN · NO FINE · PENDING ×2') and counts the readings left
 *      over in '+n more'.
 *
 * Run from services/command-deck:   node tests/canvas-layers.unit.mjs
 * (bundle written to tests/artifacts/, which is gitignored)
 */

import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const ARTIFACTS = path.join(HERE, 'artifacts');
mkdirSync(ARTIFACTS, { recursive: true });

const esbuild = await import(pathToFileURL(path.join(DECK, 'node_modules/esbuild/lib/main.js')).href);

// The store module needs the Svelte compiler; the layers read a few fields.
const stubStore = {
	name: 'stub-gods-eye-store',
	setup(build) {
		build.onResolve({ filter: /store\.svelte$/ }, () => ({ path: 'gods-eye-store-stub', namespace: 'stub' }));
		build.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
			contents: [
				'const feed = () => ({ status: "ok", note: null, updatedAt: Date.now(), total: 0 });',
				'export const godsEye = { segments: [], segmentsFeed: { status: "ok" }, pollSegments: async () => {},',
				'  cameras: [], camerasFeed: feed(), pollCameras: async () => {},',
				'  signals: [], signalsFeed: feed(), pollSignals: async () => {},',
				'  vehicles: [], via: () => "city" };',
				'export function feedError() { return null; }',
				'export function feedHint() { return null; }',
			].join('\n'),
			loader: 'js',
		}));
	},
};

const entry = [
	`export * from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/draping.ts'))};`,
	`export * from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/pursuit.ts'))};`,
	`export * from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/traffic-flow.ts'))};`,
	`export { camerasLayer } from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/cameras.ts'))};`,
	`export { signalNetworkLayer } from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/signal-network.ts'))};`,
	`export { godsEye } from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/store.svelte'))};`,
	`export * from ${JSON.stringify(path.join(DECK, 'src/lib/gods-eye/layers/labels.ts'))};`,
].join('\n');
const outfile = path.join(ARTIFACTS, 'canvas-layers-unit-bundle.mjs');
await esbuild.build({
	stdin: { contents: entry, resolveDir: DECK, loader: 'ts', sourcefile: 'canvas-layers-unit.entry.ts' },
	absWorkingDir: DECK,
	alias: { $lib: './src/lib' },
	bundle: true,
	platform: 'node',
	format: 'esm',
	outfile,
	logLevel: 'error',
	plugins: [stubStore],
});
const M = await import(`${pathToFileURL(outfile).href}?t=${Date.now()}`);

let failures = 0;
function check(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── the fake Cesium ─────────────────────────────────────────────────────────

function makeCesium({ withAlpha = true, transparent = true, enums = false, cartesianClass = false } = {}) {
	class Color {
		constructor(r, g, b, a = 1) {
			Object.assign(this, { r, g, b, a });
		}
		static fromCssColorString(css) {
			const c = new Color(0.5, 0.5, 0.5, 1);
			c.css = css;
			return c;
		}
	}
	if (withAlpha) {
		Color.prototype.withAlpha = function (a) {
			const c = new Color(this.r, this.g, this.b, a);
			c.name = this.name;
			return c;
		};
	}
	const named = (name, r, g, b) => Object.assign(new Color(r, g, b, 1), { name });
	Color.CYAN = named('CYAN', 0, 1, 1);
	Color.ORANGE = named('ORANGE', 1, 0.65, 0);
	Color.LIGHTGRAY = named('LIGHTGRAY', 0.83, 0.83, 0.83);
	Color.GRAY = named('GRAY', 0.5, 0.5, 0.5);
	Color.LIME = named('LIME', 0, 1, 0);
	Color.RED = named('RED', 1, 0, 0);
	Color.WHITE = named('WHITE', 1, 1, 1);
	Color.BLACK = named('BLACK', 0, 0, 0);
	if (transparent) Color.TRANSPARENT = Object.assign(new Color(0, 0, 0, 0), { name: 'TRANSPARENT' });
	const fromDegrees = (lon, lat, h = 0) => ({ lon, lat, h });
	const fromDegreesArray = (flat) => {
		const out = [];
		for (let i = 0; i < flat.length; i += 2) out.push({ lon: flat[i], lat: flat[i + 1], h: 0 });
		return out;
	};
	const Cartesian3 = cartesianClass
		? Object.assign(
				class {
					constructor(x, y, z) {
						Object.assign(this, { x, y, z });
					}
				},
				{ fromDegrees, fromDegreesArray },
			)
		: { fromDegrees, fromDegreesArray };
	return {
		Color,
		Cartesian2: class {
			constructor(x, y) {
				Object.assign(this, { x, y });
			}
		},
		Cartesian3,
		CallbackProperty: class {
			constructor(fn) {
				this.fn = fn;
			}
		},
		// Cesium's signature: the colour itself (an options object painted the trace white)
		ColorMaterialProperty: class {
			constructor(color) {
				if (color && typeof color === 'object' && 'color' in color && !('r' in color)) throw new TypeError('ColorMaterialProperty takes a Color, not options');
				this.color = color;
			}
		},
		PolylineDashMaterialProperty: class {
			constructor(opts) {
				Object.assign(this, opts, { dashed: true });
			}
		},
		PolylineOutlineMaterialProperty: class {
			constructor(opts) {
				Object.assign(this, opts, { outlined: true });
			}
		},
		DistanceDisplayCondition: class {
			constructor(near, far) {
				Object.assign(this, { near, far });
			}
		},
		...(enums
			? {
					HeightReference: { NONE: 10, CLAMP_TO_GROUND: 11, RELATIVE_TO_GROUND: 12, CLAMP_TO_TERRAIN: 13 },
					ClassificationType: { TERRAIN: 20, CESIUM_3D_TILE: 21, BOTH: 22 },
				}
			: {}),
	};
}

function makeViewer() {
	const byId = new Map();
	return {
		byId,
		entities: {
			add(e) {
				byId.set(e.id, e);
				return e;
			},
			removeById(id) {
				return byId.delete(id);
			},
			getById: (id) => byId.get(id),
		},
	};
}

// ── 1. draping helpers ───────────────────────────────────────────────────────

{
	const cesium = makeCesium();
	const material = { m: 1 };
	const line = M.drapedPolyline(cesium, { positions: [1, 2], width: 6, material, z: M.DRAPE_Z.flow });
	check(
		'drapedPolyline: a ground polyline on terrain AND 3D tiles',
		line.clampToGround === true && line.classificationType === 2 && line.zIndex === M.DRAPE_Z.flow,
		JSON.stringify({ c: line.clampToGround, k: line.classificationType, z: line.zIndex }),
	);
	check('drapedPolyline: keeps width / material / positions, drops z', line.width === 6 && line.material === material && line.positions.length === 2 && !('z' in line));
	check(
		'the 1.146 enum values back the helpers (HeightReference CLAMP_TO_TERRAIN 3, ClassificationType BOTH 2)',
		M.HEIGHT_REFERENCE.CLAMP_TO_GROUND === 1 && M.HEIGHT_REFERENCE.CLAMP_TO_TERRAIN === 3 && M.CLASSIFICATION_TYPE.BOTH === 2,
	);
	const z = M.DRAPE_Z;
	check(
		'ground order: flow < spines < RECOMMENDED < traces < dispatch < convoy < pursuit; fixtures below live',
		z.flow < z.spine && z.spine < z.recommended && z.recommended < z.trace && z.trace < z.dispatch && z.dispatch < z.convoy && z.convoy < z.pursuit && z.fixtureSpine < z.spine && z.superseded < z.spine,
	);

	const label = M.clampedLabel(cesium, { text: 'x', pixelOffset: { x: 0, y: 10 } });
	check(
		'clampedLabel: clamped to the terrain, depth test off at every distance, caller offset kept',
		label.heightReference === 3 && label.disableDepthTestDistance === Number.POSITIVE_INFINITY && label.pixelOffset.y === 10 && label.text === 'x',
	);
	check('clampedLabel: no eye offset when the build cannot construct a Cartesian3', !('eyeOffset' in label));
	const nudged = M.clampedLabel(makeCesium({ cartesianClass: true }), { text: 'y' });
	check('clampedLabel: nudged toward the camera (eye offset z < 0)', nudged.eyeOffset?.z === -M.LABEL_EYE_OFFSET_M && M.LABEL_EYE_OFFSET_M > 0);

	const dot = M.clampedDot(cesium, { pixelSize: 9, color: cesium.Color.CYAN, outlineColor: cesium.Color.BLACK, outlineWidth: 2, scaleByDistance: 'sbd' });
	check(
		'clampedDot: billboard graphics with an explicit size (pixelSize + 2·rim), clamped, depth test off',
		dot.width === 13 && dot.height === 13 && dot.heightReference === 3 && dot.disableDepthTestDistance === Number.POSITIVE_INFINITY,
		JSON.stringify({ w: dot.width, h: dot.height, r: dot.heightReference }),
	);
	check('clampedDot: no point vocabulary leaks onto the billboard, extra options kept', !('pixelSize' in dot) && !('color' in dot) && dot.scaleByDistance === 'sbd');
	const pulsing = M.clampedDot(cesium, { pixelSize: 10, color: 'orange', pulse: 'pulse-prop' });
	check('clampedDot: a pulse drives the billboard scale', pulsing.scale === 'pulse-prop' && pulsing.width === 10);

	const keyed = makeCesium({ enums: true });
	const kl = M.drapedPolyline(keyed, { positions: [], width: 1, material: null, z: 0 });
	const kd = M.clampedDot(keyed, { pixelSize: 4, color: 'red' });
	check('the global enums win when the build has them', kl.classificationType === 22 && kd.heightReference === 13);

	const veil = M.drapedEllipse(cesium, { radiusM: 45, material: 'veil', z: M.DRAPE_Z.pursuit });
	check(
		'drapedEllipse: a ground fill (no height, no outline), classified BOTH',
		veil.semiMajorAxis === 45 && veil.semiMinorAxis === 45 && veil.fill === true && veil.outline === false && !('height' in veil) && veil.classificationType === 2,
	);
	const zone = M.drapedPolygon(cesium, { positions: [1, 2, 3], material: 'm', z: M.DRAPE_Z.zone });
	check('drapedPolygon: a ground fill (no height / extrusion)', zone.hierarchy.positions.length === 3 && !('height' in zone) && !('extrudedHeight' in zone) && zone.outline === false);

	const ring = M.ringCoords(72.58, 23.02, 100, 48);
	const kx = 111_320 * Math.cos((23.02 * Math.PI) / 180);
	const radii = ring.map(([lon, lat]) => Math.hypot((lon - 72.58) * kx, (lat - 23.02) * 111_320));
	check('ringCoords: a closed loop (first vertex repeated last)', ring.length === 49 && ring[0][0] === ring[48][0] && ring[0][1] === ring[48][1]);
	check('ringCoords: every vertex at the radius', radii.every((r) => Math.abs(r - 100) < 0.5), `${Math.min(...radii).toFixed(2)}–${Math.max(...radii).toFixed(2)} m`);
	check('ringCoords: at least 8 segments', M.ringCoords(0, 0, 10, 2).length === 9);
	const dr = M.drapedRing(cesium, { lon: 72.58, lat: 23.02, radiusM: 55, width: 2, material: 'c', z: M.DRAPE_Z.dispatch });
	check('drapedRing: a draped closed polyline at ground height', dr.clampToGround === true && dr.positions.length === M.RING_SEGMENTS + 1 && dr.positions.every((p) => p.h === 0));

	// the audit, on plain fakes and on Property-wrapped values
	const prop = (v) => ({ getValue: () => v });
	const v = makeViewer();
	v.entities.add({ id: 'a', polyline: { clampToGround: prop(true) } });
	v.entities.add({ id: 'b', polyline: { clampToGround: false } });
	v.entities.add({ id: 'c', billboard: { heightReference: prop(3), disableDepthTestDistance: prop(Number.POSITIVE_INFINITY) }, label: M.clampedLabel(cesium, {}) });
	v.entities.add({ id: 'd', point: { heightReference: 0, disableDepthTestDistance: Number.POSITIVE_INFINITY } });
	v.entities.add({ id: 'e', ellipse: { height: 4 } });
	v.entities.add({ id: 'f', ellipse: M.drapedEllipse(cesium, { radiusM: 10, material: null, z: 0 }) });
	v.entities.add({ id: 'g', billboard: { heightReference: 3 } }); // clamped but depth-tested: a building can hide it
	v.entities.add({ id: 'h', polygon: { height: 0, extrudedHeight: 140 } });
	const audit = M.drapeAudit(v, ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'missing']);
	check('drapeAudit: draped / clamped / floating read back', audit.draped === 2 && audit.clamped === 1 && audit.floating === 5, JSON.stringify(audit));
}

// ── 2. the pursuit overlay ───────────────────────────────────────────────────

const PLAN = {
	eventId: 'evt-1',
	vehicleId: 'veh-1',
	latestSighting: {
		sightingId: 's-1',
		plate: 'GJ27EM9001',
		t: '2026-10-23T05:00:00Z',
		position: [72.5798, 23.0254],
		speedKmh: 40,
		heading: 90,
		source: 'emulated_anpr',
	},
	seedSegmentId: 1015,
	maxCostSec: 600,
	chokes: [
		{ rank: 1, junctionId: 15, junctionType: 'signalized', position: [72.585, 23.03], graphCostSec: 120, score: 0.9, signalControl: true },
		{ rank: 2, junctionId: 8, junctionType: 'priority', position: [72.59, 23.02], graphCostSec: 200, score: 0.7, signalControl: false },
	],
	note: null,
};
const TRACE = [
	[72.57, 23.02],
	[72.575, 23.022],
	[72.5798, 23.0254],
];

function overlayOf(cesium, opts = {}) {
	const viewer = makeViewer();
	const overlay = M.createPursuitOverlay(cesium, viewer);
	overlay.show({ plan: PLAN, trace: [], ...opts });
	return { viewer, overlay };
}

{
	const cesium = makeCesium();
	const { viewer, overlay } = overlayOf(cesium);
	const rim = viewer.byId.get('pursuit:ring')?.polyline;
	const veil = viewer.byId.get('pursuit:veil')?.ellipse;
	check('pursuit ring drawn as a draped rim + a ground veil', !!rim && !!veil && rim.clampToGround === true && rim.zIndex === M.DRAPE_Z.pursuit);
	check(
		'pursuit veil is a 15 % fill of the data colour on the ground, never an opaque disc',
		veil.fill === true && veil.outline === false && veil.material?.color?.a === M.PURSUIT_RING_FILL_ALPHA && M.PURSUIT_RING_FILL_ALPHA <= 0.2 && veil.material.color.name === 'CYAN' && veil.semiMajorAxis === M.PURSUIT_RING_RADIUS_M,
		`fill alpha ${veil.material?.color?.a}`,
	);
	check('pursuit rim keeps the full-colour data accent', rim.material?.name === 'CYAN' && rim.material?.a === 1);
	check('pursuit rim grows (a time-varying rim while it does)', rim.positions instanceof cesium.CallbackProperty);
	await sleep(M.PURSUIT_MOTION.ringMs + 120);
	const landed = viewer.byId.get('pursuit:ring')?.polyline?.positions;
	check(
		'pursuit rim lands on constant positions (a dynamic ground line would rebuild every frame)',
		Array.isArray(landed) && landed.length === M.RING_SEGMENTS + 1 && landed.every((p) => p.h === 0),
	);
	overlay.destroy();

	const stale = overlayOf(cesium, {
		detail: {
			eventId: 'evt-1',
			vehicleId: 'veh-1',
			sightingId: 's-1',
			cameraId: 'EM-CAM-03',
			sightingAgeSec: 1800,
			stale: true,
			staleAfterSec: 600,
			seedSegmentId: 1015,
			direction: null,
			chokes: [],
			note: null,
		},
	});
	const sRim = stale.viewer.byId.get('pursuit:ring').polyline;
	const sVeil = stale.viewer.byId.get('pursuit:veil').ellipse;
	check(
		'a STALE sighting ring goes dim (ink) — rim and veil',
		sRim.material?.name === 'LIGHTGRAY' && sVeil.material?.color?.name === 'LIGHTGRAY' && sVeil.material.color.a === M.PURSUIT_RING_FILL_ALPHA,
	);
	check('a STALE sighting draws no future arc', !stale.viewer.byId.has('pursuit:future'));
	stale.overlay.destroy();

	const bare = overlayOf(makeCesium({ withAlpha: false, transparent: false }));
	check('no veil colour on the build → the rim alone (no fill entity)', !bare.viewer.byId.has('pursuit:veil') && bare.viewer.byId.get('pursuit:ring')?.polyline?.clampToGround === true);
	bare.overlay.destroy();
	const clear = overlayOf(makeCesium({ withAlpha: false, transparent: true }));
	check('no withAlpha → a transparent veil, never the opaque data colour', clear.viewer.byId.get('pursuit:veil')?.ellipse?.material?.color?.a === 0);
	clear.overlay.destroy();
}

{
	// the whole cinematic: trace, comet, chokes, fan, arc — draped or clamped
	const cesium = makeCesium();
	const segments = new Map([[1016, { segmentId: 1016, roadName: 'Relief Road', coordinates: [[72.5798, 23.0254], [72.585, 23.027]] }]]);
	const detail = {
		eventId: 'evt-1',
		vehicleId: 'veh-1',
		sightingId: 's-1',
		cameraId: 'EM-CAM-03',
		sightingAgeSec: 30,
		stale: false,
		staleAfterSec: 600,
		seedSegmentId: 1015,
		direction: { basis: 'heading', againstOneway: false, onwardSegmentIds: [1016], towardsJunctionIds: [15] },
		chokes: [{ junctionId: 15, name: 'Lal Darwaja', likelyPassed: false }],
		note: null,
	};
	const { viewer, overlay } = overlayOf(cesium, { trace: TRACE, detail, segments });
	await sleep(M.PURSUIT_MOTION.chokeStaggerMs * 3);
	const sight = viewer.byId.get('pursuit:sight');
	check('sighting: a clamped dot (billboard) + a clamped label, no point', !!sight?.billboard && !sight.point && sight.billboard.heightReference === 3 && sight.label.heightReference === 3);
	const trace = viewer.byId.get('pursuit:trace')?.polyline;
	check('trace: draped on top of the ground order, drawn head-first', trace?.clampToGround === true && trace.zIndex === M.DRAPE_Z.pursuit && trace.positions instanceof cesium.CallbackProperty);
	check('trace: in the act colour (a Color handed to the material, never an options object)', trace.material?.color?.name === 'ORANGE', String(trace.material?.color?.name));
	const arc = viewer.byId.get('pursuit:future')?.polyline;
	check(
		'future arc: draped, marching through a time-varying dash length INSIDE the material',
		arc?.clampToGround === true && arc.material?.dashed === true && arc.material.dashLength instanceof cesium.CallbackProperty,
	);
	const march = arc.material.dashLength.fn();
	check('the march stays within 18–28 px (never a blink)', march >= 18 && march <= 28, march.toFixed(1));
	check('onward fan: draped dashes', viewer.byId.get('pursuit:onward-1016')?.polyline?.clampToGround === true);
	check('chokes: clamped dots + labels', ['pursuit:choke-15', 'pursuit:choke-8'].every((id) => viewer.byId.get(id)?.billboard?.heightReference === 3 && !viewer.byId.get(id)?.point));
	const comet = viewer.byId.get('pursuit:comet');
	check('comet: a clamped dot riding the tip', comet?.billboard?.heightReference === 3);
	await sleep(M.PURSUIT_MOTION.traceDrawMs + 120);
	const settled = viewer.byId.get('pursuit:trace')?.polyline?.positions;
	check('trace: constant positions once the head-first draw is done (whole trace)', Array.isArray(settled) && settled.length === TRACE.length);
	check('comet gone after its ride', !viewer.byId.has('pursuit:comet'));
	const ids = [...viewer.byId.keys()];
	const audit = M.drapeAudit(viewer, ids);
	check('every pursuit graphic is draped or clamped (floating 0)', audit.floating === 0 && audit.draped + audit.clamped === ids.length, JSON.stringify(audit));
	const own = overlay.audit();
	check(
		"the overlay's own audit (the canvas publishes it as layerStats.pursuit) matches, over exactly its entities",
		own.floating === 0 && own.draped === audit.draped && own.clamped === audit.clamped && own.draped + own.clamped === overlay.count(),
		JSON.stringify({ own, count: overlay.count() }),
	);
	overlay.destroy();
	check('destroy clears the overlay', viewer.byId.size === 0);
}

// ── 3. the traffic-flow layer ────────────────────────────────────────────────

const NOW = Date.now();
const line = (lon) => [
	[lon, 23.02],
	[lon + 0.01, 23.02],
];
M.godsEye.segments.push(
	{ segmentId: 1005, roadName: 'Relief Road', roadClass: 'primary', coordinates: line(72.58), geometryBasis: 'schematic' },
	{ segmentId: 1015, roadName: null, roadClass: 'trunk', coordinates: line(72.57), geometryBasis: 'osm_display' },
	{ segmentId: 1006, roadName: 'Ashram Road', roadClass: 'primary', coordinates: line(72.56), geometryBasis: 'osm_display' },
);

const liveRow = (sid, speed, cong, ageSec = 5) => ({
	segment_id: sid,
	speed_kmh: speed,
	congestion_idx: cong,
	timestamp_ms: NOW - ageSec * 1000,
	source: 'signal-ingress',
	data_quality: 'synthetic',
	origin: 'live',
	road_name: null,
});
const emuRow = (sid, speed, cong, ageSec = 5, road = null) => ({
	segment_id: sid,
	speed_kmh: speed,
	congestion_idx: cong,
	timestamp_ms: NOW - ageSec * 1000,
	source: 'event-emulator',
	data_quality: 'synthetic',
	origin: 'emulated',
	emulated: true,
	road_name: road,
});

async function flowWith(body) {
	globalThis.fetch = async () => ({ ok: true, json: async () => body });
	const viewer = makeViewer();
	const layer = M.trafficFlowLayer.create();
	layer.init({ cesium: makeCesium(), viewer, fetchJson: async () => ({}), notify() {} });
	layer.activate();
	for (let i = 0; i < 5; i++) await tick(); // the first poll lands
	layer.refresh(); // redraw over the landed rows
	const stats = layer.coverageStats();
	const cards = Object.fromEntries([...viewer.byId.entries()].map(([id, e]) => [id, e.description ?? '']));
	const lines = Object.fromEntries([...viewer.byId.entries()].map(([id, e]) => [id, e.polyline]));
	layer.deactivate();
	for (let i = 0; i < 5; i++) await tick();
	return { stats, cards, lines };
}

{
	// before C-P (no emulated plane): exactly the ingest picture
	const { stats, cards } = await flowWith({ observations: [liveRow(1005, 41, 0.35), liveRow(1006, 30, 0.5)] });
	check('no emulated plane: ingest rows colour their segments', stats.detail.coloured === 2 && stats.detail.emulated === 0, JSON.stringify(stats.detail));
	check('no emulated plane: no EMULATED tag anywhere', !/EMULATED/.test(stats.hint) && !Object.values(cards).some((c) => /EMULATED/.test(c)), stats.hint);
}

{
	const { stats, cards, lines } = await flowWith({
		observations: [liveRow(1005, 41, 0.35), liveRow(1006, 30, 0.5)],
		emulated_observations: [emuRow(1005, 8, 0.92), emuRow(1015, 12, 0.85, 5, 'Nehru Bridge Road'), emuRow(1006, 20, 0.7, 900)],
	});
	const c1005 = cards['seg:1005'];
	check('a fresh showcase row colours its segment (1005 at 92 %)', /8\.0 km\/h · congestion 92%/.test(c1005), c1005.slice(0, 120));
	check('…badged EMULATED · SIMULATED FEED, the ingest reading beside it', /EMULATED · SIMULATED FEED/.test(c1005) && /ingest plane: 41\.0 km\/h, 35%/.test(c1005), c1005.slice(0, 220));
	check(
		'a showcase row on a segment the ingest plane has nothing for still draws (1015)',
		/12\.0 km\/h · congestion 85%/.test(cards['seg:1015']) && !/ingest plane/.test(cards['seg:1015']),
	);
	check("road_name names a segment the segment feed left unnamed", cards['seg:1015'].startsWith('Nehru Bridge Road'), cards['seg:1015'].slice(0, 60));
	check(
		'a stale showcase row hands the segment back to the ingest plane (1006)',
		/30\.0 km\/h · congestion 50%/.test(cards['seg:1006']) && !/EMULATED/.test(cards['seg:1006']),
		cards['seg:1006'].slice(0, 140),
	);
	check('hint + detail count the EMULATED rows', stats.detail.emulated === 2 && / · 2 EMULATED/.test(stats.hint) && stats.detail.coloured === 3, `${stats.hint} · ${JSON.stringify(stats.detail)}`);
	check('every coloured row reads SIMULATED (the chip SIM tag)', stats.detail.simulated === 3);
	check('the geometry basis counts survive in the hint', stats.hint.includes('2 OSM-traced · 1 schematic (dashed)'), stats.hint);

	// draping (WP-22)
	const all = Object.values(lines);
	check(
		'every segment is DRAPED at the bottom of the ground order',
		all.length === 3 && all.every((l) => l.clampToGround === true && l.classificationType === 2 && l.zIndex === M.DRAPE_Z.flow),
	);
	check('…on ground positions (the surface supplies the height)', all.every((l) => l.positions.every((p) => p.h === 0)));
	check('only the SCHEMATIC chord is dashed (its gaps carry the casing)', lines['seg:1005'].material?.dashed === true && !!lines['seg:1005'].material.gapColor);
	check(
		'OSM geometry is SOLID: a coloured road wears the dark casing, never dashes',
		lines['seg:1006'].material?.outlined === true && lines['seg:1015'].material?.outlined === true && !lines['seg:1006'].material.dashed,
	);
	check('coloured width readable from the judge view (≥ 5 px)', lines['seg:1006'].width === M.FLOW_WIDTH.coloured && M.FLOW_WIDTH.coloured >= 5);
	check('the layer reports every segment draped, none floating', stats.detail.draped === 3 && stats.detail.floating === 0, JSON.stringify(stats.detail));
}

{
	// WP-20 serves geometry_basis 'osm' (real routing geometry): solid, never dashed
	M.godsEye.segments.push({ segmentId: 1041, roadName: 'Ashram Road (Motera Link)', roadClass: 'primary', coordinates: line(72.55), geometryBasis: 'osm' });
	const { lines, stats } = await flowWith({ observations: [liveRow(1041, 33, 0.3)] });
	check("geometry_basis 'osm' renders solid (casing outline, not dashes)", lines['seg:1041'].material?.outlined === true && !lines['seg:1041'].material.dashed);
	check('a NO DATA segment is a neutral draped hairline', lines['seg:1006'].clampToGround === true && lines['seg:1006'].width === M.FLOW_WIDTH.noData && !lines['seg:1006'].material?.dashed);
	check("'osm' counts as OSM-traced in the hint", stats.hint.includes('3 OSM-traced · 1 schematic (dashed)'), stats.hint);
	M.godsEye.segments.pop();
}

{
	// only the emulated plane answers: the layer is fed, not 'empty'
	const { stats } = await flowWith({ observations: [], emulated_observations: [emuRow(1005, 8, 0.92)] });
	check('an emulated-only answer is a fed layer, not an empty feed', stats.error === null && /1\/3 segments on the congestion ramp/.test(stats.hint), stats.hint);
}

// ── 4. cameras + the signal network ──────────────────────────────────────────

{
	M.godsEye.cameras.push(
		{ cameraId: 'EM-CAM-01', label: 'Lal Darwaja', lon: 72.58, lat: 23.02, kind: 'anpr', source: 'emulated', junctionId: 15, reads15m: 3, hits15m: 1, lastReadT: NOW, status: null },
		{ cameraId: 'EM-CAM-02', label: null, lon: 72.58, lat: 23.02, kind: 'cctv', source: 'emulated', junctionId: 15, reads15m: 0, hits15m: 0, lastReadT: null, status: null },
	);
	M.godsEye.signals.push(
		{ junctionId: 15, name: 'Lal Darwaja', lon: 72.58, lat: 23.021, phaseIndex: 1, phaseLost: false, greenSec: 30, cycleSec: 90, activePhase: 'NS', phaseMovements: ['NS'], movementStates: { NS: 'green' }, remainingSec: 12, offsetSec: 0, greenRatio: 0.33, source: 'osm_assumed' },
		{ junctionId: 16, name: null, lon: 72.59, lat: 23.03, phaseIndex: null, phaseLost: false, greenSec: null, cycleSec: null, activePhase: null, phaseMovements: [], movementStates: {}, remainingSec: null, offsetSec: null, greenRatio: null, source: null },
	);
	for (const layer of [M.camerasLayer, M.signalNetworkLayer]) {
		const viewer = makeViewer();
		const inst = layer.create();
		inst.init({ cesium: makeCesium(), viewer, fetchJson: async () => ({}), notify() {} });
		inst.activate();
		const ents = [...viewer.byId.values()];
		check(`${layer.id}: every marker is clamped billboard graphics (no entity carries a point)`, ents.length > 0 && ents.every((e) => !e.point && e.billboard?.heightReference === 3), `${ents.length} entities`);
		const stats = inst.coverageStats();
		check(`${layer.id}: drape audit floating 0`, stats.detail.floating === 0 && stats.detail.clamped === ents.length, JSON.stringify(stats.detail));
		inst.deactivate();
	}
	const viewer = makeViewer();
	const cams = M.camerasLayer.create();
	cams.init({ cesium: makeCesium(), viewer, fetchJson: async () => ({}), notify() {} });
	cams.activate();
	const hot = viewer.byId.get('cam:EM-CAM-01')?.billboard;
	check('a camera with watchlist hits pulses through the billboard scale (peak-size dot)', hot?.scale instanceof Object && typeof hot.scale.fn === 'function' && hot.scale.fn() <= 1 && hot.width >= 14);
	cams.deactivate();
}

// ── 5. overlay labels: the plate, the anomaly words, the site lines ─────────
{
	const cesium = makeCesium();
	const plate = M.labelPlate(cesium);
	check('label plate: showBackground with a canvas-colour background and padding', plate.showBackground === true && plate.backgroundColor instanceof cesium.Color && plate.backgroundPadding?.x === 4 && plate.backgroundPadding?.y === 2, JSON.stringify(plate));
	check('label plate opacity is the overlay labels\' one (0.72)', M.LABEL_PLATE_ALPHA === 0.72);
	check("anomaly: 'speed_jump' ×6 reads 'Speed jump ×6' (never the wire id)", M.anomalyLabel('speed_jump', 6) === 'Speed jump ×6', M.anomalyLabel('speed_jump', 6));
	check("anomaly: 'congestion_outlier' once reads 'Congestion outlier'", M.anomalyLabel('congestion_outlier', 1) === 'Congestion outlier');
	const vocab = M.ANOMALY_TYPES.map((t) => [t, M.anomalyWord(t, 'en'), M.anomalyWord(t, 'hi'), M.anomalyWord(t, 'gu')]);
	const raw = vocab.filter(([, ...words]) => words.some((w) => /_/.test(w) || !w));
	check('anomaly: every C-H type has a human word in en, hi and gu (no snake_case)', raw.length === 0, raw.map((r) => r[0]).join(', ') || `${vocab.length} types × 3`);
	check('anomaly: hi / gu words differ from en (localised, not copied)', vocab.every(([, en, hi, gu]) => hi !== en && gu !== en && hi !== gu));
	check("anomaly: an unknown id is humanised ('some_new_flag' → 'Some new flag')", M.anomalyWord('some_new_flag', 'hi') === 'Some new flag' && M.anomalyWord(null) === 'Feed anomaly');
	const folded = M.collapseLines(['STOLEN · NO FINE · PENDING', 'STOLEN · NO FINE · PENDING', 'SPEEDING +55 km/h · APPROVED']);
	check('site lines: identical lines fold into one with a count, first occurrence first', folded.length === 2 && folded[0].count === 2 && folded[1].count === 1 && folded[1].line.startsWith('SPEEDING'), JSON.stringify(folded));
	check(
		"site label: 'STOLEN · NO FINE · PENDING ×2' once, never the same line twice",
		M.siteLabelText(['STOLEN · NO FINE · PENDING', 'STOLEN · NO FINE · PENDING'], 3) === 'STOLEN · NO FINE · PENDING ×2',
		JSON.stringify(M.siteLabelText(['STOLEN · NO FINE · PENDING', 'STOLEN · NO FINE · PENDING'], 3)),
	);
	const many = M.siteLabelText(['A', 'B', 'B', 'C', 'D', 'D', 'E'], 3);
	check("site label: at most 3 distinct lines, '+n more' counts the readings left over", many === 'A\nB ×2\nC\n+3 more', JSON.stringify(many));
	check('site label: a single reading reads as itself', M.siteLabelText(['RED LIGHT · PENDING'], 3) === 'RED LIGHT · PENDING');
}

if (failures) {
	console.error(`\nFAIL canvas-layers — ${failures} assertion(s) failed`);
	process.exit(1);
}
console.log('ok   canvas-layers — all assertions passed');
process.exit(0);
