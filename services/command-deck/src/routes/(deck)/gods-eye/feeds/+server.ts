/**
 * GET /gods-eye/feeds — the oracle's live-ingest counters for CITY VITALS.
 *
 * The oracle publishes its ingest block on GET /health, which sits outside
 * the deck's /api/v1 proxy prefix, so this deck route reads it server-side
 * from the same FIXED LOOPBACK target hooks.server.ts proxies to (host
 * pinned to 127.0.0.1, only the port env-overridable — the C5 carve-out)
 * and answers a WHITELIST of liveness-level counters: connection flags,
 * messages per minute, last-message ages, rows written. No URL, error
 * text, NATS address or data row ever leaves this route, so — like the
 * liveness probes the operator gate allows anonymously — it needs no
 * operator session.
 */

import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const ORACLE_HEALTH = `http://127.0.0.1:${process.env.ORACLE_PORT ?? '8091'}/health`;

const NO_STORE = { 'cache-control': 'no-store' };

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function record(v: unknown): Record<string, unknown> {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

export const GET: RequestHandler = async () => {
	let body: Record<string, unknown>;
	try {
		const res = await fetch(ORACLE_HEALTH, { signal: AbortSignal.timeout(4000), redirect: 'manual' });
		if (!res.ok) return json({ detail: `oracle /health answered ${res.status}` }, { status: 502, headers: NO_STORE });
		body = record(await res.json());
	} catch {
		return json({ detail: 'oracle unreachable' }, { status: 502, headers: NO_STORE });
	}
	const ingest = record(body.ingest);
	const subjects: Record<string, { msgs_per_min: number | null; last_msg_age_sec: number | null }> = {};
	for (const [subject, raw] of Object.entries(record(ingest.subjects))) {
		// Subject names are the oracle's fixed NATS subjects (corridor.*).
		if (!/^[a-z][a-z0-9_.-]{0,63}$/.test(subject)) continue;
		const s = record(raw);
		subjects[subject] = { msgs_per_min: num(s.msgs_per_min), last_msg_age_sec: num(s.last_msg_age_sec) };
	}
	return json(
		{
			status: typeof body.status === 'string' ? body.status : null,
			ingest: {
				enabled: ingest.enabled === true,
				connected: ingest.connected === true,
				subscribed: ingest.subscribed === true,
				msgs_per_min: num(ingest.msgs_per_min),
				last_msg_age_sec: num(ingest.last_msg_age_sec),
				emulated_last_msg_age_sec: num(ingest.emulated_last_msg_age_sec),
				rows_written_5m: num(ingest.rows_written_5m),
				subjects,
			},
			as_of: new Date().toISOString(),
		},
		{ headers: NO_STORE },
	);
};
