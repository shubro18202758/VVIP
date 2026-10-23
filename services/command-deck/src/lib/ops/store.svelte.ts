/**
 * Ops showcase store — the single state machine behind the /ops board.
 *
 * Runes-based (Svelte 5). Route components (and the God's-eye canvas) call
 * start()/stop() — reference-counted, so the last holder out stops the
 * pollers; the store owns every poller, socket and cache:
 *
 *   board     5 s   GET /api/v1/events/active → movements fallback; preview
 *                   fixtures only when the events API is unreachable / not
 *                   deployed, or the operator's PREVIEW toggle is on
 *   band      5 s   the SELECTED live event's situation_log (never the
 *                   unscoped log) → else the city band (GET /adaptive/status
 *                   city_band: band, the moment it was ENTERED, simulated
 *                   feed, stale) → else none. A stale city band is UNKNOWN,
 *                   never GREEN. Waits for the first board load.
 *   city band 5 s   the city band on its own (CITY POSTURE falls back to
 *                   it), read whatever the selection is
 *   audit     2 s   the stream is a VIEW over server data (contract C-C):
 *                   the selected live event's GET /events/{id}/audit, else
 *                   (or when the operator follows the city) GET /city/feed
 *                   every 3 s; only an oracle serving neither falls back to
 *                   the adaptive-status rung. Local rows are what the
 *                   server cannot know (refusals before the wire, transport
 *                   errors, probe output) plus officer echoes that retire
 *                   as soon as the server row lands — so a reload shows the
 *                   same narrative. WS ladder under the vite dev server only.
 *   pursuit   6 s   interception plan, only while a LIVE vehicle_pursuit is
 *                   selected; request-token guarded (stale answers dropped)
 *   emulator  10 s  health + library + runs (+ living city when the build
 *                   serves it) through the same-origin /api/emulator; a run
 *                   the panel launched is tracked every 2 s until it ends
 *   ledger    10 s  GET /api/v1/ops/degradation-taxonomy (decisions_log
 *                   failure taxonomy; empty log is an honest empty ledger)
 *   brain     10 s  GET /api/convoy/health + /adaptive/status (posture strip)
 *   corridor  10 s  GET /corridor/summary quality_mix (SIMULATED FEED chip)
 *   approved  15 s  approved recommendations (the pursuit chain, C6)
 *   retention 60 s  GET /vehicles/retention (reaper health, C6 / DPDP)
 *   geometry  5 min junction registry + segment lines (waypoint names,
 *                   pursuit anchors, spine road names)
 *   twin      never twin compare runs ONLY on operator demand and caches per
 *                   (scenario, policies, seed) — no interval polling of the
 *                   double-compute endpoint (oracle-load mitigation)
 *
 * Network hygiene: every poller is in-flight guarded, nothing is fetched while
 * the page is hidden (one catch-up round when it returns), and a GET path
 * that answered 404 is not requested again for 60 s (client.ts). With the
 * operator gate on (VEER_OPERATOR_PIN) and nobody signed in, no operator
 * poller runs at all: the wall shows one "sign in as the duty officer"
 * state (`signInRequired`) instead of a 403 per panel.
 *
 * Language: `lang` starts in English (the server render) and adopts the
 * persisted veer_lang choice once mounted (adoptLangCookie); every change
 * the operator makes is written back to the cookie, so a sign-in reload
 * keeps HI/GU.
 *
 * Plates (C6): the board (`events`), the interception plan
 * (`interception`), the audit views (`audit`, `eventAudit`) and the probe
 * answer (`ndjsonText`) are published to every consumer — panels, the
 * canvas labels, the cockpit rails, /comms — with registration marks
 * masked (GJ27EM•••1) until a named duty officer is on console, and
 * republished raw the moment one is. The unmasked rows stay private to the
 * store's own logic, so no surface can print a plate by forgetting to mask
 * it.
 */

import { untrack } from 'svelte';
import {
	approvalSpent,
	ApiNotDeployed,
	CommitRefused,
	DEV_SERVER,
	DecisionRefused,
	SIGN_IN_REQUIRED,
	actorKindOfName,
	advanceDispatch,
	applySignalPlan,
	clearEarmark,
	compareTwinPolicies,
	compareTwinPoliciesDirect,
	createAuditSocket,
	createPollGate,
	decideDispatch,
	decideEnforcementRecommendation,
	decideEscortProposal,
	emulatorGetRun,
	emulatorHealth,
	emulatorLaunchRun,
	emulatorListRuns,
	emulatorLivingCity,
	emulatorReplayRun,
	emulatorSetLivingCity,
	emulatorStopRun,
	emulatorUseCases,
	earmarkVehicle,
	fetchActiveEvents,
	fetchAdaptiveStatus,
	fetchBrainPosture,
	fetchCityFeed,
	fetchDegradationTaxonomy,
	fetchDispatches,
	fetchEnforcementRecommendations,
	fetchEscorts,
	fetchEventAudit,
	fetchInterceptionPlan,
	fetchJunctionRegistry,
	fetchMovementsAsEvents,
	fetchOverrides,
	fetchRetention,
	fetchSegmentLines,
	fetchSignalHistory,
	fetchSignalPlans,
	fetchSituationLog,
	fetchTrafficProvenance,
	fetchWatchlist,
	isHeroUseCase,
	junctionsNearSpine,
	nearestSegment,
	openPursuitEvent,
	pollAuditTail,
	probeActorOfTier,
	requestDispatch,
	restoreSignalPrograms,
	streamAuditProbe,
	transitionEventStatus,
	isSignInRequired,
	withinBlipGrace,
	type BrainWsEvent,
	type CityBandDoc,
	type EmulatorPostResult,
	type EscortApprovalOutcome,
	type SituationLogPoint,
} from './client';
import { readLangCookie, writeLangCookie } from '$lib/i18n/lang-cookie';
import { operatorSession } from '$lib/shell/session.svelte';
import { tOps } from '$lib/i18n/strings.ops';
import { istHm } from '$lib/primitives/time';
import {
	AGENCIES,
	DEFAULT_UNIT_ID,
	EMULATOR_HERO_CLOCK_CAP,
	EVENT_TRANSITIONS,
	POLL_MS,
	PURSUIT_ANCHOR_TOL_M,
	WS_OPEN_TIMEOUT_MS,
} from './endpoints';
import { clipText, maskDetail, maskPlate, maskPlatesInText, tailText } from './format';
import { USE_CASES, useCaseById } from './usecases';
import type {
	ActorKind,
	AuditEntry,
	AuditLadderState,
	AuditScope,
	AuditView,
	BandCandidate,
	BandCode,
	BandEvidence,
	BandPoint,
	BandSourceLabel,
	BrainPosture,
	CorridorEvent,
	ClosurePolicySpec,
	DegradationTaxonomy,
	DispatchRecord,
	EmulatorLaunchState,
	EmulatorRunRow,
	EmulatorState,
	EmulatorUseCaseInfo,
	EnforcementRecommendation,
	EscortLane,
	EscortProposal,
	EscortProposalOutcome,
	FollowedEscortDoc,
	EventTransitionOutcome,
	EventTransitionTarget,
	InterceptionPlan,
	JunctionRef,
	LivingCityDoc,
	OverrideRecord,
	PursuitCandidate,
	RetentionHealth,
	SegmentRef,
	ServerAuditRow,
	SignalApplyOutcome,
	SignalHistoryRow,
	SignalPlansDoc,
	TrafficFeedProvenance,
	TwinCompareResult,
	VehicleWatchRecord,
	WaypointOption,
} from './types';

const AUDIT_CAP = 300;
/** Local rows (refusals, errors, probe output, echoes) kept per session. */
const LOCAL_AUDIT_CAP = 120;
/** Escort lanes (GET /api/convoy/escorts): a proposal must reach every console within ~2 s. */
const ESCORT_POLL_MS = 2000;
/** The brain's expiry reason for an unanswered proposal (live_escort.APPROVAL_TIMEOUT_REASON). */
const ESCORT_TIMEOUT_REASON = 'no officer response — current plan held';
/** Checkpoint tiers a proposal can come from (decider.CheckpointResult.tier; escTier_* strings). */
const ESCORT_TIERS = ['decider', 'tier0_cache_hit', 'novelty_latched', 'rate_limited', 'gemma', 'unavailable', 'deterministic', 'none'];
/** The probe answer box shows the newest this-many characters. */
const PROBE_TAIL_CHARS = 2000;
/** Raw probe tokens kept (far beyond the tail, so a cut never reaches what is shown). */
const PROBE_RAW_CAP = 16_000;
const BAND_CAP = 240;
/** Server rows read per audit poll (C-C limit). */
const AUDIT_SERVER_LIMIT = 100;
/**
 * An officer's local echo retires once a server row of the same kind by the
 * same actor lands within this window (the server row is the record).
 */
const ECHO_MATCH_MS = 20_000;
/**
 * A launch POST that has not answered by now is held by a synchronous
 * build: the panel stops waiting and follows the run from the ledger. C-G
 * answers 202 right after its ledger insert, well inside this, so either
 * way the control is free again in under a second.
 */
const LAUNCH_RACE_MS = 900;

/** localStorage key + URL flag (?preview=1) for the PREVIEW fixtures toggle. */
const PREVIEW_KEY = 'veer.ops.previewFixtures';

const BAND_RANK: Record<BandCode, number> = { GREEN: 0, AMBER: 1, RED: 2, BLACK: 3 };

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function fixtureEvents(): CorridorEvent[] {
	return USE_CASES.map((u) => ({
		eventId: u.id,
		name: u.name,
		eventKind: u.eventKind,
		protectionClass: u.protectionClass,
		crowdClass: u.crowdClass,
		state: 'planned' as const,
		routeSegmentIds: u.routeSegmentIds,
		phases: u.phases,
		currentPhase: u.phases.length ? u.phases[u.phases.length - 1].label : null,
		band: null,
		windowStart: u.departureTime,
		windowEnd: null,
		description: u.description,
		source: 'offline-fixture' as const,
	}));
}

/** A row from a live API (events or movements), never a preview fixture. */
export function isLiveEvent(ev: CorridorEvent | null | undefined): ev is CorridorEvent {
	return !!ev && ev.source !== 'offline-fixture';
}

/** Live AND uuid-keyed — the only ids the oracle's uuid-cast routes accept. */
export function isLiveUuidEvent(ev: CorridorEvent | null | undefined): ev is CorridorEvent {
	return isLiveEvent(ev) && UUID_RE.test(ev.eventId);
}

/** A live pursuit — the only events interception plans exist for. */
export function isLivePursuit(ev: CorridorEvent | null | undefined): ev is CorridorEvent {
	return isLiveUuidEvent(ev) && ev.eventKind === 'vehicle_pursuit';
}

/**
 * A corridor event with a C-F lifecycle: a live uuid row of the EVENTS API.
 * A movements-fallback row is keyed by its movement id — POST
 * /events/{movement_id}/status would only 404.
 */
export function hasLifecycle(ev: CorridorEvent | null | undefined): ev is CorridorEvent {
	return isLiveUuidEvent(ev) && ev.source === 'live-events-api';
}

/**
 * The lifecycle moves the verb rail offers for an event (contract C-F):
 * only for events-API rows, from the RAW wire status. Exported pure.
 */
export function eventVerbs(ev: CorridorEvent | null | undefined): EventTransitionTarget[] {
	if (!hasLifecycle(ev)) return [];
	return [...(EVENT_TRANSITIONS[ev.status ?? ''] ?? [])];
}

/**
 * Auto-selection preference: a live vehicle_pursuit (active before planned),
 * then any live active event, then any live event, then the first preview
 * fixture. Exported pure for the contract tests.
 */
export function preferredEvent(events: CorridorEvent[]): CorridorEvent | null {
	const live = events.filter(isLiveEvent);
	const pursuits = live.filter((e) => e.eventKind === 'vehicle_pursuit' && e.state !== 'completed' && e.state !== 'cancelled');
	return (
		pursuits.find((e) => e.state === 'active') ??
		pursuits[0] ??
		live.find((e) => e.state === 'active') ??
		live[0] ??
		events[0] ??
		null
	);
}

/**
 * The hysteresis candidate from a subject's log (oldest → newest): the
 * newest READING (observed_class) when it differs from the committed class,
 * with the run length of identical readings under that same committed band.
 * Null when the log carries no observed_class (nothing pending is visible)
 * or the newest reading agrees with the committed band. `required` mirrors
 * adaptive/hysteresis.py's shipping config (escalate 2, de-escalate 4,
 * BLACK immediate). Exported pure for the contract tests.
 */
export function bandCandidateOf(log: SituationLogPoint[]): BandCandidate | null {
	const newest = log[log.length - 1];
	if (!newest || !newest.observed || newest.observed === newest.band) return null;
	const reading = newest.observed;
	let ticks = 0;
	let since = newest.ts;
	for (let i = log.length - 1; i >= 0; i--) {
		const p = log[i];
		if (p.observed !== reading || p.band !== newest.band) break;
		ticks++;
		since = p.ts;
	}
	const required = reading === 'BLACK' ? 1 : BAND_RANK[reading] > BAND_RANK[newest.band] ? 2 : 4;
	return { band: reading, ticks, required, since };
}

/**
 * The merged audit view: the server rows of the active scope plus the local
 * rows, oldest → newest, capped. A local officer echo is dropped once a
 * server row of its kind by the same actor lands within ECHO_MATCH_MS —
 * about the SAME event when the echo names one (the row's urns carry
 * `event:<id>`), so an ACTIVATE on one card never retires a CANCEL echo of
 * another. The server row is the record. Exported pure for tests.
 */
export function mergeAuditView(server: AuditEntry[], local: AuditEntry[], cap = AUDIT_CAP): AuditEntry[] {
	const norm = (s: string) => s.trim().toLowerCase();
	const sameSubject = (r: AuditEntry, echo: AuditEntry) =>
		!echo.eventId || !r.urns?.length || r.urns.includes(`event:${echo.eventId}`);
	const confirmed = (echo: AuditEntry) =>
		!!echo.kind &&
		server.some(
			(r) =>
				r.kind === echo.kind &&
				norm(r.actor) === norm(echo.actor) &&
				Math.abs(r.ts - echo.ts) <= ECHO_MATCH_MS &&
				sameSubject(r, echo),
		);
	const kept = local.filter((e) => !(e.source === 'operator' && e.actorKind === 'human' && confirmed(e)));
	return [...server, ...kept].sort((a, b) => a.ts - b.ts || a.id - b.id).slice(-cap);
}

function planSignature(plan: InterceptionPlan): string {
	return JSON.stringify([
		plan.vehicleId,
		plan.latestSighting?.sightingId,
		plan.latestSighting?.t,
		plan.seedSegmentId,
		plan.chokes.map((c) => [c.junctionId, c.score, c.likelyPassed === true]),
		plan.stale === true,
		plan.direction?.towardsJunctionIds,
		plan.note,
	]);
}

/** Content identity of a board (mapped rows are small plain data). */
function eventsSignature(events: CorridorEvent[]): string {
	return JSON.stringify(events);
}

function auditLevelOf(row: ServerAuditRow): AuditEntry['level'] {
	if (row.actorKind === 'human') return 'decision';
	if (row.severity === 'critical') return 'error';
	if (row.kind === 'ama') return 'band';
	if (row.kind === 'brain') return 'model';
	return 'info';
}

/** An event as an officer-less console may print it: plates masked in its title and description. */
function maskEventText(e: CorridorEvent): CorridorEvent {
	const name = maskPlatesInText(e.name);
	const description = e.description ? maskPlatesInText(e.description) : e.description;
	return name === e.name && description === e.description ? e : { ...e, name, description };
}

/** A plan as an officer-less console may print it: the sighting's plate masked. */
function maskPlanPlate(plan: InterceptionPlan): InterceptionPlan {
	const s = plan.latestSighting;
	if (!s?.plate) return plan;
	return { ...plan, latestSighting: { ...s, plate: maskPlate(s.plate) } };
}

/** Masked copies per raw audit entry — a republish never rebuilds an unchanged row. */
const maskedEntries = new WeakMap<AuditEntry, AuditEntry>();

/**
 * An audit row as an officer-less console may print it: registration marks
 * masked in its text and its evidence. A local echo carries the raw mark
 * the officer acted on, a probe row whatever the brain's tools returned —
 * the published view never does.
 */
