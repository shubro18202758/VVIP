<script lang="ts">
	/**
	 * /comms — the Advisory Desk (operator, command theme).
	 *
	 * Pick an event → the generated citizen advisory side-by-side EN/HI/GU
	 * (same facts, per-audience consequence phrasing: citizens SMS/WhatsApp
	 * preview · zone officers · 108 control · municipal), the template
	 * library per event kind, and the send/draft log as FeedItems labelled
	 * DRAFTED-NOT-DELIVERED — no gateway exists, by design, shown honestly.
	 * Every generation is recorded in the draft log AND surfaced beside the
	 * live decision-audit rail (read from the ops store; the store's public
	 * surface is frozen, so the echo renders here rather than being pushed
	 * into the operator board's stream). Dispatch threads cover every
	 * agency including Jan Rakshak, read live from GET /api/v1/dispatches
	 * with its labelled fallback.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { SUPPORTED, t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import { toasts } from '$lib/shell/toast.svelte';
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import {
		AGENCIES,
		agencyByCode,
		fetchDispatches,
		type DispatchFeedState,
		type DispatchRecord,
	} from '$lib/comms/dispatch-feed';
	import {
		AUDIENCES,
		audienceLabel,
		composeAdvisoryTrilingual,
		composeAllClear,
		templateForKind,
		type AdvisoryDraft,
		type Audience,
	} from '$lib/comms/advisory';
	import { USE_CASES } from '$lib/ops/usecases';
	import '$lib/i18n/strings.citizen';

	const lang = $derived(ops.lang);

	// ── Event pick ────────────────────────────────────────────────────────────
	let selectedId = $state<string | null>(null);
	let events = $derived(ops.events.length ? ops.events : []);
	let selected = $derived(events.find((e) => e.eventId === selectedId) ?? events[0] ?? null);

	// ── Advisory generation ───────────────────────────────────────────────────
	let audience = $state<Audience>('citizens');
	let drafts = $derived(
		selected ? composeAdvisoryTrilingual(selected, audience) : ([] as AdvisoryDraft[]),
	);
	let generatedAt = $state(0);

	// ── Draft log (DRAFTED-NOT-DELIVERED by design) ──────────────────────────
	interface LogEntry {
		id: number;
		ts: number;
		text: string;
		langs: string[];
	}
	let log = $state<LogEntry[]>([]);
	let logSeq = 0;

	// ── Dispatch threads (all agencies incl. Jan Rakshak) ────────────────────
	let feed = $state<DispatchFeedState>({
		dispatches: [],
		source: 'unavailable',
		note: null,
		lastUpdated: 0,
	});
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	async function pollDispatches() {
		try {
			const dispatches = await fetchDispatches();
			feed = { dispatches, source: 'live', note: null, lastUpdated: Date.now() };
		} catch (err) {
			const notDeployed = err instanceof Error && err.name === 'ApiNotDeployed';
			feed = {
				dispatches: [],
				source: notDeployed ? 'not-deployed' : 'unavailable',
				note: err instanceof Error ? err.message : String(err),
				lastUpdated: feed.lastUpdated,
			};
		}
	}

	onMount(() => {
		ops.start();
		void pollDispatches();
		pollTimer = setInterval(() => void pollDispatches(), 10_000);
	});

	onDestroy(() => {
		ops.stop();
		if (pollTimer) clearInterval(pollTimer);
	});

	// R3 wiring: operator surface — lang + band on the shell.
	$effect(() => {
		commandBar.lang = ops.lang;
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSource;
		commandBar.onLangChange = (l: string) => (ops.lang = l);
	});

	function generate() {
		if (!selected) return;
		generatedAt = Date.now();
		log.unshift({
			id: ++logSeq,
			ts: generatedAt,
			text: drafts[0]?.text ?? '',
			langs: drafts.map((d) => d.lang),
		});
		log = log.slice(0, 40);
		toasts.push({
			kind: 'info',
			title: citizenT(lang, 'comms_drafted_badge'),
			body: `${t(lang, `kind_${selected.eventKind}`)} · ${audienceLabel(audience, 'en')}`,
		});
	}

	function draftAllClear() {
		if (!selected) return;
		const langs = SUPPORTED.map((s) => s.code);
		log.unshift({
			id: ++logSeq,
			ts: Date.now(),
			text: composeAllClear(selected, 'en'),
			langs,
		});
		log = log.slice(0, 40);
	}

	function copyDraft(text: string) {
		void navigator.clipboard?.writeText(text).catch(() => undefined);
		toasts.push({ kind: 'info', title: citizenT(lang, 'comms_copied') });
	}

	const eventDispatches = $derived(
		selected
			? feed.dispatches.filter((d) => d.event_id === selected.eventId)
			: feed.dispatches.slice(0, 10),
	);

	const STATUS_ORDER = ['requested', 'approved', 'en_route', 'on_scene', 'standing_down'];

	function statusStep(status: string): number {
		const i = STATUS_ORDER.indexOf(status);
		return i < 0 ? 0 : i;
	}
</script>

<svelte:head>
	<title>Advisory Desk · Veer</title>
</svelte:head>

<div class="comms" data-screen="comms">
	<!-- LEFT: event pick + audiences + templates + agencies ─────────────── -->
	<div class="col left">
		<Panel title={citizenT(lang, 'comms_pick_event')} pad="normal">
			{#if events.length === 0}
				<EmptyState
					title={t(lang, 'opsNoEvents')}
					hint={t(lang, 'opsEventsApiMissing')}
				/>
			{:else}
				<select bind:value={selectedId} data-testid="comms-event-select">
					{#each events as ev (ev.eventId)}
						<option value={ev.eventId}>
							{ev.name} · {ev.source === 'offline-fixture' ? t(lang, 'opsSourceOffline') : t(lang, 'opsSourceLive')}
						</option>
					{/each}
				</select>
				{#if selected}
					<p class="ev-meta">
						{t(lang, `kind_${selected.eventKind}`)} ·
						<span class="mono">{selected.routeSegmentIds.length} {t(lang, 'opsRouteSpine')}</span>
					</p>
				{/if}
			{/if}
		</Panel>

		<Panel title={citizenT(lang, 'comms_audience')}>
			<div class="audiences">
				{#each AUDIENCES as a (a)}
					<button
						class="aud"
						class:active={audience === a}
						onclick={() => (audience = a)}
						data-audience={a}
					>
						{audienceLabel(a, lang)}
					</button>
				{/each}
			</div>
		</Panel>

		<Panel title={citizenT(lang, 'comms_template_title')}>
			<ul class="templates">
				{#each USE_CASES as u (u.id)}
					<li>
						<span class="tpl-name">{t(lang, `kind_${u.eventKind}`)}</span>
						<span class="tpl-kind mono">{u.eventKind}</span>
					</li>
				{/each}
			</ul>
			{#if selected}
				<p class="tpl-preview">{templateForKind(selected.eventKind, lang)}</p>
			{/if}
		</Panel>

		<Panel title="Agencies">
			<ul class="agencies">
				{#each AGENCIES as a (a.code)}
					<li>
						<span class="ag-label">{a.label}</span>
						<span class="ag-native">{a.label_native}</span>
					</li>
				{/each}
			</ul>
		</Panel>
	</div>

	<!-- CENTER: the trilingual advisory, side-by-side ───────────────────── -->
	<div class="col center">
		<Panel
			title={citizenT(lang, 'comms_title')}
			sub={citizenT(lang, 'comms_sub')}
			status={selected ? { state: selected.source === 'offline-fixture' ? 'mixed' : 'live', updatedAt: ops.boardUpdated } : null}
			lang={lang}
		>
			{#if selected}
				<div class="gen-row">
					<Button variant="primary" size="sm" actor="duty-officer" onclick={generate}>
						{generatedAt ? citizenT(lang, 'comms_regenerate') : citizenT(lang, 'comms_generate')}
					</Button>
					<Button variant="ghost" size="sm" onclick={draftAllClear}>
						{citizenT(lang, 'comms_all_clear')}
					</Button>
					{#if generatedAt}
						<span class="gen-at mono">{new Date(generatedAt).toLocaleTimeString()}</span>
					{/if}
				</div>
				<p class="preview-note">{citizenT(lang, 'comms_preview_note')}</p>
				<div class="trilingual" data-testid="comms-trilingual">
					{#each SUPPORTED as s, i (s.code)}
						{@const d = drafts[i]}
						<article class="lang-col" data-lang={s.code}>
							<header>
								<span class="lang-name">{s.label}</span>
								<button class="copy" onclick={() => d && copyDraft(d.text)} title={citizenT(lang, 'comms_copy')}>
									{citizenT(lang, 'comms_copy')}
								</button>
							</header>
							<p class="adv-text" data-adv-lang={s.code}>{d?.text ?? ''}</p>
						</article>
					{/each}
				</div>
				<p class="no-gateway">
					<Badge variant="not-deployed" label={citizenT(lang, 'comms_drafted_badge')} />
					{citizenT(lang, 'comms_no_gateway')}
				</p>
			{:else}
				<EmptyState title={t(lang, 'opsNoEvents')} hint={t(lang, 'startDemo')} />
			{/if}
		</Panel>

		<Panel title={citizenT(lang, 'comms_draft_log')}>
			{#if log.length === 0}
				<EmptyState title={citizenT(lang, 'comms_drafted_badge')} hint={citizenT(lang, 'comms_no_gateway')} />
			{:else}
				<ul class="log">
					{#each log as entry (entry.id)}
						<li>
							<FeedItem
								ts={entry.ts}
								actor="advisory desk"
								actorKind="human"
								level="info"
								source="operator"
								{lang}
								text={entry.text}
							/>
							<span class="log-langs mono">{entry.langs.join(' · ')}</span>
						</li>
					{/each}
				</ul>
				<p class="audit-note">{citizenT(lang, 'comms_audit_echo')}</p>
			{/if}
		</Panel>
	</div>

	<!-- RIGHT: dispatch threads + decision audit rail ───────────────────── -->
	<div class="col right">
		<Panel
			title={citizenT(lang, 'comms_dispatch_threads')}
			status={{ state: feed.source === 'live' ? 'live' : feed.source === 'not-deployed' ? 'not-deployed' : 'stale', updatedAt: feed.lastUpdated }}
			lang={lang}
		>
			{#if feed.source !== 'live'}
				<p class="feed-note" data-testid="comms-dispatch-fallback">
					<Badge variant={feed.source === 'not-deployed' ? 'not-deployed' : 'offline'} label={feed.source === 'not-deployed' ? 'not deployed' : citizenT(lang, 'comms_dispatch_demo')} />
					{#if feed.note}<span class="mono note-text">{feed.note}</span>{/if}
				</p>
			{/if}
			{#if eventDispatches.length === 0}
				<EmptyState title={citizenT(lang, 'comms_dispatch_none')} />
			{:else}
				<ul class="threads">
					{#each eventDispatches as d (d.dispatch_id)}
						{@const ag = agencyByCode(d.agency)}
						<li class="thread" data-thread-status={d.status}>
							<header class="th-head">
								<span class="th-agency">{ag.label} <span class="th-native">{ag.label_native}</span></span>
								<span class="th-unit mono">{d.unit_id}</span>
							</header>
							<ol class="th-steps">
								{#each STATUS_ORDER as st (st)}
									{@const reached = statusStep(d.status) >= STATUS_ORDER.indexOf(st)}
									<li class="th-step" class:reached data-step={st}>{st}</li>
								{/each}
							</ol>
							{#if d.audit && d.audit.length}
								<ul class="th-audit">
									{#each d.audit.slice(0, 4) as step, i (d.dispatch_id + ':' + i)}
										<li>
											<span class="mono th-ts">{new Date(step.at).toLocaleTimeString()}</span>
											<span class="th-actor">{step.actor}</span>
											<span class="th-trans mono">{step.from_status ?? '—'} → {step.to_status}</span>
										</li>
									{/each}
								</ul>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</Panel>

		<Panel title={t(lang, 'auditTitle')} sub={t(lang, 'auditSub')} lang={lang}>
			{#if ops.audit.length === 0}
				<EmptyState title={t(lang, 'auditEmpty')} />
			{:else}
				<ul class="audit-rail">
					{#each ops.audit.slice(0, 12) as row (row.id)}
						<li>
							<FeedItem
								ts={row.ts}
								actor={row.actor}
								actorKind={row.source === 'operator' ? 'human' : 'machine'}
								level={row.level}
								source={row.source}
								{lang}
								text={row.text}
							/>
						</li>
					{/each}
				</ul>
			{/if}
		</Panel>
	</div>
</div>

<style>
	.comms {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: grid;
		grid-template-columns: minmax(240px, 300px) minmax(0, 1.8fr) minmax(280px, 1fr);
		gap: var(--v-gap-panel);
		padding: var(--v-pad-panel);
	}

	@media (max-width: 1400px) {
		.comms {
			grid-template-columns: minmax(240px, 300px) minmax(0, 1.8fr);
		}

		.col.right {
			grid-column: 1 / -1;
		}
	}

	@media (max-width: 1000px) {
		.comms {
			grid-template-columns: 1fr;
		}
	}

	.col {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		min-width: 0;
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	select {
		width: 100%;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2) var(--v-space-3);
		font: inherit;
		font-size: var(--v-text-sm);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
	}

	.ev-meta {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.audiences {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.aud {
		text-align: left;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		background: var(--v-surface-2);
		color: var(--v-ink-2);
		padding: var(--v-space-2) var(--v-space-3);
		font: inherit;
		font-size: var(--v-text-xs);
		cursor: pointer;
	}

	.aud.active {
		border-color: var(--v-line-select);
		color: var(--v-ink-1);
		background: var(--v-surface-3);
	}

	.templates {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.templates li {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
		font-size: var(--v-text-xs);
		padding: var(--v-space-1) 0;
		border-bottom: 1px dashed var(--v-line-1);
	}

	.tpl-kind {
		color: var(--v-ink-4);
	}

	.tpl-preview {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		border-left: 2px solid var(--v-line-2);
		padding-left: var(--v-space-2);
	}

	.agencies {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.agencies li {
		display: flex;
		justify-content: space-between;
		font-size: var(--v-text-xs);
	}

	.ag-native {
		color: var(--v-ink-3);
	}

	.gen-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.gen-at {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.preview-note {
		margin: var(--v-space-2) 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.trilingual {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: var(--v-space-3);
	}

	@media (max-width: 900px) {
		.trilingual {
			grid-template-columns: 1fr;
		}
	}

	.lang-col {
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		background: var(--v-surface-2);
		padding: var(--v-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		min-width: 0;
	}

	.lang-col header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
	}

	.lang-name {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.copy {
		background: none;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
		padding: 2px var(--v-space-2);
		cursor: pointer;
	}

	.adv-text {
		margin: 0;
		font-size: var(--v-text-sm);
		line-height: 1.55;
		color: var(--v-ink-1);
		overflow-wrap: anywhere;
	}

	.no-gateway {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: var(--v-space-3) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.log {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
	}

	.log li {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.log-langs {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		padding-left: var(--v-space-2);
	}

	.audit-note {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.feed-note {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: 0 0 var(--v-space-2);
	}

	.note-text {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.threads {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-3);
	}

	.thread {
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-3);
		background: var(--v-surface-2);
	}

	.th-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-2);
	}

	.th-agency {
		font-size: var(--v-text-sm);
		font-weight: 600;
	}

	.th-native {
		color: var(--v-ink-3);
		font-weight: 400;
		font-size: var(--v-text-xs);
	}

	.th-unit {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.th-steps {
		list-style: none;
		margin: 0 0 var(--v-space-2);
		padding: 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.th-step {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		border: 1px dashed var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
	}

	.th-step.reached {
		color: var(--v-ink-2);
		border-style: solid;
		border-color: var(--v-line-2);
	}

	.thread[data-thread-status='on_scene'] .th-step.reached:last-of-type {
		color: var(--v-band-green);
		border-color: var(--v-band-green);
	}

	.th-audit {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.th-audit li {
		display: flex;
		gap: var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.th-ts {
		color: var(--v-ink-4);
	}

	.th-trans {
		color: var(--v-ink-4);
	}

	.audit-rail {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
	}
</style>
