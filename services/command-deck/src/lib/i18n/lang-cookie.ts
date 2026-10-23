/**
 * Language persistence (contract C-M) — the `veer_lang` cookie.
 *
 * The chosen UI language must survive a full reload: signing in or out of
 * the operator gate reloads the page (every panel re-reads under the new
 * session), and a Gujarati or Hindi choice used to snap back to English.
 * The cookie is the one place the choice lives:
 *
 *   veer_lang = en | hi | gu      path=/  SameSite=Lax  1 year
 *
 * NOT HttpOnly on purpose: the browser writes it on every language change
 * (CommandShell, CitizenShell) and the ops store reads it as its initial
 * language; the server reads the same cookie for the document's <html
 * lang> (hooks.server.ts documentDefaults), so the first paint is already
 * in the chosen language. A missing or unknown value means "no choice
 * made" — every surface keeps its own route default (/driver Gujarati,
 * everything else English); an invalid value is never trusted.
 */

export const LANG_COOKIE = 'veer_lang';

/** The languages the deck ships (strings.ts SUPPORTED). */
export const LANG_CODES = ['en', 'hi', 'gu'] as const;
export type LangChoice = (typeof LANG_CODES)[number];

const MAX_AGE_SEC = 365 * 24 * 3600;

export function isLangChoice(v: unknown): v is LangChoice {
	return typeof v === 'string' && (LANG_CODES as readonly string[]).includes(v);
}

/**
 * The language a Cookie header (or `document.cookie`) carries, or null when
 * none / an unknown value is set. Malformed escapes never throw.
 */
export function langFromCookieHeader(header: string | null | undefined): LangChoice | null {
	if (!header) return null;
	for (const part of header.split(';')) {
		const eq = part.indexOf('=');
		if (eq <= 0 || part.slice(0, eq).trim() !== LANG_COOKIE) continue;
		let value: string;
		try {
			value = decodeURIComponent(part.slice(eq + 1).trim());
		} catch {
			continue;
		}
		if (isLangChoice(value)) return value;
	}
	return null;
}

/** The browser's persisted choice (null in SSR, in tests, or when none was made). */
export function readLangCookie(): LangChoice | null {
	if (typeof document === 'undefined') return null;
	try {
		return langFromCookieHeader(document.cookie);
	} catch {
		return null; // cookies disabled — the session keeps its default
	}
}

/**
 * Persist an explicit language choice (an operator or citizen pressed EN /
 * हिं / ગુ). Unknown codes are ignored; never called for a route's default.
 */
export function writeLangCookie(lang: string): void {
	if (typeof document === 'undefined' || !isLangChoice(lang)) return;
	try {
		document.cookie = `${LANG_COOKIE}=${lang}; path=/; max-age=${MAX_AGE_SEC}; SameSite=Lax`;
	} catch {
		// cookies disabled — the choice holds for this page only
	}
}
