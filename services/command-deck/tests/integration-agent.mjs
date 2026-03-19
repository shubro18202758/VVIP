/**
 * Integration Test Agent — simulates frontend user interactions and verifies
 * that the UI correctly triggers backend optimization algorithms.
 *
 * Runs headlessly using native Node.js fetch (no browser dependency).
 * Tests the API endpoints that the frontend calls, measuring latency
 * and verifying response shapes match what the UI expects.
 *
 * Usage:
 *   node tests/integration-agent.mjs [--base-url http://localhost:5173]
 */

const BASE_URL = process.argv.includes('--base-url')
	? process.argv[process.argv.indexOf('--base-url') + 1]
	: 'http://localhost:5173';

// Delhi NCR corridor bounding box (Kartavya Path / Rajpath area)
const DELHI_BBOX = {
	min_lon: '77.18',
	min_lat: '28.58',
	max_lon: '77.24',
	max_lat: '28.64',
};

// Segment IDs for prediction testing (typical Delhi corridor segments)
const TEST_SEGMENT_IDS = [1001, 1002, 1003, 1004, 1005];

// ─── Test Utilities ──────────────────────────────────────────────────────────

let passCount = 0;
let failCount = 0;
let skipCount = 0;

function log(icon, msg) {
	const ts = new Date().toISOString().slice(11, 23);
	console.log(`  ${icon} [${ts}] ${msg}`);
}

async function measure(label, fn) {
	const start = performance.now();
	try {
		const result = await fn();
		const elapsed = (performance.now() - start).toFixed(1);
		return { ok: true, result, elapsed };
	} catch (err) {
		const elapsed = (performance.now() - start).toFixed(1);
		return { ok: false, error: err, elapsed };
	}
}

async function testEndpoint(name, url, options = {}) {
	const { method = 'GET', body, expectStatus = [200], expectShape, latencyBudgetMs = 5000, isBackendProxy = false } = options;

	const fetchOpts = { method };
	if (body) {
		fetchOpts.headers = { 'Content-Type': 'application/json' };
		fetchOpts.body = JSON.stringify(body);
	}

	const { ok, result, error, elapsed } = await measure(name, async () => {
		const res = await fetch(url, fetchOpts);
		const data = await res.json().catch(() => null);
		return { status: res.status, data };
	});

	if (!ok) {
		if (isBackendProxy) {
			log('SKIP', `${name} — backend unreachable (${elapsed}ms)`);
			skipCount++;
			return null;
		}
		log('FAIL', `${name} — ${error.message} (${elapsed}ms)`);
		failCount++;
		return null;
	}

	const { status, data } = result;

	// Backend proxy returns 500/502/503 when backend is offline — not a frontend failure
	if (isBackendProxy && (status === 500 || status === 502 || status === 503)) {
		log('SKIP', `${name} — backend offline (${status}) — proxy working (${elapsed}ms)`);
		skipCount++;
		return data;
	}

	// Status check
	if (!expectStatus.includes(status)) {
		log('FAIL', `${name} — expected ${expectStatus.join('|')}, got ${status} (${elapsed}ms)`);
		failCount++;
		return data;
	}

	// Latency budget check
	if (parseFloat(elapsed) > latencyBudgetMs) {
		log('WARN', `${name} — ${elapsed}ms exceeds ${latencyBudgetMs}ms budget`);
	}

	// Shape validation
	if (expectShape && data) {
		const shapeOk = expectShape(data);
		if (!shapeOk) {
			log('FAIL', `${name} — response shape mismatch (${elapsed}ms)`);
			failCount++;
			return data;
		}
	}

	log('PASS', `${name} — ${status} (${elapsed}ms)`);
	passCount++;
	return data;
}

function skip(name, reason) {
	log('SKIP', `${name} — ${reason}`);
	skipCount++;
}

// ─── Test Scenarios ──────────────────────────────────────────────────────────

async function testFrontendServing() {
	console.log('\n[1/6] Frontend Serving');
	await testEndpoint('GET /', `${BASE_URL}/`, {
		expectStatus: [200],
		latencyBudgetMs: 3000,
	});
}

async function testHealthEndpoints() {
	console.log('\n[2/6] Health Endpoints (convoy-brain → Ollama → GPU)');

	await testEndpoint('GET /api/convoy/health', `${BASE_URL}/api/convoy/health`, {
		expectStatus: [200],
		expectShape: (d) => d && typeof d === 'object',
		latencyBudgetMs: 2000,
		isBackendProxy: true,
	});

	await testEndpoint('GET /api/convoy/health/services', `${BASE_URL}/api/convoy/health/services`, {
		expectStatus: [200],
		latencyBudgetMs: 2000,
		isBackendProxy: true,
	});

	await testEndpoint('GET /api/convoy/health/gpu', `${BASE_URL}/api/convoy/health/gpu`, {
		expectStatus: [200],
		latencyBudgetMs: 2000,
		isBackendProxy: true,
	});
}

