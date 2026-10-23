/**
 * The deck smokes' handle on the operator PIN gate: the one shared helper in scripts/lib/deck_session.mjs
 * (reads the PIN at runtime, one sign-in attempt, never logs it).
 *
 *   import { signIn, puppeteerCookies } from './lib/operator-session.mjs';
 *   const session = await signIn(origin, 'Gods-eye smoke (automated check)');
 *   await page.setCookie(...puppeteerCookies(session));
 */
export { readPin, signIn, deckFetch, puppeteerCookies, signOut } from '../../../../scripts/lib/deck_session.mjs';
