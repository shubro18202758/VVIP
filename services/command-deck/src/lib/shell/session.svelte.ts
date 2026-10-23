/**
 * Operator session (browser side) — the visible half of the opt-in gate.
 *
 * Reads GET /api/session once per operator shell mount: when the deck runs
 * with VEER_OPERATOR_PIN, `gate` is true and the command bar shows the
 * duty-officer sign-in (or who is on duty); with the gate off nothing
 * renders and nothing changes. The session cookie is HttpOnly — this
 * module only ever sees {gate, officer}, never the token.
 */

export type SignInError = 'refused' | 'throttled' | 'network' | null;

class OperatorSession {
	/** null until the first answer; false = gate off (no sign-in UI). */
	gate = $state<boolean | null>(null);
	officer = $state<string | null>(null);
	busy = $state(false);
	error = $state<SignInError>(null);

	private apply(body: unknown) {
		const b = (body ?? {}) as { gate?: unknown; officer?: unknown };
		this.gate = b.gate === true;
		this.officer = typeof b.officer === 'string' ? b.officer : null;
	}

	async refresh(): Promise<void> {
		try {
			const res = await fetch('/api/session', { headers: { accept: 'application/json' } });
			if (res.ok) this.apply(await res.json());
		} catch {
			/* sign-in stays hidden until the deck answers */
		}
	}

	async signIn(officer: string, pin: string): Promise<boolean> {
		this.busy = true;
		this.error = null;
		try {
			const res = await fetch('/api/session', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ officer, pin }),
			});
			if (res.ok) {
				this.apply(await res.json());
				return true;
			}
			this.error = res.status === 429 ? 'throttled' : 'refused';
			return false;
		} catch {
			this.error = 'network';
			return false;
		} finally {
			this.busy = false;
		}
	}

	async signOut(): Promise<void> {
		try {
			const res = await fetch('/api/session', { method: 'DELETE' });
			if (res.ok) this.apply(await res.json());
		} catch {
			/* keep the current state; the cookie expires on its own */
		}
	}
}

export const operatorSession = new OperatorSession();
