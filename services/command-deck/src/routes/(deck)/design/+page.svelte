<script lang="ts">
	/**
	 * /design — the living style sheet. Every primitive mounts here; both
	 * themes preview side by side (data-theme applies to any subtree);
	 * band cycling exercises the 600ms wipe and BLACK's breathing hairline.
	 * tests/design-smoke.mjs asserts against this page.
	 */
	import { SUPPORTED, t } from '$lib/i18n/strings';
	import type { Band } from '$lib/design/tokens';
	import Panel from '$lib/primitives/Panel.svelte';
	import PanelShell from '$lib/primitives/PanelShell.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import DataTile from '$lib/primitives/DataTile.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import AuditRow from '$lib/primitives/AuditRow.svelte';
	import MapLegend, { type LegendItem } from '$lib/primitives/MapLegend.svelte';
	import LayerRail from '$lib/primitives/LayerRail.svelte';
	import Modal from '$lib/primitives/Modal.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import BandMasthead from '$lib/primitives/BandMasthead.svelte';
	import DecisionCard from '$lib/primitives/DecisionCard.svelte';
	import EventDossier from '$lib/primitives/EventDossier.svelte';
	import { toasts } from '$lib/shell/toast.svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';

	$effect(() => {
		commandBar.lang = 'en';
	});

	const now = Date.now();

	let demoBand = $state<Band>('GREEN');
	let dwell = $state(3);
	const bands: Band[] = ['GREEN', 'AMBER', 'RED', 'BLACK'];

	let modalOpen = $state(false);
	let lang = $state('en');

	const legendItems: LegendItem[] = [
		{ id: 'corridor-events', label: 'Corridor events', colorToken: '--v-band-amber', state: 'on', hint: 'live' },
		{ id: 'vehicle-traces', label: 'Vehicle traces', colorToken: '--v-data', state: 'on', hint: 'live' },
		{ id: 'signal-network', label: 'Signal network', colorToken: '--v-tool', state: 'off', hint: 'live' },
		{ id: 'agency-units', label: 'Agency units', colorToken: '--v-band-green', state: 'on', hint: 'live' },
		{ id: 'cameras', label: 'Cameras', colorToken: '--v-ink-3', state: 'not-deployed', hint: 'not deployed (404)' }
	];

	let legendState = $state(legendItems);

	function cycleBand(next: Band) {
		demoBand = next;
	}

	function fireToast(kind: 'info' | 'act' | 'error') {
		toasts.push({
			kind,
			title: kind === 'act' ? 'Decision approved' : kind === 'error' ? 'Held at baseline' : 'Share link copied',
			body: kind === 'error' ? 'twin_not_calibrated · signal plan' : 'PI-2207 · GJ27EM9001'
		});
	}
</script>

<svelte:head><title>Design sheet · Veer</title></svelte:head>

