/**
 * Design-token gate — four suites, zero browser:
 *
 *   1. PARITY      tokens.css ↔ tokens.ts: identical variable key sets and
 *                  values for BOTH themes (extend both files together).
 *   1b. CONTRAST   every TEXT ink (ink-1/2/3) reaches WCAG AA 4.5:1 on the
 *                  canvas, surface-1 and surface-2 of its theme; the CTA
 *                  fill (daylight act-2) carries paper text at ≥ 4.5:1; the
 *                  act text ink reads on its surfaces; and the type floor
 *                  holds (operator ≥ 12 px, public meta ≥ 13 px). ink-4 is
 *                  for rules and hairlines only — never asserted as text.
 *   2. HEX SWEEP   no hex literal outside the two token files on every
 *                  surface that is on the token system today (Svelte
 *                  <style> blocks included — what eslint cannot parse).
 *   3. BOUNDARIES  the R4 import graph, enforced textually so .svelte files
 *                  are covered exactly like eslint covers .ts (design-shell
 *                  never imports ops/gods-eye; public/comms never import
 *                  the legacy stores; tokens.css imported once).
 *
 * Usage: node tests/design-tokens.mjs   (exit 1 on any violation)
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const SRC = path.join(DECK, 'src');

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

// ── helpers ─────────────────────────────────────────────────────────────

function walk(root) {
	const out = [];
	for (const entry of readdirSync(root)) {
		const p = path.join(root, entry);
		const st = statSync(p);
		if (st.isDirectory()) out.push(...walk(p));
		else out.push(p);
	}
	return out;
}

function walkMaybe(rel) {
	try {
		return walk(path.join(SRC, rel));
	} catch {
		return [];
	}
}

const norm = (v) => v.replace(/\s+/g, ' ').trim();

/** Extract `--name: value;` pairs from one flat CSS block. */
function cssVars(blockText) {
	const map = new Map();
	const re = /(--v-[a-z0-9-]+)\s*:\s*([^;]+);/g;
	let m;
	while ((m = re.exec(blockText))) {
		if (map.has(m[1])) throw new Error(`duplicate ${m[1]} in one block`);
		map.set(m[1], norm(m[2]));
	}
	return map;
}

function sliceBlock(text, startMarker) {
	const start = text.indexOf(startMarker);
	if (start < 0) throw new Error(`marker not found: ${startMarker}`);
	const open = text.indexOf('{', start);
	const end = text.indexOf('\n}', open);
	return text.slice(open + 1, end);
}

// ── 1. PARITY: tokens.css ↔ tokens.ts ──────────────────────────────────

const cssText = readFileSync(path.join(SRC, 'lib/design/tokens.css'), 'utf8');
const tsText = readFileSync(path.join(SRC, 'lib/design/tokens.ts'), 'utf8');

const cssCommand = cssVars(sliceBlock(cssText, ":root,\n[data-theme='command']"));
const cssDaylight = cssVars(sliceBlock(cssText, "[data-theme='daylight']"));