function maskAuditEntry(e: AuditEntry): AuditEntry {
	let m = maskedEntries.get(e);
	if (!m) {
		const text = maskPlatesInText(e.text);
		m = text === e.text && (e.detail === undefined || e.detail === null) ? e : { ...e, text, detail: maskDetail(e.detail) };
		maskedEntries.set(e, m);
	}
	return m;
}

/** Upper-case verb of a lifecycle move (the officer echo reads as what was asked). */
const TRANSITION_VERB: Record<EventTransitionTarget, string> = {
	approved: 'APPROVE',
	active: 'ACTIVATE',
	completed: 'COMPLETE',
	cancelled: 'CANCEL',
};

class OpsStore {
	// ─── board ────────────────────────────────────────────────────────────────
	/**
	 * The board as this console may print it (titles plate-masked until a
	 * duty officer is named). Republished only when the rows or the
	 * officer's presence change, so consumers keyed on it never re-run on a
	 * no-op poll or a keystroke.
	 */
	events = $state<CorridorEvent[]>([]);
	/** The board rows as the API mapped them (unmasked; store logic only). */
	private boardRows: CorridorEvent[] = [];
	boardStatus = $state<'live' | 'mixed' | 'offline'>('offline');
	boardUpdated = $state(0);
	boardNote = $state<string | null>(null);
	eventsApiDeployed = $state<boolean | null>(null);
	/**
	 * Operator PREVIEW toggle: show the labelled use-case fixtures next to a
	 * healthy live board (they always show when the events API is down).
	 * Persisted per browser; `?preview=1` turns it on for a session link.
	 */
	previewFixtures = $state(false);

	selectedEventId = $state<string | null>(null);

	// ─── event lifecycle (contract C-F) ───────────────────────────────────────
	/** Event id with a lifecycle POST in flight (verb rails disable; one POST per click). */
	eventBusy = $state<string | null>(null);
	/** The last lifecycle attempt — rendered inline on the card that made it. */
	eventOutcome = $state<EventTransitionOutcome | null>(null);

	// ─── situation band ───────────────────────────────────────────────────────
	/** The COMMITTED band of the subject (selected event, else the city). */
	band = $state<BandCode | null>(null);
	bandEvidence = $state<BandEvidence | null>(null);
	bandHistory = $state<BandPoint[]>([]);
	/** Which rung the band came from (logic); `bandSource` is how it reads. */
	bandSourceKind = $state<BandSourceLabel>('none');
	/**
	 * The band's inputs came from a SIMULATED feed (situation_log
	 * simulated_feed, the adaptive subject's simulated_feed, or the city
	 * band's) — null when the oracle does not say.
	 */
	bandSimulated = $state<boolean | null>(null);
	/** Epoch ms the city band's engine poll started failing (stale band); null otherwise. */
	bandStaleSince = $state<number | null>(null);
	/** The city band on its own (CITY POSTURE falls back to it), whatever is selected. */
	cityBand = $state<CityBandDoc | null>(null);
	cityBandUpdated = $state(0);
	cityBandNote = $state<string | null>(null);
	/**
	 * Epoch ms of the newest band reading — the server's timestamp_utc for a
	 * situation_log band; for the city band the engine's newest evaluation
	 * (else the poll time). 0 = no band. (Surfaces derive the true age from
	 * it; the canvas AMA chip calls it STALE past 10 min.)
	 */
	bandUpdated = $state(0);
	/**
	 * Epoch ms the band source last ANSWERED (a successful read). The band
	 * panels' freshness keys on this — a healthy band that has not changed
	 * keeps answering, so it never reads STALE just because its newest entry
	 * is old (entries can be band ENTRY times).
	 */
	bandPolledAt = $state(0);
	/** A reading awaiting hysteresis confirmation (from the log's flags). */
	bandCandidate = $state<BandCandidate | null>(null);
	/**
	 * Epoch ms the current band was entered: the newest transitioned=true
	 * row of a situation_log (when in window), or the city band's `since`.
	 */
	bandCommittedAt = $state<number | null>(null);
	overrides = $state<OverrideRecord[]>([]);
	overridesNote = $state<string | null>(null);
	overridesUpdated = $state(0);

	// ─── audit stream (a view over server data) ───────────────────────────────
	/**
	 * The merged view the stream renders (oldest → newest), as this console
	 * may print it — plate-masked until a duty officer is named (C6).
	 */
	audit = $state<AuditEntry[]>([]);
	auditRung = $state<AuditLadderState | 'connecting'>('unavailable');
	/** Operator choice: follow the selected event, or the whole city. */
	auditScope = $state<AuditScope>('event');
	/** What the view actually shows right now. */
	auditView = $state<AuditView>('none');
	/** Epoch ms of the last server answer behind the view. */
	auditServerUpdated = $state(0);
	auditNote = $state<string | null>(null);
	/**
	 * The selected live event's own audit (the dossier narrative), oldest →
	 * newest — published under the same plate rule as `audit`.
	 */
	eventAudit = $state<AuditEntry[]>([]);
	/** The event `eventAudit` belongs to. */
	eventAuditFor = $state<string | null>(null);
	auditProbeRunning = $state(false);
	/**
	 * The probe's streamed answer as this console may print it: the newest
	 * PROBE_TAIL_CHARS, masked before the cut until a duty officer is named.
	 */
	ndjsonText = $state('');
	/** The probe's last done payload (tiers used, degraded). */
	probeDone = $state<{ tiersUsed: string[]; degraded: boolean | null; at: number } | null>(null);

	// ─── twin compare ─────────────────────────────────────────────────────────
	twinResult = $state<TwinCompareResult | null>(null);
	twinRunning = $state(false);
	twinError = $state<string | null>(null);
	twinScenarioId = $state<string>(USE_CASES[1].scenarioId); // rath yatra default
	twinCandidate = $state<ClosurePolicySpec>({
		advanceClosureSec: 60,
		closureType: 'partial_closure',
	});
	twinCache = new Map<string, TwinCompareResult>();
	twinCachedKey = $state<string | null>(null);

	// ─── emulator ─────────────────────────────────────────────────────────────
	emulator = $state<EmulatorState>({
		online: false,
		healthDetail: null,
		lastChecked: 0,
		runRequested: null,
		runError: null,
		stack: null,
		livingCitySupported: false,
	});
	/** The emulator's own library (GET /use-cases): names, categories, hero flags. */
	emulatorLibrary = $state<EmulatorUseCaseInfo[]>([]);
	emulatorUseCaseIds = $state<string[]>([]);
	emulatorRuns = $state<EmulatorRunRow[]>([]);
	/** The launch / replay the panel issued, tracked until it ends. */
	emulatorLaunch = $state<EmulatorLaunchState | null>(null);
	/** 'launch' | `stop:<run>` | `replay:<run>` while that control's request is in flight. */
	emulatorBusy = $state<string | null>(null);
	emulatorActionNote = $state<string | null>(null);
	livingCity = $state<LivingCityDoc | null>(null);
	livingCityNote = $state<string | null>(null);
	livingCityBusy = $state(false);

	// ─── language (shared with citizen views) ─────────────────────────────────
	/** The UI language; written to the veer_lang cookie on every change (C-M). */
	#lang = $state<string>('en');
	get lang(): string {
		return this.#lang;
	}
	set lang(code: string) {
		if (code === this.#lang) return;
		this.#lang = code;
		writeLangCookie(code);
	}

	/**
	 * Adopt the persisted language once the page has mounted (the server
	 * render is English, so the switch happens after hydration and every
	 * surface re-renders in it). Not written back — it already is the cookie.
	 */
	adoptLangCookie() {
		const choice = readLangCookie();
		if (choice && choice !== this.#lang) this.#lang = choice;
	}

	// ─── operator gate (the deck's VEER_OPERATOR_PIN session) ────────────────
	/** null until GET /api/session answered; true = the gate is on. */
	sessionGate = $state<boolean | null>(null);
	/** A read was refused by the gate after the session was known (it lapsed). */
	sessionLapsed = $state(false);

	// ─── duty officer identity (the C6 human-actor gate) ──────────────────────
	/** Named human on console. Every city-writer action refuses while blank. */
	dutyOfficer = $state<string>('');
	dutyOfficerNote = $state<string | null>(null);
	/** The officer came from the operator gate's signed-in session (field locked). */
	dutyOfficerFromSession = $state(false);

	// ─── enforcement recommendation queue (ops-screens) ───────────────────────
	recommendations = $state<EnforcementRecommendation[]>([]);
	recommendationsUpdated = $state(0);
	recommendationsLive = $state<boolean | null>(null);
	recommendationsNote = $state<string | null>(null);
	recommendationBusy = $state<string | null>(null);
	/**
	 * Decisions the oracle refused with a 409 (C-N: 'e-challan withheld —
	 * vehicle reported stolen'), by recommendation id: the sentence is shown
	 * verbatim on the card and APPROVE never comes back for it.
	 */
	recommendationRefusals = $state<Record<string, { status: number; detail: string; reason: string | null; at: number }>>({});
	/** The queue / tiles EMULATED chip: hide everything the emulator produced (said, with a count). */
	hideEmulated = $state(false);
	/** Human-approved recommendations — the link a pursuit opens from (C6). */
	approvedRecommendations = $state<EnforcementRecommendation[]>([]);
	approvedUpdated = $state(0);

	// ─── agency dispatch lifecycle (Jan Rakshak tiles) ────────────────────────
	dispatches = $state<DispatchRecord[]>([]);
	dispatchesUpdated = $state(0);
	dispatchesLive = $state<boolean | null>(null);
	dispatchesNote = $state<string | null>(null);
	dispatchBusy = $state<string | null>(null);
	/** The last request from the tiles (inline result under the request row). */
	dispatchRequestNote = $state<{ ok: boolean; text: string; at: number; dispatchId?: string | null } | null>(null);

	// ─── city signal plans (gate card) ────────────────────────────────────────
	signalPlans = $state<SignalPlansDoc | null>(null);
	signalPlansUpdated = $state(0);
	signalPlansNote = $state<string | null>(null);
	signalApply = $state<SignalApplyOutcome | null>(null);
	signalApplying = $state(false);
	/** A RESTORE in flight ('event' | 'all') — one restore at a time, a double click sends one POST. */
	signalRestoring = $state<'event' | 'all' | null>(null);
	signalHistory = $state<SignalHistoryRow[]>([]);
	signalHistoryOpen = $state(false);
	signalHistoryUpdated = $state(0);
	/** Why the history list is empty when it is not a real empty answer. */
	signalHistoryNote = $state<string | null>(null);

	// ─── watchlist (earmarked vehicles) ───────────────────────────────────────
	watchlist = $state<VehicleWatchRecord[]>([]);
	watchlistUpdated = $state(0);
	watchlistLive = $state<boolean | null>(null);
	watchlistNote = $state<string | null>(null);
	watchBusy = $state<string | null>(null);
	retention = $state<RetentionHealth | null>(null);
	retentionNote = $state<string | null>(null);

	// ─── escort authority (officer approves every switch) ────────────────────
	/**
	 * Running escorts with their ONE open proposal and recent outcomes
	 * (GET /api/convoy/escorts every ESCORT_POLL_MS): the HUD, the /ops
	 * decision queue and the city vitals read the same lanes.
	 */
	escorts = $state<EscortLane[]>([]);
	escortsUpdated = $state(0);
	/** null until the brain answered; false = it predates escort authority (404). */
	escortsDeployed = $state<boolean | null>(null);
	escortsNote = $state<string | null>(null);
	/** The proposal whose decision is in flight — one POST per click, every console. */
	escortDecisionBusy = $state<string | null>(null);
	/** The brain's answer per approval id (inline under the card that asked). */
	escortDecisions = $state<Record<string, EscortApprovalOutcome & { decision: 'approve' | 'reject'; officer: string; at: number }>>({});

	// ─── pursuit / interception ───────────────────────────────────────────────
	/** The selected pursuit's plan as this console may print it (sighting plate masked without an officer). */
	interception = $state<InterceptionPlan | null>(null);
	/** The plan as the oracle answered it (unmasked; store logic only). */
	private interceptionRaw: InterceptionPlan | null = null;
	interceptionUpdated = $state(0);
	interceptionNote = $state<string | null>(null);
	pursuitBusy = $state<string | null>(null);
	/** Inline refusal per vehicle when a pursuit open came back refused. */
	pursuitNote = $state<{ vehicleId: string; text: string } | null>(null);

	// ─── degradation ledger (decisions_log failure taxonomy) ─────────────────
	degradation = $state<DegradationTaxonomy | null>(null);
	degradationUpdated = $state(0);
	degradationLive = $state<boolean | null>(null);
	degradationNote = $state<string | null>(null);

	// ─── brain posture + feed provenance (masthead strip) ─────────────────────
	brain = $state<BrainPosture | null>(null);
	trafficFeed = $state<TrafficFeedProvenance | null>(null);
	trafficFeedNote = $state<string | null>(null);

	// ─── city geometry (junction names, segment lines) ────────────────────────
	junctions = $state<JunctionRef[]>([]);
	segmentLines = $state<SegmentRef[]>([]);
	geometryNote = $state<string | null>(null);

	// ─── internals ────────────────────────────────────────────────────────────
	private auditSeq = 0;
	private timers: ReturnType<typeof setInterval>[] = [];
	private holders = 0;
	private gate = createPollGate();
	private onVisibility: (() => void) | null = null;
	private socket: { close: () => void } | null = null;
	private socketKey = $state<string | null>(null);
	private lastAuditSignature = '';
	private heartbeatTicks = 0;
	/** True once the first board load settled (band/audit polls wait for it). */
	private boardLoaded = false;
	private eventsSig = '';
	/** Bumped on every selection change: band/audit answers for an older subject are dropped. */
	private subjectSeq = 0;
	/** Bumped on every interception request: only the newest answer lands. */
	private interceptionToken = 0;
	private interceptionSig = '';
	/** Local audit rows (refusals, errors, probe output, echoes) — a session ring. */
	private localAudit: AuditEntry[] = [];
	/** The selected event's server audit as the oracle answered it (store logic only). */
	private eventAuditRows: AuditEntry[] = [];
	/** The probe's raw answer tokens (store logic only; `ndjsonText` is what prints). */
	private probeTokens = '';
	/** Board reads issued / applied: an older read never overwrites a newer one. */
	private boardSeq = 0;
	private boardAppliedSeq = 0;
	/** Epoch ms of the last read the LIVE events API answered (the blip grace runs from it). */
	boardLiveAt = 0;
	/** Rows of the legacy adaptive-status rung (only when no server narrative answers). */
	private legacyAudit: AuditEntry[] = [];
	/** The city narrative's newest answer, oldest → newest. */
	private cityFeed: AuditEntry[] = [];
	private cityFeedAt = 0;
	private cityFeedSig = '';
	private eventAuditSig = '';
	private geometryAt = 0;
	private emuTrackTimer: ReturnType<typeof setInterval> | null = null;
	/** Events whose 'audit via the 2 s poll rung' note was already written. */
	private rungNoted = new Set<string>();
	/** The $effect.root that follows the shell's operator session (cleaned up on stop). */
	private sessionSync: (() => void) | null = null;
	/** The run ledger answered at least once (launch binding by run id). */
	private emulatorRunsRead = false;
	/** Escort outcomes already narrated (`approvalId:status`); seeded on the first read. */
	private escortOutcomesSeen = new Set<string>();
	private escortOutcomesSeeded = false;

	get selectedEvent(): CorridorEvent | null {
		return this.events.find((e) => e.eventId === this.selectedEventId) ?? null;
	}

	/**
	 * The band's provenance as it is PRINTED (the canvas AMA chip shows it
	 * verbatim): scoped to what it speaks for — the selected event's own
	 * situation log, or the city band — tagged SIMULATED FEED when its
	 * inputs are synthetic, and 'UNKNOWN — engine poll failing since <IST>'
	 * for a stale city band (which is never shown as GREEN).
	 */
	get bandSource(): string {
		const lang = this.lang;
		if (this.bandStaleSince != null) {
			return tOps(lang, 'bandSourceStale', `${istHm(this.bandStaleSince)} IST`);
		}
		const sim = this.bandSimulated === true ? ` · ${tOps(lang, 'bandSimulatedTag')}` : '';
		if (this.bandSourceKind === 'situation_log') return `${tOps(lang, 'bandScopeEvent')}${sim}`;
		if (this.bandSourceKind === 'city-band') return `${tOps(lang, 'bandScopeCity')}${sim}`;
		if (this.bandSourceKind === 'adaptive-status') return `${tOps(lang, 'bandScopeAdaptive')}${sim}`;
		return tOps(lang, 'bandScopeNone');
	}

	/**
	 * Gate on (VEER_OPERATOR_PIN) and no signed-in duty officer: operator
	 * reads are refused (403), so the wall pauses its pollers and shows one
	 * sign-in state instead of a refusal per panel.
	 */
	get signInRequired(): boolean {
		return this.sessionGate === true && !this.dutyOfficerFromSession;
	}

	eventById(id: string | null | undefined): CorridorEvent | null {
		return id ? (this.events.find((e) => e.eventId === id) ?? null) : null;
	}

	/** Closures for the map overlay: segment spine + colour per event kind. */
	get closures(): Array<{ eventId: string; segmentId: number; kind: string; live: boolean }> {
		const out: Array<{ eventId: string; segmentId: number; kind: string; live: boolean }> = [];
		for (const e of this.events) {
			const live = e.source !== 'offline-fixture';
			for (const s of e.routeSegmentIds) {
				out.push({ eventId: e.eventId, segmentId: s, kind: e.eventKind, live });
			}
		}
		return out;
	}

	/** Every open escort proposal (running escorts only) — newest first. */
	get escortProposals(): EscortProposal[] {
		return this.escorts
			.map((l) => l.proposal)
			.filter((p): p is EscortProposal => p !== null)
			.sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0));
	}