<div class="page">
	<header class="intro">
		<h1>Design sheet</h1>
		<p>
			One token contract, two themes (command graphite / daylight paper), one motion curve.
			Keyboard: <kbd>/</kbd> focuses the command bar search, <kbd>Esc</kbd> clears.
		</p>
		<div class="lang" role="group" aria-label="language">
			{#each SUPPORTED as s (s.code)}
				<button type="button" class:active={lang === s.code} onclick={() => (lang = s.code)}>
					{s.label}
				</button>
			{/each}
		</div>
	</header>

	<section class="sec" data-design="themes">
		<h2>Themes — one variable list</h2>
		<div class="theme-row">
			<div class="theme-box" data-theme="command" data-theme-preview="command">
				<h3>command · operator</h3>
				<div class="swatches">
					<span class="sw" style="background: var(--v-canvas)"><i>canvas</i></span>
					<span class="sw" style="background: var(--v-surface-1)"><i>surface-1</i></span>
					<span class="sw" style="background: var(--v-surface-2)"><i>surface-2</i></span>
					<span class="sw" style="background: var(--v-surface-3)"><i>surface-3</i></span>
					<span class="sw" style="background: var(--v-act)"><i>act</i></span>
					<span class="sw" style="background: var(--v-data)"><i>data</i></span>
					<span class="sw" style="background: var(--v-tool)"><i>tool</i></span>
					<span class="sw line" style="background: var(--v-line-2)"><i>line-2</i></span>
				</div>
				<p class="mono">13px base · JetBrains Mono for every mutable number</p>
			</div>
			<div class="theme-box" data-theme="daylight" data-theme-preview="daylight">
				<h3>daylight · citizen</h3>
				<div class="swatches">
					<span class="sw" style="background: var(--v-canvas)"><i>canvas</i></span>
					<span class="sw" style="background: var(--v-paper)"><i>paper</i></span>
					<span class="sw" style="background: var(--v-surface-1)"><i>surface-1</i></span>
					<span class="sw" style="background: var(--v-surface-2)"><i>surface-2</i></span>
					<span class="sw" style="background: var(--v-act)"><i>act</i></span>
					<span class="sw" style="background: var(--v-band-green)"><i>green</i></span>
					<span class="sw" style="background: var(--v-band-red)"><i>red</i></span>
					<span class="sw line" style="background: var(--v-line-2)"><i>line-2</i></span>
				</div>
				<p class="mono">16px base · calm strips · citizens never see BLACK</p>
			</div>
		</div>
	</section>

	<section class="sec">
		<h2>Band masthead — hysteresis dwell, then the 600ms wipe</h2>
		<BandMasthead
			band={demoBand}
			posture="{demoBand} — posture sentence for the city, one line"
			candidate={{ band: 'RED', ticks: dwell, required: 4 }}
			bandSource="situation_log"
			{lang}
		/>
		<div class="row">
			{#each bands as b (b)}
				<Button variant={b === demoBand ? 'secondary' : 'ghost'} size="sm" onclick={() => cycleBand(b)}>
					{b}
				</Button>
			{/each}
			<Button variant="ghost" size="sm" onclick={() => (dwell = (dwell % 4) + 1)}>
				tick {dwell}/4
			</Button>
			<span class="mono note">BLACK breathes (2s hairline, the only breathing element)</span>
		</div>
	</section>

	<section class="sec">
		<h2>Counters — DataTiles</h2>
		<div class="tiles">
			<DataTile label="active events" value="3" source={{ variant: 'live', label: 'live' }} />
			<DataTile label="pending human decisions" value="2" source={{ variant: 'act', label: 'pending' }} />
			<DataTile label="units dispatched" value="5" unit="units" source={{ variant: 'fixture', label: 'mixed' }} />
			<DataTile label="ambulance windows" value="1" source={{ variant: 'emulated', label: 'emulated' }} />
		</div>
	</section>

	<section class="sec two-col">
		<div>
			<h2>Panels &amp; badges</h2>
			<Panel title="Panel" sub="renders only what it is given" status={{ state: 'live', updatedAt: now }} {lang}>
				<div class="badges">
					<Badge variant="live" />
					<Badge variant="offline" />
					<Badge variant="fixture" />
					<Badge variant="emulated" />
					<Badge variant="not-deployed" />
					<Badge variant="stale" />
					<Badge variant="pending" />
					<Badge variant="approved" />
					<Badge variant="rejected" />
					<Badge variant="deferred" />
					<Badge variant="act" />
					<Badge variant="neutral" label="neutral" />
					<Badge variant="band" band="GREEN" />
					<Badge variant="band" band="AMBER" />
					<Badge variant="band" band="RED" />
					<Badge variant="band" band="BLACK" />
				</div>
			</Panel>
			<PanelShell title="PanelShell" variant="raised" {lang}>
				<p class="mono">PanelShell = Panel (alias)</p>
			</PanelShell>
			<EmptyState title="Nothing here — honestly" hint="Every fixture-fed surface says so." />
		</div>
		<div>
			<h2>Buttons — commit needs a named actor</h2>
			<div class="row">
				<Button variant="primary" size="sm">commit (locked)</Button>
				<Button variant="primary" size="sm" actor="PI-2207">commit · PI-2207</Button>
				<Button variant="secondary" size="sm">secondary</Button>
				<Button variant="ghost" size="sm">ghost</Button>
				<Button variant="danger-outline" size="sm">reject</Button>
				<Button variant="secondary" size="sm" busy>busy</Button>
				<Button variant="primary" size="citizen" actor="108-driver">request corridor</Button>
			</div>
			<h2>Band chips — BLACK is inverted, never a void</h2>
			<div class="row">
				<BandChip band="GREEN" />
				<BandChip band="AMBER" />
				<BandChip band="RED" />
				<BandChip band="BLACK" />
				<BandChip band="RED" hollow />
			</div>
		</div>
	</section>

	<section class="sec">
		<h2>Audit ledger — human flashes, machines sit in dim mono</h2>
		<div class="feed">
			<FeedItem
				ts={now - 4000}
				actor="PI-2207"
				actorKind="human"
				level="decision"
				text="Approved e-challan GJ27EM9001 — 112 km/h in a 60 segment"
				detail={{ plate: 'GJ27EM9001', rule: 'R_SPD_SEGMENT', evidence: 'emulated_anpr' }}
				{lang}
			/>
			<AuditRow
				ts={now - 9000}
				actor="adaptive"
				actorKind="machine"
				level="band"
				text="band GREEN→AMBER · floor applied · hysteresis n=4"
				source="audit-poll"
				{lang}
			/>
			<AuditRow
				ts={now - 15000}
				actor="mcp:interception_plan"
				actorKind="machine"
				level="tool"
				text="ranked chokes 1→3 (toll 1.0 / underpass 0.4)"
				source="ndjson-chat"
				{lang}
			/>
			<FeedItem ts={now - 21000} actor="gemma" actorKind="machine" level="model" text="reasoning trace (dim mono)" {lang} />
			<FeedItem
				ts={now - 30000}
				actor="signals"
				actorKind="machine"
				level="error"
				text="apply refused — twin_not_calibrated · held at baseline"
				source="audit-poll"
				{lang}
			/>
		</div>
	</section>

	<section class="sec two-col">
		<div>
			<h2>Layer rail — honest layer states</h2>
			<LayerRail
				items={legendState}
				onToggle={(id) =>
					(legendState = legendState.map((i) =>
						i.id === id && i.state !== 'not-deployed'
							? { ...i, state: i.state === 'on' ? 'off' : 'on' }
							: i
					))}
				{lang}
			/>
			<h2>Map legend (plain)</h2>
			<MapLegend items={legendState} {lang} />
		</div>
		<div>
			<h2>Decision card — the C6 gate</h2>
			<DecisionCard
				kind="echallan"
				title="GJ27EM9001 · 112 km/h in a 60 segment"
				deadline="retention 09:42"
				namedActor={null}
				actions={[
					{ id: 'approve', label: 'approve', kind: 'commit' },
					{ id: 'reject', label: 'reject', kind: 'reject' },
					{ id: 'defer', label: 'defer', kind: 'defer' }
				]}
				onaction={(id) => fireToast(id === 'approve' ? 'act' : 'info')}
				{lang}
			>
				<p>Rule R_SPD_SEGMENT · sighting EMULATED · evidence expander below.</p>
				{#snippet evidence()}
					<pre class="mono">plate GJ27EM9001 · anpr_source emulated · window 09:42–10:00</pre>
				{/snippet}
			</DecisionCard>
			<DecisionCard
				kind="corridor-request"
				title="108 · cardiac · SG Highway"
				deadline="golden-hour 00:14"
				namedActor="PI-2207"
				actions={[
					{ id: 'approve', label: 'approve window', kind: 'commit' },
					{ id: 'defer', label: 'defer', kind: 'defer' }
				]}
				onaction={() => fireToast('act')}
				{lang}
			/>
		</div>
	</section>

	<section class="sec">
		<h2>Event dossier — frame + slots</h2>
		<EventDossier
			event={{ eventId: 'EV-2026-0712', name: 'Rath Yatra corridor', band: 'AMBER', state: 'active' }}
			{lang}
		>
			{#snippet meta()}
				<p class="mono">kind procession · crowd EXTREME · spine Ashram Rd</p>
			{/snippet}
			{#snippet phases()}
				<p class="mono">planned 05:30 → active 07:10 → complete (lift 2s)</p>
			{/snippet}
			{#snippet audit()}
				<FeedItem ts={now - 60000} actor="operator" actorKind="human" level="decision" text="corridor opened for yatra" {lang} />
			{/snippet}
			{#snippet advisory()}
				<p class="mono">EN / हिंदी / ગુજરાતી — one fact, three languages</p>
			{/snippet}
		</EventDossier>
	</section>

	<section class="sec">
		<h2>Modal &amp; toast — feedback, never for refusals</h2>
		<div class="row">
			<Button variant="secondary" size="sm" onclick={() => (modalOpen = true)}>open evidence modal</Button>
			<Button variant="secondary" size="sm" onclick={() => fireToast('info')}>toast · info</Button>
			<Button variant="secondary" size="sm" onclick={() => fireToast('act')}>toast · act</Button>
			<Button variant="secondary" size="sm" onclick={() => fireToast('error')}>toast · error</Button>
		</div>
		<Modal open={modalOpen} title="Evidence · GJ27EM9001" onClose={() => (modalOpen = false)}>
			<p class="mono">
				rule R_SPD_SEGMENT · speed 112 km/h · limit 60 · source emulated_anpr · retention countdown 09:42
			</p>
			<p>{t(lang, 'veil_close')}.</p>
		</Modal>
	</section>
</div>

<style>
	.page {
		padding: var(--v-space-4);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-5);
		max-width: 1280px;
		margin: 0 auto;
		width: 100%;
	}

	.intro {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-4);
		flex-wrap: wrap;
	}

	.intro h1 {
		margin: 0;
		font-size: var(--v-text-lg);
		color: var(--v-ink-1);
	}

	.intro p {
		margin: 0;
		flex: 1;
		min-width: 24ch;
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
	}

	kbd {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: 0 var(--v-space-1);
	}

	.lang {
		display: flex;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		overflow: hidden;
	}

	.lang button {
		background: transparent;
		border: none;
		border-right: 1px solid var(--v-line-1);
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
		padding: var(--v-space-1) var(--v-space-2);
		cursor: pointer;
	}

	.lang button:last-child {
		border-right: none;
	}

	.lang button.active {
		background: var(--v-surface-3);
		color: var(--v-ink-1);
	}

	.sec {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
	}

	.sec h2 {
		margin: 0;
		font-size: var(--v-text-xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.two-col {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
		gap: var(--v-space-4);
	}

	.theme-row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
		gap: var(--v-space-3);
	}

	.theme-box {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		background: var(--v-canvas);
	}

	.theme-box h3 {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.swatches {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.sw {
		display: inline-flex;
		align-items: flex-end;
		width: 84px;
		height: 34px;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
	}

	.sw i {
		font-style: normal;
		font-family: var(--v-font-mono);
		font-size: 9px;
		line-height: 1.4;
		padding: 0 var(--v-space-1);
		color: var(--v-ink-3);
		background: var(--v-canvas);
		border-radius: var(--v-radius-chip);
		margin: 2px;
	}

	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
		gap: var(--v-space-2);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.note {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.badges {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.feed {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.mono {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		margin: 0;
	}

	.sec :global(.mono) {
		font-family: var(--v-font-mono);
	}

	.sec {
		scroll-margin-top: var(--v-space-4);
	}
</style>
