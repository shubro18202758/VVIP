/**
 * Canvas bus — the queue between the agent's canvas DIRECTIVES (contract
 * C-D2) and the human who applies or dismisses them.
 *
 *   offer(directive, world)   validate (directives.ts) and queue; a malformed
 *                             or unresolvable proposal is dropped and counted
 *                             by reason, never guessed at
 *   resolve(world)            City Command re-checks queued URNs against its
 *                             registry + network (a proposal made on /chat is
 *                             structurally checked there, resolved here)
 *   settle(id, decision)      the human's answer: removed from the queue,
 *                             logged, and acknowledged to the brain ONCE
 *                             (POST /api/convoy/chat/directives/ack)
 *   requestSelection(urn)     a cited entity opened from /chat — City Command
 *                             serves it (select + fly) when its canvas mounts,
 *                             as the human's own pick (source 'chat')
 *
 * Module state: the queue survives SPA navigation (the agent console and
 * City Command share it), entries dedupe by id and expire after 10 minutes;
 * the ids it has seen are remembered for that long only.
 * Nothing here moves the camera — the canvas applies a directive only when
 * a human presses Apply (never auto-applied).
 */

import { ackDirective } from '$lib/api/client';
import type { SelectionSource } from './entities/selection.svelte';
import {
	DIRECTIVE_TTL_MS,
	countDrop,
	directiveUrns,
	emptyTally,
	resolveUrn,
	validateDirective,
	type CanvasDirective,
	type DirectiveWorld,
	type DropReason,
	type DropTally,
	type Validation,
} from './directives';

/** How many settled / dropped directives the bus remembers (newest first). */
const LOG_MAX = 50;
/** An 'Open on map' request older than this is not served. */
const SELECTION_TTL_MS = 5 * 60 * 1000;

export type DirectiveDecision = 'applied' | 'dismissed';

export interface DirectiveLogEntry {
	id: string;
	verb: string;
	label: string;
	decision: DirectiveDecision | 'dropped' | 'expired';
	reason: DropReason | null;
	at: number;
	/** The brain's ack: pending → recorded | failed; n/a without a session. */
	ack: 'pending' | 'recorded' | 'failed' | 'n/a';
	detail: string | null;
}

class CanvasBus {
	/** Proposals awaiting a human (oldest first). */
	pending = $state<CanvasDirective[]>([]);
	dropped = $state<DropTally>(emptyTally());
	log = $state<DirectiveLogEntry[]>([]);
	/** acks posted (applied + dismissed) — one per settled directive. */
	acks = $state(0);
	/** A selection made on another view (a cited entity), for City Command. */
	selectionRequest = $state<{ urn: string; at: number; source: SelectionSource } | null>(null);

	/** Directive ids already offered → when (pruned with the TTL, never unbounded). */
	private seen = new Map<string, number>();

	/** Validate and queue one streamed directive (a repeat id is ignored). */
	offer(raw: unknown, world: DirectiveWorld | null = null, now = Date.now()): Validation | null {
		this.expire(now);
		const id = raw && typeof raw === 'object' ? (raw as { id?: unknown }).id : null;
		if (typeof id === 'string' && this.seen.has(id)) return null;
		const result = validateDirective(raw, world, now);
		if (typeof id === 'string') this.seen.set(id, now);
		if (!result.ok) {
			this.dropped = countDrop(this.dropped, result.reason);
			this.remember({
				id: typeof id === 'string' ? id : '—',
				verb: String((raw as { verb?: unknown })?.verb ?? '—'),
				label: String((raw as { label?: unknown })?.label ?? ''),
				decision: 'dropped',
				reason: result.reason,
				at: now,
				ack: 'n/a',
				detail: result.detail,
			});
			return result;
		}
		this.pending = [...this.pending, result.directive];
		return result;
	}