	/** The /escorts lane of one escort, when the last read answered and lists it. */
	private freshEscortLane(movementId: string): EscortLane | null {
		if (this.escortsDeployed !== true || this.escortsNote != null) return null;
		return this.escorts.find((l) => l.movementId === movementId) ?? null;
	}

	/** The escort's own status document, when it is this escort's and reports escort authority. */
	private ownEscortDoc(movementId: string, followed: FollowedEscortDoc | null | undefined): FollowedEscortDoc | null {
		return followed && followed.movementId === movementId && followed.proposalsReported ? followed : null;
	}

	/**
	 * The open proposal of one escort (null when none is open). The /escorts
	 * lanes answer when their last read succeeded and lists the escort; else
	 * the escort's OWN status document (C-J `pending_approval`), which a
	 * console following it passes — so the HUD and the planner rail show
	 * the proposal without GET /escorts (not deployed, failing or not read
	 * yet); else the lanes as last read.
	 */
	escortProposalFor(movementId: string | null | undefined, followed?: FollowedEscortDoc | null): EscortProposal | null {
		if (!movementId) return null;
		const lane = this.freshEscortLane(movementId);
		if (lane) return lane.proposal;
		const own = this.ownEscortDoc(movementId, followed);
		if (own) return own.proposal;
		return this.escorts.find((l) => l.movementId === movementId)?.proposal ?? null;
	}

	/** The newest closed proposal of one escort (the HUD's outcome line) — same sources, same order. */
	escortOutcomeFor(movementId: string | null | undefined, followed?: FollowedEscortDoc | null): EscortProposalOutcome | null {
		if (!movementId) return null;
		const own = this.freshEscortLane(movementId) ? null : this.ownEscortDoc(movementId, followed);
		const outcomes = own ? own.outcomes : (this.escorts.find((l) => l.movementId === movementId)?.outcomes ?? []);
		return outcomes.length ? outcomes[outcomes.length - 1] : null;
	}

	/** Road names of segment ids (signage order, three then '+n'). */
	private roadsText(ids: number[], names: string[] = []): string {
		const out: string[] = [];
		for (const n of [...names, ...ids.map((id) => this.roadName(id) ?? `seg ${id}`)]) {
			if (n && !out.includes(n)) out.push(n);
		}
		return out.length > 3 ? `${out.slice(0, 3).join(', ')} +${out.length - 3}` : out.join(', ') || '—';
	}

	/**
	 * An escort proposal as this console prints it, in its language, from the
	 * proposal's structured fields (the brain's English summary is the
	 * tooltip): the route or hold, the km, what it avoids, the closures.
	 */
	escortProposalText(p: EscortProposal): string {
		const lang = this.lang;
		const pct = p.peakCongestion != null ? Math.round(p.peakCongestion * 100) : '—';
		const change = p.proposed;
		if (p.kind === 'plan_b' || p.kind === 'reroute') {
			if (!change || change.replan) return tOps(lang, 'escReplan', this.roadsText(change?.avoids.length ? change.avoids : p.segmentIds));
			// via what is new on it (the roads taken instead); an older brain: the route ahead
			const via = change.viaRoadNames.length
				? this.roadsText([], change.viaRoadNames)
				: change.viaSegmentIds.length
					? this.roadsText(change.viaSegmentIds)
					: this.roadsText([], change.roadNames);
			const parts = [tOps(lang, 'escRouteVia', tOps(lang, `escKind_${p.kind}`), via)];
			if (change.newRemainingM != null && change.remainingM != null) {
				parts.push(
					tOps(lang, 'escKm', (change.newRemainingM / 1000).toFixed(1), (change.remainingM / 1000).toFixed(1)) +
						(change.longDetour ? ` · ${tOps(lang, 'escLongDetour')}` : ''),
				);
			}
			if (change.avoids.length) parts.push(tOps(lang, 'escAvoids', this.roadsText(change.avoids), pct));
			if (change.closuresNew.length || change.closuresLifted.length) {
				parts.push(tOps(lang, 'escClosures', change.closuresNew.length, change.closuresLifted.length));
			}
			return parts.join(' · ');
		}
		if (p.kind === 'hold') {
			const held = change?.holdSegmentIds.length ? change.holdSegmentIds : p.segmentIds;
			const sec = Math.round(change?.maxHoldSec ?? 0) || '—';
			return tOps(lang, p.trigger === 'phase_transition' ? 'escHoldDeparture' : 'escHold', this.roadsText(held), pct, sec);
		}
		if (p.kind === 'continue') return tOps(lang, 'escContinue', this.roadsText(p.segmentIds), pct);
		return p.summary ?? p.action.replace(/_/g, ' ');
	}

	/**
	 * The proposal's tag: 'AI PROPOSES' for a model's answer, 'RULE
	 * PROPOSES' when no model answered and the deterministic rule stood in —
	 * never an AI label on a rule's proposal.
	 */
	escortProposerTag(p: EscortProposal, queue = false): string {
		const rule = p.tier === 'deterministic' || p.tier === 'none';
		return tOps(this.lang, queue ? (rule ? 'escQueueBadgeRule' : 'escQueueBadge') : rule ? 'escRuleProposes' : 'escAiProposes');
	}

	/**
	 * Who proposed it, in plain words: 'decider · strategic tier ·
	 * confidence 0.93' (tier-0 memory, a novelty latch, a tactical fallback),
	 * or the deterministic rule that stood in for a model (no confidence).
	 */
	escortDeciderText(p: EscortProposal): string {
		const tier = p.tier ?? 'none';
		const label = ESCORT_TIERS.includes(tier) ? tOps(this.lang, `escTier_${tier}`) : tier.replace(/_/g, ' ');
		if (p.confidence == null) return label;
		return tOps(this.lang, 'escDecider', label, p.confidence.toFixed(2));
	}

	/**
	 * Why an officer is asked, when it says more than the decider line: a
	 * low confidence, a withheld (novel) answer, a degraded tier, a route
	 * that cannot be carried out. A confident decider's own sentence ('…
	 * an officer approves every switch') repeats the decider line and the
	 * authority note, so it is not printed twice (it stays in the audit).
	 */
	escortProposalWhy(p: EscortProposal): string | null {
		if (!p.reason) return null;
		const confident = (p.tier === 'decider' || p.tier === 'tier0_cache_hit') && /an officer approves every switch$/.test(p.reason);
		return confident ? null : p.reason;
	}

	/** How a closed proposal ended, in this console's language. */
	escortOutcomeText(o: EscortProposalOutcome): string {
		const lang = this.lang;
		const by = o.decidedBy ?? '—';
		if (o.status === 'approved') return tOps(lang, o.applied === false ? 'escOutcomeNotApplied' : 'escOutcomeApproved', by);
		if (o.status === 'rejected') return tOps(lang, 'escOutcomeRejected', by);
		if (o.status === 'superseded') return tOps(lang, 'escOutcomeSuperseded');
		if (o.status === 'expired') {
			return !o.expiryReason || o.expiryReason === ESCORT_TIMEOUT_REASON
				? tOps(lang, 'escOutcomeExpired')
				: tOps(lang, 'escOutcomeExpiredWhy', o.expiryReason);
		}
		return o.status;
	}

	/** Board counts in EVENTS (never per-segment entities): total / live / preview / active. */
	get eventCounts(): { total: number; live: number; preview: number; active: number } {
		const live = this.events.filter((e) => e.source !== 'offline-fixture');
		return {
			total: this.events.length,
			live: live.length,
			preview: this.events.length - live.length,
			active: live.filter((e) => e.state === 'active').length,
		};
	}

	// ─── names (geometry registry) ────────────────────────────────────────────

	junctionName(id: number | null | undefined): string | null {
		if (id == null) return null;
		return this.junctions.find((j) => j.junctionId === id)?.name ?? null;
	}

	roadName(segmentId: number | null | undefined): string | null {
		if (segmentId == null) return null;
		return this.segmentLines.find((s) => s.segmentId === segmentId)?.roadName ?? null;
	}

	// ─── plates (C6: masked unless a named duty officer is on console) ────────

	/** A plate as this console may print it. */
	plate(plate: string | null | undefined): string | null {
		if (!plate) return null;
		return this.officerOn ? plate : maskPlate(plate);
	}

	/** Free text (titles, recommendations, echoes) as this console may print it. */
	text(text: string | null | undefined): string {
		if (!text) return '';
		return this.officerOn ? text : maskPlatesInText(text);
	}

	/**
	 * An evidence object (an audit row's detail, a probe's tool args) as this
	 * console may print it: every string masked, a `plate` key replaced by
	 * its masked form — deep, bounded, never mutating the original.
	 */
	detail(value: unknown): unknown {
		return this.officerOn ? value : maskDetail(value);
	}

	/** Publish the board to consumers — raw for a named officer, plate-masked otherwise. */
	private publishBoard() {
		this.events = this.officerOn ? this.boardRows : this.boardRows.map(maskEventText);
	}

	/** Publish the interception plan to consumers (same rule as the board). */
	private publishInterception() {
		const raw = this.interceptionRaw;
		this.interception = raw && !this.officerOn ? maskPlanPlate(raw) : raw;
	}

	/** Publish the selected event's audit (the dossier narrative) — same rule. */
	private publishEventAudit() {
		this.eventAudit = this.officerOn ? this.eventAuditRows : this.eventAuditRows.map(maskAuditEntry);
	}

	/** Publish the probe answer's tail — masked before the cut without an officer. */
	private publishProbeAnswer() {
		this.ndjsonText = tailText(this.probeTokens, PROBE_TAIL_CHARS, this.officerOn);
	}

	/** Officer presence flipped: every published view changes its plate rule. */
	private onOfficerPresence(before: boolean) {
		if (before === this.officerOn) return;
		this.publishBoard();
		this.publishInterception();
		this.publishEventAudit();
		this.publishProbeAnswer();
		this.rebuildAudit();
	}

	// ─── lifecycle ────────────────────────────────────────────────────────────

	start() {
		this.holders++;
		if (this.timers.length) return;
		this.restorePreviewFlag();
		this.adoptLangCookie();
		// Every operator route that starts the store adopts the operator
		// gate's signed-in officer (the shell's session read), and follows it
		// — a sign-out releases the officer on every route.
		if (!this.sessionSync && typeof window !== 'undefined') {
			this.sessionSync = $effect.root(() => {
				$effect(() => {
					const gate = operatorSession.gate;
					const officer = operatorSession.officer;
					untrack(() => this.applySession(gate, officer));
				});
			});
		}
		// The gate's state first: an anonymous console under VEER_OPERATOR_PIN
		// polls nothing (every operator read would be refused).
		void this.readSession().then(() => this.pollEverything());
		const every = (name: string, fn: () => Promise<unknown>, ms: number) =>
			this.timers.push(setInterval(() => void this.tick(name, fn), ms));
		every('board', () => this.pollBoard(), POLL_MS.board);
		every('band', () => this.pollBand(), POLL_MS.band);
		every('cityband', () => this.pollCityBand(), POLL_MS.band);
		every('audit', () => this.pollAudit(), POLL_MS.audit);
		every('emulator', () => this.pollEmulator(), POLL_MS.emulator);
		every('enforcement', () => this.pollRecommendations(), POLL_MS.enforcement);
		every('approved', () => this.pollApproved(), POLL_MS.approved);
		every('dispatch', () => this.pollDispatches(), POLL_MS.dispatch);
		every('signals', () => this.pollSignalPlans(), POLL_MS.signals);
		every('watchlist', () => this.pollWatchlist(), POLL_MS.watchlist);
		every('ledger', () => this.pollDegradation(), POLL_MS.ledger);
		every('interception', () => this.pollInterception(), POLL_MS.interception);
		every('brain', () => this.pollBrain(), POLL_MS.brain);
		every('corridor', () => this.pollTrafficFeed(), POLL_MS.corridor);
		every('retention', () => this.pollRetention(), POLL_MS.retention);
		every('geometry', () => this.loadGeometry(), POLL_MS.geometry);
		every('escorts', () => this.pollEscorts(), ESCORT_POLL_MS);
		if (typeof document !== 'undefined') {
			// Back from a hidden tab: one catch-up round instead of waiting a tick.
			this.onVisibility = () => {
				if (!document.hidden) this.pollEverything();
			};
			document.addEventListener('visibilitychange', this.onVisibility);
		}
	}

	/**
	 * One poller tick: skipped while the operator gate wants a sign-in (an
	 * anonymous console reads nothing — no 403 storm), else the in-flight /
	 * hidden-page gate.
	 */
	private tick(name: string, fn: () => Promise<unknown>): Promise<boolean> {
		if (this.signInRequired) return Promise.resolve(false);
		return this.gate(name, fn);
	}

	/**
	 * The operator gate's state before the first poll round: the shell's
	 * session read (GET /api/session, shared with the sign-in control) —
	 * bounded; a deck without the route is a deck without the gate.
	 */
	private async readSession(): Promise<void> {
		if (this.sessionGate !== null || typeof fetch === 'undefined') return;
		await Promise.race([operatorSession.refresh(), new Promise((r) => setTimeout(r, 3000))]);
		this.applySession(operatorSession.gate, operatorSession.officer);
	}

	/**
	 * The operator gate's state (also pushed by the (deck) layout whenever
	 * the shell's session read answers): gate on → the signed-in officer is
	 * the duty officer (field locked); signing out releases it.
	 */
	applySession(gate: boolean | null, officer: string | null) {
		if (gate === null) return;
		const wasRequired = this.signInRequired;
		this.sessionGate = gate;
		if (gate) this.adoptSessionOfficer(officer);
		else if (this.dutyOfficerFromSession) this.adoptSessionOfficer(null);
		if (!this.signInRequired) this.sessionLapsed = false;
		// Signed in after an anonymous start: read everything now.
		if (wasRequired && !this.signInRequired && this.timers.length) this.pollEverything();
	}

	/**
	 * A read came back refused by the gate. With the gate known ON it is a
	 * lapsed session (expired / signed out elsewhere); before the session
	 * read has answered it is just the anonymous state, which the sign-in
	 * wall already says — never 'your session lapsed' to someone who never
	 * signed in.
	 */
	private noteRefusal(err: unknown): boolean {
		if (!isSignInRequired(err)) return false;
		if (this.sessionGate === true) this.sessionLapsed = true;
		return true;
	}

	stop() {
		this.holders = Math.max(0, this.holders - 1);
		if (this.holders > 0) return;
		this.sessionSync?.();
		this.sessionSync = null;
		for (const t of this.timers) clearInterval(t);
		this.timers = [];
		this.stopRunTracking();
		if (this.onVisibility && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', this.onVisibility);
		}
		this.onVisibility = null;
		this.socket?.close();
		this.socket = null;
		this.socketKey = null;
	}

