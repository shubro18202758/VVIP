/**
 * Canvas bus + directive validator unit test (wave 3, contract C-D2).
 *
 *   node tests/canvas-bus.unit.mjs
 *
 * esbuild bundles the REAL modules — gods-eye/directives.ts (the pure
 * validator), gods-eye/canvas-bus.svelte.ts (the queue), api/client.ts
 * (the stream dispatcher + the ack) and i18n/packs.agent.ts — with `$lib`
 * aliased to src/lib (the contract-runner pattern); `$state` is an identity
 * stub so the rune class runs as a plain class. `fetch` is a recorder: the
 * ack is the only request the bus may make.
 *
 * Asserts: an unknown URN, an off-allowlist verb, out-of-bbox coordinates
 * and oversize text are DROPPED and COUNTED by reason; valid proposals of
 * all six verbs queue; ids dedupe; entries expire after 10 minutes; a
 * structural-only offer (no canvas) is re-resolved by the canvas later;
 * settle() acks exactly once and never twice; a directive line is
 * non-terminal in the stream dispatcher; the agent pack has en/hi/gu parity.
 *
 * Exits 0 with `ok <name>` lines, 1 on any failure.
 */

import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const OUT = path.join(HERE, 'artifacts', 'canvas-bus-bundle.mjs');

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

// ─── bundle the real modules ────────────────────────────────────────────────

mkdirSync(path.dirname(OUT), { recursive: true });
const esbuild = createRequire(path.join(DECK, 'package.json'))('esbuild');
const ENTRY = `
export * as directives from './src/lib/gods-eye/directives';
export * as bus from './src/lib/gods-eye/canvas-bus.svelte';
export * as client from './src/lib/api/client';
export * as agentPack from './src/lib/i18n/packs.agent';
export { t } from './src/lib/i18n/strings';
`;
const built = await esbuild.build({
	stdin: { contents: ENTRY, resolveDir: DECK, loader: 'ts', sourcefile: 'canvas-bus-entry.ts' },
	absWorkingDir: DECK,
	alias: { $lib: './src/lib' },
	bundle: true,
	format: 'esm',
	platform: 'node',
	outfile: OUT,
	logLevel: 'silent',
});
ok('bundle builds (esbuild, $lib aliased)', built.errors.length === 0, `${built.errors.length} errors`);

globalThis.$state = (v) => v;
const posts = [];
globalThis.fetch = async (url, init = {}) => {
	posts.push({ url: String(url), method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : null });
	return new Response(JSON.stringify({ directive_id: 'x', decision: 'applied', recorded_in: 'decisions_log' }), {
		status: 200,
		headers: { 'content-type': 'application/json' },
	});
};

const { directives, bus, client, agentPack, t } = await import(pathToFileURL(OUT).href + `?t=${Date.now()}`);
const { canvasBus } = bus;

// ─── a canvas world: one vehicle, one camera, segment 1012, junction 3 ──────

const VEHICLE = 'vehicle:1b94e9d2-7e11-4174-a735-7dd3be3189cf';
const world = {
	hasUrn: (urn) => urn === VEHICLE || urn === 'camera:EM-CAM-02',
	hasSegment: (id) => id === 1012,
	hasJunction: (id) => id === 3,
	layerIds: ['events', 'flow', 'cameras', 'units'],
};
const provenance = { session_id: 'sess-1', tier: 'heuristic', confidence: null, tool_call_id: 'call_1_city_query_entities' };
let n = 0;
const d = (verb, args, extra = {}) => ({ id: `dir_${++n}`, verb, args, label: `${verb} test`, provenance, ...extra });

// ─── the validator alone ─────────────────────────────────────────────────────