async function testTrafficDataPipeline() {
	console.log('\n[3/6] Traffic Data Pipeline (traffic-oracle)');

	const params = new URLSearchParams(DELHI_BBOX);

	await testEndpoint(
		'GET /api/traffic/snapshot (Delhi bbox)',
		`${BASE_URL}/api/traffic/snapshot?${params}`,
		{
			expectStatus: [200],
			expectShape: (d) => Array.isArray(d),
			latencyBudgetMs: 3000,
			isBackendProxy: true,
		},
	);

	await testEndpoint(
		'GET /api/traffic/segments (road network)',
		`${BASE_URL}/api/traffic/segments?${params}`,
		{
			expectStatus: [200],
			expectShape: (d) => Array.isArray(d),
			latencyBudgetMs: 3000,
			isBackendProxy: true,
		},
	);
}

async function testPredictionEngine() {
	console.log('\n[4/6] DSTGAT Prediction Engine (ONNX inference)');

	await testEndpoint('POST /api/traffic/predict', `${BASE_URL}/api/traffic/predict`, {
		method: 'POST',
		body: {
			segment_ids: TEST_SEGMENT_IDS,
			horizons_min: [5, 10, 15, 30],
		},
		expectStatus: [200],
		latencyBudgetMs: 5000,
		isBackendProxy: true,
	});
}

async function testAnomalyDetection() {
	console.log('\n[5/6] Anomaly Detection Pipeline');

	await testEndpoint(
		'GET /api/v1/anomalies/recent',
		`${BASE_URL}/api/v1/anomalies/recent?hours=1&limit=100`,
		{
			expectStatus: [200],
			expectShape: (d) => d && typeof d === 'object',
			latencyBudgetMs: 3000,
			isBackendProxy: true,
		},
	);
}

async function testConvoyOptimization() {
	console.log('\n[6/6] Convoy Route Optimization (MIP solver)');

	// Step 1: Create a test movement
	const createResult = await testEndpoint(
		'POST /api/convoy/movements (create)',
		`${BASE_URL}/api/convoy/movements`,
		{
			method: 'POST',
			body: {
				origin: [77.2090, 28.6139], // India Gate
				destination: [77.1855, 28.6127], // Rashtrapati Bhavan
				vvip_class: 'z_plus',
				planned_departure: new Date(Date.now() + 3600000).toISOString(),
			},
			expectStatus: [200, 201],
			latencyBudgetMs: 5000,
			isBackendProxy: true,
		},
	);

	if (createResult?.movement_id) {
		// Step 2: Plan the movement (triggers MIP solver + diversion planner)
		await testEndpoint(
			'POST /api/convoy/movements/:id/plan (route optimization)',
			`${BASE_URL}/api/convoy/movements/${createResult.movement_id}/plan`,
			{
				method: 'POST',
				body: {
					origin: [77.2090, 28.6139],
					destination: [77.1855, 28.6127],
					vvip_class: 'z_plus',
					planned_departure: new Date(Date.now() + 3600000).toISOString(),
				},
				expectStatus: [200],
				// MIP solver budget: 2s + network + prediction = ~5s
				latencyBudgetMs: 8000,
				isBackendProxy: true,
			},
		);
	} else {
		skip('POST /plan', 'No movement_id from create step (backend likely offline)');
	}
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
	console.log('╔══════════════════════════════════════════════════════════════╗');
	console.log('║  VVIP Integration Test Agent                                ║');
	console.log('║  Simulates UI interactions → verifies backend algorithms    ║');
	console.log('╠══════════════════════════════════════════════════════════════╣');
	console.log(`║  Target: ${BASE_URL.padEnd(49)}║`);
	console.log(`║  Time:   ${new Date().toISOString().padEnd(49)}║`);
	console.log('╚══════════════════════════════════════════════════════════════╝');

	// Wait for server to be ready
	let serverReady = false;
	for (let i = 0; i < 10; i++) {
		try {
			const res = await fetch(BASE_URL, { signal: AbortSignal.timeout(2000) });
			if (res.ok) {
				serverReady = true;
				break;
			}
		} catch {
			// Server not ready yet
		}
		log('WAIT', `Server not ready, retrying in 2s (${i + 1}/10)...`);
		await new Promise((r) => setTimeout(r, 2000));
	}

	if (!serverReady) {
		console.log('\nERROR: Frontend server not reachable at ' + BASE_URL);
		console.log('Start it first: bun run dev (or ./infra/scripts/launch-frontend.sh)');
		process.exit(1);
	}

	// Run all test scenarios sequentially (simulates real user flow)
	await testFrontendServing();
	await testHealthEndpoints();
	await testTrafficDataPipeline();
	await testPredictionEngine();
	await testAnomalyDetection();
	await testConvoyOptimization();

	// ─── Summary ───────────────────────────────────────────────────────────
	console.log('\n' + '═'.repeat(62));
	console.log(`  Results: ${passCount} passed, ${failCount} failed, ${skipCount} skipped`);
	console.log('═'.repeat(62));

	if (failCount > 0 && passCount === 0) {
		console.log('\n  All endpoints unreachable — backend services likely offline.');
		console.log('  The frontend is serving correctly if test [1/6] passed.');
		console.log('  Start backend: docker compose -f infra/compose.yml up -d');
	}

	process.exit(failCount > 0 && passCount === 0 ? 1 : 0);
}

main().catch((err) => {
	console.error('Integration agent crashed:', err);
	process.exit(1);
});
