<script lang="ts">
	/**
	 * EntityDossier — the right drawer of VEER CITY COMMAND: one entity, its
	 * 1-hop neighbours and its audit trail, from
	 * GET /api/v1/city/entity/{kind}/{id} (contract C-C), opened by any
	 * selection (a map pick, a fusion-rail row, a vital, a related chip).
	 *
	 *   HEADER   kind · label · LIVE/EMULATED source · state · age of the
	 *            entity's own time; fly-to and close
	 *   PLATE    vehicles, reads and e-challans: MASKED (GJ27EM•••1) until a
	 *            named duty officer is on console — the officer field here
	 *            sets ops.dutyOfficer (C6)
	 *   FACTS    the kind's curated fields (nothing invented: absent → omitted)
	 *   PURSUIT  for a pursuit (or its vehicle): the interception plan's honesty
	 *            — stale sighting, direction basis, against a one-way, the
	 *            legal onward segments, chokes likely passed
	 *   RELATED  1-hop chips grouped by relation; a click selects and flies
	 *   AUDIT    human rows on the act-orange rail with the officer; machine
	 *            rows with their tier and confidence; system rows dim
	 *   LINKS    deep links into the decision wall (/ops#rec=…, #dsp=…, #evt=…)
	 *   MAP      what exactly was clicked on the canvas (the layer's own line)
	 *   EXPLAIN  'Explain this' streams the agent (POST /chat/stream with
	 *            context_urn, C-D2) into the drawer — plates masked unless an
	 *            officer is named, cancellable; any canvas proposal it makes
	 *            queues as an AI-suggests chip (a human applies it)
	 *
	 * The officer field is hidden while the signed-in session names the duty
	 * officer (ops.dutyOfficerFromSession): the identity is the session's.
	 * A corridor event created with an operator label (an acceptance test, a
	 * rehearsal) wears a TEST pill — it never reads as a real movement.
	 */
	import { onDestroy, untrack } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { corridor } from '$lib/ops/components/template/corridor.svelte';
	import { t, kindLabel } from '$lib/i18n/strings';
	import '$lib/i18n/packs.agent';
	import { streamChat } from '$lib/api/client';
	import { canvasBus } from '../canvas-bus.svelte';
	import { selection } from '../entities/selection.svelte';
	import { dossier } from '../entities/dossier.svelte';
	import { cityRegistry } from '../entities/registry.svelte';
	import { godsEye } from '../store.svelte';
	import { displayPlate, displayText, maskPlate } from '../plates';
	import { parseUrn, type CityEntity, type FeedRow } from '../city-api';
	import { fetchInterceptionDetail, type InterceptionDetail } from '../data';
	import { ageOf, fmtAge, istDayTime, istStamp, kindWord } from './format';

	let now = $state(Date.now());
	$effect(() => {
		const timer = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(timer);
	});

	// The open dossier follows the selection.
	$effect(() => {
		const urn = selection.current;
		untrack(() => dossier.open(urn));
	});

	// Leaving City Command (or closing the drawer) stops the 6 s dossier refresh.
	onDestroy(() => {
		dossier.close();
		explainCtrl?.abort();
	});

	// ─── Explain this (the agent, grounded in this entity — C-D2) ─────────────

	type Explain = { urn: string; text: string; status: 'streaming' | 'done' | 'error'; tiers: string[]; note: string | null };
	let explain = $state<Explain | null>(null);
	let explainCtrl: AbortController | null = null;
	let explainSession: string | null = null;

	// A new selection drops the previous entity's answer (and stops its stream).
	$effect(() => {
		const urn = selection.current;
		untrack(() => {
			if (explain && explain.urn !== urn) {
				explainCtrl?.abort();
				explainCtrl = null;
				explain = null;
			}
		});
	});

	function explainThis() {
		const urn = selection.current;
		if (!urn) return;
		explainCtrl?.abort();
		const ctrl = new AbortController();
		explainCtrl = ctrl;
		explain = { urn, text: '', status: 'streaming', tiers: [], note: null };
		const ev = parseUrn(urn)?.kind === 'event' ? parseUrn(urn)?.id : null;
		void streamChat(
			'Explain this entity for the duty officer: what it is, its current state and source, ' +
				'what the city picture links it to, and what (if anything) waits on a named officer. ' +
				'Use names and labels, never ids.',
			null,
			null,
			{
				onToken: (tok) => {
					if (explain && explain.urn === urn) explain.text += tok;
				},
				onThought: () => {},
				onToolCall: () => {},
				onToolResult: () => {},
				onDirective: (d) => canvasBus.offer(d, null),
				onDone: (d) => {
					if (d.sessionId) explainSession = d.sessionId;
					if (explain && explain.urn === urn) {
						explain.status = 'done';
						explain.tiers = d.tiersUsed;
					}
				},
				onError: (e) => {
					if (ctrl.signal.aborted || !explain || explain.urn !== urn) return;
					explain.status = 'error';
					explain.note = e.message;
				},
			},
			{ sessionId: explainSession, signal: ctrl.signal, contextUrn: urn, eventId: ev ?? null },
		).catch((e) => {
			if (ctrl.signal.aborted || !explain || explain.urn !== urn) return;
			explain.status = 'error';
			explain.note = e instanceof Error ? e.message : String(e);
		});
	}

	function cancelExplain() {
		explainCtrl?.abort();
		explainCtrl = null;
		if (explain) {
			explain.status = 'done';
			if (!explain.text) explain = null;
		}
	}

	const lang = $derived(ops.lang);
	const officer = $derived(ops.officerOn);
	/**
	 * The loader's answer only when it is THIS selection's: for the one render
	 * between a selection change and the effect that re-opens the loader, the
	 * drawer shows the registry's row for the new entity — never the previous
	 * entity's facts, chips or audit (nor a pursuit read for the old subject).
	 */
	const current = $derived(dossier.urn === selection.current);
	const data = $derived(current ? dossier.data : null);
	const dossierState = $derived(current ? dossier.state : 'loading');
	const entity = $derived(data?.entity ?? (selection.current ? cityRegistry.get(selection.current) : null));
	const parsed = $derived(parseUrn(selection.current));

	let officerDraft = $state('');

	function setOfficer() {
		const name = officerDraft.trim();
		if (name.length >= 2) ops.setDutyOfficer(name);
	}

	function str(v: unknown): string | null {
		return typeof v === 'string' && v.length ? v : v != null && typeof v !== 'object' ? String(v) : null;
	}

	function num(v: unknown): number | null {
		return typeof v === 'number' && Number.isFinite(v) ? v : null;
	}

	function epoch(v: unknown): number | null {
		if (typeof v !== 'string') return null;
		const ms = Date.parse(v);
		return Number.isFinite(ms) ? ms : null;
	}

	/** The entity's title as this console may show it. */
	function titleOf(e: CityEntity): string {
		const plate = str(e.props.plate);
		if (officer && plate) {
			const masked = str(e.props.plate_masked) ?? maskPlate(plate);
			return masked ? e.label.split(masked).join(plate) : e.label;
		}
		if (officer && e.kind === 'event' && str(e.props.title)) return str(e.props.title) as string;
		return e.label;
	}

	/** Row text as this console may show it (the server masked it; an officer sees the plate). */
	function rowText(r: FeedRow): string {
		const plate = str(r.props.plate);
		const masked = str(r.props.plate_masked);
		if (officer && plate && masked) return r.text.split(masked).join(plate);
		return r.text;
	}

	type Fact = { k: string; v: string; tone?: 'act' | 'dim' | 'emu' };

	/** Curated facts per kind — only fields the oracle sent. */
	const facts = $derived.by((): Fact[] => {
		const e = entity;
		if (!e) return [];
		const p = e.props;
		const out: Fact[] = [];
		const push = (k: string, v: unknown, tone?: Fact['tone']) => {
			const s = str(v);
			if (s != null && s !== '') out.push({ k, v: s, tone });
		};
		switch (e.kind) {
			case 'event': {
				push('kind', kindLabel(lang, str(p.event_kind)));
				push('status', p.status);
				push('protection', p.protection_class);
				push('crowd', p.crowd_class);
				if (epoch(p.planned_start)) push('planned', `${istDayTime(epoch(p.planned_start))} → ${istDayTime(epoch(p.planned_end))} IST`);
				const spines = Array.isArray(p.route_spines) ? (p.route_spines as Array<Record<string, unknown>>) : [];
				if (spines.length) {
					push(
						'spines',
						spines
							.map((s) => `${str(s.label) ?? 'spine'} (${Array.isArray(s.segment_ids) ? s.segment_ids.length : 0} segs)`)
							.join(' · '),
					);
				}
				const meta = (p.metadata ?? {}) as Record<string, unknown>;
				push('FIR', meta.fir);
				push('opened by', meta.opened_by, 'act');
				if (meta.source) push('producer', `${meta.source}${meta.run_id != null ? ` · run ${meta.run_id}` : ''}${meta.mode ? ` · ${meta.mode}` : ''}`);
				if (e.band) push('band', `${e.band}${p.band_code ? ` · ${p.band_code}` : ''}`);
				break;
			}
			case 'vehicle':
				push('earmark', p.earmark_state, p.earmark_state === 'flagged' ? 'act' : undefined);
				push('lawful basis', p.lawful_basis);
				push('reason', displayText(str(p.earmark_reason), officer));
				if (epoch(p.retention_until)) push('retention until', `${istDayTime(epoch(p.retention_until))} IST (server-set)`);
				push('class', p.vehicle_class);
				push('last camera', p.last_camera_id);
				if (num(p.last_speed_kmh) != null) push('last speed', `${num(p.last_speed_kmh)} km/h`);
				push('hit', p.matched_plate_hit, 'act');
				push('privacy', p.privacy_note, 'dim');
				break;
			case 'sighting':
				push('camera', p.camera_id);
				if (num(p.speed_kmh) != null) push('speed', `${num(p.speed_kmh)} km/h`);
				if (num(p.heading) != null) push('heading', `${num(p.heading)}°`);
				push('hit', p.matched_plate_hit, 'act');
				push('read source', p.read_source, e.source === 'emulated' ? 'emu' : undefined);
				if (p.run_id != null) push('emulator run', p.run_id, 'emu');
				break;
			case 'camera':
				push('kind', str(p.camera_kind)?.toUpperCase());
				push('registry', `${p.camera_source ?? '—'}${p.registered ? ' · registered' : ''}`);
				if (p.junction_id != null) push('junction', `J${p.junction_id}`);
				if (p.segment_id != null) push('segment', p.segment_id);
				if (num(p.heading_deg) != null) push('facing', `${Math.round(num(p.heading_deg) as number)}°`);
				push('reads · 15 min', p.reads_15m);
				push('watchlist hits · 15 min', p.hits_15m, num(p.hits_15m) ? 'act' : undefined);
				if (epoch(p.last_read_t)) push('last read', `${istStamp(epoch(p.last_read_t), now)} IST`);
				push('data', 'location + counts only — no frame, image or plate', 'dim');
				break;
			case 'junction': {
				push('name', p.name);
				push('ગુજરાતી', p.name_gu);
				push('type', p.junction_type);
				push('signal', p.signal_control ? 'signalised' : 'unsignalised');
				push('program', p.program_source);
				if (num(p.cycle_sec) != null) push('cycle', `${p.cycle_sec}s · offset ${p.offset_sec ?? 0}s · green ratio ${p.green_ratio ?? '—'}`);
				const ap = (p.active_phase ?? null) as Record<string, unknown> | null;
				if (ap) {
					const ms = (ap.movement_states ?? {}) as Record<string, unknown>;
					push(
						'phase now',
						`φ${ap.index ?? '?'} ${ap.lost ? 'ALL-RED' : 'green'} · ${Object.entries(ms)
							.map(([m, st]) => `${m} ${st}`)
							.join(', ')}${num(ap.remaining_sec) != null ? ` · ${ap.remaining_sec}s left` : ''}`,
					);
				}
				push('phase basis', p.phase_basis === 'program_clock' ? 'program clock (not controller telemetry)' : p.phase_basis, 'dim');
				break;
			}
			case 'unit':
				push('agency', p.agency_label ?? p.agency);
				push('status', e.state);
				if (num(p.eta_sec) != null) push('ETA', `${Math.round(num(p.eta_sec) as number)}s`);
				if (num(p.progress) != null) push('progress', `${Math.round((num(p.progress) as number) * 100)}%`);
				if (p.waypoint_junction != null) push('waypoint', `J${p.waypoint_junction}`);
				push('source', `${p.unit_source ?? 'emulated'} · EMULATED`, 'emu');
				if (p.double_booked) push('conflict', 'approved on several live dispatches', 'act');
				break;
			case 'convoy':
				push('movement', p.movement_id);
				if (num(p.speed_kmh) != null) push('speed', `${Math.round(num(p.speed_kmh) as number)} km/h`);
				if (num(p.heading) != null) push('heading', `${Math.round(num(p.heading) as number)}°`);
				if (num(p.progress) != null) push('progress', `${Math.round((num(p.progress) as number) * 100)}% of the primary route`);
				if (p.segment_id != null) push('segment', p.segment_id);
				push('position', p.position_source === 'emulated_gps' ? 'EMULATED GPS (route-walking simulator)' : p.position_source, p.position_source === 'emulated_gps' ? 'emu' : undefined);
				push('note', p.note, 'dim');
				break;
			case 'dispatch':
				push('agency', p.agency_label ?? p.agency);
				// The dispatch RECORD is live (a real request + decision); the unit
				// answering it is emulated (projection / C-A) — said on the unit.
				if (p.unit_emulated === true && str(p.unit_id)) push('unit', `${p.unit_id} · ${t(lang, 'cc_emulated')}`, 'emu');
				else push('unit', p.unit_id);
				push('status', e.state, e.state === 'requested' ? 'act' : undefined);
				push('for', displayText(str(p.event_title), officer));
				if (p.waypoint_junction != null) push('waypoint', `J${p.waypoint_junction}`);
				push('requested by', p.requested_by);
				push('decided by', p.decided_by, 'act');
				if (epoch(p.approved_at)) push('approved', `${istStamp(epoch(p.approved_at), now)} IST`);
				break;
			case 'recommendation': {
				push('rule', p.rule_id);
				push('status', str(p.status)?.toUpperCase(), p.status === 'pending' ? 'act' : undefined);
				const ev = (p.evidence ?? {}) as Record<string, unknown>;
				if (num(ev.speed_kmh) != null) {
					push(
						'evidence',
						`${ev.speed_kmh} km/h in a ${ev.speed_limit_kmh ?? '—'} km/h zone${num(ev.excess_kmh) != null ? ` (+${ev.excess_kmh})` : ''}${
							ev.road_name ? ` on ${ev.road_name}` : ''
						}`,
					);
				}
				push('camera', p.camera_id);
				push('decided by', p.decided_by, 'act');
				if (epoch(p.decided_at)) push('decided', `${istStamp(epoch(p.decided_at), now)} IST`);
				push('proposal', displayText(str(p.recommendation), officer), 'dim');
				push('rail', p.status === 'pending' ? 'a named officer must decide — no automated penalty' : 'decided by a named officer (append-only audit)', 'dim');
				break;
			}
			case 'anomaly':
				push('type', p.anomaly_type);
				push('severity', p.severity);
				push('road', p.road_name ?? (p.segment_id != null ? `segment ${p.segment_id}` : null));
				push('in 15 min', p.count_15m);
				if (Array.isArray(p.anomaly_flags)) push('flags', (p.anomaly_flags as unknown[]).join(', '));
				break;
			case 'segment':
				push('road', p.road_name);
				push('class', p.road_class);
				push('lanes', p.lanes);
				push('limit', p.speed_limit_kmh != null ? `${p.speed_limit_kmh} km/h` : null);
				push('one-way', p.oneway === true ? 'yes' : p.oneway === false ? 'no' : null);
				if (num(p.length_m) != null) push('length', `${Math.round(num(p.length_m) as number)} m`);
				break;
		}
		return out;
	});

	const plate = $derived(entity && str(entity.props.plate) ? (entity.props.plate as string) : null);

	// ─── related, grouped ──────────────────────────────────────────────────────

	const RELATION_ORDER = [
		'pursued_vehicle',
		'vehicle',
		'pursuit',
		'authorizing_recommendation',
		'recommendation',
		'evidence_sighting',
		'sighting',
		'seen_by_camera',
		'camera',
		'recent_hit',
		'dispatch',
		'unit',
		'convoy',
		'event',
		'assigned_event',
		'waypoint_junction',
		'junction',
		'signal_junction',
		'dispatch_waypoint',
		'route_segment',
		'adjacent_segment',
		'adjacent_junction',
		'spine_of',
		'signal_event',
		'segment',
		'anomaly',
	];

	const groups = $derived.by(() => {
		const m = new Map<string, CityEntity[]>();
		for (const r of data?.related ?? []) {
			const list = m.get(r.relation);
			if (list) list.push(r.entity);
			else m.set(r.relation, [r.entity]);
		}
		const rank = (rel: string) => {
			const i = RELATION_ORDER.indexOf(rel);
			return i < 0 ? RELATION_ORDER.length : i;
		};
		return [...m.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
	});

	function chipDetail(e: CityEntity): string {
		const p = e.props;
		switch (e.kind) {
			case 'sighting':
				return `${p.camera_id ?? '—'} · ${istStamp(e.t, now)}${num(p.speed_kmh) != null ? ` · ${p.speed_kmh} km/h` : ''}`;
			case 'recommendation':
				return `${str(p.status)?.toUpperCase() ?? '—'}${p.decided_by ? ` · ${p.decided_by}` : ''}`;
			case 'camera':
				return `${num(p.reads_15m) ?? 0} reads · ${num(p.hits_15m) ?? 0} hits`;
			case 'event':
				return `${str(p.event_kind)?.replace(/_/g, ' ') ?? ''} · ${e.state ?? '—'}`;
			default:
				return e.state ?? '';
		}
	}

	function go(e: CityEntity) {
		selection.select(e.urn, { fly: true, source: 'dossier' });
	}

	// ─── pursuit facts (interception plan honesty, WP-03) ──────────────────────

	const pursuitEventId = $derived.by((): string | null => {
		const e = entity;
		if (!e) return null;
		if (e.kind === 'event' && e.props.event_kind === 'vehicle_pursuit') return e.id;
		if (e.kind === 'vehicle') {
			const rel = (data?.related ?? []).find((r) => r.relation === 'pursuit' && r.entity.state !== 'completed' && r.entity.state !== 'cancelled');
			return rel?.entity.id ?? null;
		}
		return null;
	});

	/**
	 * The pursuit's interception plan, read for this dossier (the canvas
	 * overlay keeps its own copy for the board's selected pursuit) and
	 * re-read with every dossier refresh.
	 */
	let plan = $state.raw<InterceptionDetail | null>(null);
	$effect(() => {
		const id = pursuitEventId;
		void data?.fetchedAt;
		untrack(() => {
			if (!id) {
				plan = null;
				return;
			}
			if (plan?.eventId !== id && godsEye.interception?.eventId === id) plan = godsEye.interception;
			void fetchInterceptionDetail(id, AbortSignal.timeout(12_000))
				.then((d) => {
					if (pursuitEventId === id) plan = d;
				})
				.catch(() => {
					if (pursuitEventId === id && plan?.eventId !== id) plan = null;
				});
		});
	});

	function junctionName(id: number): string {
		const fromPlan = plan?.chokes.find((c) => c.junctionId === id)?.name;
		if (fromPlan) return fromPlan;
		const j = cityRegistry.get(`junction:${id}`);
		return j && j.label !== `junction:${id}` ? j.label : `J${id}`;
	}

	function segmentName(id: number): string {
		return godsEye.segmentById.get(id)?.roadName ?? `segment ${id}`;
	}

	// ─── audit ─────────────────────────────────────────────────────────────────

	const audit = $derived(data?.audit ?? []);

	function machineDetail(r: FeedRow): string {
		const tier = str(r.props.tier);
		const conf = num(r.props.confidence);
		const bits: string[] = [];
		if (tier) bits.push(`tier ${tier}`);
		if (conf != null) bits.push(`conf ${conf.toFixed(2)}`);
		if (!tier && r.kind === 'enforcement' && str(r.props.rule_id)) bits.push(`rule ${r.props.rule_id}`);
		if (r.kind === 'anpr' && str(r.props.hit)) bits.push(String(r.props.hit));
		return bits.join(' · ');
	}

	// ─── deep links ────────────────────────────────────────────────────────────

	const links = $derived.by((): Array<{ href: string; label: string }> => {
		const e = entity;
		if (!e) return [];
		const out: Array<{ href: string; label: string }> = [];
		if (e.kind === 'recommendation' && e.state === 'pending') out.push({ href: `/ops#rec=${e.id}`, label: t(lang, 'cc_decide_queue') });
		if (e.kind === 'dispatch' && e.state === 'requested') out.push({ href: `/ops#dsp=${e.id}`, label: t(lang, 'cc_decide_queue') });
		if (e.kind === 'vehicle') {
			const pending = (data?.related ?? []).find((r) => r.entity.kind === 'recommendation' && r.entity.state === 'pending');
			if (pending) out.push({ href: `/ops#rec=${pending.entity.id}`, label: `${t(lang, 'cc_decide_queue')} (${pending.entity.id.slice(0, 8)})` });
		}
		if (e.kind === 'event') out.push({ href: `/ops#evt=${e.id}`, label: t(lang, 'cc_open_wall') });
		return out;
	});

	const entityAge = $derived(entity?.t != null ? ageOf(entity.t, now) : null);
	/** A labelled (test / rehearsal) corridor event: metadata.label. */
	const testLabel = $derived.by((): string | null => {
		if (entity?.kind !== 'event') return null;
		const meta = (entity.props.metadata ?? {}) as Record<string, unknown>;
		return typeof meta.label === 'string' && meta.label.trim() ? meta.label.trim() : null;
	});

	/**
	 * Provenance, not freshness: road segments and feed anomalies stand on the
	 * signal-ingress feed, which is SYNTHETIC while the corridor summary says
	 * so — such an entity reads SIMULATED FEED, never LIVE.
	 */
	const feedBacked = $derived(!!entity && entity.source !== 'emulated' && (entity.kind === 'anomaly' || entity.kind === 'segment'));
	const simFeed = $derived(
		feedBacked && (entity?.props.data_quality === 'synthetic' || corridor.provenance === 'simulated'),
	);
	/** The summary has not said what the ingest rows are (yet): never LIVE by default. */
	const feedUnverified = $derived(feedBacked && !simFeed && corridor.provenance === 'unverified');
</script>

{#if selection.current}
	<div class="dossier" data-testid="entity-dossier" data-urn={selection.current} data-state={dossierState}>
		<header class="d-head">
			<div class="d-kicker">
				<span class="kind mono">{kindWord(parsed?.kind, typeof entity?.props.rule_id === 'string' ? entity.props.rule_id : null)}</span>
				{#if entity}
					<span class="src" class:emu={entity.source === 'emulated'} class:sim={simFeed} class:unverified={feedUnverified} data-testid="dossier-source">
						{entity.source === 'emulated'
							? t(lang, 'cc_emulated')
							: simFeed
								? t(lang, 'cc_simulated_feed')
								: feedUnverified
									? t(lang, 'cc_provenance_unverified')
									: 'LIVE'}
					</span>
					{#if entity.kind === 'dispatch' && entity.props.unit_emulated === true}
						<span class="src emu" data-testid="dossier-unit-emulated">UNIT · {t(lang, 'cc_emulated')}</span>
					{/if}
					{#if testLabel}<span class="src test" data-testid="dossier-test" title={testLabel}>{t(lang, 'dossier_test')} · {displayText(testLabel, officer)}</span>{/if}
					{#if entity.state}<span class="state mono">{entity.state.replace(/_/g, ' ')}</span>{/if}
					{#if entityAge != null}<span class="age mono" title={entity.t != null ? `${istDayTime(entity.t)} IST` : undefined}>{fmtAge(entityAge)}</span>{/if}
				{/if}
				<span class="spacer"></span>
				<button type="button" class="icon" title={t(lang, 'cc_fly')} aria-label={t(lang, 'cc_fly')} onclick={() => selection.select(selection.current, { fly: true, source: 'dossier' })}>⌖</button>
				<button type="button" class="icon" title={t(lang, 'cc_close')} aria-label={t(lang, 'cc_close')} onclick={() => selection.clear()}>✕</button>
			</div>
			<h2 class="d-title" data-testid="dossier-title">{entity ? titleOf(entity) : selection.current}</h2>
			{#if dossierState === 'loading' && !data}
				<p class="d-note mono">{t(lang, 'cc_loading_dossier')}</p>
			{:else if dossierState === 'not-found'}
				<p class="d-note mono">{t(lang, 'cc_not_found')} — {dossier.note}</p>
			{:else if current && dossier.note}
				<p class="d-note mono">{dossier.note}</p>
			{/if}
		</header>

		{#if plate}
			<section class="d-sec plate-sec" data-testid="dossier-plate">
				<div class="plate-row">
					<span class="plate mono" data-masked={officer ? 'false' : 'true'}>{displayPlate(plate, officer)}</span>
					{#if officer}
						<span class="plate-who mono">
							{t(lang, 'cc_plates_shown')} {ops.dutyOfficer.trim()}{#if ops.dutyOfficerFromSession} · {t(lang, 'dossier_session_officer')}{/if}
						</span>
					{/if}
				</div>
				{#if !officer && !ops.dutyOfficerFromSession}
					<form
						class="officer"
						onsubmit={(e) => {
							e.preventDefault();
							setOfficer();
						}}
					>
						<input
							type="text"
							bind:value={officerDraft}
							placeholder={t(lang, 'cc_officer_prompt')}
							aria-label={t(lang, 'cc_officer_prompt')}
							data-testid="dossier-officer"
							maxlength="64"
						/>
						<button type="submit" disabled={officerDraft.trim().length < 2}>{t(lang, 'cc_officer_set')}</button>
					</form>
				{/if}
			</section>
		{/if}

		<section class="d-sec explain-sec" data-testid="dossier-explain">
			<div class="explain-row">
				{#if explain?.status === 'streaming'}
					<span class="explain-busy mono">{t(lang, 'dossier_explaining')}</span>
					<button type="button" class="explain-btn ghost" data-testid="dossier-explain-cancel" onclick={cancelExplain}>
						{t(lang, 'dossier_explain_cancel')}
					</button>
				{:else}
					<button type="button" class="explain-btn" data-testid="dossier-explain-btn" onclick={explainThis}>✦ {t(lang, 'dossier_explain')}</button>
				{/if}
				{#if explain && !officer}<span class="explain-mask">{t(lang, 'dossier_explain_masked')}</span>{/if}
			</div>
			{#if explain && (explain.text || explain.status !== 'streaming')}
				<p class="explain-text" data-testid="dossier-explain-answer" data-status={explain.status}>
					{explain.text ? displayText(explain.text, officer) : explain.status === 'error' ? t(lang, 'dossier_explain_failed') : '…'}
				</p>
				{#if explain.status === 'done' && explain.tiers.length}
					<p class="explain-tiers mono">{t(lang, 'dossier_explain_by')} · {explain.tiers.join(' · ')}</p>
				{/if}
				{#if explain.note}<p class="explain-tiers mono">{displayText(explain.note, officer)}</p>{/if}
			{/if}
		</section>

		{#if facts.length}
			<section class="d-sec">
				<h3>{t(lang, 'cc_facts')}</h3>
				<dl class="facts">
					{#each facts as f (f.k)}
						<dt>{f.k}</dt>
						<dd class:act={f.tone === 'act'} class:dim={f.tone === 'dim'} class:emu={f.tone === 'emu'}>{f.v}</dd>
					{/each}
				</dl>
			</section>
		{/if}

		{#if plan}
			<section class="d-sec pursuit-sec" data-testid="dossier-interception">
				<h3>{t(lang, 'cc_interception')}</h3>
				<p class="kv mono" class:stale={plan.stale}>
					{#if plan.stale}<b class="stale-tag">STALE</b>{/if}
					latest read {plan.sightingAgeSec != null ? `${fmtAge(plan.sightingAgeSec)} ago` : '—'}
					{plan.cameraId ? `at ${plan.cameraId}` : ''}
					{plan.stale ? '— the vehicle may be beyond every junction listed' : ''}
				</p>
				{#if plan.direction}
					<p class="kv mono">
						direction: {plan.direction.basis ?? '—'}
						{#if plan.direction.againstOneway}<b class="act">· AGAINST ONE-WAY (projected as observed)</b>{/if}
						{#if plan.direction.towardsJunctionIds.length}
							· towards {plan.direction.towardsJunctionIds.map(junctionName).join(', ')}
						{/if}
					</p>
					{#if plan.direction.onwardSegmentIds.length}
						<p class="kv mono dim">onward: {plan.direction.onwardSegmentIds.map(segmentName).join(' · ')}</p>
					{/if}
				{/if}
				<ol class="chokes">
					{#each plan.chokes.slice(0, 5) as c (c.junctionId)}
						<li class:passed={c.likelyPassed}>
							<button type="button" onclick={() => selection.select(`junction:${c.junctionId}`, { fly: true, source: 'dossier' })}>
								<span class="mono">#{c.rank}</span>
								{c.name ?? `J${c.junctionId}`} · {c.junctionType}
								<span class="mono">{c.graphCostSec != null ? `${Math.round(c.graphCostSec)}s` : ''}</span>
								{#if c.likelyPassed}<em>likely passed</em>{/if}
							</button>
						</li>
					{/each}
				</ol>
				{#if plan.note}<p class="kv dim">{plan.note}</p>{/if}
			</section>
		{/if}

		{#if groups.length}
			<section class="d-sec" data-testid="dossier-related">
				<h3>{t(lang, 'cc_related')} <span class="mono n">{data?.related.length ?? 0}</span></h3>
				{#each groups as [relation, list] (relation)}
					<div class="rel-group" data-relation={relation}>
						<span class="rel-name">{relation.replace(/_/g, ' ')} · {list.length}</span>
						<div class="chips">
							{#each list.slice(0, 24) as e (e.urn)}
								<button
									type="button"
									class="rel-chip"
									class:emu={e.source === 'emulated'}
									class:pending={e.kind === 'recommendation' && e.state === 'pending'}
									data-urn={e.urn}
									title={`${e.urn} · ${e.source.toUpperCase()}`}
									onclick={() => go(e)}
								>
									<b>{titleOf(e)}</b>
									<span class="mono">{chipDetail(e)}</span>
								</button>
							{/each}
						</div>
					</div>
				{/each}
			</section>
		{/if}

		{#if links.length}
			<section class="d-links">
				{#each links as l (l.href)}
					<a href={l.href} class="deep">{l.label} →</a>
				{/each}
			</section>
		{/if}

		<section class="d-sec" data-testid="dossier-audit">
			<h3>{t(lang, 'cc_audit')} <span class="mono n">{audit.length}</span></h3>
			{#if audit.length}
				<ol class="audit">
					{#each audit as r (r.id)}
						<li class="a-row {r.actorKind}" class:emu={r.source === 'emulated'} data-actor-kind={r.actorKind}>
							<span class="a-time mono">{istStamp(r.t, now)}</span>
							<span class="a-body">
								<span class="a-actor">{r.actor ?? r.actorKind}</span>
								{#if r.actorKind === 'machine' && machineDetail(r)}<span class="a-tier mono">{machineDetail(r)}</span>{/if}
								<span class="a-text">{rowText(r)}</span>
							</span>
							{#if r.source === 'emulated'}<span class="pill">{t(lang, 'cc_emulated')}</span>{/if}
						</li>
					{/each}
				</ol>
			{:else}
				<p class="kv dim">{dossierState === 'loading' ? '…' : '—'}</p>
			{/if}
		</section>

		{#if selection.pick}
			<section class="d-sec ge-pick map-pick" data-pick-layer={selection.pick.layer}>
				<h3>{t(lang, 'cc_map_pick')} · {selection.pick.layer}</h3>
				<p class="pick-text">{displayText(selection.pick.text, officer)}</p>
			</section>
		{/if}
	</div>
{/if}

<style>
	.dossier {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		min-height: 0;
	}

	.d-head {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.d-kicker {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.kind {
		font-size: var(--v-text-2xs);
		letter-spacing: 0.12em;
		color: var(--v-data);
	}

	.src {
		font-family: var(--v-font-mono);
		font-size: 9px;
		letter-spacing: 0.1em;
		padding: 1px 6px;
		border: 1px solid var(--v-band-green);
		color: var(--v-band-green);
		border-radius: var(--v-radius-pill);
	}

	.src.emu {
		border-style: dashed;
		border-color: var(--v-ink-2);
		color: var(--v-ink-2);
	}

	/* synthetic ingest feed: the tool accent of machine senses, dashed */
	.src.sim {
		border-style: dashed;
		border-color: var(--v-tool);
		color: var(--v-tool);
	}

	/* provenance not (yet) reported: neutral ink, never the live green */
	.src.unverified {
		border-style: dotted;
		border-color: var(--v-ink-3);
		color: var(--v-ink-3);
	}

	.state {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}

	.age {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.spacer {
		flex: 1;
	}

	.icon {
		width: var(--v-ctl-sm);
		height: var(--v-ctl-sm);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		background: var(--v-surface-1);
		color: var(--v-ink-2);
		cursor: pointer;
		font: inherit;
	}

	.icon:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
	}

	.d-title {
		margin: 0;
		font-family: var(--v-font-display);
		font-size: var(--v-text-md);
		font-weight: 700;
		color: var(--v-ink-1);
		line-height: 1.25;
		overflow-wrap: anywhere;
	}

	.d-note {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.d-sec {
		padding: var(--v-space-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		background: color-mix(in srgb, var(--v-surface-1) 70%, transparent);
	}

	h3 {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	h3 .n {
		color: var(--v-ink-1);
		letter-spacing: 0;
	}

	.plate-sec {
		border-color: var(--v-line-2);
	}

	.plate-row {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.plate {
		font-size: var(--v-text-lg);
		font-weight: 700;
		letter-spacing: 0.08em;
		color: var(--v-ink-1);
		padding: 2px var(--v-space-2);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		background: var(--v-surface-2);
	}

	.plate-who {
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
	}

	.officer {
		display: flex;
		gap: var(--v-space-1);
		margin-top: var(--v-space-2);
	}

	.officer input {
		flex: 1;
		min-width: 0;
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-1);
		font: inherit;
		font-size: var(--v-text-2xs);
		padding: var(--v-space-1) var(--v-space-2);
	}

	.officer input:focus-visible {
		outline: none;
		border-color: var(--v-act);
	}

	.officer button {
		border: 1px solid var(--v-act);
		border-radius: var(--v-radius-btn);
		background: transparent;
		color: var(--v-act-text);
		font: inherit;
		font-size: var(--v-text-2xs);
		padding: 0 var(--v-space-2);
		cursor: pointer;
	}

	.officer button:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.facts {
		display: grid;
		grid-template-columns: minmax(84px, auto) 1fr;
		gap: 2px var(--v-space-2);
		margin: 0;
		font-size: var(--v-text-2xs);
	}

	.facts dt {
		color: var(--v-ink-3);
		font-family: var(--v-font-mono);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.facts dd {
		margin: 0;
		color: var(--v-ink-1);
		overflow-wrap: anywhere;
	}

	.facts dd.act,
	.act {
		color: var(--v-act-text);
	}

	.facts dd.dim,
	.dim {
		color: var(--v-ink-3);
	}

	.facts dd.emu {
		color: var(--v-ink-2);
		font-style: italic;
	}

	.kv {
		margin: 2px 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		line-height: 1.45;
	}

	.kv.stale {
		color: var(--v-ink-3);
	}

	.stale-tag {
		color: var(--v-band-amber);
		letter-spacing: 0.1em;
		margin-right: var(--v-space-1);
	}

	.pursuit-sec {
		border-left: 3px solid var(--v-act);
	}

	.chokes {
		list-style: none;
		margin: var(--v-space-1) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.chokes button {
		display: flex;
		gap: var(--v-space-2);
		align-items: baseline;
		width: 100%;
		padding: 2px var(--v-space-1);
		background: transparent;
		border: 1px solid transparent;
		border-radius: var(--v-radius-chip);
		color: var(--v-ink-1);
		font: inherit;
		font-size: var(--v-text-2xs);
		text-align: left;
		cursor: pointer;
	}

	.chokes button:hover {
		border-color: var(--v-line-select);
	}

	.chokes li.passed button {
		color: var(--v-ink-3);
	}

	.chokes em {
		font-style: normal;
		color: var(--v-band-amber);
	}

	.rel-group + .rel-group {
		margin-top: var(--v-space-2);
	}

	.rel-name {
		display: block;
		font-family: var(--v-font-mono);
		font-size: 9px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--v-ink-3);
		margin-bottom: 2px;
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.rel-chip {
		display: inline-flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 1px;
		max-width: 100%;
		padding: 3px var(--v-space-2);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		color: var(--v-ink-1);
		font: inherit;
		font-size: var(--v-text-2xs);
		text-align: left;
		cursor: pointer;
	}

	.rel-chip:hover {
		border-color: var(--v-line-select);
	}

	.rel-chip.emu {
		border-style: dashed;
	}

	.rel-chip.pending {
		border-color: var(--v-act);
	}

	.rel-chip b {
		font-weight: 600;
		max-width: 300px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.rel-chip span {
		font-size: 9px;
		color: var(--v-ink-3);
	}

	.d-links {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-2);
	}

	.deep {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
		border: 1px solid var(--v-act);
		border-radius: var(--v-radius-btn);
		padding: 2px var(--v-space-2);
		text-decoration: none;
	}

	.deep:hover {
		background: var(--v-act-veil);
	}

	.audit {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.a-row {
		display: grid;
		grid-template-columns: auto 1fr auto;
		gap: var(--v-space-2);
		align-items: baseline;
		padding: 3px var(--v-space-1) 3px var(--v-space-2);
		border-left: 2px solid var(--v-line-2);
		font-size: var(--v-text-2xs);
	}

	/* human decisions ride the act-orange rail */
	.a-row.human {
		border-left-color: var(--v-act);
		background: var(--v-act-veil);
	}

	.a-row.machine {
		border-left-color: var(--v-data);
	}

	.a-row.system {
		border-left-color: var(--v-ink-4);
		opacity: 0.85;
	}

	.a-time {
		color: var(--v-ink-3);
		white-space: nowrap;
	}

	.a-body {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.a-actor {
		font-weight: 600;
		color: var(--v-ink-1);
	}

	.a-row.human .a-actor {
		color: var(--v-act-text);
	}

	.a-row.machine .a-actor,
	.a-row.system .a-actor {
		font-family: var(--v-font-mono);
		font-weight: 400;
		color: var(--v-ink-2);
	}

	.a-tier {
		font-size: 9px;
		color: var(--v-data);
	}

	.a-text {
		color: var(--v-ink-2);
		overflow-wrap: anywhere;
	}

	.pill {
		font-family: var(--v-font-mono);
		font-size: 9px;
		letter-spacing: 0.06em;
		padding: 0 5px;
		border: 1px dashed var(--v-ink-3);
		border-radius: var(--v-radius-pill);
		color: var(--v-ink-3);
		white-space: nowrap;
	}

	.map-pick {
		border-style: dashed;
	}

	/* a labelled test / rehearsal event — never a real movement */
	.src.test {
		border-style: solid;
		border-color: var(--v-band-amber);
		color: var(--v-band-amber);
		max-width: 220px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.explain-sec {
		border-color: color-mix(in srgb, var(--v-tool) 45%, transparent);
		border-left: 2px solid var(--v-tool);
	}

	.explain-row {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--v-space-2);
	}

	.explain-btn {
		border: 1px solid var(--v-tool);
		border-radius: var(--v-radius-btn);
		background: color-mix(in srgb, var(--v-tool) 10%, transparent);
		color: var(--v-tool);
		font: inherit;
		font-size: var(--v-text-2xs);
		padding: 2px var(--v-space-2);
		cursor: pointer;
	}

	.explain-btn.ghost {
		border-color: var(--v-line-2);
		background: transparent;
		color: var(--v-ink-2);
	}

	.explain-btn:hover {
		border-color: var(--v-line-select);
	}

	.explain-busy {
		font-size: var(--v-text-2xs);
		color: var(--v-tool);
	}

	.explain-mask {
		font-size: 9px;
		color: var(--v-ink-3);
	}

	.explain-text {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xs);
		line-height: 1.5;
		color: var(--v-ink-1);
		white-space: pre-wrap;
	}

	.explain-tiers {
		margin: var(--v-space-1) 0 0;
		font-size: 9px;
		color: var(--v-ink-3);
	}

	.pick-text {
		margin: 0;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		white-space: pre-line;
		line-height: 1.45;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
