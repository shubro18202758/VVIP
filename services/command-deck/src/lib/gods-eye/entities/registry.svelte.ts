/**
 * City entity registry — the URN-keyed fused picture every layer, HUD
 * surface and dossier reads (contract C-C).
 *
 * One poller (GET /api/v1/city/entities every 4 s, reference-counted
 * start/stop, in-flight guarded, paused while the page is hidden with one
 * catch-up round on return) fills an immutable snapshot; the indexes
 * (by URN, by kind, reverse links) are rebuilt once per snapshot, and a
 * reactive `version` is the only thing components subscribe to — the
 * snapshot itself is held raw (no deep proxy over hundreds of entities).
 *
 * A BLIP — a failed read within BLIP_GRACE_MS of the last good answer (an
 * oracle restart, one dropped request) — keeps status 'ok' on the last-good
 * snapshot, the note saying so: one failed poll never flips City Command to
 * NO DATA or blanks the layers. Past the grace the status is 'down'.
 *
 * Degradation is per kind: `served(kind)` is true only while the endpoint
 * answers AND that kind's own source reported ok. Layers with a WP-05
 * fetcher (vehicles, junctions, units, cameras) fall back to it when a
 * kind is not served (gods-eye store); kinds that exist only here
 * (recommendations, dispatches, convoys, anomalies, sightings) render an
 * honest "city fusion API unavailable" state instead.
 */

import { createPollGate, withinBlipGrace } from '$lib/ops/client';
import {
	CITY_POLL_MS,
	NotDeployed,
	fetchCitySnapshot,
	type CityEntity,
	type CitySnapshot,
	type EntityKind,
	type KindFeed,
} from '../city-api';

export type RegistryStatus = 'idle' | 'ok' | 'not-deployed' | 'down';

class CityRegistry {
	/** Bumped on every applied snapshot — the one reactive dependency. */
	version = $state(0);
	status = $state<RegistryStatus>('idle');
	note = $state<string | null>(null);
	/** Epoch ms of the last successful answer (0 = never). */
	updatedAt = $state(0);
	/** Server time of the snapshot. */
	asOf = $state<number | null>(null);
	tookMs = $state<number | null>(null);

	private snap: CitySnapshot | null = null;
	private byUrn = new Map<string, CityEntity>();
	private kindIndex = new Map<string, CityEntity[]>();
	/** urn → urns that link TO it (the reverse of `links`). */
	private backLinks = new Map<string, string[]>();

	private holders = 0;
	private timer: ReturnType<typeof setInterval> | null = null;
	private gate = createPollGate();
	private onVisibility: (() => void) | null = null;

	// ─── lifecycle ────────────────────────────────────────────────────────────

	start() {
		this.holders++;
		if (this.timer) return;
		void this.gate('city', () => this.poll());
		this.timer = setInterval(() => void this.gate('city', () => this.poll()), CITY_POLL_MS);
		if (typeof document !== 'undefined') {
			this.onVisibility = () => {
				if (!document.hidden) void this.gate('city', () => this.poll());
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

	/** One poll now (bypasses the interval; still in-flight guarded). */
	refresh(): Promise<boolean> {
		return this.gate('city', () => this.poll());
	}

	private async poll() {
		try {
			const snap = await fetchCitySnapshot(AbortSignal.timeout(8000));
			this.apply(snap);
			this.status = 'ok';
			this.note = null;
			this.updatedAt = Date.now();
		} catch (err) {
			const why = err instanceof Error ? err.message : String(err);
			if (err instanceof NotDeployed) {
				this.status = 'not-deployed';
				this.note = 'city fusion API not deployed (404) — layers fall back to their own feeds';
			} else if (this.status === 'ok' && withinBlipGrace(this.updatedAt)) {
				// A blip: the last-good snapshot stands as the current picture, said so.
				this.note = `city fusion API blip (${why}) — last-good picture held (${Math.round((Date.now() - this.updatedAt) / 1000)} s old)`;
			} else {
				// Keep the last-good snapshot; its age shows on every surface.
				this.status = 'down';
				this.note = `city fusion API unreachable (${why}) — showing last-good`;
			}
		}
	}

	private apply(snap: CitySnapshot) {
		const byUrn = new Map<string, CityEntity>();
		const kinds = new Map<string, CityEntity[]>();
		const back = new Map<string, string[]>();
		for (const e of snap.entities) {
			byUrn.set(e.urn, e);
			const list = kinds.get(e.kind);
			if (list) list.push(e);
			else kinds.set(e.kind, [e]);
			for (const l of e.links) {
				const b = back.get(l);
				if (b) b.push(e.urn);
				else back.set(l, [e.urn]);
			}
		}
		this.snap = snap;
		this.byUrn = byUrn;
		this.kindIndex = kinds;
		this.backLinks = back;
		this.asOf = snap.asOf;
		this.tookMs = snap.tookMs;
		this.version++;
	}

	// ─── reads (each subscribes to `version`) ─────────────────────────────────

	get(urn: string | null | undefined): CityEntity | null {
		void this.version;
		return urn ? (this.byUrn.get(urn) ?? null) : null;
	}

	byKind(kind: EntityKind): CityEntity[] {
		void this.version;
		return this.kindIndex.get(kind) ?? [];
	}

	/** 1-hop neighbours: what `urn` links to plus what links to it (deduped). */
	related(urn: string): CityEntity[] {
		void this.version;
		const self = this.byUrn.get(urn);
		const out = new Map<string, CityEntity>();
		for (const l of self?.links ?? []) {
			const e = this.byUrn.get(l);
			if (e) out.set(e.urn, e);
		}
		for (const l of this.backLinks.get(urn) ?? []) {
			const e = this.byUrn.get(l);
			if (e) out.set(e.urn, e);
		}
		out.delete(urn);
		return [...out.values()];
	}

	feed(kind: EntityKind): KindFeed | null {
		void this.version;
		return this.snap?.feeds[kind] ?? null;
	}

	/** True while the snapshot is current and this kind's own source answered ok. */
	served(kind: EntityKind): boolean {
		void this.version;
		if (this.status !== 'ok' || !this.snap) return false;
		const f = this.snap.feeds[kind];
		return !!f && f.status === 'ok';
	}

	/** Entity count of the current snapshot. */
	get size(): number {
		void this.version;
		return this.byUrn.size;
	}
}

export const cityRegistry = new CityRegistry();
