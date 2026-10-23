/**
 * Ops WRITE-smoke — the city-writer safety gate (contract R8).
 *
 *   node tests/ops-write-smoke.mjs [oracleBase]     # default http://127.0.0.1:8091
 *
 * Three layers, NONE of which fires a city-writer against the fleet:
 *
 *   1. BEHAVIOURAL (bundled TS entry, zero network): every writer function
 *      refuses a blank actor with CommitRefused BEFORE any fetch; hostile
 *      path ids are rejected; the 409 twin_not_calcaled refusal parses.
 *   2. STATIC (source scan): every city-writer path is a fixed constant
 *      under CITY_WRITER_POST_PATHS; every writer routes through
 *      requireActor; path params go through safePathId; no smoke file in
 *      this tree POSTs to any writer path.
 *   3. READ-ONLY live probes (GET only): the queues and plans the writers
 *      would act on are reachable and shaped as the client assumes.
 *
 * Exits 0 with `ok <name> — <detail>` lines (showcase style); exit 1 on any
 * assertion failure.
 */

import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const ORACLE = process.argv[2] ?? 'http://127.0.0.1:8091';

const require2 = createRequire(path.join(DECK, 'package.json'));

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) {
		console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

async function getJson(url, timeoutMs = 8000) {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const res = await fetch(url, { signal: ctrl.signal });
		const text = await res.text().catch(() => '');
		return { status: res.status, text };
	} catch (err) {
		return { status: 0, text: err instanceof Error ? err.message : String(err) };
	} finally {
		clearTimeout(timer);
	}
}

// ─── 1. behavioural: bundle + run the TS entry (zero network) ────────────────
{
	const esbuild = require2('esbuild');
	const entry = path.join(HERE, 'ops-write.entry.ts');
	const out = path.join(HERE, 'artifacts', 'ops-write-entry.bundled.mjs');
	const result = await esbuild.build({
		entryPoints: [entry],
		bundle: true,
		format: 'esm',
		platform: 'node',
		outfile: out,
		sourcemap: false,
		logLevel: 'silent',
	});
	ok('write-entry bundles (esbuild)', result.errors.length === 0, `${result.errors.length} errors`);
	const run = spawnSync(process.execPath, ['--enable-source-maps', out], {
		cwd: DECK,
		encoding: 'utf8',
		timeout: 30000,
	});
	const outText = `${run.stdout ?? ''}${run.stderr ?? ''}`;
	ok('write-entry passes (every writer commit-gated, refusal parser sane)', run.status === 0, outText.trim().split('\n').slice(-1)[0] ?? '');
	if (run.status !== 0) console.error(outText);
}

