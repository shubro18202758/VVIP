/**
 * Ops live smoke — the /ops data contract probed straight at the argus
 * upstreams, so the client's assumptions cannot drift from the stack
 * silently.
 *
 *   node tests/ops-live-smoke.mjs [oracleBase] [brainBase] [emulatorBase]
 *
 * Defaults are the live argus stack (infra/compose.argus-override.yml):
 * oracle :8091, brain :8092, emulator :8090 — or ORACLE_URL / BRAIN_URL /
 * EMULATOR_URL, or just ORACLE_PORT / BRAIN_PORT / EMULATOR_PORT (the
 * scripts/veer_env.sh names), so the same smoke reaches a private sandbox
 * stack. It talks to the upstreams on loopback, never through the deck, so
 * the deck's operator gate is not in the path (the deck smokes cover the
 * proxy and the gate).
 *
 * Read-only (R8): every probe is a GET except the twin's pure compute POSTs
 * (simulate/twin, twin/compare) and ONE informational chat turn (the brain's
 * read-only rail; it drives the shared models, one call). It never starts an
 * emulator run, never decides, approves, dispatches or earmarks anything.
 * Exits 0 with `ok <name> — <detail>` lines; 1 on any failure.
 */

const env = process.env;
const ORACLE = (process.argv[2] ?? env.ORACLE_URL ?? `http://127.0.0.1:${env.ORACLE_PORT ?? '8091'}`).replace(/\/+$/, '');
const BRAIN = (process.argv[3] ?? env.BRAIN_URL ?? `http://127.0.0.1:${env.BRAIN_PORT ?? '8092'}`).replace(/\/+$/, '');
const EMULATOR = (process.argv[4] ?? env.EMULATOR_URL ?? `http://127.0.0.1:${env.EMULATOR_PORT ?? '8090'}`).replace(/\/+$/, '');

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

function json(r) {
	try {
		return JSON.parse(r.text);
	} catch {
		return null;
	}
}

console.log(`ops live smoke: oracle ${ORACLE} · brain ${BRAIN} · emulator ${EMULATOR}`);

// ─── board feed ──────────────────────────────────────────────────────────────
let activeEvents = [];
{
	const r = await status(`${ORACLE}/api/v1/events/active`);
	activeEvents = json(r)?.events ?? [];
	ok('events/active is 200 (the board\'s primary rung)', r.status === 200 && Array.isArray(json(r)?.events), `status=${r.status} events=${activeEvents.length}`);

	const m = await status(`${ORACLE}/api/v1/movements/active`);
	ok('movements/active is 200 (the convoy-era fallback rung)', m.status === 200, m.text.slice(0, 80));
}

// ─── adaptive band ───────────────────────────────────────────────────────────
{
	const s = await status(`${ORACLE}/api/v1/adaptive/status`);
	ok('adaptive/status is 200 (band fallback rung)', s.status === 200, s.text.slice(0, 80));

	// Scoped to a real event when the board has one (the store never polls it unscoped).
	const scope = activeEvents[0]?.event_id ? `event_id=${encodeURIComponent(activeEvents[0].event_id)}&` : '';
	const log = await status(`${ORACLE}/api/v1/adaptive/situation_log?${scope}limit=10`);
	ok('situation_log is 200 (the band\'s primary rung)', log.status === 200, `status=${log.status}${scope ? ' (scoped to the first active event)' : ''}`);

	const ov = await status(`${ORACLE}/api/v1/adaptive/overrides?${scope}limit=10`);
	ok('overrides is 200 (the AMA card\'s evidence)', ov.status === 200, `status=${ov.status}`);

	const p = await status(`${ORACLE}/api/v1/adaptive/params?vvip_class=X`);
	ok('adaptive/params is 200 (shadow-eval evidence rung)', p.status === 200, p.text.slice(0, 100));
}