{
	const v = directives.validateDirective;
	const track = v(d('canvas_track_entity', { urn: VEHICLE }), world);
	ok('track: a registry URN is accepted', track.ok && track.directive.args.urn === VEHICLE);
	ok('provenance shaped', track.ok && track.directive.sessionId === 'sess-1' && track.directive.tier === 'heuristic');
	ok('a segment of the canvas network resolves', v(d('canvas_fly_to', { urn: 'segment:1012' }), world).ok);
	ok('a junction of the canvas network resolves', v(d('canvas_annotate', { urn: 'junction:3', text: 'choke 1' }), world).ok);
	const unknown = v(d('canvas_track_entity', { urn: 'vehicle:00000000-0000-4000-8000-000000000000' }), world);
	ok('an unknown URN is refused (urn)', !unknown.ok && unknown.reason === 'urn');
	ok('a malformed URN is refused (urn)', v(d('canvas_track_entity', { urn: 'person:42' }), world).reason === 'urn');
	ok('an off-allowlist verb is refused (verb)', v(d('approve_recommendation', { urn: VEHICLE }), world).reason === 'verb');
	ok('a point outside the city network is refused (bbox)', v(d('canvas_fly_to', { lon: 77.2, lat: 28.6 }), world).reason === 'bbox');
	// SP Ring Road (West) at lon 72.485 lies west of the CITY_BBOX boot view but on the
	// road network: the brain accepts it (CANVAS_BBOX), so the canvas must too
	ok('a point on SP Ring Road (West) is accepted', v(d('canvas_fly_to', { lon: 72.485, lat: 23.03 }), world).ok);
	ok('a point inside the city is accepted', v(d('canvas_fly_to', { lon: 72.58, lat: 23.03, range_m: 5 }), world).directive?.args.rangeM === 100);
	ok('a note over 140 characters is refused (text)', v(d('canvas_annotate', { urn: VEHICLE, text: 'x'.repeat(141) }), world).reason === 'text');
	ok('a 140-character note is accepted', v(d('canvas_annotate', { urn: VEHICLE, text: 'x'.repeat(140) }), world).ok);
	ok('an unknown sensor is refused (args)', v(d('canvas_set_sensor', { mode: 'thermal' }), world).reason === 'args');
	ok('a layer not on this canvas is refused (args)', v(d('canvas_toggle_layer', { layer_id: 'faces', on: true }), world).reason === 'args');
	ok('highlight: one unknown URN refuses the whole set', v(d('canvas_highlight', { urns: [VEHICLE, 'camera:NOPE'] }), world).reason === 'urn');
	ok('highlight caps at 20', v(d('canvas_highlight', { urns: Array.from({ length: 21 }, () => VEHICLE) }), world).reason === 'args');
	ok('structure-only (no canvas): a well-formed URN passes', v(d('canvas_track_entity', { urn: 'vehicle:00000000-0000-4000-8000-000000000000' }), null).ok);
	ok('structure-only still refuses the bbox', v(d('canvas_fly_to', { lon: 0, lat: 0 }), null).reason === 'bbox');
	ok('a directive without an id is malformed', v({ verb: 'canvas_fly_to', args: {} }, world).reason === 'malformed');
}

// ─── the bus: drops are counted by reason ────────────────────────────────────

{
	const offers = [
		d('canvas_track_entity', { urn: 'vehicle:00000000-0000-4000-8000-000000000000' }), // urn
		d('dispatch_request', { unit: 'JR-1' }), // verb
		d('canvas_fly_to', { lon: 72.9, lat: 23.0 }), // bbox (east of the road network)
		d('canvas_annotate', { urn: VEHICLE, text: 'y'.repeat(200) }), // text
	];
	for (const o of offers) canvasBus.offer(o, world);
	const tally = canvasBus.dropped;
	ok('unknown URN dropped + counted', tally.urn === 1, JSON.stringify(tally));
	ok('off-allowlist verb dropped + counted', tally.verb === 1);
	ok('out-of-bbox coordinates dropped + counted', tally.bbox === 1);
	ok('oversize text dropped + counted', tally.text === 1);
	ok('total drop count', tally.total === 4);
	ok('nothing dropped is queued', canvasBus.pending.length === 0);
	ok('every drop is logged with its reason', canvasBus.log.filter((e) => e.decision === 'dropped').length === 4);
}

// ─── the bus: valid proposals queue, dedupe, expire, settle once ─────────────

{
	const all = [
		d('canvas_fly_to', { urn: VEHICLE }),
		d('canvas_track_entity', { urn: VEHICLE }),
		d('canvas_highlight', { urns: [VEHICLE, 'camera:EM-CAM-02'] }),
		d('canvas_annotate', { urn: 'junction:3', text: 'choke', severity: 'warn' }),
		d('canvas_set_sensor', { mode: 'flir-iron' }),
		d('canvas_toggle_layer', { layer_id: 'cameras', on: false }),
	];
	for (const o of all) canvasBus.offer(o, world);
	ok('all six verbs queue', canvasBus.pending.length === 6, canvasBus.pending.map((x) => x.verb).join(','));
	canvasBus.offer(all[0], world);
	ok('a repeated id is not queued twice', canvasBus.pending.length === 6);

	const first = canvasBus.pending[0];
	ok('settle applies once', canvasBus.settle(first.id, 'applied', 'Duty Officer Test') === true);
	ok('a second settle finds nothing', canvasBus.settle(first.id, 'dismissed') === false);
	await new Promise((r) => setTimeout(r, 10));
	const acks = posts.filter((p) => p.url.endsWith('/chat/directives/ack'));
	ok('exactly one ack posted', acks.length === 1, `${acks.length} acks`);
	ok('ack body: session, directive, decision, officer', acks[0]?.body?.session_id === 'sess-1' && acks[0].body.directive_id === first.id && acks[0].body.decision === 'applied' && acks[0].body.officer === 'Duty Officer Test');
	ok('ack goes through the deck proxy', acks[0]?.url === '/api/convoy/chat/directives/ack' && acks[0].method === 'POST');
	ok('the log records the applied ack', canvasBus.log.find((e) => e.id === first.id)?.ack === 'recorded');

	const second = canvasBus.pending[0];
	canvasBus.settle(second.id, 'dismissed');
	await new Promise((r) => setTimeout(r, 10));
	ok('dismiss acks dismissed', posts.filter((p) => p.url.endsWith('/ack')).at(-1)?.body.decision === 'dismissed');
	ok('settled directives leave the queue', !canvasBus.get(first.id) && !canvasBus.get(second.id) && canvasBus.pending.length === 4);

	const later = Date.now() + directives.DIRECTIVE_TTL_MS + 1;
	const expired = canvasBus.expire(later);
	ok('entries expire after 10 minutes (counted)', expired === 4 && canvasBus.pending.length === 0 && canvasBus.dropped.expired === 4);
	ok('the only requests were the two acks', posts.length === 2 && posts.every((p) => p.method === 'POST' && p.url.endsWith('/ack')));
}

