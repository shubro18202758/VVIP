<script lang="ts">
	/**
	 * CityVitals — the left rail of VEER CITY COMMAND: the city in one column.
	 *
	 *   POSTURE     the highest band over ACTIVE events (registry); with no
	 *               active banded event, the CITY band the signal writer acts
	 *               on (GET /adaptive/status city_band). Either is aged from
	 *               its newest READING — the event's newest situation_log row
	 *               (band_t), the engine's last evaluation for the city band
	 *               (else this console's last read) — and is STALE only when
	 *               that reading is over 10 min old: a band HELD for hours on
	 *               a ticking engine is not stale. When it was entered (the
	 *               event's TRANSITION row in the city feed, the city band's
	 *               since) is its own 'held … since HH:MM IST' line. A stale
	 *               city band (engine poll failing) reads UNKNOWN with since
	 *               when, never GREEN. SIMULATED FEED when the band's inputs
	 *               are synthetic. The canvas AMA chip speaks for the
	 *               SELECTED event and says so in its scope.
	 *   EVENTS      OPEN corridor events by kind (planning · approved ·
	 *               active — the header says 'open', the rows count active)
	 *   DECISIONS   everything a named officer must decide, linked to /ops:
	 *               penal e-challans an officer CAN approve; evidence-only
	 *               stolen-vehicle sightings (R_STOLEN_SIGHTING — never a
	 *               fine) and penal rows withheld for a vehicle reported
	 *               stolen (reject only) on lines of their own; dispatch
	 *               requests (108 counted apart); and the escorts' open
	 *               proposals (officer approves every switch — the AI only
	 *               proposes a route switch, Plan B, a hold or closures)
	 *   UNITS       fielded · en route · on scene (all EMULATED)
	 *   ANPR        watchlist hits + reads in the last 15 min (counts only)
	 *   ETHICS      the retention reaper's health, lawful-basis counts, the
	 *               plate mask state — vehicles only · no faces ·
	 *               human-approved penalties (kept above the fold at 1080p)
	 *   SIGNALS     writer flag, twin gate in flight, last plan verdict,
	 *               programs at the phase clock
	 *   AI TIERS    decider (connected / loading / unreachable, p95, latch,
	 *               tier-0), Gemma, degradation
	 *   FEEDS       ingest msgs/min + last-message age (SIMULATED FEED when the
	 *               corridor summary stands on synthetic rows), city-picture
	 *               age, newest sighting, emulator run + living city, and the
	 *               road network's geometry basis (OSM-traced — 'osm' or
	 *               'osm_display' — vs schematic)
	 *
	 * Every number is a count of fused rows or a server health field; a rung
	 * that cannot answer says so in neutral ink, never as zero-and-green.
	 * Clicking a vital selects (and flies to) the entity behind it.
	 */
	import { onMount, onDestroy } from 'svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { isSignInRequired } from '$lib/ops/client';
	import { istDayTime, istTime } from './format';
	import { t, kindLabel } from '$lib/i18n/strings';
	import { corridor } from '$lib/ops/components/template/corridor.svelte';
	import { cityRegistry } from '../entities/registry.svelte';
	import { selection } from '../entities/selection.svelte';
	import { godsEye } from '../store.svelte';
	import { cityVitals } from './vitals.svelte';
	import { fusionFeed } from './feed.svelte';
	import { ageOf, fmtAge } from './format';
	import { isAmber } from '../layers/signal-network';
	import { unitPhase } from '../layers/agency';
	import type { BandCode, CityEntity } from '../city-api';

	const BAND_RANK: Record<BandCode, number> = { GREEN: 0, AMBER: 1, RED: 2, BLACK: 3 };
	/** A posture older than this renders STALE (the AMA chip's threshold). */
	const POSTURE_STALE_SEC = 600;

	let now = $state(Date.now());
	let clock: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		cityRegistry.start();
		cityVitals.start();
		fusionFeed.start();
		corridor.start();
		clock = setInterval(() => (now = Date.now()), 1000);
	});

	onDestroy(() => {
		if (clock) clearInterval(clock);
		corridor.stop();
		fusionFeed.stop();
		cityVitals.stop();
		cityRegistry.stop();
	});

	const lang = $derived(ops.lang);

	/** A feed note as the console prints it: the gate's refusal in the console's words. */
	function noteText(note: string | null | undefined): string | null {
		if (!note) return null;
		return isSignInRequired(note) ? t(lang, 'cc_signin_required') : note;
	}

	function epoch(v: unknown): number | null {
		if (typeof v !== 'string') return null;
		const t = Date.parse(v);
		return Number.isFinite(t) ? t : null;
	}

	// ─── posture ───────────────────────────────────────────────────────────────

	const posture = $derived.by(() => {
		const active = cityRegistry.byKind('event').filter((e) => e.state === 'active' && e.band);
		if (active.length) {
			const top = [...active].sort(
				(a, b) => BAND_RANK[b.band as BandCode] - BAND_RANK[a.band as BandCode] || (b.t ?? 0) - (a.t ?? 0),
			)[0];
			const transition = fusionFeed.rows.find(
				(r) =>
					r.kind === 'ama' &&
					r.id.startsWith('situation_log:') &&
					r.urns.includes(top.urn) &&
					r.props.situation_class === top.band,
			);
			// Aged from the subject's newest reading (band_t); its transition
			// row is when the band was entered — how long it has held.
			const readingAt = epoch(top.props.band_t);
			return {
				band: top.band as BandCode | null,
				urn: top.urn as string | null,
				label: top.label,
				events: active.length,
				at: readingAt ?? transition?.t ?? null,
				basis: readingAt != null ? ('reading' as const) : ('transition' as const),
				source: 'events' as const,
				simulated: top.props.simulated_feed === true,
				emulated: top.source === 'emulated',
				heldSince: transition?.t ?? null,
				engineStale: false,
				staleSince: null as number | null,
			};
		}
		// No active event carries a band: the city band (never empty while it answers).
		const cb = ops.cityBand;
		if (cb && (cb.band || cb.stale)) {
			return {
				band: cb.stale ? null : cb.band,
				urn: null,
				label: t(lang, 'cc_posture_city'),
				events: 0,
				// Aged from its NEWEST READING (the engine's last evaluation, else
				// this console's last read) — never from the moment it was entered:
				// that is how long it has HELD (heldSince), not how old it is.
				at: cb.stale ? null : (cb.evaluatedAt ?? (ops.cityBandUpdated || null)),
				basis: cb.stale ? ('none' as const) : ('reading' as const),
				source: 'city-band' as const,
				simulated: cb.simulatedFeed === true,
				emulated: false,
				heldSince: cb.stale ? null : cb.since,
				engineStale: cb.stale,
				staleSince: cb.stale ? cb.staleSince : null,
			};
		}
		if (ops.band && ops.bandSourceKind === 'city-band') {
			return {
				band: ops.band as BandCode | null,
				urn: null,
				label: t(lang, 'cc_posture_city'),
				events: 0,
				at: ops.bandUpdated || null,
				basis: ops.bandUpdated ? ('reading' as const) : ('none' as const),
				source: 'city-band' as const,
				simulated: ops.bandSimulated === true,
				emulated: false,
				heldSince: ops.bandCommittedAt,
				engineStale: false,
				staleSince: null as number | null,
			};
		}
		return null;
	});

	const postureAge = $derived(posture?.at != null ? ageOf(posture.at, now) : null);
	const postureStale = $derived(postureAge != null && postureAge > POSTURE_STALE_SEC);
	/** How long the band has held (its entry time) — a line of its own, never its age. */
	const postureHeld = $derived(posture?.heldSince != null ? ageOf(posture.heldSince, now) : null);

	/** HH:MM IST today, '22 Oct 21:40' on an older day. */
	function istMinute(ms: number): string {
		return now - ms < 20 * 3600_000 ? istTime(ms).slice(0, 5) : istDayTime(ms);
	}

	// ─── events / decisions / units / ANPR ─────────────────────────────────────

	const eventKinds = $derived.by(() => {
		const m = new Map<string, { kind: string; total: number; active: number; emulated: number; first: CityEntity }>();
		for (const e of cityRegistry.byKind('event')) {
			const kind = typeof e.props.event_kind === 'string' ? e.props.event_kind : 'unknown';
			const row = m.get(kind) ?? { kind, total: 0, active: 0, emulated: 0, first: e };
			row.total++;
			if (e.state === 'active') row.active++;
			if (e.source === 'emulated') row.emulated++;
			m.set(kind, row);
		}
		return [...m.values()].sort((a, b) => b.active - a.active || b.total - a.total);
	});

	const pendingRecs = $derived(cityRegistry.byKind('recommendation').filter((r) => r.state === 'pending'));
	/** Penal e-challans an officer can approve — never a stolen vehicle's (C-N). */
	const echallans = $derived(pendingRecs.filter((r) => r.props.rule_kind !== 'evidence' && r.props.approvable !== false));
	/** Evidence-only stolen-vehicle sightings (R_STOLEN_SIGHTING): a pursuit authorisation, no fine. */
	const stolenSightings = $derived(pendingRecs.filter((r) => r.props.rule_kind === 'evidence'));
	/** Penal rows withheld for a vehicle reported stolen: an officer can only reject them. */
	const withheld = $derived(pendingRecs.filter((r) => r.props.rule_kind !== 'evidence' && r.props.approvable === false));
	const requested = $derived(cityRegistry.byKind('dispatch').filter((d) => d.state === 'requested'));
	const ambulance = $derived(requested.filter((d) => d.props.agency === 'ambulance'));
	/** The escorts' open proposals (officer approves every switch). */
	const escortProposals = $derived(ops.escortProposals);
	const decisionsTotal = $derived(pendingRecs.length + requested.length + escortProposals.length);
	/** The road network as the canvas draws it: OSM-traced ('osm' / 'osm_display') vs schematic. */
	const roadsTraced = $derived(godsEye.segments.filter((s) => s.geometryBasis !== 'schematic').length);
	const roadsSchematic = $derived(godsEye.segments.length - roadsTraced);

	const units = $derived(godsEye.units);
	// One partition (agency.ts unitPhase): an arrived unit is on scene, never also en route.
	const unitsEnRoute = $derived(units.filter((u) => unitPhase(u) === 'en_route'));
	const unitsOnScene = $derived(units.filter((u) => unitPhase(u) === 'on_scene'));

	const cams = $derived(godsEye.cameras);
	const hits15 = $derived(cams.reduce((n, c) => n + c.hits15m, 0));
	const reads15 = $derived(cams.reduce((n, c) => n + c.reads15m, 0));
	const reporting = $derived(cams.filter((c) => c.reads15m > 0).length);
	const busiestCam = $derived(
		[...cams].sort((a, b) => b.hits15m * 1000 + b.reads15m - (a.hits15m * 1000 + a.reads15m))[0] ?? null,
	);

	// ─── signals ───────────────────────────────────────────────────────────────

	const plans = $derived(ops.signalPlans);
	const programs = $derived(godsEye.signals);
	const programsAmber = $derived(programs.filter((s) => isAmber(s)).length);
	const programsRed = $derived(programs.filter((s) => !isAmber(s) && s.phaseLost === true).length);

	// ─── feeds ─────────────────────────────────────────────────────────────────

	const ingest = $derived(cityVitals.ingest);
	const brain = $derived(cityVitals.brain);
	const retention = $derived(cityVitals.retention);
	const latestRun = $derived(cityVitals.runs.value?.[0] ?? null);
	const living = $derived(cityVitals.livingCity);
	const registryAge = $derived(cityRegistry.updatedAt ? ageOf(cityRegistry.updatedAt, now) : null);
	const sightingsT = $derived(cityRegistry.feed('sighting')?.newestT ?? cityRegistry.feed('vehicle')?.newestT ?? null);

	// ─── ethics ────────────────────────────────────────────────────────────────

	const lawful = $derived.by(() => {
		const m = new Map<string, number>();
		for (const v of cityRegistry.byKind('vehicle')) {
			const basis = typeof v.props.lawful_basis === 'string' ? v.props.lawful_basis : 'none';
			m.set(basis, (m.get(basis) ?? 0) + 1);
		}
		return [...m.entries()].sort((a, b) => b[1] - a[1]);
	});

	function go(urn: string | null | undefined) {
		if (urn) selection.select(urn, { fly: true, source: 'vitals' });
	}

	// What this rail shows, published for the canvas test snapshot (window.__GODS_EYE__.vitals).
	$effect(() => {
		const kinds: Record<string, number> = {};
		let activeEvents = 0;
		let emulatedEvents = 0;
		for (const k of eventKinds) {
			kinds[k.kind] = k.total;
			activeEvents += k.active;
			emulatedEvents += k.emulated;
		}
		// built apart so the snapshot's additive keys (stolen sightings,
		// withheld rows, escort proposals, the roads' basis) ride along
		const decisionsShown = {
			total: decisionsTotal,
			echallans: echallans.length,
			stolenSightings: stolenSightings.length,
			withheld: withheld.length,
			escortProposals: escortProposals.length,
			dispatchRequests: requested.length,
			ambulance: ambulance.length,
		};
		const feedsShown = {
			ingestPerMin: ingest.value?.msgsPerMin ?? null,
			ingestAgeSec: ingest.value?.lastMsgAgeSec ?? null,
			simulated: corridor.simulated,
			registryEntities: cityRegistry.size,
			registryAgeSec: registryAge != null ? Math.round(registryAge) : null,
			sightingAgeSec: sightingsT != null ? Math.round(ageOf(sightingsT, now) ?? 0) : null,
			emulatorRun: latestRun?.runId ?? null,
			livingCity: living.value ? (living.value.status ?? (living.value.enabled ? 'on' : 'off')) : living.state,
			roads: { traced: roadsTraced, schematic: roadsSchematic },
		};
		cityVitals.shown = {
			posture: posture
				? {
						band: posture.band,
						ageSec: postureAge != null ? Math.round(postureAge) : null,
						heldSec: postureHeld != null ? Math.round(postureHeld) : null,
						basis: posture.basis,
						stale: postureStale || posture.engineStale,
						source: posture.source,
						urn: posture.urn,
						simulated: posture.simulated,
					}
				: null,
			events: { total: cityRegistry.byKind('event').length, active: activeEvents, emulated: emulatedEvents, kinds },
			decisions: decisionsShown,
			units: { out: units.length, enRoute: unitsEnRoute.length, onScene: unitsOnScene.length },
			anpr: { hits15m: hits15, reads15m: reads15, reporting, cameras: cams.length },
			signals: {
				writerOn: plans ? plans.writerEnabled : null,
				twinInFlight: plans ? (plans.gate.twinInFlight ?? 0) : null,
				programs: programs.length,
				green: programs.length - programsAmber - programsRed,
				amber: programsAmber,
				red: programsRed,
			},
			feeds: feedsShown,
			ai: brain.value
				? {
						decider: brain.value.decider,
						p95Ms: brain.value.p95Ms,
						gemma: brain.value.gemma,
						degraded: brain.value.deciderDegraded,
					}
				: null,
			ethics: {
				reaper: retention.value ? (retention.value.healthy === false ? 'failing' : 'healthy') : retention.state,
				consecutiveFailures: retention.value?.consecutiveFailures ?? null,
				lawfulBasis: Object.fromEntries(lawful),
				platesMasked: !ops.officerOn,
			},
		};
	});

	onDestroy(() => {
		cityVitals.shown = null;
	});