// ─── twin (pure compute) ─────────────────────────────────────────────────────
{
	const c = await status(`${ORACLE}/api/v1/simulate/twin/compare`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			scenario_id: 'rath_yatra_ahmedabad',
			candidate_policy: { advance_closure_sec: 60, closure_type: 'partial_closure' },
		}),
	}, 60000);
	ok('twin/compare is 200 (contract C2)', c.status === 200, `status=${c.status}`);

	// Explicit fields, NO scenario_id → the explicit policy must be echoed (the
	// C2 clobber bug was scenario_id-only). The spine is the emulator's sweep
	// belt, a drivable chain on the real-road network (WP-20).
	const explicit = {
		route_segment_ids: [1015, 1012, 1049, 1013],
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
	}, 120000);
	const twin = json(t);
	const runs = twin?.runs ?? [];
	ok('simulate/twin (explicit fields) is 200', t.status === 200, t.text.slice(0, 100));
	ok('simulate/twin returned replication runs', Array.isArray(runs) && runs.length === 4, `runs=${runs.length}`);
	ok(
		'simulate/twin echoes the explicit closure policy',
		twin?.closure_policy?.advance_closure_sec === 60 && twin?.closure_policy?.closure_type === 'partial_closure',
		JSON.stringify(twin?.closure_policy ?? null),
	);
}

// ─── brain ───────────────────────────────────────────────────────────────────
{
	const h = await status(`${BRAIN}/health`, {}, 15000);
	const hb = json(h);
	ok(
		'brain /health ok with both model tiers connected',
		h.status === 200 && hb?.status === 'ok'
			&& hb?.strategic_tier?.model_host === 'connected' && hb?.tactical_tier?.llama_server === 'connected',
		h.text.slice(0, 100),
	);

	// NDJSON audit rung: one short turn, informational (drives the shared models).
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 120000);
	let ndjsonLines = 0;
	let sawType = '';
	try {
		const res = await fetch(`${BRAIN}/chat/stream`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ message: 'In one sentence: what tools do you have for corridor traffic?' }),
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
		ok('chat/stream delivers NDJSON lines (audit rung 3)', ndjsonLines >= 1, `lines=${ndjsonLines} lastType=${sawType}`);
	} catch (err) {
		ok('chat/stream delivers NDJSON lines (audit rung 3)', false, String(err));
	} finally {
		clearTimeout(timer);
	}
}

// ─── emulator (GET only — this smoke never starts a run) ─────────────────────
{
	const e = await status(`${EMULATOR}/health`, {}, 5000);
	const eb = json(e);
	const stack = eb?.stack ?? {};
	// connectivity, not status: 'degraded' also reports historical conditions
	// (a failed last run) that no wiring fix changes — printed, not failed
	ok(
		'emulator /health connected (store, valkey, twin, nats)',
		e.status === 200 && !!(stack.store && stack.valkey && stack.twin && stack.nats),
		`status=${eb?.status ?? e.status}${eb?.degraded_reasons?.length ? ` degraded_reasons=${eb.degraded_reasons.join('; ')}` : ''}`,
	);

	const uc = await status(`${EMULATOR}/api/v1/emulator/use-cases`, {}, 5000);
	const useCaseIds = (json(uc) ?? []).map?.((u) => u.id) ?? [];
	ok('emulator use-cases list matches the showcase library', uc.status === 200 && useCaseIds.length >= 8, `ids=${useCaseIds.slice(0, 3).join(',')}…`);

	const runs = await status(`${EMULATOR}/api/v1/emulator/runs?limit=5`, {}, 5000);
	// counts only: a run document's spec can carry the sweep's (emulated) earmarked plate
	ok('emulator runs listable (the panel\'s runs table feed)', runs.status === 200 && Array.isArray(json(runs)?.runs),
		`status=${runs.status} runs=${json(runs)?.runs?.length ?? '?'}`);
}

