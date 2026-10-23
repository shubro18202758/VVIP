<script lang="ts">
	/**
	 * CitizenShell — public chrome (daylight tokens apply via the (public)
	 * layout's data-theme, and the server already renders the page in
	 * daylight). Its own minimal CitizenBar — brand, plain-language posture,
	 * language switch — and the two public views as tabs; nothing operator
	 * side (no band code, no operator search, no share, no deck link).
	 *
	 * Page-owned bar fields are cleared whenever the route changes (before
	 * the next page mounts and assigns its own), and <html lang> follows the
	 * chosen language so screen readers pronounce HI/GU correctly. A choice
	 * made on the language switch is persisted in the veer_lang cookie (C-M);
	 * a page's own default (Gujarati on /driver) never is.
	 */
	import { page } from '$app/stores';
	import { onNavigate } from '$app/navigation';
	import { t } from '$lib/i18n/strings';
	import '$lib/i18n/packs';
	import { writeLangCookie } from '$lib/i18n/lang-cookie';
	import CitizenBar from './CitizenBar.svelte';
	import Toaster from './Toaster.svelte';
	import { commandBar, resetCommandBar } from './command-bar.svelte';
	import { PUBLIC_NAV } from './nav';

	let { children } = $props();

	const pathname = $derived($page.url.pathname);

	onNavigate((nav) => {
		if (nav.from?.route.id !== nav.to?.route.id) resetCommandBar();
	});

	$effect(() => {
		document.documentElement.lang = commandBar.lang;
	});

	/** The citizen's own choice: persisted, then handed to the page that owns the language. */
	function pickLang(code: string) {
		writeLangCookie(code);
		if (commandBar.onLangChange) commandBar.onLangChange(code);
		else commandBar.lang = code;
	}
</script>

<div class="shell" data-shell="citizen">
	<CitizenBar lang={commandBar.lang} tier={commandBar.tier} onLangChange={pickLang} />
	<nav class="tabs" aria-label={t(commandBar.lang, 'nav_section_public')}>
		{#each PUBLIC_NAV as item (item.href)}
			<a
				href={item.href}
				class:active={pathname === item.href || pathname.startsWith(item.href + '/')}
			>
				<span class="glyph" aria-hidden="true">{item.glyph}</span>
				{t(commandBar.lang, item.key)}
			</a>
		{/each}
	</nav>
	<main class="main">
		{@render children?.()}
	</main>
	<Toaster />
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}

	.tabs {
		display: flex;
		gap: var(--v-space-2);
		padding: var(--v-space-2) var(--v-space-4);
		background: var(--v-paper);
		border-bottom: 1px solid var(--v-line-1);
		flex-shrink: 0;
		overflow-x: auto;
	}

	.tabs a {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
		height: var(--v-ctl-touch);
		padding: 0 var(--v-space-4);
		border: 1px solid transparent;
		border-radius: var(--v-radius-pill);
		color: var(--v-ink-2);
		text-decoration: none;
		font-size: var(--v-text-sm);
		white-space: nowrap;
	}

	.tabs a.active {
		border-color: var(--v-line-2);
		background: var(--v-surface-1);
		color: var(--v-ink-1);
	}

	.tabs a:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.glyph {
		flex-shrink: 0;
	}

	.main {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}
</style>
