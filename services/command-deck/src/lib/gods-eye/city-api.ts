/**
 * City fusion client — the C-C surfaces VEER CITY COMMAND reads.
 *
 *   GET /api/v1/city/entities            the fused snapshot (registry, 4 s)
 *   GET /api/v1/city/entity/{kind}/{id}  one entity's dossier (on selection)
 *   GET /api/v1/city/feed?since=         the city narrative (fusion rail, 3 s)
 *
 * plus the read-only health surfaces the CITY VITALS rail fuses:
 *
 *   GET /gods-eye/feeds                  the oracle's ingest block (this deck's
 *                                        own server route: /health is not
 *                                        under the /api/v1 proxy prefix)
 *   GET /api/v1/vehicles/retention       the C6 retention reaper's health
 *   GET /api/convoy/health               the two-tier brain (decider + Gemma)
 *   GET /api/emulator/api/v1/emulator/runs?limit=   newest emulator runs (C-G)
 *   GET /api/emulator/api/v1/emulator/living-city   the living-city status (C-G)
 *   GET /api/convoy/movements/{id}/escort           escort status (C-J)
 *
 * Same security contract as data.ts and src/lib/ops/client.ts (C5): every
 * URL is a fixed constant from this module / endpoints.ts, or built from an
 * id validated against the oracle's own grammar — nothing here accepts a
 * caller-supplied URL. A 404 is "not deployed" (NotDeployed, 60 s per-path
 * back-off), never a crash. The pure `parse*` functions are exported for
 * tests/city-command-smoke.mjs.
 *
 * Ethics rail (C6): the server masks plates in every label and text; the raw
 * plate travels only in props.plate, and the deck shows it only to a named
 * duty officer (plates.ts). No record here carries a face, person or frame.
 */

import { BRAIN_API, EMULATOR_PROXY_PATH, ORACLE_V1, safePathId } from '$lib/ops/endpoints';
import { parseEscortOutcome, parseEscortProposal } from '$lib/ops/client';
import type { EscortProposal, EscortProposalOutcome } from '$lib/ops/types';
import { Down, NotDeployed, getJson } from './data';

// ─── Fixed endpoints ─────────────────────────────────────────────────────────

export const CITY_ENTITIES_API = `${ORACLE_V1}/city/entities`;
/** + /{kind}/{id} */
export const CITY_ENTITY_API = `${ORACLE_V1}/city/entity`;
export const CITY_FEED_API = `${ORACLE_V1}/city/feed`;
export const RETENTION_API = `${ORACLE_V1}/vehicles/retention`;
export const BRAIN_HEALTH_API = `${BRAIN_API}/health`;
export const EMULATOR_RUNS_API = `${EMULATOR_PROXY_PATH}/api/v1/emulator/runs`;
export const LIVING_CITY_API = `${EMULATOR_PROXY_PATH}/api/v1/emulator/living-city`;
/** Deck-own server route (routes/(deck)/gods-eye/feeds/+server.ts). */
export const FEED_HEALTH_API = '/gods-eye/feeds';

/** City-entity poll cadence (contract C-C: the deck polls every 4 s). */
export const CITY_POLL_MS = 4000;
/** Fusion-rail poll cadence. */
export const FEED_POLL_MS = 3000;

// ─── URNs ────────────────────────────────────────────────────────────────────

export const ENTITY_KINDS = [
	'event',
	'vehicle',
	'sighting',
	'camera',
	'junction',
	'unit',
	'convoy',
	'dispatch',
	'recommendation',
	'anomaly',
	'segment',
] as const;

export type EntityKind = (typeof ENTITY_KINDS)[number];

/** The oracle's id grammar per kind family (city/common.py parse_entity_id). */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const INT_RE = /^[1-9][0-9]{0,17}$/;
const SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;
const UUID_KINDS = new Set<EntityKind>(['event', 'vehicle', 'sighting', 'dispatch', 'recommendation']);
const INT_KINDS = new Set<EntityKind>(['junction', 'segment', 'anomaly']);

export function isEntityKind(v: unknown): v is EntityKind {
	return typeof v === 'string' && (ENTITY_KINDS as readonly string[]).includes(v);
}

