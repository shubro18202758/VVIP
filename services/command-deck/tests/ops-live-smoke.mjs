/**
 * Ops showcase live smoke — run against the LIVE fleet on this box.
 *
 *   node tests/ops-live-smoke.mjs [oracleBase] [brainBase] [emulatorBase]
 *
 * Defaults match the deck's fixed endpoint constants (through the vite dev
 * proxy the client uses the same paths; here we probe the upstreams directly
 * plus one probe through the dev proxy when it is running). Asserts exactly
 * the fallback ladder the /ops client implements, so any drift between the
 * client's assumptions and the live stack fails loudly.
 */

const ORACLE = process.argv[2] ?? 'http://localhost:8081';
const BRAIN = process.argv[3] ?? 'http://localhost:8080';
const EMULATOR = process.argv[4] ?? 'http://localhost:8090';

let failures = 0;

function ok(name, cond, detail = '') {
	if (cond) {
		console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

async function status(url, opts = {}, timeoutMs = 8000) {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const res = await fetch(url, { ...opts, signal: ctrl.signal });
		const text = await res.text().catch(() => '');
		return { status: res.status, text };
	} catch (err) {
		return { status: 0, text: err instanceof Error ? err.message : String(err) };
	} finally {
		clearTimeout(timer);
	}
}

// ─── board feed ──────────────────────────────────────────────────────────────
{
	const r = await status(`${ORACLE}/api/v1/events/active`);
	ok('events/active responds', r.status > 0, `status=${r.status}`);
	ok(
		'events/active 404 means the client falls back (expected today)',
		r.status === 404 || r.status === 200,
		`status=${r.status} body=${r.text.slice(0, 80)}`,
	);

	const m = await status(`${ORACLE}/api/v1/movements/active`);
	ok('movements/active is 200 (fallback rung live)', m.status === 200, m.text.slice(0, 80));
}

// ─── adaptive band ───────────────────────────────────────────────────────────
{
	const s = await status(`${ORACLE}/api/v1/adaptive/status`);
	ok('adaptive/status is 200 (band fallback rung)', s.status === 200, s.text.slice(0, 80));

	const log = await status(`${ORACLE}/api/v1/adaptive/situation_log?event_id=demo&limit=10`);
	ok(
		'situation_log is 404 (primary rung not deployed) or 200',
		log.status === 404 || log.status === 200,
		`status=${log.status}`,
	);

	const ov = await status(`${ORACLE}/api/v1/adaptive/overrides?event_id=demo&limit=10`);
	ok(
		'overrides is 404 (not deployed) or 200',
		ov.status === 404 || ov.status === 200,
		`status=${ov.status}`,
	);

	const p = await status(`${ORACLE}/api/v1/adaptive/params?vvip_class=X`);
	ok('adaptive/params is 200 (shadow-eval evidence rung)', p.status === 200, p.text.slice(0, 100));
}

// ─── twin ────────────────────────────────────────────────────────────────────
{
	const c = await status(`${ORACLE}/api/v1/simulate/twin/compare`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			scenario_id: 'rath_yatra_ahmedabad',
			candidate_policy: { advance_closure_sec: 60, closure_type: 'partial_closure' },
		}),
	});
	ok(
		'twin/compare is 404 (fallback path) or 200 (contract C2 deployed)',
		c.status === 404 || c.status === 200,
		`status=${c.status}`,
	);

	// The labelled fallback: explicit fields, NO scenario_id → explicit policy
	// must be echoed (the C2 clobber bug was scenario_id-only).
	const explicit = {
		route_segment_ids: [1012, 1015, 1005, 1013],
		departure_time: '2025-06-27T07:00:00+05:30',
		closure_policy: { advance_closure_sec: 60, closure_type: 'partial_closure' },
		horizon_min: 60,
		n_replications: 4,
		seed: 20250627,
	};
	const t = await status(`${ORACLE}/api/v1/simulate/twin`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(explicit),
	});
	let runs = [];
	try {
		const parsed = JSON.parse(t.text);
		runs = parsed.runs ?? [];
	} catch {
		/* body not json */
	}
	ok('simulate/twin (explicit fields) is 200', t.status === 200, t.text.slice(0, 100));
	ok('simulate/twin returned replication runs', Array.isArray(runs) && runs.length === 4, `runs=${runs.length}`);
}

