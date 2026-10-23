<script lang="ts">
	/**
	 * CommandPalette — ⌘K / Ctrl+K over the God's-eye canvas (City Command
	 * and the wall views). One line, five commands:
	 *
	 *   fly to <plate | URN | junction | road>   select + fly (the dossier opens
	 *                                            on City Command)
	 *   show <agency> units near <road>          highlight the EMULATED units within
	 *                                            1.5 km of that road and frame them
	 *   layer <id> on|off                        a canvas layer
	 *   sensor <mode>                            raw · flir-whot · flir-bhot · flir-iron
	 *   ask <question>                           the agent console's stream: its
	 *                                            canvas directives go through the bus
	 *                                            (AI suggests … Apply / Dismiss)
	 *
	 * Ethics rail: VEHICLES ONLY — a plate resolves through the fused
	 * registry's earmarked vehicles; there is no person search, ever. Every
	 * plate shown here is masked unless a named duty officer is on console;
	 * a typed plate is matched against the registry, never echoed back raw.
	 * Nothing here decides anything: a fly is a camera move, an ask is a
	 * question whose proposals a human applies.
	 */
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import '$lib/i18n/packs.agent';
	import { streamChat } from '$lib/api/client';
	import { cityRegistry } from '../entities/registry.svelte';
	import { selection } from '../entities/selection.svelte';
	import { godsEye } from '../store.svelte';
	import { canvasBus } from '../canvas-bus.svelte';
	import { SENSOR_MODES, resolveUrn, type DirectiveWorld } from '../directives';
	import { displayPlate, displayText } from '../plates';
	import type { CityEntity } from '../city-api';

	let {
		layerIds,
		world,
		onfly,
		onhighlight,
		onlayer,
		onsensor,
	}: {
		layerIds: readonly string[];
		/** What the canvas can resolve right now (for streamed directives). */
		world: () => DirectiveWorld;
		/** A point flight (a road's extent, a group of units). */
		onfly: (lon: number, lat: number, rangeM: number) => void;
		onhighlight: (urns: string[]) => void;
		onlayer: (id: string, on: boolean) => void;
		onsensor: (mode: (typeof SENSOR_MODES)[number]) => void;
	} = $props();

	type Item = { key: string; label: string; detail: string; run: () => void };

	let open = $state(false);
	let query = $state('');
	let active = $state(0);
	let note = $state<string | null>(null);
	let answer = $state('');
	let asking = $state(false);
	let inputEl: HTMLInputElement | null = $state(null);
	let session: string | null = null;
	let controller: AbortController | null = null;

	const lang = $derived(ops.lang);
	const officer = $derived(ops.officerOn);

	const AGENCIES: Record<string, string> = {
		'jan rakshak': 'jan_rakshak',
		jan_rakshak: 'jan_rakshak',
		police: 'police',
		traffic: 'traffic',
		ambulance: 'ambulance',
		'108': 'ambulance',
		fire: 'fire',
	};
	const NEAR_M = 1500;

	function norm(v: unknown): string {
		return String(v ?? '')
			.replace(/[\s\-.]/g, '')
			.toUpperCase();
	}

	function show() {
		open = true;
		note = null;
		active = 0;
		requestAnimationFrame(() => inputEl?.focus());
	}

	function close() {
		open = false;
		query = '';
		active = 0;
	}

	function onWindowKey(e: KeyboardEvent) {
		if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
			e.preventDefault();
			if (open) close();
			else show();
		}
	}

	function onInputKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			close();
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			active = Math.min(items.length - 1, active + 1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			active = Math.max(0, active - 1);
		} else if (e.key === 'Enter') {
			e.preventDefault();
			items[active]?.run();
		}
	}

	/** An entity's label as this console may show it (C6: plates masked). */
	function entityLabel(e: CityEntity): string {
		const plate = typeof e.props.plate === 'string' ? e.props.plate : null;
		if (plate) return displayPlate(plate, officer) ?? e.label;
		return displayText(e.label, officer);
	}

	function flyUrn(urn: string) {
		selection.select(urn, { fly: true, source: 'palette' });
		close();
	}

	function distM(a: [number, number], b: [number, number]): number {
		const kx = 111_320 * Math.cos((a[1] * Math.PI) / 180);
		return Math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * 110_540);
	}

	function roadSegments(name: string) {
		const q = name.trim().toLowerCase();
		if (!q) return [];
		return godsEye.segments.filter((s) => (s.roadName ?? '').toLowerCase().includes(q));
	}

	function frame(points: Array<[number, number]>) {
		if (!points.length) return;
		const lons = points.map((p) => p[0]);
		const lats = points.map((p) => p[1]);
		const lon = (Math.min(...lons) + Math.max(...lons)) / 2;
		const lat = (Math.min(...lats) + Math.max(...lats)) / 2;
		const span = Math.max(distM([Math.min(...lons), lat], [Math.max(...lons), lat]), distM([lon, Math.min(...lats)], [lon, Math.max(...lats)]));
		onfly(lon, lat, Math.min(16_000, Math.max(1600, span * 1.8)));
	}

	function flyTargets(q: string): Item[] {
		const out: Item[] = [];
		const text = q.trim();
		if (!text) return out;
		const w = world();
		// 1. a URN the canvas resolves
		const asUrn = resolveUrn(text, w);
		if (asUrn.urn && asUrn.resolved) {
			const e = cityRegistry.get(asUrn.urn);
			out.push({ key: `urn:${asUrn.urn}`, label: `${t(lang, 'pal_fly')} ${e ? entityLabel(e) : asUrn.urn}`, detail: asUrn.urn, run: () => flyUrn(asUrn.urn as string) });
		}
		// 2. a plate — matched against the registry's earmarked vehicles only
		const plate = norm(text);
		if (plate.length >= 4) {
			for (const v of cityRegistry.byKind('vehicle')) {
				const raw = norm(v.props.plate);
				if (raw && (raw === plate || (plate.length >= 6 && raw.startsWith(plate)))) {
					out.push({
						key: v.urn,
						label: `${t(lang, 'pal_fly')} ${entityLabel(v)}`,
						detail: `${v.urn} · ${String(v.props.earmark_state ?? v.state ?? '')}${v.source === 'emulated' ? ' · EMULATED' : ''}`,
						run: () => flyUrn(v.urn),
					});
				}
			}
		}
		// 3. junctions by name or number
		const jNum = /^(?:j|junction\s*)(\d{1,6})$/i.exec(text);
		for (const j of cityRegistry.byKind('junction')) {
			const name = String(j.props.name ?? j.label ?? '');
			if ((jNum && j.id === jNum[1]) || (text.length >= 3 && name.toLowerCase().includes(text.toLowerCase()))) {
				out.push({ key: j.urn, label: `${t(lang, 'pal_fly')} ${name || j.urn}`, detail: j.urn, run: () => flyUrn(j.urn) });
			}
		}
		// 4. roads (the canvas network) — framed whole
		if (text.length >= 3) {
			const byRoad = new Map<string, typeof godsEye.segments>();
			for (const s of roadSegments(text)) {
				const k = s.roadName ?? `segment ${s.segmentId}`;
				byRoad.set(k, [...(byRoad.get(k) ?? []), s]);
			}
			for (const [road, segs] of byRoad) {
				out.push({
					key: `road:${road}`,
					label: `${t(lang, 'pal_fly')} ${road}`,
					detail: `${segs.length} segment(s) · ${segs.map((s) => s.segmentId).slice(0, 4).join(', ')}`,
					run: () => {
						frame(segs.flatMap((s) => s.coordinates));
						close();
					},
				});
			}
		}
		// 5. any other entity whose (masked) label carries the text
		if (text.length >= 3) {
			for (const kind of ['camera', 'unit', 'event', 'convoy'] as const) {
				for (const e of cityRegistry.byKind(kind)) {
					if (out.some((i) => i.key === e.urn)) continue;
					if (e.label.toLowerCase().includes(text.toLowerCase()) || e.id.toLowerCase() === text.toLowerCase()) {
						out.push({ key: e.urn, label: `${t(lang, 'pal_fly')} ${entityLabel(e)}`, detail: e.urn, run: () => flyUrn(e.urn) });
					}
				}
			}
		}
		return out.slice(0, 8);
	}

	function unitsNear(agencyWord: string | null, road: string): Item[] {
		const agency = agencyWord ? (AGENCIES[agencyWord.trim().toLowerCase()] ?? null) : null;
		const segs = roadSegments(road);
		if (!segs.length) {
			return [{ key: 'units:none', label: t(lang, 'pal_unknown_road'), detail: road, run: () => (note = t(lang, 'pal_unknown_road')) }];
		}
		const pts = segs.flatMap((s) => s.coordinates);
		const units = godsEye.units.filter(
			(u) => (!agency || u.agency === agency) && pts.some((p) => distM([u.lon, u.lat], p) <= NEAR_M),
		);
		const roadName = segs[0].roadName ?? road;
		const label = units.length ? String(t(lang, 'pal_units_near', units.length, roadName)) : String(t(lang, 'pal_none_near', roadName));
		return [
			{
				key: `units:${agency ?? 'all'}:${roadName}`,
				label: `${t(lang, 'pal_units')} · ${label}`,
				detail: `${agency ?? 'all agencies'} · within ${NEAR_M / 1000} km · EMULATED units`,
				run: () => {
					onhighlight(units.map((u) => `unit:${u.unitId}`));
					frame([...pts, ...units.map((u) => [u.lon, u.lat] as [number, number])]);
					note = label;
					close();
				},
			},
		];
	}

	function ask(question: string) {
		controller?.abort();
		const ctrl = new AbortController();
		controller = ctrl;
		answer = '';
		asking = true;
		note = t(lang, 'pal_sent');
		void streamChat(
			question,
			null,
			null,
			{
				onToken: (tok) => (answer += tok),
				onThought: () => {},
				onToolCall: () => {},
				onToolResult: () => {},
				onDirective: (d) => canvasBus.offer(d, world()),
				onDone: (d) => {
					if (d.sessionId) session = d.sessionId;
					asking = false;
				},
				onError: (e) => {
					if (ctrl.signal.aborted) return;
					answer = e.message;
					asking = false;
				},
			},
			{ sessionId: session, signal: ctrl.signal, eventId: ops.selectedEventId },
		).catch(() => {
			asking = false;
		});
	}

	const items = $derived.by((): Item[] => {
		void cityRegistry.version;
		const q = query.trim();
		let m: RegExpExecArray | null;
		if ((m = /^fly\s+to\s+(.+)$/i.exec(q))) return flyTargets(m[1]);
		if ((m = /^show\s+(?:(.+?)\s+)?units?\s+(?:near|on|at)\s+(.+)$/i.exec(q))) return unitsNear(m[1] ?? null, m[2]);
		if ((m = /^layer\s+([a-z0-9-]+)\s+(on|off)$/i.exec(q))) {
			const id = m[1].toLowerCase();
			const on = m[2].toLowerCase() === 'on';
			return layerIds.includes(id)
				? [{ key: `layer:${id}`, label: `${t(lang, 'pal_layer')} ${id} ${on ? 'on' : 'off'}`, detail: id, run: () => { onlayer(id, on); close(); } }]
				: [];
		}
		if ((m = /^sensor\s+([a-z-]+)$/i.exec(q))) {
			const mode = (m[1].toLowerCase() === 'command' ? 'raw' : m[1].toLowerCase()) as (typeof SENSOR_MODES)[number];
			return (SENSOR_MODES as readonly string[]).includes(mode)
				? [{ key: `sensor:${mode}`, label: `${t(lang, 'pal_sensor')} ${mode}`, detail: mode, run: () => { onsensor(mode); close(); } }]
				: [];
		}
		if ((m = /^ask\s+(.+)$/i.exec(q))) {
			const question = m[1];
			return [{ key: 'ask', label: `${t(lang, 'pal_ask')}: ${displayText(question, officer)}`, detail: 'POST /chat/stream', run: () => ask(question) }];
		}
		// Bare text: a fly target first, then the templates.
		const bare = q ? flyTargets(q) : [];
		const templates: Item[] = [
			{ key: 'tpl:fly', label: 'fly to …', detail: 'plate · URN · junction · road', run: () => (query = 'fly to ') },
			{ key: 'tpl:units', label: 'show police units near …', detail: 'jan rakshak · police · traffic · ambulance · fire', run: () => (query = 'show police units near ') },
			{ key: 'tpl:layer', label: 'layer … on|off', detail: layerIds.join(' · '), run: () => (query = 'layer ') },
			{ key: 'tpl:sensor', label: 'sensor …', detail: SENSOR_MODES.join(' · '), run: () => (query = 'sensor ') },
			{ key: 'tpl:ask', label: 'ask …', detail: t(lang, 'pal_ask'), run: () => (query = 'ask ') },
		];
		return [...bare, ...templates].slice(0, 8);
	});

	$effect(() => {
		void items.length;
		if (active >= items.length) active = Math.max(0, items.length - 1);
	});
