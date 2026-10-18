<script lang="ts">
	/**
	 * EmulatorPanel — cause-tape control, restyled on the design system
	 * (behavior unchanged): talks to the emulator on the absolute loopback
	 * constant, stages use-case tapes, lists recent runs. Offline = labelled
	 * chip; the rest of the board keeps polling the real stack APIs.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { EMULATOR_BASE } from '$lib/ops/endpoints';
	import { USE_CASES } from '$lib/ops/usecases';

	let selectedUseCase = $state(USE_CASES[1].id);
	// Default ×60: a demo tape completes in ~1 min wall time; ×1 runs real-time.
	let selectedClock = $state(60);

	const lang = $derived(ops.lang);
</script>

{#snippet headRight()}
	<Badge variant={ops.emulator.online ? 'live' : 'offline'} label={ops.emulator.online ? t(lang, 'emuOnline') : t(lang, 'emuOffline')} />
{/snippet}

<Panel
	title={t(lang, 'emuTitle')}
	sub={`${t(lang, 'emuSub')} · ${EMULATOR_BASE}`}
	status={ops.emulator.lastChecked ? { state: ops.emulator.online ? 'live' : 'offline', updatedAt: ops.emulator.lastChecked } : null}
	lang={lang}
	{...{ 'header-right': headRight }}
>
	<div class="row">
		<Button variant="ghost" size="sm" onclick={() => void ops.pollEmulator()}>{t(lang, 'emuCheck')}</Button>
		<span class="detail mono">
			{#if ops.emulator.lastChecked}
				{ops.emulator.online ? 'GET /health ok' : (ops.emulator.healthDetail ?? 'connection refused').slice(0, 80)}
			{:else}
				probing…
			{/if}
		</span>
	</div>

	<div class="row launch">
		<label>
			<span>{t(lang, 'emuLaunch')}</span>
			<select bind:value={selectedUseCase}>
				{#each USE_CASES as u (u.id)}
					<option value={u.id}>
						{u.name}{ops.emulatorUseCaseIds.includes(u.id) ? '' : ' (not in emulator library)'}
					</option>
				{/each}
			</select>
		</label>
		<label class="clock">
			<span>clock ×</span>
			<select bind:value={selectedClock}>
				{#each [1, 10, 60] as c (c)}
					<option value={c}>{c}</option>
				{/each}
			</select>
		</label>
		<Button
			variant="secondary"
			size="sm"
			disabled={!ops.emulator.online || !ops.emulatorUseCaseIds.includes(selectedUseCase)}
			onclick={() => void ops.stageUseCase(selectedUseCase, selectedClock)}
		>
			{t(lang, 'emuLaunch')}
		</Button>
	</div>

	{#if ops.emulator.runRequested}
		<p class="ok mono">staged: {ops.emulator.runRequested}</p>
	{/if}
	{#if ops.emulator.runError}
		<p class="err mono">{ops.emulator.runError}</p>
	{/if}

	{#if ops.emulatorRuns.length}
		<div class="runs">
			<div class="runs-title">runs (GET /api/v1/emulator/runs)</div>
			<table>
				<thead>
					<tr><th>#</th><th>use case</th><th>status</th><th>clock</th></tr>
				</thead>
				<tbody>
					{#each ops.emulatorRuns as r (r.runId)}
						<tr>
							<td class="num">{r.runId}{r.isReplay ? ' ↻' : ''}</td>
							<td>{r.useCaseId}</td>
							<td class:okrun={r.status === 'completed'} class:badrun={r.status === 'failed'}>{r.status}</td>
							<td class="num">×{r.clockScale ?? '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	<p class="note">
		{t(lang, 'emuLaunchNote')}
		Injects causes only (event rows, inputs docs, scheduled events, NATS corridor.traffic.emulated) — the
		decision stack plans, classifies and shadow-evals on its own.
	</p>
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

	.launch label {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.launch label span {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.launch select {
		height: var(--v-ctl-sm);
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.ok {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-green);
	}

	.err {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
		word-break: break-all;
	}

	.runs-title {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		margin-bottom: var(--v-space-1);
	}

	.runs table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--v-text-2xs);
	}

	.runs th {
		text-align: left;
		font-weight: var(--v-weight-status);
		color: var(--v-ink-4);
		padding: 1px var(--v-space-2);
		border-bottom: 1px solid var(--v-line-2);
	}

	.runs td {
		padding: 1px var(--v-space-2);
		border-bottom: 1px solid var(--v-line-1);
		color: var(--v-ink-2);
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

	.note {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