// ─── brain audit channels ────────────────────────────────────────────────────
{
	const h = await status(`${BRAIN}/health`);
	ok('brain /health is 200', h.status === 200, h.text.slice(0, 80));

	// NDJSON audit rung: one short probe, informational (drives the LLM).
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 60000);
	let ndjsonLines = 0;
	let sawType = '';
	try {
		const res = await fetch(`${BRAIN}/chat/stream`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				message: 'In one sentence: what tools do you have for corridor traffic?',
				movement_id: null,
				vvip_class: null,
			}),
			signal: ctrl.signal,
		});
		ok('chat/stream responds 200', res.status === 200, `status=${res.status}`);
		if (res.body) {
			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buf = '';
			while (ndjsonLines < 5) {
				const { done, value } = await reader.read();
				if (done) break;
				buf += decoder.decode(value, { stream: true });
				const lines = buf.split('\n');
				buf = lines.pop() ?? '';
				for (const line of lines) {
					if (!line.trim()) continue;
					ndjsonLines++;
					try {
						sawType = JSON.parse(line).type;
					} catch {
						/* partial */
					}
					if (ndjsonLines >= 5) break;
				}
			}
			reader.cancel().catch(() => {});
		}
		ok(
			'chat/stream delivers NDJSON lines (audit rung 3)',
			ndjsonLines >= 1,
			`lines=${ndjsonLines} lastType=${sawType}`,
		);
	} catch (err) {
		ok('chat/stream delivers NDJSON lines (audit rung 3)', false, String(err));
	} finally {
		clearTimeout(timer);
	}
}

// ─── emulator (real contract, deployed by the emulator stream mid-build) ────
{
	const e = await status(`${EMULATOR}/health`, {}, 4000);
	ok(
		'emulator :8090 health answers (online → panel drives the real API; refused → "emulator offline")',
		e.status === 0 || e.status === 200,
		`status=${e.status} ${e.text.slice(0, 100)}`,
	);

	if (e.status === 200) {
		const uc = await status(`${EMULATOR}/api/v1/emulator/use-cases`, {}, 5000);
		let useCaseIds = [];
		try {
			useCaseIds = JSON.parse(uc.text).map((u) => u.id);
		} catch {
			/* not json */
		}
		ok('emulator use-cases list matches the showcase library ids', uc.status === 200 && useCaseIds.length >= 8, `ids=${useCaseIds.slice(0, 3).join(',')}…`);

		// The POST holds until the tape finishes; confirmation is the run ROW
		// (written immediately). Fire the POST, then poll the list — this is
		// exactly what the panel does (client timeout does not stop the tape).
		const before = await status(`${EMULATOR}/api/v1/emulator/runs?limit=1`, {}, 5000);
		const beforeId = (() => {
			try {
				return JSON.parse(before.text).runs?.[0]?.run_id ?? 0;
			} catch {
				return 0;
			}
		})();
		const abortCtrl = new AbortController();
		fetch(`${EMULATOR}/api/v1/emulator/runs`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ use_case_id: 'vvip_convoy_base', clock_scale: 60 }),
			signal: abortCtrl.signal,
		}).catch(() => {});
		let staged = null;
		for (let i = 0; i < 10 && !staged; i++) {
			await new Promise((r) => setTimeout(r, 1000));
			const list = await status(`${EMULATOR}/api/v1/emulator/runs?limit=3`, {}, 5000);
			try {
				const rows = JSON.parse(list.text).runs ?? [];
				staged = rows.find((r) => r.run_id > beforeId && r.spec?.use_case_id === 'vvip_convoy_base') ?? null;
			} catch {
				/* retry */
			}
		}
		abortCtrl.abort(); // response not needed; the tape runs server-side
		ok(
			'emulator run row appears after POST /api/v1/emulator/runs',
			staged !== null,
			staged ? `run_id=${staged.run_id} status=${staged.status}` : 'no new run row within 10s',
		);

		const runs = await status(`${EMULATOR}/api/v1/emulator/runs?limit=5`, {}, 5000);
		ok('emulator runs listable (panel runs table feed)', runs.status === 200, runs.text.slice(0, 100));
	}
}

