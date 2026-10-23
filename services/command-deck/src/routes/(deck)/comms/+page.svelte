<script lang="ts">
	/**
	 * /comms — the Advisory Desk (operator, command theme).
	 *
	 * Pick an event → the generated advisory side-by-side EN/HI/GU (same
	 * facts, per-audience consequence phrasing: citizens SMS/WhatsApp
	 * preview · zone officers · 108 control · municipal), the template
	 * library per event kind, and the draft log as FeedItems labelled
	 * DRAFTED-NOT-DELIVERED — no gateway exists, by design, shown honestly.
	 * Drafts live in this tab only: nothing is written to the audit record,
	 * and the page says exactly that.
	 *
	 * Facts come from comms/facts.ts — the same derivation /citizen's public
	 * projection runs (closure plan, else the adaptive advance closure with
	 * the oracle's own provenance — live adaptation or doctrine baseline;
	 * signage road names; road-graph alternate) — so the citizen draft here
	 * reads exactly like the public card, and the facts line under the
	 * drafts shows the officer where the hold length came from (or that a
	 * read failed). Operator-only kinds (pursuit, crime incident, any kind
	 * the oracle reports that is not on the public allowlist) never get a
	 * citizen draft or a citizen all-clear.
	 *
	 * Dispatch threads cover every agency including Jan Rakshak, read live
	 * from GET /api/v1/dispatches, for the selected event or all events, on
	 * the backend's own ladder (requested → approved → en_route → on_scene
	 * → completed; deferred / cancelled as terminal chips).
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { SUPPORTED, kindLabel, localizeNumber, t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import { toasts } from '$lib/shell/toast.svelte';
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import { istClock } from '$lib/primitives/time';
	import {
		AGENCIES,
		DISPATCH_EXITS,
		DISPATCH_LADDER,
		agencyByCode,
		fetchDispatches,
		reachedSteps,
		type DispatchFeedState,
	} from '$lib/comms/dispatch-feed';
	import {
		AUDIENCES,
		audienceLabel,
		composeAdvisoryTrilingual,
		composeAllClearTrilingual,
		holdSourceKey,
		templateForKind,
		type AdvisoryDraft,
		type AdvisoryFacts,
		type Audience,
	} from '$lib/comms/advisory';
	import { factsKey, resolveFacts } from '$lib/comms/facts';
	import { PUBLIC_EVENT_KINDS, isPublicKind } from '$lib/citizen/holds';
	import '$lib/i18n/strings.citizen';

	const lang = $derived(ops.lang);

	// ── Event pick ────────────────────────────────────────────────────────────
	let selectedId = $state<string | null>(null);
	let events = $derived(ops.events.length ? ops.events : []);
	let selected = $derived(events.find((e) => e.eventId === selectedId) ?? null);

	// The select must show what the desk is working on: adopt the first
	// event (and re-adopt when the selected one leaves the board).
	$effect(() => {
		if (events.length && !events.some((e) => e.eventId === selectedId)) {
			selectedId = events[0].eventId;
		}
	});

	function eventLabel(ev: (typeof events)[number]): string {
		const name = ev.name && ev.name !== ev.eventId ? ev.name : ev.eventId.slice(0, 8);
		const src = ev.source === 'offline-fixture' ? t(lang, 'opsSourceOffline') : t(lang, 'opsSourceLive');
		return `${kindLabel(lang, ev.eventKind)} · ${name} · ${src}`;
	}

	// ── Facts (resolved per event; same derivation as /citizen) ──────────────
	// Re-resolved when the event's facts change and every 30 s (the live
	// adaptive advance closure moves with the situation band); a token
	// drops answers that arrive after a newer request.
	let facts = $state<AdvisoryFacts | null>(null);
	let factsTick = $state(0);
	let factsFor = '';
	let factsToken = 0;
	$effect(() => {
		const ev = selected;
		const tick = factsTick;
		if (!ev) {
			facts = null;
			factsFor = '';
			return;
		}
		const key = factsKey(ev);
		if (`${key}|${tick}` === factsFor) return;
		if (!factsFor.startsWith(`${key}|`)) facts = null;
		factsFor = `${key}|${tick}`;
		const token = ++factsToken;
		void resolveFacts(ev).then((f) => {
			if (token === factsToken) facts = f;
		});
	});

	/**
	 * The kind the drafts are written for: the oracle's raw kind once the
	 * facts resolved (an unknown kind is withheld), the board's until then.
	 */
	const draftKind = $derived(
		facts && selected && facts.eventId === selected.eventId ? facts.eventKind : (selected?.eventKind ?? ''),
	);

	// ── Advisory generation ───────────────────────────────────────────────────
	let audience = $state<Audience>('citizens');
	let drafts = $derived(facts ? composeAdvisoryTrilingual(facts, audience) : ([] as AdvisoryDraft[]));
	let generatedAt = $state(0);

	// ── Template library ──────────────────────────────────────────────────────
	let templateKind = $state<string | null>(null);
	const templateKinds = [...PUBLIC_EVENT_KINDS];
	/**
	 * An operator-only kind (pursuit, crime): no citizen draft exists for it,
	 * so GENERATE steps back (ghost, disabled, said why) and the centre
	 * column shows the citizen TEMPLATES preview instead of empty drafts.
	 */
	const operatorOnly = $derived(!!selected && !isPublicKind(draftKind));
	const previewKind = $derived(templateKind ?? (isPublicKind(draftKind) ? draftKind : templateKinds[0]));

	// ── Draft log (this tab only; DRAFTED-NOT-DELIVERED by design) ───────────
	interface LogEntry {
		id: number;
		ts: number;
		texts: Array<{ lang: string; text: string }>;
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
	let threadScope = $state<'event' | 'all'>('event');
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

	let factsTimer: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		ops.start();
		void pollDispatches();
		pollTimer = setInterval(() => void pollDispatches(), 10_000);
		factsTimer = setInterval(() => (factsTick += 1), 30_000);
	});

	onDestroy(() => {
		ops.stop();
		if (pollTimer) clearInterval(pollTimer);
		if (factsTimer) clearInterval(factsTimer);
	});

	// R3 wiring: operator surface — lang + band on the shell.
	$effect(() => {
		commandBar.lang = ops.lang;
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSourceKind === 'none' ? null : ops.bandSource;
		commandBar.onLangChange = (l: string) => (ops.lang = l);
	});

	function pushLog(texts: LogEntry['texts']) {
		log.unshift({ id: ++logSeq, ts: Date.now(), texts });
		log = log.slice(0, 40);
	}

	function generate() {
		if (!selected || !drafts.length) return;
		generatedAt = Date.now();
		pushLog(drafts.map((d) => ({ lang: d.lang, text: d.text })));
		toasts.push({
			kind: 'info',
			title: citizenT(lang, 'comms_drafted_badge'),
			body: `${kindLabel(lang, draftKind)} · ${audienceLabel(audience, lang)}`,
		});
	}

	function draftAllClear() {
		// A citizen all-clear: never for an operator-only kind.
		if (!selected || !isPublicKind(draftKind)) return;
		pushLog(composeAllClearTrilingual(draftKind, selected.emulated === true));
	}

	function copyDraft(text: string) {
		void navigator.clipboard?.writeText(text).catch(() => undefined);
		toasts.push({ kind: 'info', title: citizenT(lang, 'comms_copied') });
	}

	const eventDispatches = $derived(
		selected ? feed.dispatches.filter((d) => d.event_id === selected.eventId) : [],
	);
	const shownDispatches = $derived(
		threadScope === 'all' || !selected ? feed.dispatches.slice(0, 20) : eventDispatches,
	);
	const otherEventCount = $derived(feed.dispatches.length - eventDispatches.length);

	function statusWord(status: string): string {
		const key = `dispatch_status_${status}`;
		const word = citizenT(lang, key);
		return word === key ? status : word;
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
						<option value={ev.eventId}>{eventLabel(ev)}</option>
					{/each}
				</select>
				{#if selected}
					<p class="ev-meta">
						<span>{kindLabel(lang, draftKind)}</span>
						<span class="mono">{selected.routeSegmentIds.length} {t(lang, 'opsRouteSpine')}</span>
						{#if !isPublicKind(draftKind)}
							<span class="withheld-note" data-testid="comms-withheld-badge">{citizenT(lang, 'comms_withheld')}</span>
						{/if}
						{#if selected.emulated}
							<!-- every draft of this event carries the same marker in its text -->
							<span data-testid="comms-emulated-badge"><Badge variant="emulated" label={citizenT(lang, 'citizen_emulated_badge')} title={citizenT(lang, 'adv_emulated_prefix')} /></span>
						{/if}
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
				{#each templateKinds as kind (kind)}
					<li>
						<button
							type="button"
							class="tpl"
							class:active={(templateKind ?? draftKind) === kind}
							onclick={() => (templateKind = kind)}
							data-template-kind={kind}
						>
							<span class="tpl-name">{kindLabel(lang, kind)}</span>
							<span class="tpl-kind mono">{kind}</span>
						</button>
					</li>
				{/each}
			</ul>
			{#if templateKind ?? draftKind}
				<p class="tpl-preview" data-testid="comms-template-preview">
					{templateForKind(templateKind ?? draftKind, lang)}
				</p>
			{:else}
				<p class="tpl-preview">{citizenT(lang, 'comms_template_hint')}</p>
			{/if}
		</Panel>

		<Panel title={citizenT(lang, 'comms_agencies')}>
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
					<Button
						variant={operatorOnly ? 'ghost' : 'primary'}
						size="sm"
						actor="duty-officer"
						onclick={generate}
						disabled={!facts || operatorOnly}
						title={operatorOnly ? citizenT(lang, 'comms_withheld') : undefined}
					>
						<span data-testid="comms-generate" data-demoted={operatorOnly ? 'yes' : 'no'}>
							{generatedAt ? citizenT(lang, 'comms_regenerate') : citizenT(lang, 'comms_generate')}
						</span>
					</Button>
					<Button variant="ghost" size="sm" onclick={draftAllClear} disabled={!isPublicKind(draftKind)}>
						{citizenT(lang, 'comms_all_clear')}
					</Button>
					{#if generatedAt}
						<span class="gen-at mono" data-testid="comms-generated-at">{istClock(generatedAt)} IST</span>
					{/if}
				</div>
				{#if operatorOnly}
					<!-- No citizen draft for an operator-only kind: the citizen templates, previewed. -->
					<section class="tpl-centre" data-testid="comms-template-centre" data-kind={previewKind}>
						<p class="withheld-line">{citizenT(lang, 'comms_withheld')}</p>
						<h4>{citizenT(lang, 'comms_template_title')} · {kindLabel(lang, previewKind)}</h4>
						<div class="trilingual">
							{#each SUPPORTED as s (s.code)}
								<!-- data-template-lang, not data-lang: [data-lang] names the three DRAFT columns -->
								<article class="lang-col" data-template-lang={s.code}>
									<header><span class="lang-name">{s.label}</span></header>
									<p class="adv-text" lang={s.code}>{templateForKind(previewKind, s.code)}</p>
								</article>
							{/each}
						</div>
						<p class="preview-note">{citizenT(lang, 'comms_template_hint')}</p>
					</section>
				{/if}
				<p class="preview-note">{citizenT(lang, 'comms_preview_note')}</p>
				{#if facts && isPublicKind(draftKind)}
					<!-- Where the drafted hold length came from (or that a read failed). -->
					<p
						class="facts-line"
						data-testid="comms-facts"
						data-hold-source={facts.holdSource ?? (facts.unavailable?.hold ? 'unavailable' : 'none')}
					>
						{#if facts.holdMinutes != null}
							<span class="mono">{t(lang, 'freezeMinutes', localizeNumber(facts.holdMinutes, lang))}</span>
							· {citizenT(lang, holdSourceKey(facts.holdSource))}
						{:else}
							{citizenT(lang, facts.unavailable?.hold ? 'card_hold_unavailable' : 'card_hold_unknown')}
						{/if}
						{#if facts.unavailable?.alternate}
							· {citizenT(lang, 'card_alt_unavailable')}
						{/if}
					</p>
				{/if}
				<div class="trilingual" class:demoted={operatorOnly} data-testid="comms-trilingual" data-ready={facts ? 'yes' : 'no'}>
					{#each SUPPORTED as s, i (s.code)}
						{@const d = drafts[i]}
						<article
							class="lang-col"
							data-lang={s.code}
							data-withheld={d?.withheld ? 'yes' : 'no'}
							data-facts={d ? JSON.stringify(d.facts) : ''}
						>
							<header>
								<span class="lang-name">{s.label}</span>
								<button class="copy" onclick={() => d && copyDraft(d.text)} title={citizenT(lang, 'comms_copy')} disabled={!d || d.withheld}>
									{citizenT(lang, 'comms_copy')}
								</button>
							</header>
							<p class="adv-text" class:withheld={d?.withheld} data-adv-lang={s.code} lang={s.code}>{d?.text ?? '…'}</p>
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
				<EmptyState title={citizenT(lang, 'comms_drafted_badge')} hint={citizenT(lang, 'comms_session_only')} />
			{:else}
				<ul class="log">
					{#each log as entry (entry.id)}
						<li>
							{#each entry.texts as tx (tx.lang)}
								<FeedItem
									ts={entry.ts}
									actor="advisory desk"
									actorKind="human"
									level="info"
									source="operator"
									{lang}
									text={tx.text}
								/>
							{/each}
							<span class="log-langs mono">{entry.texts.map((tx) => tx.lang).join(' · ')}</span>
						</li>
					{/each}
				</ul>
				<p class="audit-note">{citizenT(lang, 'comms_session_only')}</p>
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
			<div class="scope" role="group" aria-label={citizenT(lang, 'comms_dispatch_threads')}>
				<button type="button" class:active={threadScope === 'event'} onclick={() => (threadScope = 'event')} data-scope="event">
					{citizenT(lang, 'comms_scope_event')}
				</button>
				<button type="button" class:active={threadScope === 'all'} onclick={() => (threadScope = 'all')} data-scope="all">
					{citizenT(lang, 'comms_scope_all')} <span class="mono">{feed.dispatches.length}</span>
				</button>
			</div>
			{#if feed.source !== 'live'}
				<p class="feed-note" data-testid="comms-dispatch-fallback">
					<Badge variant={feed.source === 'not-deployed' ? 'not-deployed' : 'offline'} label={feed.source === 'not-deployed' ? citizenT(lang, 'comms_not_deployed') : citizenT(lang, 'comms_dispatch_demo')} />
					{#if feed.note}<span class="mono note-text">{feed.note}</span>{/if}
				</p>
			{/if}
			{#if shownDispatches.length === 0}
				<EmptyState title={citizenT(lang, 'comms_dispatch_none')} />
				{#if threadScope === 'event' && otherEventCount > 0}
					<button type="button" class="other-events" onclick={() => (threadScope = 'all')}>
						{citizenT(lang, 'comms_other_events', otherEventCount)} →
					</button>
				{/if}
			{:else}
				<ul class="threads">
					{#each shownDispatches as d (d.dispatch_id)}
						{@const ag = agencyByCode(d.agency)}
						{@const reached = reachedSteps(d)}
						<li class="thread" data-thread-status={d.status}>
							<header class="th-head">
								<span class="th-agency">{ag.label} <span class="th-native">{ag.label_native}</span></span>
								<span class="th-unit mono">{d.unit_id}</span>
							</header>
							<ol class="th-steps">
								{#each DISPATCH_LADDER as st (st)}
									<li class="th-step" class:reached={reached.has(st)} class:current={d.status === st} data-step={st}>
										{statusWord(st)}
									</li>
								{/each}
								{#if (DISPATCH_EXITS as readonly string[]).includes(d.status)}
									<li class="th-step th-exit exit-{d.status}" data-step={d.status}>{statusWord(d.status)}</li>
								{/if}
							</ol>
							{#if d.audit && d.audit.length}
								<ul class="th-audit">
									{#each d.audit.slice(0, 4) as step, i (d.dispatch_id + ':' + i)}
										<li>
											<span class="mono th-ts">{istClock(step.at)} IST</span>
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

	/* the meta line wraps — a long kind name or the withheld note never clips */
	.ev-meta {
		margin: var(--v-space-2) 0 0;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--v-space-1) var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.withheld-note {
		flex-basis: 100%;
		color: var(--v-ink-2);
		border-left: 2px solid var(--v-line-2);
		padding-left: var(--v-space-2);
	}

	.tpl-centre {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		margin: var(--v-space-2) 0;
		padding: var(--v-space-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-panel);
	}

	.tpl-centre h4 {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.withheld-line {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	/* the drafts of an operator-only kind are withheld text — kept, but quiet */
	.trilingual.demoted {
		opacity: 0.55;
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
		border-bottom: 1px dashed var(--v-line-1);
	}

	.tpl {
		display: flex;
		width: 100%;
		justify-content: space-between;
		gap: var(--v-space-2);
		background: none;
		border: none;
		color: var(--v-ink-2);
		font: inherit;
		font-size: var(--v-text-xs);
		padding: var(--v-space-1) 0;
		cursor: pointer;
		text-align: left;
	}

	.tpl.active {
		color: var(--v-ink-1);
	}

	.tpl:focus-visible,
	.scope button:focus-visible,
	.other-events:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.tpl-kind {
		color: var(--v-ink-3);
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
		color: var(--v-ink-3);
	}

	.preview-note {
		margin: var(--v-space-2) 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.facts-line {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
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
		color: var(--v-ink-3);
		padding-left: var(--v-space-2);
	}

	.audit-note {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.feed-note {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: 0 0 var(--v-space-2);
	}

	.note-text {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
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
		color: var(--v-ink-3);
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
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 1px var(--v-space-2);
	}

	.th-step.reached {
		color: var(--v-ink-2);
		border-style: solid;
		border-color: var(--v-line-2);
	}

	.th-step.current {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
	}

	.thread[data-thread-status='completed'] .th-step.current {
		color: var(--v-band-green);
		border-color: var(--v-band-green);
	}

	.th-exit {
		border-style: solid;
	}

	.th-exit.exit-deferred {
		color: var(--v-band-amber);
		border-color: var(--v-band-amber);
	}

	.th-exit.exit-cancelled {
		color: var(--v-band-red);
		border-color: var(--v-band-red);
	}

	.scope {
		display: inline-flex;
		margin-bottom: var(--v-space-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		overflow: hidden;
	}

	.scope button {
		background: transparent;
		border: none;
		border-right: 1px solid var(--v-line-1);
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
		padding: var(--v-space-1) var(--v-space-2);
		cursor: pointer;
	}

	.scope button:last-child {
		border-right: none;
	}

	.scope button.active {
		background: var(--v-surface-3);
		color: var(--v-ink-1);
	}

	.other-events {
		margin-top: var(--v-space-2);
		background: none;
		border: none;
		padding: 0;
		color: var(--v-data);
		font-size: var(--v-text-xs);
		cursor: pointer;
	}

	.adv-text.withheld {
		color: var(--v-ink-3);
		font-style: italic;
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
		color: var(--v-ink-3);
	}

	.th-trans {
		color: var(--v-ink-3);
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
