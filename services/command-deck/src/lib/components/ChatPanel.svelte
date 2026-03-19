<!-- ChatPanel.svelte — Main LLM chat container with streaming, thoughts, and tool call display -->

<script lang="ts">
	import {
		messages,
		isStreaming,
		showThoughts,
		showToolCalls,
		autoScroll,
		addUserMessage,
		startAssistantMessage,
		appendToMessage,
		addThought,
		addToolCall,
		updateToolCallState,
		finishStreaming,
		clearChat,
	} from '$stores/chat';
	import { activeConvoy } from '$stores/convoy';
	import ChatMessage from './ChatMessage.svelte';
	import ChatInput from './ChatInput.svelte';
	import { streamChat } from '$api/client';

	let scrollContainer = $state<HTMLDivElement | null>(null);

	// Auto-scroll to bottom when messages change (if autoScroll is enabled)
	$effect(() => {
		// Track messages array to trigger on change
		const _msgs = $messages;
		if ($autoScroll && scrollContainer) {
			// Use requestAnimationFrame to ensure DOM has updated
			requestAnimationFrame(() => {
				if (scrollContainer) {
					scrollContainer.scrollTop = scrollContainer.scrollHeight;
				}
			});
		}
	});

	function sendMessage(text: string): void {
		addUserMessage(text);
		const msgId = startAssistantMessage();

		streamChat(
			text,
			$activeConvoy?.movementId ?? null,
			$activeConvoy?.vvipClass ?? null,
			{
				onToken: (t) => appendToMessage(msgId, t),
				onThought: (s) =>
					addThought(msgId, {
						...s,
						timestamp: Date.now(),
					}),
				onToolCall: (c) =>
					addToolCall(msgId, {
						...c,
						result: null,
						durationMs: null,
						startedAt: Date.now(),
					}),
				onToolResult: (r) =>
					updateToolCallState(msgId, r.callId, r.state, r.result, r.durationMs),
				onDone: () => finishStreaming(msgId),
				onError: (e) => {
					appendToMessage(msgId, `\n\n**Error:** ${e.message}`);
					finishStreaming(msgId);
				},
			},
		);
	}

	function handleClear(): void {
		if ($messages.length === 0) return;
		clearChat();
	}

	function handleScrollEvent(): void {
		if (!scrollContainer) return;
		const { scrollTop, scrollHeight, clientHeight } = scrollContainer;
		const atBottom = scrollHeight - scrollTop - clientHeight < 40;
		if ($autoScroll !== atBottom) {
			autoScroll.set(atBottom);
		}
	}
</script>

