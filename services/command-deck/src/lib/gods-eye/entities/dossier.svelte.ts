/**
 * Dossier loader — GET /api/v1/city/entity/{kind}/{id} for the selected
 * entity, kept fresh while it stays selected.
 *
 * One request token per selection (an answer for a selection that moved on
 * is dropped), a short per-URN cache so re-selecting or flying to a recent
 * entity costs nothing, and a 6 s refresh while the dossier is open (the
 * audit trail and the related list are live). A 404 is an honest "not in
 * the city picture" state (an entity that left the snapshot, or a URN the
 * oracle does not resolve yet), never an error toast.
 */

import { pageHidden } from '$lib/ops/client';
import { NotDeployed, fetchDossier, type Dossier } from '../city-api';

const CACHE_TTL_MS = 15_000;
const REFRESH_MS = 6000;

export type DossierState = 'idle' | 'loading' | 'ok' | 'not-found' | 'down';

class DossierLoader {
	urn = $state<string | null>(null);
	state = $state<DossierState>('idle');
	data = $state.raw<Dossier | null>(null);
	note = $state<string | null>(null);

	private cache = new Map<string, Dossier>();
	private token = 0;
	private timer: ReturnType<typeof setInterval> | null = null;

	/** Show `urn` (null closes): cached answer first, then a fresh read. */
	open(urn: string | null) {
		if (urn === this.urn) return;
		this.urn = urn;
		this.token++;
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
		if (!urn) {
			this.state = 'idle';
			this.data = null;
			this.note = null;
			return;
		}
		const cached = this.cache.get(urn);
		this.data = cached ?? null;
		this.state = cached ? 'ok' : 'loading';
		this.note = null;
		void this.load(urn, this.token);
		this.timer = setInterval(() => {
			if (!pageHidden() && this.urn === urn) void this.load(urn, this.token);
		}, REFRESH_MS);
	}

	close() {
		this.open(null);
	}

	/** Force a re-read of the open dossier (after an operator action). */
	reload() {
		if (this.urn) void this.load(this.urn, this.token);
	}

	/**
	 * A cached (≤ 15 s) or freshly read dossier for any URN — the canvas
	 * uses it to find the position of an entity the snapshot does not carry
	 * (a completed event, a camera hit older than the snapshot window).
	 */
	async peek(urn: string): Promise<Dossier | null> {
		const hit = this.cache.get(urn);
		if (hit && Date.now() - hit.fetchedAt < CACHE_TTL_MS) return hit;
		try {
			const d = await fetchDossier(urn, AbortSignal.timeout(8000));
			this.remember(urn, d);
			return d;
		} catch {
			return null;
		}
	}

	private remember(urn: string, d: Dossier) {
		this.cache.set(urn, d);
		if (this.cache.size > 64) {
			const oldest = this.cache.keys().next().value;
			if (oldest) this.cache.delete(oldest);
		}
	}

	private async load(urn: string, token: number) {
		try {
			const d = await fetchDossier(urn, AbortSignal.timeout(10_000));
			this.remember(urn, d);
			if (token !== this.token) return;
			this.data = d;
			this.state = 'ok';
			this.note = null;
		} catch (err) {
			if (token !== this.token) return;
			if (err instanceof NotDeployed) {
				this.state = 'not-found';
				this.note = 'not in the city picture (404) — the oracle has no dossier for this entity';
			} else {
				this.state = this.data ? 'ok' : 'down';
				this.note = `dossier read failed (${err instanceof Error ? err.message : String(err)})${this.data ? ' — showing last-good' : ''}`;
			}
		}
	}
}

export const dossier = new DossierLoader();
