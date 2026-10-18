<script lang="ts">
	/**
	 * Panel — the operator surface unit. Matte, hairline-separated; renders
	 * ONLY what it is given (per-panel degradation stays the panel's
	 * concern — status is optional and honest when absent).
	 */
	import { t } from '$lib/i18n/strings';

	type PanelStatusState = 'live' | 'mixed' | 'offline' | 'not-deployed' | 'stale';

	interface Props {
		title?: string;
		sub?: string;
		status?: { state: PanelStatusState; updatedAt: number } | null;
		pad?: 'none' | 'normal' | 'roomy';
		scroll?: boolean;
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
		pad = 'normal',
		scroll = false,
		variant = 'flat',
		lang = 'en',
		children,
		'header-right': headerRight,
		footer
	}: Props = $props();

	const now = $state({ tick: Date.now() });
	let ageLabel = $derived.by(() => {
		if (!status || !status.updatedAt) return t(lang, 'never_updated');
		const secs = Math.max(0, Math.round((now.tick - status.updatedAt) / 1000));
		return `${t(lang, 'updated_ago')} ${secs}s`;
	});
</script>

<section
	class="panel"
	class:raised={variant === 'raised'}
	class:sink={variant === 'sink'}
	class:glass={variant === 'glass'}
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
				{#if status}
					<span
						class="status status-{status.state}"
						class:stale-dashed={status.state === 'not-deployed' || status.state === 'stale'}
						title={ageLabel}
					>
						{status.state}
					</span>
					<span class="age">{ageLabel}</span>
				{/if}
				{#if headerRight}{@render headerRight()}{/if}
			</div>
		</header>
	{/if}

	<div class="body" class:scroll class:pad-none={pad === 'none'} class:pad-roomy={pad === 'roomy'}>
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

	.raised {
		background: var(--v-surface-2);
		border-color: var(--v-line-2);
	}

	.sink {
		background: var(--v-surface-sink);
	}

	.head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--v-space-2);
		padding: var(--v-pad-panel);
		padding-bottom: 0;
	}

	.head-text {
		min-width: 0;
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
		align-items: center;
		gap: var(--v-space-2);
		flex-shrink: 0;
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
