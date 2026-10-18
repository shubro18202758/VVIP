<script lang="ts">
	/**
	 * ChatConsole — the rebuilt /chat agent console on the design system.
	 *
	 * Keeps the real NDJSON streamChat wiring (POST /api/convoy/chat/stream
	 * via $lib/api/client — read-only import) but on FeedItem primitives:
	 * model tokens render mono, tool calls render as --v-tool cards with
	 * durations, reasoning is a toggle. Message state is LOCAL (the legacy
	 * $stores/chat store is out of bounds for this stream). The context
	 * header is the SELECTED EVENT from the ops store; when the brain is
	 * down the console shows its labelled degradation chip instead of a
	 * silent spinner.
	 */
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import { streamChat } from '$lib/api/client';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';

	interface ToolCall {
		callId: string;
		toolName: string;
		state: 'pending' | 'running' | 'success' | 'error';
		durationMs: number | null;
	}

	interface Thought {
		stepIndex: number;
		text: string;
	}

	interface Message {
		id: number;
		role: 'user' | 'agent';
		text: string;
		thoughts: Thought[];
		toolCalls: ToolCall[];
	}

	let { lang = 'en' }: { lang?: string } = $props();

	let messages = $state<Message[]>([]);
	let draft = $state('');
	let streaming = $state(false);
	let degraded = $state(false);
	let showThoughts = $state(true);
	let showTools = $state(true);
	let scrollEl: HTMLDivElement | undefined = $state();

	let seq = 0;
	let selected = $derived(ops.selectedEvent);

	$effect(() => {
		void messages.length;
		requestAnimationFrame(() => {
			if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
		});
	});

	function send() {
		const text = draft.trim();
		if (!text || streaming) return;

		const ctx = selected ? `[${selected.eventId} · ${selected.name}] ` : '';
		messages.push({ id: ++seq, role: 'user', text, thoughts: [], toolCalls: [] });
		const reply: Message = { id: ++seq, role: 'agent', text: '', thoughts: [], toolCalls: [] };
		messages.push(reply);
		draft = '';
		streaming = true;
		degraded = false;

		streamChat(ctx + text, null, null, {
			onToken: (tok) => (reply.text += tok),
			onThought: (s) => reply.thoughts.push({ stepIndex: s.stepIndex, text: s.text }),
			onToolCall: (c) => {
				const existing = reply.toolCalls.find((tc) => tc.callId === c.callId);
				if (existing) existing.state = c.state;
				else reply.toolCalls.push({ callId: c.callId, toolName: c.toolName, state: c.state, durationMs: null });
			},
			onToolResult: (r) => {
				const tc = reply.toolCalls.find((x) => x.callId === r.callId);
				if (tc) {
					tc.state = r.state;
					tc.durationMs = r.durationMs;
				}
			},
			onDone: () => {
				streaming = false;
			},
			onError: () => {
				streaming = false;
				degraded = true;
			},
		}).catch(() => {
			// streamChat rejects outright when fetch itself fails (proxy
			// down / request aborted) — the callbacks never run, so the
			// console must clear its streaming state here too.
			streaming = false;
			degraded = true;
		});
	}

	function stateLabel(s: ToolCall['state']): string {
		switch (s) {
			case 'pending':
				return citizenT(lang, 'chat_tool_pending');
			case 'running':
				return citizenT(lang, 'chat_tool_running');
			case 'success':
				return citizenT(lang, 'chat_tool_success');
			case 'error':
				return citizenT(lang, 'chat_tool_error');
		}
	}
</script>

