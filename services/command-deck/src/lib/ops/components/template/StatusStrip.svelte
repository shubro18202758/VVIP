<script lang="ts">
	/**
	 * StatusStrip — the VVIP corridor module's top status bar row, rebuilt on
	 * tokens and wired to the REAL stack: model-tier health dots read the
	 * live convoy-brain health (decider pplx-27b + gemma tactical, with the
	 * degradation tier) — a decider still LOADING its weights is amber
	 * 'loading', never red 'down' — the oracle dot probes the corridor
	 * summary, the emulator dot reads the ops store's emulator probe; the
	 * counters are live board counts; the clock is IST, mono, ticking.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { corridor } from './corridor.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { t } from '$lib/i18n/strings';
	import { parseBrainHealth } from '$lib/gods-eye/city-api';

	let clock = $state('--:--:--');
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	let decider = $state<'ok' | 'degraded' | 'loading' | 'down'>('down');
	let deciderNote = $state<string | null>(null);
	let gemma = $state<'ok' | 'down'>('down');
	let healthTimer: ReturnType<typeof setInterval> | null = null;

	const liveEvents = $derived(
		ops.events.filter((e) => e.source !== 'offline-fixture' && e.state === 'active').length,
	);
	const pendingEvents = $derived(
		ops.events.filter((e) => e.source !== 'offline-fixture' && e.state === 'planned').length,
	);

	async function pollBrainHealth() {
		if (document.hidden) return;
		try {
			const res = await fetch('/api/convoy/health', { signal: AbortSignal.timeout(6000) });
			if (!res.ok) throw new Error(String(res.status));
			const h = parseBrainHealth(await res.json());
			decider =
				h.decider === 'loading'
					? 'loading'
					: h.decider === 'connected'
						? h.deciderDegraded
							? 'degraded'
							: 'ok'
						: 'down';
			deciderNote =
				decider === 'loading'
					? 'decider weights loading — the heuristic floor answers meanwhile'
					: h.deciderDegradedReason ?? (h.p95Ms != null ? `p95 ${Math.round(h.p95Ms)} ms` : null);
			gemma = h.gemma === 'connected' ? 'ok' : 'down';
		} catch {
			decider = 'down';
			deciderNote = 'brain unreachable';
			gemma = 'down';
		}
	}

	function tickClock() {
		clock = new Intl.DateTimeFormat('en-GB', {
			timeZone: 'Asia/Kolkata',
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit',
			hour12: false,
		}).format(new Date());
	}

	onMount(() => {
		tickClock();
		clockTimer = setInterval(tickClock, 1000);
		void pollBrainHealth();
		healthTimer = setInterval(() => void pollBrainHealth(), 15000);
		corridor.start();
	});

	onDestroy(() => {
		if (clockTimer) clearInterval(clockTimer);
		if (healthTimer) clearInterval(healthTimer);
		corridor.stop();
	});
</script>

<div class="strip" data-template="status-strip">
	<div class="brand">
		<span class="brand-tile" aria-hidden="true">
			<svg viewBox="0 0 24 24" width="22" height="22">
				<path
					d="M12 2 L20 5 V12 C20 17 16.4 20.6 12 22 C7.6 20.6 4 17 4 12 V5 Z"
					fill="none"
					stroke="var(--v-act)"
					stroke-width="1.6"
				/>
				<path d="M8 12.5 L11 15.5 L16.5 9" fill="none" stroke="var(--v-act-2)" stroke-width="1.8" />
			</svg>
		</span>
		<span class="brand-text">
			<strong class="brand-title">{t(ops.lang, 'nav_dashboard')}</strong>
			<span class="brand-sub">{t(ops.lang, 'tpl_strip_sub')}</span>
		</span>
	</div>

	<div class="dots" role="status" aria-label="stack health">
		<span
			class="dot-row"
			class:ok={decider === 'ok'}
			class:degraded={decider === 'degraded' || decider === 'loading'}
			class:down={decider === 'down'}
			data-decider={decider}
			title={deciderNote ?? undefined}
		>
			<i class="dot"></i><span class="mono">DECIDER{decider === 'loading' ? ' · LOADING' : decider === 'degraded' ? ' · DEGRADED' : ''}</span>
		</span>
		<span class="dot-row" class:ok={gemma === 'ok'} class:down={gemma === 'down'}>
			<i class="dot"></i><span class="mono">GEMMA</span>
		</span>
		<span class="dot-row" class:ok={corridor.status === 'ok'} class:down={corridor.status === 'down'}>
			<i class="dot"></i><span class="mono">ORACLE</span>
		</span>
		<span class="dot-row" class:ok={ops.emulator.online} class:down={!ops.emulator.online}>
			<i class="dot"></i><span class="mono">EMULATOR</span>
		</span>
	</div>

	<div class="counts">
		<span class="count">
			<span class="count-label mono">ACTIVE</span>
			<span class="count-value mono">{liveEvents}</span>
		</span>
		<span class="count">
			<span class="count-label mono">PLANNED</span>
			<span class="count-value mono">{pendingEvents}</span>
		</span>
		{#if ops.band}
			<BandChip band={ops.band} size="sm" />
		{/if}
	</div>

	<div class="clock mono" title="India Standard Time">
		{clock} <span class="ist">IST</span>
		<i class="live-dot" aria-hidden="true"></i>
	</div>
</div>

<style>
	.strip {
		display: flex;
		align-items: center;
		gap: var(--v-space-4);
		padding: var(--v-space-1) var(--v-space-4);
		background: linear-gradient(90deg, var(--v-surface-sink), var(--v-surface-1), var(--v-surface-sink));
		border-bottom: 1px solid var(--v-line-1);
		min-height: 40px;
	}

	.brand {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		min-width: 0;
	}

	.brand-tile {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: var(--v-radius-btn);
		border: 1px solid var(--v-glass-border);
		background: var(--v-act-veil);
		flex-shrink: 0;
	}

	.brand-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.brand-title {
		font-family: var(--v-font-display);
		font-size: var(--v-text-sm);
		font-weight: 700;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--v-ink-1);
		white-space: nowrap;
	}

	.brand-sub {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.dots {
		display: flex;
		gap: var(--v-space-3);
		margin-left: auto;
	}

	.dot-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
	}

	.dot {
		width: 7px;
		height: 7px;
		border-radius: var(--v-radius-pill);
		background: var(--v-ink-4);
		flex-shrink: 0;
	}

	.dot-row.ok .dot {
		background: var(--v-band-green);
		box-shadow: 0 0 8px var(--v-band-green);
		animation: dot-breathe var(--v-dur-breathe) var(--v-ease) infinite;
	}

	.dot-row.degraded .dot {
		background: var(--v-band-amber);
		box-shadow: 0 0 8px var(--v-band-amber);
	}

	.dot-row.down .dot {
		background: var(--v-band-red);
	}

	@keyframes dot-breathe {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.55;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.dot-row.ok .dot {
			animation: none;
		}
	}

	.counts {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
	}

	.count {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-1);
	}

	.count-label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
	}

	.count-value {
		font-size: var(--v-text-md);
		font-weight: 700;
		color: var(--v-ink-1);
		font-variant-numeric: tabular-nums;
	}

	.clock {
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
		font-variant-numeric: tabular-nums;
	}

	.ist {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
	}

	.live-dot {
		width: 7px;
		height: 7px;
		border-radius: var(--v-radius-pill);
		background: var(--v-band-green);
		margin-left: var(--v-space-1);
		animation: dot-breathe var(--v-dur-breathe) var(--v-ease) infinite;
	}

	@media (prefers-reduced-motion: reduce) {
		.live-dot {
			animation: none;
		}
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	@media (max-width: 1100px) {
		.dots {
			display: none;
		}
	}
</style>
