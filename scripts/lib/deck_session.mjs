/**
 * Operator sign-in for the deck's PIN gate (VEER_OPERATOR_PIN), shared by every Node check — the scripts
 * here and the deck smokes (services/command-deck/tests/lib/operator-session.mjs re-exports it).
 *
 *   const session = await signIn('http://127.0.0.1:5273', 'Journey check (automated)');
 *   const res = await deckFetch(session, '/api/v1/units');         // fetch carrying the session cookie
 *   await page.setCookie(...puppeteerCookies(session));             // BEFORE page.goto
 *   await signOut(session);
 *
 * signIn resolves {gate:false} when the deck's gate is off (nothing to sign in to) and throws when the deck
 * is unreachable or refuses. One attempt per call: the deck throttles 8 failed sign-ins per 5 min per client
 * (operator-session.server.ts), so a retry loop would lock the operator out.
 *
 * The PIN is read at call time from process.env.VEER_OPERATOR_PIN, else the file named by
 * VEER_OPERATOR_PIN_FILE (a sandbox deck's throwaway PIN), else the VEER_OPERATOR_PIN= line of
 * VEER_ENV_FILE (~/.config/veer/veer.env). A PIN file that is NAMED but missing or unreadable is an error —
 * never a fall-back to veer.env's (live) PIN. Surrounding whitespace is trimmed (the deck trims its own
 * PIN); a quoted veer.env value is unquoted as scripts/veer_env.sh's loader does. It is never logged and
 * never leaves this module except in the sign-in request body. Every request is time-bounded.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const COOKIE = 'veer_operator';

const trimmed = (value) => (typeof value === 'string' ? value.trim() : '') || null;

export function readPin() {
	if (process.env.VEER_OPERATOR_PIN) return trimmed(process.env.VEER_OPERATOR_PIN);
	const pinFile = process.env.VEER_OPERATOR_PIN_FILE;
	if (pinFile) {
		try {
			return trimmed(fs.readFileSync(pinFile, 'utf8'));
		} catch {
			return null;
		}
	}
	const envFile = process.env.VEER_ENV_FILE || path.join(os.homedir(), '.config/veer/veer.env');
	if (!fs.existsSync(envFile)) return null;
	const lines = fs
		.readFileSync(envFile, 'utf8')
		.split(/\r?\n/)
		.map((l) => l.trimStart())
		.filter((l) => l.startsWith('VEER_OPERATOR_PIN='));
	const value = lines.length ? lines[lines.length - 1].slice('VEER_OPERATOR_PIN='.length).trimEnd() : '';
	return trimmed(value.replace(/^"(.*)"$/, '$1').replace(/^'(.*)'$/, '$1'));
}

export async function signIn(deckUrl, officer) {
	const base = deckUrl.replace(/\/+$/, '');
	const probe = await fetch(`${base}/api/session`, { cache: 'no-store', signal: AbortSignal.timeout(8000) });
	if (!probe.ok) throw new Error(`deck session: GET ${base}/api/session -> HTTP ${probe.status}`);
	const { gate } = await probe.json();
	if (!gate) return { base, gate: false, officer: null, cookie: null, value: null };
	const pin = readPin();
	if (!pin) {
		throw new Error(
			!process.env.VEER_OPERATOR_PIN && process.env.VEER_OPERATOR_PIN_FILE
				? `deck session: VEER_OPERATOR_PIN_FILE (${process.env.VEER_OPERATOR_PIN_FILE}) is missing, unreadable or empty — not falling back to veer.env`
				: 'deck session: the gate is on but no PIN is configured (VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE or veer.env)',
		);
	}
	const res = await fetch(`${base}/api/session`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ officer, pin }),
		signal: AbortSignal.timeout(10000),
	});
	if (res.status !== 200) {
		throw new Error(`deck session: sign-in refused (HTTP ${res.status}) — not retrying (the deck throttles failed sign-ins)`);
	}
	const setCookie = typeof res.headers.getSetCookie === 'function'
		? res.headers.getSetCookie().join(', ')
		: res.headers.get('set-cookie') || '';
	const m = setCookie.match(new RegExp(`(?:^|[,;]\\s*)${COOKIE}=([^;,]+)`));
	if (!m) throw new Error('deck session: the sign-in response carried no session cookie');
	return { base, gate: true, officer, cookie: `${COOKIE}=${m[1]}`, value: m[1] };
}

export function deckFetch(session, pathOrUrl, init = {}) {
	const url = /^https?:\/\//.test(pathOrUrl) ? pathOrUrl : session.base + pathOrUrl;
	const headers = new Headers(init.headers || {});
	if (session.cookie) headers.set('cookie', session.cookie);
	return fetch(url, { ...init, headers });
}

/** Cookies for Puppeteer's page.setCookie(...) — call it before the first page.goto. */
export function puppeteerCookies(session) {
	if (!session.cookie) return [];
	return [{ name: COOKIE, value: session.value, url: session.base, httpOnly: true, sameSite: 'Strict' }];
}

export async function signOut(session) {
	if (!session?.cookie) return;
	await fetch(`${session.base}/api/session`, {
		method: 'DELETE',
		headers: { cookie: session.cookie },
		signal: AbortSignal.timeout(5000),
	}).catch(() => {});
}