	/**
	 * One gated round of every poller. The board goes first: the band and
	 * audit pollers stay idle until it settles (and selection kicks them),
	 * so the first band read is always scoped to the selected subject.
	 */
	private pollEverything() {
		void this.tick('board', () => this.pollBoard());
		void this.tick('band', () => this.pollBand());
		void this.tick('cityband', () => this.pollCityBand());
		void this.tick('audit', () => this.pollAudit());
		void this.tick('emulator', () => this.pollEmulator());
		void this.tick('enforcement', () => this.pollRecommendations());
		void this.tick('approved', () => this.pollApproved());
		void this.tick('dispatch', () => this.pollDispatches());
		void this.tick('signals', () => this.pollSignalPlans());
		void this.tick('watchlist', () => this.pollWatchlist());
		void this.tick('ledger', () => this.pollDegradation());
		void this.tick('interception', () => this.pollInterception());
		void this.tick('brain', () => this.pollBrain());
		void this.tick('corridor', () => this.pollTrafficFeed());
		void this.tick('retention', () => this.pollRetention());
		void this.tick('geometry', () => this.loadGeometry());
		void this.tick('escorts', () => this.pollEscorts());
	}

	// ─── board ────────────────────────────────────────────────────────────────

	/**
	 * One board read. `fresh` (after a lifecycle commit) never joins a GET
	 * that left before the write, and a read never lands over a newer one —
	 * so the board and ACTIVE EVENTS follow the click, not a stale poll.
	 *
	 * A BLIP — the live events API failing within BLIP_GRACE_MS of its last
	 * good answer (an oracle restart, one dropped request) — keeps the
	 * last-good LIVE board: no PREVIEW fixtures swapped in, no movements
	 * fallback, the 'updated' age left to grow (stale, said in the note).
	 * Past the grace it is an outage and the board falls back as before; a
	 * 404 (not deployed) or a gate refusal is never a blip.
	 */
	async pollBoard(fresh = false) {
		const seq = ++this.boardSeq;
		let live: CorridorEvent[] = [];
		let note: string | null = null;
		let deployed: boolean | null = null;
		let refused = false;
		let blip: string | null = null;
		try {
			live = await fetchActiveEvents({ fresh });
			deployed = true;
		} catch (err) {
			if (this.noteRefusal(err)) {
				// The operator gate refused the read: nothing is wrong upstream,
				// and no preview fixture stands in for a board nobody may read.
				refused = true;
				note = SIGN_IN_REQUIRED;
			} else if (!(err instanceof ApiNotDeployed) && withinBlipGrace(this.boardLiveAt)) {
				const age = Math.round((Date.now() - this.boardLiveAt) / 1000);
				blip = `events API blip (${err instanceof Error ? err.message : String(err)}) — last-good live board held (${age} s old)`;
			} else if (err instanceof ApiNotDeployed) {
				deployed = false;
				try {
					live = await fetchMovementsAsEvents();
					note = 'events API not deployed (404) — movements fallback + offline preview';
				} catch {
					note = 'events API not deployed (404) and movements API down — offline preview';
				}
			} else {
				try {
					live = await fetchMovementsAsEvents();
					note = `events API unreachable (${err instanceof Error ? err.message : String(err)}) — movements fallback + offline preview`;
				} catch {
					note = 'stack unreachable — offline preview';
				}
			}
		}
		if (seq < this.boardAppliedSeq) return; // a newer read already landed
		this.boardAppliedSeq = seq;
		if (blip) {
			// the live board stands as it was (rows, status, selection); only the note says why it ages
			this.boardNote = blip;
			return;
		}
		if (deployed === true) this.boardLiveAt = Date.now();
		this.eventsApiDeployed = deployed;
		// Preview fixtures stand in only when the live board cannot answer, or
		// when the operator asked for them — never padding a healthy board,
		// and never in place of a board the gate refused to show.
		const withFixtures = !refused && (this.previewFixtures || deployed !== true);
		const next = withFixtures ? [...live, ...fixtureEvents()] : live;
		if (deployed === true && this.previewFixtures) {
			note = 'PREVIEW on — labelled use-case fixtures shown beside the live board';
		}
		// Reassign only on change: consumers keyed on the array (canvas layers,
		// follow effects) must not re-run on a no-op poll.
		const sig = eventsSignature(next);
		if (sig !== this.eventsSig) {
			this.eventsSig = sig;
			this.boardRows = next;
			this.publishBoard();
		}
		// Keep the last-good timestamp when every live source is down.
		if (live.length || deployed === true || !note) this.boardUpdated = Date.now();
		if (deployed === true) this.sessionLapsed = false;
		this.boardNote = note;
		this.boardStatus =
			deployed === true ? (this.previewFixtures ? 'mixed' : 'live') : live.length ? 'mixed' : 'offline';
		const first = !this.boardLoaded;
		this.boardLoaded = true;
		this.ensureSelection(first);
		this.rebindSocket();
	}

	/**
	 * Keep a valid selection: when nothing (or a vanished event) is selected,
	 * auto-select through selectEvent — a live vehicle_pursuit first — so the
	 * band, audit and interception plan load exactly as for a click.
	 */
	private ensureSelection(firstLoad: boolean) {
		const current = this.selectedEventId;
		if (current && this.events.some((e) => e.eventId === current)) {
			if (firstLoad) this.selectEvent(current); // a pre-board selection loads now
			return;
		}
		const pick = preferredEvent(this.events);
		if (pick) {
			this.selectEvent(pick.eventId);
		} else if (current || firstLoad) {
			this.selectedEventId = null;
			this.resetSubject();
			void this.pollBand();
			void this.pollAudit();
		}
	}

	/** Operator PREVIEW toggle (labelled fixtures beside a live board). */
	setPreviewFixtures(on: boolean) {
		this.previewFixtures = on;
		try {
			if (on) localStorage.setItem(PREVIEW_KEY, '1');
			else localStorage.removeItem(PREVIEW_KEY);
		} catch {
			// storage unavailable (private mode) — the toggle still holds for the session
		}
		void this.pollBoard();
	}

	private restorePreviewFlag() {
		try {
			const flag = new URLSearchParams(window.location.search).get('preview');
			if (flag === '1' || flag === 'on') this.previewFixtures = true;
			else if (flag === '0' || flag === 'off') this.previewFixtures = false;
			else this.previewFixtures = localStorage.getItem(PREVIEW_KEY) === '1';
		} catch {
			// no window/storage (SSR, tests) — keep the default (off)
		}
	}

	/** Forget everything scoped to the previous subject. */
	private resetSubject() {
		this.subjectSeq++;
		this.band = null;
		this.bandHistory = [];
		this.bandSourceKind = 'none';
		this.bandSimulated = null;
		this.bandStaleSince = null;
		this.bandUpdated = 0;
		this.bandPolledAt = 0;
		this.bandCandidate = null;
		this.bandCommittedAt = null;
		this.heartbeatTicks = 0;
		this.lastAuditSignature = '';
		this.eventAuditRows = [];
		this.eventAudit = [];
		this.eventAuditFor = null;
		this.eventAuditSig = '';
		this.legacyAudit = [];
		this.interceptionToken++;
		this.interceptionRaw = null;
		this.interception = null;
		this.interceptionSig = '';
		this.interceptionNote = null;
		this.interceptionUpdated = 0;
		// The probe's answer box speaks for the event it asked about — never
		// under another one (its rows stay in that event's narrative).
		this.probeTokens = '';
		this.publishProbeAnswer();
		this.probeDone = null;
		this.rebuildAudit();
	}

	selectEvent(id: string) {
		if (id !== this.selectedEventId) {
			this.selectedEventId = id;
			this.resetSubject();
		}
		void this.pollBand();
		void this.pollAudit(true);
		void this.loadInterception(id);
		this.rebindSocket();
	}

	// ─── event lifecycle (contract C-F) ───────────────────────────────────────

