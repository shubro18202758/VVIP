/**
 * Integration Test Agent — walks the deck's same-origin proxy the way the UI
 * does and verifies that every surface it calls answers the shape the UI
 * expects, within a latency budget, on the argus stack (Ahmedabad's
 * real-road network).
 *
 * Runs headlessly using native Node.js fetch (no browser dependency). The
 * deck's operator gate is honoured: one sign-in through tests/lib/
 * operator-session.mjs (one attempt, PIN read at run time from
 * VEER_OPERATOR_PIN, else VEER_OPERATOR_PIN_FILE, else veer.env — never
 * logged), the session cookie rides on every gated call, and the session is
 * revoked at the end. With the gate on, an anonymous gated read must be
 * refused (403).
 *
 * Read-only (R8): GETs plus the pure compute POSTs the UI makes
 * (predict/flow, optimize/routes). It creates no movement, plans nothing,
 * decides nothing; nothing is skipped on an unreachable backend — a dark
 * surface is a FAIL.
 *
 * Usage:
 *   node tests/integration-agent.mjs [--base-url http://127.0.0.1:5273]
 *   (default: DECK_URL, else http://127.0.0.1:${DECK_PORT:-5273})
 */

import { deckFetch, signIn, signOut } from './lib/operator-session.mjs';

const BASE_URL = (
	process.argv.includes('--base-url')
		? process.argv[process.argv.indexOf('--base-url') + 1]
		: process.env.DECK_URL ?? `http://127.0.0.1:${process.env.DECK_PORT ?? '5273'}`
).replace(/\/+$/, '');

// Ahmedabad: the road network's extent (WP-20) and the segments it carries.
const CITY_BBOX = { min_lon: '72.3', min_lat: '22.8', max_lon: '72.9', max_lat: '23.3' };
const EXPECT_SEGMENTS = Number(process.env.EXPECT_SEGMENTS ?? 49);

// ─── Test Utilities ──────────────────────────────────────────────────────────

let passCount = 0;
let failCount = 0;
let session = { base: BASE_URL, gate: false, cookie: null };

function log(icon, msg) {
	const ts = new Date().toISOString().slice(11, 23);
	console.log(`  ${icon} [${ts}] ${msg}`);
}

async function testEndpoint(name, path, options = {}) {
	const { method = 'GET', body, expectStatus = [200], expectShape, latencyBudgetMs = 5000, anonymous = false } = options;
	const init = { method, signal: AbortSignal.timeout(Math.max(latencyBudgetMs * 4, 15000)) };
	if (body) {
		init.headers = { 'Content-Type': 'application/json' };
		init.body = JSON.stringify(body);
	}
	const start = performance.now();
	let status = 0;
	let data = null;
	try {
		const res = anonymous ? await fetch(BASE_URL + path, init) : await deckFetch(session, path, init);
		status = res.status;
		data = await res.json().catch(() => null);
	} catch (err) {
		const elapsed = (performance.now() - start).toFixed(1);
		log('FAIL', `${name} — ${err instanceof Error ? err.message : String(err)} (${elapsed}ms)`);
		failCount++;
		return null;
	}
	const elapsed = (performance.now() - start).toFixed(1);
	if (!expectStatus.includes(status)) {
		log('FAIL', `${name} — expected ${expectStatus.join('|')}, got ${status} (${elapsed}ms)`);
		failCount++;
		return data;
	}
	if (parseFloat(elapsed) > latencyBudgetMs) {
		log('WARN', `${name} — ${elapsed}ms exceeds ${latencyBudgetMs}ms budget`);
	}
	if (expectShape) {
		const verdict = expectShape(data);
		if (verdict !== true) {
			log('FAIL', `${name} — response shape mismatch${typeof verdict === 'string' ? `: ${verdict}` : ''} (${elapsed}ms)`);
			failCount++;
			return data;
		}
	}
	log('PASS', `${name} — ${status} (${elapsed}ms)`);
	passCount++;
	return data;
}

// ─── Test Scenarios ──────────────────────────────────────────────────────────

async function testDeckAndGate() {
	console.log('\n[1/6] Deck serving + operator gate');
	await testEndpoint('GET / (the cockpit)', '/', { anonymous: true, latencyBudgetMs: 3000 });
	await testEndpoint('GET /api/session', '/api/session', {
		anonymous: true,
		expectShape: (d) => (d && typeof d.gate === 'boolean') || 'no {gate}',
	});
	if (session.gate) {
		await testEndpoint('anonymous GET /api/v1/movements/active is refused (gate on)', '/api/v1/movements/active', {
			anonymous: true,
			expectStatus: [403],
		});
	} else {
		log('NOTE', 'operator gate OFF on this deck — every proxied read is anonymous');
	}
}

