<script module lang="ts">
	/**
	 * One shared 1 s clock for every mounted panel's 'updated Ns ago'
	 * label: a single interval, started by the first panel that shows a
	 * status and stopped with the last one (the label used to freeze at the
	 * age it had when the data landed — the clock was never ticked).
	 */
	const clock = $state({ now: Date.now() });
	let clockUsers = 0;
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	function useClock(): () => void {
		clockUsers += 1;
		if (!clockTimer) {
			clock.now = Date.now();
			clockTimer = setInterval(() => (clock.now = Date.now()), 1000);
		}
		return () => {
			clockUsers -= 1;
			if (clockUsers <= 0 && clockTimer) {
				clearInterval(clockTimer);
				clockTimer = null;
				clockUsers = 0;
			}
		};
	}

	/** Compact age: 42s · 4m · 3h · 2d. */
	function fmtAge(secs: number): string {
		if (secs < 120) return `${secs}s`;
		if (secs < 7200) return `${Math.floor(secs / 60)}m`;
		if (secs < 172_800) return `${Math.floor(secs / 3600)}h`;
		return `${Math.floor(secs / 86_400)}d`;
	}
</script>

<script lang="ts">
	/**
	 * Panel — the operator surface unit. Matte, hairline-separated; renders
	 * ONLY what it is given (per-panel degradation stays the panel's
	 * concern — status is optional and honest when absent).
	 *
	 * Freshness: the age label ticks on the shared clock, and a status
	 * whose last update is older than 3× the panel's poll interval renders
	 * STALE (dashed) whatever state the caller passed — a 'live' badge can
	 * no longer outlive the feed behind it. The state chip reads in the
	 * panel's language (the raw state stays in data-state).
	 */
	import { t } from '$lib/i18n/strings';

	type PanelStatusState = 'live' | 'mixed' | 'offline' | 'not-deployed' | 'stale';

	interface Props {
		title?: string;
		sub?: string;
		status?: { state: PanelStatusState; updatedAt: number } | null;
		/**
		 * The feed's poll interval (ms). The status turns STALE after 3× this
		 * without an update. Defaults to 10 s — the slowest ops-wall cadence.
		 */
		pollMs?: number;
		pad?: 'none' | 'normal' | 'roomy';
		scroll?: boolean;
		/**
		 * Fill the parent's height: the panel stretches and its body becomes
		 * a flex column, so one child (flex: 1) can be the scroller — the
		 * wall's fixed-height audit band uses this.
		 */
		fill?: boolean;
		variant?: 'flat' | 'raised' | 'sink' | 'glass';
		lang?: string;
		children?: import('svelte').Snippet;
		'header-right'?: import('svelte').Snippet;
		footer?: import('svelte').Snippet;
	}

	let {
		title,
		sub,
		status = null,
		pollMs = 10_000,
		pad = 'normal',
		scroll = false,
		fill = false,
		variant = 'flat',
		lang = 'en',
		children,
		'header-right': headerRight,
		footer
	}: Props = $props();

	// Subscribe to the shared clock only while a status is shown (a derived
	// boolean, so a fresh status object per poll does not re-subscribe).
	let hasStatus = $derived(!!status);
	$effect(() => {
		if (!hasStatus) return;
		return useClock();
	});

	let ageSecs = $derived(
		status && status.updatedAt ? Math.max(0, Math.floor((clock.now - status.updatedAt) / 1000)) : null,
	);
	let ageLabel = $derived(
		ageSecs == null ? t(lang, 'never_updated') : `${t(lang, 'updated_ago')} ${fmtAge(ageSecs)}`,
	);
	/** Older than 3× the poll interval: the feed has stopped answering. */
	let overdue = $derived(ageSecs != null && ageSecs * 1000 > 3 * pollMs);
	let shownState = $derived<PanelStatusState | null>(
		status ? (overdue && status.state !== 'not-deployed' ? 'stale' : status.state) : null,
	);
</script>

<section
	class="panel"
	class:raised={variant === 'raised'}
	class:sink={variant === 'sink'}
	class:glass={variant === 'glass'}
	class:fill
	data-prim="panel"
