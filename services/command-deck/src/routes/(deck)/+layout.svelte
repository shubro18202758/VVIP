<script lang="ts">
	/**
	 * (deck) — operator surfaces. Command theme (command-graphite) + the
	 * CommandShell chrome; pages supply the command-bar state they own.
	 */
	import CommandShell from '$lib/shell/CommandShell.svelte';

	let { children } = $props();

	$effect(() => {
		document.documentElement.dataset.theme = 'command';
	});
</script>

<CommandShell>
	{@render children?.()}
</CommandShell>
