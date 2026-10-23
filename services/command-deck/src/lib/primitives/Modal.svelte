<script lang="ts">
	/**
	 * Modal — surface-3 dialog on an 8% canvas veil (no blur), 14px radius,
	 * hairline. Focus trap + Escape + focus return. NEVER for fail-closed
	 * signal refusals — those render inline 'held at baseline' (motion
	 * doctrine); this component is for evidence detail and confirmations.
	 *
	 * Opening focus goes to the FIRST field (an autofocus control, else the
	 * first input, else the dialog) — and never moves once focus is already
	 * inside the dialog: a fast typist or an autofill writing the officer's
	 * name in the first frame must not lose characters to a late refocus.
	 */
	import { t } from '$lib/i18n/strings';
	import { commandBar } from '$lib/shell/command-bar.svelte';

	interface Props {
		open: boolean;
		title: string;
		width?: string;
		onClose: () => void;
		children?: import('svelte').Snippet;
	}

	let { open, title, width = '560px', onClose, children }: Props = $props();

	let dialogEl: HTMLElement | undefined = $state();
	let restoreFocus: HTMLElement | null = null;

	const FOCUSABLE =
		'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

	function trap(event: KeyboardEvent) {
		if (!open || !dialogEl) return;
		if (event.key === 'Escape') {
			event.stopPropagation();
			onClose();
			return;
		}
		if (event.key !== 'Tab') return;
		const nodes = Array.from(dialogEl.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
			(el) => el.offsetParent !== null
		);
		if (nodes.length === 0) {
			event.preventDefault();
			dialogEl.focus();
			return;
		}
		const first = nodes[0];
		const last = nodes[nodes.length - 1];
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault();
			first.focus();
		}
	}

	$effect(() => {
		if (!open) return;
		restoreFocus = (document.activeElement as HTMLElement) ?? null;
		const raf = requestAnimationFrame(() => {
			if (!dialogEl || dialogEl.contains(document.activeElement)) return;
			const first =
				dialogEl.querySelector<HTMLElement>('[autofocus]') ??
				dialogEl.querySelector<HTMLElement>('input:not([type="hidden"]):not([disabled]), select, textarea');
			(first ?? dialogEl).focus();
		});
		return () => {
			cancelAnimationFrame(raf);
			restoreFocus?.focus?.();
			restoreFocus = null;
		};
	});
</script>

<svelte:window onkeydown={trap} />

{#if open}
	<div class="veil" aria-hidden="true"></div>
	<div class="position">
		<div
			class="modal"
			role="dialog"
			aria-modal="true"
			aria-label={title}
			tabindex="-1"
			bind:this={dialogEl}
			style:max-width={width}
			data-prim="modal"
		>
			<header class="head">
				<h2 class="title">{title}</h2>
				<button class="close" type="button" onclick={onClose} aria-label={t(commandBar.lang, 'btn_close')}>
					×
				</button>
			</header>
			<div class="body">
				{@render children?.()}
			</div>
		</div>
	</div>
{/if}

<style>
	.veil {
		position: fixed;
		inset: 0;
		z-index: 90;
		background: var(--v-veil);
	}

	.position {
		position: fixed;
		inset: 0;
		z-index: 91;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--v-space-4);
		pointer-events: none;
	}

	.modal {
		pointer-events: auto;
		display: flex;
		flex-direction: column;
		width: 100%;
		max-height: min(80vh, 720px);
		background: var(--v-surface-3);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-modal);
		overflow: hidden;
	}

	.modal:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--v-space-2);
		padding: var(--v-space-3) var(--v-space-4);
		border-bottom: 1px solid var(--v-line-1);
	}

	.title {
		margin: 0;
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-1);
	}

	.close {
		background: transparent;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		color: var(--v-ink-2);
		width: var(--v-ctl-sm);
		height: var(--v-ctl-sm);
		display: inline-flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		font-size: var(--v-text-md);
		line-height: 1;
	}

	.close:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
	}

	.close:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.body {
		padding: var(--v-space-4);
		overflow-y: auto;
		color: var(--v-ink-2);
		font-size: var(--v-text-base);
	}
</style>
