<script lang="ts">
	/**
	 * ToolDock — the template's floating bottom tool dock over the canvas:
	 * AI TRACE and DOSSIER toggles, the situation-wall cross-link, and the
	 * city anchor readout (the template's coordinates pill, honest and
	 * static: the canvas camera moves, the city does not).
	 */
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';

	let {
		traceOpen = false,
		onToggleTrace = () => {},
		dossierOpen = false,
		onToggleDossier = () => {}
	}: {
		traceOpen?: boolean;
		onToggleTrace?: () => void;
		dossierOpen?: boolean;
		onToggleDossier?: () => void;
	} = $props();
</script>

<div class="dock" data-template="tool-dock">
	<button
		type="button"
		class="dock-btn"
		class:on={traceOpen}
		onclick={onToggleTrace}
		aria-pressed={traceOpen}
	>
		⚡ {t(ops.lang, 'tpl_trace')}
	</button>
	<button
		type="button"
		class="dock-btn"
		class:on={dossierOpen}
		onclick={onToggleDossier}
		aria-pressed={dossierOpen}
	>
		▣ {t(ops.lang, 'tpl_dossier')}
	</button>

	<i class="sep" aria-hidden="true"></i>

	<a class="dock-btn link" href="/ops" title={t(ops.lang, 'tpl_wall')}>⬢ {t(ops.lang, 'tpl_wall')}</a>

	<i class="sep" aria-hidden="true"></i>

	<span class="coords mono" title="Ahmedabad city anchor">
		23.0225°N · 72.5714°E · AMD
	</span>
</div>

<style>
	.dock {
		position: absolute;
		left: 50%;
		transform: translateX(-50%);
		bottom: var(--v-space-3);
		display: flex;
		align-items: center;
		gap: var(--v-space-1);
		padding: var(--v-space-1) var(--v-space-2);
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		border: 1px solid var(--v-glass-border);
		border-radius: var(--v-radius-pill);
		box-shadow: 0 8px 28px var(--v-veil);
		z-index: 25;
	}

	.dock-btn {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		height: var(--v-ctl-md);
		padding: 0 var(--v-space-3);
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-line-2);
		background: transparent;
		color: var(--v-ink-2);
		font-family: var(--v-font-ui);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		cursor: pointer;
		text-decoration: none;
		transition:
			color var(--v-dur-out) var(--v-ease),
			border-color var(--v-dur-out) var(--v-ease),
			box-shadow var(--v-dur-out) var(--v-ease);
	}

	.dock-btn:hover {
		color: var(--v-ink-1);
		border-color: var(--v-act);
	}

	.dock-btn.on {
		color: var(--v-act-text);
		border-color: var(--v-act);
		box-shadow: 0 0 10px var(--v-glow-act);
		background: var(--v-act-veil);
	}

	.sep {
		width: 1px;
		height: 16px;
		background: var(--v-glass-border);
	}

	.coords {
		padding: 0 var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		letter-spacing: 0.06em;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	@media (max-width: 900px) {
		.coords {
			display: none;
		}
	}
</style>