/** True when `id` is a legal id for `kind` (never reaches a URL otherwise). */
export function validEntityId(kind: EntityKind, id: string): boolean {
	if (UUID_KINDS.has(kind)) return UUID_RE.test(id);
	if (INT_KINDS.has(kind)) return INT_RE.test(id);
	return SLUG_RE.test(id);
}

/** Canonical `<kind>:<id>` (contract C-C). */
export function urnOf(kind: EntityKind, id: string | number): string {
	return `${kind}:${id}`;
}

/** Split a URN at its FIRST colon (slug ids may contain colons); null when invalid. */
export function parseUrn(urn: string | null | undefined): { kind: EntityKind; id: string } | null {
	if (typeof urn !== 'string') return null;
	const i = urn.indexOf(':');
	if (i <= 0) return null;
	const kind = urn.slice(0, i);
	const id = urn.slice(i + 1);
	if (!isEntityKind(kind) || !validEntityId(kind, id)) return null;
	return { kind, id };
}

// ─── Records ─────────────────────────────────────────────────────────────────

export type SourceBadge = 'live' | 'emulated';
export type BandCode = 'GREEN' | 'AMBER' | 'RED' | 'BLACK';

/** One CityEntity (contract C-C), times as epoch ms. */
export interface CityEntity {
	urn: string;
	kind: EntityKind;
	id: string;
	label: string;
	/** Null only for dossier records of entities with no known position. */
	lon: number | null;
	lat: number | null;
	state: string | null;
	band: BandCode | null;
	source: SourceBadge;
	/** Epoch ms of the entity's own time (sighting, update, program …). */
	t: number | null;
	/** Server-computed age at snapshot time. */
	ageSec: number | null;
	links: string[];
	props: Record<string, unknown>;
}

export interface KindFeed {
	count: number;
	newestT: number | null;
	ageSec: number | null;
	unlocated: number;
	/** ok | unavailable | error (a dead source never blanks the city). */
	status: string;
	error: string | null;
	/** Producer-specific facts (camera registry, patrol feed, conflicts …). */
	extra: Record<string, unknown>;
}

export interface CitySnapshot {
	asOf: number | null;
	entities: CityEntity[];
	feeds: Record<string, KindFeed>;
	tookMs: number | null;
}

export type FeedKind = 'event' | 'enforcement' | 'anpr' | 'dispatch' | 'signals' | 'ama' | 'brain';

export const FEED_KINDS: readonly FeedKind[] = ['event', 'enforcement', 'anpr', 'dispatch', 'signals', 'ama', 'brain'];

/** One FeedItem (contract C-C), `t` as epoch ms. */
export interface FeedRow {
	id: string;
	t: number;
	kind: FeedKind;
	urns: string[];
	actor: string | null;
	actorKind: 'human' | 'machine' | 'system';
	text: string;
	source: SourceBadge;
	severity: 'info' | 'warn' | 'critical';
	props: Record<string, unknown>;
}

export interface DossierRelation {
	relation: string;
	entity: CityEntity;
}

export interface Dossier {
	entity: CityEntity;
	related: DossierRelation[];
	audit: FeedRow[];
	fetchedAt: number;
}

// ─── Parsers (pure) ──────────────────────────────────────────────────────────

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length ? v : null;
}

function epochMs(v: unknown): number | null {
	if (typeof v === 'number' && Number.isFinite(v)) return v;
	if (typeof v === 'string') {
		const t = Date.parse(v);
		return Number.isFinite(t) ? t : null;
	}
	return null;
}

