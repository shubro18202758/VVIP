<script lang="ts">
	/**
	 * CommandShell — operator chrome: CommandBar (state from the R3 module),
	 * the nav rail (never orange), the page, the Toaster. This shell owns no
	 * data — pages supply state.
	 *
	 * Page-owned bar fields (band, SHARE handler, …) are cleared whenever
	 * the route changes, before the next page mounts and assigns its own —
	 * so /ops' SHARE VIEW never survives onto /comms. (afterNavigate would
	 * be too late: it runs after the incoming page's effects.) <html lang>
	 * follows the chosen language, and every choice the operator makes on
	 * the language switch is persisted in the veer_lang cookie (C-M) — a
	 * sign-in reload, or the next visit, opens in the same language.
	 */
	import { page } from '$app/stores';
	import { onNavigate } from '$app/navigation';
	import { t } from '$lib/i18n/strings';
	import '$lib/i18n/packs';
	import { writeLangCookie } from '$lib/i18n/lang-cookie';
	import CommandBar from './CommandBar.svelte';
	import Toaster from './Toaster.svelte';
	import { commandBar, resetCommandBar } from './command-bar.svelte';
	import { DECK_NAV, PUBLIC_NAV } from './nav';

	let { children } = $props();

	const pathname = $derived($page.url.pathname);

	onNavigate((nav) => {
		if (nav.from?.route.id !== nav.to?.route.id) resetCommandBar();
	});

	$effect(() => {
		document.documentElement.lang = commandBar.lang;
	});

	/** The operator's own choice: persisted, then handed to the page that owns the language. */
	function pickLang(code: string) {
		writeLangCookie(code);
		if (commandBar.onLangChange) commandBar.onLangChange(code);
		else commandBar.lang = code;
	}
</script>

<div class="shell" data-shell="command">
	<CommandBar
		band={commandBar.band}
		bandSource={commandBar.bandSource}
		lang={commandBar.lang}
		onLangChange={pickLang}
		onShare={commandBar.onShare}
	/>
	<div class="frame">
		<nav class="rail" aria-label={t(commandBar.lang, 'nav_section_deck')}>
			<h2 class="rail-label">{t(commandBar.lang, 'nav_section_deck')}</h2>
			<ul>
				{#each DECK_NAV as item (item.href)}
					<li>
						<a
							href={item.href}
							class:active={pathname === item.href || pathname.startsWith(item.href + '/')}
						>
							<span class="glyph" aria-hidden="true">{item.glyph}</span>
							<span class="label">{t(commandBar.lang, item.key)}</span>
						</a>
					</li>
				{/each}
			</ul>
			<h2 class="rail-label">{t(commandBar.lang, 'nav_section_public')}</h2>
			<ul>
				{#each PUBLIC_NAV as item (item.href)}
					<li>
						<a
							href={item.href}
							class:active={pathname === item.href || pathname.startsWith(item.href + '/')}
						>
							<span class="glyph" aria-hidden="true">{item.glyph}</span>
							<span class="label">{t(commandBar.lang, item.key)}</span>
						</a>
					</li>
				{/each}
			</ul>
			<div class="rail-foot">
				<span class="mono">v0.2.0 · regulate, never freeze</span>
			</div>
		</nav>
		<main class="main">
			{@render children?.()}
		</main>
	</div>
	<Toaster />
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}

	.frame {
		display: flex;
		flex: 1;
		min-height: 0;
	}

	.rail {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		width: 200px;
		flex-shrink: 0;
		padding: var(--v-space-3) var(--v-space-2);
		background: var(--v-surface-sink);
		border-right: 1px solid var(--v-line-1);
		overflow-y: auto;
	}

	.rail-label {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.rail ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.rail a {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		padding: var(--v-space-1) var(--v-space-2);
		border-radius: var(--v-radius-btn);
		color: var(--v-ink-3);
		text-decoration: none;
		font-size: var(--v-text-xs);
		border: 1px solid transparent;
		transition:
			color var(--v-dur-out) var(--v-ease),
			border-color var(--v-dur-out) var(--v-ease);
	}

	.rail a:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-1);
	}

	/* active nav: line-select edge, NEVER orange */
	.rail a.active {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
		background: var(--v-surface-2);
	}

	.rail a:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.glyph {
		width: 14px;
		text-align: center;
		flex-shrink: 0;
	}

	.rail-foot {
		margin-top: auto;
		padding-top: var(--v-space-3);
		border-top: 1px solid var(--v-line-1);
	}

	.rail-foot .mono {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.main {
		flex: 1;
		min-width: 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
		overflow: auto;
	}
</style>