function tsVars(sectionText) {
	const map = new Map();
	// values may be single- OR double-quoted (the font stacks quote family
	// names inside, so they are written as double-quoted TS strings).
	const re = /'(--v-[a-z0-9-]+)':\s*(['"])((?:\\.|(?!\2).)*)\2/g;
	let m;
	while ((m = re.exec(sectionText))) {
		if (map.has(m[1])) throw new Error(`duplicate ${m[1]} in ts section`);
		map.set(m[1], norm(m[3]));
	}
	return map;
}

const cmdStart = tsText.indexOf('command: {');
const dayStart = tsText.indexOf('daylight: {');
if (cmdStart < 0 || dayStart < 0 || dayStart < cmdStart) {
	ok('tokens.ts sections found', false, 'expected command then daylight sections');
	process.exit(1);
}
const tsCommand = tsVars(tsText.slice(cmdStart, dayStart));
const tsDaylight = tsVars(tsText.slice(dayStart));

const sameKeys = (a, b) => a.size === b.size && [...a.keys()].every((k) => b.has(k));

ok(
	'parity: css command keys === ts command keys',
	sameKeys(cssCommand, tsCommand),
	`${cssCommand.size} vars`
);
ok(
	'parity: css daylight keys === ts daylight keys',
	sameKeys(cssDaylight, tsDaylight),
	`${cssDaylight.size} vars`
);
ok(
	'parity: command and daylight share ONE variable list',
	sameKeys(cssCommand, cssDaylight),
	'both themes define the same names'
);

const valueDiff = (a, b, label) => {
	const bad = [...a.keys()].filter((k) => b.has(k) && a.get(k) !== b.get(k));
	ok(`parity: ${label} values match`, bad.length === 0, bad.map((k) => `${k} css=${a.get(k)} ts=${b.get(k)}`).join(' | '));
};
valueDiff(cssCommand, tsCommand, 'command');
valueDiff(cssDaylight, tsDaylight, 'daylight');

// ── 1b. CONTRAST (WCAG relative luminance) ──────────────────────────────

function luminance(hex) {
	const m = /^#([0-9a-f]{6})$/i.exec(hex ?? '');
	if (!m) return null;
	const c = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255);
	const lin = c.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
	return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

function contrast(a, b) {
	const la = luminance(a);
	const lb = luminance(b);
	if (la == null || lb == null) return null;
	return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

const AA = 4.5;
for (const [theme, vars] of [
	['command', cssCommand],
	['daylight', cssDaylight],
]) {
	const fails = [];
	for (const ink of ['--v-ink-1', '--v-ink-2', '--v-ink-3']) {
		for (const bg of ['--v-canvas', '--v-surface-1', '--v-surface-2']) {
			const r = contrast(vars.get(ink), vars.get(bg));
			if (r == null || r < AA) fails.push(`${ink} on ${bg} = ${r == null ? 'n/a' : r.toFixed(2)}`);
		}
	}
	const act = contrast(vars.get('--v-act-text'), vars.get('--v-surface-1'));
	if (act == null || act < AA) fails.push(`--v-act-text on --v-surface-1 = ${act?.toFixed(2) ?? 'n/a'}`);
	ok(`contrast: ${theme} text inks ≥ ${AA}:1 on canvas / surface-1 / surface-2`, fails.length === 0, fails.join(' | ') || 'AA');
}
const cta = contrast(cssDaylight.get('--v-act-2'), cssDaylight.get('--v-paper'));
ok('contrast: daylight CTA fill (act-2) carries paper text ≥ 4.5:1', cta != null && cta >= AA, cta?.toFixed(2) ?? 'n/a');

const px = (v) => (v && /^\d+(\.\d+)?px$/.test(v) ? Number.parseFloat(v) : NaN);
const floorFails = [];
for (const k of ['--v-text-2xs', '--v-text-xs', '--v-text-sm']) {
	if (!(px(cssCommand.get(k)) >= 12)) floorFails.push(`command ${k}=${cssCommand.get(k)}`);
	if (!(px(cssDaylight.get(k)) >= 13)) floorFails.push(`daylight ${k}=${cssDaylight.get(k)}`);
}
ok('type floor: operator text ≥ 12 px, public meta ≥ 13 px', floorFails.length === 0, floorFails.join(' | ') || 'floors hold');

// ── 2. HEX SWEEP ────────────────────────────────────────────────────────
//
// Enforced on every surface that is ON the token system today. The frozen
// legacy tree and the not-yet-rebuilt ops/gods-eye/citizen panels are hex-
// dense by grounding and OUT of scope until their owning stream lands them
// on primitives — extend HEX_SWEEP_TARGETS in that same commit.
const TOKEN_FILES = new Set(['tokens.ts', 'tokens.css']);
const HEX_SWEEP_TARGETS = [
	'lib/design',
	'lib/primitives',
	'lib/shell',
	'lib/i18n',
	// citizen-screens surfaces landed on primitives — targets extended in
	// that stream's commit per the rule above (coverage added, never removed).
	'lib/citizen',
	'lib/comms',
	// the restored template cockpit (2026-10-14 merge): built token-only
	// from day one, so it joins the sweep in the same commit (coverage
	// added, never removed).
	'lib/ops/components/template',
	'routes/(deck)/+page.svelte',
	'routes/(deck)/design',
	'routes/(deck)/comms',
	'routes/(deck)/chat',
	'routes/(deck)/+layout.svelte',
	'routes/(public)/citizen',
	'routes/(public)/driver',
	'routes/(public)/+layout.svelte',
	'routes/+layout.svelte'
];

const HEX_RE = /#[0-9a-fA-F]{3,8}/g;
const hexViolations = [];
for (const target of HEX_SWEEP_TARGETS) {
	const p = path.join(SRC, target);
	let files = [];
	try {
		files = statSync(p).isDirectory() ? walk(p) : [p];
	} catch {
		hexViolations.push(`${target}: target missing (topology broken?)`);
		continue;
	}
	for (const f of files) {
		if (TOKEN_FILES.has(path.basename(f)) && path.dirname(f).endsWith('design')) continue;
		const rel = path.relative(DECK, f);
		const lines = readFileSync(f, 'utf8').split('\n');
		lines.forEach((line, i) => {
			for (const m of line.matchAll(HEX_RE)) {
				// Svelte block tags ("#each" → matches "#eac", h is not hex)
				// are keywords, never colours: a hex colour is never
				// immediately followed by a letter.
				const after = line[m.index + m[0].length] ?? '';
				if (/[a-zA-Z]/.test(after)) continue;
				hexViolations.push(`${rel}:${i + 1}: ${line.trim().slice(0, 90)}`);
			}
		});
	}
}
ok(
	'hex sweep: no hex literal outside the token files',
	hexViolations.length === 0,
	hexViolations.length ? hexViolations.slice(0, 8).join(' | ') : `${HEX_SWEEP_TARGETS.length} targets clean`
);

// ── 3. BOUNDARIES (the R4 import graph, textual so .svelte is covered) ─

// Matches BOTH import forms: `… from '<spec>'` and side-effect `import '<spec>'`.
// (Probe-tested: a named `$lib/ops` import appended to a shell file MUST fire.)
const IMPORT_RE = /(?:from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"])/g;
function importsOf(file) {
	const text = readFileSync(file, 'utf8');
	const list = [];
	let m;
	while ((m = IMPORT_RE.exec(text))) list.push(m[1] ?? m[2]);
	return list;
}

const rel = (f) => path.relative(DECK, f);

// R4-1: design system never imports ops/gods-eye state.
const designFiles = [
	...walkMaybe('lib/design'),
	...walkMaybe('lib/primitives'),
	...walkMaybe('lib/shell'),
	path.join(SRC, 'routes/(deck)/+layout.svelte')
].filter((f) => /\.(ts|js|svelte)$/.test(f));
const rule1 = [];
for (const f of designFiles) {
	for (const imp of importsOf(f)) {
		if (/^\$lib\/(ops|gods-eye)(\/|$)/.test(imp)) rule1.push(`${rel(f)} → ${imp}`);
	}
}
ok(
	'boundary: design/shell/primitives never import $lib/ops|$lib/gods-eye',
	rule1.length === 0,
	rule1.slice(0, 5).join(' | ')
);

// R4-2: public/comms surfaces never import the legacy data-service stores.
const publicFiles = [
	...walkMaybe('lib/citizen'),
	...walkMaybe('lib/comms'),
	...walkMaybe('routes/(public)'),
	...walkMaybe('routes/(deck)/chat'),
	...walkMaybe('routes/(deck)/comms')
].filter((f) => /\.(ts|js|svelte)$/.test(f));
const rule2 = [];
for (const f of publicFiles) {
	for (const imp of importsOf(f)) {
		if (/^(\$stores|\$lib\/stores)(\/|$)/.test(imp) || imp === '$lib/api/data-service') {
			rule2.push(`${rel(f)} → ${imp}`);
		}
	}
}
ok(
	'boundary: public/comms never import legacy stores or data-service',
	rule2.length === 0,
	rule2.slice(0, 5).join(' | ')
);

// R4-3: tokens.css is imported exactly once, by the root layout.
const rule3 = [];
for (const f of walk(SRC)) {
	if (!/\.(ts|js|svelte)$/.test(f)) continue;
	for (const imp of importsOf(f)) {
		if (imp.includes('design/tokens.css') && rel(f) !== 'src/routes/+layout.svelte') {
			rule3.push(`${rel(f)} → ${imp}`);
		}
	}
}
ok('boundary: tokens.css imported only by the root layout', rule3.length === 0, rule3.join(' | '));

// ── result ──────────────────────────────────────────────────────────────

if (failures > 0) {
	console.error(`design-tokens: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   design-tokens — parity, hex sweep and boundaries all pass');
process.exit(0);
