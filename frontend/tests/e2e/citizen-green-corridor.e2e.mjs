/**
 * End-to-end smoke test (headless Chromium): the citizen + emergency story.
 *
 * Journey verified: operator console → CITIZEN ALERTS → advisory page
 * (idle → live movement, freeze schedule, alternates, language toggle,
 * subscribe demo) → quick green-corridor request → 108 driver view →
 * request → proposal → duty-officer approve → WINDOW OPEN with live
 * countdown → back-link.
 *
 * Runs entirely against the scripted demo simulation — no backend needed.
 *
 * Usage (from frontend/):
 *   npm i --no-save puppeteer-core
 *   npx esbuild tests/e2e/entry.jsx --bundle --format=esm \
 *     --loader:.jsx=jsx --outfile=tests/e2e/build/test-bundle.js
 *   node tests/e2e/citizen-green-corridor.e2e.mjs
 *
 * Requires a Chrome binary; set CHROME_PATH (defaults to the puppeteer
 * cache path used in development).
 */
import puppeteer from 'puppeteer-core';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = HERE; // serves index.html + build/test-bundle.js
const BUILD = path.join(HERE, 'build');
const CHROME = process.env.CHROME_PATH
  || `${process.env.HOME}/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome`;

const MIME = { '.html': 'text/html', '.js': 'text/javascript' };
const server = http.createServer((req, res) => {
  const rel = req.url === '/' ? 'index.html' : req.url;
  const p = path.join(ROOT, rel);
  try {
    const body = fs.readFileSync(p);
    res.writeHead(200, { 'content-type': MIME[path.extname(p)] || 'text/plain' });
    res.end(body);
  } catch { res.writeHead(404); res.end('nf'); }
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (!fs.existsSync(path.join(BUILD, 'test-bundle.js'))) {
    console.error('Build the bundle first (see header comment).');
    process.exit(2);
  }
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const url = `http://127.0.0.1:${server.address().port}/`;

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 420, height: 900 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  const R = {};
  await page.goto(url, { waitUntil: 'networkidle0', timeout: 30000 });
  await sleep(500);

  R.dashboard = await page.evaluate(() => {
    const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('CITIZEN ALERTS'));
    if (btn) btn.click();
    return !!btn;
  });
  await sleep(600);

  let c = await page.content();
  R.advisoryIdle = c.includes('No active corridor restrictions') && c.includes('हिंदी');

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'ગુજરાતી')?.click();
  });
  await sleep(400);
  R.gujarati = (await page.content()).includes('કોરિડોર એલર્ટ');
  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'English')?.click();
  });
  await sleep(300);

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Play a demo movement'))?.click();
  });
  await sleep(3000);
  c = await page.content();
  R.activeStatus = c.includes('Protected movement in progress');
  R.freezeSchedule = c.includes('Where holds are expected') && c.includes('Shahibag') && c.includes('Use instead');

  await page.type('input[inputmode="numeric"]', '9876543210', { delay: 10 });
  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Subscribe'))?.click();
  });
  await sleep(500);
  c = await page.content();
  R.subscribed = c.includes('Subscribed') && c.includes('no real SMS');

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Request green corridor'))?.click();
  });
  await sleep(400);
  c = await page.content();
  R.quickRequest = c.includes('108-AHM-0000');

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.toLowerCase().includes('driver view'))?.click();
  });
  await sleep(600);
  c = await page.content();
  R.driverView = c.includes('Green Corridor') && c.includes('Request corridor window');

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Request corridor window'))?.click();
  });
  await sleep(1400);
  c = await page.content();
  R.proposal = c.includes('Window proposed by the platform') && c.includes('Convoy reaches your stretch in');

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Approve'))?.click();
  });
  await sleep(600);
  c = await page.content();
  R.windowOpen = c.includes('WINDOW OPEN') && c.includes('Window closes in') && c.includes('minutes returned to the golden hour');

  await page.evaluate(() => {
    [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Citizen alerts'))?.click();
  });
  await sleep(500);
  R.backToAdvisory = (await page.content()).includes('Corridor Alerts');

  R.pageErrors = errors;
  await browser.close();
  server.close();

  const failed = Object.entries(R).filter(([k, v]) => typeof v === 'boolean' && !v).map(([k]) => k);
  console.log(JSON.stringify(R, null, 2));
  console.log(failed.length === 0 && errors.length === 0 ? 'E2E SMOKE: PASS' : `E2E SMOKE: FAIL (${failed.join(', ')})`);
  process.exit(failed.length === 0 && errors.length === 0 ? 0 : 1);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