	/**
	 * Re-resolve queued proposals against the canvas (City Command): one
	 * whose URN is not on the canvas is dropped ('urn'). Call only with a
	 * READY world (registry answered, network loaded).
	 */
	resolve(world: DirectiveWorld, now = Date.now()): number {
		this.expire(now);
		let removed = 0;
		const keep: CanvasDirective[] = [];
		for (const d of this.pending) {
			const missing = directiveUrns(d).find((u) => !resolveUrn(u, world).resolved);
			if (missing) {
				removed++;
				this.dropped = countDrop(this.dropped, 'urn');
				this.remember({ id: d.id, verb: d.verb, label: d.label, decision: 'dropped', reason: 'urn', at: now, ack: 'n/a', detail: `${missing} is not on the canvas` });
			} else {
				keep.push(d);
			}
		}
		if (removed) this.pending = keep;
		return removed;
	}

	/** Expire proposals older than the TTL (counted as 'expired'). */
	expire(now = Date.now()): number {
		const fresh = this.pending.filter((d) => now - d.receivedAt < DIRECTIVE_TTL_MS);
		const gone = this.pending.length - fresh.length;
		if (gone) {
			for (const d of this.pending) {
				if (now - d.receivedAt >= DIRECTIVE_TTL_MS) {
					this.dropped = countDrop(this.dropped, 'expired');
					this.remember({ id: d.id, verb: d.verb, label: d.label, decision: 'expired', reason: 'expired', at: now, ack: 'n/a', detail: null });
				}
			}
			this.pending = fresh;
		}
		if (this.selectionRequest && now - this.selectionRequest.at > SELECTION_TTL_MS) this.selectionRequest = null;
		// A stream never repeats an id after the TTL: forget it then.
		for (const [seenId, at] of this.seen) if (now - at >= DIRECTIVE_TTL_MS) this.seen.delete(seenId);
		return gone;
	}

	get(id: string): CanvasDirective | null {
		return this.pending.find((d) => d.id === id) ?? null;
	}

	/**
	 * The human's answer. The directive leaves the queue at once (a second
	 * press finds nothing), the log records it, and the brain gets ONE ack.
	 * Returns false when the id is no longer queued.
	 */
	settle(id: string, decision: DirectiveDecision, officer: string | null = null): boolean {
		const d = this.get(id);
		if (!d) return false;
		this.pending = this.pending.filter((x) => x.id !== id);
		const entry: DirectiveLogEntry = {
			id: d.id,
			verb: d.verb,
			label: d.label,
			decision,
			reason: null,
			at: Date.now(),
			ack: d.sessionId ? 'pending' : 'n/a',
			detail: null,
		};
		this.remember(entry);
		if (d.sessionId) {
			this.acks++;
			void ackDirective(d.sessionId, d.id, decision, officer).then((res) => {
				this.log = this.log.map((e) =>
					e.id === d.id && e.decision === decision ? { ...e, ack: res.ok ? 'recorded' : 'failed', detail: res.detail } : e,
				);
			});
		}
		return true;
	}

	/** A selection made on another view: City Command selects + flies when it mounts. */
	requestSelection(urn: string, source: SelectionSource = 'chat', now = Date.now()) {
		this.selectionRequest = { urn, at: now, source };
	}

	/** City Command takes the pending selection (once), with who made it. */
	takeSelection(now = Date.now()): { urn: string; source: SelectionSource } | null {
		const req = this.selectionRequest;
		this.selectionRequest = null;
		return req && now - req.at <= SELECTION_TTL_MS ? { urn: req.urn, source: req.source } : null;
	}

	/** How many directive ids the bus still remembers (bounded by the TTL). */
	get seenCount(): number {
		return this.seen.size;
	}

	/** Test / snapshot view (plain data). */
	snapshot() {
		return {
			pending: this.pending.map((d) => ({ id: d.id, verb: d.verb, label: d.label, tier: d.tier })),
			dropped: { ...this.dropped },
			acks: this.acks,
			log: this.log.slice(0, 10).map((e) => ({ ...e })),
		};
	}

	private remember(entry: DirectiveLogEntry) {
		this.log = [entry, ...this.log].slice(0, LOG_MAX);
	}
}

export const canvasBus = new CanvasBus();
