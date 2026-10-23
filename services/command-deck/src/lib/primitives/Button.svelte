<script lang="ts">
	/**
	 * Button — control-height actions on tokens.
	 *
	 * primary (human-act orange) is RESERVED for human-commit actions and
	 * MUST NOT render enabled without a named-actor prop: passing no `actor`
	 * yields a visibly disabled button with the officer hint — the C6 gate
	 * at primitive level. danger-outline (band-red hairline) is for
	 * reject/abort. No navigation is ever orange. The lock hint and the busy
	 * label read in the console's language (the shell's, unless `lang` is
	 * passed).
	 */
	import { t } from '$lib/i18n/strings';
	import { commandBar } from '$lib/shell/command-bar.svelte';

	type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger-outline';
	type ButtonSize = 'sm' | 'm' | 'citizen';

	interface Props {
		variant?: ButtonVariant;
		size?: ButtonSize;
		busy?: boolean;
		disabled?: boolean;
		/** Named officer/operator identity; required to enable primary. */
		actor?: string | null;
		type?: 'button' | 'submit';
		title?: string;
		/** UI language for the hint / busy label (defaults to the shell's). */
		lang?: string | null;
		onclick?: (e: MouseEvent) => void;
		children?: import('svelte').Snippet;
	}

	let {
		variant = 'secondary',
		size = 'm',
		busy = false,
		disabled = false,
		actor = null,
		type = 'button',
		title,
		lang = null,
		onclick,
		children
	}: Props = $props();

	const uiLang = $derived(lang ?? commandBar.lang);
	/** The gate: a human-commit button cannot enable without an actor. */
	const commitLocked = $derived(variant === 'primary' && !actor);
	const reallyDisabled = $derived(disabled || busy || commitLocked);
	const hint = $derived(commitLocked ? t(uiLang, 'officer_hint') : title);
</script>

<button
	{type}
		class="btn {variant} {size}"
		class:busy
		disabled={reallyDisabled}
		aria-disabled={reallyDisabled}
		aria-busy={busy || undefined}
		title={hint}
		{onclick}
		data-prim="button"
>
	<span class="label" class:busy-hidden={busy}>{@render children?.()}</span>
	{#if busy}<span class="busy-label mono">{t(uiLang, 'btn_busy')}…</span>{/if}
</button>

<style>
	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: var(--v-space-1);
		font-family: var(--v-font-ui);
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		border-radius: var(--v-radius-btn);
		border: 1px solid var(--v-line-2);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
		cursor: pointer;
		transition:
			background var(--v-dur-out) var(--v-ease),
			border-color var(--v-dur-out) var(--v-ease),
			color var(--v-dur-out) var(--v-ease);
	}

	.btn:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.sm {
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
	}

	.m {
		height: var(--v-ctl-md);
		padding: 0 var(--v-space-3);
	}

	.citizen {
		height: var(--v-ctl-touch);
		padding: 0 var(--v-space-4);
		font-size: var(--v-text-md);
	}

	/* primary = a human must act — orange edge/text, act veil on hover,
	 * never a large flat fill. */
	.primary {
		border-color: var(--v-act);
		color: var(--v-act-text);
		background: var(--v-act-veil);
	}

	.primary:not(:disabled):hover {
		border-color: var(--v-act-2);
		background: color-mix(in srgb, var(--v-act) 22%, transparent);
	}

	/* danger-outline — reject/abort. */
	.danger-outline {
		border-color: var(--v-band-red);
		color: var(--v-band-red);
		background: transparent;
	}

	.danger-outline:not(:disabled):hover {
		background: color-mix(in srgb, var(--v-band-red) 12%, transparent);
	}

	.ghost {
		border-color: transparent;
		background: transparent;
		color: var(--v-ink-2);
	}

	.ghost:not(:disabled):hover {
		border-color: var(--v-line-2);
		color: var(--v-ink-1);
	}

	.secondary:not(:disabled):hover {
		border-color: var(--v-line-select);
	}

	.busy {
		color: var(--v-ink-3);
	}

	.label {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
	}

	.busy-hidden {
		visibility: hidden;
	}

	.busy-label {
		position: absolute;
	}

	.mono {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
	}

	.btn {
		position: relative;
	}
</style>
