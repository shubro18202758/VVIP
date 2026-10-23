/**
 * Fusion feed — the city narrative behind the FUSION RAIL.
 *
 * GET /api/v1/city/feed: the first read takes the newest 100 rows, then
 * every 3 s the rows newer than the newest seen MINUS an overlap window
 * (`since` = newest wire time − 2 min). A row can become visible late with
 * an older stamp — a brain decider checkpoint stamped mid-plan joins the
 * feed only once the movement's corridor event is registered — and a
 * cursor at the newest row would skip it for good in a busy city. Rows are
 * de-duplicated by their stable id ("<table>:<key>"), so the overlap never
 * repeats one; they are kept newest first and capped. Reference-
 * counted, in-flight guarded, paused while hidden (one catch-up round on
 * return). Nothing is synthesized: every row is an audit row the oracle
 * unioned from a table that already exists. A failed read within
 * BLIP_GRACE_MS of the last good one is a blip: the rail stays 'ok' on its
 * rows (the note says so) instead of reading 'down' for one dropped poll.
 */

import { createPollGate, withinBlipGrace } from '$lib/ops/client';
import { FEED_POLL_MS, NotDeployed, fetchCityFeed, type FeedKind, type FeedRow } from '../city-api';

const CAP = 400;
/** Re-read window behind the newest row seen (late-visible rows, see above). */
const OVERLAP_MS = 120_000;

class FusionFeed {
	rows = $state.raw<FeedRow[]>([]);
	status = $state<'idle' | 'ok' | 'not-deployed' | 'down'>('idle');
	note = $state<string | null>(null);
	updatedAt = $state(0);
	/** Rows that arrived on the newest poll (the rail flashes them once). */
	freshIds = $state.raw<Set<string>>(new Set());

	private seen = new Set<string>();
	private newestT: number | null = null;
	private holders = 0;
	private timer: ReturnType<typeof setInterval> | null = null;
	private gate = createPollGate();
	private onVisibility: (() => void) | null = null;

	start() {
		this.holders++;
		if (this.timer) return;
		void this.gate('feed', () => this.poll());
		this.timer = setInterval(() => void this.gate('feed', () => this.poll()), FEED_POLL_MS);
		if (typeof document !== 'undefined') {
			this.onVisibility = () => {
				if (!document.hidden) void this.gate('feed', () => this.poll());
			};
			document.addEventListener('visibilitychange', this.onVisibility);
		}
	}

	stop() {
		this.holders = Math.max(0, this.holders - 1);
		if (this.holders > 0) return;
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
		if (this.onVisibility && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', this.onVisibility);
		}
		this.onVisibility = null;
	}

	/** Rows per feed kind (filter chip counts). */
	counts(): Record<FeedKind, number> {
		const out: Record<FeedKind, number> = { event: 0, enforcement: 0, anpr: 0, dispatch: 0, signals: 0, ama: 0, brain: 0 };
		for (const r of this.rows) out[r.kind]++;
		return out;
	}

	private async poll() {
		try {
			const since = this.newestT == null ? null : this.newestT - OVERLAP_MS;
			const items = await fetchCityFeed(since, since == null ? 100 : 200, AbortSignal.timeout(8000));
			const fresh = items.filter((r) => !this.seen.has(r.id));
			for (const r of fresh) this.seen.add(r.id);
			if (fresh.length) {
				const merged = [...fresh, ...this.rows].sort((a, b) => b.t - a.t).slice(0, CAP);
				if (this.seen.size > CAP * 4) this.seen = new Set(merged.map((r) => r.id));
				this.rows = merged;
				this.newestT = Math.max(this.newestT ?? 0, ...fresh.map((r) => r.t));
				this.freshIds = this.updatedAt ? new Set(fresh.map((r) => r.id)) : new Set();
			} else if (this.freshIds.size) {
				this.freshIds = new Set();
			}
			this.status = 'ok';
			this.note = null;
			this.updatedAt = Date.now();
		} catch (err) {
			const why = err instanceof Error ? err.message : String(err);
			if (err instanceof NotDeployed) {
				this.status = 'not-deployed';
				this.note = 'city feed not deployed (404)';
			} else if (this.status === 'ok' && withinBlipGrace(this.updatedAt)) {
				this.note = `city feed blip (${why}) — last-good rows held (${Math.round((Date.now() - this.updatedAt) / 1000)} s old)`;
			} else {
				this.status = 'down';
				this.note = `city feed unreachable (${why}) — showing last-good`;
			}
		}
	}
}

export const fusionFeed = new FusionFeed();
