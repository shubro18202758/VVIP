<script lang="ts">
	/**
	 * (deck) — operator surfaces. Command theme (command-graphite) + the
	 * CommandShell chrome; pages supply the command-bar state they own.
	 *
	 * The operator gate's session is read HERE, once per mount of any
	 * operator route ('/', /ops, /gods-eye, /comms, /chat): with the gate on
	 * (VEER_OPERATOR_PIN) the officer the deck verified becomes the duty
	 * officer everywhere — the ops store (started by every one of those
	 * pages) follows the shell's session state and adopts it: plates unmask
	 * for them, writer controls unlock, and the manual officer fields lock
	 * to that name, which is the name the server audits (x-veer-officer).
	 * Signing out releases it. This layout itself stays on the design-shell
	 * side of the R4 boundary (it never imports the ops store).
	 */
	import { onMount } from 'svelte';
	import CommandShell from '$lib/shell/CommandShell.svelte';
	import { operatorSession } from '$lib/shell/session.svelte';

	let { children } = $props();

	onMount(() => {
		void operatorSession.refresh();
	});

	$effect(() => {
		document.documentElement.dataset.theme = 'command';
	});
</script>

<CommandShell>
	{@render children?.()}
</CommandShell>
