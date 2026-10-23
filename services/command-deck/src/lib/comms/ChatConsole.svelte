<script lang="ts">
	/**
	 * ChatConsole — the rebuilt /chat agent console on the design system.
	 *
	 * Real NDJSON streamChat wiring (POST /api/convoy/chat/stream, contract
	 * C-D, via $lib/api/client) on FeedItem primitives: tool calls render as
	 * --v-tool cards (server durations only), reasoning is a toggle with the
	 * tier that produced each step, and the finished turn carries its
	 * tiers_used chips (decider · Gemma · heuristic) plus any operator
	 * alerts. Answers render as PROSE: a brain that still streams a raw JSON
	 * envelope gets its prose field extracted, or an honest "no prose answer"
	 * note with the envelope verbatim — never raw JSON passed off as an
	 * answer.
	 *
	 * Context: the SELECTED EVENT from the ops store scopes every turn
	 * (event_id — contract C-D2 — plus movement_id + protection class on the
	 * wire), and the brain's done.data.session_id carries follow-ups in the
	 * same session until the event changes or the console is cleared. The
	 * CONTEXT line names the event (plate-masked), never its raw UUID. When
	 * the brain is down the console shows its labelled degradation chip
	 * instead of a spinner.
	 *
	 * Agentic God's-eye (C-D2): suggestion chips start the common questions;
	 * every answer token, thought and tool summary prints through ops.text()
	 * (plates masked unless a duty officer is named); the URNs the tools
	 * named come back as citation chips (select + fly on City Command); the
	 * agent's canvas directives queue on the canvas bus and show here as
	 * 'Open on map' chips — a human applies them on the canvas, never here.
	 *
	 * Svelte 5 note: `messages` is a deep $state proxy — an object pushed
	 * into it is COPIED into a proxy, so streamed tokens must be written to
	 * the proxied entry (messages[i]), never to the original literal.
	 */
	import { onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { ops } from '$lib/ops/store.svelte';
	import type { CorridorEvent } from '$lib/ops/types';
	import type { VvipClass } from '$lib/types';
	import { kindLabel, t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import '$lib/i18n/packs.agent';
	import { streamChat, type ChatCitation, type ChatDirective, type ChatDone, type ChatTier } from '$lib/api/client';
	import { movementIdOf } from '$lib/comms/facts';
	import { canvasBus } from '$lib/gods-eye/canvas-bus.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';

	interface ToolCall {
		callId: string;
		toolName: string;
		state: 'pending' | 'running' | 'success' | 'error';
		durationMs: number | null;
		summary: string | null;
	}

	interface Thought {
		stepIndex: number;
		text: string;
		tier: ChatTier | null;
	}

	interface Message {
		id: number;
		role: 'user' | 'agent';
		text: string;
		thoughts: Thought[];
		toolCalls: ToolCall[];
		/** done.data of the finished turn (null while streaming / on error). */
		done: ChatDone | null;
		error: string | null;
		/** Canvas directives the turn proposed (queued on the canvas bus). */
		directives: ChatDirective[];
	}

	/** One suggestion chip: the label shown, the question sent. */
	interface Suggestion {
		key: string;
		label: string;
		message: string;
	}

	let { lang = 'en' }: { lang?: string } = $props();

	let messages = $state<Message[]>([]);
	let draft = $state('');
	let streaming = $state(false);
	let degraded = $state(false);
	let showThoughts = $state(true);
	let showTools = $state(true);
	let scrollEl: HTMLDivElement | undefined = $state();
	/** Brain session carried across follow-ups (done.data.session_id). */
	let sessionId = $state<string | null>(null);
	/** The event the session was opened for — a new event starts a new one. */
	let sessionEventId: string | null = null;
	let controller: AbortController | null = null;

	let seq = 0;
	let selected = $derived(ops.selectedEvent);

	// Follow the reply as it streams (tokens, thoughts and tool cards).
	$effect(() => {
		const last = messages[messages.length - 1];
		void messages.length;
		void last?.text.length;
		void last?.thoughts.length;
		void last?.toolCalls.length;
		requestAnimationFrame(() => {
			if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
		});
	});

	onDestroy(() => controller?.abort());

	const VVIP_CLASSES: readonly VvipClass[] = ['Z+', 'Z', 'Y', 'X'];

	function vvipClassOf(ev: CorridorEvent | null): VvipClass | null {
		const cls = ev?.protectionClass;
		return cls && (VVIP_CLASSES as readonly string[]).includes(cls) ? (cls as VvipClass) : null;
	}

	/** The selected event as this console may name it (plates masked). */
	function eventName(ev: CorridorEvent): string {
		return ev.name && ev.name !== ev.eventId ? ops.text(ev.name) : kindLabel(lang, ev.eventKind);
	}

	/**
	 * Suggestion chips (3-4): about the selected event when there is one,
	 * else the city's common questions. The label is localised; the question
	 * sent stays in the brain's working language.
	 */
	let suggestions = $derived.by((): Suggestion[] => {
		const ev = selected;
		if (ev) {
			const out: Suggestion[] = [
				{ key: 'traffic', label: t(lang, 'chat_suggest_traffic'), message: 'Live traffic on this corridor — which segments are slowest right now?' },
			];
			if (ev.eventKind === 'vehicle_pursuit') {
				out.push({ key: 'interception', label: t(lang, 'chat_suggest_interception'), message: 'What is the interception plan for this pursuit?' });
			}
			out.push(
				{ key: 'closures', label: t(lang, 'chat_suggest_closures'), message: 'What is the status of this corridor and its closures?' },
				{ key: 'map', label: t(lang, 'chat_suggest_map'), message: 'Show this event on the map' },
			);
			return out.slice(0, 4);
		}
		return [
			{ key: 'stolen', label: t(lang, 'chat_suggest_stolen'), message: 'Show me the stolen vehicle on the map' },
			{ key: 'events', label: t(lang, 'chat_suggest_events'), message: 'Which corridor events are active right now?' },
			{ key: 'units', label: t(lang, 'chat_suggest_units'), message: 'Where are the agency units right now?' },
		];
	});

	/** A cited entity on City Command: select + fly when its canvas mounts. */
	function openCitation(c: ChatCitation) {
		canvasBus.requestSelection(c.urn);
		void goto('/gods-eye');
	}

	/** A proposed canvas move waits as an AI-suggests chip on City Command. */
	function openDirective() {
		void goto('/gods-eye');
	}

	function ask(s: Suggestion) {
		if (streaming) return;
		draft = s.message;
		send(s.label);
	}

	function send(shown: string | null = null) {
		const text = draft.trim();
		if (!text || streaming) return;
		const ev = selected;
		if ((ev?.eventId ?? null) !== sessionEventId) {
			sessionId = null;
			sessionEventId = ev?.eventId ?? null;
		}

		messages.push({ id: ++seq, role: 'user', text: shown ?? text, thoughts: [], toolCalls: [], done: null, error: null, directives: [] });
		messages.push({ id: ++seq, role: 'agent', text: '', thoughts: [], toolCalls: [], done: null, error: null, directives: [] });
		// The PROXIED entry — writes to it re-render; writes to the pushed
		// literal would be invisible (the reply bubble used to stay empty).
		const r = messages[messages.length - 1];
		draft = '';
		streaming = true;
		degraded = false;
		controller?.abort();
		const ctrl = new AbortController();
		controller = ctrl;

		streamChat(
			text,
			ev ? movementIdOf(ev) : null,
			vvipClassOf(ev),
			{
				onToken: (tok) => (r.text += tok),
				onDirective: (d) => {
					r.directives.push(d);
					canvasBus.offer(d, null);
				},
				onThought: (s) => r.thoughts.push({ stepIndex: s.stepIndex, text: s.text, tier: s.tier }),
				onToolCall: (c) => {
					const open = r.toolCalls.find(
						(tc) => tc.callId === c.callId && (tc.state === 'pending' || tc.state === 'running'),
					);
					if (open) open.state = c.state;
					else
						r.toolCalls.push({
							callId: c.callId,
							toolName: c.toolName,
							state: c.state,
							durationMs: null,
							summary: null,
						});
				},
				onToolResult: (res) => {
					const tc =
						r.toolCalls.findLast(
							(x) => x.callId === res.callId && (x.state === 'pending' || x.state === 'running'),
						) ?? r.toolCalls.findLast((x) => x.callId === res.callId);
					if (tc) {
						tc.state = res.state;
						tc.durationMs = res.durationMs;
						tc.summary = res.summary;
					} else {
						r.toolCalls.push({
							callId: res.callId,
							toolName: res.toolName ?? res.callId,
							state: res.state,
							durationMs: res.durationMs,
							summary: res.summary,
						});
					}
				},
				onDone: (d) => {
					r.done = d;
					if (d.sessionId) sessionId = d.sessionId;
					streaming = false;
				},
				onError: (e) => {
					if (ctrl.signal.aborted) return;
					r.error = e.message;
					streaming = false;
					degraded = true;
				},
			},
			{ sessionId, signal: ctrl.signal, eventId: ev?.eventId ?? null },
		).catch((e) => {
			// streamChat rejects outright when fetch itself fails (proxy
			// down / request aborted) — the callbacks never run, so the
			// console must clear its streaming state here too.
			if (ctrl.signal.aborted) return;
			r.error = e instanceof Error ? e.message : String(e);
			streaming = false;
			degraded = true;
		});
	}

	function clearConsole() {
		controller?.abort();
		controller = null;
		messages = [];
		sessionId = null;
		sessionEventId = null;
		streaming = false;
		degraded = false;
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

	function tierLabel(tier: string): string {
		if (tier === 'decider') return citizenT(lang, 'chat_tier_decider');
		if (tier === 'gemma') return citizenT(lang, 'chat_tier_gemma');
		if (tier === 'heuristic') return citizenT(lang, 'chat_tier_heuristic');
		return tier;
	}

	/** "decider:pplx-decider-v1-27b" → {tier:'decider', model:'pplx-…'}. */
	function parseTier(entry: string): { tier: string; model: string | null } {
		const i = entry.indexOf(':');
		return i < 0 ? { tier: entry, model: null } : { tier: entry.slice(0, i), model: entry.slice(i + 1) || null };
	}

	const PROSE_FIELDS = ['answer', 'final_answer', 'response', 'reasoning', 'summary', 'message', 'text'];

	/**
	 * The answer as prose. Contract C-D streams prose tokens; an older brain
	 * streams its JSON envelope instead — take its prose field, else say
	 * there is none and keep the envelope verbatim (never shown as prose).
	 */
	function answerOf(m: Message): { prose: string | null; raw: string | null; action: string | null; pending: boolean } {
		const text = m.text.trim();
		if (!text) return { prose: null, raw: null, action: null, pending: false };
		if (!text.startsWith('{')) return { prose: m.text, raw: null, action: null, pending: false };
		const finished = m.done !== null || m.error !== null;
		try {
			const obj = JSON.parse(text) as unknown;
			const rec = obj && typeof obj === 'object' && !Array.isArray(obj) ? (obj as Record<string, unknown>) : {};
			const field = PROSE_FIELDS.find((k) => typeof rec[k] === 'string' && (rec[k] as string).trim());
			const action = typeof rec.action === 'string' && rec.action !== 'call_tool' ? rec.action : null;
			return { prose: field ? String(rec[field]) : null, raw: text, action, pending: false };
		} catch {
			// Mid-stream a JSON envelope is not parseable yet: wait for it.
			return finished
				? { prose: m.text, raw: null, action: null, pending: false }
				: { prose: null, raw: null, action: null, pending: true };
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
			<span class="ctx-event" title={selected.eventId} data-testid="chat-context-event">
				{kindLabel(lang, selected.eventKind)} · <b>{eventName(selected)}</b>
				{#if selected.state}· <span class="mono">{selected.state}</span>{/if}
				{#if vvipClassOf(selected)}
					· <span class="mono">{vvipClassOf(selected)}</span>
				{/if}
			</span>
		{:else}
			<span class="ctx-none">{citizenT(lang, 'chat_context_none')}</span>
		{/if}
		{#if sessionId}
			<span class="ctx-session mono" data-testid="chat-session" title={sessionId}>
				{citizenT(lang, 'chat_session')} {sessionId.slice(0, 8)}
			</span>
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
			<Button variant="ghost" size="sm" onclick={clearConsole}>{citizenT(lang, 'chat_clear')}</Button>
		{/if}
	</div>

	<div class="stream" bind:this={scrollEl} data-testid="chat-stream">
		{#if messages.length === 0}
			<EmptyState title={citizenT(lang, 'chat_title')} hint={citizenT(lang, 'chat_placeholder')} />
			<div class="suggest" data-testid="chat-suggestions">
				<span class="suggest-label">{t(lang, 'chat_suggest_title')}</span>
				{#each suggestions as s (s.key)}
					<button type="button" class="suggest-chip" data-suggest={s.key} onclick={() => ask(s)}>{s.label}</button>
				{/each}
			</div>
		{:else}
			{#each messages as m (m.id)}
				{@const answer = m.role === 'agent' ? answerOf(m) : null}
				<article class="msg role-{m.role}" data-msg-role={m.role}>
					<span class="who">{m.role === 'user' ? citizenT(lang, 'chat_you') : citizenT(lang, 'chat_agent')}</span>

					{#if m.role === 'agent' && showThoughts && m.thoughts.length}
						<details class="thoughts">
							<summary>{citizenT(lang, 'chat_thoughts')} · <span class="mono">{m.thoughts.length}</span></summary>
							{#each m.thoughts as th, i (i)}
								<p class="thought">
									<span class="mono idx">{th.stepIndex}</span>
									{#if th.tier}<span class="tier-tag mono" data-tier={th.tier}>{tierLabel(th.tier)}</span>{/if}
									{ops.text(th.text)}
								</p>
							{/each}
						</details>
					{/if}

					{#if m.role === 'agent' && showTools && m.toolCalls.length}
						<ul class="tools">
							{#each m.toolCalls as tc, i (i)}
								<li class="tool-card state-{tc.state}" data-tool-state={tc.state} data-testid="chat-tool-card">
									<span class="tool-name mono">{tc.toolName}</span>
									<span class="tool-state">{stateLabel(tc.state)}</span>
									{#if tc.durationMs != null}
										<span class="tool-dur mono">{tc.durationMs} ms</span>
									{/if}
									{#if tc.summary}
										<span class="tool-summary">{ops.text(tc.summary)}</span>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}

					{#if m.role === 'user'}
						<p class="body">{ops.text(m.text)}</p>
					{:else if answer?.prose}
						{#if answer.action}
							<span class="action-tag mono">{citizenT(lang, 'chat_action')} · {answer.action}</span>
						{/if}
						<p class="body prose" data-testid="chat-answer">{ops.text(answer.prose)}</p>
					{:else if answer?.raw}
						<p class="no-prose" data-testid="chat-no-prose">{citizenT(lang, 'chat_raw_envelope')}</p>
						<pre class="raw mono">{ops.text(answer.raw)}</pre>
					{:else if streaming && m === messages[messages.length - 1]}
						<p class="body mono dim">…</p>
					{/if}

					{#if m.role === 'agent' && m.directives.length}
						<div class="directives" data-testid="chat-directives">
							<span class="tiers-label">{t(lang, 'chat_directives')}</span>
							{#each m.directives as d (d.id)}
								<span class="directive-chip" data-verb={d.verb}>
									<span class="mono">{t(lang, `agent_verb_${d.verb}`)}</span>
									{ops.text(d.label.replace(/^(track|fly to|highlight|note on|sensor|layer)\s+/i, ''))}
									{#if d.provenance.tier}<span class="tier-model"> · {d.provenance.tier}</span>{/if}
									<button type="button" class="link-btn" data-testid="chat-open-map" onclick={openDirective}>{t(lang, 'agent_open_map')} →</button>
								</span>
							{/each}
						</div>
					{/if}

					{#if m.role === 'agent' && m.done && m.done.citations.length}
						<div class="citations" data-testid="chat-citations">
							<span class="tiers-label">{t(lang, 'chat_cited')}</span>
							{#each m.done.citations as c (c.urn)}
								<button type="button" class="cite-chip" data-urn={c.urn} title={`${c.urn} — ${t(lang, 'chat_open_city')}`} onclick={() => openCitation(c)}>
									{ops.text(c.label)}
								</button>
							{/each}
						</div>
					{/if}

					{#if m.role === 'agent' && m.done && (m.done.tiersUsed.length || m.done.degraded)}
						<div class="tiers" data-testid="chat-tiers">
							<span class="tiers-label">{citizenT(lang, 'chat_tiers')}</span>
							{#each m.done.tiersUsed as entry, i (i)}
								{@const tier = parseTier(entry)}
								<span class="tier-chip mono" data-tier={tier.tier} title={tier.model ?? tier.tier}>
									{tierLabel(tier.tier)}{#if tier.model}<span class="tier-model"> · {tier.model}</span>{/if}
								</span>
							{/each}
							{#if m.done.degraded}
								<Badge variant="stale" label={citizenT(lang, 'chat_degraded_turn')} />
							{/if}
						</div>
					{/if}

					{#if m.role === 'agent' && m.done && m.done.operatorAlerts.length}
						<ul class="alerts" aria-label={citizenT(lang, 'chat_alerts')}>
							{#each m.done.operatorAlerts as alert, i (i)}
								<li>{ops.text(alert)}</li>
							{/each}
						</ul>
					{/if}

					{#if m.role === 'agent' && m.error}
						<p class="msg-error mono">{ops.text(m.error)}</p>
					{/if}
				</article>
			{/each}
		{/if}
	</div>

	{#if messages.length > 0 && !streaming}
		<div class="suggest compact" data-testid="chat-suggestions">
			{#each suggestions as s (s.key)}
				<button type="button" class="suggest-chip" data-suggest={s.key} onclick={() => ask(s)}>{s.label}</button>
			{/each}
		</div>
	{/if}

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

	.ctx-session {
		margin-left: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: 0 var(--v-space-2);
	}

	.tier-tag {
		font-size: var(--v-text-2xs);
		color: var(--v-tool);
		margin-right: var(--v-space-1);
	}

	.tool-summary {
		flex-basis: 100%;
		color: var(--v-ink-2);
	}

	.tool-card {
		flex-wrap: wrap;
	}

	.action-tag {
		align-self: flex-start;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 0 var(--v-space-2);
	}

	.body.prose {
		color: var(--v-ink-1);
	}

	.no-prose {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.raw {
		margin: 0;
		max-height: 12rem;
		overflow: auto;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2);
	}

	.tiers {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--v-space-1);
		margin-top: var(--v-space-1);
	}

	.tiers-label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-4);
	}

	.tier-chip {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border: 1px solid color-mix(in srgb, var(--v-tool) 45%, transparent);
		border-radius: var(--v-radius-pill);
		padding: 0 var(--v-space-2);
	}

	.tier-model {
		color: var(--v-ink-4);
	}

	.alerts {
		margin: var(--v-space-1) 0 0;
		padding-left: var(--v-space-4);
		font-size: var(--v-text-xs);
		color: var(--v-band-amber);
	}

	.msg-error {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
		overflow-wrap: anywhere;
	}

	.suggest {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: center;
		gap: var(--v-space-2);
		margin-top: var(--v-space-3);
	}

	.suggest.compact {
		justify-content: flex-start;
		margin-top: 0;
	}

	.suggest-label {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-4);
	}

	.suggest-chip {
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: color-mix(in srgb, var(--v-tool) 7%, transparent);
		border: 1px solid color-mix(in srgb, var(--v-tool) 45%, transparent);
		border-radius: var(--v-radius-pill);
		padding: var(--v-space-1) var(--v-space-3);
		cursor: pointer;
	}

	.suggest-chip:hover {
		border-color: var(--v-tool);
	}

	.suggest-chip:focus-visible,
	.cite-chip:focus-visible,
	.link-btn:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.citations,
	.directives {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--v-space-1);
		margin-top: var(--v-space-1);
	}

	.cite-chip {
		font: inherit;
		font-size: var(--v-text-2xs);
		color: var(--v-data);
		background: color-mix(in srgb, var(--v-data) 8%, transparent);
		border: 1px solid color-mix(in srgb, var(--v-data) 45%, transparent);
		border-radius: var(--v-radius-chip);
		padding: 0 var(--v-space-2);
		cursor: pointer;
		max-width: 26ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.cite-chip:hover {
		border-color: var(--v-data);
	}

	.directive-chip {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-1);
		border: 1px solid var(--v-tool);
		border-left: 3px solid var(--v-tool);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
	}

	.link-btn {
		font: inherit;
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
		background: transparent;
		border: none;
		padding: 0 0 0 var(--v-space-1);
		cursor: pointer;
		text-decoration: underline;
		text-underline-offset: 2px;
	}
</style>