// ─── decision surfaces (GET only — the writers stay human actions, R8) ──────
{
	// A detail line never echoes a decision-surface body: the recommendation,
	// dispatch, unit, watchlist and interception rows carry plates, FIRs and
	// unit ids, and a smoke log is no place for one (C6) — counts only.
	const recs = await status(`${ORACLE}/api/v1/enforcement/recommendations?status=pending&limit=10`);
	ok('enforcement/recommendations 200 (the queue feed)', recs.status === 200,
		`status=${recs.status} rows=${json(recs)?.recommendations?.length ?? '?'}`);
	if (recs.status === 200) {
		const body = json(recs);
		const first = body?.recommendations?.[0];
		ok(
			'recommendation rows carry evidence + rule (the card expander)',
			!first || (typeof first.rule_id === 'string' && first.evidence != null),
			`pending=${body?.recommendations?.length ?? 0}${first ? ` rule=${first.rule_id}` : ''}`,
		);
	}

	const disp = await status(`${ORACLE}/api/v1/dispatches?limit=5`);
	ok('dispatches 200 (the tiles feed)', disp.status === 200, `status=${disp.status} rows=${json(disp)?.dispatches?.length ?? '?'}`);
	if (disp.status === 200) {
		const jr = json(disp)?.dispatches?.find((d) => d.agency === 'jan_rakshak');
		ok(
			'jan_rakshak dispatches carry the audit trail',
			!jr || Array.isArray(jr.audit),
			jr ? `unit=${jr.unit_id} audit=${jr.audit.length} status=${jr.status}` : 'no jan_rakshak rows',
		);
	}

	const units = await status(`${ORACLE}/api/v1/units`);
	ok('units 200 (canvas agency layer)', units.status === 200,
		`status=${units.status} units=${json(units)?.units?.length ?? '?'} source=${json(units)?.source ?? '?'}`);

	const plans = await status(`${ORACLE}/api/v1/signals/plans`);
	ok('signals/plans 200 (the gate card)', plans.status === 200, plans.text.slice(0, 90));
	if (plans.status === 200) {
		const body = json(plans);
		ok(
			'band plan table rows are rank-able (band + floors)',
			Array.isArray(body?.band_plan_table) && body.band_plan_table.every((r) => typeof r.band === 'string'),
			`${body?.band_plan_table?.length ?? 0} rows · writer_enabled=${body?.writer_enabled}`,
		);
		ok('signals/plans carries the city band provenance (city_band_since)', typeof body?.city_band_since === 'string', `city_band=${body?.city_band} since ${body?.city_band_since}`);
	}

	const hist = await status(`${ORACLE}/api/v1/signals/history?limit=5`);
	ok('signals/history 200 or 503 (one click away, honest when down)', hist.status === 200 || hist.status === 503, `status=${hist.status}`);

	const vehicles = await status(`${ORACLE}/api/v1/vehicles`);
	ok('vehicles 200 (watchlist feed)', vehicles.status === 200, `status=${vehicles.status} rows=${json(vehicles)?.vehicles?.length ?? '?'}`);
	if (vehicles.status === 200) {
		const flagged = json(vehicles)?.vehicles?.find((v) => v.earmark_state === 'flagged');
		ok(
			'earmarked rows carry lawful basis + retention (C6 shape)',
			!flagged || (typeof flagged.lawful_basis === 'string' && typeof flagged.retention_until === 'string'),
			flagged ? `basis=${flagged.lawful_basis}` : 'none flagged',
		);
	}

	const pursuit = activeEvents.find((e) => e.event_kind === 'vehicle_pursuit');
	if (pursuit) {
		const plan = await status(`${ORACLE}/api/v1/events/${pursuit.event_id}/interception-plan`);
		ok('interception-plan 200 for the live pursuit', plan.status === 200, `status=${plan.status}`);
	} else {
		console.log('skip interception-plan — no pursuit active on the board');
	}
}

console.log(failures === 0 ? '\nlive smoke: all checks passed' : `\nlive smoke: ${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
