<script lang="ts">
	/**
	 * ReplayBar — the REPLAY face of the fusion rail: one recorded emulator
	 * tape on the canvas clock (hud/replay.svelte.ts → replay.ts).
	 *
	 *   the permanent EMULATED pill (a tape is a recording of emulated causes)
	 *   run picker · play / pause · speed · the scrubber (tape seconds, with the
	 *   wall time the frame was recorded at, IST)
	 *   the inputs_doc sparkline — predicted congestion, crowd score, weather —
	 *   with the cursor
	 *   the tape's narrative up to the cursor (hits, incidents, unit status);
	 *   a row click moves the cursor to that frame
	 */
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { displayText } from '../plates';
	import { replay } from './replay.svelte';
	import { istDayTime, istTime } from './format';

	const lang = $derived(ops.lang);
	const tape = $derived(replay.tape);
	const duration = $derived(tape?.durationSec ?? 0);
	const narrative = $derived(replay.narrativeUpTo(replay.cursorSec).slice(0, 60));
	const readsSoFar = $derived(tape ? tape.anpr.filter((r) => r.t <= replay.cursorSec).length : 0);
	const hitsSoFar = $derived(tape ? tape.anpr.filter((r) => r.t <= replay.cursorSec && r.hit).length : 0);

	const SPARK_W = 360;
	const SPARK_H = 46;

	function sparkPath(key: 'predictedCongestion' | 'crowdScore' | 'weatherSeverity'): string {
		if (!tape || !tape.inputs.length || duration <= 0) return '';
		const pts = tape.inputs
			.filter((p) => p[key] != null)
			.map((p) => `${((p.t / duration) * SPARK_W).toFixed(1)},${(SPARK_H - 2 - Math.max(0, Math.min(1, p[key] as number)) * (SPARK_H - 4)).toFixed(1)}`);
		return pts.length ? `M${pts.join(' L')}` : '';
	}

	const cursorX = $derived(duration > 0 ? (replay.cursorSec / duration) * SPARK_W : 0);

	function clockLabel(sec: number): string {
		const s = Math.max(0, Math.round(sec));
		return `t+${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
	}
</script>

<div class="replay" data-testid="replay-bar" data-status={replay.status}>
	<div class="controls">
		<span class="emu-pill" data-testid="replay-emulated-pill" title={t(lang, 'cc_replay_note')}>◐ {t(lang, 'cc_emulated')} · REPLAY</span>
		<label class="run">
			<span>{t(lang, 'cc_run')}</span>
			<select
				value={replay.runId ?? ''}
				onchange={(e) => {
					// Run ids are positive integers; an empty selection loads nothing.
					const raw = (e.currentTarget as HTMLSelectElement).value;
					const id = Number(raw);
					if (raw !== '' && Number.isInteger(id) && id > 0 && id !== replay.runId) void replay.load(id);
				}}
				data-testid="replay-run"
			>
				{#if replay.runId != null && !replay.runs.some((r) => r.runId === replay.runId)}
					<option value={replay.runId}>run {replay.runId}</option>
				{/if}
				{#each replay.runs as r (r.runId)}
					<option value={r.runId}>run {r.runId} · {r.useCaseId ?? '—'}{r.isReplay ? ' (replay)' : ''}</option>
				{/each}
			</select>
		</label>
		<button type="button" class="btn" onclick={() => (replay.playing ? replay.pause() : replay.play())} disabled={replay.status !== 'ready'}>
			{replay.playing ? `❚❚ ${t(lang, 'cc_pause')}` : `▶ ${t(lang, 'cc_play')}`}
		</button>
		<label class="speed">
			<span>{t(lang, 'cc_speed')}</span>
			<select bind:value={replay.speed}>
				<option value={10}>×10</option>
				<option value={30}>×30</option>
				<option value={60}>×60</option>
				<option value={120}>×120</option>
			</select>
		</label>
		<span class="clock mono">
			{clockLabel(replay.cursorSec)} / {clockLabel(duration)}
			{#if tape}· {istTime(tape.t0 + replay.cursorSec * 1000)} IST ({istDayTime(tape.t0)}){/if}
		</span>
		<span class="counts mono">{readsSoFar} reads · <b class:act={hitsSoFar > 0}>{hitsSoFar} hits</b></span>
	</div>

	{#if replay.status === 'ready' && tape}
		<div class="scrub-row">
			<input
				type="range"
				class="scrubber"
				min="0"
				max={duration}
				step="1"
				value={replay.cursorSec}
				oninput={(e) => {
					replay.pause();
					replay.setCursor(Number((e.currentTarget as HTMLInputElement).value));
				}}
				aria-label="replay scrubber (tape seconds)"
				data-testid="replay-scrubber"
			/>
			<svg class="spark" viewBox={`0 0 ${SPARK_W} ${SPARK_H}`} preserveAspectRatio="none" role="img" aria-label="tape inputs: predicted congestion, crowd, weather">
				<path class="s-cong" d={sparkPath('predictedCongestion')} />
				<path class="s-crowd" d={sparkPath('crowdScore')} />
				<path class="s-wx" d={sparkPath('weatherSeverity')} />
				<line class="s-cursor" x1={cursorX} x2={cursorX} y1="0" y2={SPARK_H} />
			</svg>
			<div class="spark-key mono">
				<span class="k-cong">congestion</span> · <span class="k-crowd">crowd</span> · <span class="k-wx">weather</span>
			</div>
		</div>
		<ol class="narrative">
			{#each narrative as n (`${n.frame}-${n.kind}-${n.t}`)}
				<li>
					<button type="button" class="n-row sev-{n.severity}" onclick={() => replay.setCursor(n.t)}>
						<span class="mono t">{clockLabel(n.t)}</span>
						<span class="mono k">{n.kind.toUpperCase()}</span>
						<span class="txt">{displayText(n.text, ops.officerOn)}</span>
						<span class="pill">{t(lang, 'cc_emulated')}</span>
					</button>
				</li>
			{:else}
				<li class="empty mono">— {t(lang, 'cc_replay_note')} —</li>
			{/each}
		</ol>
	{:else}
		<p class="note mono">
			{replay.status === 'loading' ? 'loading the tape…' : (replay.note ?? t(lang, 'cc_replay_empty'))}
		</p>
	{/if}
</div>

<style>
	.replay {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		flex: 1;
		min-height: 0;
	}

	.controls {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		flex-wrap: wrap;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.emu-pill {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		font-weight: 700;
		letter-spacing: 0.1em;
		padding: 2px var(--v-space-2);
		border: 1px dashed var(--v-tool);
		border-radius: var(--v-radius-pill);
		color: var(--v-tool);
	}

	.run,
	.speed {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--v-ink-3);
	}

	select {
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-1);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		padding: 1px var(--v-space-1);
	}

	.btn {
		border: 1px solid var(--v-data);
		border-radius: var(--v-radius-btn);
		background: transparent;
		color: var(--v-data);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		padding: 2px var(--v-space-2);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.4;
		cursor: default;
	}

	.clock,
	.counts {
		color: var(--v-ink-2);
	}

	.act {
		color: var(--v-act-text);
	}

	.scrub-row {
		display: grid;
		grid-template-columns: 1fr;
		gap: 2px;
	}

	.scrubber {
		width: 100%;
		accent-color: var(--v-tool);
	}

	.spark {
		width: 100%;
		height: 38px;
		background: color-mix(in srgb, var(--v-surface-sink) 60%, transparent);
		border-radius: var(--v-radius-chip);
	}

	.spark path {
		fill: none;
		stroke-width: 1.5;
		vector-effect: non-scaling-stroke;
	}

	.s-cong {
		stroke: var(--v-band-amber);
	}

	.s-crowd {
		stroke: var(--v-tool);
	}

	.s-wx {
		stroke: var(--v-data);
		opacity: 0.7;
	}

	.s-cursor {
		stroke: var(--v-ink-1);
		stroke-width: 1.5;
		vector-effect: non-scaling-stroke;
	}

	.spark-key {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.k-cong {
		color: var(--v-band-amber);
	}

	.k-crowd {
		color: var(--v-tool);
	}

	.k-wx {
		color: var(--v-data);
	}

	.narrative {
		list-style: none;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		flex: 1;
		min-height: 0;
		scrollbar-width: thin;
	}

	.n-row {
		display: grid;
		grid-template-columns: auto auto minmax(0, 1fr) auto;
		gap: var(--v-space-2);
		align-items: baseline;
		width: 100%;
		padding: 2px var(--v-space-2);
		background: transparent;
		border: none;
		border-left: 2px solid var(--v-tool);
		color: var(--v-ink-2);
		font: inherit;
		font-size: var(--v-text-2xs);
		text-align: left;
		cursor: pointer;
	}

	.n-row:hover {
		background: var(--v-surface-2);
	}

	.n-row.sev-critical {
		border-left-color: var(--v-act);
		color: var(--v-ink-1);
	}

	.t {
		color: var(--v-ink-3);
	}

	.k {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.txt {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pill {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		padding: 0 5px;
		border: 1px dashed var(--v-ink-3);
		border-radius: var(--v-radius-pill);
		color: var(--v-ink-3);
	}

	.note,
	.empty {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		padding: var(--v-space-2);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