<div class="chat-panel">
	<!-- Header bar -->
	<div class="panel-header">
		<div class="header-left">
			<h2 class="panel-title">Agent Console</h2>
			{#if $isStreaming}
				<span class="streaming-badge">Streaming</span>
			{/if}
		</div>

		<div class="header-controls">
			<label class="toggle-control" title="Show agent reasoning steps">
				<input
					type="checkbox"
					bind:checked={$showThoughts}
				/>
				<span class="toggle-label">Thoughts</span>
			</label>

			<label class="toggle-control" title="Show MCP tool invocations">
				<input
					type="checkbox"
					bind:checked={$showToolCalls}
				/>
				<span class="toggle-label">Tools</span>
			</label>

			<button
				class="clear-button"
				onclick={handleClear}
				disabled={$messages.length === 0}
				title="Clear conversation"
			>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<path d="M3 6h18"/>
					<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
					<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
				</svg>
			</button>
		</div>
	</div>

	<!-- Scrollable message list -->
	<div
		class="message-list"
		bind:this={scrollContainer}
		onscroll={handleScrollEvent}
	>
		{#if $messages.length === 0}
			<div class="empty-state">
				<div class="empty-icon" aria-hidden="true">
					<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
						<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
						<circle cx="12" cy="10" r="0.5" fill="currentColor"/>
						<circle cx="8" cy="10" r="0.5" fill="currentColor"/>
						<circle cx="16" cy="10" r="0.5" fill="currentColor"/>
					</svg>
				</div>
				<h3 class="empty-title">Convoy Agent Ready</h3>
				<p class="empty-description">
					Ask the agent to plan routes, predict traffic, evaluate scenarios,
					or manage diversions. The agent has access to real-time traffic data
					and optimization tools.
				</p>
			</div>
		{:else}
			{#each $messages as msg (msg.id)}
				<ChatMessage message={msg} />
			{/each}
		{/if}

		{#if !$autoScroll && $messages.length > 0}
			<button
				class="scroll-to-bottom"
				onclick={() => {
					autoScroll.set(true);
					if (scrollContainer) {
						scrollContainer.scrollTop = scrollContainer.scrollHeight;
					}
				}}
				aria-label="Scroll to latest message"
			>
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M12 5v14M5 12l7 7 7-7"/>
				</svg>
			</button>
		{/if}
	</div>

	<!-- Fixed-bottom input -->
	<ChatInput onSend={sendMessage} disabled={$isStreaming} />
</div>

<style>
	.chat-panel {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: #0f172a;
		border: 1px solid #334155;
		border-radius: 0.75rem;
		overflow: hidden;
	}

	/* ── Header ──────────────────────────────────────────────────────── */

	.panel-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid #334155;
		background: #1e293b;
		flex-shrink: 0;
	}

	.header-left {
		display: flex;
		align-items: center;
		gap: 0.625rem;
	}

	.panel-title {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 700;
		color: #f1f5f9;
	}

	.streaming-badge {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #3b82f6;
		padding: 0.125rem 0.5rem;
		border: 1px solid rgba(59, 130, 246, 0.4);
		border-radius: 999px;
		background: rgba(59, 130, 246, 0.1);
		animation: badgePulse 2s ease-in-out infinite;
	}

	@keyframes badgePulse {
		0%, 100% {
			opacity: 1;
		}
		50% {
			opacity: 0.6;
		}
	}

	.header-controls {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.toggle-control {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		cursor: pointer;
		user-select: none;
	}

	.toggle-control input[type='checkbox'] {
		width: 14px;
		height: 14px;
		accent-color: #3b82f6;
		cursor: pointer;
		margin: 0;
	}

	.toggle-label {
		font-size: 0.75rem;
		color: #94a3b8;
		font-weight: 500;
	}

	.clear-button {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		border-radius: 0.375rem;
		background: none;
		border: 1px solid #334155;
		color: #94a3b8;
		cursor: pointer;
		transition: background 0.15s, color 0.15s, border-color 0.15s;
	}

	.clear-button:hover:not(:disabled) {
		background: #334155;
		color: #ef4444;
		border-color: #475569;
	}

	.clear-button:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	/* ── Message List ─────────────────────────────────────────────────── */

	.message-list {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		position: relative;
		scrollbar-width: thin;
		scrollbar-color: #334155 transparent;
	}

	.message-list::-webkit-scrollbar {
		width: 6px;
	}

	.message-list::-webkit-scrollbar-track {
		background: transparent;
	}

	.message-list::-webkit-scrollbar-thumb {
		background: #334155;
		border-radius: 3px;
	}

	.message-list::-webkit-scrollbar-thumb:hover {
		background: #475569;
	}

	/* ── Empty State ──────────────────────────────────────────────────── */

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		text-align: center;
		padding: 3rem 2rem;
		flex: 1;
	}

	.empty-icon {
		color: #334155;
		margin-bottom: 1rem;
	}

	.empty-title {
		margin: 0 0 0.5rem;
		font-size: 1.125rem;
		font-weight: 600;
		color: #94a3b8;
	}

	.empty-description {
		margin: 0;
		font-size: 0.8125rem;
		color: #64748b;
		max-width: 28rem;
		line-height: 1.5;
	}

	/* ── Scroll-to-Bottom Button ──────────────────────────────────────── */

	.scroll-to-bottom {
		position: sticky;
		bottom: 0.5rem;
		align-self: center;
		width: 2rem;
		height: 2rem;
		border-radius: 50%;
		background: #1e293b;
		border: 1px solid #334155;
		color: #94a3b8;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: background 0.15s, color 0.15s;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
		z-index: 5;
	}

	.scroll-to-bottom:hover {
		background: #334155;
		color: #f1f5f9;
	}
</style>