</script>

<svelte:window onkeydown={onWindowKey} />

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="pal-veil" onclick={close}></div>
	<div class="palette" role="dialog" aria-modal="true" aria-label={t(lang, 'pal_title')} data-testid="command-palette">
		<input
			bind:this={inputEl}
			bind:value={query}
			onkeydown={onInputKey}
			placeholder={t(lang, 'pal_placeholder')}
			aria-label={t(lang, 'pal_title')}
			data-testid="palette-input"
			autocomplete="off"
			spellcheck="false"
		/>
		{#if items.length}
			<ul class="pal-items" role="listbox">
				{#each items as item, i (item.key)}
					<li role="option" aria-selected={i === active}>
						<button type="button" class:active={i === active} onmouseenter={() => (active = i)} onclick={() => item.run()} data-key={item.key}>
							<span class="pal-label">{item.label}</span>
							<span class="pal-detail mono">{item.detail}</span>
						</button>
					</li>
				{/each}
			</ul>
		{:else if query.trim()}
			<p class="pal-empty">{t(lang, 'pal_no_match')}</p>
		{/if}
		{#if asking || answer}
			<p class="pal-answer" data-testid="palette-answer">{answer ? displayText(answer, officer) : '…'}</p>
		{/if}
		<footer class="pal-foot">
			<span>{t(lang, 'pal_hint')}</span>
			<span class="pal-ethics">{t(lang, 'pal_vehicles_only')}</span>
		</footer>
		{#if note}<p class="pal-note">{displayText(note, officer)}</p>{/if}
	</div>
{/if}

<style>
	.pal-veil {
		position: fixed;
		inset: 0;
		z-index: 60;
		background: color-mix(in srgb, var(--v-canvas) 45%, transparent);
	}

	.palette {
		position: fixed;
		z-index: 61;
		top: 12vh;
		left: 50%;
		transform: translateX(-50%);
		width: min(720px, calc(100vw - 32px));
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-space-3);
		background: color-mix(in srgb, var(--v-surface-1) 96%, transparent);
		border: 1px solid var(--v-data);
		border-radius: var(--v-radius-panel);
		box-shadow: 0 18px 48px color-mix(in srgb, var(--v-canvas) 70%, transparent);
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.palette input {
		height: var(--v-ctl-lg);
		padding: 0 var(--v-space-3);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
		font: inherit;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-sm);
	}

	.palette input:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.pal-items {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 46vh;
		overflow-y: auto;
	}

	.pal-items button {
		width: 100%;
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-3);
		border: 1px solid transparent;
		border-radius: var(--v-radius-chip);
		background: transparent;
		color: var(--v-ink-1);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}

	.pal-items button.active {
		border-color: var(--v-data);
		background: color-mix(in srgb, var(--v-data) 10%, transparent);
	}

	.pal-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pal-detail {
		flex-shrink: 0;
		max-width: 45%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.pal-empty,
	.pal-note,
	.pal-answer {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.pal-answer {
		padding: var(--v-space-2);
		border-left: 2px solid var(--v-tool);
		background: color-mix(in srgb, var(--v-tool) 6%, transparent);
		max-height: 8rem;
		overflow-y: auto;
		white-space: pre-wrap;
	}

	.pal-foot {
		display: flex;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.pal-ethics {
		color: var(--v-ink-2);
	}
</style>
