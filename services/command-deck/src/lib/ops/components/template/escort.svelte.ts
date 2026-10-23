/**
 * Escort follower (contract C-J) — ONE reader of the live escort's status,
 * shared by the VVIP corridor module's planner rail (the lifecycle: ESCORT
 * LIVE → COMPLETED / STOPPED / FAILED, and the officer's STOP control) and
 * the convoy HUD (GPS speed, progress, ETA), so the two never disagree and
 * never fan out into parallel polls.
 *
 * GET /api/convoy/movements/{id}/escort every 1.5 s while a movement is
 * followed (a hold or an approval the brain refers to the officer must
 * reach the HUD within a couple of seconds): in-flight guarded, paused
 * while the page is hidden, and every
 * answer is checked against the movement still followed (a late answer for
 * a dropped escort never lands). A brain that predates the route answers
 * 405/404 — 'not deployed', re-asked after a 60 s back-off (the HUD then
 * reads the C-B convoy entity alone). A terminal status (completed /
 * stopped / failed) is final: the follower keeps it, notes when it was
 * first seen and stops polling. Nothing is inferred on the client — the
 * lifecycle is whatever the brain reports.
 */

import { NotDeployed, fetchEscortStatus, type EscortStatus } from '$lib/gods-eye/city-api';

const POLL_MS = 1500;
const NOT_DEPLOYED_BACKOFF_MS = 60_000;

export type EscortTerminal = 'completed' | 'stopped' | 'failed';

class EscortFollower {
	movementId = $state<string | null>(null);
	status = $state.raw<EscortStatus | null>(null);
	note = $state<string | null>(null);
	/** Epoch ms when the terminal status was first seen (the HUD timer freezes there). */
	endedAt = $state<number | null>(null);

	private timer: ReturnType<typeof setInterval> | null = null;
	private inFlight = false;
	private backoffUntil = 0;

	/** Follow `movementId`'s escort (null stops following). */
	follow(movementId: string | null) {
		if (movementId === this.movementId) return;
		this.halt();
		this.movementId = movementId;
		this.status = null;
		this.note = null;
		this.endedAt = null;
		this.backoffUntil = 0;
		if (!movementId) return;
		void this.read(movementId);
		this.timer = setInterval(() => void this.read(movementId), POLL_MS);
	}

	/** Read once now (after an operator STOP), unless a read is already in flight. */
	refresh() {
		if (this.movementId && !this.endedAt) void this.read(this.movementId);
	}

	/** The brain's final word on the escort, once it has one. */
	get terminal(): EscortTerminal | null {
		const s = this.status?.status;
		return s === 'completed' || s === 'stopped' || s === 'failed' ? s : null;
	}

	private halt() {
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
	}

	private async read(id: string) {
		if (this.inFlight || Date.now() < this.backoffUntil) return;
		if (typeof document !== 'undefined' && document.hidden) return;
		this.inFlight = true;
		try {
			const status = await fetchEscortStatus(id, AbortSignal.timeout(6000));
			if (id !== this.movementId) return;
			this.status = status;
			this.note = null;
			if (this.terminal) {
				this.endedAt ??= status.updatedAt ?? Date.now();
				this.halt();
			}
		} catch (err) {
			if (id !== this.movementId) return;
			if (err instanceof NotDeployed) {
				this.backoffUntil = Date.now() + NOT_DEPLOYED_BACKOFF_MS;
				this.note = 'escort status route not deployed — reading the convoy GPS mirror';
			} else {
				this.note = err instanceof Error ? err.message.slice(0, 120) : String(err);
			}
		} finally {
			this.inFlight = false;
		}
	}
}

export const escortFollower = new EscortFollower();
