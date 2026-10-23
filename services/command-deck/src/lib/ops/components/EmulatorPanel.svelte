<script lang="ts">
	/**
	 * EmulatorPanel — the emulator control plane (contract C-G) through the
	 * deck's same-origin /api/emulator proxy. Every cause it injects is
	 * EMULATED and labelled as such; the decision stack plans, classifies and
	 * shadow-evaluates on its own.
	 *
	 *   · the library is the EMULATOR's own (GET /use-cases — all ten tapes,
	 *     incl. the rath_yatra_monsoon_ambulance and watchlist_anpr_sweep
	 *     flagships), not the deck's fixture list;
	 *   · LAUNCH is async: C-G answers 202 {run_id} at once and the run's
	 *     progress (frame/total, delivered/failed) is followed every 2 s; an
	 *     older build that holds the POST until the tape ends is never awaited
	 *     — its run is found in the ledger and followed the same way;
	 *   · flagship tapes are capped at ×10, and the panel states the engine's
	 *     effective sampling at the chosen clock (5 s wall tick × clock, e.g.
	 *     ×60 → 1 decision / 5 sim-min);
	 *   · STOP (C-G; completes the run's corridor event via C-F) and REPLAY
	 *     on each run row; LIVING CITY status + start/stop when the build
	 *     serves it (its /health says so — the route is never probed blind);
	 *   · a run whose corridor event is still 'planning' mid-run says so
	 *     ('corridor event not live yet — activation retried every 5 s'); a
	 *     failed or interrupted run shows the emulator's own reason; the
	 *     living city lists each slot's event status and its counters;
	 *   · a stolen-vehicle sweep's scenario premise (C-N,
	 *     spec.scenario_precondition) shows on its run, labelled EMULATED:
	 *     the FIR-named plate's stolen-FIR earmark created or already active
	 *     (its reads file evidence-only sightings), or failed — that plate's
	 *     camera reads were skipped (the plate masked without an officer).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { EMULATOR_CLOCKS, EMULATOR_HERO_CLOCK_CAP, POLL_MS } from '$lib/ops/endpoints';
	import { fmtAgeSince, istClock, istHm, samplingOf } from '$lib/ops/format';
	import { t } from '$lib/i18n/strings';
	import { tOps } from '$lib/i18n/strings.ops';
	import type { EmulatorRunPremise, EmulatorRunRow } from '$lib/ops/types';

	const lang = $derived(ops.lang);

	let selectedUseCase = $state('rath_yatra_monsoon_ambulance');
	let selectedClock = $state(10);
	let replayClock = $state(10);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	const library = $derived(ops.emulatorLibrary);
	const info = $derived(ops.useCaseInfo(selectedUseCase));
	const hero = $derived(info?.hero === true);
	const effectiveClock = $derived(ops.launchClock(selectedUseCase, selectedClock));
	const sampling = $derived(samplingOf(effectiveClock));
	const inLibrary = $derived(library.some((u) => u.id === selectedUseCase));
	const launch = $derived(ops.emulatorLaunch);

	// The library answered and the chosen tape is not in it: pick its first tape.
	$effect(() => {
		if (library.length && !library.some((u) => u.id === selectedUseCase)) selectedUseCase = library[0].id;
	});

	// A hero tape never offers more than ×10.
	$effect(() => {
		if (hero && selectedClock > EMULATOR_HERO_CLOCK_CAP) selectedClock = EMULATOR_HERO_CLOCK_CAP;
	});

	function phaseVariant(phase: string): 'live' | 'pending' | 'neutral' | 'rejected' | 'approved' {
		if (phase === 'running' || phase === 'posting') return 'pending';
		if (phase === 'completed') return 'approved';
		if (phase === 'failed' || phase === 'refused' || phase === 'interrupted') return 'rejected';
		return 'neutral';
	}

	function progressPct(r: EmulatorRunRow): number | null {
		const p = r.progress;
		if (!p || !p.total) return null;
		return Math.max(0, Math.min(100, Math.round((p.frame / p.total) * 100)));
	}

	const trackedRun = $derived(launch?.runId != null ? (ops.emulatorRuns.find((r) => r.runId === launch.runId) ?? null) : null);
	const lc = $derived(ops.livingCity);

	/**
	 * One scenario-precondition entry (C-N, spec.scenario_precondition) as this
	 * console may print it: the FIR-named plate masked without an officer, a
	 * failure's reason verbatim (plates masked too).
	 */
	function premiseText(p: EmulatorRunPremise): string {
		const plate = ops.plate(p.plate) ?? '—';
		const text =
			p.result === 'delivered'
				? tOps(lang, p.earmark === 'created' ? 'emuPremise_created' : 'emuPremise_already_active', plate, p.firRef)
				: p.result === 'failed'
					? tOps(lang, 'emuPremise_failed', plate, p.reason ? ops.text(p.reason) : null)
					: tOps(lang, 'emuPremise_skipped', plate);
		return `${tOps(lang, 'emuPremiseTitle')}: ${text}`;
	}
