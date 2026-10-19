/**
 * Ops showcase store — the single state machine behind the /ops board.
 *
 * Runes-based (Svelte 5). The route component calls start()/stop(); the store
 * owns every poller, socket and cache:
 *
 *   board     5 s   GET /api/v1/events/active → movements fallback → fixtures
 *   band      5 s   GET /api/v1/adaptive/situation_log → status/params rungs
 *   audit     2 s   poll rung (event audit tail → adaptive status), plus the
 *                   WS ladder for the selected event and the on-demand NDJSON
 *                   probe into the brain's /chat/stream audit channel
 *   emulator  10 s  health probe on the absolute loopback constant
 *   ledger    10 s  GET /api/v1/ops/degradation-taxonomy (decisions_log
 *                   failure taxonomy; empty log is an honest empty ledger)
 *   twin      never twin compare runs ONLY on operator demand and caches per
 *                   (scenario, policies, seed) — no interval polling of the
 *                   double-compute endpoint (oracle-load mitigation)
 */

import {
	ApiNotDeployed,
	advanceDispatch,
	applySignalPlan,
	clearEarmark,
	compareTwinPolicies,
	compareTwinPoliciesDirect,
	createAuditSocket,
	decideDispatch,
	decideEnforcementRecommendation,
	emulatorHealth,
	emulatorListRuns,
	emulatorStageUseCase,
	emulatorUseCases,
	earmarkVehicle,
	fetchActiveEvents,
	fetchAdaptiveParams,
	fetchAdaptiveStatus,
	fetchDegradationTaxonomy,
	fetchDispatches,
	fetchEnforcementRecommendations,
	fetchInterceptionPlan,
	fetchMovementsAsEvents,
	fetchOverrides,
	fetchSignalHistory,
	fetchSignalPlans,
	fetchSituationLog,
	fetchWatchlist,
	openPursuitEvent,
	pollAuditTail,
	requestDispatch,
	restoreSignalPrograms,
	streamAuditProbe,
	type BrainWsEvent,
} from './client';
import { POLL_MS, WS_OPEN_TIMEOUT_MS } from './endpoints';
import { USE_CASES, useCaseById } from './usecases';
import type {
	AuditEntry,
	AuditLadderState,
	BandCode,
	BandEvidence,
	BandPoint,
	CorridorEvent,
	ClosurePolicySpec,
	DegradationTaxonomy,
	DispatchRecord,
	EmulatorState,
	EnforcementRecommendation,
	InterceptionPlan,
	OverrideRecord,
	SignalApplyOutcome,
	SignalPlansDoc,
	TwinCompareResult,
	VehicleWatchRecord,
} from './types';

const AUDIT_CAP = 300;
const BAND_CAP = 240;

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

class OpsStore {
	// ─── board ────────────────────────────────────────────────────────────────
	events = $state<CorridorEvent[]>([]);
	boardStatus = $state<'live' | 'mixed' | 'offline'>('offline');
	boardUpdated = $state(0);
	boardNote = $state<string | null>(null);
	eventsApiDeployed = $state<boolean | null>(null);

	selectedEventId = $state<string | null>(null);

	// ─── situation band ───────────────────────────────────────────────────────
	band = $state<BandCode | null>(null);
	bandEvidence = $state<BandEvidence | null>(null);
	bandHistory = $state<BandPoint[]>([]);
	bandSource = $state<'situation_log' | 'adaptive-status' | 'none'>('none');
	bandUpdated = $state(0);
	overrides = $state<OverrideRecord[]>([]);
	overridesNote = $state<string | null>(null);

