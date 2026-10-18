<script lang="ts">
	/**
	 * PlannerRail — the template's left planning rail, rebuilt as the REAL
	 * Blue Book lifecycle on the live two-tier brain:
	 *
	 *   pick origin/destination (canonical Ahmedabad POIs, the template's
	 *   own list) + protection class → PLAN runs createMovement +
	 *   planMovement through convoy-brain (the same audited LangGraph
	 *   workflows the original aspired to) → route candidates render with
	 *   the RECOMMENDED ribbon on the decider's pick → a named duty
	 *   officer (the C6 gate) launches the escort (launchEscort).
	 *
	 * No client-side simulation anywhere: every step is a real call; every
	 * failure renders honestly in the rail.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { createMovement, planMovement, launchEscort } from '$lib/api/client';
	import type { PlanRouteCandidate } from '$lib/api/client';
	import { ops } from '$lib/ops/store.svelte';
	import { toasts } from '$lib/shell/toast.svelte';
	import { t } from '$lib/i18n/strings';

	/**
	 * The template's canonical POI list ([lon, lat] — API order), verified
	 * against the SEEDED Ahmedabad network (corridor.road_segments): eight
	 * template POIs land exactly on network endpoints; off-network template
	 * names are replaced by on-network junction anchors so every plan call
	 * resolves to real segments (the planner snaps via a ±0.002° bbox).
	 */
	const POIS: ReadonlyArray<{ name: string; coord: [number, number] }> = [
		{ name: 'Sarkhej–Ashram South', coord: [72.5714, 23.0225] },
		{ name: 'Lal Darwaja', coord: [72.5798, 23.0254] },
		{ name: 'Ellis Bridge / CG Road', coord: [72.562, 23.03] },
		{ name: 'Sabarmati Riverfront', coord: [72.578, 23.035] },
		{ name: 'Sabarmati Ashram', coord: [72.5802, 23.0607] },
		{ name: 'Narendra Modi Stadium', coord: [72.5957, 23.0916] },
		{ name: 'Drive-In Road', coord: [72.542, 23.035] },
		{ name: 'Prahlad Nagar', coord: [72.52, 23.025] },
		{ name: 'Satellite', coord: [72.51, 23.02] },
		{ name: 'CTM Crossroads', coord: [72.61, 23.03] },
		{ name: 'Naroda', coord: [72.635, 23.045] },
		{ name: 'Kankaria Lake', coord: [72.6, 23.007] },
		{ name: 'SVPI Airport', coord: [72.6266, 23.0733] },
		{ name: 'Science City', coord: [72.6588, 23.0684] },
	];

	/** Protection-class pills (template coding: Z+ red · Z orange · Y blue · X slate — token slots only). */
	const CLASSES: ReadonlyArray<{ id: 'Z+' | 'Z' | 'Y' | 'X'; token: string }> = [
		{ id: 'Z+', token: '--v-band-red' },
		{ id: 'Z', token: '--v-act' },
		{ id: 'Y', token: '--v-data' },
		{ id: 'X', token: '--v-ink-3' },
	];

	let originIdx = $state(0);
	let destIdx = $state(1);
	let vvipClass = $state<'Z+' | 'Z' | 'Y' | 'X'>('Z');
	let departure = $state('');

	let phase = $state<'idle' | 'planning' | 'planned' | 'escorting'>('idle');
	let movementId = $state<string | null>(null);
	let candidates = $state<PlanRouteCandidate[]>([]);
	let recommendation = $state<string | null>(null);
	let securityNote = $state<string | null>(null);
	let error = $state<string | null>(null);
	let escortStartedAt = $state<number | null>(null);

	/** The decider's recommended candidate (top of the ranked list). */
	const bestId = $derived(candidates[0]?.route_id ?? null);

	function defaultDeparture(): string {
		const d = new Date(Date.now() + 30 * 60 * 1000);
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
	}

	async function plan() {
		if (phase === 'planning') return;
		error = null;
		candidates = [];
		recommendation = null;
		phase = 'planning';
		const dep = departure || defaultDeparture();
		const params = {
			origin: POIS[originIdx].coord,
			destination: POIS[destIdx].coord,
			vvipClass,
			plannedDeparture: new Date(dep).toISOString(),
		};
		try {
			const { movementId: id } = await createMovement(params);
			movementId = id;
			const doc = await planMovement(id!, params);
			const ranked = [doc.primary_route, ...(doc.alternate_routes ?? [])].filter(
				(r): r is PlanRouteCandidate => r != null,
			);
			candidates = ranked;
			recommendation = doc.overall_reasoning ?? null;
			securityNote = doc.security_compliant
				? `security ✓ ${typeof doc.security_score === 'number' ? doc.security_score.toFixed(2) : ''}`
				: 'security: not compliant — see audit';
			phase = ranked.length ? 'planned' : 'idle';
			if (!ranked.length) error = doc.overall_reasoning ?? 'decider returned no candidates';
			toasts.push({
				kind: 'act',
				title: t(ops.lang, 'tpl_candidates'),
				body: `${id} · ${ranked.length} ${t(ops.lang, 'tpl_planned_by')}`,
			});
		} catch (err) {
			phase = 'idle';
			error = err instanceof Error ? err.message : String(err);
		}
	}

	async function escort() {
		if (!movementId || phase === 'escorting') return;
		error = null;
		try {
			await launchEscort(movementId, POIS[destIdx].coord);
			phase = 'escorting';
			escortStartedAt = Date.now();
			toasts.push({
				kind: 'act',
				title: t(ops.lang, 'tpl_escort_live'),
				body: `${movementId} · ${ops.dutyOfficer}`,
			});
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	function reset() {
		phase = 'idle';
		movementId = null;
		candidates = [];
		recommendation = null;
		securityNote = null;
		error = null;
		escortStartedAt = null;
	}

	const plannedEtaMin = $derived.by(() => {
		const best = candidates[0];
		return best ? Math.round((best.estimated_time_sec ?? 0) / 60) : null;
	});

	/** Exported readouts for the escort HUD (page wires via callback prop). */
	const escortState = $derived({
		movementId,
		startedAt: escortStartedAt,
		plannedEtaMin,
		vvipClass,
		destination: POIS[destIdx].name,
	});

	let {
		onEscortState = () => {}
	}: { onEscortState?: (s: typeof escortState) => void } = $props();

	$effect(() => {
		onEscortState(escortState);
	});
</script>

<Panel variant="glass" title={t(ops.lang, 'tpl_planner')} sub="pplx-27b decider · gemma tactical · audited" lang={ops.lang} scroll>
	<div class="form">
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_origin')}</span>
			<select bind:value={originIdx} disabled={phase === 'planning' || phase === 'escorting'}>
				{#each POIS as poi, i (poi.name)}
					<option value={i}>{poi.name}</option>
				{/each}
			</select>
		</label>
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_destination')}</span>
			<select bind:value={destIdx} disabled={phase === 'planning' || phase === 'escorting'}>
				{#each POIS as poi, i (poi.name)}
					<option value={i}>{poi.name}</option>
				{/each}
			</select>
		</label>
		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_departure')}</span>
			<input type="datetime-local" bind:value={departure} placeholder={defaultDeparture()} />
		</label>

		<div class="field">
			<span class="field-label">{t(ops.lang, 'tpl_class')}</span>
			<div class="class-pills" role="radiogroup" aria-label={t(ops.lang, 'tpl_class')}>
				{#each CLASSES as c (c.id)}
					<button
						type="button"
						role="radio"
						aria-checked={vvipClass === c.id}
						class="class-pill"
						class:selected={vvipClass === c.id}
						style={`--pill: var(${c.token})`}
						onclick={() => (vvipClass = c.id)}
						disabled={phase === 'escorting'}
					>
						{c.id}
					</button>
				{/each}
			</div>
		</div>

		<label class="field">
			<span class="field-label">{t(ops.lang, 'tpl_officer')}</span>
			<input
				type="text"
				bind:value={ops.dutyOfficer}
				placeholder="e.g. R. Mehta"
				title={t(ops.lang, 'officer_hint')}
			/>
		</label>

		<div class="actions">
			{#if phase === 'escorting'}
				<Button variant="secondary" size="sm" onclick={reset}>{t(ops.lang, 'btn_close')}</Button>
			{:else}
				<Button variant="secondary" size="sm" busy={phase === 'planning'} onclick={() => void plan()}>
					{phase === 'planning' ? t(ops.lang, 'tpl_planning') : t(ops.lang, 'tpl_plan')}
				</Button>
				{#if phase === 'planned' && movementId}
					<Button
						variant="primary"
						size="sm"
						actor={ops.officerOn ? ops.dutyOfficer : ''}
						onclick={() => void escort()}
					>
						{t(ops.lang, 'tpl_escort')}
					</Button>
				{/if}
			{/if}
		</div>

		{#if error}
			<p class="error mono">{error}</p>
		{/if}
	</div>

	{#if phase === 'planned' || phase === 'escorting'}
		<div class="candidates">
			<h3 class="rail-h">{t(ops.lang, 'tpl_candidates')}</h3>
			{#if recommendation}
				<p class="rec">{recommendation}</p>
			{/if}
			{#if securityNote}
				<p class="sec-note mono">{securityNote}</p>
			{/if}
			{#each candidates as route, idx (route.route_id)}
				{@const recommended = route.route_id === bestId}
				{@const score = route.score ?? route.weighted_score ?? route.composite_score ?? null}
				<article class="cand" class:recommended>
					{#if recommended}<span class="ribbon">{t(ops.lang, 'tpl_recommended')}</span>{/if}
					<header class="cand-head">
						<span class="cand-name">{t(ops.lang, 'tpl_candidates')} {String.fromCharCode(65 + idx)}</span>
						{#if score != null}<span class="cand-score mono">{score.toFixed(2)}</span>{/if}
					</header>
					<div class="cand-grid mono">
						{#if route.total_distance_m != null}
							<span>{(route.total_distance_m / 1000).toFixed(1)} {t(ops.lang, 'tpl_km')}</span>
						{/if}
						{#if route.estimated_time_sec != null}
							<span>{Math.round(route.estimated_time_sec / 60)} {t(ops.lang, 'tpl_min')}</span>
						{/if}
						<span>{route.segment_ids?.length ?? 0} {t(ops.lang, 'tpl_segs')}</span>
					</div>
					{#if route.reason}
						<p class="cand-why"><em>{t(ops.lang, 'tpl_why')}:</em> {route.reason}</p>
					{/if}
					{#if route.security_score != null && route.disruption_score != null}
						<div class="cand-bars">
							<span class="bar" title={`security ${route.security_score.toFixed(2)}`}>
								<i style={`width: ${Math.round(route.security_score * 100)}%`}></i>
								<b>SEC</b>
							</span>
							<span class="bar" title={`disruption ${route.disruption_score.toFixed(2)}`}>
								<i style={`width: ${Math.round(route.disruption_score * 100)}%`}></i>
								<b>DIS</b>
							</span>
						</div>
					{/if}
				</article>
			{/each}
		</div>
	{:else if phase === 'idle' && !error}
		<EmptyState title={t(ops.lang, 'tpl_no_plan')} />
	{/if}

	{#if movementId}
		<p class="mid mono">mvmt {movementId}</p>
	{/if}
</Panel>

<style>
	.form {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		min-width: 0;
	}

	.field-label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	select,
	input {
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-1);
		font-family: var(--v-font-ui);
		font-size: var(--v-text-xs);
		padding: var(--v-space-1) var(--v-space-2);
		width: 100%;
	}

	select:focus-visible,
	input:focus-visible {
		outline: none;
		border-color: var(--v-act);
		box-shadow: 0 0 0 2px var(--v-act-veil);
	}

	.class-pills {
		display: flex;
		gap: var(--v-space-1);
	}

	.class-pill {
		flex: 1;
		height: var(--v-ctl-md);
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-line-2);
		background: transparent;
		color: var(--pill, var(--v-ink-2));
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		font-weight: 700;
		cursor: pointer;
		transition:
			border-color var(--v-dur-out) var(--v-ease),
			box-shadow var(--v-dur-out) var(--v-ease);
	}

	.class-pill.selected {
		border-color: var(--pill, var(--v-act));
		box-shadow: 0 0 10px var(--v-glow-act);
		background: color-mix(in srgb, var(--pill, var(--v-act)) 12%, transparent);
	}

	.actions {
		display: flex;
		gap: var(--v-space-2);
	}

	.error {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
		border-left: 3px solid var(--v-band-red);
		padding-left: var(--v-space-2);
		word-break: break-word;
	}

	.rail-h {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.rec {
		margin: var(--v-space-1) 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border-left: 3px solid var(--v-act);
		padding-left: var(--v-space-2);
	}

	.sec-note {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: 0.04em;
	}

	.candidates {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		margin-top: var(--v-space-2);
	}

	.cand {
		position: relative;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		background: var(--v-surface-1);
	}

	.cand.recommended {
		border-color: var(--v-act);
		box-shadow: 0 0 14px var(--v-glow-act);
		overflow: hidden;
	}

	.ribbon {
		position: absolute;
		top: 0;
		right: 0;
		background: linear-gradient(135deg, var(--v-act), var(--v-act-2));
		color: var(--v-surface-sink);
		font-family: var(--v-font-display);
		font-size: var(--v-text-2xs);
		font-weight: 800;
		letter-spacing: 0.12em;
		padding: 2px 8px;
		border-bottom-left-radius: var(--v-radius-btn);
	}

	.cand-head {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.cand-name {
		font-family: var(--v-font-display);
		font-size: var(--v-text-xs);
		font-weight: 700;
		color: var(--v-ink-1);
	}

	.cand-score {
		font-size: var(--v-text-sm);
		font-weight: 700;
		color: var(--v-data);
		font-variant-numeric: tabular-nums;
	}

	.cand-grid {
		display: flex;
		gap: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		font-variant-numeric: tabular-nums;
	}

	.cand-why {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border-left: 3px solid var(--v-act);
		padding-left: var(--v-space-2);
	}

	.cand-why em {
		color: var(--v-act-text);
		font-style: normal;
	}

	.cand-bars {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.bar {
		position: relative;
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.bar i {
		display: block;
		height: 3px;
		border-radius: 2px;
		background: linear-gradient(90deg, var(--v-act), var(--v-act-2));
		box-shadow: 0 0 6px var(--v-glow-act);
	}

	.bar:first-child i {
		background: var(--v-band-green);
		box-shadow: none;
	}

	.bar b {
		font-weight: 400;
		letter-spacing: var(--v-tracking-status);
	}

	.mid {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
