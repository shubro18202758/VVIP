<!-- DiversionTimeline.svelte — Visual timeline of segment diversions with real-time status -->
<!-- Displays diversion schedule with queue lengths, countdown timers, and progress bars -->

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { diversions, activeDiversions } from '$stores/convoy';
	import type { DiversionEntry, DiversionStatus } from '$lib/types';

	// ─── State ────────────────────────────────────────────────────────────────
	let now = $state(Date.now());
	let tickInterval: ReturnType<typeof setInterval> | null = null;

	// ─── Derived from stores ──────────────────────────────────────────────────
	let allDiversions = $derived($diversions);
	let activeCount = $derived($activeDiversions.length);

	// ─── Diversion type styling ───────────────────────────────────────────────
	const typeColors: Record<string, string> = {
		full_closure: '#ef4444',
		partial_closure: '#f59e0b',
		speed_restriction: '#3b82f6',
		signal_override: '#8b5cf6',
	};

	const typeIcons: Record<string, string> = {
		full_closure: '\u26D4',
		partial_closure: '\u26A0',
		speed_restriction: '\u23F0',
		signal_override: '\u26A1',
	};

	const statusColors: Record<DiversionStatus, string> = {
		pending: '#f59e0b',
		active: '#22c55e',
		completed: '#6b7280',
	};

	// ─── Time helpers ─────────────────────────────────────────────────────────
	function parseIso(isoStr: string): number {
		return new Date(isoStr).getTime();
	}

	function formatCountdown(ms: number): string {
		if (ms <= 0) return '00:00';
		const totalSec = Math.floor(ms / 1000);
		const hrs = Math.floor(totalSec / 3600);
		const mins = Math.floor((totalSec % 3600) / 60);
		const secs = totalSec % 60;
		if (hrs > 0) {
			return `${hrs}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
		}
		return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
	}

	function formatTime(isoStr: string): string {
		try {
			const d = new Date(isoStr);
			return d.toLocaleTimeString('en-US', {
				hour: '2-digit',
				minute: '2-digit',
				second: '2-digit',
				hour12: false,
			});
		} catch {
			return isoStr;
		}
	}

	// ─── Per-diversion computed data ──────────────────────────────────────────
	function getCountdown(div: DiversionEntry): number {
		if (div.status !== 'pending') return 0;
		return Math.max(0, parseIso(div.activateAt) - now);
	}

	function getProgressPct(div: DiversionEntry): number {
		if (div.status !== 'active') return div.status === 'completed' ? 100 : 0;
		const start = parseIso(div.activateAt);
		const end = parseIso(div.deactivateAt);
		const total = end - start;
		if (total <= 0) return 100;
		const elapsed = now - start;
		return Math.min(100, Math.max(0, (elapsed / total) * 100));
	}

	function getTimeRemaining(div: DiversionEntry): number {
		if (div.status !== 'active') return 0;
		return Math.max(0, parseIso(div.deactivateAt) - now);
	}

	function getQueueBarWidth(queueLengthM: number): number {
		// Normalize queue to 0-100 (assume 500m as max for visualization)
		return Math.min(100, (queueLengthM / 500) * 100);
	}

	function queueSeverityColor(queueLengthM: number): string {
		if (queueLengthM < 100) return '#22c55e';
		if (queueLengthM < 250) return '#eab308';
		if (queueLengthM < 400) return '#f97316';
		return '#ef4444';
	}

	// ─── Tick timer for live countdown/progress ───────────────────────────────
	onMount(() => {
		tickInterval = setInterval(() => {
			now = Date.now();
		}, 1000);
	});

	onDestroy(() => {
		if (tickInterval) clearInterval(tickInterval);
	});
</script>

<div class="diversion-timeline">
	<div class="timeline-header">
		<h3 class="timeline-title">Diversion Schedule</h3>
		<div class="header-stats">
			<span class="stat-pill">{allDiversions.length} total</span>
			{#if activeCount > 0}
				<span class="stat-pill active">{activeCount} active</span>
			{/if}
		</div>
	</div>

	{#if allDiversions.length === 0}
		<div class="empty-state">
			<div class="empty-icon">{'\u2B21'}</div>
			<span>No diversions planned</span>
			<span class="empty-sub">Diversions will appear here when a convoy route is active</span>
		</div>
	{:else}
		<div class="timeline">
			{#each allDiversions as div (div.diversionId)}
				{@const countdown = getCountdown(div)}
				{@const progress = getProgressPct(div)}
				{@const remaining = getTimeRemaining(div)}
				{@const typeColor = typeColors[div.diversionType] ?? '#6b7280'}

				<div
					class="timeline-entry"
					class:pending={div.status === 'pending'}
					class:active={div.status === 'active'}
					class:completed={div.status === 'completed'}
				>
					<!-- Left accent bar -->
					<div class="accent-bar" style="background: {typeColor}"></div>

					<div class="entry-content">
						<!-- Entry header -->
						<div class="entry-header">
							<div class="entry-title-row">
								<span class="type-icon">{typeIcons[div.diversionType] ?? '\u25CF'}</span>
								<span class="segment-name">{div.segmentName}</span>
							</div>
							<span
								class="status-badge"
								style="background: {statusColors[div.status]}20; color: {statusColors[div.status]}"
							>
								{div.status.toUpperCase()}
							</span>
						</div>

						<!-- Type and ID -->
						<div class="entry-meta">
							<span class="diversion-type">{div.diversionType.replace(/_/g, ' ')}</span>
							<span class="diversion-id">Seg #{div.segmentId}</span>
						</div>

						<!-- Timing -->
						<div class="entry-timing">
							<span class="time-range">
								{formatTime(div.activateAt)} &mdash; {formatTime(div.deactivateAt)}
							</span>
						</div>

						<!-- Pending: Countdown timer -->
						{#if div.status === 'pending' && countdown > 0}
							<div class="countdown-section">
								<span class="countdown-label">Activates in</span>
								<span class="countdown-value">{formatCountdown(countdown)}</span>
								<div class="countdown-bar-track">
									<div class="countdown-bar-pulse"></div>
								</div>
							</div>
						{/if}

						<!-- Active: Progress bar -->
						{#if div.status === 'active'}
							<div class="progress-section">
								<div class="progress-header">
									<span class="progress-label">Duration elapsed</span>
									<span class="progress-pct">{progress.toFixed(0)}%</span>
								</div>
								<div class="progress-track">
									<div
										class="progress-fill"
										style="width: {progress}%"
									></div>
								</div>
								<span class="time-remaining">
									{formatCountdown(remaining)} remaining
								</span>
							</div>
						{/if}

						<!-- Queue length bar -->
						{#if div.queueLengthM > 0}
							<div class="queue-section">
								<div class="queue-header">
									<span class="queue-label">Queue Length</span>
									<span class="queue-value" style="color: {queueSeverityColor(div.queueLengthM)}">
										{div.queueLengthM.toFixed(0)}m
									</span>
								</div>
								<div class="queue-track">
									<div
										class="queue-fill"
										style="width: {getQueueBarWidth(div.queueLengthM)}%; background: {queueSeverityColor(div.queueLengthM)}"
									></div>
								</div>
							</div>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.diversion-timeline {
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
		background: #0f172a;
		color: #f1f5f9;
		font-size: 0.8125rem;
	}

	/* ─── Header ────────────────────────────────────────────────────────── */
	.timeline-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.timeline-title {
		margin: 0;
		font-size: 0.875rem;
		font-weight: 600;
		color: #f1f5f9;
	}

	.header-stats {
		display: flex;
		gap: 0.375rem;
	}

	.stat-pill {
		padding: 0.125rem 0.5rem;
		border-radius: 9999px;
		background: #1e293b;
		color: #94a3b8;
		font-size: 0.6875rem;
		font-weight: 500;
	}

	.stat-pill.active {
		background: rgba(34, 197, 94, 0.15);
		color: #22c55e;
	}

	/* ─── Empty state ──────────────────────────────────────────────────── */
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.375rem;
		padding: 1.5rem;
		color: #94a3b8;
		text-align: center;
	}

	.empty-icon {
		font-size: 1.5rem;
		opacity: 0.5;
	}

	.empty-sub {
		font-size: 0.6875rem;
		color: #64748b;
	}

	/* ─── Timeline list ────────────────────────────────────────────────── */
	.timeline {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	/* ─── Timeline entry ───────────────────────────────────────────────── */
	.timeline-entry {
		display: flex;
		border-radius: 0.375rem;
		background: #1e293b;
		border: 1px solid #334155;
		overflow: hidden;
		transition: border-color 0.15s ease;
	}

	.timeline-entry.active {
		border-color: rgba(34, 197, 94, 0.4);
		box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.1);
	}

	.timeline-entry.pending {
		border-color: rgba(245, 158, 11, 0.3);
	}

	.timeline-entry.completed {
		opacity: 0.6;
	}

	.accent-bar {
		width: 4px;
		flex-shrink: 0;
	}

	.entry-content {
		flex: 1;
		padding: 0.625rem;
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	/* ─── Entry header ─────────────────────────────────────────────────── */
	.entry-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.entry-title-row {
		display: flex;
		align-items: center;
		gap: 0.375rem;
	}

	.type-icon {
		font-size: 0.875rem;
	}

	.segment-name {
		font-weight: 600;
		font-size: 0.8125rem;
		color: #f1f5f9;
	}

	.status-badge {
		font-size: 0.5625rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		padding: 0.125rem 0.375rem;
		border-radius: 0.2rem;
	}

	/* ─── Meta ─────────────────────────────────────────────────────────── */
	.entry-meta {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.diversion-type {
		font-size: 0.6875rem;
		text-transform: capitalize;
		color: #94a3b8;
	}

	.diversion-id {
		font-size: 0.625rem;
		color: #64748b;
		font-family: monospace;
	}

	/* ─── Timing ───────────────────────────────────────────────────────── */
	.entry-timing {
		display: flex;
		align-items: center;
	}

	.time-range {
		font-size: 0.6875rem;
		font-family: monospace;
		color: #94a3b8;
	}

	/* ─── Countdown (pending) ──────────────────────────────────────────── */
	.countdown-section {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.375rem 0.5rem;
		background: rgba(245, 158, 11, 0.08);
		border-radius: 0.25rem;
	}

	.countdown-label {
		font-size: 0.625rem;
		color: #94a3b8;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.countdown-value {
		font-size: 0.9375rem;
		font-weight: 700;
		font-family: monospace;
		color: #f59e0b;
	}

	.countdown-bar-track {
		flex: 1;
		height: 3px;
		background: #334155;
		border-radius: 2px;
		overflow: hidden;
	}

	.countdown-bar-pulse {
		width: 40%;
		height: 100%;
		background: #f59e0b;
		border-radius: 2px;
		animation: pulse-slide 1.5s ease-in-out infinite;
	}

	@keyframes pulse-slide {
		0% { transform: translateX(-100%); opacity: 0.3; }
		50% { opacity: 1; }
		100% { transform: translateX(250%); opacity: 0.3; }
	}

	/* ─── Progress (active) ────────────────────────────────────────────── */
	.progress-section {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.progress-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.progress-label {
		font-size: 0.625rem;
		color: #94a3b8;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.progress-pct {
		font-size: 0.75rem;
		font-weight: 600;
		font-family: monospace;
		color: #22c55e;
	}

	.progress-track {
		width: 100%;
		height: 5px;
		background: #334155;
		border-radius: 3px;
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		border-radius: 3px;
		background: linear-gradient(90deg, #166534, #22c55e);
		transition: width 1s linear;
	}

	.time-remaining {
		font-size: 0.625rem;
		color: #64748b;
		font-family: monospace;
	}

	/* ─── Queue length ─────────────────────────────────────────────────── */
	.queue-section {
		display: flex;
		flex-direction: column;
		gap: 0.1875rem;
	}

	.queue-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.queue-label {
		font-size: 0.625rem;
		color: #94a3b8;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.queue-value {
		font-size: 0.75rem;
		font-weight: 600;
		font-family: monospace;
	}

	.queue-track {
		width: 100%;
		height: 4px;
		background: #334155;
		border-radius: 2px;
		overflow: hidden;
	}

	.queue-fill {
		height: 100%;
		border-radius: 2px;
		transition: width 0.5s ease;
	}
</style>
