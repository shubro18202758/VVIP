<script lang="ts">
	/**
	 * PanelShell — alias of Panel (the plan names both; panels render only
	 * what they are given, per-panel degradation stays the panel's concern).
	 */
	import Panel from './Panel.svelte';

	let { children, ...rest } = $props();
</script>

<Panel {...rest}>
	{#if children}{@render children()}{/if}
</Panel>

<!-- Intentionally no <style>: PanelShell is a pure pass-through of Panel. -->