</script>

<div class="vitals" data-testid="city-vitals">
	<header class="v-head">
		<h2>{t(lang, 'cc_vitals')}</h2>
		<span class="v-reg mono" class:warn={cityRegistry.status !== 'ok'} title={noteText(cityRegistry.note) ?? undefined}>
			{cityRegistry.size} · {registryAge != null ? fmtAge(registryAge) : '—'}
		</span>
	</header>

	<!-- POSTURE -->
	<section class="v-sec posture-sec" data-testid="vitals-posture">
		<h3>{t(lang, 'cc_posture')}</h3>
		{#if posture}
			<button
				type="button"
				class="posture"
				class:stale={postureStale || posture.engineStale}
				disabled={!posture.urn}
				onclick={() => go(posture.urn)}
				data-source={posture.source}
				data-basis={posture.basis}
				title={posture.source === 'city-band'
					? t(lang, 'cc_posture_city_hint')
					: `highest band over ${posture.events} active event(s)`}
			>
				{#if posture.band}
					<BandChip band={posture.band} />
				{:else}
					<span class="unknown mono" data-testid="vitals-band-unknown">{t(lang, 'cc_band_unknown')}</span>
				{/if}
				<span class="p-age mono" data-age-sec={postureAge != null ? Math.round(postureAge) : ''}>
					{#if posture.engineStale}
						{posture.staleSince != null
							? t(lang, 'cc_band_stale_since', `${istMinute(posture.staleSince)} IST`)
							: t(lang, 'cc_band_stale')}
					{:else if posture.basis === 'none'}
						{t(lang, 'cc_posture_city')}
					{:else}
						{fmtAge(postureAge)} {t(lang, posture.basis === 'transition' ? 'cc_since_transition' : 'cc_reading_age')}
					{/if}
				</span>
				{#if postureStale}<span class="stale-tag" data-testid="vitals-posture-stale">{t(lang, 'cc_stale')}</span>{/if}
				{#if posture.emulated}<span class="pill emu" data-testid="vitals-posture-emulated">{t(lang, 'cc_emulated')}</span>{/if}
				{#if posture.simulated}<span class="pill sim" data-testid="vitals-posture-simulated">{t(lang, 'cc_simulated_feed')}</span>{/if}
			</button>
			{#if posture.heldSince != null && postureHeld != null}
				<p class="p-held mono" data-testid="vitals-posture-held" data-held-sec={Math.round(postureHeld)}>
					{t(lang, 'cc_band_held_since', fmtAge(postureHeld), `${istMinute(posture.heldSince)} IST`)}
				</p>
			{/if}
			<p class="p-sub" class:wrap={posture.source !== 'events'}>
				{#if posture.source === 'events'}
					{posture.label}{posture.events > 1 ? ` · +${posture.events - 1}` : ''}
				{:else}
					{t(lang, 'cc_posture_writer')}
				{/if}
			</p>
		{:else}
			<p class="muted">{t(lang, 'cc_posture_none')}</p>
		{/if}
	</section>

	<!-- EVENTS BY KIND -->
	<section class="v-sec">
		<h3 title={t(lang, 'cc_open_events_hint')}>
			{t(lang, 'cc_events_by_kind')} <span class="count mono">{cityRegistry.byKind('event').length}</span>
		</h3>
		{#if eventKinds.length}
			<ul class="rows">
				{#each eventKinds as k (k.kind)}
					<li>
						<button type="button" class="row-btn" onclick={() => go(k.first.urn)}>
							<span class="r-name">{kindLabel(lang, k.kind)}</span>
							<span class="r-val mono">
								{k.total}{#if k.active}<em class="act">{` · ${k.active} ${t(lang, 'cc_active')}`}</em>{/if}
							</span>
							{#if k.emulated}<span class="pill emu">{t(lang, 'cc_emulated')}</span>{/if}
						</button>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="muted">{t(lang, 'cc_no_events')}</p>
		{/if}
	</section>

	<!-- PENDING HUMAN DECISIONS -->
	<section class="v-sec decisions" class:hot={decisionsTotal > 0} data-testid="vitals-decisions">
		<h3>{t(lang, 'cc_pending')} <span class="count mono">{decisionsTotal}</span></h3>
		<ul class="rows">
			{#if escortProposals.length}
				<li data-testid="vitals-escort-proposals">
					<a class="row-btn" href="/" title={t(lang, 'cc_escort_proposals_hint')}>
						<span class="r-name">{t(lang, 'cc_escort_proposals')}</span>
						<span class="r-val mono act">{escortProposals.length}</span>
					</a>
					<a class="deep" href="/ops">{t(lang, 'cc_decide')} →</a>
				</li>
			{/if}
			<li data-testid="vitals-echallans" data-count={echallans.length}>
				<button type="button" class="row-btn" onclick={() => go(echallans[0]?.urn)} disabled={!echallans.length} title={t(lang, 'cc_echallan_hint')}>
					<span class="r-name">{t(lang, 'cc_echallan')}</span>
					<span class="r-val mono" class:act={echallans.length > 0}>{echallans.length}</span>
				</button>
				{#if echallans.length}<a class="deep" href={`/ops#rec=${echallans[0].id}`}>{t(lang, 'cc_decide')} →</a>{/if}
			</li>
			{#if stolenSightings.length}
				<li data-testid="vitals-stolen-sightings">
					<button type="button" class="row-btn" onclick={() => go(stolenSightings[0]?.urn)} title={t(lang, 'cc_stolen_sightings_hint')}>
						<span class="r-name">{t(lang, 'cc_stolen_sightings')}</span>
						<span class="r-val mono act">{stolenSightings.length}</span>
					</button>
					<a class="deep" href={`/ops#rec=${stolenSightings[0].id}`}>{t(lang, 'cc_decide')} →</a>
				</li>
			{/if}
			{#if withheld.length}
				<li data-testid="vitals-withheld">
					<button type="button" class="row-btn" onclick={() => go(withheld[0]?.urn)} title={t(lang, 'cc_withheld_hint')}>
						<span class="r-name">{t(lang, 'cc_withheld')}</span>
						<span class="r-val mono">{withheld.length}</span>
					</button>
				</li>
			{/if}
			<li>
				<button type="button" class="row-btn" onclick={() => go(requested[0]?.urn)} disabled={!requested.length}>
					<span class="r-name">{t(lang, 'cc_dispatch_req')}</span>
					<span class="r-val mono" class:act={requested.length > 0}>{requested.length}</span>
				</button>
				{#if requested.length}<a class="deep" href={`/ops#dsp=${requested[0].id}`}>{t(lang, 'cc_decide')} →</a>{/if}
			</li>
			<li>
				<button type="button" class="row-btn" onclick={() => go(ambulance[0]?.urn)} disabled={!ambulance.length}>
					<span class="r-name">{t(lang, 'cc_ambulance_req')}</span>
					<span class="r-val mono" class:act={ambulance.length > 0}>{ambulance.length}</span>
				</button>
			</li>
		</ul>
	</section>

	<!-- UNITS + ANPR -->
	<section class="v-sec two">
		<div>
			<h3>{t(lang, 'cc_units')}</h3>
			<button type="button" class="big" onclick={() => go(unitsEnRoute[0] ? `unit:${unitsEnRoute[0].unitId}` : units[0] ? `unit:${units[0].unitId}` : null)} disabled={!units.length}>
				<b class="mono">{units.length}</b>
				<span>{t(lang, 'cc_fielded')}</span>
			</button>
			<p class="sub mono">{unitsEnRoute.length} {t(lang, 'cc_en_route')} · {unitsOnScene.length} {t(lang, 'cc_on_scene')}</p>
			{#if units.length}<span class="pill emu">{t(lang, 'cc_emulated')}</span>{/if}
		</div>
		<div>
			<h3>{t(lang, 'cc_anpr')}</h3>
			<button type="button" class="big" onclick={() => go(busiestCam ? `camera:${busiestCam.cameraId}` : null)} disabled={!busiestCam}>
				<b class="mono" class:act={hits15 > 0}>{hits15}</b>
				<span>{t(lang, 'cc_hits')}</span>
			</button>
			<p class="sub mono">{reads15} {t(lang, 'cc_reads')} · {reporting}/{cams.length} {t(lang, 'cc_cameras_reporting')}</p>
		</div>
	</section>

	<!-- ETHICS RAILS -->
	<section class="v-sec ethics" data-testid="vitals-ethics">
		<h3>{t(lang, 'cc_ethics')}</h3>
		<p class="ethics-line">{t(lang, 'cc_ethics_line')}</p>
		<p class="kv mono">
			{t(lang, 'cc_reaper')}:
			{#if retention.value}
				<span class:ok={retention.value.healthy === true} class:warn={retention.value.healthy === false}>
					{retention.value.healthy === false ? t(lang, 'cc_failing') : t(lang, 'cc_healthy')}
				</span>
				· {retention.value.consecutiveFailures ?? 0} {t(lang, 'cc_failures')} · {retention.value.defaultRetentionDays ?? '—'} d / cap {retention.value.earmarkCapDays ?? '—'} d
			{:else}
				<em class="dim">{retention.state === 'idle' ? '…' : (noteText(retention.note) ?? retention.state)}</em>
			{/if}
		</p>
		{#if retention.value?.lastReap}
			{@const reap = retention.value.lastReap}
			<p class="kv mono dim" data-testid="vitals-last-reap">
				{t(lang, 'cc_last_reap')}:
				{#if reap.ran}
					{t(lang, 'cc_reap_removed')} {reap.removed ?? 0}
				{:else if reap.failed}
					<span class="warn">{t(lang, 'cc_failing')}</span>{reap.reason ? ` — ${reap.reason}` : ''}
				{:else}
					{t(lang, 'cc_reap_skipped')}{reap.reason ? ` (${reap.reason})` : ''}
				{/if}
				{#if retention.value.lastSuccessAt}· {fmtAge(ageOf(retention.value.lastSuccessAt, now))} {t(lang, 'cc_ago')}{/if}
			</p>
		{/if}
		{#if lawful.length}
			<p class="kv mono">
				{t(lang, 'cc_lawful')}: {lawful.map(([basis, n]) => `${basis.replace(/_/g, ' ')} ×${n}`).join(' · ')}
			</p>
		{/if}
		<p class="kv mono" class:act={ops.officerOn}>
			{ops.officerOn ? `${t(lang, 'cc_plates_shown')} ${ops.dutyOfficer.trim()}` : t(lang, 'cc_plates_masked')}
		</p>
	</section>

	<!-- SIGNALS -->
	<section class="v-sec">
		<h3>{t(lang, 'cc_signals')}</h3>
		{#if plans}
			<p class="kv mono">
				<span class:act={plans.writerEnabled}>{t(lang, plans.writerEnabled ? 'cc_writer_on' : 'cc_writer_off')}</span>
				· {t(lang, 'cc_twin_gate')} {plans.gate.twinInFlight ?? 0} {t(lang, 'cc_in_flight')}
			</p>
			{#if plans.lastPlan}
				<p class="kv mono dim">
					{t(lang, 'cc_last_plan')}: {plans.lastPlan.applied ? `applied ${plans.lastPlan.band ?? ''}` : `held — ${plans.lastPlan.reason ?? 'fail-closed'}`}
				</p>
			{/if}
		{:else}
			<p class="muted mono">{noteText(ops.signalPlansNote) ?? t(lang, 'cc_signals_pending')}</p>
		{/if}
		<p class="kv mono">
			{programs.length} {t(lang, 'cc_programs')} ·
			<span class="sg">{programs.length - programsAmber - programsRed}</span> /
			<span class="sa">{programsAmber}</span> /
			<span class="sr">{programsRed}</span>
		</p>
	</section>

	<!-- AI TIERS -->
	<section class="v-sec" data-testid="vitals-ai">
		<h3>{t(lang, 'cc_ai')}</h3>
		{#if brain.value}
			<p class="kv mono">
				<i class="dot" class:ok={brain.value.decider === 'connected' && !brain.value.deciderDegraded} class:amber={brain.value.decider === 'loading' || brain.value.deciderDegraded} class:down={brain.value.decider === 'unreachable'}></i>
				{t(lang, 'cc_decider')}
				{t(lang, brain.value.decider === 'connected' ? 'cc_connected' : brain.value.decider === 'loading' ? 'cc_loading' : 'cc_unreachable')}
				{#if brain.value.p95Ms != null}· p95 {Math.round(brain.value.p95Ms)} ms{/if}
			</p>
			<p class="kv mono">
				<i class="dot" class:ok={brain.value.gemma === 'connected'} class:down={brain.value.gemma !== 'connected'}></i>
				{t(lang, 'cc_gemma')} {t(lang, brain.value.gemma === 'connected' ? 'cc_connected' : 'cc_unreachable')}
			</p>
			<p class="kv mono dim" title={`novelty_latch: ${brain.value.noveltyLatch ?? '—'} · tier0_cache: ${brain.value.tier0Cache ?? '—'}`}>
				{brain.value.deciderDegraded ? `${t(lang, 'cc_degraded')}: ${brain.value.deciderDegradedReason ?? '—'}` : t(lang, 'cc_nominal')}
				· {t(lang, 'cc_novelty_check')} {t(lang, `cc_onoff_${brain.value.noveltyLatch === 'armed' ? 'on' : brain.value.noveltyLatch ? 'off' : 'unknown'}`)}
				· {t(lang, 'cc_routine_cache')} {t(lang, `cc_onoff_${brain.value.tier0Cache === 'enabled' ? 'on' : brain.value.tier0Cache ? 'off' : 'unknown'}`)}
			</p>
		{:else}
			<p class="muted mono">{brain.state === 'idle' ? '…' : `${t(lang, 'cc_brain')} ${noteText(brain.note) ?? brain.state}`}</p>
		{/if}
	</section>

	<!-- FEEDS -->
	<section class="v-sec" data-testid="vitals-feeds">
		<h3>{t(lang, 'cc_feeds')}</h3>
		<dl class="feeds mono">
			<dt>{t(lang, 'cc_ingest')}</dt>
			<dd>
				{#if ingest.value}
					{ingest.value.msgsPerMin ?? 0}{t(lang, 'cc_per_min')} · {fmtAge(ingest.value.lastMsgAgeSec)}
					{#if !ingest.value.connected}<em class="warn">· {t(lang, 'cc_nats_down')}</em>{/if}
				{:else}
					<em class="dim">{ingest.state === 'idle' ? '…' : (noteText(ingest.note) ?? ingest.state)}</em>
				{/if}
				{#if corridor.simulated}<span class="pill sim">{t(lang, 'cc_simulated_feed')}</span>{/if}
			</dd>
			<dt>{t(lang, 'cc_registry')}</dt>
			<dd>
				{cityRegistry.size} · {registryAge != null ? fmtAge(registryAge) : '—'}{cityRegistry.tookMs != null ? ` · ${Math.round(cityRegistry.tookMs)} ms` : ''}
			</dd>
			<dt>{t(lang, 'cc_roads')}</dt>
			<dd data-testid="vitals-roads" data-traced={roadsTraced} data-schematic={roadsSchematic}>
				{#if godsEye.segments.length}
					{roadsTraced} {t(lang, 'cc_osm_traced')}{#if roadsSchematic} · {roadsSchematic} {t(lang, 'cc_schematic')}{/if}
				{:else}
					<em class="dim">…</em>
				{/if}
			</dd>
			<dt>{t(lang, 'cc_sightings')}</dt>
			<dd>{sightingsT != null ? `${fmtAge(ageOf(sightingsT, now))} ${t(lang, 'cc_ago')}` : '—'}</dd>
			<dt>{t(lang, 'cc_emulator')}</dt>
			<dd>
				{#if latestRun}
					run {latestRun.runId} · {latestRun.useCaseId ?? '—'} · {latestRun.status ?? '—'}
				{:else}
					<em class="dim">{cityVitals.runs.state === 'idle' ? '…' : (noteText(cityVitals.runs.note) ?? t(lang, 'cc_no_runs'))}</em>
				{/if}
			</dd>
			<dt>{t(lang, 'cc_living_city')}</dt>
			<dd>
				{#if living.value}
					{living.value.status ?? (living.value.enabled ? 'on' : 'off')} · {living.value.activeSlots} {t(lang, 'cc_slots')}
				{:else}
					<em class="dim">{living.state === 'not-deployed' ? `${t(lang, 'cc_not_deployed')} (C-G)` : living.state === 'idle' ? '…' : (noteText(living.note) ?? living.state)}</em>
				{/if}
			</dd>
		</dl>
	</section>
</div>

<style>
	.vitals {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		min-height: 0;
	}

	.v-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	h2 {
		margin: 0;
		font-family: var(--v-font-display);
		font-size: var(--v-text-sm);
		font-weight: 700;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--v-ink-1);
	}

	h3 {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
	}

	.count {
		color: var(--v-ink-1);
		font-size: var(--v-text-xs);
		letter-spacing: 0;
	}

	.v-reg {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.v-reg.warn {
		color: var(--v-band-amber);
	}

	.v-sec {
		padding: var(--v-space-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		background: color-mix(in srgb, var(--v-surface-1) 70%, transparent);
	}

	.v-sec.two {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--v-space-2);
	}

	.decisions.hot {
		border-left: 3px solid var(--v-act);
	}

	/* wraps: the age, STALE and the provenance pills never clip at the rail's width */
	.posture {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--v-space-1) var(--v-space-2);
		width: 100%;
		padding: var(--v-space-1) 0;
		background: transparent;
		border: none;
		color: var(--v-ink-1);
		cursor: pointer;
		font: inherit;
		text-align: left;
	}

	.posture:disabled {
		cursor: default;
	}

	.posture :global([data-prim='band-chip']) {
		font-size: var(--v-text-sm);
		padding: var(--v-space-1) var(--v-space-3);
	}

	.posture.stale :global([data-prim='band-chip']) {
		border-style: dashed;
		opacity: 0.6;
	}

	.p-age {
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	/* a city band whose engine poll fails: UNKNOWN, dashed — never a colour */
	.unknown {
		font-size: var(--v-text-xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-2);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
	}

	.stale-tag {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.1em;
		color: var(--v-band-amber);
	}

	/* how long the band has held — its entry time, never its age */
	.p-held {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.p-sub {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* the city band's sentence wraps — an event title is what gets cut */
	.p-sub.wrap {
		white-space: normal;
	}

	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.rows li {
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
	}

	a.row-btn {
		text-decoration: none;
	}

	.row-btn {
		flex: 1;
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		min-width: 0;
		padding: 2px var(--v-space-1);
		background: transparent;
		border: 1px solid transparent;
		border-radius: var(--v-radius-chip);
		color: var(--v-ink-2);
		font: inherit;
		font-size: var(--v-text-xs);
		cursor: pointer;
		text-align: left;
	}

	.row-btn:hover:not(:disabled) {
		border-color: var(--v-line-select);
		color: var(--v-ink-1);
	}

	.row-btn:disabled {
		cursor: default;
		opacity: 0.7;
	}

	.r-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.r-val {
		color: var(--v-ink-1);
		font-variant-numeric: tabular-nums;
	}

	.r-val em {
		font-style: normal;
	}

	.act {
		color: var(--v-act-text);
	}

	.deep {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
		text-decoration: none;
		white-space: nowrap;
	}

	.deep:hover {
		text-decoration: underline;
	}

	.big {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-1);
		background: transparent;
		border: none;
		padding: 0;
		color: var(--v-ink-2);
		font: inherit;
		font-size: var(--v-text-2xs);
		cursor: pointer;
	}

	.big:disabled {
		cursor: default;
	}

	.big b {
		font-size: var(--v-text-lg);
		font-weight: 700;
		color: var(--v-ink-1);
		font-variant-numeric: tabular-nums;
	}

	.big b.act {
		color: var(--v-act-text);
	}

	.sub {
		margin: 2px 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.kv {
		margin: 2px 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		line-height: 1.45;
	}

	.kv.dim,
	.dim {
		color: var(--v-ink-3);
	}

	.ok {
		color: var(--v-band-green);
	}

	.warn {
		color: var(--v-band-amber);
		font-style: normal;
	}

	.sg {
		color: var(--v-band-green);
	}

	.sa {
		color: var(--v-band-amber);
	}

	.sr {
		color: var(--v-band-red);
	}

	.feeds {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 2px var(--v-space-2);
		margin: 0;
		font-size: var(--v-text-2xs);
	}

	.feeds dt {
		color: var(--v-ink-3);
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}

	.feeds dd {
		margin: 0;
		color: var(--v-ink-2);
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.feeds em {
		font-style: normal;
	}

	.pill {
		display: inline-flex;
		align-items: center;
		height: 15px;
		padding: 0 5px;
		margin-left: var(--v-space-1);
		border: 1px dashed var(--v-ink-2);
		border-radius: var(--v-radius-pill);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.08em;
		color: var(--v-ink-2);
		white-space: nowrap;
		vertical-align: middle;
	}

	.pill.sim {
		border-color: var(--v-tool);
		color: var(--v-tool);
	}

	.dot {
		display: inline-block;
		width: 7px;
		height: 7px;
		border-radius: var(--v-radius-pill);
		background: var(--v-ink-4);
		margin-right: 2px;
	}

	.dot.ok {
		background: var(--v-band-green);
	}

	.dot.amber {
		background: var(--v-band-amber);
	}

	.dot.down {
		background: var(--v-band-red);
	}

	.ethics {
		border-color: var(--v-line-2);
	}

	.ethics-line {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: 0.04em;
		color: var(--v-ink-1);
	}

	.muted {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