	// ─── audit stream ─────────────────────────────────────────────────────────
	audit = $state<AuditEntry[]>([]);
	auditRung = $state<AuditLadderState | 'connecting'>('unavailable');
	auditProbeRunning = $state(false);
	ndjsonText = $state('');

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
	});
	emulatorUseCaseIds = $state<string[]>([]);
	emulatorRuns = $state<Array<{ runId: number; useCaseId: string; status: string; clockScale: number | null; isReplay: boolean }>>([]);

	// ─── language (shared with citizen views) ─────────────────────────────────
	lang = $state<string>('en');

	// ─── duty officer identity (the C6 human-actor gate) ──────────────────────
	/** Named human on console. Every city-writer action refuses while blank. */
	dutyOfficer = $state<string>('');
	dutyOfficerNote = $state<string | null>(null);

	// ─── enforcement recommendation queue (ops-screens) ───────────────────────
	recommendations = $state<EnforcementRecommendation[]>([]);
	recommendationsUpdated = $state(0);
	recommendationsLive = $state<boolean | null>(null);
	recommendationsNote = $state<string | null>(null);
	recommendationBusy = $state<string | null>(null);

	// ─── agency dispatch lifecycle (Jan Rakshak tiles) ────────────────────────
	dispatches = $state<DispatchRecord[]>([]);
	dispatchesUpdated = $state(0);
	dispatchesLive = $state<boolean | null>(null);
	dispatchesNote = $state<string | null>(null);
	dispatchBusy = $state<string | null>(null);

	// ─── city signal plans (gate card) ────────────────────────────────────────
	signalPlans = $state<SignalPlansDoc | null>(null);
	signalPlansUpdated = $state(0);
	signalPlansNote = $state<string | null>(null);
	signalApply = $state<SignalApplyOutcome | null>(null);
	signalApplying = $state(false);
	signalHistory = $state<Array<Record<string, unknown>>>([]);
	signalHistoryOpen = $state(false);

	// ─── watchlist (earmarked vehicles) ───────────────────────────────────────
	watchlist = $state<VehicleWatchRecord[]>([]);
	watchlistUpdated = $state(0);
	watchlistLive = $state<boolean | null>(null);
	watchlistNote = $state<string | null>(null);
	watchBusy = $state<string | null>(null);

	// ─── pursuit / interception ───────────────────────────────────────────────
	interception = $state<InterceptionPlan | null>(null);
	interceptionUpdated = $state(0);
	interceptionNote = $state<string | null>(null);
	pursuitBusy = $state<string | null>(null);

	// ─── degradation ledger (decisions_log failure taxonomy) ─────────────────
	degradation = $state<DegradationTaxonomy | null>(null);
	degradationUpdated = $state(0);
	degradationLive = $state<boolean | null>(null);
	degradationNote = $state<string | null>(null);

	// ─── internals ────────────────────────────────────────────────────────────
	private auditSeq = 0;
	private timers: ReturnType<typeof setInterval>[] = [];
	private socket: { close: () => void } | null = null;
	private socketKey = $state<string | null>(null);
	private lastAuditSignature = '';
	private heartbeatTicks = 0;

	get selectedEvent(): CorridorEvent | null {
		return this.events.find((e) => e.eventId === this.selectedEventId) ?? null;
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

	// ─── lifecycle ────────────────────────────────────────────────────────────

	start() {
		if (this.timers.length) return;
		void this.pollBoard();
		void this.pollBand();
		void this.pollAudit();
		void this.pollEmulator();
		void this.pollRecommendations();
		void this.pollDispatches();
		void this.pollSignalPlans();
		void this.pollWatchlist();
		void this.pollDegradation();
		this.timers.push(setInterval(() => void this.pollBoard(), POLL_MS.board));
		this.timers.push(setInterval(() => void this.pollBand(), POLL_MS.band));
		this.timers.push(setInterval(() => void this.pollAudit(), POLL_MS.audit));
		this.timers.push(setInterval(() => void this.pollEmulator(), POLL_MS.emulator));
		this.timers.push(setInterval(() => void this.pollRecommendations(), POLL_MS.enforcement));
		this.timers.push(setInterval(() => void this.pollDispatches(), POLL_MS.dispatch));
		this.timers.push(setInterval(() => void this.pollSignalPlans(), POLL_MS.signals));
		this.timers.push(setInterval(() => void this.pollWatchlist(), POLL_MS.watchlist));
		this.timers.push(setInterval(() => void this.pollDegradation(), POLL_MS.ledger));
		if (!this.selectedEventId && this.events.length) {
			this.selectedEventId = this.events[0].eventId;
		}
	}

	stop() {
		for (const t of this.timers) clearInterval(t);
		this.timers = [];
		this.socket?.close();
		this.socket = null;
		this.socketKey = null;
	}

	// ─── board ────────────────────────────────────────────────────────────────

	async pollBoard() {
		let live: CorridorEvent[] = [];
		let note: string | null = null;
		let deployed: boolean | null = null;
		try {
			live = await fetchActiveEvents();
			deployed = true;
		} catch (err) {
			if (err instanceof ApiNotDeployed) {
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
		this.eventsApiDeployed = deployed;
		const fixtures = fixtureEvents();
		// Live rows first; fixtures always visible, clearly labelled.
		const prevSelected = this.selectedEventId;
		this.events = [...live, ...fixtures];
		if (!prevSelected) this.selectedEventId = this.events[0]?.eventId ?? null;
		// Keep the last-good timestamp when every live source is down.
		if (live.length || deployed === true || !note) this.boardUpdated = Date.now();
		this.boardNote = note;
		this.boardStatus = live.length ? (note ? 'mixed' : 'live') : 'offline';
		this.rebindSocket();
	}

	selectEvent(id: string) {
		this.selectedEventId = id;
		this.band = null;
		this.bandHistory = [];
		this.lastAuditSignature = '';
		void this.pollBand();
		void this.pollAudit();
		void this.loadInterception(id);
		this.rebindSocket();
	}

	// ─── situation band ───────────────────────────────────────────────────────

	async pollBand() {
		const ev = this.selectedEvent;
		// Primary: per-event situation_log history.
		try {
			const log = await fetchSituationLog(this.selectedEventId);
			if (log.length) {
				this.bandHistory = log
					.slice(-BAND_CAP)
					.map((p) => ({ ...p, source: 'situation_log' as const }));
				this.band = log[log.length - 1].band;
				this.bandSource = 'situation_log';
				this.bandUpdated = Date.now();
			} else {
				this.bandSource = 'situation_log';
				this.bandUpdated = Date.now();
			}
		} catch {
			// Fallback rungs: adaptive status per movement, then class params.
			try {
				const status = await fetchAdaptiveStatus();
				const match =
					(ev && status.movements.find((m) => m.id === ev.eventId)) ||
					status.movements.find((m) => m.situationCode) ||
					null;
				if (match?.situationCode) {
					this.appendBandPoint(match.situationCode, match.severityScore, 'adaptive-status');
					this.band = match.situationCode;
					this.bandSource = 'adaptive-status';
					this.bandUpdated = Date.now();
				} else if (ev) {
					const evidence = await fetchAdaptiveParams(ev.protectionClass);
					if (evidence.situationClass) {
						this.appendBandPoint(evidence.situationClass, null, 'adaptive-status');
						this.band = evidence.situationClass;
					}
					this.bandEvidence = evidence;
					this.bandSource = 'adaptive-status';
					this.bandUpdated = Date.now();
				}
			} catch {
				// leave last-good band + stale timestamp (per-panel fallback)
			}
		}
		void this.loadOverrides();
	}

	private appendBandPoint(band: BandCode, severity: number | null, source: BandPoint['source']) {
		const last = this.bandHistory[this.bandHistory.length - 1];
		this.heartbeatTicks++;
		if (last && last.band === band && this.heartbeatTicks % 5 !== 0) return;
		this.bandHistory = [...this.bandHistory.slice(-(BAND_CAP - 1)), { ts: Date.now(), band, severityScore: severity, source }];
	}

	private async loadOverrides() {
		try {
			this.overrides = await fetchOverrides(this.selectedEventId);
			this.overridesNote = null;
		} catch (err) {
			this.overridesNote =
				err instanceof ApiNotDeployed
					? 'overrides endpoint not deployed (404) — shadow-eval evidence from /adaptive/params'
					: `overrides unavailable: ${err instanceof Error ? err.message : String(err)}`;
			try {
				if (this.bandEvidence === null && this.selectedEvent) {
					this.bandEvidence = await fetchAdaptiveParams(this.selectedEvent.protectionClass);
				}
			} catch {
				// keep whatever evidence we have
			}
		}
	}

	// ─── audit stream ─────────────────────────────────────────────────────────

	private pushAudit(
		source: AuditEntry['source'],
		level: AuditEntry['level'],
		actor: string,
		text: string,
		detail?: unknown,
	) {
		this.audit = [
			...this.audit.slice(-(AUDIT_CAP - 1)),
			{ id: ++this.auditSeq, ts: Date.now(), source, level, actor, text, detail },
		];
	}

	async pollAudit() {
		try {
			const snap = await pollAuditTail(this.selectedEventId);
			if (snap.source === 'event-audit' && snap.eventAudit) {
				const sig = JSON.stringify(snap.eventAudit.map((r) => r['id'] ?? r['timestamp'] ?? r));
				if (sig !== this.lastAuditSignature) {
					this.lastAuditSignature = sig;
					for (const row of snap.eventAudit.slice(-8)) {
						this.pushAudit(
							'audit-poll',
							'decision',
							String(row['actor'] ?? row['source'] ?? 'event'),
							String(row['text'] ?? row['summary'] ?? row['action'] ?? JSON.stringify(row)).slice(0, 400),
							row,
						);
					}
				}
				if (this.auditRung === 'unavailable' || this.auditRung === 'connecting') {
					this.auditRung = 'poll';
				}
				return;
			}
			const a = snap.adaptive;
			if (!a) return;
			const bandParts = a.movements
				.filter((m) => m.situationCode)
				.map((m) => `${m.id.slice(0, 8)}=${m.situationCode}${m.severityScore != null ? `(${m.severityScore.toFixed(0)})` : ''}`);
			const sig = JSON.stringify([
				a.degradationTier,
				a.lastShadowEval,
				a.auditTrail.length,
				bandParts,
			]);
			if (sig !== this.lastAuditSignature) {
				this.lastAuditSignature = sig;
				if (bandParts.length) {
					this.pushAudit('audit-poll', 'band', 'adaptive', `situation snapshot · ${bandParts.join(' · ')}`);
				}
				this.pushAudit(
					'audit-poll',
					'info',
					'adaptive',
					`degradation=${a.degradationTier} shadow_eval=${a.lastShadowEval ?? 'n/a'} audit_trail=${a.auditTrail.length}`,
				);
				for (const row of a.auditTrail.slice(-4)) {
					this.pushAudit(
						'audit-poll',
						'info',
						'degradation',
						JSON.stringify(row).slice(0, 300),
						row,
					);
				}
			}
			if (this.auditRung === 'unavailable' || this.auditRung === 'connecting') {
				this.auditRung = 'poll';
			}
		} catch {
			// Poll rung itself down — WS rung may still carry the stream.
			if (!this.socket && this.auditRung !== 'unavailable') {
				this.auditRung = 'unavailable';
				this.pushAudit('audit-poll', 'error', 'deck', 'audit poll unreachable and no WS channel open');
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
		if (!ev) return;
		this.pushAudit('operator', 'info', 'operator', `subscribed to audit channel for ${ev.eventId}`);
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
					);
				}
			},
			WS_OPEN_TIMEOUT_MS,
		);
		this.socket = sock;
	}

	private onWsEvent(ev: BrainWsEvent) {
		const payload = ev.payload ?? {};
		switch (ev.type) {
			case 'convoy.status':
			case 'event.status':
				this.pushAudit(
					this.auditRung === 'ws-event' ? 'ws-event' : 'ws-convoy',
					'info',
					'brain',
					`${ev.type} ${JSON.stringify(payload).slice(0, 240)}`,
					payload,
				);
				break;
			case 'convoy.position':
			case 'event.position':
				// high-frequency — only every 6th frame keeps the log readable
				if (this.auditSeq % 6 === 0) {
					this.pushAudit(
						this.auditRung === 'ws-event' ? 'ws-event' : 'ws-convoy',
						'info',
						'tracking',
						`position ${JSON.stringify(payload).slice(0, 160)}`,
					);
				}
				break;
			case 'situation.changed':
				this.pushAudit('ws-event', 'band', 'adaptive', `band → ${String(payload['band'] ?? payload['situation_class'] ?? '?')}`, payload);
				break;
			default:
				this.pushAudit(
					this.auditRung === 'ws-event' ? 'ws-event' : 'ws-convoy',
					'info',
					'brain',
					`${ev.type} ${JSON.stringify(payload).slice(0, 200)}`,
					payload,
				);
		}
	}

	/** On-demand NDJSON probe into the brain's audit channel (decider→Gemma→tools). */
	async runAuditProbe(message: string) {
		if (this.auditProbeRunning) return;
		this.auditProbeRunning = true;
		this.ndjsonText = '';
		this.pushAudit('operator', 'info', 'operator', `audit probe: "${message.slice(0, 140)}"`);
		let tokens = '';
		try {
			await streamAuditProbe(message, {
				onThought: (text) => {
					this.pushAudit('ndjson-chat', 'decision', 'decider', text.slice(0, 400));
				},
				onToken: (text) => {
					tokens += text;
					this.ndjsonText = tokens.slice(-2000);
				},
				onToolCall: (name, args) => {
					this.pushAudit('ndjson-chat', 'tool', `mcp:${name}`, `call ${JSON.stringify(args).slice(0, 200)}`, args);
				},
				onToolResult: (name, state, dur) => {
					this.pushAudit('ndjson-chat', 'tool', `mcp:${name}`, `result ${state} in ${dur}ms`);
				},
				onDone: () => {
					if (tokens) this.pushAudit('ndjson-chat', 'model', 'gemma', tokens.slice(0, 600));
				},
				onError: (msg) => {
					this.pushAudit('ndjson-chat', 'error', 'chat/stream', msg.slice(0, 300));
				},
			});
		} finally {
			this.auditProbeRunning = false;
		}
	}

	clearAudit() {
		this.audit = [];
		this.lastAuditSignature = '';
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
		this.pushAudit('operator', 'info', 'operator', `twin shadow-eval: ${u.scenarioId} candidate ${this.twinCandidate.advanceClosureSec}s/${this.twinCandidate.closureType}`);
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
			);
		} catch (err) {
			this.twinError = err instanceof Error ? err.message : String(err);
			this.pushAudit('operator', 'error', 'twin', `compare failed: ${this.twinError}`);
		} finally {
			this.twinRunning = false;
		}
	}

	// ─── emulator ─────────────────────────────────────────────────────────────

	async pollEmulator() {
		const health = await emulatorHealth();
		this.emulator = {
			...this.emulator,
			online: health.online,
			healthDetail: health.detail,
			lastChecked: Date.now(),
		};
		if (!health.online) return;
		try {
			const [useCases, runs] = await Promise.all([
				emulatorUseCases(),
				emulatorListRuns(8),
			]);
			this.emulatorUseCaseIds = useCases.map((u) => u.id);
			this.emulatorRuns = runs.map((r) => ({
				runId: r.runId,
				useCaseId: r.useCaseId,
				status: r.status,
				clockScale: r.clockScale,
				isReplay: r.isReplay,
			}));
		} catch {
			// health said online but the API probes failed — keep last-good
		}
	}

	async stageUseCase(useCaseId: string, clockScale?: number) {
		const u = useCaseById(useCaseId);
		const id = u ? u.id : useCaseId;
		const res = await emulatorStageUseCase(id, clockScale ? { clockScale } : undefined);
		this.emulator = {
			...this.emulator,
			runRequested: res.ok ? id : this.emulator.runRequested,
			runError: res.ok ? null : res.detail,
		};
		this.pushAudit(
			'operator',
			res.ok ? 'info' : 'error',
			'emulator',
			`stage ${id} → ${res.detail.slice(0, 200)}`,
		);
		if (res.ok) void this.pollEmulator();
	}

	// ─── decision queue: enforcement, dispatch, signals, watchlist ────────────

	/** Commander counters (the four DataTiles). Live-derived, fixture-honest. */
	get counters() {
		const pendingRecs = this.recommendations.filter((r) => r.status === 'pending').length;
		const requestedDispatches = this.dispatches.filter((d) => d.status === 'requested').length;
		const unitsOut = this.dispatches.filter((d) =>
			d.status === 'approved' || d.status === 'en_route' || d.status === 'on_scene',
		).length;
		const ambulanceWindows = this.dispatches.filter(
			(d) => d.agency === 'ambulance' && (d.status === 'approved' || d.status === 'en_route' || d.status === 'on_scene'),
		).length;
		return {
			activeEvents: this.events.filter((e) => e.source !== 'offline-fixture' && e.state === 'active').length,
			pendingDecisions: pendingRecs + requestedDispatches,
			unitsDispatched: unitsOut,
			ambulanceWindows,
		};
	}

	/** True when a human is on console — every writer action requires this. */
	get officerOn(): boolean {
		return this.dutyOfficer.trim().length >= 2;
	}

	setDutyOfficer(name: string) {
		this.dutyOfficer = name.slice(0, 120);
		this.dutyOfficerNote = this.officerOn
			? null
			: 'a named duty officer must be on console before approve / reject / dispatch / signal actions can commit';
	}

	/** Require the officer for a commit; push refusal into the audit trail. */
	private requireOfficerFor(operation: string): string | null {
		const who = this.dutyOfficer.trim();
		if (who.length < 2) {
			this.dutyOfficerNote = `${operation} refused — set the duty officer identity first (no automated enforcement)`;
			this.pushAudit('operator', 'error', 'gate', `${operation} refused: no named duty officer on console`);
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

	async decideRecommendation(id: string, decision: 'approve' | 'reject', note = '') {
		const officer = this.requireOfficerFor(`e-challan ${decision}`);
		if (!officer) return false;
		const rec = this.recommendations.find((r) => r.recommendationId === id);
		this.recommendationBusy = id;
		this.pushAudit(
			'operator',
			'decision',
			officer,
			`e-challan ${decision} ${rec?.plate ?? id.slice(0, 8)}${note ? ` — ${note.slice(0, 140)}` : ''}`,
		);
		try {
			await decideEnforcementRecommendation(id, { decision, officer, note });
			this.pushAudit('operator', 'info', 'oracle', `recommendation ${id.slice(0, 8)} now ${decision === 'approve' ? 'approved' : 'rejected'}`);
			await this.pollRecommendations();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `decision failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
			void this.pollRecommendations();
			return false;
		} finally {
			this.recommendationBusy = null;
		}
	}

	async pollDispatches() {
		try {
			const rows = await fetchDispatches(40);
			this.dispatches = rows;
			this.dispatchesLive = true;
			this.dispatchesUpdated = Date.now();
			this.dispatchesNote = null;
		} catch (err) {
			this.dispatchesLive = false;
			this.dispatchesNote =
				err instanceof ApiNotDeployed
					? 'dispatches API not deployed (404)'
					: `dispatches unavailable: ${err instanceof Error ? err.message : String(err)}`;
		}
	}

	async requestJanRakshak(eventId: string, unitId: string, waypointJunction?: number) {
		const officer = this.requireOfficerFor('unit dispatch request');
		if (!officer) return false;
		this.dispatchBusy = eventId;
		try {
			const rec = await requestDispatch(eventId, {
				agency: 'jan_rakshak',
				unitId,
				...(waypointJunction != null ? { waypointJunction } : {}),
				requestedBy: officer,
			});
			this.pushAudit('operator', 'decision', officer, `Jan Rakshak ${unitId} requested for ${eventId.slice(0, 8)} → dispatch ${rec?.dispatchId.slice(0, 8) ?? '?'}`);
			await this.pollDispatches();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `dispatch request failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
			return false;
		} finally {
			this.dispatchBusy = null;
		}
	}

	async decideDispatchAction(id: string, action: 'approve' | 'defer', note = '') {
		const officer = this.requireOfficerFor(`dispatch ${action}`);
		if (!officer) return false;
		this.dispatchBusy = id;
		try {
			const rec = await decideDispatch(id, { action, officer, note });
			this.pushAudit('operator', 'decision', officer, `dispatch ${id.slice(0, 8)} ${action === 'approve' ? 'approved' : 'deferred'}${rec?.unitId ? ` (${rec.unitId})` : ''}`);
			await this.pollDispatches();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `dispatch decision failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
			void this.pollDispatches();
			return false;
		} finally {
			this.dispatchBusy = null;
		}
	}

	async advanceDispatchStatus(id: string, status: 'en_route' | 'on_scene' | 'completed' | 'cancelled', note = '') {
		const officer = this.requireOfficerFor(`dispatch ${status}`);
		if (!officer) return false;
		this.dispatchBusy = id;
		try {
			await advanceDispatch(id, { status, officer, note });
			this.pushAudit('operator', 'decision', officer, `dispatch ${id.slice(0, 8)} → ${status}`);
			await this.pollDispatches();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `dispatch status failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
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
	 * baseline' — never a modal, never hidden (motion doctrine).
	 */
	async applyCityPlan(band?: BandCode) {
		const officer = this.requireOfficerFor('signal plan apply');
		if (!officer) return;
		if (this.signalApplying) return;
		this.signalApplying = true;
		this.pushAudit('operator', 'decision', officer, `signal plan APPLY requested${band ? ` (band ${band})` : ''}`);
		try {
			const outcome = await applySignalPlan(
				{
					...(band ? { band } : {}),
					eventId: this.selectedEventId ?? undefined,
				},
				officer,
			);
			this.signalApply = outcome;
			if (outcome.applied) {
				this.pushAudit('operator', 'info', 'signal-writer', `plan applied (${outcome.status})`);
			} else {
				this.pushAudit(
					'operator',
					'band',
					'signal-writer',
					`plan HELD at baseline (${outcome.status}${outcome.refusalReason ? `: ${outcome.refusalReason}` : ''})`,
					outcome.detail,
				);
			}
			await this.pollSignalPlans();
		} finally {
			this.signalApplying = false;
		}
	}

	async restorePrograms(all = false) {
		const officer = this.requireOfficerFor('signal program restore');
		if (!officer) return;
		this.pushAudit('operator', 'decision', officer, `signal programs RESTORE${all ? ' (all)' : ''}`);
		try {
			await restoreSignalPrograms(all ? { all: true } : { eventId: this.selectedEventId ?? undefined }, officer);
			await this.pollSignalPlans();
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `restore failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
		}
	}

	async loadSignalHistory() {
		this.signalHistoryOpen = true;
		try {
			this.signalHistory = await fetchSignalHistory(20);
		} catch {
			this.signalHistory = [];
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
		this.pushAudit('operator', 'decision', officer, `earmark ${plate} → ${state} (basis ${lawfulBasis}, ${retentionDays}d)`);
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
			this.pushAudit('operator', 'error', 'oracle', `earmark failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
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
			this.pushAudit('operator', 'decision', officer, `earmark cleared for vehicle ${vehicleId.slice(0, 8)}`);
			await this.pollWatchlist();
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `earmark clear failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
			return false;
		} finally {
			this.watchBusy = null;
		}
	}

	async loadInterception(eventId: string) {
		try {
			const plan = await fetchInterceptionPlan(eventId);
			this.interception = plan;
			this.interceptionUpdated = Date.now();
			this.interceptionNote = null;
		} catch (err) {
			this.interception = null;
			this.interceptionNote =
				err instanceof ApiNotDeployed
					? 'interception-plan not deployed (404)'
					: `no interception plan: ${err instanceof Error ? err.message : String(err)}`;
		}
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

	/** Open a pursuit for a watchlist vehicle — human-initiated (C6). */
	async openPursuit(vehicleId: string, plate: string | null, seedSegmentId: number, recommendationId?: string) {
		const officer = this.requireOfficerFor('pursuit open');
		if (!officer) return false;
		this.pursuitBusy = vehicleId;
		this.pushAudit('operator', 'decision', officer, `pursuit OPEN for ${plate ?? vehicleId.slice(0, 8)}`);
		try {
			const { eventId } = await openPursuitEvent({
				vehicleId,
				...(recommendationId ? { recommendationId } : {}),
				plate,
				seedSegmentId,
				actor: officer,
			});
			this.pushAudit('operator', 'info', 'oracle', `pursuit event ${eventId.slice(0, 8)} opened`);
			await this.pollBoard();
			this.selectedEventId = eventId;
			await this.loadInterception(eventId);
			return true;
		} catch (err) {
			this.pushAudit('operator', 'error', 'oracle', `pursuit open failed: ${err instanceof Error ? err.message : String(err)}`.slice(0, 240));
			return false;
		} finally {
			this.pursuitBusy = null;
		}
	}
}

function runsSource(rung: string): AuditEntry['source'] {
	return rung === 'ws-event' ? 'ws-event' : rung === 'ws-convoy' ? 'ws-convoy' : 'operator';
}

function fmtNum(n: number | null | undefined): string {
	return n == null ? 'n/a' : n.toFixed(1);
}

export const ops = new OpsStore();