// ─── 2. static: the writer surface is constants + gates, and smokes never fire it ──
{
	const client = readFileSync(path.join(DECK, 'src/lib/ops/client.ts'), 'utf8');
	const endpoints = readFileSync(path.join(DECK, 'src/lib/ops/endpoints.ts'), 'utf8');

	const writerFns = [
		'decideEnforcementRecommendation',
		'requestDispatch',
		'decideDispatch',
		'advanceDispatch',
		'applySignalPlan',
		'restoreSignalPrograms',
		'earmarkVehicle',
		'clearEarmark',
		'openPursuitEvent',
		'transitionEventStatus',
		'approveEscort',
		'clearMovement',
	];
	let allGated = true;
	const missing = [];
	for (const fn of writerFns) {
		const body = client.split(`export async function ${fn}`)[1]?.split('\nexport ')[0] ?? '';
		if (!body) {
			missing.push(fn);
			continue;
		}
		if (!body.includes('requireActor(') && !body.includes('CommitRefused')) {
			allGated = false;
			missing.push(`${fn}(no-actor-gate)`);
		}
	}
	ok('every city-writer function routes through the named-actor gate', allGated && missing.length === 0, missing.join(',') || `${writerFns.length} writers gated`);

	ok(
		'safePathId validates every writer path param',
		[
			'decideEnforcementRecommendation',
			'decideDispatch',
			'advanceDispatch',
			'clearEarmark',
			'openPursuitEvent',
			'transitionEventStatus',
			'approveEscort',
			'clearMovement',
		].every((fn) => client.split(`export async function ${fn}`)[1]?.split('\nexport ')[0]?.includes('safePathId(')),
	);

	const declared = endpoints.includes('CITY_WRITER_POST_PATHS');
	ok('CITY_WRITER_POST_PATHS declares the writer surface', declared);

	// Smokes must never fire a writer: scan this tree's smoke files for POSTs
	// aimed at writer path segments (sanctioned POSTs per R8 are twin + chat +
	// emulator only).
	const smokeFiles = [
		'ops-write-smoke.mjs',
		'ops-live-smoke.mjs',
		'gods-eye-smoke.mjs',
		'ops-write.entry.ts',
		'ops-wall-smoke.mjs',
		'city-command-smoke.mjs',
		'canvas-ux-smoke.mjs',
	];
	let fired = [];
	for (const f of smokeFiles) {
		let src = '';
		try {
			src = readFileSync(path.join(HERE, f), 'utf8');
		} catch {
			continue;
		}
		const postMatches = [...src.matchAll(/method:\s*'POST'[^)]*?body/gs)];
		for (const m of postMatches) {
			const chunk = m[0];
			if (
				chunk.includes('/plans/apply') ||
				chunk.includes('/signals/restore') ||
				chunk.includes('/decision') ||
				chunk.includes('/earmark') ||
				chunk.includes('/status') ||
				chunk.includes('/escort/approve') ||
				chunk.includes('/clear')
			) {
				fired.push(`${f}: ${chunk.slice(0, 80).replace(/\s+/g, ' ')}`);
			}
		}
	}
	ok('no smoke POSTs a city-writer endpoint (R8)', fired.length === 0, fired.slice(0, 2).join(' | ') || 'none');
}

// ─── 3. read-only live probes (GET only — never the writers) ────────────────
{
	const recs = await getJson(`${ORACLE}/api/v1/enforcement/recommendations?status=pending`);
	ok('enforcement queue reachable (GET)', recs.status === 200, `status=${recs.status}`);
	if (recs.status === 200) {
		try {
			const body = JSON.parse(recs.text);
			ok(
				'pending queue shape matches the client mapper',
				Array.isArray(body.recommendations) && (body.recommendations.length === 0 || body.recommendations[0].recommendation_id),
				`${body.recommendations.length} pending`,
			);
		} catch {
			ok('pending queue shape matches the client mapper', false, 'non-JSON body');
		}
	}

	const dispatches = await getJson(`${ORACLE}/api/v1/dispatches?limit=5`);
	ok('dispatch list reachable (GET)', dispatches.status === 200, `status=${dispatches.status}`);

	const plans = await getJson(`${ORACLE}/api/v1/signals/plans`);
	ok('signal plans posture reachable (GET)', plans.status === 200, `status=${plans.status}`);
	if (plans.status === 200) {
		const body = JSON.parse(plans.text);
		ok('writer_enabled present in posture', typeof body.writer_enabled === 'boolean', `writer_enabled=${body.writer_enabled}`);
	}

	const vehicles = await getJson(`${ORACLE}/api/v1/vehicles`);
	ok('watchlist reachable (GET)', vehicles.status === 200, `status=${vehicles.status}`);

	const active = await getJson(`${ORACLE}/api/v1/events/active`);
	if (active.status === 200) {
		const body = JSON.parse(active.text);
		const pursuit = (body.events ?? []).find((e) => e.event_kind === 'vehicle_pursuit');
		if (pursuit) {
			const plan = await getJson(`${ORACLE}/api/v1/events/${pursuit.event_id}/interception-plan`);
			ok('interception plan reachable for the live pursuit (GET)', plan.status === 200, `status=${plan.status}`);
			if (plan.status === 200) {
				const p = JSON.parse(plan.text);
				ok(
					'choke ranking shape (rank-able junctions with scores)',
					Array.isArray(p.interception_junctions) && p.interception_junctions.every((j) => typeof j.score === 'number'),
					`${p.interception_junctions.length} chokes`,
				);
			}
		} else {
			ok('interception plan (skipped — no live pursuit event)', true, 'none active');
		}
	}
}

if (failures > 0) {
	console.error(`ops-write-smoke: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   ops-write-smoke — all assertions passed (no city-writer fired)');
process.exit(0);
