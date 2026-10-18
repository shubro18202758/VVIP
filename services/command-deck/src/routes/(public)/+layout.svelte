<script lang="ts">
	/**
	 * (public) — citizen/driver surfaces. Daylight theme (warm paper) + the
	 * CitizenShell chrome; on unmount the theme resets to command so the
	 * operator deck never inherits paper tokens.
	 */
	import CitizenShell from '$lib/shell/CitizenShell.svelte';

	let { children } = $props();

	$effect(() => {
		document.documentElement.dataset.theme = 'daylight';
		return () => {
			document.documentElement.dataset.theme = 'command';
		};
	});
</script>

<CitizenShell>
	{@render children?.()}
</CitizenShell>
