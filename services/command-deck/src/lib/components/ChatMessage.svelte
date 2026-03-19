<!-- ChatMessage.svelte — Individual chat message renderer with thoughts, tool calls, and markdown-lite -->

<script lang="ts">
	import type { ChatMessage } from '$lib/types';
	import { showThoughts, showToolCalls } from '$stores/chat';
	import ToolCallCard from './ToolCallCard.svelte';
	import ThoughtChain from './ThoughtChain.svelte';

	let { message } = $props<{ message: ChatMessage }>();

	let thoughtsExpanded = $state(true);
	let toolsExpanded = $state(true);

	const isUser = $derived(message.role === 'user');
	const isAssistant = $derived(message.role === 'assistant');

	const hasThoughts = $derived(message.thoughts.length > 0);
	const hasToolCalls = $derived(message.toolCalls.length > 0);

	const showThoughtsSection = $derived($showThoughts && hasThoughts && isAssistant);
	const showToolsSection = $derived($showToolCalls && hasToolCalls && isAssistant);

	const timeString = $derived(
		new Date(message.timestamp).toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
		})
	);

	/**
	 * Minimal markdown renderer: bold, inline code, code blocks, and unordered lists.
	 * Returns HTML string for use with {@html}.
	 */
	function renderMarkdownLite(text: string): string {
		if (!text) return '';

		// Escape HTML entities first
		let html = text
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;');

		// Code blocks (``` ... ```)
		html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_match, _lang, code) => {
			return `<pre class="msg-code-block"><code>${code.trim()}</code></pre>`;
		});

		// Inline code (`...`)
		html = html.replace(/`([^`]+)`/g, '<code class="msg-inline-code">$1</code>');

		// Bold (**...**)
		html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

		// Italic (*...*)
		html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');

		// Unordered lists (lines starting with - or *)
		html = html.replace(
			/((?:^|\n)[*\-] .+(?:\n[*\-] .+)*)/g,
			(block) => {
				const items = block
					.trim()
					.split('\n')
					.map((line) => `<li>${line.replace(/^[*\-] /, '')}</li>`)
					.join('');
				return `<ul class="msg-list">${items}</ul>`;
			}
		);

		// Paragraphs: split by double newlines
		html = html.replace(/\n\n/g, '</p><p>');

		// Single newlines to <br>
		html = html.replace(/\n/g, '<br>');

		return html;
	}

	const renderedContent = $derived(renderMarkdownLite(message.content));
</script>

<div
	class="chat-message"
	class:chat-message--user={isUser}
	class:chat-message--assistant={isAssistant}
	role="article"
	aria-label="{message.role} message"
>
	<div class="message-row">
		{#if isAssistant}
			<div class="avatar avatar--assistant" aria-hidden="true">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<path d="M12 2a4 4 0 0 1 4 4v2a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z"/>
					<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
					<circle cx="12" cy="7" r="0.5" fill="currentColor"/>
				</svg>
			</div>
		{/if}

		<div class="bubble" class:bubble--user={isUser} class:bubble--assistant={isAssistant}>
			{#if showThoughtsSection}
				<div class="thoughts-section">
					<button
						class="section-header"
						onclick={() => (thoughtsExpanded = !thoughtsExpanded)}
						aria-expanded={thoughtsExpanded}
					>
						<span class="section-icon" class:section-icon--open={thoughtsExpanded}>&#9656;</span>
						<span class="section-label">Reasoning</span>
						<span class="section-count">{message.thoughts.length} step{message.thoughts.length !== 1 ? 's' : ''}</span>
					</button>
					{#if thoughtsExpanded}
						<div class="section-content">
							<ThoughtChain thoughts={message.thoughts} />
						</div>
					{/if}
				</div>
			{/if}

			{#if showToolsSection}
				<div class="tools-section">
					<button
						class="section-header"
						onclick={() => (toolsExpanded = !toolsExpanded)}
						aria-expanded={toolsExpanded}
					>
						<span class="section-icon" class:section-icon--open={toolsExpanded}>&#9656;</span>
						<span class="section-label">Tool Invocations</span>
						<span class="section-count">{message.toolCalls.length}</span>
					</button>
					{#if toolsExpanded}
						<div class="section-content tool-cards">
							{#each message.toolCalls as tc (tc.callId)}
								<ToolCallCard toolCall={tc} />
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			{#if message.content}
				<div class="message-content">
					{@html renderedContent}
				</div>
			{/if}

			{#if message.isStreaming && !message.content}
				<div class="empty-streaming">
					<span class="streaming-dots">
						<span class="dot"></span>
						<span class="dot"></span>
						<span class="dot"></span>
					</span>
				</div>
			{/if}

			{#if message.isStreaming && message.content}
				<span class="streaming-cursor" aria-label="Streaming">&#9612;</span>
			{/if}
		</div>

		{#if isUser}
			<div class="avatar avatar--user" aria-hidden="true">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<circle cx="12" cy="8" r="4"/>
					<path d="M20 21a8 8 0 1 0-16 0"/>
				</svg>
			</div>
		{/if}
	</div>

	<div class="message-time" class:message-time--right={isUser}>
		{timeString}
	</div>
</div>

<style>
	.chat-message {
		padding: 0.25rem 0;
	}

	.message-row {
		display: flex;
		gap: 0.625rem;
		align-items: flex-start;
	}

	.chat-message--user .message-row {
		justify-content: flex-end;
	}

	.chat-message--assistant .message-row {
		justify-content: flex-start;
	}

	/* ── Avatars ──────────────────────────────────────────────────────── */

	.avatar {
		flex-shrink: 0;
		width: 2rem;
		height: 2rem;
		border-radius: 50%;
		display: flex;
		align-items: center;
		justify-content: center;
		margin-top: 0.125rem;
	}

	.avatar--assistant {
		background: #1e293b;
		border: 1px solid #334155;
		color: #94a3b8;
	}

	.avatar--user {
		background: #1d4ed8;
		color: #dbeafe;
	}

	/* ── Bubbles ──────────────────────────────────────────────────────── */

	.bubble {
		max-width: 80%;
		min-width: 3rem;
		border-radius: 0.75rem;
		padding: 0.75rem 1rem;
		line-height: 1.5;
		font-size: 0.875rem;
		word-break: break-word;
	}

	.bubble--user {
		background: #3b82f6;
		color: #ffffff;
		border-bottom-right-radius: 0.25rem;
	}

	.bubble--assistant {
		background: #1e293b;
		color: #f1f5f9;
		border: 1px solid #334155;
		border-bottom-left-radius: 0.25rem;
	}

	/* ── Section Toggles (Thoughts / Tools) ──────────────────────────── */

	.thoughts-section,
	.tools-section {
		margin-bottom: 0.625rem;
		border-bottom: 1px solid rgba(51, 65, 85, 0.6);
		padding-bottom: 0.5rem;
	}

	.section-header {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		padding: 0.25rem 0;
		background: none;
		border: none;
		color: #94a3b8;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		cursor: pointer;
		transition: color 0.15s;
	}

	.section-header:hover {
		color: #cbd5e1;
	}

	.section-icon {
		font-size: 0.625rem;
		transition: transform 0.2s;
	}

	.section-icon--open {
		transform: rotate(90deg);
	}

	.section-count {
		margin-left: auto;
		font-weight: 400;
		font-size: 0.6875rem;
		opacity: 0.7;
	}

	.section-content {
		margin-top: 0.375rem;
		animation: sectionExpand 0.2s ease-out;
	}

	@keyframes sectionExpand {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	.tool-cards {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	/* ── Message Content (markdown-lite) ─────────────────────────────── */

	.message-content {
		line-height: 1.6;
	}

	.message-content :global(p) {
		margin: 0 0 0.5rem;
	}

	.message-content :global(p:last-child) {
		margin-bottom: 0;
	}

	.message-content :global(strong) {
		font-weight: 600;
		color: #f8fafc;
	}

	.message-content :global(.msg-inline-code) {
		background: rgba(0, 0, 0, 0.3);
		padding: 0.125rem 0.375rem;
		border-radius: 0.25rem;
		font-family: 'JetBrains Mono', 'Fira Code', monospace;
		font-size: 0.8125em;
		color: #e2e8f0;
	}

	.message-content :global(.msg-code-block) {
		background: rgba(0, 0, 0, 0.4);
		padding: 0.75rem;
		border-radius: 0.375rem;
		font-family: 'JetBrains Mono', 'Fira Code', monospace;
		font-size: 0.8125em;
		line-height: 1.5;
		overflow-x: auto;
		margin: 0.5rem 0;
		color: #e2e8f0;
	}

	.message-content :global(.msg-code-block code) {
		background: none;
		padding: 0;
	}

	.message-content :global(.msg-list) {
		margin: 0.375rem 0;
		padding-left: 1.25rem;
	}

	.message-content :global(.msg-list li) {
		margin-bottom: 0.25rem;
	}

	.bubble--user .message-content :global(strong) {
		color: #ffffff;
	}

	.bubble--user .message-content :global(.msg-inline-code) {
		background: rgba(255, 255, 255, 0.2);
		color: #ffffff;
	}

	/* ── Streaming Indicators ─────────────────────────────────────────── */

	.streaming-cursor {
		display: inline;
		animation: cursorBlink 1s step-end infinite;
		color: #3b82f6;
		font-weight: 700;
	}

	@keyframes cursorBlink {
		0%, 100% {
			opacity: 1;
		}
		50% {
			opacity: 0;
		}
	}

	.empty-streaming {
		display: flex;
		align-items: center;
		padding: 0.25rem 0;
	}

	.streaming-dots {
		display: flex;
		gap: 0.25rem;
	}

	.dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: #94a3b8;
		animation: dotBounce 1.4s ease-in-out infinite;
	}

	.dot:nth-child(2) {
		animation-delay: 0.2s;
	}

	.dot:nth-child(3) {
		animation-delay: 0.4s;
	}

	@keyframes dotBounce {
		0%, 80%, 100% {
			opacity: 0.3;
			transform: scale(0.8);
		}
		40% {
			opacity: 1;
			transform: scale(1);
		}
	}

	/* ── Timestamp ─────────────────────────────────────────────────────── */

	.message-time {
		font-size: 0.6875rem;
		color: #64748b;
		padding: 0.125rem 2.75rem;
	}

	.message-time--right {
		text-align: right;
	}
</style>