async function testHealthEndpoints() {
	// (the oracle's liveness is /health, outside the /api/v1 proxy prefix; its
	// proxied reads below stand for it)
	console.log('\n[2/6] Health through the proxy (convoy-brain → models, emulator)');
	await testEndpoint('GET /api/convoy/health', '/api/convoy/health', {
		latencyBudgetMs: 3000,
		expectShape: (d) =>
			(d?.status === 'ok' && d?.strategic_tier?.model_host === 'connected' && d?.tactical_tier?.llama_server === 'connected')
			|| `status=${d?.status} decider=${d?.strategic_tier?.model_host} gemma=${d?.tactical_tier?.llama_server}`,
	});
	await testEndpoint('GET /api/emulator/health', '/api/emulator/health', {
		latencyBudgetMs: 3000,
		expectShape: (d) => (d?.stack?.store && d?.stack?.valkey) || 'store/valkey not connected',
	});
}

async function testTrafficDataPipeline() {
	console.log('\n[3/6] Traffic data (traffic-oracle, the real-road network)');
	const params = new URLSearchParams(CITY_BBOX);
	await testEndpoint('GET /api/traffic/segments (city bbox)', `/api/traffic/segments?${params}`, {
		latencyBudgetMs: 3000,
		expectShape: (d) => (Array.isArray(d) && d.length === EXPECT_SEGMENTS) || `segments=${Array.isArray(d) ? d.length : 'none'} (want ${EXPECT_SEGMENTS})`,
	});
	await testEndpoint('GET /api/v1/corridor/summary', '/api/v1/corridor/summary', {
		latencyBudgetMs: 3000,
		expectShape: (d) => (Number.isInteger(d?.total_segments) && d.total_segments > 0) || `total_segments=${d?.total_segments}`,
	});
}

async function testPredictionEngine() {
	console.log('\n[4/6] Flow forecaster (POST /api/v1/predict/flow — compute)');
	await testEndpoint('POST /api/v1/predict/flow', '/api/v1/predict/flow', {
		method: 'POST',
		body: { segment_ids: [1001, 1005, 1013], horizons_min: [5, 15] },
		latencyBudgetMs: 5000,
		expectShape: (d) => (d?.predictions && Object.keys(d.predictions).length >= 1) || 'no predictions',
	});
}

async function testAnomalyDetection() {
	console.log('\n[5/6] Anomaly feed');
	await testEndpoint('GET /api/v1/anomalies/recent', '/api/v1/anomalies/recent?hours=1&limit=100', {
		latencyBudgetMs: 3000,
		expectShape: (d) => Array.isArray(d?.anomalies) || 'no anomalies[]',
	});
}

async function testRouteOptimization() {
	console.log('\n[6/6] Route optimizer (POST /api/v1/optimize/routes — compute, no movement)');
	// Lal Darwaja's Relief Road (1005) → the airport spur (1029): the Z route.
	await testEndpoint('POST /api/v1/optimize/routes', '/api/v1/optimize/routes', {
		method: 'POST',
		body: { origin_segment: 1005, destination_segment: 1029, max_candidates: 3 },
		latencyBudgetMs: 8000,
		expectShape: (d) => (Array.isArray(d?.routes) && d.routes.length >= 1 && d.routes[0].segment_ids?.length >= 1) || 'no route',
	});
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
	console.log('╔══════════════════════════════════════════════════════════════╗');
	console.log('║  Veer Integration Test Agent                                 ║');
	console.log('║  Walks the deck proxy as the UI does → verifies the stack    ║');
	console.log('╠══════════════════════════════════════════════════════════════╣');
	console.log(`║  Target: ${BASE_URL.padEnd(52)}║`);
	console.log(`║  Time:   ${new Date().toISOString().padEnd(52)}║`);
	console.log('╚══════════════════════════════════════════════════════════════╝');

	session = await signIn(BASE_URL, 'Integration agent (automated check)');
	try {
		await testDeckAndGate();
		await testHealthEndpoints();
		await testTrafficDataPipeline();
		await testPredictionEngine();
		await testAnomalyDetection();
		await testRouteOptimization();
	} finally {
		await signOut(session);
	}

	console.log('\n' + '═'.repeat(62));
	console.log(`  Results: ${passCount} passed, ${failCount} failed (operator gate ${session.gate ? 'on — signed in' : 'off'})`);
	console.log('═'.repeat(62));
	process.exit(failCount > 0 ? 1 : 0);
}

main().catch(async (err) => {
	await signOut(session);
	console.error('Integration agent crashed:', err instanceof Error ? err.message : err);
	process.exit(1);
});