// ─── structural offers from the console are resolved by the canvas ──────────

{
	canvasBus.offer(d('canvas_track_entity', { urn: VEHICLE }), null);
	canvasBus.offer(d('canvas_track_entity', { urn: 'vehicle:00000000-0000-4000-8000-000000000000' }), null);
	ok('console offers queue on structure alone', canvasBus.pending.length === 2);
	const before = canvasBus.dropped.urn;
	const removed = canvasBus.resolve(world);
	ok('the canvas drops what it cannot resolve', removed === 1 && canvasBus.pending.length === 1 && canvasBus.dropped.urn === before + 1);
	canvasBus.settle(canvasBus.pending[0].id, 'dismissed');

	canvasBus.requestSelection(VEHICLE);
	const taken = canvasBus.takeSelection();
	ok('a cited selection is taken once', taken?.urn === VEHICLE && canvasBus.takeSelection() === null);
	ok('a citation a human clicked is a human pick (source chat, not agent)', taken?.source === 'chat');
	canvasBus.requestSelection(VEHICLE, 'chat', Date.now() - 10 * 60 * 1000);
	ok('a stale selection request is not served', canvasBus.takeSelection() === null);
	// seen ids are remembered for the TTL only (the page may run for days)
	const later = Date.now() + 11 * 60 * 1000;
	canvasBus.expire(later);
	ok('seen directive ids are forgotten after the TTL', canvasBus.seenCount === 0, String(canvasBus.seenCount));
}

// ─── the stream dispatcher: directive lines are non-terminal ────────────────

{
	const seen = [];
	const cb = {
		onToken: () => {},
		onThought: () => {},
		onToolCall: () => {},
		onToolResult: () => {},
		onDone: (x) => seen.push(['done', x]),
		onError: () => {},
		onDirective: (x) => seen.push(['directive', x]),
	};
	const line = { type: 'directive', data: { id: 'dir_abc', verb: 'canvas_track_entity', args: { urn: VEHICLE }, label: 'track GJ27EM•••1', provenance } };
	const end = client.dispatchChatEvent(line, cb);
	ok('a directive is non-terminal', end === null);
	ok('a directive reaches onDirective shaped', seen[0]?.[0] === 'directive' && seen[0][1].provenance.sessionId === 'sess-1' && seen[0][1].provenance.toolCallId === 'call_1_city_query_entities');
	const noCb = { ...cb, onDirective: undefined };
	ok('a consumer without onDirective ignores it', client.dispatchChatEvent(line, noCb) === null);
	client.dispatchChatEvent({ type: 'done', data: { session_id: 's', tiers_used: [], operator_alerts: [], degraded: false, citations: [{ urn: VEHICLE, label: 'GJ27EM•••1' }, { bad: 1 }] } }, cb);
	const done = seen.find((x) => x[0] === 'done')?.[1];
	ok('done.citations parsed (malformed rows skipped)', done?.citations.length === 1 && done.citations[0].urn === VEHICLE);
	client.dispatchChatEvent({ type: 'done', data: { session_id: 's' } }, cb);
	ok('an older brain: citations default to []', Array.isArray(seen.at(-1)[1].citations) && seen.at(-1)[1].citations.length === 0);
}

// ─── i18n: the agent pack has en/hi/gu parity ────────────────────────────────

{
	const { en, hi, gu } = agentPack.AGENT_PACKS;
	const keys = Object.keys(en).sort();
	ok('hi has every en key', JSON.stringify(Object.keys(hi).sort()) === JSON.stringify(keys));
	ok('gu has every en key', JSON.stringify(Object.keys(gu).sort()) === JSON.stringify(keys));
	ok('registered: t() resolves hi', t('hi', 'agent_apply') === 'लागू करें' && t('gu', 'agent_apply') === 'લાગુ કરો');
	ok('function strings render', t('en', 'agent_more', 3) === '+3 more');
}

if (failures > 0) {
	console.error(`\n${failures} canvas-bus failures`);
	process.exit(1);
}
console.log('\nall canvas-bus assertions passed');
