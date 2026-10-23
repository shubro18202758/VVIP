<script lang="ts">
	/**
	 * WatchlistConsole — earmarked plates, ANPR sightings, and the pursuit
	 * entry point. Reads GET /api/v1/vehicles (earmark state, lawful basis,
	 * retention horizon, last sighting — every synthetic read labelled
	 * EMULATED per the ethics rail); the earmark/clear writers sit behind
	 * the named-officer gate with the lawful-basis enum the backend enforces.
	 * The retention line states the sighting-retention policy and whether
	 * the reaper is actually running (GET /api/v1/vehicles/retention — a
	 * failing DPDP control is shown, never silent). Plates are masked
	 * (GJ27EM•••1) until a duty officer is named (C6).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { LAWFUL_BASIS_CODES, EARMARK_STATES, POLL_MS } from '$lib/ops/endpoints';
	import { tOps } from '$lib/i18n/strings.ops';
	import { fmtAgeSince, fmtRetentionUntil, istClock } from '$lib/ops/format';

	const lang = $derived(ops.lang);

	let plate = $state('');
	let earmarkState = $state<'watch' | 'flagged'>('flagged');
	let basis = $state<(typeof LAWFUL_BASIS_CODES)[number]>('stolen_vehicle_fir');
	let reason = $state('');
	let days = $state(30);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	const ret = $derived(ops.retention);

	async function submitEarmark() {
		if (!plate.trim() || reason.trim().length < 3) return;
		const ok = await ops.earmarkPlate(plate.trim(), earmarkState, reason.trim(), basis, days);
		if (ok) {
			plate = '';
			reason = '';
		}
	}
</script>

<Panel
	title={tOps(lang, 'watchTitle')}
	sub={tOps(lang, 'watchSub')}
	status={ops.watchlistUpdated ? { state: ops.watchlistLive === false ? 'stale' : 'live', updatedAt: ops.watchlistUpdated } : null}
	pollMs={POLL_MS.watchlist}
	lang={lang}
	scroll
>
	<p class="retention mono" data-testid="retention-health" data-healthy={ret?.healthy == null ? '' : ret.healthy ? '1' : '0'}>
		{#if ret}
			<span>{tOps(lang, 'retentionLine', ret.defaultDays ?? '—', ret.earmarkCapDays ?? '—')}</span>
			<span class:ok={ret.healthy === true} class:bad={ret.healthy === false}>
				{ret.healthy === false ? tOps(lang, 'retentionFailing', ret.consecutiveFailures) : ret.healthy ? tOps(lang, 'retentionHealthy') : '—'}
			</span>
			{#if ret.lastSuccessAt}<span>{tOps(lang, 'retentionLastRun', istClock(ret.lastSuccessAt), ret.lastRemoved ?? 0)}</span>{/if}
		{:else}
			<span>{ops.retentionNote ?? tOps(lang, 'retentionUnknown')}</span>
		{/if}
	</p>
	<form class="earmark-form" onsubmit={(e) => { e.preventDefault(); void submitEarmark(); }}>
		<input
			class="w-input mono"
			type="text"
			placeholder={tOps(lang, 'watchPlate')}
			bind:value={plate}
			spellcheck="false"
			autocomplete="off"
		/>
		<select class="w-select" bind:value={earmarkState} aria-label={tOps(lang, 'watchState')}>
			{#each EARMARK_STATES as s (s)}
				<option value={s}>{s}</option>
			{/each}
		</select>
		<select class="w-select" bind:value={basis} aria-label={tOps(lang, 'watchBasis')}>
			{#each LAWFUL_BASIS_CODES as b (b)}
				<option value={b}>{b}</option>
			{/each}
		</select>
		<input
			class="w-input reason"
			type="text"
			placeholder={tOps(lang, 'watchReasonPlaceholder')}
			bind:value={reason}
		/>
		<label class="days">
			<span class="mono">{tOps(lang, 'watchDays')}</span>
			<input class="w-days mono" type="number" min="1" max="90" bind:value={days} />
		</label>
		<Button variant="primary" size="sm" actor={ops.officerOn ? ops.dutyOfficer : null} onclick={() => void submitEarmark()}>
			{tOps(lang, 'watchEarmark')}
		</Button>
	</form>

	{#if ops.watchlist.length === 0}
		<EmptyState title={tOps(lang, 'watchNone')} hint={ops.watchlistNote ?? undefined} />
	{:else}
		<ul class="watch-rows">
			{#each ops.watchlist as w (w.vehicleId)}
				<li class="watch-row" class:flagged={w.earmarkState === 'flagged'}>
					<div class="row-main">
						<span class="plate mono" data-testid="watch-plate">{ops.plate(w.plate) ?? w.vehicleId.slice(0, 8)}</span>
						<Badge variant={w.earmarkState === 'flagged' ? 'act' : 'pending'} label={w.earmarkState ?? '—'} />
						{#if w.lastSighting?.source === 'emulated_anpr'}
							<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />
						{/if}
					</div>
					<p class="row-reason">{ops.text(w.earmarkReason) || '—'}</p>
					<dl class="row-facts mono">
						<div><dt>{tOps(lang, 'watchBasis')}</dt><dd>{w.lawfulBasis ?? '—'}</dd></div>
						<div><dt>{tOps(lang, 'watchRetention')}</dt><dd>{fmtRetentionUntil(w.retentionUntil, clock)}</dd></div>
						<div><dt>{tOps(lang, 'watchLastSeen')}</dt><dd>{fmtAgeSince(w.lastSighting?.t ?? null, clock)}{w.lastSighting?.t ? ` · ${istClock(w.lastSighting.t)} IST` : ''}{w.lastSighting?.speedKmh != null ? ` · ${tOps(lang, 'watchSpeed', Math.round(w.lastSighting.speedKmh))}` : ''}</dd></div>
					</dl>
					<div class="row-actions">
						<Button
							variant="ghost"
							size="sm"
							busy={ops.watchBusy === w.vehicleId}
							actor={ops.officerOn ? ops.dutyOfficer : null}
							onclick={() => void ops.clearWatch(w.vehicleId)}
						>
							{tOps(lang, 'watchClear')}
						</Button>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</Panel>

<style>
	.retention {
		margin: 0 0 var(--v-space-2);
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.retention .ok {
		color: var(--v-band-green);
	}

	.retention .bad {
		color: var(--v-band-red);
	}

	.earmark-form {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-2);
		align-items: center;
		margin-bottom: var(--v-space-3);
		padding-bottom: var(--v-space-3);
		border-bottom: 1px solid var(--v-line-1);
	}

	.w-input {
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		min-width: 90px;
	}

	.w-input.reason {
		flex: 1;
		min-width: 160px;
	}

	.w-select {
		height: var(--v-ctl-sm);
		font: inherit;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.days {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.w-days {
		width: 52px;
		height: var(--v-ctl-sm);
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.watch-rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
	}

	.watch-row {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		padding: var(--v-space-2) var(--v-space-3);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-1);
		border-left: 2px solid var(--v-band-amber);
		border-radius: var(--v-radius-panel);
	}

	.watch-row.flagged {
		border-left-color: var(--v-band-red);
	}

	.row-main {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.plate {
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
		letter-spacing: 0.04em;
	}

	.row-reason {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.row-facts {
		margin: 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-3);
	}

	.row-facts div {
		display: flex;
		gap: var(--v-space-1);
		align-items: baseline;
	}

	.row-facts dt {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.row-facts dd {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.row-actions {
		display: flex;
		justify-content: flex-end;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