function record(v: unknown): Record<string, unknown> {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

const BANDS: readonly BandCode[] = ['GREEN', 'AMBER', 'RED', 'BLACK'];

/** One wire CityEntity → record; null when it cannot be identified. */
export function parseCityEntity(raw: unknown): CityEntity | null {
	const r = record(raw);
	const kind = r.kind;
	const id = r.id != null ? String(r.id) : null;
	if (!isEntityKind(kind) || !id) return null;
	const urn = str(r.urn) ?? urnOf(kind, id);
	const lon = num(r.lon);
	const lat = num(r.lat);
	return {
		urn,
		kind,
		id,
		label: str(r.label) ?? urn,
		lon: lon != null && lat != null ? lon : null,
		lat: lon != null && lat != null ? lat : null,
		state: str(r.state),
		band: typeof r.band === 'string' && (BANDS as readonly string[]).includes(r.band) ? (r.band as BandCode) : null,
		source: r.source === 'emulated' ? 'emulated' : 'live',
		t: epochMs(r.t),
		ageSec: num(r.age_sec),
		links: Array.isArray(r.links) ? r.links.filter((u): u is string => typeof u === 'string') : [],
		props: record(r.props),
	};
}

/** GET /city/entities body → snapshot (unknown kinds and junk rows dropped). */
export function parseCitySnapshot(resp: unknown): CitySnapshot {
	const body = record(resp);
	const entities: CityEntity[] = [];
	for (const raw of Array.isArray(body.entities) ? body.entities : []) {
		const e = parseCityEntity(raw);
		if (e) entities.push(e);
	}
	const feeds: Record<string, KindFeed> = {};
	for (const [kind, raw] of Object.entries(record(body.feeds))) {
		const f = record(raw);
		const { count, newest_t, age_sec, unlocated, status, error, ...extra } = f;
		feeds[kind] = {
			count: num(count) ?? 0,
			newestT: epochMs(newest_t),
			ageSec: num(age_sec),
			unlocated: num(unlocated) ?? 0,
			status: str(status) ?? 'ok',
			error: str(error),
			extra,
		};
	}
	return { asOf: epochMs(body.as_of), entities, feeds, tookMs: num(body.took_ms) };
}

const ACTOR_KINDS = ['human', 'machine', 'system'] as const;
const SEVERITIES = ['info', 'warn', 'critical'] as const;

/** One wire FeedItem → row; null when it cannot be placed in time. */
export function parseFeedRow(raw: unknown): FeedRow | null {
	const r = record(raw);
	const id = str(r.id);
	const t = epochMs(r.t);
	const kind = r.kind;
	if (!id || t == null || typeof kind !== 'string' || !(FEED_KINDS as readonly string[]).includes(kind)) return null;
	return {
		id,
		t,
		kind: kind as FeedKind,
		urns: Array.isArray(r.urns) ? r.urns.filter((u): u is string => typeof u === 'string') : [],
		actor: str(r.actor),
		actorKind: (ACTOR_KINDS as readonly string[]).includes(String(r.actor_kind))
			? (r.actor_kind as FeedRow['actorKind'])
			: 'system',
		text: str(r.text) ?? '',
		source: r.source === 'emulated' ? 'emulated' : 'live',
		severity: (SEVERITIES as readonly string[]).includes(String(r.severity))
			? (r.severity as FeedRow['severity'])
			: 'info',
		props: record(r.props),
	};
}

export function parseFeedRows(resp: unknown): FeedRow[] {
	const body = record(resp);
	const items = Array.isArray(body.items) ? body.items : Array.isArray(resp) ? (resp as unknown[]) : [];
	const out: FeedRow[] = [];
	for (const raw of items) {
		const row = parseFeedRow(raw);
		if (row) out.push(row);
	}
	return out;
}

/** GET /city/entity/{kind}/{id} body → dossier; null when the entity is unreadable. */
export function parseDossier(resp: unknown, fetchedAt = Date.now()): Dossier | null {
	const body = record(resp);
	const entity = parseCityEntity(body.entity);
	if (!entity) return null;
	const related: DossierRelation[] = [];
	for (const raw of Array.isArray(body.related) ? body.related : []) {
		const r = record(raw);
		const e = parseCityEntity(r.entity);
		if (e) related.push({ relation: str(r.relation) ?? 'related', entity: e });
	}
	return { entity, related, audit: parseFeedRows({ items: body.audit }), fetchedAt };
}

// ─── Fetchers ────────────────────────────────────────────────────────────────

/** GET /city/entities — the whole fused snapshot (all kinds, no bbox). */
export async function fetchCitySnapshot(signal?: AbortSignal): Promise<CitySnapshot> {
	return parseCitySnapshot(await getJson<unknown>(CITY_ENTITIES_API, 8000, signal));
}

/** GET /city/entity/{kind}/{id} — throws NotDeployed on 404 (unknown entity or route). */
export async function fetchDossier(urn: string, signal?: AbortSignal): Promise<Dossier> {
	const parsed = parseUrn(urn);
	if (!parsed) throw new Down(`invalid entity urn: ${urn}`);
	const resp = await getJson<unknown>(
		`${CITY_ENTITY_API}/${parsed.kind}/${encodeURIComponent(parsed.id)}?audit_limit=60`,
		10000,
		signal,
	);
	const dossier = parseDossier(resp);
	if (!dossier) throw new Down(`unreadable dossier for ${urn}`);
	return dossier;
}

/** GET /city/feed?since=&limit= — newest first. */
export async function fetchCityFeed(since: number | null, limit = 100, signal?: AbortSignal): Promise<FeedRow[]> {
	const q = new URLSearchParams({ limit: String(Math.max(1, Math.min(500, Math.round(limit)))) });
	if (since != null && Number.isFinite(since)) q.set('since', new Date(since).toISOString());
	return parseFeedRows(await getJson<unknown>(`${CITY_FEED_API}?${q}`, 8000, signal));
}

// ─── Health surfaces (CITY VITALS) ───────────────────────────────────────────

export interface IngestHealth {
	enabled: boolean;
	connected: boolean;
	subscribed: boolean;
	msgsPerMin: number | null;
	lastMsgAgeSec: number | null;
	emulatedLastMsgAgeSec: number | null;
	rowsWritten5m: number | null;
	/** Per-subject msgs/min + last age (corridor.traffic.live, …units.emulated, …). */
	subjects: Array<{ subject: string; msgsPerMin: number | null; lastMsgAgeSec: number | null }>;
}

export function parseIngestHealth(resp: unknown): IngestHealth | null {
	const body = record(record(resp).ingest);
	if (!Object.keys(body).length) return null;
	const subjects = Object.entries(record(body.subjects)).map(([subject, raw]) => {
		const s = record(raw);
		return { subject, msgsPerMin: num(s.msgs_per_min), lastMsgAgeSec: num(s.last_msg_age_sec) };
	});
	return {
		enabled: body.enabled === true,
		connected: body.connected === true,
		subscribed: body.subscribed === true,
		msgsPerMin: num(body.msgs_per_min),
		lastMsgAgeSec: num(body.last_msg_age_sec),
		emulatedLastMsgAgeSec: num(body.emulated_last_msg_age_sec),
		rowsWritten5m: num(body.rows_written_5m),
		subjects,
	};
}

/** GET /gods-eye/feeds → the oracle ingest block (null when the oracle is down). */
export async function fetchIngestHealth(signal?: AbortSignal): Promise<IngestHealth | null> {
	return parseIngestHealth(await getJson<unknown>(FEED_HEALTH_API, 6000, signal));
}

/**
 * The reaper's last pass, as the oracle reports it (vehicles/store.py):
 * {ran: true, removed: n} · {ran: false, reason: 'locked elsewhere'} (another
 * replica held the lock) · {ran: false, failed: true, reason}.
 */
export interface ReapResult {
	ran: boolean;
	removed: number | null;
	failed: boolean;
	reason: string | null;
}

export interface RetentionHealth {
	defaultRetentionDays: number | null;
	earmarkCapDays: number | null;
	healthy: boolean | null;
	consecutiveFailures: number | null;
	lastReap: ReapResult | null;
	lastSuccessAt: number | null;
	due: boolean | null;
	policy: string | null;
}

export function parseRetention(resp: unknown): RetentionHealth {
	const body = record(resp);
	const reaper = record(body.reaper);
	const last = reaper.last_result != null && typeof reaper.last_result === 'object' ? record(reaper.last_result) : null;
	return {
		defaultRetentionDays: num(body.default_retention_days),
		earmarkCapDays: num(body.earmark_retention_cap_days),
		healthy: typeof reaper.healthy === 'boolean' ? reaper.healthy : null,
		consecutiveFailures: num(reaper.consecutive_failures),
		lastReap: last
			? {
					ran: last.ran === true,
					removed: num(last.removed),
					failed: last.failed === true,
					reason: str(last.reason)?.slice(0, 120) ?? null,
				}
			: null,
		lastSuccessAt: epochMs(reaper.last_success_at),
		due: typeof reaper.due === 'boolean' ? reaper.due : null,
		policy: str(body.policy),
	};
}

export async function fetchRetention(signal?: AbortSignal): Promise<RetentionHealth> {
	return parseRetention(await getJson<unknown>(RETENTION_API, 6000, signal));
}

export interface BrainHealth {
	/** connected | loading | unreachable (WP-12 adds 'loading' while the model loads). */
	decider: 'connected' | 'loading' | 'unreachable';
	deciderDegraded: boolean;
	deciderDegradedReason: string | null;
	p95Ms: number | null;
	noveltyLatch: string | null;
	tier0Cache: string | null;
	gemma: 'connected' | 'unreachable';
	status: string | null;
}

export function parseBrainHealth(resp: unknown): BrainHealth {
	const body = record(resp);
	const s = record(body.strategic_tier);
	const tac = record(body.tactical_tier);
	const host = str(s.model_host);
	return {
		decider: host === 'connected' ? 'connected' : host === 'loading' ? 'loading' : 'unreachable',
		deciderDegraded: s.degraded === true,
		deciderDegradedReason: str(s.degraded_reason),
		p95Ms: num(s.p95_decide_latency_ms),
		noveltyLatch: str(s.novelty_latch),
		tier0Cache: str(s.tier0_cache),
		gemma: tac.llama_server === 'connected' ? 'connected' : 'unreachable',
		status: str(body.status),
	};
}

export async function fetchBrainHealth(signal?: AbortSignal): Promise<BrainHealth> {
	return parseBrainHealth(await getJson<unknown>(BRAIN_HEALTH_API, 6000, signal));
}

export interface EmulatorRunRow {
	runId: number;
	useCaseId: string | null;
	status: string | null;
	clockScale: number | null;
	createdAt: number | null;
	endedAt: number | null;
	isReplay: boolean;
	/** C-G progress, when the emulator reports it. */
	progress: { frame: number | null; total: number | null } | null;
}

export function parseEmulatorRuns(resp: unknown): EmulatorRunRow[] {
	const rows = Array.isArray(record(resp).runs) ? (record(resp).runs as unknown[]) : [];
	const out: EmulatorRunRow[] = [];
	for (const raw of rows) {
		const r = record(raw);
		const runId = num(r.run_id);
		if (runId == null) continue;
		const spec = record(r.spec);
		const prog = record(r.progress);
		out.push({
			runId,
			useCaseId: str(r.use_case_id) ?? str(spec.use_case_id),
			status: str(r.status),
			clockScale: num(r.clock_scale),
			createdAt: epochMs(r.created_at),
			endedAt: epochMs(r.ended_at),
			isReplay: spec.is_replay === true || r.is_replay === true,
			progress: Object.keys(prog).length ? { frame: num(prog.frame), total: num(prog.total) } : null,
		});
	}
	return out.sort((a, b) => b.runId - a.runId);
}

export async function fetchEmulatorRuns(limit = 8, signal?: AbortSignal): Promise<EmulatorRunRow[]> {
	return parseEmulatorRuns(
		await getJson<unknown>(`${EMULATOR_RUNS_API}?limit=${Math.max(1, Math.min(50, Math.round(limit)))}`, 8000, signal),
	);
}

export interface LivingCityStatus {
	enabled: boolean;
	status: string | null;
	activeSlots: number;
	nextCause: { at: number | null; title: string | null } | null;
}

export function parseLivingCity(resp: unknown): LivingCityStatus {
	const body = record(resp);
	const next = Array.isArray(body.next_causes) ? record(body.next_causes[0]) : {};
	return {
		enabled: body.enabled === true,
		status: str(body.status),
		activeSlots: Array.isArray(body.active_slots) ? body.active_slots.length : num(body.active_slots) ?? 0,
		nextCause: Object.keys(next).length ? { at: epochMs(next.at), title: str(next.title) } : null,
	};
}

export async function fetchLivingCity(signal?: AbortSignal): Promise<LivingCityStatus> {
	return parseLivingCity(await getJson<unknown>(LIVING_CITY_API, 6000, signal));
}

// ─── Escort status (contract C-J, WP-12) ─────────────────────────────────────
//
// Escort authority (officer approves every switch, WP-21): the document also
// carries the escort's ONE open proposal (`pending_approval`) and its closed
// ones (`approval_outcomes`), parsed with the ops client's own C-J parsers —
// so a console following one escort shows its proposal without GET
// /api/convoy/escorts (the ops store falls back to it; see
// escortProposalFor).

/** A hold the escort is under (C-J status doc `hold`): who ordered it, on which segments. */
export interface EscortHold {
	segmentIds: number[];
	orderedBy: string | null;
	reason: string | null;
	peakCongestion: number | null;
	since: number | null;
	maxSec: number | null;
}

/**
 * An escort action the brain referred to a named officer (C-J
 * `pending_approvals[]`): a hold, a re-route, a detour. The officer
 * approves or rejects it; the AI never does.
 */
export interface EscortApproval {
	approvalId: string;
	action: string;
	reason: string | null;
	raisedAt: number | null;
	tier: string | null;
	confidence: number | null;
	segmentIds: number[];
	/** The detour the action would take (drawn dashed as a preview). */
	detourSegmentIds: number[];
	detourM: number | null;
	remainingM: number | null;
	/** Its numbers came from a SIMULATED feed. */
	simulated: boolean;
}

export interface EscortStatus {
	movementId: string;
	eventId: string | null;
	/** idle | waiting_for_gps | running | completed | stopped | failed */
	status: string;
	iteration: number | null;
	position: { lon: number; lat: number; speedKmh: number | null; heading: number | null; t: number | null; source: string | null } | null;
	progress: number | null;
	etaSec: number | null;
	activeDiversions: number;
	lastDecision: { phase: string | null; tier: string | null; action: string | null; confidence: number | null } | null;
	startedAt: number | null;
	updatedAt: number | null;
	detail: string | null;
	/** The hold in force (null when the convoy is free to move). */
	hold: EscortHold | null;
	/** Actions waiting on a named officer (newest last) — the legacy list of a brain that predates escort authority. */
	pendingApprovals: EscortApproval[];
	/**
	 * The escort-authority rule the brain enforces (C-J `escort_authority`,
	 * 'officer_approves_every_switch'); null on a brain that predates it.
	 */
	authority: string | null;
	/**
	 * The document reports escort authority (`pending_approval` /
	 * `approval_outcomes` / `escort_authority`): its proposal fields are
	 * authoritative for this escort, and the legacy `pendingApprovals` list
	 * mirrors them (never shown twice).
	 */
	proposalsReported: boolean;
	/**
	 * The escort's ONE open proposal (C-J `pending_approval`) — answerable
	 * only while the escort runs (null once it ended). A console following
	 * one escort shows it even without GET /api/convoy/escorts.
	 */
	proposal: EscortProposal | null;
	/** Its closed proposals, newest last (C-J `approval_outcomes`). */
	outcomes: EscortProposalOutcome[];
	/** How long a proposal waits for an officer (s; C-J `approval_timeout_sec`). */
	approvalTimeoutSec: number | null;
	/** The congestion the escort reads comes from a SIMULATED feed (null = not reported). */
	trafficSimulated: boolean | null;
	/** GPS: 'emulated' (the brain's walker) or 'live' (a real fix); null = not reported. */
	gpsMode: 'emulated' | 'live' | null;
}

function intList(v: unknown): number[] {
	return Array.isArray(v) ? v.filter((n): n is number => typeof n === 'number' && Number.isFinite(n)) : [];
}

function parseHold(v: unknown): EscortHold | null {
	if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
	const h = v as Record<string, unknown>;
	return {
		segmentIds: intList(h.segment_ids),
		orderedBy: str(h.ordered_by),
		reason: str(h.reason),
		peakCongestion: num(h.peak_congestion),
		since: epochMs(h.since),
		maxSec: num(h.max_sec),
	};
}

function parseApprovals(v: unknown): EscortApproval[] {
	const out: EscortApproval[] = [];
	for (const raw of Array.isArray(v) ? v : []) {
		if (!raw || typeof raw !== 'object') continue;
		const a = raw as Record<string, unknown>;
		const id = str(a.approval_id);
		if (!id || (a.status != null && a.status !== 'pending')) continue;
		const dq = Array.isArray(a.data_quality) ? a.data_quality : a.data_quality != null ? [a.data_quality] : [];
		out.push({
			approvalId: id,
			action: str(a.action) ?? 'action',
			reason: str(a.reason),
			raisedAt: epochMs(a.raised_at),
			tier: str(a.tier),
			confidence: num(a.confidence),
			segmentIds: intList(a.segment_ids),
			detourSegmentIds: intList(a.detour_segment_ids),
			detourM: num(a.detour_m),
			remainingM: num(a.remaining_m),
			simulated: a.simulated === true || dq.includes('synthetic'),
		});
	}
	return out;
}

/** Escort lifecycle states after which nothing is answerable any more (C-J). */
const ESCORT_ENDED = new Set(['completed', 'stopped', 'failed']);

export function parseEscortStatus(movementId: string, resp: unknown): EscortStatus {
	const body = record(resp);
	const pos = record(body.position);
	const lon = num(pos.lon);
	const lat = num(pos.lat);
	const last = record(body.last_decision);
	const progress = num(body.progress);
	const status = str(body.status) ?? 'idle';
	const eventId = str(body.event_id);
	const authority = str(body.escort_authority);
	const proposalsReported =
		authority != null || 'pending_approval' in body || Array.isArray(body.approval_outcomes);
	return {
		movementId,
		eventId,
		status,
		iteration: num(body.iteration),
		position:
			lon != null && lat != null
				? {
						lon,
						lat,
						speedKmh: num(pos.speed_kmh),
						heading: num(pos.heading),
						t: epochMs(pos.t),
						source: str(pos.source),
					}
				: null,
		progress: progress == null ? null : Math.max(0, Math.min(1, progress)),
		etaSec: num(body.eta_sec),
		activeDiversions: Array.isArray(body.active_diversions) ? body.active_diversions.length : 0,
		lastDecision: Object.keys(last).length
			? { phase: str(last.phase), tier: str(last.tier), action: str(last.action), confidence: num(last.confidence) }
			: null,
		startedAt: epochMs(body.started_at),
		updatedAt: epochMs(body.updated_at),
		detail: str(body.detail),
		hold: parseHold(body.hold),
		pendingApprovals: parseApprovals(body.pending_approvals),
		authority,
		proposalsReported,
		// a proposal is answerable only while its escort runs (the brain
		// nulls it once the run ended; a persisted doc is held to the same)
		proposal: ESCORT_ENDED.has(status) ? null : parseEscortProposal(body.pending_approval, movementId, eventId),
		outcomes: (Array.isArray(body.approval_outcomes) ? body.approval_outcomes : [])
			.map((o) => parseEscortOutcome(o, movementId))
			.filter((o): o is EscortProposalOutcome => o !== null),
		approvalTimeoutSec: num(body.approval_timeout_sec),
		trafficSimulated: (() => {
			const f = record(body.traffic_feed);
			return typeof f.simulated === 'boolean' ? f.simulated : null;
		})(),
		gpsMode: (() => {
			const g = record(body.gps);
			const mode = str(g.mode) ?? (str(pos.source) === 'emulated_gps' ? 'emulated' : str(pos.source) === 'gps' ? 'live' : null);
			return mode === 'emulated' || mode === 'live' ? mode : null;
		})(),
	};
}

/**
 * GET /api/convoy/movements/{id}/escort — NotDeployed until WP-12 lands.
 * A brain without the route answers the GET with 405 (only POST /escort
 * exists there): that is "not deployed" too, never an error state.
 */
export async function fetchEscortStatus(movementId: string, signal?: AbortSignal): Promise<EscortStatus> {
	const id = safePathId(movementId);
	if (!id) throw new Down(`invalid movement id: ${movementId}`);
	try {
		return parseEscortStatus(id, await getJson<unknown>(`${BRAIN_API}/movements/${id}/escort`, 6000, signal));
	} catch (err) {
		if (err instanceof Down && err.message.startsWith('405')) {
			throw new NotDeployed(405, 'escort status route not deployed (brain predates contract C-J)');
		}
		throw err;
	}
}

export { Down, NotDeployed };
