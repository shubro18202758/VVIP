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
 *   - path params are safePathId-validated (invalid ids throw before fetch);
 *   - an officer name carrying a bidirectional override / isolate control
 *     (U+202A–U+202E, U+2066–U+2069) is refused like a blank one, while
 *     ZWJ / ZWNJ (Indic names) stay legal;
 *   - the C-N 409 ('e-challan withheld — vehicle reported stolen') parses to
 *     the oracle's verbatim sentence.
 */

import {
	BIDI_CONTROL_RE,
	CommitRefused,
	advanceDispatch,
	applySignalPlan,
	approveEscort,
	clearEarmark,
	clearMovement,
	decideDispatch,
	decideEnforcementRecommendation,
	earmarkVehicle,
	openPursuitEvent,
	parseDecisionRefusal,
	parseSignalRefusal,
	requestDispatch,
	restoreSignalPrograms,
	transitionEventStatus,
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

await refuses('event lifecycle (C-F) refuses blank officer', () =>
	transitionEventStatus('4508d86d-566b-438f-8414-b3d67fd7e6e4', { toStatus: 'active', officer: '' }),
);
await refuses('escort approval refuses blank officer', () =>
	approveEscort('9775233f-ddb0-4c6b-b9c1-16e98fc760ef', { approval_id: 'ap-1', decision: 'approve', officer: ' ' }),
);
await refuses('escort approval (reject) refuses blank officer', () =>
	approveEscort('9775233f-ddb0-4c6b-b9c1-16e98fc760ef', { approval_id: 'ap-1', decision: 'reject', officer: '' }),
);
await refuses('movement CLEAR refuses blank officer', () => clearMovement('9775233f-ddb0-4c6b-b9c1-16e98fc760ef', ''));

// ── 1b. bidi controls in an officer name are refused; ZWJ/ZWNJ are names ──
const RLO = '\u202e';
await refuses('e-challan decision refuses an officer name with U+202E', () =>
	decideEnforcementRecommendation('e8dae008-f923-4e26-be84-c9ed13f11f80', { decision: 'approve', officer: `Mehta ${RLO}reversed` }),
);
await refuses('event lifecycle refuses an officer name with U+2066', () =>
	transitionEventStatus('4508d86d-566b-438f-8414-b3d67fd7e6e4', { toStatus: 'active', officer: 'Officer \u2066X' }),
);
await refuses('escort approval refuses an officer name with U+202A', () =>
	approveEscort('9775233f-ddb0-4c6b-b9c1-16e98fc760ef', { approval_id: 'ap-1', decision: 'approve', officer: 'Officer \u202aX' }),
);
await refuses('movement CLEAR refuses an officer name with an RLM mark (the brain 422s it)', () =>
	clearMovement('9775233f-ddb0-4c6b-b9c1-16e98fc760ef', 'Officer \u200fX'),
);
await refuses('e-challan decision refuses an officer name with a control character', () =>
	decideEnforcementRecommendation('e8dae008-f923-4e26-be84-c9ed13f11f80', { decision: 'approve', officer: 'Officer\u0007X' }),
);
ok('ZWJ / ZWNJ stay legal in a name (Indic scripts)', !BIDI_CONTROL_RE.test('क्\u200dष \u200cમહેતા'));

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
for (const [name, fn] of [
	['hostile movement id rejected (escort approve)', () => approveEscort('../movements', { approval_id: 'ap-1', decision: 'approve', officer: 'Officer X' })],
	['hostile approval id rejected (escort approve)', () => approveEscort('9775233f-ddb0-4c6b-b9c1-16e98fc760ef', { approval_id: 'ap/../x', decision: 'approve', officer: 'Officer X' })],
	['hostile movement id rejected (clear)', () => clearMovement('x y z', 'Officer X')],
	['hostile event id rejected (lifecycle)', () => transitionEventStatus('../../events', { toStatus: 'active', officer: 'Officer X' })],
] as const) {
	try {
		await fn();
		ok(name, false, 'hostile id accepted');
	} catch (err) {
		ok(name, !(err instanceof CommitRefused) && !(err instanceof TypeError), (err as Error).constructor.name);
	}
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

// ── 4. the C-N 409: shown verbatim, its reason kept ─────────────────────────
const withheld = parseDecisionRefusal(
	JSON.stringify({ detail: 'e-challan withheld — vehicle reported stolen', reason: 'stolen_vehicle_fir' }),
);
ok('C-N 409 detail kept verbatim', withheld.detail === 'e-challan withheld — vehicle reported stolen', withheld.detail);
ok('C-N 409 reason kept', withheld.reason === 'stolen_vehicle_fir', String(withheld.reason));
const nested = parseDecisionRefusal(JSON.stringify({ detail: { detail: 'e-challan withheld — vehicle reported stolen', reason: 'stolen_vehicle_fir' } }));
ok('C-N 409 nested detail parsed', nested.detail === 'e-challan withheld — vehicle reported stolen' && nested.reason === 'stolen_vehicle_fir');

if (failures > 0) {
	console.error(`ops-write-entry: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   ops-write-entry — all writer-gate assertions passed');
process.exit(0);
