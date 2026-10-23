/**
 * showcase_screenshots.mjs — evidence capture for the flagship showcase run.
 *
 * NOT a test: a camera. It photographs the argus command deck (the
 * SvelteKit deck; default the live origin http://127.0.0.1:5273, which
 * proxies /api/v1 to the traffic-oracle) while the flagship corridor event
 * is ACTIVE in the real stack:
 *
 *   1. showcase_dashboard.png        /ops — the situation wall; its event
 *                                     board lists the live corridor events
 *                                     (GET /api/v1/events/active through the
 *                                     deck, signed in)
 *   2. showcase_citizen_advisory.png /citizen — the public advisory (the
 *                                     anonymous, plate-masked surface) with
 *                                     the corridor restriction on a card
 *   3. showcase_green_corridor.png   /driver — the 108 driver's flow at idle:
 *                                     a live corridor's stretches on offer,
 *                                     or — while the flagship is an EMULATED
 *                                     run — the honest note that emulated
 *                                     corridors are withheld from requests
 *
 * Read-only: it never clicks a request, an approval or any other control.
 * A green-corridor window opens only when a NAMED duty officer approves the
 * driver's request on /ops (C6); a script must not stand in for that
 * officer, so the window is not photographed open here.
 *
 * Operator gate: ONE sign-in through scripts/lib/deck_session.mjs (one
 * attempt, PIN read at run time, never logged); the cookie is set on the
 * /ops page before its navigation and the session is revoked on exit. The
 * public pages are photographed anonymously, as the public sees them.
 *
 * Usage: node showcase_screenshots.mjs [deck-origin] [out-dir]
 * Env:   CHROME_PATH (defaults to the puppeteer cache path used in
 *        development, same default as frontend/tests/e2e); VEER_OPERATOR_PIN,
 *        else VEER_OPERATOR_PIN_FILE (a sandbox deck's PIN), else veer.env.
 * Exit:  0 all three surfaces show what they must; 1 incomplete; 2 harness
 *        error (including a refused sign-in).
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { deckFetch, puppeteerCookies, signIn, signOut } from './lib/deck_session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ORIGIN = (process.argv[2] || 'http://127.0.0.1:5273').replace(/\/+$/, '');
const OUT = process.argv[3] || 'showcase';
const CHROME = process.env.CHROME_PATH
  || `${process.env.HOME}/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome`;

// puppeteer-core resolves from the frontend's node_modules (the same tree
// the e2e harness uses); ESM ignores NODE_PATH, so import by file URL.
const PUPPETEER_ENTRY = path.join(
  HERE, '..', 'frontend', 'node_modules', 'puppeteer-core',
  'lib', 'cjs', 'puppeteer', 'puppeteer-core.js',
);
if (!fs.existsSync(PUPPETEER_ENTRY)) {
  console.error(`puppeteer-core not found at ${PUPPETEER_ENTRY} (run npm ci in frontend/)`);
  process.exit(2);
}
const puppeteer = await import(pathToFileURL(PUPPETEER_ENTRY).href);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = (page, name) => page.screenshot({
  path: path.join(OUT, name),
  fullPage: false,
  captureBeyondViewport: false,
});

const results = { chrome: CHROME, origin: ORIGIN, shots: {} };
let session = null;

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  session = await signIn(ORIGIN, 'Showcase screenshots (automated check)');
  results.operatorGate = session.gate ? 'on (signed in for /ops)' : 'off';
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
  });
  try {
    // ── 1. /ops: the live corridor events on the situation wall ──────────
    const ops = await browser.newPage();
    await ops.setViewport({ width: 1680, height: 950 });
    if (session.gate) await ops.setCookie(...puppeteerCookies(session));
    ops.on('pageerror', (e) => { results.pageErrors ||= []; results.pageErrors.push(String(e)); });
    await ops.goto(`${ORIGIN}/ops`, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await ops.waitForSelector('.wall', { timeout: 30000 });
    await sleep(6000); // the board's poll rung lands the live events
    const feed = await deckFetch(session, '/api/v1/events/active', { signal: AbortSignal.timeout(15000) })
      .then(async (r) => ({ status: r.status, events: r.ok ? ((await r.json()).events || []) : [] }))
      .catch((e) => ({ status: 0, events: [], error: String(e) }));
    const board = await ops.evaluate(() => ({
      eventRows: document.querySelectorAll('.event-card, .board .event, article[data-event]').length,
      band: document.querySelector('[data-prim="band-masthead"]')?.getAttribute('data-band') ?? null,
    }));
    results.dashboardLiveEvents = { http: feed.status, active: feed.events.length, ids: feed.events.map((e) => e.event_id), ...board };
    results.dashboardShowsActive = feed.status === 200 && feed.events.length >= 1 && board.eventRows >= 1;
    await shot(ops, 'showcase_dashboard.png');
    results.shots.dashboard = 'showcase_dashboard.png';
    await ops.close();

    // ── 2. /citizen: the public advisory (anonymous — as the public sees it)
    const citizen = await browser.newPage();
    await citizen.setViewport({ width: 1440, height: 900 });
    await citizen.goto(`${ORIGIN}/citizen`, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await citizen.waitForSelector('[data-screen="citizen"]', { timeout: 30000 });
    await sleep(6500); // the public feed's poll rung lands the events
    const adv = await citizen.evaluate(() => ({
      strip: document.querySelector('[data-strip]')?.getAttribute('data-strip') ?? null,
      cards: document.querySelectorAll('.event-card').length,
      sources: [...document.querySelectorAll('.event-card')].map((c) => c.getAttribute('data-source')),
    }));
    results.advisory = adv;
    results.advisoryActive = !!adv.strip && adv.cards >= 1;
    await shot(citizen, 'showcase_citizen_advisory.png');
    results.shots.citizenAdvisory = 'showcase_citizen_advisory.png';
    await citizen.close();

    // ── 3. /driver: the 108 flow at idle — live stretches on offer, or the
    //       emulated corridors honestly withheld (never a request filed) ──
    const driver = await browser.newPage();
    await driver.setViewport({ width: 1440, height: 900 });
    await driver.goto(`${ORIGIN}/driver`, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await driver.waitForSelector('[data-screen="driver"]', { timeout: 30000 });
    await sleep(6500);
    const flow = await driver.evaluate(() => {
      const root = document.querySelector('[data-screen="driver"]');
      return {
        phase: root?.getAttribute('data-phase') ?? null,
        mode: root?.getAttribute('data-mode') ?? null,
        stretches: document.querySelectorAll('[data-testid="stretch-select"] option:not([value=""])').length,
        emulatedWithheld: Number(document.querySelector('[data-testid="driver-emulated-withheld"]')?.getAttribute('data-count') ?? 0),
      };
    });
    results.driver = flow;
    results.driverFlowHonest = flow.phase === 'idle'
      && ((flow.mode === 'live' && flow.stretches >= 1) || (flow.mode === 'scripted' && flow.emulatedWithheld >= 1));
    await shot(driver, 'showcase_green_corridor.png');
    results.shots.greenCorridor = 'showcase_green_corridor.png';
    await driver.close();
  } finally {
    await browser.close();
    await signOut(session);
  }
  fs.writeFileSync(path.join(OUT, 'screenshot_manifest.json'), JSON.stringify(results, null, 2));
  const ok = results.dashboardShowsActive && results.advisoryActive && results.driverFlowHonest;
  console.log(JSON.stringify(results, null, 2));
  console.log(ok ? 'SCREENSHOTS: OK' : 'SCREENSHOTS: INCOMPLETE');
  process.exit(ok ? 0 : 1);
}

main().catch(async (e) => {
  await signOut(session);
  console.error('HARNESS ERROR', e);
  process.exit(2);
});
