<!-- ChatInput.svelte — Auto-resizing message input with quick action chips -->

<script lang="ts">
	let {
		onSend,
		disabled = false,
	} = $props<{
		onSend: (text: string) => void;
		disabled?: boolean;
	}>();

	let inputText = $state('');
	let textareaEl = $state<HTMLTextAreaElement | null>(null);

	const canSend = $derived(!disabled && inputText.trim().length > 0);

	const quickActions = [
		{ label: 'Plan route', prompt: 'Plan an optimal convoy route considering current traffic conditions.' },
		{ label: 'Check traffic', prompt: 'What is the current traffic situation along the corridor?' },
		{ label: 'Evaluate scenarios', prompt: 'Evaluate alternative routing scenarios and compare their trade-offs.' },
	];

	function send(): void {
		const text = inputText.trim();
		if (!text || disabled) return;
		onSend(text);
		inputText = '';
		resizeTextarea();
	}

	function handleKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			send();
		}
	}

	function handleQuickAction(prompt: string): void {
		if (disabled) return;
		onSend(prompt);
	}

	function resizeTextarea(): void {
		if (!textareaEl) return;
		textareaEl.style.height = 'auto';
		const lineHeight = 24;
		const minHeight = lineHeight;
		const maxHeight = lineHeight * 5;
		const scrollHeight = textareaEl.scrollHeight;
		textareaEl.style.height = `${Math.min(Math.max(scrollHeight, minHeight), maxHeight)}px`;
	}

	$effect(() => {
		// Re-run whenever inputText changes
		inputText;
		resizeTextarea();
	});
</script>

<div class="chat-input-container">
	<div class="quick-actions">
		{#each quickActions as action (action.label)}
			<button
				class="quick-chip"
				onclick={() => handleQuickAction(action.prompt)}
				disabled={disabled}
			>
				{action.label}
			</button>
		{/each}
	</div>

	<div class="input-row">
		<textarea
			bind:this={textareaEl}
			bind:value={inputText}
			onkeydown={handleKeydown}
			oninput={resizeTextarea}
			placeholder={disabled ? 'Agent is responding...' : 'Message the convoy agent...'}
			rows="1"
			disabled={disabled}
			class="message-textarea"
			aria-label="Chat message input"
		></textarea>

		<button
			class="send-button"
			onclick={send}
			disabled={!canSend}
			aria-label="Send message"
		>
			<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M22 2L11 13"/>
				<path d="M22 2L15 22L11 13L2 9L22 2Z"/>
			</svg>
		</button>
	</div>
</div>

<style>
	.chat-input-container {
		padding: 0.75rem 1rem 1rem;
		border-top: 1px solid #334155;
		background: #0f172a;
	}

	/* ── Quick Action Chips ────────────────────────────────────────────── */

	.quick-actions {
		display: flex;
		gap: 0.5rem;
		margin-bottom: 0.625rem;
		flex-wrap: wrap;
	}

	.quick-chip {
		padding: 0.3125rem 0.75rem;
		font-size: 0.75rem;
		font-weight: 500;
		color: #94a3b8;
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 999px;
		cursor: pointer;
		transition: background 0.15s, color 0.15s, border-color 0.15s;
		white-space: nowrap;
	}

	.quick-chip:hover:not(:disabled) {
		background: #334155;
		color: #f1f5f9;
		border-color: #475569;
	}

	.quick-chip:active:not(:disabled) {
		background: #475569;
	}

	.quick-chip:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* ── Input Row ──────────────────────────────────────────────────────── */

	.input-row {
		display: flex;
		align-items: flex-end;
		gap: 0.5rem;
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.75rem;
		padding: 0.5rem 0.5rem 0.5rem 0.875rem;
		transition: border-color 0.15s;
	}

	.input-row:focus-within {
		border-color: #3b82f6;
	}

	.message-textarea {
		flex: 1;
		resize: none;
		border: none;
		outline: none;
		background: transparent;
		color: #f1f5f9;
		font-family: inherit;
		font-size: 0.875rem;
		line-height: 1.5rem;
		padding: 0;
		min-height: 1.5rem;
		max-height: 7.5rem;
		overflow-y: auto;
	}

	.message-textarea::placeholder {
		color: #64748b;
	}

	.message-textarea:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	/* ── Send Button ────────────────────────────────────────────────────── */

	.send-button {
		flex-shrink: 0;
		width: 2.25rem;
		height: 2.25rem;
		border-radius: 0.5rem;
		background: #3b82f6;
		color: #ffffff;
		border: none;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: background 0.15s, opacity 0.15s, transform 0.1s;
	}

	.send-button:hover:not(:disabled) {
		background: #2563eb;
	}

	.send-button:active:not(:disabled) {
		transform: scale(0.95);
	}

	.send-button:disabled {
		background: #334155;
		color: #64748b;
		cursor: not-allowed;
	}
</style>