	/**
	 * Move an event along its lifecycle (POST /events/{id}/status) under the
	 * named officer. One POST at a time (eventBusy) — a double click never
	 * sends two. A 409 comes back with the moves the server allows and is
	 * rendered inline on the card; on success the board, counters and audit
	 * refresh at once so ACTIVE EVENTS and the feed row follow the click.
	 */
	async transitionEvent(eventId: string, toStatus: EventTransitionTarget, note = ''): Promise<boolean> {
		if (this.eventBusy) return false;
		const ev = this.eventById(eventId);
		if (!hasLifecycle(ev)) {
			this.pushAudit(
				'operator',
				'error',
				'gate',
				`event ${toStatus} refused: ${isLiveUuidEvent(ev) ? 'a movements-fallback row is not a corridor event (no C-F lifecycle)' : 'preview fixtures have no lifecycle (select a live event)'}`,
				undefined,
				{ actorKind: 'system', eventId },
			);
			return false;
		}
		const officer = this.requireOfficerFor(`event ${toStatus}`);
		if (!officer) return false;
		this.eventBusy = eventId;
		this.pushAudit('operator', 'decision', officer, `${TRANSITION_VERB[toStatus]} ${ev.name}${note.trim() ? ` — ${clipText(note.trim(), 140)}` : ''}`, undefined, {
			actorKind: 'human',
			eventId,
			kind: 'event',
		});
		try {
			const outcome = await transitionEventStatus(eventId, { toStatus, officer, note });
			this.eventOutcome = outcome;
			if (outcome.ok) {
				await this.pollBoard(true);
				void this.pollAudit(true);
				if (outcome.closedDispatches) void this.pollDispatches();
				return true;
			}
			this.pushAudit(
				'operator',
				'error',
				'oracle',
				outcome.status === 409
					? `${toStatus} refused (409): ${outcome.detail ?? 'illegal transition'}`
					: clipText(`${toStatus} failed (${outcome.status || 'no answer'}): ${outcome.detail ?? ''}`, 240),
				outcome,
				{ actorKind: 'machine', eventId },
			);
			void this.pollBoard(true); // a 409 means the event moved on: read where it is now
			return false;
		} catch (err) {
			if (!(err instanceof CommitRefused)) {
				this.pushAudit('operator', 'error', 'deck', clipText(`${toStatus} not sent: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
					actorKind: 'system',
					eventId,
				});
			}
			return false;
		} finally {
			this.eventBusy = null;
		}
	}

	// ─── situation band ───────────────────────────────────────────────────────

	/**
	 * Band truth, in order:
	 *   1. the selected LIVE event's own situation_log (bandSourceKind
	 *      'situation_log'; band = the newest committed class; bandUpdated =
	 *      that row's timestamp_utc; SIMULATED FEED from the row's C-Q
	 *      simulated_feed, else the adaptive subject's);
	 *   2. otherwise the city band the signal writer acts on (GET /adaptive/
	 *      status city_band, kind 'city-band'). Its `since` — the moment the
	 *      band was ENTERED — is the held-since line (bandCommittedAt: 'held
	 *      2h03m since 01:47 IST', never reset by a re-read); bandUpdated is
	 *      the engine's newest evaluation (engine_loop.last_tick_age_sec, else
	 *      the read time), the same 'newest reading' every rung reports — so
	 *      a band held for hours is never aged as if its reading were stale.
	 *      A STALE city band (engine poll failing) is band null + 'UNKNOWN —
	 *      engine poll failing since <IST>' — never GREEN;
	 *   3. otherwise none (band null).
	 * The unscoped log is never read: it mixes completed events' history.
	 */
	async pollBand() {
		if (!this.boardLoaded) return;
		const seq = this.subjectSeq;
		const ev = this.selectedEvent;
		let log: SituationLogPoint[] = [];
		let logFailed = false;
		let logAnswered = false;
		if (isLiveEvent(ev)) {
			const subjects = [ev.eventId];
			if (ev.movementId && ev.movementId !== ev.eventId) subjects.push(ev.movementId);
			for (const subject of subjects) {
				try {
					log = await fetchSituationLog(subject);
					logAnswered = true;
				} catch {
					logFailed = true;
					log = [];
				}
				if (log.length) break;
			}
		}
		if (seq !== this.subjectSeq) return; // the selection moved on
		if (log.length) {
			this.applySituationLog(log);
		} else if (logFailed && !logAnswered && this.bandSourceKind === 'situation_log') {
			// transient read failure: keep the subject's last-good band; its
			// server timestamp ages honestly on every surface
		} else {
			await this.applyCityBand(seq);
		}
		if (seq === this.subjectSeq) void this.loadOverrides();
	}

	private applySituationLog(log: SituationLogPoint[]) {
		const newest = log[log.length - 1];
		this.band = newest.band;
		this.bandSourceKind = 'situation_log';
		this.bandStaleSince = null;
		// C-Q: the reading's own input provenance; until the oracle logs it,
		// the adaptive engine's subject (event, else its movement) says.
		const ev = this.selectedEvent;
		const subject =
			(ev && this.adaptiveSubjects.get(ev.eventId)) ?? (ev?.movementId ? this.adaptiveSubjects.get(ev.movementId) : undefined);
		this.bandSimulated = newest.simulatedFeed ?? subject?.simulatedFeed ?? null;
		this.bandUpdated = newest.ts;
		this.bandPolledAt = Date.now();
		this.bandHistory = log.slice(-BAND_CAP).map((p) => ({
			ts: p.ts,
			band: p.observed ?? p.band,
			severityScore: p.severityScore,
			source: 'situation_log' as const,
			committed: p.band,
			transitioned: p.transitioned,
		}));
		let committedAt: number | null = null;
		for (let i = log.length - 1; i >= 0; i--) {
			if (log[i].transitioned) {
				committedAt = log[i].ts;
				break;
			}
		}
		this.bandCommittedAt = committedAt;
		this.bandCandidate = bandCandidateOf(log);
	}

	private async applyCityBand(seq: number) {
		const doc = await this.cityBandDoc();
		if (seq !== this.subjectSeq) return;
		this.bandCandidate = null;
		this.bandCommittedAt = null;
		if (doc?.stale) {
			// The engine poll behind the band is failing: the last band is not
			// the city's band any more. UNKNOWN, labelled with since when.
			this.band = null;
			this.bandSourceKind = 'city-band';
			this.bandStaleSince = doc.staleSince;
			this.bandSimulated = doc.simulatedFeed;
			this.bandUpdated = 0;
			this.bandPolledAt = this.cityBandUpdated;
			this.bandHistory = [];
		} else if (doc?.band) {
			if (this.bandSourceKind !== 'city-band') this.bandHistory = [];
			this.band = doc.band;
			this.bandSourceKind = 'city-band';
			this.bandStaleSince = null;
			this.bandSimulated = doc.simulatedFeed;
			// Newest reading = the engine's last evaluation (else the read);
			// how long the band has HELD is its entry time, on its own line.
			this.bandUpdated = doc.evaluatedAt ?? this.cityBandUpdated;
			this.bandCommittedAt = doc.since;
			this.bandPolledAt = this.cityBandUpdated;
			this.appendBandPoint(doc.band, null, 'city-band', this.cityBandUpdated);
		} else {
			this.band = null;
			this.bandSourceKind = 'none';
			this.bandStaleSince = null;
			this.bandSimulated = null;
			this.bandUpdated = 0;
			this.bandPolledAt = 0;
			this.bandHistory = [];
		}
	}

	/** The city band document: the cached read when fresh, else a read. */
	private async cityBandDoc(): Promise<CityBandDoc | null> {
		const fresh = this.cityBandUpdated && Date.now() - this.cityBandUpdated < POLL_MS.band * 1.5;
		if (!fresh) await this.pollCityBand();
		return this.cityBand;
	}

	/** Adaptive subjects by id (event or movement): their band and feed provenance. */
	private adaptiveSubjects = new Map<string, { simulatedFeed: boolean | null; bandSince: number | null }>();

	/**
	 * GET /adaptive/status → the city band (band, entered-since, simulated
	 * feed, stale) plus each subject's simulated_feed. An oracle without
	 * city_band falls back to the signal writer's /signals/plans city_band
	 * (no since / stale there: its age is the read time, said so).
	 */
	async pollCityBand() {
		try {
			const snap = await fetchAdaptiveStatus();
			this.adaptiveSubjects = new Map(snap.movements.map((m) => [m.id, { simulatedFeed: m.simulatedFeed, bandSince: m.bandSince }]));
			let doc = snap.cityBand;
			if (!doc) {
				const plans = this.signalPlans && Date.now() - this.signalPlansUpdated < POLL_MS.signals * 1.5 ? this.signalPlans : null;
				if (!plans) await this.pollSignalPlans();
				doc = this.signalPlans?.cityBand
					? { band: this.signalPlans.cityBand, since: null, subjects: this.signalPlans.bandSubjects, simulatedFeed: null, stale: false, staleSince: null, staleReason: null }
					: null;
			}
			// A stale band always says since when its poll began failing: the
			// oracle's stale_since, else the first read that saw it stale (it
			// began no later) — never the moment the band was entered.
			if (doc?.stale && doc.staleSince == null) {
				doc = { ...doc, staleSince: this.cityBand?.stale ? (this.cityBand.staleSince ?? Date.now()) : Date.now() };
			}
			this.cityBand = doc;
			this.cityBandUpdated = Date.now();
			this.cityBandNote = null;
		} catch (err) {
			this.noteRefusal(err);
			this.cityBandNote = err instanceof ApiNotDeployed ? 'adaptive status not deployed (404)' : `city band unavailable: ${err instanceof Error ? err.message : String(err)}`;
			// keep the last-good doc; its read time ages on every surface
		}
	}

	/**
	 * Client-accumulated readings of a live-computed band (no server history
	 * exists for it): a point on change, plus a heartbeat every 5th read so
	 * the sparkline shows how long the band has held as observed here.
	 */
	private appendBandPoint(band: BandCode, severity: number | null, source: BandPoint['source'], ts: number) {
		const last = this.bandHistory[this.bandHistory.length - 1];
		this.heartbeatTicks++;
		if (last && last.band === band && this.heartbeatTicks % 5 !== 0) return;
		if (last && last.ts === ts) return;
		this.bandHistory = [...this.bandHistory.slice(-(BAND_CAP - 1)), { ts, band, severityScore: severity, source }];
	}

	/**
	 * Read-only override evidence (GET /adaptive/overrides) for the selected
	 * live event, else city-wide. The newest row feeds the AMA evidence; the
	 * side-effectful GET /adaptive/params is never polled (it persists a
	 * param_overrides row and creates subject state per call on today's
	 * oracle).
	 */
	private async loadOverrides() {
		const ev = this.selectedEvent;
		try {
			const rows = await fetchOverrides(isLiveUuidEvent(ev) ? ev.eventId : null);
			this.overrides = rows;
			this.overridesNote = null;
			this.overridesUpdated = Date.now();
			const newest = rows[0] ?? null;
			this.bandEvidence = newest
				? {
						shadowEval: newest.shadowEval,
						floorApplied: newest.floorApplied,
						failClosedReason:
							newest.failClosedReason ??
							(newest.shadowEval && newest.shadowEval !== 'passed' && newest.shadowEvalDetail !== 'shadow_eval_not_required'
								? (newest.shadowEvalDetail ?? null)
								: null),
						situationClass: newest.situationClass,
					}
				: null;
		} catch (err) {
			this.overridesNote =
				err instanceof ApiNotDeployed
					? 'overrides endpoint not deployed (404) — no shadow-eval evidence yet'
					: `overrides unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	// ─── audit stream (a view over server data) ───────────────────────────────

	/**
	 * A LOCAL row: something the server cannot know (a refusal before the
	 * wire, a transport error, probe output, a deck note) or an officer echo
	 * that retires when its server row lands. `actorKind` is explicit — an
	 * officer's own request is human; oracle/twin/writer/deck replies never.
	 */
	private pushAudit(
		source: AuditEntry['source'],
		level: AuditEntry['level'],
		actor: string,
		text: string,
		detail?: unknown,
		opts?: { actorKind?: ActorKind; eventId?: string | null; kind?: string | null },
	) {
		const actorKind = opts?.actorKind ?? (source === 'operator' && level === 'decision' ? 'human' : 'machine');
		this.localAudit = [
			...this.localAudit.slice(-(LOCAL_AUDIT_CAP - 1)),
			{
				id: ++this.auditSeq,
				ts: Date.now(),
				source,
				level,
				actor,
				text,
				detail,
				actorKind,
				eventId: opts?.eventId ?? null,
				kind: opts?.kind ?? null,
			},
		];
		this.rebuildAudit();
	}

	/** Server rows → audit entries (stable ids: a server row keeps its entry id). */
	private serverEntries(rows: ServerAuditRow[], prev: AuditEntry[]): AuditEntry[] {
		const known = new Map(prev.filter((e) => e.serverId).map((e) => [e.serverId as string, e.id]));
		return rows.map((r) => ({
			id: known.get(r.id) ?? ++this.auditSeq,
			ts: r.ts,
			source: 'audit-poll' as const,
			level: auditLevelOf(r),
			actor: r.actor ?? r.actorKind,
			text: clipText(r.text, 600),
			detail: r,
			actorKind: r.actorKind,
			serverId: r.id,
			kind: r.kind,
			emulated: r.source === 'emulated',
			simulated: r.simulated === true,
			severity: r.severity,
			urns: r.urns,
		}));
	}

	/**
	 * Recompute the merged view from the active scope + local rows, and
	 * publish it under the plate rule. The EVENT view keeps only the local
	 * rows of the selected event plus context-free machine/system rows
	 * (refusals, transport errors): an officer's echo about another event,
	 * or about a city-scope write (an e-challan, an earmark, RESTORE ALL),
	 * lands in another narrative — here it would never retire.
	 */
	private rebuildAudit() {
		const ev = this.selectedEvent;
		let server: AuditEntry[] = [];
		let local = this.localAudit;
		if (this.auditView === 'event') {
			// only the SELECTED event's rows — never another subject's narrative
			if (isLiveUuidEvent(ev) && this.eventAuditFor === ev.eventId) server = this.eventAuditRows;
			const id = ev?.eventId ?? null;
			local = local.filter((e) => (e.eventId == null ? !(e.source === 'operator' && e.actorKind === 'human') : e.eventId === id));
		} else if (this.auditView === 'city') server = this.cityFeed;
		else if (this.auditView === 'legacy') server = this.legacyAudit;
		const merged = mergeAuditView(server, local);
		this.audit = this.officerOn ? merged : merged.map(maskAuditEntry);
	}

	/** Operator scope toggle: follow the selected event, or the whole city. */
	setAuditScope(scope: AuditScope) {
		if (scope === this.auditScope) return;
		this.auditScope = scope;
		this.cityFeedAt = 0; // a fresh read for the new scope
		void this.pollAudit(true);
	}

	/**
	 * One audit round. The selected LIVE event's audit is read every tick
	 * (the dossier narrative needs it whichever scope the stream follows);
	 * the city feed every POLL_MS.feed while the stream follows the city or
	 * no live event is selected. Neither answering (an oracle without C-C)
	 * falls to the legacy adaptive-status rung.
	 */
	async pollAudit(force = false) {
		if (!this.boardLoaded) return;
		const seq = this.subjectSeq;
		const ev = this.selectedEvent;
		const liveEvent = isLiveUuidEvent(ev) ? ev : null;
		let eventOk = false;
		if (liveEvent) {
			try {
				const rows = await fetchEventAudit(liveEvent.eventId, AUDIT_SERVER_LIMIT);
				if (seq !== this.subjectSeq) return;
				const sig = JSON.stringify(rows.map((r) => r.id));
				if (sig !== this.eventAuditSig || this.eventAuditFor !== liveEvent.eventId) {
					this.eventAuditSig = sig;
					this.eventAuditRows = this.serverEntries(rows, this.eventAuditRows);
					this.publishEventAudit();
				}
				this.eventAuditFor = liveEvent.eventId;
				eventOk = true;
			} catch {
				if (seq !== this.subjectSeq) return;
				// not deployed / event gone — the city narrative stands in
			}
		}
		const wantCity = !liveEvent || this.auditScope === 'city' || !eventOk;
		let cityOk = this.cityFeed.length > 0 && Date.now() - this.cityFeedAt < POLL_MS.feed * 3;
		if (wantCity && (force || Date.now() - this.cityFeedAt >= POLL_MS.feed)) {
			try {
				const rows = await fetchCityFeed(AUDIT_SERVER_LIMIT);
				const sig = JSON.stringify(rows.map((r) => r.id));
				if (sig !== this.cityFeedSig) {
					this.cityFeedSig = sig;
					this.cityFeed = this.serverEntries(rows, this.cityFeed);
				}
				this.cityFeedAt = Date.now();
				cityOk = true;
			} catch {
				cityOk = false;
			}
		}
		if (seq !== this.subjectSeq) return;
		if (eventOk && this.auditScope === 'event') {
			this.auditView = 'event';
		} else if (wantCity && cityOk) {
			this.auditView = 'city';
		} else if (eventOk) {
			this.auditView = 'event';
		} else {
			await this.pollLegacyAudit(seq);
			return;
		}
		this.auditServerUpdated = Date.now();
		this.auditNote = null;
		this.legacyAudit = [];
		if (this.auditRung === 'unavailable' || this.auditRung === 'connecting') this.auditRung = 'poll';
		this.rebuildAudit();
	}

	/** The pre-C-C rung: the adaptive status snapshot as audit rows. */
	private async pollLegacyAudit(seq: number) {
		try {
			const snap = await pollAuditTail(null);
			if (seq !== this.subjectSeq) return;
			const a = snap.adaptive;
			if (!a) return;
			const bandParts = a.movements
				.filter((m) => m.situationCode)
				.map((m) => `${m.id.slice(0, 8)}=${m.situationCode}${m.severityScore != null ? `(${m.severityScore.toFixed(0)})` : ''}`);
			const sig = JSON.stringify([a.degradationTier, a.lastShadowEval, a.auditTrail.length, bandParts]);
			if (sig !== this.lastAuditSignature) {
				this.lastAuditSignature = sig;
				const now = Date.now();
				const rows: AuditEntry[] = [];
				const add = (level: AuditEntry['level'], actor: string, text: string, detail?: unknown) =>
					rows.push({ id: ++this.auditSeq, ts: now, source: 'audit-poll', level, actor, text, detail, actorKind: 'machine' });
				if (bandParts.length) add('band', 'adaptive', `situation snapshot · ${bandParts.join(' · ')}`);
				add('info', 'adaptive', `degradation=${a.degradationTier} shadow_eval=${a.lastShadowEval ?? 'n/a'} audit_trail=${a.auditTrail.length}`);
				for (const row of a.auditTrail.slice(-4)) add('info', 'degradation', clipText(JSON.stringify(row), 300), row);
				this.legacyAudit = [...this.legacyAudit, ...rows].slice(-60);
			}
			this.auditView = 'legacy';
			this.auditServerUpdated = Date.now();
			this.auditNote = 'no C-C narrative on this oracle — legacy adaptive-status rung';
			if (this.auditRung === 'unavailable' || this.auditRung === 'connecting') this.auditRung = 'poll';
			this.rebuildAudit();
		} catch {
			// Poll rung itself down — WS rung may still carry the stream.
			if (!this.socket && this.auditRung !== 'unavailable') {
				this.auditRung = 'unavailable';
				this.auditView = 'none';
				this.pushAudit('audit-poll', 'error', 'deck', 'audit poll unreachable and no WS channel open', undefined, {
					actorKind: 'system',
				});
			}
		}
	}

	private rebindSocket() {
		const ev = this.selectedEvent;
		const key = ev ? `${ev.eventId}::${ev.source}` : null;
		if (key === this.socketKey) return;
		this.socket?.close();
		this.socket = null;
		this.socketKey = key;
		if (!isLiveEvent(ev)) return;
		if (!DEV_SERVER) {
			// The adapter-node origin cannot upgrade WebSockets (hooks.server.ts):
			// the ladder would only hang to its timeout. The 2 s poll rung IS the
			// channel here — said once per event, not on every reselect.
			if (this.rungNoted.has(ev.eventId)) return;
			this.rungNoted.add(ev.eventId);
			this.pushAudit(
				'audit-poll',
				'info',
				'deck',
				`audit for ${ev.eventId.slice(0, 8)} via the 2 s poll rung (this origin serves no WebSocket upgrade)`,
				undefined,
				{ actorKind: 'system', eventId: ev.eventId },
			);
			return;
		}
		this.pushAudit('operator', 'info', 'deck', `subscribed to audit channel for ${ev.eventId}`, undefined, { actorKind: 'system' });
		const sock = createAuditSocket(
			ev.eventId,
			(wsEvent: BrainWsEvent) => this.onWsEvent(wsEvent),
			(rung) => {
				this.auditRung = rung === 'connecting' ? 'connecting' : (rung as AuditLadderState);
				if (rung !== 'connecting') {
					this.pushAudit(
						runsSource(rung),
						'info',
						'deck',
						rung === 'unavailable'
							? 'WS ladder exhausted — falling back to 2 s audit poll'
							: `audit channel connected via ${rung}`,
						undefined,
						{ actorKind: 'system' },
					);
				}
			},
			WS_OPEN_TIMEOUT_MS,
		);
		this.socket = sock;
	}

	private onWsEvent(ev: BrainWsEvent) {
		const payload = ev.payload ?? {};
		const rung = this.auditRung === 'ws-event' ? 'ws-event' : 'ws-convoy';
		switch (ev.type) {
			case 'convoy.status':
			case 'event.status':
				this.pushAudit(rung, 'info', 'brain', `${ev.type} ${clipText(JSON.stringify(payload), 240)}`, payload);
				break;
			case 'convoy.position':
			case 'event.position':
				// high-frequency — only every 6th frame keeps the log readable
				if (this.auditSeq % 6 === 0) {
					this.pushAudit(rung, 'info', 'tracking', `position ${clipText(JSON.stringify(payload), 160)}`);
				}
				break;
			case 'situation.changed':
				this.pushAudit('ws-event', 'band', 'adaptive', `band → ${String(payload['band'] ?? payload['situation_class'] ?? '?')}`, payload);
				break;
			default:
				this.pushAudit(rung, 'info', 'brain', `${ev.type} ${clipText(JSON.stringify(payload), 200)}`, payload);
		}
	}

	/**
	 * On-demand NDJSON probe into the brain's audit channel for the SELECTED
	 * event (its movement id + protection class): decider (strategic) →
	 * tactical (Gemma) → tools. Every row it writes is a machine row.
	 */
	async runAuditProbe(message: string) {
		if (this.auditProbeRunning) return;
		this.auditProbeRunning = true;
		this.probeTokens = '';
		this.publishProbeAnswer();
		const ev = this.selectedEvent;
		const movementId = ev?.movementId && UUID_RE.test(ev.movementId) ? ev.movementId : null;
		const vvipClass = ev && ev.protectionClass !== 'none' ? ev.protectionClass : null;
		// Every row of this probe belongs to the probed event's narrative.
		const eventId = ev?.eventId ?? null;
		// The answer's opening (the ledger row) — kept whole even if the raw tail rolls.
		let head = '';
		// The box follows this probe only while its event stays selected.
		const seq = this.subjectSeq;
		// A read, not a commit: an unnamed console may ask — and the ledger says so.
		this.pushAudit(
			'operator',
			'info',
			this.officerOn ? this.dutyOfficer.trim() : 'operator (unnamed)',
			`audit probe: "${clipText(message, 140)}"${ev ? ` · ${clipText(ev.name, 60)}` : ''}`,
			undefined,
			{ actorKind: 'human', eventId },
		);
		try {
			await streamAuditProbe(message, { movementId, vvipClass, eventId: isLiveUuidEvent(ev) ? ev.eventId : null, eventKind: ev?.eventKind ?? null }, {
				onThought: (text, tier) => {
					this.pushAudit('ndjson-chat', 'decision', probeActorOfTier(tier), clipText(text, 400), undefined, { actorKind: 'machine', eventId });
				},
				onToken: (text) => {
					if (head.length < 1000) head = (head + text).slice(0, 1000);
					if (seq !== this.subjectSeq) return;
					this.probeTokens = (this.probeTokens + text).slice(-PROBE_RAW_CAP);
					this.publishProbeAnswer();
				},
				onToolCall: (name, args) => {
					this.pushAudit('ndjson-chat', 'tool', `mcp:${name}`, `call ${clipText(JSON.stringify(args), 200)}`, args, { actorKind: 'machine', eventId });
				},
				onToolResult: (name, ok, summary) => {
					this.pushAudit('ndjson-chat', ok ? 'tool' : 'error', `mcp:${name}`, `${ok ? 'result' : 'failed'} · ${clipText(summary, 200)}`, undefined, {
						actorKind: 'machine',
						eventId,
					});
				},
				onDone: (done) => {
					if (head) this.pushAudit('ndjson-chat', 'model', 'answer', clipText(head, 600), undefined, { actorKind: 'machine', eventId });
					if (done) {
						if (seq === this.subjectSeq) this.probeDone = { tiersUsed: done.tiersUsed, degraded: done.degraded, at: Date.now() };
						for (const alert of done.operatorAlerts.slice(0, 3)) {
							this.pushAudit('ndjson-chat', 'band', 'brain', `operator alert: ${clipText(alert, 200)}`, undefined, { actorKind: 'machine', eventId });
						}
					}
				},
				onError: (msg) => {
					this.pushAudit('ndjson-chat', 'error', 'chat/stream', clipText(msg, 300), undefined, { actorKind: 'system', eventId });
				},
			});
		} finally {
			this.auditProbeRunning = false;
		}
	}

	/** Clear the LOCAL rows (probe output, notes); server rows are the record. */
	clearAudit() {
		this.localAudit = [];
		this.lastAuditSignature = '';
		this.legacyAudit = [];
		this.probeTokens = '';
		this.publishProbeAnswer();
		this.rebuildAudit();
	}

	// ─── twin compare (on demand only) ────────────────────────────────────────

	twinCacheKey(): string {
		const u = useCaseById(this.twinScenarioId);
		const b = u?.baselinePolicy ?? { advanceClosureSec: 120, closureType: 'full_closure' as const };
		return [
			this.twinScenarioId,
			b.advanceClosureSec,
			b.closureType,
			this.twinCandidate.advanceClosureSec,
			this.twinCandidate.closureType,
			u?.seed ?? 0,
		].join('|');
	}

	async runTwinCompare(force = false) {
		if (this.twinRunning) return;
		const key = this.twinCacheKey();
		if (!force && this.twinCache.has(key)) {
			this.twinResult = this.twinCache.get(key) ?? null;
			this.twinCachedKey = key;
			return;
		}
		const u = useCaseById(this.twinScenarioId);
		if (!u) {
			this.twinError = `unknown scenario: ${this.twinScenarioId}`;
			return;
		}
		this.twinRunning = true;
		this.twinError = null;
		this.pushAudit('operator', 'info', 'twin', `twin shadow-eval requested: ${u.scenarioId} candidate ${this.twinCandidate.advanceClosureSec}s/${this.twinCandidate.closureType}`, undefined, {
			actorKind: 'machine',
		});
		try {
			let result: TwinCompareResult;
			try {
				result = await compareTwinPolicies(u.scenarioId, u.baselinePolicy, this.twinCandidate, u.seed);
			} catch (err) {
				if (err instanceof ApiNotDeployed) {
					// Labelled fallback: two explicit-field /simulate/twin arms,
					// NO scenario_id — explicit policies are honoured on this path.
					result = await compareTwinPoliciesDirect(
						u.scenarioId,
						u.baselinePolicy,
						this.twinCandidate,
						{
							routeSegmentIds: u.routeSegmentIds,
							departureTime: u.departureTime,
							horizonMin: u.horizonMin,
							nReplications: u.nReplications,
							seed: u.seed,
						},
					);
				} else {
					throw err;
				}
			}
			this.twinCache.set(key, result);
			this.twinCache.set(`${key}#${result.source}`, result);
			this.twinResult = result;
			this.twinCachedKey = key;
			this.pushAudit(
				'operator',
				'info',
				'twin',
				`compare complete (${result.source}): disruption Δ=${fmtNum(result.deltas['disruptionVehicleHours'])} transit Δ=${fmtNum(result.deltas['transitTimeSec'])}`,
				undefined,
				{ actorKind: 'machine' },
			);
		} catch (err) {
			this.twinError = err instanceof Error ? err.message : String(err);
			this.pushAudit('operator', 'error', 'twin', `compare failed: ${this.twinError}`, undefined, { actorKind: 'machine' });
		} finally {
			this.twinRunning = false;
		}
	}

	// ─── emulator (contract C-G; tolerant of the synchronous build) ───────────

	async pollEmulator() {
		const health = await emulatorHealth();
		this.emulator = {
			...this.emulator,
			online: health.online,
			healthDetail: health.detail,
			lastChecked: Date.now(),
			stack: health.stack,
			livingCitySupported: health.livingCity !== null,
		};
		if (!health.online) return;
		try {
			const [library, runs] = await Promise.all([emulatorUseCases(), emulatorListRuns(8)]);
			this.emulatorLibrary = library;
			this.emulatorUseCaseIds = library.map((u) => u.id);
			// The list may not carry a running run's event_status / reason (the
			// per-run read does): keep what the tracker last read for it.
			const prev = new Map(this.emulatorRuns.map((r) => [r.runId, r]));
			this.emulatorRuns = runs.map((r) => {
				const p = prev.get(r.runId);
				return p && r.eventStatus == null && r.status === p.status
					? { ...r, eventStatus: p.eventStatus ?? null, failReason: r.failReason ?? p.failReason ?? null }
					: r;
			});
			this.emulatorRunsRead = true;
			this.reconcileLaunch(runs);
		} catch {
			// health said online but the API probes failed — keep last-good
		}
		if (health.livingCity) {
			try {
				this.livingCity = await emulatorLivingCity();
				this.livingCityNote = null;
			} catch (err) {
				this.livingCityNote = err instanceof Error ? err.message : String(err);
			}
		} else {
			this.livingCity = null;
			this.livingCityNote = null;
		}
	}

	/** The library entry for a use case (the emulator's, else the deck's fixture name). */
	useCaseInfo(id: string): EmulatorUseCaseInfo | null {
		const hit = this.emulatorLibrary.find((u) => u.id === id);
		if (hit) return hit;
		const fx = useCaseById(id);
		return fx
			? {
					id: fx.id,
					name: fx.name,
					category: null,
					vvipClass: fx.protectionClass,
					horizonMin: fx.horizonMin,
					seed: fx.seed,
					routeSegmentIds: fx.routeSegmentIds,
					hero: isHeroUseCase(fx.id, null),
				}
			: null;
	}

	/** The clock a launch actually uses: hero tapes are capped at ×10. */
	launchClock(useCaseId: string, requested: number): number {
		const scale = Math.max(1, Math.round(requested));
		return this.useCaseInfo(useCaseId)?.hero ? Math.min(scale, EMULATOR_HERO_CLOCK_CAP) : scale;
	}

	/**
	 * Launch a tape. C-G answers 202 {run_id} at once and the run is tracked
	 * by id; an older build holds the POST until the tape ends — the request
	 * is raced, never awaited by the UI, and the run is found in the ledger.
	 */
	async launchUseCase(useCaseId: string, clockScale: number) {
		if (this.emulatorBusy === 'launch') return;
		const scale = this.launchClock(useCaseId, clockScale);
		this.emulatorBusy = 'launch';
		this.emulatorActionNote = null;
		const startedAt = Date.now();
		this.emulatorLaunch = {
			kind: 'launch',
			useCaseId,
			clockScale: scale,
			runId: null,
			phase: 'posting',
			contract: null,
			detail: null,
			at: startedAt,
			ledgerMaxRunId: this.ledgerMaxRunId(),
		};
		this.pushAudit('operator', 'info', 'emulator', `launch ${useCaseId} ×${scale} (EMULATED causes)`, undefined, { actorKind: 'system' });
		await this.racePost(emulatorLaunchRun(useCaseId, scale), startedAt);
	}

	/** Replay a recorded tape (same contract handling as a launch). */
	async replayRun(runId: number, clockScale = 10) {
		if (this.emulatorBusy) return;
		const row = this.emulatorRuns.find((r) => r.runId === runId);
		const useCaseId = row?.useCaseId ?? '—';
		const scale = this.launchClock(useCaseId, clockScale);
		this.emulatorBusy = `replay:${runId}`;
		this.emulatorActionNote = null;
		const startedAt = Date.now();
		this.emulatorLaunch = {
			kind: 'replay',
			useCaseId,
			clockScale: scale,
			runId: null,
			phase: 'posting',
			contract: null,
			detail: `replay of run #${runId}`,
			at: startedAt,
			ledgerMaxRunId: this.ledgerMaxRunId(),
		};
		this.pushAudit('operator', 'info', 'emulator', `replay run #${runId} ×${scale} (EMULATED causes)`, undefined, { actorKind: 'system' });
		await this.racePost(emulatorReplayRun(runId, scale), startedAt);
	}

	/** The newest run id the ledger has shown (null = it was never read). */
	private ledgerMaxRunId(): number | null {
		if (!this.emulatorRunsRead) return null;
		return this.emulatorRuns.reduce((max, r) => Math.max(max, r.runId), 0);
	}

	/** Back-compat name for the launch control (older panels call it). */
	async stageUseCase(useCaseId: string, clockScale?: number) {
		await this.launchUseCase(useCaseId, clockScale ?? 10);
	}

	private async racePost(post: Promise<EmulatorPostResult>, startedAt: number) {
		const raced = await Promise.race([
			post.then((r) => ({ settled: true as const, r })),
			new Promise<{ settled: false }>((resolve) => setTimeout(() => resolve({ settled: false }), LAUNCH_RACE_MS)),
		]);
		this.emulatorBusy = null;
		if (raced.settled) {
			this.applyPostResult(raced.r, startedAt);
		} else {
			// A synchronous build is holding the request until the tape ends:
			// the run row exists already — find it in the ledger and track it.
			// (The panel explains this state in the operator's language.)
			if (this.emulatorLaunch) {
				this.emulatorLaunch = { ...this.emulatorLaunch, phase: 'running', contract: 'sync', detail: null };
			}
			void post.then((r) => this.applyPostResult(r, startedAt));
		}
		void this.pollEmulator();
		this.startRunTracking();
	}

	private applyPostResult(r: EmulatorPostResult, startedAt: number) {
		const launch = this.emulatorLaunch;
		if (!launch || launch.at !== startedAt) return; // a newer launch replaced it
		if (r.kind === 'accepted') {
			const async = r.httpStatus === 202;
			this.emulatorLaunch = {
				...launch,
				runId: r.runId ?? launch.runId,
				phase: async ? 'running' : runPhase(r.status),
				contract: async ? 'c-g' : 'sync',
				detail: async ? `202 accepted — run #${r.runId ?? '?'} running` : `${r.httpStatus}: run finished (${r.status})`,
			};
			this.emulator = { ...this.emulator, runRequested: launch.useCaseId, runError: null };
		} else if (r.kind === 'conflict') {
			this.emulatorLaunch = {
				...launch,
				runId: r.runId,
				phase: 'refused',
				contract: 'c-g',
				detail: `409 — ${launch.useCaseId} is already running${r.runId != null ? ` as run #${r.runId}` : ''}`,
			};
		} else if (launch.contract === 'sync' && launch.phase === 'running') {
			// The held request finally failed — the deck proxy stops waiting
			// on an upstream after 60 s (a 502 in the browser console) — but
			// the run itself keeps going; the ledger says how it ends.
			this.emulatorLaunch = {
				...launch,
				detail: `the held launch request ended without an answer (${r.httpStatus || 'no status'}; the deck proxy waits 60 s) — the run continues, followed from the ledger`,
			};
		} else {
			this.emulatorLaunch = { ...launch, phase: 'failed', detail: `${r.httpStatus || 'no answer'}: ${r.detail}` };
			this.emulator = { ...this.emulator, runError: r.detail };
			this.pushAudit('operator', 'error', 'emulator', clipText(`launch ${launch.useCaseId} refused (${r.httpStatus}): ${r.detail}`, 240), undefined, {
				actorKind: 'system',
			});
		}
		void this.pollEmulator();
	}

	/**
	 * Match the panel's launch to its ledger row (sync builds return no id):
	 * the OLDEST run of that tape newer than every run the ledger showed
	 * before the launch — ids, never clocks, so a skewed browser or
	 * emulator clock cannot hide the run. Only without a prior ledger read
	 * does the creation time decide (a generous window).
	 */
	private reconcileLaunch(runs: EmulatorRunRow[]) {
		const launch = this.emulatorLaunch;
		if (!launch) return;
		let row = launch.runId != null ? runs.find((r) => r.runId === launch.runId) : undefined;
		if (!row && launch.runId == null && launch.phase !== 'failed') {
			const mine = runs.filter((r) => r.useCaseId === launch.useCaseId && r.isReplay === (launch.kind === 'replay'));
			const floor = launch.ledgerMaxRunId;
			row =
				floor != null
					? mine.filter((r) => r.runId > floor).sort((a, b) => a.runId - b.runId)[0]
					: mine.find((r) => Date.parse(r.createdAt ?? '') >= launch.at - 120_000);
		}
		if (!row) return;
		const phase = runPhase(row.status);
		if (launch.runId !== row.runId || launch.phase !== phase) {
			this.emulatorLaunch = { ...launch, runId: row.runId, phase: launch.phase === 'refused' ? 'refused' : phase };
		}
		if (phase !== 'running') this.stopRunTracking();
	}

	/** Follow the tracked run every 2 s (C-G progress) until it ends. */
	private startRunTracking() {
		if (this.emuTrackTimer || typeof setInterval === 'undefined') return;
		this.emuTrackTimer = setInterval(() => void this.gate('emulator-run', () => this.trackRun()), POLL_MS.emulatorRun);
	}

	private stopRunTracking() {
		if (this.emuTrackTimer) clearInterval(this.emuTrackTimer);
		this.emuTrackTimer = null;
	}

	private async trackRun() {
		const launch = this.emulatorLaunch;
		if (!launch || (launch.phase !== 'running' && launch.phase !== 'posting' && launch.phase !== 'refused')) {
			this.stopRunTracking();
			return;
		}
		if (launch.runId == null) {
			try {
				this.reconcileLaunch(await emulatorListRuns(8));
			} catch {
				// keep trying on the next tick
			}
			return;
		}
		try {
			const row = await emulatorGetRun(launch.runId);
			this.emulatorRuns = this.emulatorRuns.some((r) => r.runId === row.runId)
				? this.emulatorRuns.map((r) => (r.runId === row.runId ? row : r))
				: [row, ...this.emulatorRuns].slice(0, 8);
			this.reconcileLaunch([row]);
			if (row.status !== 'running') {
				this.stopRunTracking();
				void this.pollBoard();
			}
		} catch {
			// a transient miss; the next tick retries
		}
	}

	/** STOP a running tape (C-G): its corridor event then completes via C-F. */
	async stopRun(runId: number) {
		if (this.emulatorBusy) return;
		this.emulatorBusy = `stop:${runId}`;
		try {
			const res = await emulatorStopRun(runId);
			// A 200 for a run that had already ended carries ITS status — never a stop.
			const stopped = res.ok && res.runStatus === 'stopped';
			const said = !res.ok ? res.detail : stopped ? 'stopped' : `had already ended (${res.runStatus})`;
			this.emulatorActionNote = `run #${runId} ${res.ok ? said : `stop: ${said}`}`;
			this.pushAudit('operator', res.ok ? 'info' : 'error', 'emulator', clipText(`stop run #${runId} → ${said}`, 240), undefined, {
				actorKind: 'system',
			});
			if (res.ok && this.emulatorLaunch?.runId === runId) {
				this.emulatorLaunch = {
					...this.emulatorLaunch,
					phase: runPhase(res.runStatus ?? 'stopped'),
					detail: stopped ? 'stopped by the operator' : `the run had already ended (${res.runStatus})`,
				};
			}
			await this.pollEmulator();
			if (res.ok) {
				void this.pollBoard();
				void this.pollAudit(true);
			}
		} finally {
			this.emulatorBusy = null;
		}
	}

	/** LIVING CITY start/stop (C-G) — only when the build serves the route. */
	async setLivingCity(action: 'start' | 'stop') {
		if (this.livingCityBusy || !this.emulator.livingCitySupported) return;
		this.livingCityBusy = true;
		try {
			this.livingCity = await emulatorSetLivingCity(action);
			this.livingCityNote = null;
			this.pushAudit('operator', 'info', 'emulator', `living city ${action} → ${this.livingCity.status}`, undefined, { actorKind: 'system' });
			void this.pollBoard();
		} catch (err) {
			this.livingCityNote = err instanceof Error ? err.message : String(err);
		} finally {
			this.livingCityBusy = false;
		}
	}

	// ─── decision queue: enforcement, dispatch, signals, watchlist ────────────

	/**
	 * Dispatches whose corridor event is still open. While the events API
	 * answers, a dispatch of an event that has left the board (completed /
	 * cancelled — e.g. one closed before C-F walked its dispatches shut) is
	 * not out on the road: GET /units projects none of them, so neither do
	 * the counters. Without an authoritative board every dispatch counts.
	 */
	get openDispatches(): DispatchRecord[] {
		if (this.eventsApiDeployed !== true) return this.dispatches;
		const open = new Set(this.boardRows.filter(isLiveEvent).map((e) => e.eventId));
		return this.dispatches.filter((d) => d.eventId != null && open.has(d.eventId));
	}

	/** A dispatch whose event has closed (off an authoritative board) — never projected. */
	dispatchEventClosed(d: DispatchRecord): boolean {
		if (this.eventsApiDeployed !== true || d.eventId == null) return false;
		return !this.boardRows.some((e) => e.eventId === d.eventId);
	}

	/** Commander counters (the four DataTiles). Live-derived, fixture-honest. */
	get counters() {
		const pendingRecs = this.recommendations.filter((r) => r.status === 'pending').length;
		const open = this.openDispatches;
		const requestedDispatches = open.filter((d) => d.status === 'requested').length;
		const unitsOut = open.filter((d) => d.status === 'approved' || d.status === 'en_route' || d.status === 'on_scene').length;
		const ambulanceWindows = open.filter(
			(d) => d.agency === 'ambulance' && (d.status === 'approved' || d.status === 'en_route' || d.status === 'on_scene'),
		).length;
		const active = this.events.filter((e) => e.source !== 'offline-fixture' && e.state === 'active');
		return {
			activeEvents: active.length,
			activeEmulated: active.filter((e) => e.emulated).length,
			// the escorts' open proposals wait on a named officer too
			pendingDecisions: pendingRecs + requestedDispatches + this.escortProposals.length,
			unitsDispatched: unitsOut,
			ambulanceWindows,
		};
	}

	/** True when a human is on console — every writer action requires this. */
	get officerOn(): boolean {
		return this.dutyOfficer.trim().length >= 2;
	}

	setDutyOfficer(name: string) {
		if (this.dutyOfficerFromSession) return; // the signed-in session owns the identity
		const before = this.officerOn;
		this.dutyOfficer = name.slice(0, 120);
		this.dutyOfficerNote = this.officerOn
			? null
			: 'a named duty officer must be on console before approve / reject / dispatch / signal actions can commit';
		this.onOfficerPresence(before);
	}

	/**
	 * The operator gate's signed-in officer (GET /api/session, gate:true)
	 * becomes the duty officer and locks the field; signing out releases it.
	 */
	adoptSessionOfficer(name: string | null) {
		const before = this.officerOn;
		const who = name?.trim() ?? '';
		if (who.length >= 2) {
			this.dutyOfficerFromSession = true;
			this.dutyOfficer = who.slice(0, 120);
			this.dutyOfficerNote = null;
		} else if (this.dutyOfficerFromSession) {
			this.dutyOfficerFromSession = false;
			this.dutyOfficer = '';
		}
		this.onOfficerPresence(before);
	}

	/** Require the officer for a commit; push refusal into the audit trail. */
	private requireOfficerFor(operation: string): string | null {
		const who = this.dutyOfficer.trim();
		if (who.length < 2) {
			this.dutyOfficerNote = `${operation} refused — set the duty officer identity first (no automated enforcement)`;
			this.pushAudit('operator', 'error', 'gate', `${operation} refused: no named duty officer on console`, undefined, { actorKind: 'system' });
			return null;
		}
		return who;
	}

	async pollRecommendations() {
		try {
			const rows = await fetchEnforcementRecommendations('pending');
			this.recommendations = rows;
			this.recommendationsLive = true;
			this.recommendationsUpdated = Date.now();
			this.recommendationsNote = null;
		} catch (err) {
			this.recommendationsLive = false;
			this.recommendationsNote =
				err instanceof ApiNotDeployed
					? 'enforcement API not deployed (404) — queue hidden, board unaffected'
					: `recommendations unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	/** The human-approved recommendations (ids only matter: the pursuit chain). */
	async pollApproved() {
		try {
			this.approvedRecommendations = await fetchEnforcementRecommendations('approved');
			this.approvedUpdated = Date.now();
		} catch {
			// keep last-good; a pursuit card then says what is missing
		}
	}

	/**
	 * The vehicle behind a recommendation is reported stolen (its watchlist
	 * earmark's lawful basis is stolen_vehicle_fir): a penal e-challan would
	 * fine the theft victim, so it can only be rejected (C-N).
	 */
	stolenVehicle(vehicleId: string | null | undefined): VehicleWatchRecord | null {
		if (!vehicleId) return null;
		const w = this.watchlist.find((v) => v.vehicleId === vehicleId);
		return w && w.lawfulBasis === 'stolen_vehicle_fir' ? w : null;
	}

	/**
	 * APPROVE / REJECT an e-challan under the named officer. One decision per
	 * card at a time (recommendationBusy) — a double click sends one POST.
	 * A PENAL rule on a vehicle reported stolen is never approved from here
	 * (refused before the wire); an oracle 409 ('e-challan withheld — vehicle
	 * reported stolen') is kept on the card verbatim and never retried. An
	 * R_STOLEN_SIGHTING approve is the officer authorising the pursuit
	 * evidence (C6), not a fine.
	 */
	async decideRecommendation(id: string, decision: 'approve' | 'reject', note = '') {
		if (this.recommendationBusy) return false;
		const rec = this.recommendations.find((r) => r.recommendationId === id);
		if (decision === 'approve') {
			if (this.recommendationRefusals[id]) return false; // the oracle said no; never retried
			if (rec && rec.ruleId !== 'R_STOLEN_SIGHTING' && this.stolenVehicle(rec.vehicleId)) {
				this.pushAudit('operator', 'error', 'gate', 'e-challan approve refused: the vehicle is reported stolen (FIR) — its owner is the theft victim', undefined, {
					actorKind: 'system',
					kind: 'enforcement',
				});
				return false;
			}
		}
		const officer = this.requireOfficerFor(`e-challan ${decision}`);
		if (!officer) return false;
		this.recommendationBusy = id;
		const verb = rec?.ruleId === 'R_STOLEN_SIGHTING' ? (decision === 'approve' ? 'pursuit authorised' : 'stolen sighting dismissed') : `e-challan ${decision}`;
		this.pushAudit(
			'operator',
			'decision',
			officer,
			`${verb} ${rec?.plate ?? id.slice(0, 8)}${note ? ` — ${clipText(note, 140)}` : ''}`,
			undefined,
			{ actorKind: 'human', kind: 'enforcement' },
		);
		try {
			await decideEnforcementRecommendation(id, { decision, officer, note });
			await Promise.all([this.pollRecommendations(), this.pollApproved()]);
			void this.pollAudit(true);
			return true;
		} catch (err) {
			if (err instanceof DecisionRefused) {
				this.recommendationRefusals = {
					...this.recommendationRefusals,
					[id]: { status: err.status, detail: err.detail, reason: err.reason, at: Date.now() },
				};
				this.pushAudit('operator', 'error', 'oracle', clipText(`${decision} refused (409): ${err.detail}`, 240), undefined, {
					actorKind: 'machine',
					kind: 'enforcement',
				});
			} else {
				this.pushAudit('operator', 'error', 'oracle', clipText(`decision failed: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
					actorKind: 'machine',
				});
			}
			void this.pollRecommendations();
			return false;
		} finally {
			this.recommendationBusy = null;
		}
	}

	// ─── escort authority ─────────────────────────────────────────────────────

	/**
	 * GET /api/convoy/escorts: the running escorts' open proposals and recent
	 * outcomes. A proposal that expired (no officer answered — the convoy held
	 * its current plan), was superseded or was decided on ANOTHER console is
	 * narrated once in the feed; outcomes older than this console's first
	 * read are history (the server feed carries them).
	 */
	async pollEscorts() {
		try {
			const { lanes } = await fetchEscorts();
			this.escorts = lanes;
			this.escortsUpdated = Date.now();
			this.escortsDeployed = true;
			this.escortsNote = null;
			for (const lane of lanes) {
				for (const o of lane.outcomes) {
					const key = `${o.approvalId}:${o.status}`;
					if (this.escortOutcomesSeen.has(key)) continue;
					this.escortOutcomesSeen.add(key);
					if (this.escortOutcomesSeeded && !this.escortDecisions[o.approvalId]) this.narrateEscortOutcome(o);
				}
			}
			this.escortOutcomesSeeded = true;
		} catch (err) {
			if (this.noteRefusal(err)) return;
			if (err instanceof ApiNotDeployed) {
				this.escortsDeployed = false;
				this.escorts = [];
				this.escortsNote = 'escort proposals not reported by this brain (predates escort authority)';
				return;
			}
			this.escortsNote = err instanceof Error ? err.message : String(err);
		}
	}

	/** One closed proposal as a feed row (expired / superseded / decided elsewhere). */
	private narrateEscortOutcome(o: EscortProposalOutcome) {
		const what = o.summary ?? o.action.replace(/_/g, ' ');
		const text =
			o.status === 'expired'
				? `escort proposal expired — ${o.expiryReason ?? 'no officer response — current plan held'}: ${what}`
				: o.status === 'superseded'
					? `escort proposal superseded by a newer one: ${what}`
					: `escort proposal ${o.status}${o.decidedBy ? ` by ${o.decidedBy}` : ''}: ${what}`;
		const human = o.status === 'approved' || o.status === 'rejected';
		this.pushAudit(human ? 'operator' : 'audit-poll', human ? 'decision' : 'info', human ? (o.decidedBy ?? 'officer') : 'convoy-brain', clipText(text, 240), undefined, {
			actorKind: human ? 'human' : 'machine',
			kind: 'brain',
		});
	}

	/**
	 * A named officer approves or rejects an escort proposal — the AI never
	 * does. One decision in flight across every surface (HUD, planner rail,
	 * decision queue): a second click while one is out is ignored.
	 */
	async decideEscortProposal(proposal: EscortProposal, decision: 'approve' | 'reject'): Promise<EscortApprovalOutcome | null> {
		if (this.escortDecisionBusy) return null;
		const officer = this.requireOfficerFor(`escort proposal ${decision}`);
		if (!officer) return null;
		this.escortDecisionBusy = proposal.approvalId;
		const what = proposal.summary ?? proposal.action.replace(/_/g, ' ');
		this.pushAudit('operator', 'decision', officer, clipText(`escort proposal ${decision === 'approve' ? 'approved' : 'rejected'}: ${what}`, 240), undefined, {
			actorKind: 'human',
			kind: 'brain',
			eventId: proposal.eventId,
		});
		try {
			const res = await decideEscortProposal(proposal.movementId, { approval_id: proposal.approvalId, decision, officer });
			this.escortDecisions = { ...this.escortDecisions, [proposal.approvalId]: { ...res, decision, officer, at: Date.now() } };
			this.escortOutcomesSeen.add(`${proposal.approvalId}:${res.status ?? ''}`);
			if (!res.ok) {
				this.pushAudit('operator', 'error', 'convoy-brain', clipText(`escort proposal ${decision} refused (${res.httpStatus || 'network'}): ${res.detail ?? '—'}`, 240), undefined, {
					actorKind: 'machine',
					kind: 'brain',
				});
			}
			void this.pollEscorts();
			return res;
		} finally {
			this.escortDecisionBusy = null;
		}
	}

	async pollDispatches() {
		try {
			const rows = await fetchDispatches(40);
			this.dispatches = rows;
			this.dispatchesLive = true;
			this.dispatchesUpdated = Date.now();
			this.dispatchesNote = null;
			this.retireRequestNote();
		} catch (err) {
			this.dispatchesLive = false;
			this.dispatchesNote =
				err instanceof ApiNotDeployed
					? 'dispatches API not deployed (404)'
					: `dispatches unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	/**
	 * Waypoints the tiles offer for an event: for a live pursuit with a plan,
	 * 'dispatch to choke #N' (the ranked interception junctions, likely-passed
	 * ones last); otherwise the junctions nearest the event's spine.
	 */
	waypointOptions(ev: CorridorEvent | null): WaypointOption[] {
		if (!isLiveUuidEvent(ev)) return [];
		const plan = this.interception;
		if (ev.eventKind === 'vehicle_pursuit' && plan && plan.eventId === ev.eventId && plan.chokes.length) {
			// Plan rank order, but a junction the vehicle has likely passed is
			// never the default: those follow every live choke (rank kept).
			return plan.chokes
				.filter((c) => c.junctionId > 0)
				.sort((a, b) => Number(a.likelyPassed === true) - Number(b.likelyPassed === true) || a.rank - b.rank)
				.map((c) => ({
					junctionId: c.junctionId,
					source: 'interception' as const,
					rank: c.rank,
					name: c.name ?? this.junctionName(c.junctionId) ?? `J${c.junctionId}`,
					distanceM: null,
					likelyPassed: c.likelyPassed === true,
				}));
		}
		return junctionsNearSpine(ev.routeSegmentIds, this.segmentLines, this.junctions, 3).map((n, i) => ({
			junctionId: n.junction.junctionId,
			source: 'spine' as const,
			rank: i + 1,
			name: n.junction.name,
			distanceM: Math.round(n.distanceM),
			likelyPassed: false,
		}));
	}

	/**
	 * REQUEST a unit for an event (POST /events/{id}/dispatch) — any agency
	 * of the registry, with a waypoint junction so /units can project it once
	 * a duty officer approves.
	 */
	async requestDispatchUnit(
		eventId: string,
		input: { agency: string; unitId: string; waypointJunction?: number | null; note?: string },
	): Promise<boolean> {
		if (this.dispatchBusy) return false;
		const officer = this.requireOfficerFor('unit dispatch request');
		if (!officer) return false;
		const agency = input.agency in AGENCIES ? input.agency : 'jan_rakshak';
		const unitId = input.unitId.trim() || DEFAULT_UNIT_ID[agency] || 'JR-EM-01';
		this.dispatchBusy = eventId;
		const label = AGENCIES[agency as keyof typeof AGENCIES]?.label ?? agency;
		this.pushAudit(
			'operator',
			'decision',
			officer,
			`${label} ${unitId} requested${input.waypointJunction != null ? ` → J${input.waypointJunction}` : ''}`,
			undefined,
			{ actorKind: 'human', eventId, kind: 'dispatch' },
		);
		try {
			const rec = await requestDispatch(eventId, {
				agency,
				unitId,
				...(input.waypointJunction != null ? { waypointJunction: input.waypointJunction } : {}),
				requestedBy: officer,
				...(input.note ? { note: input.note } : {}),
			});
			this.dispatchRequestNote = {
				ok: true,
				text: `dispatch ${rec?.dispatchId.slice(0, 8) ?? '?'} requested — awaiting a duty officer's APPROVE`,
				at: Date.now(),
				dispatchId: rec?.dispatchId ?? null,
			};
			await this.pollDispatches();
			void this.pollAudit(true);
			return true;
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			this.dispatchRequestNote = { ok: false, text: clipText(`request refused: ${msg}`, 240), at: Date.now() };
			this.pushAudit('operator', 'error', 'oracle', clipText(`dispatch request failed: ${msg}`, 240), undefined, { actorKind: 'machine', eventId });
			return false;
		} finally {
			this.dispatchBusy = null;
		}
	}

	/**
	 * The request row's result line speaks for ONE dispatch while it waits:
	 * it retires once that dispatch leaves 'requested' (approved, deferred,
	 * cancelled) or after 2 minutes — never an 'awaiting APPROVE' line under
	 * a request that was decided long ago.
	 */
	private retireRequestNote() {
		const note = this.dispatchRequestNote;
		if (!note) return;
		const tooOld = Date.now() - note.at > 120_000;
		const row = note.dispatchId ? this.dispatches.find((d) => d.dispatchId === note.dispatchId) : null;
		if (tooOld || (row && row.status !== 'requested')) this.dispatchRequestNote = null;
	}

	/** The corridor event a dispatch serves (its echoes belong to that narrative). */
	private dispatchEventId(dispatchId: string): string | null {
		return this.dispatches.find((d) => d.dispatchId === dispatchId)?.eventId ?? null;
	}

	/** Back-compat: a Jan Rakshak request (the tiles' original verb). */
	async requestJanRakshak(eventId: string, unitId: string, waypointJunction?: number) {
		return this.requestDispatchUnit(eventId, { agency: 'jan_rakshak', unitId, waypointJunction: waypointJunction ?? null });
	}

	async decideDispatchAction(id: string, action: 'approve' | 'defer', note = '') {
		if (this.dispatchBusy) return false;
		const officer = this.requireOfficerFor(`dispatch ${action}`);
		if (!officer) return false;
		this.dispatchBusy = id;
		this.pushAudit('operator', 'decision', officer, `dispatch ${id.slice(0, 8)} ${action === 'approve' ? 'approved' : 'deferred'}`, undefined, {
			actorKind: 'human',
			kind: 'dispatch',
			eventId: this.dispatchEventId(id),
		});
		try {
			await decideDispatch(id, { action, officer, note });
			await this.pollDispatches();
			void this.pollAudit(true);
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', clipText(`dispatch decision failed: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
				actorKind: 'machine',
			});
			void this.pollDispatches();
			return false;
		} finally {
			this.dispatchBusy = null;
		}
	}

	async advanceDispatchStatus(id: string, status: 'en_route' | 'on_scene' | 'completed' | 'cancelled', note = '') {
		if (this.dispatchBusy) return false;
		const officer = this.requireOfficerFor(`dispatch ${status}`);
		if (!officer) return false;
		this.dispatchBusy = id;
		this.pushAudit('operator', 'decision', officer, `dispatch ${id.slice(0, 8)} → ${status}`, undefined, {
			actorKind: 'human',
			kind: 'dispatch',
			eventId: this.dispatchEventId(id),
		});
		try {
			await advanceDispatch(id, { status, officer, note });
			await this.pollDispatches();
			void this.pollAudit(true);
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', clipText(`dispatch status failed: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
				actorKind: 'machine',
			});
			void this.pollDispatches();
			return false;
		} finally {
			this.dispatchBusy = null;
		}
	}

	async pollSignalPlans() {
		try {
			const doc = await fetchSignalPlans();
			this.signalPlans = doc;
			this.signalPlansUpdated = Date.now();
			this.signalPlansNote = null;
		} catch (err) {
			this.signalPlansNote =
				err instanceof ApiNotDeployed
					? 'signals/plans not deployed (404)'
					: `signal plans unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	/**
	 * APPLY the city signal plan. The fail-closed refusal is a first-class
	 * outcome: it lands in signalApply and renders inline as 'held at
	 * baseline' — never a modal, never hidden (motion doctrine). The event
	 * attribution goes out only for a live uuid event (a preview fixture's
	 * slug would hit the writer's uuid cast).
	 */
	async applyCityPlan(band?: BandCode) {
		const officer = this.requireOfficerFor('signal plan apply');
		if (!officer) return;
		if (this.signalApplying) return;
		this.signalApplying = true;
		this.pushAudit('operator', 'decision', officer, `signal plan APPLY requested${band ? ` (band ${band})` : ''}`, undefined, {
			actorKind: 'human',
			kind: 'signals',
		});
		try {
			const ev = this.selectedEvent;
			const outcome = await applySignalPlan(
				{
					...(band ? { band } : {}),
					...(isLiveUuidEvent(ev) ? { eventId: ev.eventId } : {}),
				},
				officer,
			);
			this.signalApply = outcome;
			if (outcome.applied) {
				this.pushAudit('operator', 'info', 'signal-writer', `plan applied (${outcome.status})`, undefined, { actorKind: 'machine' });
			} else {
				this.pushAudit(
					'operator',
					'band',
					'signal-writer',
					`plan HELD at baseline (${outcome.status}${outcome.refusalReason ? `: ${outcome.refusalReason}` : ''})`,
					outcome.detail,
					{ actorKind: 'machine' },
				);
			}
			await this.pollSignalPlans();
			if (this.signalHistoryOpen) void this.loadSignalHistory();
		} finally {
			this.signalApplying = false;
		}
	}

	/**
	 * RESTORE the assumed programs for the selected LIVE event (or all). A
	 * preview fixture has no signal history and a slug id the writer cannot
	 * cast, so with one selected (and not `all`) the restore is refused here,
	 * in the audit trail, before any request.
	 */
	async restorePrograms(all = false) {
		if (this.signalRestoring) return;
		const officer = this.requireOfficerFor('signal program restore');
		if (!officer) return;
		const ev = this.selectedEvent;
		const eventId = isLiveUuidEvent(ev) ? ev.eventId : undefined;
		if (!all && !eventId) {
			this.pushAudit(
				'operator',
				'error',
				'gate',
				'signal programs RESTORE refused: select a live event (preview fixtures carry no signal history) or restore all',
				undefined,
				{ actorKind: 'system' },
			);
			return;
		}
		this.signalRestoring = all ? 'all' : 'event';
		this.pushAudit('operator', 'decision', officer, `signal programs RESTORE${all ? ' (all)' : ` for ${eventId!.slice(0, 8)}`}`, undefined, {
			actorKind: 'human',
			kind: 'signals',
			// a city-wide restore belongs to the city narrative, not the selected event's
			eventId: all ? null : (eventId ?? null),
		});
		try {
			await restoreSignalPrograms(all ? { all: true } : { eventId }, officer);
			await this.pollSignalPlans();
			if (this.signalHistoryOpen) void this.loadSignalHistory();
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', clipText(`restore failed: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
				actorKind: 'machine',
			});
		} finally {
			this.signalRestoring = null;
		}
	}

	async loadSignalHistory() {
		this.signalHistoryOpen = true;
		try {
			this.signalHistory = await fetchSignalHistory(20);
			this.signalHistoryUpdated = Date.now();
			this.signalHistoryNote = null;
		} catch (err) {
			this.signalHistory = [];
			this.signalHistoryNote =
				err instanceof ApiNotDeployed
					? 'signal history not deployed (404)'
					: `signal history unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	async pollWatchlist() {
		try {
			const rows = await fetchWatchlist();
			this.watchlist = rows;
			this.watchlistLive = true;
			this.watchlistUpdated = Date.now();
			this.watchlistNote = null;
		} catch (err) {
			this.watchlistLive = false;
			this.watchlistNote =
				err instanceof ApiNotDeployed
					? 'vehicles API not deployed (404)'
					: `watchlist unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	/** Sighting-retention reaper health (C6 / DPDP) for the watchlist console. */
	async pollRetention() {
		try {
			this.retention = await fetchRetention();
			this.retentionNote = null;
		} catch (err) {
			this.retentionNote =
				err instanceof ApiNotDeployed
					? 'retention status not deployed (404)'
					: `retention status unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	async earmarkPlate(
		plate: string,
		state: 'watch' | 'flagged',
		reason: string,
		lawfulBasis: string,
		retentionDays = 30,
	) {
		const officer = this.requireOfficerFor('vehicle earmark');
		if (!officer) return false;
		this.watchBusy = plate;
		this.pushAudit('operator', 'decision', officer, `earmark ${plate} → ${state} (basis ${lawfulBasis}, ${retentionDays}d)`, undefined, {
			actorKind: 'human',
		});
		try {
			await earmarkVehicle(plate, {
				state,
				reason,
				lawfulBasis,
				retentionDays,
				earmarkedBy: officer,
			});
			await this.pollWatchlist();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', clipText(`earmark failed: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
				actorKind: 'machine',
			});
			return false;
		} finally {
			this.watchBusy = null;
		}
	}

	async clearWatch(vehicleId: string) {
		const officer = this.requireOfficerFor('earmark clear');
		if (!officer) return false;
		this.watchBusy = vehicleId;
		try {
			await clearEarmark(vehicleId, officer);
			this.pushAudit('operator', 'decision', officer, `earmark cleared for vehicle ${vehicleId.slice(0, 8)}`, undefined, { actorKind: 'human' });
			await this.pollWatchlist();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', clipText(`earmark clear failed: ${err instanceof Error ? err.message : String(err)}`, 240), undefined, {
				actorKind: 'machine',
			});
			return false;
		} finally {
			this.watchBusy = null;
		}
	}

	// ─── pursuit / interception ───────────────────────────────────────────────

	/** The 6 s pursuit poller: refresh the plan while a LIVE pursuit is selected. */
	async pollInterception() {
		const ev = this.selectedEvent;
		if (!isLivePursuit(ev)) return;
		await this.loadInterception(ev.eventId);
	}

	/**
	 * Load the interception plan for `eventId` — only for a LIVE
	 * vehicle_pursuit (preview fixtures and other kinds have none; asking
	 * would 422). Request-token guarded: an answer for an older request or a
	 * no-longer-selected event is dropped. The plan object is replaced only
	 * when its content changed, so the canvas overlay re-animates on a new
	 * sighting, not on every poll.
	 */
	async loadInterception(eventId: string) {
		const ev = this.events.find((e) => e.eventId === eventId) ?? null;
		const token = ++this.interceptionToken;
		const previewPursuit = !!ev && ev.source === 'offline-fixture' && ev.eventKind === 'vehicle_pursuit';
		if (!isLivePursuit(ev)) {
			this.interceptionRaw = null;
			this.interception = null;
			this.interceptionSig = '';
			this.interceptionUpdated = 0;
			this.interceptionNote = previewPursuit
				? 'preview fixture — interception plans exist only for live pursuits'
				: null;
			return;
		}
		try {
			const plan = await fetchInterceptionPlan(eventId);
			if (token !== this.interceptionToken || this.selectedEventId !== eventId) return;
			const sig = planSignature(plan);
			if (sig !== this.interceptionSig) {
				this.interceptionSig = sig;
				this.interceptionRaw = plan;
				this.publishInterception();
			}
			this.interceptionUpdated = Date.now();
			this.interceptionNote = null;
		} catch (err) {
			if (token !== this.interceptionToken || this.selectedEventId !== eventId) return;
			// Keep this pursuit's last plan (the panel shows it stale); never
			// another event's.
			if (this.interceptionRaw && this.interceptionRaw.eventId !== eventId) {
				this.interceptionRaw = null;
				this.interception = null;
				this.interceptionSig = '';
			}
			this.interceptionNote =
				err instanceof ApiNotDeployed
					? 'interception-plan not deployed (404)'
					: `no interception plan: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	/** Vehicles an OPEN pursuit already references (metadata.vehicle_id). */
	get pursuedVehicleIds(): Set<string> {
		const ids = new Set<string>();
		for (const e of this.events) {
			if (isLiveEvent(e) && e.eventKind === 'vehicle_pursuit' && e.metadata?.vehicleId && e.state !== 'completed' && e.state !== 'cancelled') {
				ids.add(e.metadata.vehicleId);
			}
		}
		return ids;
	}

	/**
	 * The pursuit cards the queue may offer: FLAGGED vehicles with a latest
	 * sighting and no open pursuit. Each anchors on the segment of its latest
	 * sighting (the interception seed rule: nearest road within 300 m) and
	 * opens from the newest human-APPROVED recommendation for the vehicle
	 * (C6); a missing link is named, never papered over.
	 */
	get pursuitCandidates(): PursuitCandidate[] {
		const pursued = this.pursuedVehicleIds;
		return this.watchlist
			.filter((w) => w.earmarkState === 'flagged' && w.lastSighting && !pursued.has(w.vehicleId))
			.map((w) => {
				const sight = w.lastSighting!;
				const hit = this.segmentLines.length
					? nearestSegment([sight.lon, sight.lat], this.segmentLines, PURSUIT_ANCHOR_TOL_M)
					: null;
				const anchor = hit
					? { segmentId: hit.segment.segmentId, roadName: hit.segment.roadName, distanceM: Math.round(hit.distanceM) }
					: null;
				// C-N: a vehicle reported stolen is pursued on its approved
				// R_STOLEN_SIGHTING only — never on an e-challan approved before
				// the theft report (the oracle refuses that basis too)
				const stolen = w.lawfulBasis === 'stolen_vehicle_fir';
				const approved = this.approvedRecommendations
					.filter(
						(r) =>
							r.vehicleId === w.vehicleId &&
							r.status === 'approved' &&
							!approvalSpent(r) &&
							(!stolen || r.ruleId === 'R_STOLEN_SIGHTING'),
					)
					.sort((a, b) => Date.parse(b.decidedAt ?? b.createdAt ?? '') - Date.parse(a.decidedAt ?? a.createdAt ?? ''))[0];
				const recommendationId = approved?.recommendationId ?? null;
				return {
					vehicle: w,
					anchor,
					recommendationId,
					refusal: !anchor ? ('no-anchor' as const) : !recommendationId ? ('no-approved-recommendation' as const) : null,
				};
			});
	}

	// ─── degradation ledger ────────────────────────────────────────────────────

	/**
	 * Poll the decisions_log failure taxonomy. An EMPTY ledger from a live
	 * endpoint is a valid, honest answer (no degradation on record) — only
	 * transport failures flip degradationLive and leave the last-good ledger
	 * in place with a labelled note.
	 */
	async pollDegradation() {
		try {
			const taxonomy = await fetchDegradationTaxonomy();
			this.degradation = taxonomy;
			this.degradationLive = true;
			this.degradationUpdated = Date.now();
			this.degradationNote = null;
		} catch (err) {
			this.degradationLive = false;
			this.degradationNote =
				err instanceof ApiNotDeployed
					? 'ops degradation-taxonomy not deployed (404) — no ledger'
					: `degradation ledger unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	// ─── masthead: brain posture + traffic-feed provenance ───────────────────

	async pollBrain() {
		this.brain = await fetchBrainPosture();
	}

	async pollTrafficFeed() {
		try {
			this.trafficFeed = await fetchTrafficProvenance();
			this.trafficFeedNote = null;
		} catch (err) {
			this.trafficFeedNote = err instanceof Error ? err.message : String(err);
		}
	}

	// ─── geometry (junction registry + segment lines) ─────────────────────────

	/** Junction names/positions + segment lines; refreshed every few minutes. */
	async loadGeometry(force = false) {
		if (!force && this.geometryAt && Date.now() - this.geometryAt < POLL_MS.geometry - 1000) return;
		const [junctions, segments] = await Promise.allSettled([fetchJunctionRegistry(), fetchSegmentLines()]);
		const notes: string[] = [];
		if (junctions.status === 'fulfilled') this.junctions = junctions.value;
		else notes.push('junction registry unavailable');
		if (segments.status === 'fulfilled') this.segmentLines = segments.value;
		else notes.push('segment geometry unavailable');
		this.geometryNote = notes.length ? notes.join(' · ') : null;
		if (junctions.status === 'fulfilled' || segments.status === 'fulfilled') this.geometryAt = Date.now();
	}

	/**
	 * Open a pursuit for a watchlist vehicle — human-initiated (C6): anchored
	 * on its latest sighting's segment, from its approved recommendation.
	 */
	async openPursuitFor(vehicleId: string) {
		if (this.pursuitBusy) return false;
		const c = this.pursuitCandidates.find((p) => p.vehicle.vehicleId === vehicleId);
		if (!c) return false;
		if (!c.anchor || !c.recommendationId) {
			this.pursuitNote = {
				vehicleId,
				text: !c.anchor ? 'no road anchor for the latest sighting' : 'no human-approved recommendation for this vehicle (C6)',
			};
			return false;
		}
		return this.openPursuit(vehicleId, c.vehicle.plate, c.anchor.segmentId, c.recommendationId);
	}

	/** Open a pursuit for a watchlist vehicle — human-initiated (C6). */
	async openPursuit(vehicleId: string, plate: string | null, seedSegmentId: number, recommendationId?: string) {
		if (this.pursuitBusy) return false;
		const officer = this.requireOfficerFor('pursuit open');
		if (!officer) return false;
		this.pursuitBusy = vehicleId;
		this.pursuitNote = null;
		this.pushAudit('operator', 'decision', officer, `pursuit OPEN for ${plate ?? vehicleId.slice(0, 8)} at segment ${seedSegmentId}`, undefined, {
			actorKind: 'human',
			kind: 'event',
		});
		try {
			const { eventId } = await openPursuitEvent({
				vehicleId,
				...(recommendationId ? { recommendationId } : {}),
				plate,
				seedSegmentId,
				actor: officer,
			});
			await this.pollBoard();
			this.selectEvent(eventId);
			return true;
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			this.pursuitNote = { vehicleId, text: clipText(msg, 240) };
			this.pushAudit('operator', 'error', 'oracle', clipText(`pursuit open failed: ${msg}`, 240), undefined, { actorKind: 'machine' });
			return false;
		} finally {
			this.pursuitBusy = null;
		}
	}
}

/** Ledger status → the panel's launch phase. */
function runPhase(status: string): EmulatorLaunchState['phase'] {
	if (status === 'running') return 'running';
	if (status === 'stopped') return 'stopped';
	if (status === 'interrupted') return 'interrupted';
	if (status === 'failed') return 'failed';
	return 'completed';
}

function runsSource(rung: string): AuditEntry['source'] {
	return rung === 'ws-event' ? 'ws-event' : rung === 'ws-convoy' ? 'ws-convoy' : 'operator';
}

function fmtNum(n: number | null | undefined): string {
	return n == null ? 'n/a' : n.toFixed(1);
}

/** Exported for components that classify free-text actors (dispatch tails). */
export { actorKindOfName };

export const ops = new OpsStore();