<div class="console" data-screen="chat">
	<header class="head">
		<div class="head-text">
			<h1>{citizenT(lang, 'chat_title')}</h1>
			<p>{citizenT(lang, 'chat_sub')}</p>
		</div>
		{#if degraded}
			<Badge variant="offline" label={citizenT(lang, 'chat_offline')} />
		{/if}
	</header>

	<!-- Context header: the SELECTED EVENT from the ops store. -->
	<section class="context" data-testid="chat-context">
		<span class="ctx-label">{citizenT(lang, 'chat_context')}</span>
		{#if selected}
			<span class="ctx-event">
				{t(lang, `kind_${selected.eventKind}`)} · <span class="mono">{selected.eventId}</span>
			</span>
		{:else}
			<span class="ctx-none">{citizenT(lang, 'chat_context_none')}</span>
		{/if}
	</section>

	{#if degraded}
		<p class="degraded-note" data-testid="chat-degraded">{citizenT(lang, 'chat_offline_note')}</p>
	{/if}

	<div class="toggles">
		<label class="toggle">
			<input type="checkbox" bind:checked={showThoughts} />
			{citizenT(lang, 'chat_thoughts')}
		</label>
		<label class="toggle">
			<input type="checkbox" bind:checked={showTools} />
			{citizenT(lang, 'chat_tools')}
		</label>
		{#if messages.length}
			<Button variant="ghost" size="sm" onclick={() => (messages = [])}>{citizenT(lang, 'chat_clear')}</Button>
		{/if}
	</div>

	<div class="stream" bind:this={scrollEl} data-testid="chat-stream">
		{#if messages.length === 0}
			<EmptyState title={citizenT(lang, 'chat_title')} hint={citizenT(lang, 'chat_placeholder')} />
		{:else}
			{#each messages as m (m.id)}
				<article class="msg role-{m.role}" data-msg-role={m.role}>
					<span class="who">{m.role === 'user' ? citizenT(lang, 'chat_you') : citizenT(lang, 'chat_agent')}</span>

					{#if m.role === 'agent' && showThoughts && m.thoughts.length}
						<details class="thoughts">
							<summary>{citizenT(lang, 'chat_thoughts')} · <span class="mono">{m.thoughts.length}</span></summary>
							{#each m.thoughts as th (th.stepIndex)}
								<p class="thought"><span class="mono idx">{th.stepIndex}</span> {th.text}</p>
							{/each}
						</details>
					{/if}

					{#if m.role === 'agent' && showTools && m.toolCalls.length}
						<ul class="tools">
							{#each m.toolCalls as tc (tc.callId)}
								<li class="tool-card state-{tc.state}" data-tool-state={tc.state}>
									<span class="tool-name mono">{tc.toolName}</span>
									<span class="tool-state">{stateLabel(tc.state)}</span>
									{#if tc.durationMs != null}
										<span class="tool-dur mono">{tc.durationMs} ms</span>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}

					{#if m.text}
						<p class="body" class:mono={m.role === 'agent'}>{m.text}</p>
					{:else if m.role === 'agent' && streaming}
						<p class="body mono dim">…</p>
					{/if}
				</article>
			{/each}
		{/if}
	</div>

	<form
		class="composer"
		onsubmit={(e) => {
			e.preventDefault();
			send();
		}}
	>
		<input
			type="text"
			bind:value={draft}
			placeholder={citizenT(lang, 'chat_placeholder')}
			aria-label={citizenT(lang, 'chat_placeholder')}
			disabled={degraded && messages.length === 0}
			data-testid="chat-input"
		/>
		<Button type="submit" variant="secondary" size="sm" busy={streaming} disabled={!draft.trim() || streaming}>
			{citizenT(lang, 'chat_send')}
		</Button>
	</form>
</div>

<style>
	.console {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		height: 100%;
		min-height: 0;
		padding: var(--v-pad-panel);
		font-size: var(--v-text-base);
		color: var(--v-ink-1);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: var(--v-gap-panel);
	}

	.head h1 {
		margin: 0;
		font-size: var(--v-text-lg);
	}

	.head p {
		margin: var(--v-space-1) 0 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-xs);
	}

	.context {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-3);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		font-size: var(--v-text-xs);
	}

	.ctx-label {
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.ctx-event {
		color: var(--v-ink-1);
	}

	.ctx-none {
		color: var(--v-ink-3);
	}

	.degraded-note {
		margin: 0;
		padding: var(--v-space-2) var(--v-space-3);
		border: 1px dashed var(--v-band-red);
		border-radius: var(--v-radius-chip);
		color: var(--v-band-red);
		font-size: var(--v-text-xs);
	}

	.toggles {
		display: flex;
		align-items: center;
		gap: var(--v-space-4);
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.toggle {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		cursor: pointer;
	}

	.stream {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-3);
		padding-right: var(--v-space-1);
	}

	.msg {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		padding: var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		background: var(--v-surface-1);
	}

	.msg.role-user {
		background: var(--v-surface-2);
	}

	.who {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.thoughts {
		border: 1px dashed var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2) var(--v-space-3);
		font-size: var(--v-text-xs);
	}

	.thoughts summary {
		cursor: pointer;
		color: var(--v-ink-3);
	}

	.thought {
		margin: var(--v-space-1) 0 0;
		color: var(--v-ink-2);
	}

	.idx {
		color: var(--v-ink-4);
		margin-right: var(--v-space-1);
	}

	.tools {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.tool-card {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-3);
		border: 1px solid color-mix(in srgb, var(--v-tool) 45%, transparent);
		border-left: 2px solid var(--v-tool);
		border-radius: var(--v-radius-chip);
		font-size: var(--v-text-xs);
		background: color-mix(in srgb, var(--v-tool) 6%, transparent);
	}

	.tool-name {
		flex: 1;
		color: var(--v-tool);
	}

	.tool-state {
		color: var(--v-ink-3);
	}

	.tool-card.state-error .tool-state {
		color: var(--v-band-red);
	}

	.tool-dur {
		color: var(--v-ink-4);
	}

	.body {
		margin: 0;
		white-space: pre-wrap;
		font-size: var(--v-text-sm);
		line-height: 1.55;
	}

	.body.mono {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
	}

	.body.dim {
		color: var(--v-ink-4);
	}

	.composer {
		display: flex;
		gap: var(--v-space-2);
	}

	.composer input {
		flex: 1;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2) var(--v-space-3);
		font: inherit;
		font-size: var(--v-text-sm);
		background: var(--v-surface-1);
		color: var(--v-ink-1);
		height: var(--v-ctl-lg);
	}

	.composer input:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}
</style>
