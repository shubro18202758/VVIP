<script lang="ts">
	import ChatPanel from '$components/ChatPanel.svelte';
	import CorridorMap from '$components/CorridorMap.svelte';
	import { activeConvoy } from '$stores/convoy';
	import { messages, showThoughts, showToolCalls } from '$stores/chat';
</script>

<div class="chat-page">
	<div class="chat-panel-container">
		<ChatPanel />
	</div>
	<div class="chat-context">
		<div class="context-map">
			<CorridorMap zoom={13} />
		</div>
		<div class="context-info">
			{#if $activeConvoy}
				<div class="context-card">
					<h3>Active Convoy</h3>
					<div class="ctx-row">
						<span>Movement:</span>
						<span class="mono">{$activeConvoy.movementId}</span>
					</div>
					<div class="ctx-row">
						<span>Class:</span>
						<span class="badge">{$activeConvoy.vvipClass}</span>
					</div>
					<div class="ctx-row">
						<span>Status:</span>
						<span>{$activeConvoy.status}</span>
					</div>
				</div>
			{:else}
				<div class="context-card">
					<h3>No Active Convoy</h3>
					<p class="hint">The agent can still answer questions about traffic, routing, and protocols.</p>
				</div>
			{/if}

			<div class="context-card">
				<h3>Display Options</h3>
				<label>
					<input type="checkbox" bind:checked={$showThoughts} />
					Show reasoning chain
				</label>
				<label>
					<input type="checkbox" bind:checked={$showToolCalls} />
					Show MCP tool calls
				</label>
			</div>

			<div class="context-card">
				<h3>Stats</h3>
				<div class="ctx-row">
					<span>Messages:</span>
					<span class="mono">{$messages.length}</span>
				</div>
			</div>
		</div>
	</div>
</div>

<style>
	.chat-page {
		display: grid;
		grid-template-columns: 1fr 380px;
		gap: 1rem;
		flex: 1;
		min-height: 0;
	}

	.chat-panel-container {
		display: flex;
		flex-direction: column;
		overflow: hidden;
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
	}

	.chat-context {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		overflow-y: auto;
	}

	.context-map {
		height: 300px;
		border-radius: 0.5rem;
		overflow: hidden;
		flex-shrink: 0;
	}

	.context-info {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.context-card {
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		padding: 1rem;
	}

	.context-card h3 {
		font-size: 0.85rem;
		color: #94a3b8;
		margin: 0 0 0.5rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.ctx-row {
		display: flex;
		justify-content: space-between;
		font-size: 0.85rem;
		padding: 0.2rem 0;
		color: #94a3b8;
	}

	.mono { font-family: monospace; color: #f1f5f9; }

	.badge {
		background: #1e3a5f;
		color: #3b82f6;
		padding: 0.1rem 0.4rem;
		border-radius: 0.2rem;
		font-weight: 700;
		font-size: 0.8rem;
	}

	.hint {
		color: #64748b;
		font-size: 0.8rem;
		font-style: italic;
		margin: 0;
	}

	.context-card label {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.85rem;
		color: #94a3b8;
		padding: 0.2rem 0;
		cursor: pointer;
	}
</style>
