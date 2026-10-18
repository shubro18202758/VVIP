/**
 * Ops WRITE-smoke entry — bundled with esbuild and executed by node via
 * tests/ops-write-smoke.mjs (node cannot import .ts directly; esbuild is
 * vendored in node_modules as a vite dependency — the ops-guard.entry.ts
 * pattern).
 *
 * Asserts the city-writer contract R8 WITHOUT any network:
 *   - every writer function REFUSES a blank/unnamed actor (CommitRefused)
 *     before any fetch is attempted (C6: no automated enforcement);
 *   - the 409 fail-closed refusal parser reads the oracle's detail shape
 *     (the twin_not_calibrated fixture the gate card renders inline);
 *   - path params are safePathId-validated (invalid ids throw before fetch).
 */

import {
	CommitRefused,
	advanceDispatch,
	applySignalPlan,
	clearEarmark,
	decideDispatch,
	decideEnforcementRecommendation,
	earmarkVehicle,
	openPursuitEvent,
	parseSignalRefusal,
	requestDispatch,
	restoreSignalPrograms,
} from '../src/lib/ops/client';

let failures = 0;
function ok(name: string, cond: boolean, detail = '') {
	if (cond) {
		console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

async function refuses(name: string, fn: () => Promise<unknown>) {
	try {
		await fn();
		ok(name, false, 'NO refusal — the writer fired without a named officer');
	} catch (err) {
		ok(name, err instanceof CommitRefused, err?.constructor?.name ?? String(err));
	}
}

// ── 1. every city-writer refuses a blank actor BEFORE any fetch ─────────────
await refuses('e-challan decision refuses blank officer', () =>
	decideEnforcementRecommendation('e8dae008-f923-4e26-be84-c9ed13f11f80', {
		decision: 'approve',
		officer: '',
	}),
);
await refuses('e-challan decision refuses whitespace officer', () =>
	decideEnforcementRecommendation('e8dae008-f923-4e26-be84-c9ed13f11f80', {
		decision: 'reject',
		officer: '   ',
	}),
);
await refuses('dispatch request refuses blank requested_by', () =>
	requestDispatch('3ef08290-66d8-511b-a25a-3d2967c79985', {
		agency: 'jan_rakshak',
		unitId: 'JR-EM-01',
		requestedBy: '',
	}),
);
await refuses('dispatch decision refuses blank officer', () =>
	decideDispatch('fdfb461c-b7bf-4f2f-aee2-e5741c684259', { action: 'approve', officer: '' }),
);
await refuses('dispatch status refuses blank officer', () =>
	advanceDispatch('fdfb461c-b7bf-4f2f-aee2-e5741c684259', { status: 'en_route', officer: '' }),
);
await refuses('signal plan APPLY refuses blank actor', () => applySignalPlan({ band: 'RED' }, ''));
await refuses('signal RESTORE refuses blank actor', () => restoreSignalPrograms({ all: true }, ' x'));
await refuses('vehicle earmark refuses blank actor', () =>
	earmarkVehicle('GJ27EM9001', {
		state: 'flagged',
		reason: 'stolen vehicle — FIR on file',
		lawfulBasis: 'stolen_vehicle_fir',
		earmarkedBy: '',
	}),
);
await refuses('vehicle earmark refuses short reason (min 3)', () =>
	earmarkVehicle('GJ27EM9001', {
		state: 'watch',
		reason: 'x',
		lawfulBasis: 'mva_1988_s183',
		earmarkedBy: 'Duty Officer R. Mehta',
	}),
);
await refuses('earmark clear refuses blank actor', () => clearEarmark('9a030e3e-8429-4444-8a7d-534267345748', ''));
await refuses('pursuit open refuses blank actor', () =>
	openPursuitEvent({
		vehicleId: '9a030e3e-8429-4444-8a7d-534267345748',
		plate: 'GJ27EM9001',
		seedSegmentId: 1005,
		actor: ' ',
	}),
);

// ── 2. path params validated (safePathId) before any fetch ──────────────────
try {
	await decideEnforcementRecommendation('../etc/passwd', { decision: 'approve', officer: 'Officer X' });
	ok('hostile recommendation id rejected', false, 'traversal id accepted');
} catch (err) {
	ok(
		'hostile recommendation id rejected',
		!(err instanceof CommitRefused) && !(err instanceof TypeError),
		`${(err as Error).constructor.name}: ${String((err as Error).message).slice(0, 60)}`,
	);
}
try {
	await clearEarmark('9a030e3e 8429 (drop table)', 'Officer X');
	ok('hostile vehicle id rejected', false, 'id with spaces accepted');
} catch (err) {
	ok('hostile vehicle id rejected', true, (err as Error).constructor.name);
}

// ── 3. the 409 fail-closed refusal parser (the gate card's inline render) ───
const twinRefusal = JSON.stringify({
	detail: {
		applied: false,
		band: 'RED',
		reason: 'twin_not_calibrated',
		detail: 'candidate policy REFUSED: the corridor twin has no calibrated run for this scenario',
		planned_junctions: [],
	},
});
const parsed = parseSignalRefusal(twinRefusal);
ok('409 refusal reason parsed', parsed.reason === 'twin_not_calibrated', `reason=${parsed.reason}`);
ok('409 refusal detail preserved for the expander', parsed.detail?.['reason'] === 'twin_not_calibrated');
const stringDetail = parseSignalRefusal(JSON.stringify({ detail: 'writer disabled by policy' }));
ok('string detail refusal parsed', stringDetail.reason === 'writer disabled by policy');
const garbage = parseSignalRefusal('not-json<<');
ok('garbage body degrades to raw text', typeof garbage.reason === 'string' && garbage.reason.length > 0);

if (failures > 0) {
	console.error(`ops-write-entry: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   ops-write-entry — all writer-gate assertions passed');
process.exit(0);
