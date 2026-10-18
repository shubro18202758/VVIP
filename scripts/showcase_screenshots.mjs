/**
 * showcase_screenshots.mjs — evidence capture for the flagship showcase run.
 *
 * NOT a test: a camera. It photographs the live showcase interface at the
 * command-deck origin (default http://localhost:5173, the nginx deck that
 * proxies /api/v1 -> traffic-oracle :8081 and /api/agent -> convoy-brain
 * :8080) while the flagship corridor event is ACTIVE in the real stack:
 *
 *   1. showcase_dashboard.png       — ops console; the ACTIVE CORRIDOR
 *                                     EVENTS panel lists the live injected
 *                                     flagship movement (polled from
 *                                     GET /api/v1/movements/active)
 *   2. showcase_citizen_advisory.png— citizen advisory with an active
 *                                     corridor restriction rendered
 *   3. showcase_green_corridor.png  — the 108 driver's view with a
 *                                     green-corridor WINDOW OPEN
 *
 * Usage: node showcase_screenshots.mjs <deck-origin> <out-dir>
 * Env:   CHROME_PATH (defaults to the puppeteer cache path used in
 *        development, same default as frontend/tests/e2e).
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ORIGIN = process.argv[2] || 'http://localhost:5173';
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

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1680, height: 950 });
  page.on('pageerror', (e) => { results.pageErrors ||= []; results.pageErrors.push(String(e)); });

  // ── 1. ops dashboard: live flagship movement in the corridor panel ──────
  await page.goto(ORIGIN, { waitUntil: 'networkidle2', timeout: 45000 });
  await sleep(4000); // let the live polls land (summary 10s, movements 15s)
  const dash = await page.evaluate(async () => {
    const r = await fetch('/api/v1/movements/active');
    const j = await r.json();
    return { active: (j.movements || []).length, ids: (j.movements || []).map((m) => m.movement_id) };
  });
  results.dashboardLiveMovements = dash;
  // the live proof: the deck's own proxy serves the flagship event in the
  // active-movements feed AND the header renders the count
  const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 20000));
  results.dashboardShowsActive = dash.active >= 1 && /\bACTIVE\b/.test(bodyText);
  await shot(page, 'showcase_dashboard.png');
  results.shots.dashboard = 'showcase_dashboard.png';

  // ── 2. citizen advisory: active corridor restriction ────────────────────
  await page.evaluate(() => {
    [...document.querySelectorAll('button')]
      .find((b) => b.textContent.includes('CITIZEN ALERTS'))?.click();
  });
  await sleep(800);
  await page.evaluate(() => {
    [...document.querySelectorAll('button')]
      .find((b) => b.textContent.includes('Play a demo movement'))?.click();
  });
  await sleep(3500); // movement goes live; freeze schedule renders
  const advText = await page.evaluate(() => document.body.innerText.slice(0, 20000));
  results.advisoryActive = /in progress/i.test(advText) || /hold/i.test(advText);
  await shot(page, 'showcase_citizen_advisory.png');
  results.shots.citizenAdvisory = 'showcase_citizen_advisory.png';

  // ── 3. green corridor: request -> proposal -> WINDOW OPEN ───────────────
  await page.evaluate(() => {
    [...document.querySelectorAll('button')]
      .find((b) => b.textContent.includes('Request green corridor'))?.click();
  });
  await sleep(500);
  await page.evaluate(() => {
    [...document.querySelectorAll('button')]
      .find((b) => b.textContent.toLowerCase().includes('driver view'))?.click();
  });
  await sleep(800);
  await page.evaluate(() => {
    [...document.querySelectorAll('button')]
      .find((b) => b.textContent.includes('Request corridor window'))?.click();
  });
  await sleep(1600);
  await page.evaluate(() => {
    [...document.querySelectorAll('button')]
      .find((b) => b.textContent.includes('Approve'))?.click();
  });
  await sleep(900);
  const gcText = await page.evaluate(() => document.body.innerText.slice(0, 20000));
  results.greenCorridorWindowOpen = /WINDOW OPEN/i.test(gcText);
  await shot(page, 'showcase_green_corridor.png');
  results.shots.greenCorridor = 'showcase_green_corridor.png';

  await browser.close();
  fs.writeFileSync(path.join(OUT, 'screenshot_manifest.json'), JSON.stringify(results, null, 2));
  const ok = results.dashboardShowsActive && results.advisoryActive && results.greenCorridorWindowOpen;
  console.log(JSON.stringify(results, null, 2));
  console.log(ok ? 'SCREENSHOTS: OK' : 'SCREENSHOTS: INCOMPLETE');
  process.exit(ok ? 0 : 1);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