// ─── decision surfaces (ops-screens; GET only — writers stubbed per R8) ─────
{
	const recs = await status(`${ORACLE}/api/v1/enforcement/recommendations?status=pending&limit=10`);
	ok('enforcement/recommendations 200 (the queue feed)', recs.status === 200, recs.text.slice(0, 90));
	if (recs.status === 200) {
		const body = JSON.parse(recs.text);
		const first = body.recommendations?.[0];
		ok(
			'recommendation rows carry evidence + rule (the card expander)',
			!first || (typeof first.rule_id === 'string' && first.evidence != null),
			`pending=${body.recommendations.length}${first ? ` rule=${first.rule_id}` : ''}`,
		);
	}

	const disp = await status(`${ORACLE}/api/v1/dispatches?limit=5`);
	ok('dispatches 200 (the tiles feed)', disp.status === 200, disp.text.slice(0, 90));
	if (disp.status === 200) {
		const body = JSON.parse(disp.text);
		const jr = body.dispatches?.find((d) => d.agency === 'jan_rakshak');
		ok(
			'jan_rakshak dispatches carry the audit trail',
			!jr || Array.isArray(jr.audit),
			jr ? `unit=${jr.unit_id} audit=${jr.audit.length} status=${jr.status}` : 'no jan_rakshak rows',
		);
	}

	const units = await status(`${ORACLE}/api/v1/units`);
	ok('units 200 (canvas agency layer)', units.status === 200, units.text.slice(0, 80));

	const plans = await status(`${ORACLE}/api/v1/signals/plans`);
	ok('signals/plans 200 (the gate card)', plans.status === 200, plans.text.slice(0, 90));
	if (plans.status === 200) {
		const body = JSON.parse(plans.text);
		ok(
			'band plan table rows are rank-able (band + floors)',
			Array.isArray(body.band_plan_table) && body.band_plan_table.every((r) => typeof r.band === 'string'),
			`${body.band_plan_table.length} rows · writer_enabled=${body.writer_enabled}`,
		);
	}

	const hist = await status(`${ORACLE}/api/v1/signals/history?limit=5`);
	ok('signals/history 200 or 503 (one click away, honest when down)', hist.status === 200 || hist.status === 503, `status=${hist.status}`);

	const vehicles = await status(`${ORACLE}/api/v1/vehicles`);
	ok('vehicles 200 (watchlist feed)', vehicles.status === 200, vehicles.text.slice(0, 80));
	if (vehicles.status === 200) {
		const body = JSON.parse(vehicles.text);
		const flagged = body.vehicles?.find((v) => v.earmark_state === 'flagged');
		ok(
			'earmarked rows carry lawful basis + retention (C6 shape)',
			!flagged || (typeof flagged.lawful_basis === 'string' && typeof flagged.retention_until === 'string'),
			flagged ? `plate=${flagged.plate} basis=${flagged.lawful_basis}` : 'none flagged',
		);
	}

	const active = await status(`${ORACLE}/api/v1/events/active`);
	if (active.status === 200) {
		const body = JSON.parse(active.text);
		const pursuit = (body.events ?? []).find((e) => e.event_kind === 'vehicle_pursuit');
		if (pursuit) {
			const plan = await status(`${ORACLE}/api/v1/events/${pursuit.event_id}/interception-plan`);
			ok('interception-plan 200 for the live pursuit', plan.status === 200, plan.text.slice(0, 80));
		} else {
			ok('interception-plan (skipped — no pursuit active)', true, 'none');
		}
	}
}

console.log(failures === 0 ? '\nlive smoke: all checks passed' : `\nlive smoke: ${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
