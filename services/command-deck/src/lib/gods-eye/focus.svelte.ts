/**
 * Canvas focus bus — board surfaces ask the God's-eye canvas to fly to an
 * event and hold the camera on it.
 *
 * One-way and honest: a surface (the cockpit LiveRail, the canvas pick
 * card) calls `focus.request(eventId)`; the mounted canvas consumes the
 * request, flies to the event's spine centroid and sets
 * `godsEye.subjectLockId` (the LOCKED chip) only when that flight LANDS.
 * Surfaces never claim a lock themselves — before this bus, the cockpit
 * rail set the lock id directly and the chip read LOCKED while the camera
 * never moved.
 *
 * Every request bumps `seq`, so asking for the same event twice re-flies.
 * A canvas serves only requests made after it mounted (the bus outlives
 * SPA navigation; a canvas on the next view must not replay the last
 * view's request). The canvas reports what actually happened through
 * `settle()`:
 *   pending      queued (canvas loading, geometry not loaded yet) or flying
 *   locked       the flight landed and the camera is held on the subject
 *   no-geometry  the event has no resolvable spine — nothing to fly to
 *   no-canvas    no canvas is mounted on this view (or WebGL is unavailable)
 *   cancelled    the operator took the camera, or the canvas closed, before
 *                the flight landed
 *
 * RELEASE is remembered per subject across views: once the operator lets
 * go of an event (RELEASE chip, drag, wheel, tour), no canvas's selection
 * follow re-locks it until the selection moves to another event or a
 * surface explicitly asks to focus on it again.
 */

export type FocusOutcome = 'pending' | 'locked' | 'no-geometry' | 'no-canvas' | 'cancelled';

class FocusBus {
	/** Monotonic request counter — the canvas reacts to each increment. */
	seq = $state(0);
	/** Subject of the newest request (a corridor event id). */
	eventId = $state<string | null>(null);
	/** What the canvas did with the newest request (null before any). */
	outcome = $state<FocusOutcome | null>(null);
	/** Human-readable detail for `outcome` (HUD flash + rail tooltip). */
	note = $state<string | null>(null);
	/** The subject the operator released — selection follow skips it (see above). */
	released: string | null = null;

	/** Canvases mounted right now (a request with none is answered 'no-canvas'). */
	private canvases = 0;

	/** Ask the mounted canvas to fly to `eventId` and lock the camera on it. */
	request(eventId: string) {
		this.eventId = eventId;
		this.released = null; // an explicit ask overrides an earlier RELEASE
		this.seq += 1;
		if (this.canvases === 0) {
			this.outcome = 'no-canvas';
			this.note = "no God's-eye canvas on this view";
		} else {
			this.outcome = 'pending';
			this.note = null;
		}
	}

	/**
	 * Canvas → bus: report the outcome of request `seq`. Stale reports (a
	 * newer request superseded this one) are dropped.
	 */
	settle(seq: number, outcome: FocusOutcome, note: string | null = null) {
		if (seq !== this.seq) return;
		this.outcome = outcome;
		this.note = note;
	}

	/** A canvas mounted: counts it until the returned detach runs (onDestroy). */
	attach(): () => void {
		this.canvases += 1;
		let attached = true;
		return () => {
			if (!attached) return;
			attached = false;
			this.canvases = Math.max(0, this.canvases - 1);
		};
	}
}

export const focus = new FocusBus();
