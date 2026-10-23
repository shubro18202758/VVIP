<script lang="ts">
	/**
	 * OperatorSignIn — the command bar's duty-officer sign-in, shown ONLY
	 * when the deck runs with the opt-in operator gate (VEER_OPERATOR_PIN).
	 * Signed out: an "Officer sign-in" control opening a name + PIN dialog.
	 * Signed in: who is on duty, and sign out. Gate off: renders nothing.
	 * After a sign-in or sign-out the page reloads so every panel re-reads
	 * its feeds under the new session (no stale 403s left on screen); the
	 * reload keeps the URL hash, so a shared City Command view restores.
	 * `inline` renders the form itself (no dialog) — City Command's sign-in
	 * panel embeds it.
	 */
	import { onMount } from 'svelte';
	import { t } from '$lib/i18n/strings';
	import Button from '$lib/primitives/Button.svelte';
	import Modal from '$lib/primitives/Modal.svelte';
	import { operatorSession } from './session.svelte';

	let { lang, inline = false }: { lang: string; inline?: boolean } = $props();

	let open = $state(false);
	let officer = $state('');
	let pin = $state('');

	onMount(() => {
		void operatorSession.refresh();
	});

	async function submit() {
		if (await operatorSession.signIn(officer.trim(), pin)) {
			pin = '';
			open = false;
			location.reload();
		}
	}

	async function signOut() {
		await operatorSession.signOut();
		location.reload();
	}
</script>

{#snippet signinForm(autofocus: boolean)}
	<form
		class="signin"
		data-shell="operator-signin-form"
		onsubmit={(e) => {
			e.preventDefault();
			void submit();
		}}
	>
		<p class="note">{t(lang, 'signin_note')}</p>
		<label>
			<span>{t(lang, 'signin_officer')}</span>
			<!-- svelte-ignore a11y_autofocus -->
			<input type="text" bind:value={officer} autocomplete="name" minlength="2" maxlength="64" required {autofocus} data-testid="signin-officer" />
		</label>
		<label>
			<span>{t(lang, 'signin_pin')}</span>
			<input type="password" bind:value={pin} autocomplete="current-password" inputmode="numeric" required data-testid="signin-pin" />
		</label>
		{#if operatorSession.error}
			<p class="error" role="alert">
				{operatorSession.error === 'throttled' ? t(lang, 'signin_throttled') : t(lang, 'signin_refused')}
			</p>
		{/if}
		<Button type="submit" variant="secondary" size="sm" busy={operatorSession.busy} disabled={officer.trim().length < 2 || !pin} {lang}>
			{t(lang, 'signin_submit')}
		</Button>
	</form>
{/snippet}

{#if inline}
	{#if operatorSession.gate && !operatorSession.officer}
		{@render signinForm(false)}
	{/if}
{:else}
	{#if operatorSession.gate}
		{#if operatorSession.officer}
			<span class="on-duty" data-shell="operator-session" data-officer={operatorSession.officer}>
				<span class="who mono">{t(lang, 'signin_on_duty')} · {operatorSession.officer}</span>
				<button type="button" class="out" onclick={() => void signOut()}>{t(lang, 'signin_signout')}</button>
			</span>
		{:else}
			<button type="button" class="open" data-shell="operator-signin" onclick={() => (open = true)}>
				{t(lang, 'signin_open')}
			</button>
		{/if}
	{/if}

	<Modal {open} title={t(lang, 'signin_title')} width="380px" onClose={() => (open = false)}>
		{@render signinForm(true)}
	</Modal>
{/if}

<style>
	.open,
	.out {
		background: transparent;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-2);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		cursor: pointer;
	}

	.open {
		border-color: var(--v-act-text);
		color: var(--v-act-text);
	}

	.open:focus-visible,
	.out:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.on-duty {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
	}

	.who {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.signin {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-3);
	}

	.note {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	label {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	input {
		height: var(--v-ctl-lg);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		background: var(--v-surface-1);
		color: var(--v-ink-1);
		padding: 0 var(--v-space-3);
		font: inherit;
	}

	input:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.error {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-band-red);
	}
</style>
