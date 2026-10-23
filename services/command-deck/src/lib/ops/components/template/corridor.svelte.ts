/**
 * Corridor summary feed — one poller shared by the cockpit's readouts
 * (stats pill, live rail, escort HUD) and City Command's vitals, so the
 * surfaces never fan out into parallel polls of the same oracle endpoint.
 *
 * Honest degradation: an unreachable oracle keeps the last-good numbers
 * with a stale timestamp. A summary over NO observations is not a clear
 * corridor: when the oracle reports status 'no_data' (or zero segments
 * with data), `noLiveFeed` is true and the surfaces say so — they never
 * print zeros as 'FLOW CLEAR · CORRIDOR GREEN'.
 *
 * Provenance: the oracle says what the numbers stand on (`provenance`
 * live | emulated | mixed | none, the row quality mix). The integrated
 * signal-ingress feed is SYNTHETIC — `simulated` is true whenever any
 * emulated segment backs the summary, and every surface then labels it
 * SIMULATED FEED (EN/HI/GU), never LIVE.
 *
 * Network hygiene: in-flight guarded, paused while the page is hidden;
 * start/stop are reference-counted (the cockpit strip, City Command's
 * vitals and the canvas's ANOMALY layer — on every view, /ops included —
 * may each hold it).
 */

import { getCorridorSummary } from '$lib/api/client';
import { createPollGate } from '$lib/ops/client';
import { POLL_MS } from '$lib/ops/endpoints';

export interface CorridorSummaryRow {
	totalSegments: number;
	avgSpeedKmh: number;
	avgCongestionIdx: number;
	congestedSegments: number;
	criticalSegments: number;
	/** 'no_data' when no fresh observation backs the numbers (oracle WP-01). */
	status: 'green' | 'amber' | 'red' | 'no_data';
	/** live | emulated | mixed | none — null on oracles that predate the field. */
	provenance: 'live' | 'emulated' | 'mixed' | 'none' | null;
	emulatedSegments: number | null;
	dataAgeSec: number | null;
	syntheticRows: number | null;
}

/**
 * Normalise the wire summary: any status outside green|amber|red (the
 * oracle's 'no_data', or a missing field) is 'no_data', and so is a summary
 * computed over zero segments. Exported pure for the contract tests.
 */
export function normaliseCorridorSummary(row: {
	totalSegments: number;
	avgSpeedKmh: number;
	avgCongestionIdx: number;
	congestedSegments: number;
	criticalSegments: number;
	status: string;
	provenance?: 'live' | 'emulated' | 'mixed' | 'none' | null;
	emulatedSegments?: number | null;
	dataAgeSec?: number | null;
	qualityMix?: { synthetic: number } | null;
}): CorridorSummaryRow {
	const wire = row.status;
	const known = wire === 'green' || wire === 'amber' || wire === 'red';
	const total = Number.isFinite(row.totalSegments) ? row.totalSegments : 0;
	return {
		totalSegments: total,
		avgSpeedKmh: Number.isFinite(row.avgSpeedKmh) ? row.avgSpeedKmh : 0,
		avgCongestionIdx: Number.isFinite(row.avgCongestionIdx) ? row.avgCongestionIdx : 0,
		congestedSegments: Number.isFinite(row.congestedSegments) ? row.congestedSegments : 0,
		criticalSegments: Number.isFinite(row.criticalSegments) ? row.criticalSegments : 0,
		status: known && total > 0 ? wire : 'no_data',
		provenance: row.provenance ?? null,
		emulatedSegments: row.emulatedSegments ?? null,
		dataAgeSec: row.dataAgeSec ?? null,
		syntheticRows: row.qualityMix ? row.qualityMix.synthetic : null,
	};
}

class CorridorFeed {
	row = $state<CorridorSummaryRow | null>(null);
	updatedAt = $state(0);
	status = $state<'ok' | 'down'>('ok');

	private timer: ReturnType<typeof setInterval> | null = null;
	private holders = 0;
	private gate = createPollGate();

	start() {
		this.holders++;
		if (this.timer) return;
		void this.gate('corridor', () => this.poll());
		this.timer = setInterval(() => void this.gate('corridor', () => this.poll()), POLL_MS.corridor);
	}

	stop() {
		this.holders = Math.max(0, this.holders - 1);
		if (this.holders > 0) return;
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
	}

	private async poll() {
		try {
			const row = await getCorridorSummary();
			this.row = normaliseCorridorSummary(row);
			this.updatedAt = Date.now();
			this.status = 'ok';
		} catch {
			this.status = 'down'; // keep last-good row + timestamp
		}
	}

	/** True when the summary is not backed by live observations. */
	get noLiveFeed(): boolean {
		return !!this.row && this.row.status === 'no_data';
	}

	/**
	 * True when synthetic (emulated) segments back the summary — the
	 * integrated signal-ingress feed. Surfaces label it SIMULATED FEED.
	 */
	get simulated(): boolean {
		const r = this.row;
		if (!r || this.noLiveFeed) return false;
		return r.provenance === 'emulated' || r.provenance === 'mixed' || (r.emulatedSegments ?? 0) > 0;
	}

	/**
	 * What the summary says the ingest rows ARE, for surfaces that stand on
	 * the same feed without reading it themselves (anomaly pulses, segment
	 * dossiers): 'simulated' (synthetic rows back it), 'live' (the oracle
	 * reports live provenance and no emulated segment), else 'unverified' —
	 * no answer yet, no fresh rows, or an oracle that predates the field.
	 * Never 'live' by default.
	 */
	get provenance(): 'simulated' | 'live' | 'unverified' {
		if (this.simulated) return 'simulated';
		const r = this.row;
		if (r && !this.noLiveFeed && r.provenance === 'live' && (r.emulatedSegments ?? 0) === 0 && (r.syntheticRows ?? 0) === 0) {
			return 'live';
		}
		return 'unverified';
	}

	/** Flow word from the average congestion index (template vocabulary); null without live data. */
	get flowWord(): 'CLEAR' | 'LIGHT' | 'MODERATE' | 'HEAVY' | 'GRIDLOCK' | null {
		if (!this.row || this.noLiveFeed) return null;
		const c = this.row.avgCongestionIdx;
		if (c <= 0.2) return 'CLEAR';
		if (c <= 0.4) return 'LIGHT';
		if (c <= 0.6) return 'MODERATE';
		if (c <= 0.8) return 'HEAVY';
		return 'GRIDLOCK';
	}
}

export const corridor = new CorridorFeed();
