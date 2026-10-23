<script lang="ts">
	/**
	 * ThreatLadder — the template's 5-rung posture ladder (nominal →
	 * critical), colored ONLY through existing token slots (band-green,
	 * data, band-amber, act, band-red). The live rung is the city band
	 * mapped onto the ladder; no band yet = idle ladder, honestly labelled.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';

	type Rung = {
		id: 'nominal' | 'guarded' | 'moderate' | 'elevated' | 'critical';
		token: string;
		note: string;
	};

	const RUNGS: ReadonlyArray<Rung> = [
		{ id: 'nominal', token: '--v-band-green', note: 'flow nominal' },
		{ id: 'guarded', token: '--v-data', note: 'watch corridors' },
		{ id: 'moderate', token: '--v-band-amber', note: 'bands holding' },
		{ id: 'elevated', token: '--v-act', note: 'act now' },
		{ id: 'critical', token: '--v-band-red', note: 'all agencies' },
	];

	const bandRung = $derived<null | Rung['id']>(
		ops.band === 'GREEN'
			? 'nominal'
			: ops.band === 'AMBER'
				? 'moderate'
				: ops.band === 'RED'
					? 'elevated'
					: ops.band === 'BLACK'
						? 'critical'
						: null,
	);
</script>

<Panel variant="glass" title={t(ops.lang, 'tpl_threat')} lang={ops.lang}>
	<ul class="ladder">
		{#each RUNGS as rung (rung.id)}
			{@const active = rung.id === bandRung}
			<li
				class="rung"
				class:active
				style={`--rung: var(${rung.token})`}
				aria-current={active ? 'true' : undefined}
			>
				<i class="edge" aria-hidden="true"></i>
				<span class="rung-name">{rung.id.toUpperCase()}</span>
				<span class="rung-note">{rung.note}</span>
				{#if active}<i class="pulse" aria-hidden="true"></i>{/if}
			</li>
		{/each}
	</ul>
	{#if !bandRung}
		<p class="idle">{t(ops.lang, 'tpl_threat_none')}</p>
	{/if}
</Panel>

<style>
	.ladder {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.rung {
		position: relative;
		display: grid;
		grid-template-columns: 3px 1fr auto;
		align-items: center;
		gap: var(--v-space-2);
		padding: var(--v-space-1) var(--v-space-2);
		border-radius: var(--v-radius-btn);
		border: 1px solid transparent;
	}

	.edge {
		width: 3px;
		height: 18px;
		border-radius: 2px;
		background: var(--rung);
		opacity: 0.5;
	}

	.rung-name {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		font-weight: 700;
		letter-spacing: var(--v-tracking-status);
		color: var(--rung);
	}

	.rung-note {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.rung.active {
		border-color: var(--rung);
		background: color-mix(in srgb, var(--rung) 10%, transparent);
		box-shadow: 0 0 12px color-mix(in srgb, var(--rung) 22%, transparent);
	}

	.rung.active .edge {
		opacity: 1;
	}

	.pulse {
		position: absolute;
		right: var(--v-space-2);
		width: 6px;
		height: 6px;
		border-radius: var(--v-radius-pill);
		background: var(--rung);
		animation: rung-pulse var(--v-dur-pulse) steps(2, jump-none) infinite;
	}

	@keyframes rung-pulse {
		0% {
			opacity: 1;
		}
		100% {
			opacity: 0.25;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.pulse {
			animation: none;
		}
	}

	.idle {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}
</style>