</script>

{#snippet headRight()}
	<Badge variant={ops.emulator.online ? 'live' : 'offline'} label={ops.emulator.online ? t(lang, 'emuOnline') : t(lang, 'emuOffline')} />
	<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />
{/snippet}

<Panel
	title={t(lang, 'emuTitle')}
	sub={tOps(lang, 'emuSubProxy')}
	status={ops.emulator.lastChecked ? { state: ops.emulator.online ? 'live' : 'offline', updatedAt: ops.emulator.lastChecked } : null}
	pollMs={POLL_MS.emulator}
	lang={lang}
	{...{ 'header-right': headRight }}
>
	<div class="row health">
		<Button variant="ghost" size="sm" onclick={() => void ops.pollEmulator()}>{t(lang, 'emuCheck')}</Button>
		<span class="detail mono">
			{#if ops.emulator.lastChecked}
				{ops.emulator.online ? (ops.emulator.healthDetail ?? 'GET /health ok') : (ops.emulator.healthDetail ?? 'connection refused').slice(0, 80)}
				{#if ops.emulator.stack}
					· {Object.entries(ops.emulator.stack).map(([k, v]) => `${k} ${v ? '✓' : '✗'}`).join(' ')}
				{/if}
			{:else}
				probing…
			{/if}
		</span>
		<span class="detail mono lib" data-testid="emu-library-count" data-count={library.length}>{tOps(lang, 'emuLibrary', library.length)}</span>
	</div>

	<div class="launch" data-testid="emu-launch">
		<label class="field grow">
			<span>{tOps(lang, 'emuUseCase')}</span>
			<select bind:value={selectedUseCase} data-testid="emu-use-case">
				{#if library.length}
					{#each library as u (u.id)}
						<option value={u.id}>{u.hero ? '★ ' : ''}{u.name}{u.category ? ` · ${u.category}` : ''}</option>
					{/each}
				{:else}
					<option value={selectedUseCase}>{selectedUseCase} (library not loaded)</option>
				{/if}
			</select>
		</label>
		<label class="field">
			<span>{tOps(lang, 'emuClock')}</span>
			<select bind:value={selectedClock} data-testid="emu-clock">
				{#each EMULATOR_CLOCKS as c (c)}
					<option value={c} disabled={hero && c > EMULATOR_HERO_CLOCK_CAP}>×{c}{hero && c > EMULATOR_HERO_CLOCK_CAP ? ` (${tOps(lang, 'emuHeroCapShort')})` : ''}</option>
				{/each}
			</select>
		</label>
		<Button
			variant="secondary"
			size="sm"
			busy={ops.emulatorBusy === 'launch'}
			disabled={!ops.emulator.online || !inLibrary || ops.emulatorBusy !== null}
			onclick={() => void ops.launchUseCase(selectedUseCase, selectedClock)}
		>
			<span data-testid="emu-launch-btn">{tOps(lang, 'emuLaunchBtn')}</span>
		</Button>
	</div>
	<p class="sampling mono" data-testid="emu-sampling">
		{tOps(lang, 'emuSampling', effectiveClock, tOps(lang, 'emuSamplingNote', sampling.value, sampling.unit))}
		{#if hero}· {tOps(lang, 'emuHeroCap', EMULATOR_HERO_CLOCK_CAP)}{/if}
	</p>

	{#if launch}
		<div class="launch-state" data-testid="emu-launch-state" data-phase={launch.phase} data-run-id={launch.runId ?? ''} data-contract={launch.contract ?? ''}>
			<div class="ls-head">
				<Badge variant={phaseVariant(launch.phase)} label={tOps(lang, `emuPhase_${launch.phase}`)} />
				<span class="mono">{launch.kind === 'replay' ? tOps(lang, 'emuReplayOf') : ''}{launch.useCaseId} ×{launch.clockScale}{launch.runId != null ? ` · run #${launch.runId}` : ''}</span>
				<span class="mono dim">{tOps(lang, 'emuSince', fmtAgeSince(launch.at, clock))}</span>
			</div>
			{#if trackedRun?.progress}
				{@const pct = progressPct(trackedRun)}
				<div class="bar" role="progressbar" aria-valuenow={pct ?? 0} aria-valuemin="0" aria-valuemax="100">
					<span class="fill" style:width="{pct ?? 0}%"></span>
				</div>
				<p class="mono dim">{tOps(lang, 'emuProgress', trackedRun.progress.frame, trackedRun.progress.total, trackedRun.progress.delivered, trackedRun.progress.failed)}</p>
			{/if}
			{#if trackedRun && trackedRun.status === 'running' && trackedRun.eventStatus === 'planning'}
				<p class="mono pending-event" data-testid="emu-event-pending">{tOps(lang, 'emuEventNotLive')}</p>
			{/if}
			{#each trackedRun?.premises ?? [] as p, i (i)}
				<p class="mono premise" class:err={p.result === 'failed'} data-testid="emu-premise" data-result={p.result} data-earmark={p.earmark ?? ''}>
					<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />
					<span>{premiseText(p)}</span>
				</p>
			{/each}
			{#if trackedRun && (trackedRun.status === 'failed' || trackedRun.status === 'interrupted') && trackedRun.failReason}
				<p class="mono err" data-testid="emu-fail-reason">{tOps(lang, 'emuFailReason', trackedRun.failReason)}</p>
			{/if}
			{#if launch.detail}<p class="mono dim">{launch.detail}</p>{/if}
			{#if launch.contract === 'sync' && launch.phase === 'running'}<p class="mono dim">{tOps(lang, 'emuSyncNote')}</p>{/if}
		</div>
	{/if}
	{#if ops.emulatorActionNote}<p class="note-line mono">{ops.emulatorActionNote}</p>{/if}

	{#if ops.emulatorRuns.length}
		<div class="runs">
			<div class="runs-head">
				<span class="runs-title">{tOps(lang, 'emuRunsTitle')}</span>
				<label class="replay-clock">
					<span>{tOps(lang, 'emuReplayClock')}</span>
					<select bind:value={replayClock}>
						{#each EMULATOR_CLOCKS as c (c)}
							<option value={c}>×{c}</option>
						{/each}
					</select>
				</label>
			</div>
			<table>
				<thead>
					<tr><th>#</th><th>{tOps(lang, 'emuUseCase')}</th><th>{tOps(lang, 'emuStatus')}</th><th>×</th><th>{tOps(lang, 'emuStarted')}</th><th></th></tr>
				</thead>
				<tbody>
					{#each ops.emulatorRuns as r (r.runId)}
						<tr data-testid="emu-run" data-run-id={r.runId} data-status={r.status}>
							<td class="num">{r.runId}{r.isReplay ? ' ↻' : ''}</td>
							<td class="uc" title={r.useCaseId}>{r.useCaseId}</td>
							<td
								class:okrun={r.status === 'completed'}
								class:badrun={r.status === 'failed' || r.status === 'interrupted'}
								class:runrun={r.status === 'running'}
								title={r.failReason ?? (r.status === 'running' && r.eventStatus === 'planning' ? tOps(lang, 'emuEventNotLive') : undefined)}
								data-event-status={r.eventStatus ?? ''}
							>
								{r.status}{#if r.status === 'running' && progressPct(r) != null}&nbsp;{progressPct(r)}%{/if}{#if r.status === 'running' && r.eventStatus === 'planning'}&nbsp;·&nbsp;{tOps(lang, 'emuEventPendingShort')}{/if}
							</td>
							<td class="num">{r.clockScale ?? '—'}</td>
							<td class="num">{r.createdAt ? istHm(r.createdAt) : '—'}</td>
							<td class="acts">
								{#if r.status === 'running'}
									<Button variant="danger-outline" size="sm" busy={ops.emulatorBusy === `stop:${r.runId}`} disabled={ops.emulatorBusy !== null} onclick={() => void ops.stopRun(r.runId)}>
										<span data-testid="emu-stop">{tOps(lang, 'emuStop')}</span>
									</Button>
								{:else}
									<Button variant="ghost" size="sm" busy={ops.emulatorBusy === `replay:${r.runId}`} disabled={ops.emulatorBusy !== null || !ops.emulator.online} onclick={() => void ops.replayRun(r.runId, replayClock)}>
										<span data-testid="emu-replay">{tOps(lang, 'emuReplay')}</span>
									</Button>
								{/if}
							</td>
						</tr>
						{#if r.premises?.length}
							<!-- the run's EMULATED scenario premise (C-N): earmark created / already active / failed → reads skipped -->
							<tr class="premise-row" data-testid="emu-run-premise" data-run-id={r.runId}>
								<td></td>
								<td colspan="5">
									{#each r.premises as p, i (i)}
										<span class="premise" class:err={p.result === 'failed'} data-result={p.result} data-earmark={p.earmark ?? ''}>{tOps(lang, 'labelEmulated')} · {premiseText(p)}</span>
									{/each}
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	<section class="living" data-testid="living-city" data-supported={ops.emulator.livingCitySupported ? '1' : '0'} data-status={lc?.status ?? ''}>
		<div class="lc-head">
			<span class="lc-title">{tOps(lang, 'emuLivingCity')}</span>
			{#if ops.emulator.livingCitySupported && lc}
				<Badge variant={lc.status === 'running' ? 'live' : 'neutral'} label={lc.status} />
				<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />
				<span class="spacer"></span>
				<Button
					variant="secondary"
					size="sm"
					busy={ops.livingCityBusy}
					onclick={() => void ops.setLivingCity(lc.status === 'running' ? 'stop' : 'start')}
				>
					<span data-testid="living-city-toggle">{lc.status === 'running' ? tOps(lang, 'emuLivingStop') : tOps(lang, 'emuLivingStart')}</span>
				</Button>
			{:else}
				<span class="mono dim">{ops.emulator.online ? tOps(lang, 'emuLivingAbsent') : tOps(lang, 'labelOffline')}</span>
			{/if}
		</div>
		{#if lc}
			<p class="mono dim">
				{lc.dateIst ?? '—'}{lc.daySeed != null ? ` · seed ${lc.daySeed}` : ''} · {tOps(lang, 'emuLivingSlots', lc.activeSlots.length)}{lc.units.length ? ` · ${tOps(lang, 'emuLivingUnits', lc.units.length)}` : ''}{lc.since ? ` · ${tOps(lang, 'emuSince', fmtAgeSince(lc.since, clock))}` : ''}
			</p>
			{#if lc.activeSlots.length}
				<ul class="slots">
					{#each lc.activeSlots.slice(0, 4) as s (s.slotId)}
						<li class="mono" data-slot-status={s.eventStatus ?? ''}>
							<span class="kind">{s.kind}</span> {ops.text(s.title)} <span class="dim">{s.startsAt ? istHm(s.startsAt) : '—'}–{s.endsAt ? istHm(s.endsAt) : '—'}</span>
							{#if s.eventStatus}<span class="slot-status" class:pending-event={s.eventStatus === 'planning'}>{s.eventStatus === 'planning' ? tOps(lang, 'emuEventPendingShort') : s.eventStatus}</span>{/if}
						</li>
					{/each}
				</ul>
			{/if}
			{#if lc.counters && Object.keys(lc.counters).length}
				<p class="mono dim" data-testid="living-city-counters">
					{Object.entries(lc.counters).slice(0, 6).map(([k, n]) => `${k.replace(/_/g, ' ')} ${n}`).join(' · ')}
					{#if lc.preempted}· {tOps(lang, 'emuLivingPreempted', lc.preempted)}{/if}
					{#if lc.refiling}· {tOps(lang, 'emuLivingRefiling', lc.refiling)}{/if}
					{#if lc.violators === false}· {tOps(lang, 'emuLivingNoViolators')}{/if}
				</p>
			{/if}
			{#if lc.detail}<p class="mono dim">{lc.detail}</p>{/if}
			{#if lc.nextCauses.length}
				<p class="mono dim">{tOps(lang, 'emuLivingNext')}: {ops.text(lc.nextCauses.slice(0, 2).map((c) => `${c.at ? istClock(c.at) : '—'} ${c.title}`).join(' · '))}</p>
			{/if}
		{/if}
		{#if ops.livingCityNote}<p class="mono err">{ops.livingCityNote}</p>{/if}
	</section>

	<p class="note">{tOps(lang, 'emuCausesOnly')}</p>
</Panel>

<style>
	.row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.detail {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.detail.lib {
		margin-left: auto;
	}

	.launch {
		display: flex;
		align-items: flex-end;
		gap: var(--v-space-2);
		flex-wrap: wrap;
		margin-top: var(--v-space-2);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.field.grow {
		flex: 1;
		min-width: 160px;
	}

	.field span,
	.replay-clock span {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
	}

	select {
		height: var(--v-ctl-sm);
		min-width: 0;
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.sampling {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-data);
	}

	.launch-state {
		margin-top: var(--v-space-2);
		padding: var(--v-space-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.launch-state p {
		margin: 0;
		font-size: var(--v-text-2xs);
	}

	.ls-head {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.bar {
		height: 4px;
		border-radius: var(--v-radius-pill);
		background: var(--v-line-1);
		overflow: hidden;
	}

	.fill {
		display: block;
		height: 100%;
		background: var(--v-data);
		transition: width var(--v-dur-out) var(--v-ease);
	}

	.note-line {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.runs {
		margin-top: var(--v-space-2);
	}

	.runs-head {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-1);
	}

	.runs-title {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
	}

	.replay-clock {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
	}

	.runs table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--v-text-2xs);
		table-layout: fixed;
	}

	.runs th {
		text-align: left;
		font-weight: var(--v-weight-status);
		color: var(--v-ink-3);
		padding: 1px var(--v-space-1);
		border-bottom: 1px solid var(--v-line-2);
	}

	.runs th:nth-child(1) {
		width: 4.2em; /* run id + the replay mark ↻ */
	}

	.runs th:nth-child(4) {
		width: 2.4em;
	}

	.runs th:nth-child(5) {
		width: 4.4em;
	}

	.runs th:nth-child(6) {
		width: 6.5em;
	}

	.runs td {
		padding: 1px var(--v-space-1);
		border-bottom: 1px solid var(--v-line-1);
		color: var(--v-ink-2);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.uc {
		font-family: var(--v-font-mono);
	}

	.acts {
		text-align: right;
	}

	.num {
		font-family: var(--v-font-mono);
	}

	.okrun {
		color: var(--v-band-green);
	}

	.badrun {
		color: var(--v-band-red);
	}

	.runrun {
		color: var(--v-data);
	}

	.living {
		margin-top: var(--v-space-2);
		padding-top: var(--v-space-2);
		border-top: 1px solid var(--v-line-1);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.living p {
		margin: 0;
		font-size: var(--v-text-2xs);
	}

	.lc-head {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.lc-head .dim {
		font-size: var(--v-text-2xs);
	}

	.lc-title {
		font-size: var(--v-text-xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
	}

	.spacer {
		flex: 1;
	}

	.slots {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.slots .kind {
		color: var(--v-data);
	}

	.dim {
		color: var(--v-ink-3);
	}

	.err {
		color: var(--v-band-red);
	}

	.note {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	/* a run (or slot) whose corridor event has not gone live yet — said, never hidden */
	.pending-event {
		color: var(--v-band-amber);
	}

	.slot-status {
		margin-left: var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	/* a run's EMULATED scenario premise (C-N) — a failed one in red: its stolen-plate reads were skipped */
	.premise {
		color: var(--v-ink-2);
	}

	.premise.err {
		color: var(--v-band-red);
	}

	.launch-state p.premise {
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		flex-wrap: wrap;
	}

	.runs .premise-row td {
		white-space: normal;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
	}

	.premise-row .premise {
		display: block;
	}
</style>
