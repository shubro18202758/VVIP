/**
 * Corridor summary feed — one poller shared by the cockpit's readouts
 * (stats pill, live rail, escort HUD), so the template surfaces never
 * fan out into three parallel polls of the same oracle endpoint.
 *
 * Honest degradation: an unreachable oracle keeps the last-good numbers
 * with a stale timestamp; empty corridors report zeros (they are real
 * zeros — the oracle sums over the seeded network).
 */

import { getCorridorSummary } from '$lib/api/client';

export interface CorridorSummaryRow {
	totalSegments: number;
	avgSpeedKmh: number;
	avgCongestionIdx: number;
	congestedSegments: number;
	criticalSegments: number;
	status: 'green' | 'amber' | 'red';
}

class CorridorFeed {
	row = $state<CorridorSummaryRow | null>(null);
	updatedAt = $state(0);
	status = $state<'ok' | 'down'>('ok');

	private timer: ReturnType<typeof setInterval> | null = null;

	start() {
		if (this.timer) return;
		void this.poll();
		this.timer = setInterval(() => void this.poll(), 10000);
	}

	stop() {
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
	}

	private async poll() {
		try {
			const row = await getCorridorSummary();
			this.row = row;
			this.updatedAt = Date.now();
			this.status = 'ok';
		} catch {
			this.status = 'down'; // keep last-good row + timestamp
		}
	}

	/** Flow word from the average congestion index (template vocabulary). */
	get flowWord(): 'CLEAR' | 'LIGHT' | 'MODERATE' | 'HEAVY' | 'GRIDLOCK' | null {
		if (!this.row) return null;
		const c = this.row.avgCongestionIdx;
		if (c <= 0.2) return 'CLEAR';
		if (c <= 0.4) return 'LIGHT';
		if (c <= 0.6) return 'MODERATE';
		if (c <= 0.8) return 'HEAVY';
		return 'GRIDLOCK';
	}
}

export const corridor = new CorridorFeed();