>
	{#if variant === 'glass'}<span class="sweep" aria-hidden="true"></span>{/if}
	{#if title || status || headerRight}
		<header class="head">
			<div class="head-text">
				{#if title}<h2 class="title">{title}</h2>{/if}
				{#if sub}<p class="sub">{sub}</p>{/if}
			</div>
			<div class="head-side">
				{#if status && shownState}
					<span
						class="status status-{shownState}"
						class:stale-dashed={shownState === 'not-deployed' || shownState === 'stale'}
						data-overdue={overdue || undefined}
						data-state={shownState}
						title={overdue ? `${ageLabel} — no update for over 3× the ${Math.round(pollMs / 1000)}s poll` : ageLabel}
					>
						{t(lang, `panel_state_${shownState}`)}
					</span>
					<span class="age" class:overdue data-age-sec={ageSecs ?? ''}>{ageLabel}</span>
				{/if}
				{#if headerRight}{@render headerRight()}{/if}
			</div>
		</header>
	{/if}

	<div class="body" class:scroll class:fill-body={fill} class:pad-none={pad === 'none'} class:pad-roomy={pad === 'roomy'}>
		{@render children?.()}
	</div>

	{#if footer}
		<footer class="foot">
			{@render footer()}
		</footer>
	{/if}
</section>

<style>
	.panel {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		min-height: 0;
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
	}

	.fill {
		height: 100%;
	}

	.fill-body {
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.raised {
		background: var(--v-surface-2);
		border-color: var(--v-line-2);
	}

	.sink {
		background: var(--v-surface-sink);
	}

	/* The side (status, age, header-right badges) wraps BELOW the title
	 * when the panel is narrow, instead of crushing the title column to a
	 * word per line under an overlapping status pill. */
	.head {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--v-space-2);
		padding: var(--v-pad-panel);
		padding-bottom: 0;
	}

	.head-text {
		flex: 1 1 16ch;
		min-width: min(16ch, 100%);
	}

	/* glass instruments (the restored template idiom): floating surfaces
	 * over the map get backdrop blur + a saffron sweep hairline on the top
	 * edge; structural panels stay matte + hairline. */
	.glass {
		position: relative;
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		border: 1px solid var(--v-glass-border);
		overflow: hidden;
	}

	.glass .title {
		font-family: var(--v-font-display);
		color: var(--v-ink-1);
		letter-spacing: 0.14em;
	}

	.sweep {
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		background: linear-gradient(
			90deg,
			transparent,
			var(--v-act) 30%,
			var(--v-act-2) 50%,
			var(--v-act) 70%,
			transparent
		);
		background-size: 220% 100%;
		animation: panel-sweep var(--v-dur-sweep, 3.5s) linear infinite;
		pointer-events: none;
	}

	@keyframes panel-sweep {
		from {
			background-position: 180% 0;
		}
		to {
			background-position: -80% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.sweep {
			animation: none;
			background-size: 100% 100%;
		}
	}

	.title {
		margin: 0;
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
	}

	.sub {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.head-side {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: flex-end;
		gap: var(--v-space-2);
		max-width: 100%;
	}

	.status {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		padding: 1px var(--v-space-2);
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-line-2);
		color: var(--v-ink-2);
	}

	.status-live {
		border-color: var(--v-band-green);
		color: var(--v-band-green);
	}

	.status-mixed {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.status-offline,
	.status-not-deployed,
	.status-stale {
		border-color: var(--v-line-2);
		color: var(--v-ink-3);
	}

	.stale-dashed {
		border-style: dashed;
	}

	.age {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		font-variant-numeric: tabular-nums;
	}

	.age.overdue {
		color: var(--v-band-amber);
	}

	.body {
		flex: 1;
		min-height: 0;
		padding: var(--v-pad-panel);
	}

	.pad-none {
		padding: 0;
	}

	.pad-roomy {
		padding: var(--v-space-4);
	}

	.scroll {
		overflow-y: auto;
	}

	.foot {
		border-top: 1px solid var(--v-line-1);
		padding: var(--v-space-2) var(--v-pad-panel);
	}
</style>
