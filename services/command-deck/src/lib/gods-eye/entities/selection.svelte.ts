/**
 * Entity selection — the one "what am I looking at" the City Command
 * shares between the map, the dossier, the fusion rail and the board.
 *
 *   current   the selected entity URN (the dossier shows it; the canvas
 *             rings it); null = nothing selected
 *   hover     the URN under the pointer (canvas hover, rail hover)
 *   select(urn, {fly})   set the selection; `fly` also asks the mounted
 *             canvas to fly the camera to the entity (a monotonically
 *             increasing request seq, like the focus bus: a canvas serves
 *             only requests made after it mounted, so the next view never
 *             replays the last one)
 *
 * Event URNs keep the ops store's selection in sync (ops.selectEvent), so
 * the band, audit tail, pursuit overlay and board follow the map. The sync
 * is one-way (map → board): a board auto-selection on load is not a human
 * pick and never opens a dossier on its own.
 */

import { ops } from '$lib/ops/store.svelte';
import { parseUrn } from '../city-api';

/** What the canvas picked to produce a selection (the dossier's MAP line). */
export interface MapPick {
	pickId: string;
	layer: string;
	text: string;
}

/**
 * Who made the selection: 'agent' = an AI-suggested directive a human
 * applied; 'palette' = ⌘K; 'chat' = a citation chip a human clicked on the
 * agent console (a human pick from the chat, not an AI suggestion).
 */
export type SelectionSource =
	| 'map'
	| 'rail'
	| 'dossier'
	| 'tour'
	| 'vitals'
	| 'share'
	| 'replay'
	| 'agent'
	| 'palette'
	| 'chat';

export type FlyOutcome = 'pending' | 'flying' | 'landed' | 'no-position' | 'no-canvas' | 'cancelled';

class EntitySelection {
	current = $state<string | null>(null);
	hover = $state<string | null>(null);
	source = $state<SelectionSource | null>(null);
	/** The canvas pick behind the current selection (null for rail/dossier picks). */
	pick = $state<MapPick | null>(null);
	/** Epoch ms of the newest selection change (dossier staleness, tests). */
	selectedAt = $state(0);

	/** Fly requests: the canvas reacts to each increment. */
	flySeq = $state(0);
	flyUrn = $state<string | null>(null);
	flyOutcome = $state<FlyOutcome | null>(null);
	flyNote = $state<string | null>(null);
	/** Request → landing of the newest fly request (ms, page clock) — the fly-to latency an operator feels. */
	flyLandedMs = $state<number | null>(null);
	private flyRequestedAt = 0;

	/** Select `urn` (null clears). `fly` asks the canvas to bring it into view. */
	select(
		urn: string | null,
		opts: { fly?: boolean; source?: SelectionSource; pick?: MapPick | null } = {},
	) {
		const parsed = urn ? parseUrn(urn) : null;
		const next = parsed ? urn : null;
		this.current = next;
		this.source = next ? (opts.source ?? 'map') : null;
		this.pick = next ? (opts.pick ?? null) : null;
		this.selectedAt = Date.now();
		if (next && opts.fly) {
			this.flyUrn = next;
			this.flySeq += 1;
			this.flyOutcome = 'pending';
			this.flyNote = null;
			this.flyLandedMs = null;
			this.flyRequestedAt = performance.now();
		}
		// Map → board: an event pick selects the event on the board (only an
		// event the board carries; re-selecting is a no-op — selectEvent
		// resets the band and audit tail).
		if (parsed?.kind === 'event' && parsed.id !== ops.selectedEventId && ops.events.some((e) => e.eventId === parsed.id)) {
			ops.selectEvent(parsed.id);
		}
	}

	clear() {
		this.select(null);
	}

	/** Canvas → bus: the outcome of fly request `seq` (stale reports dropped). */
	settleFly(seq: number, outcome: FlyOutcome, note: string | null = null) {
		if (seq !== this.flySeq) return;
		this.flyOutcome = outcome;
		this.flyNote = note;
		if (outcome === 'landed') this.flyLandedMs = Math.round(performance.now() - this.flyRequestedAt);
	}
}

export const selection = new EntitySelection();
