/**
 * Public feed — the ONE data source of /citizen and /driver.
 *
 * Polls the deck's own GET /api/public/events: public-kind events,
 * projected server-side (public-api.server.ts). The public pages never
 * start the operator store, so an anonymous browser never requests
 * /vehicles, /enforcement, /dispatches, the audit tail or the emulator —
 * and never receives a pursuit or crime event, even in a network log.
 *
 * Ladder, labelled at every rung:
 *   live         the projection answered (zero events is a real answer)
 *   stale        it stopped answering < 2 min after a live answer — the
 *                last live rows stay up with their "updated" time
 *   unavailable  / not-deployed — the labelled offline preview (use-case
 *                fixtures), never mixed with live rows
 *
 * One poll in flight at a time; no polling while the tab is hidden;
 * ref-counted start/stop so /citizen → /driver hand-offs keep one timer.
 */

import { fixturePublicEvents, mapPublicFeed, type PublicCorridorEvent, type PublicLaterEvent } from './public-model';
import type { StripTier } from './holds';

/** Fixed same-origin endpoints (served by the deck, not proxied). */
export const PUBLIC_EVENTS_API = '/api/public/events';
export const PUBLIC_DISPATCH_API = '/api/public/dispatches';

const POLL_MS = 10_000;
const STALE_GRACE_MS = 120_000;

export type PublicFeedStatus = 'connecting' | 'live' | 'stale' | 'unavailable' | 'not-deployed';

function detailOf(body: unknown): string | null {
	const d = body && typeof body === 'object' ? (body as Record<string, unknown>).detail : null;
	return typeof d === 'string' ? d : null;
}

class PublicFeed {
	events = $state<PublicCorridorEvent[]>([]);
	/** Public events starting beyond the 2 h horizon (dated heads-up rows). */
	later = $state<PublicLaterEvent[]>([]);
	status = $state<PublicFeedStatus>('connecting');
	/** Server as_of of the last live answer (epoch ms); 0 = never. */
	updatedAt = $state(0);
	/** Worst plain-language tier across live public events; null = unknown. */
	tier = $state<StripTier | null>(null);
	note = $state<string | null>(null);

	private timer: ReturnType<typeof setInterval> | null = null;
	private refs = 0;
	private inFlight = false;
	private lastLiveAt = 0;
	private readonly onVisibility = () => {
		if (!document.hidden) void this.poll();
	};

	/** True while the labelled offline preview stands in for live rows. */
	get preview(): boolean {
		return this.status === 'unavailable' || this.status === 'not-deployed';
	}

	start(): void {
		this.refs += 1;
		if (this.timer) return;
		void this.poll();
		this.timer = setInterval(() => void this.poll(), POLL_MS);
		document.addEventListener('visibilitychange', this.onVisibility);
	}

	stop(): void {
		this.refs = Math.max(0, this.refs - 1);
		if (this.refs > 0 || !this.timer) return;
		clearInterval(this.timer);
		this.timer = null;
		document.removeEventListener('visibilitychange', this.onVisibility);
	}

	async poll(): Promise<void> {
		if (this.inFlight || document.hidden) return;
		this.inFlight = true;
		try {
			const res = await fetch(PUBLIC_EVENTS_API, {
				headers: { accept: 'application/json' },
				signal: AbortSignal.timeout(10_000),
			});
			const body: unknown = await res.json().catch(() => null);
			const feed = res.ok ? mapPublicFeed(body) : null;
			if (feed) {
				this.events = feed.events;
				this.later = feed.later;
				this.tier = feed.tier;
				this.updatedAt = feed.asOf;
				this.lastLiveAt = Date.now();
				this.status = 'live';
				this.note = null;
			} else {
				this.degrade(res.status === 404 ? 'not-deployed' : 'unavailable', detailOf(body) ?? `HTTP ${res.status}`);
			}
		} catch (err) {
			this.degrade('unavailable', err instanceof Error ? err.message : String(err));
		} finally {
			this.inFlight = false;
		}
	}

	private degrade(status: 'unavailable' | 'not-deployed', note: string): void {
		this.note = note;
		if (this.lastLiveAt && Date.now() - this.lastLiveAt < STALE_GRACE_MS) {
			// Keep the last live answer, visibly stale, through a short blip.
			this.status = 'stale';
			return;
		}
		this.status = status;
		this.tier = null;
		this.events = fixturePublicEvents();
		this.later = [];
	}
}

export const publicFeed = new PublicFeed();
