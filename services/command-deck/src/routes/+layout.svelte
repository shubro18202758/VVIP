<script lang="ts">
	import { page } from '$app/stores';
	import { onMount, onDestroy } from 'svelte';
	import { startDataService, stopDataService } from '$lib/api/data-service';
	import ConnectionBanner from '$components/ConnectionBanner.svelte';

	let { children } = $props();

	onMount(() => {
		startDataService();
	});

	onDestroy(() => {
		stopDataService();
	});

	const navItems = [
		{ href: '/', label: 'Dashboard', icon: '◻' },
		{ href: '/map', label: 'Corridor Map', icon: '◈' },
		{ href: '/planning', label: 'Route Planning', icon: '◇' },
		{ href: '/command', label: 'Command Center', icon: '◆' },
		{ href: '/chat', label: 'Agent Console', icon: '◎' },
		{ href: '/simulation', label: 'Simulation', icon: '▣' },
	];
</script>

<div class="app-layout">
	<nav class="sidebar">
		<div class="logo">
			<span class="logo-icon">◉</span>
			VVIP Command Deck
		</div>
		<ul class="nav-links">
			{#each navItems as item}
				<li>
					<a
						href={item.href}
						class:active={$page.url.pathname === item.href}
					>
						<span class="nav-icon">{item.icon}</span>
						{item.label}
					</a>
				</li>
			{/each}
		</ul>
		<div class="sidebar-footer">
			<div class="version">v0.2.0 · Edge Deploy</div>
		</div>
	</nav>
	<main class="content">
		<ConnectionBanner />
		{@render children()}
	</main>
</div>

<style>
	:global(html, body) {
		margin: 0;
		padding: 0;
		height: 100%;
		overflow: hidden;
		font-family: system-ui, -apple-system, sans-serif;
		background: #0f172a;
		color: #f1f5f9;
	}

	:global(*) {
		box-sizing: border-box;
	}

	:global(::-webkit-scrollbar) {
		width: 6px;
	}
	:global(::-webkit-scrollbar-track) {
		background: #0f172a;
	}
	:global(::-webkit-scrollbar-thumb) {
		background: #334155;
		border-radius: 3px;
	}

	.app-layout {
		height: 100vh;
		overflow: hidden;
	}

	.sidebar {
		background: #1e293b;
		padding: 1.5rem 0.75rem;
		border-right: 1px solid #334155;
		display: flex;
		flex-direction: column;
		position: fixed;
		top: 0;
		left: 0;
		bottom: 0;
		width: 220px;
		z-index: 100;
	}

	.logo {
		font-size: 1rem;
		font-weight: 700;
		margin-bottom: 2rem;
		color: #3b82f6;
		padding: 0 0.5rem;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.logo-icon {
		font-size: 1.25rem;
	}

	.nav-links {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		flex: 1;
	}

	.nav-links a {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.55rem 0.75rem;
		color: #94a3b8;
		text-decoration: none;
		border-radius: 0.375rem;
		transition: background 0.15s, color 0.15s;
		font-size: 0.9rem;
	}

	.nav-links a:hover {
		background: #334155;
		color: #f1f5f9;
	}

	.nav-links a.active {
		background: #1e3a5f;
		color: #3b82f6;
		font-weight: 600;
	}

	.nav-icon {
		font-size: 1rem;
		width: 1.25rem;
		text-align: center;
	}

	.sidebar-footer {
		padding: 0 0.5rem;
		border-top: 1px solid #334155;
		padding-top: 0.75rem;
	}

	.version {
		font-size: 0.7rem;
		color: #475569;
		font-family: monospace;
	}

	.content {
		padding: 1rem;
		margin-left: 220px;
		height: 100vh;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}
</style>
