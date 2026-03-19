<script lang="ts">
	import DiversionTimeline from '$components/DiversionTimeline.svelte';
	import ConvoyTracker from '$components/ConvoyTracker.svelte';
	import CorridorMap from '$components/CorridorMap.svelte';
	import {
		activeConvoy,
		diversions,
		convoyIsActive,
		selectedRoute,
		updateConvoyStatus,
		updateConvoyPosition,
		clearConvoy,
	} from '$stores/convoy';
	import { messages } from '$stores/chat';
	import { createConvoySocket, launchEscort, clearMovement } from '$api/client';
	import type { WsEvent } from '$lib/types';

	let socketConn: { close: () => void } | null = null;
	let agentLog = $state<Array<{ time: string; text: string; level: 'info' | 'warn' | 'error' }>>([]);

	function connectWebSocket() {
		const convoy = $activeConvoy;
		if (!convoy) return;

		socketConn?.close();
		socketConn = createConvoySocket(convoy.movementId, handleWsEvent);
		addLog('WebSocket connected to convoy-brain', 'info');
	}

	function handleWsEvent(event: WsEvent) {
		switch (event.type) {
			case 'convoy.position': {
				const p = event.payload as { lon: number; lat: number; speedKmh: number; headingDeg: number };
				updateConvoyPosition(p.lon, p.lat, p.speedKmh, p.headingDeg);
				break;
			}
			case 'convoy.status': {
				const s = event.payload as { status: string };
				updateConvoyStatus(s.status as any);
				addLog(`Convoy status: ${s.status}`, 'info');
				break;
			}
			case 'diversion.activated': {
				const d = event.payload as { diversionId: string; segmentName: string };
				addLog(`Diversion activated: ${d.segmentName}`, 'warn');
				break;
			}
			case 'diversion.deactivated': {
				const d = event.payload as { diversionId: string; segmentName: string };
				addLog(`Diversion cleared: ${d.segmentName}`, 'info');
				break;
			}
			case 'agent.thought': {
				const t = event.payload as { text: string };
				addLog(`Agent reasoning: ${t.text.slice(0, 100)}...`, 'info');
				break;
			}
			case 'agent.tool_call': {
				const tc = event.payload as { toolName: string };
				addLog(`Tool invoked: ${tc.toolName}`, 'info');
				break;
			}
			default:
				break;
		}
	}

	function addLog(text: string, level: 'info' | 'warn' | 'error') {
		const time = new Date().toLocaleTimeString('en-US', { hour12: false });
		agentLog = [{ time, text, level }, ...agentLog].slice(0, 200);
	}

	async function handleApprove() {
		if (!$activeConvoy) return;
		updateConvoyStatus('approved');
		addLog('Movement approved by operator', 'info');
		connectWebSocket();
	}

	async function handleLaunch() {
		if (!$activeConvoy) return;
		try {
			await launchEscort($activeConvoy.movementId);
			updateConvoyStatus('active');
			addLog('Escort launched', 'info');
		} catch (err) {
			addLog(`Launch failed: ${err instanceof Error ? err.message : String(err)}`, 'error');
		}
	}

	async function handleAbort() {
		if (!$activeConvoy) return;
		try {
			await clearMovement($activeConvoy.movementId);
			addLog('Movement aborted', 'error');
			clearConvoy();
			socketConn?.close();
		} catch (err) {
			addLog(`Abort failed: ${err instanceof Error ? err.message : String(err)}`, 'error');
		}
	}
</script>

<div class="command-page">
	<h1>Command Center</h1>

	<div class="command-layout">
		<!-- Left: Controls and info -->
		<div class="command-left">
			<!-- Convoy Control -->
			<section class="card">
				<h2>Convoy Control</h2>
				{#if $activeConvoy}
					<ConvoyTracker
						movementId={$activeConvoy.movementId}
						position={$activeConvoy.position}
						speed={$activeConvoy.speedKmh}
						status={$activeConvoy.status}
						vvipClass={$activeConvoy.vvipClass}
					/>
					<div class="action-buttons">
						<button
							class="btn-approve"
							onclick={handleApprove}
							disabled={$activeConvoy.status !== 'planning'}
						>
							Approve Movement
						</button>
						<button
							class="btn-launch"
							onclick={handleLaunch}
							disabled={$activeConvoy.status !== 'approved'}
						>
							Launch Escort
						</button>
						<button class="btn-abort" onclick={handleAbort}>
							Abort Movement
						</button>
					</div>
				{:else}
					<p class="placeholder">No active convoy. <a href="/planning">Plan a route</a> first.</p>
				{/if}
			</section>

			<!-- Live Diversions -->
			<section class="card">
				<h2>Live Diversions</h2>
				<DiversionTimeline diversions={$diversions} />
			</section>
		</div>

		<!-- Center: Live Map -->
		<div class="command-map">
			<CorridorMap zoom={14} />
		</div>

		<!-- Right: Agent Log -->
		<div class="command-right">
			<section class="card agent-log">
				<h2>Agent Activity Log</h2>
				<div class="log-entries">
					{#if agentLog.length === 0}
						<p class="placeholder">No agent activity{$activeConvoy ? '' : ' — convoy-brain offline'}</p>
					{:else}
						{#each agentLog as entry}
							<div class="log-entry" class:warn={entry.level === 'warn'} class:error={entry.level === 'error'}>
								<span class="log-time">{entry.time}</span>
								<span class="log-text">{entry.text}</span>
							</div>
						{/each}
					{/if}
				</div>
			</section>
		</div>
	</div>
</div>

<style>
	.command-page {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
	}
	.command-page h1 { margin-bottom: 1rem; flex-shrink: 0; }

	.command-layout {
		display: grid;
		grid-template-columns: 300px 1fr 320px;
		gap: 1rem;
		flex: 1;
		min-height: 0;
	}

	.command-left {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		overflow-y: auto;
	}

	.command-map {
		border-radius: 0.5rem;
		overflow: hidden;
	}

	.command-right {
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.card {
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		padding: 1rem;
	}

	.card h2 {
		font-size: 0.85rem;
		margin: 0 0 0.75rem;
		color: #94a3b8;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.action-buttons {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
		margin-top: 0.75rem;
	}

	.btn-approve, .btn-launch, .btn-abort {
		border: none;
		padding: 0.5rem 1rem;
		border-radius: 0.375rem;
		cursor: pointer;
		font-weight: 600;
		font-size: 0.825rem;
	}

	.btn-approve { background: #22c55e; color: #0f172a; }
	.btn-launch { background: #3b82f6; color: white; }
	.btn-abort { background: #ef4444; color: white; }

	.btn-approve:disabled, .btn-launch:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}

	.placeholder {
		color: #64748b;
		font-style: italic;
		font-size: 0.875rem;
	}

	.placeholder a {
		color: #3b82f6;
		text-decoration: none;
	}

	.agent-log {
		flex: 1;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.log-entries {
		flex: 1;
		font-family: monospace;
		font-size: 0.775rem;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.log-entry {
		display: flex;
		gap: 0.5rem;
		padding: 0.2rem 0.4rem;
		border-radius: 0.2rem;
		line-height: 1.4;
	}

	.log-entry.warn { background: rgba(245, 158, 11, 0.1); }
	.log-entry.error { background: rgba(239, 68, 68, 0.1); }

	.log-time {
		color: #475569;
		flex-shrink: 0;
		min-width: 6ch;
	}

	.log-text {
		color: #94a3b8;
		word-break: break-word;
	}

	.log-entry.warn .log-text { color: #f59e0b; }
	.log-entry.error .log-text { color: #ef4444; }
</style>
