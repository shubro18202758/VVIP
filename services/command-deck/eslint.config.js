// @ts-check
/**
 * Flat ESLint config for the command deck — JavaScript, TypeScript and Svelte.
 * `eslint ^9.16` is a devDependency and `lint: eslint .` is scripted
 * (package.json).
 *
 * WHAT IT LINTS
 *   .js/.mjs/.cjs    @eslint/js recommended
 *   .ts, .svelte.ts  + typescript-eslint recommended (syntax only — no type
 *                    information, so `eslint .` needs no build and stays fast)
 *   .svelte          + eslint-plugin-svelte recommended; <script lang="ts">
 *                    parsed by typescript-eslint
 *
 * WHAT IT ENFORCES on all of them (the R4 import graph, owned by
 * design-shell):
 *   1. design/shell/primitives + (deck)/+layout.svelte may never import
 *      $lib/ops/** or $lib/gods-eye/** (one-way: pages → shell).
 *   2. citizen/comms + (public)/** + (deck)/chat|comms may never import
 *      $lib/stores/** or $lib/api/data-service (legacy feeds stay legacy).
 *   3. only the root layout may import $lib/design/tokens.css.
 * tests/design-tokens.mjs keeps the textual twin of these boundaries (and the
 * hex sweep eslint cannot express); it runs in scripts/verify_all.sh and CI.
 * Mono-for-mutable-numbers and orange-area discipline remain REVIEW-enforced
 * (eslint cannot see rendered semantics).
 *
 * The TypeScript and Svelte parsers are devDependencies (typescript-eslint,
 * eslint-plugin-svelte, svelte-eslint-parser, globals). Where they are not
 * installed this config says so on stderr and lints the .js/.mjs/.cjs files
 * only — it never reports .ts/.svelte as linted when they were not.
 */

/** an optional devDependency, or null when it is not installed */
async function optional(name) {
	try {
		const mod = await import(name);
		return mod.default ?? mod;
	} catch {
		return null;
	}
}

const js = await optional('@eslint/js');
const ts = await optional('typescript-eslint');
const svelte = await optional('eslint-plugin-svelte');
const globals = await optional('globals');
const full = Boolean(ts && svelte);
if (!full) {
	console.warn(
		'eslint.config.js: typescript-eslint / eslint-plugin-svelte are not installed — linting .js/.mjs/.cjs ONLY; ' +
			'.ts and .svelte are NOT linted (npm install the deck devDependencies)'
	);
}

const CODE = full ? '{js,mjs,cjs,ts,svelte}' : '{js,mjs,cjs}';
const TOKENS_CSS = {
	name: '$lib/design/tokens.css',
	message: 'tokens.css is imported once by the root layout; consume var(--v-*) or tokens.ts instead.'
};
/** ONE no-restricted-imports per block that carries every boundary its files
 *  are under: when two blocks match a file, flat config REPLACES the rule's
 *  options instead of merging them */
const restrict = (patterns = []) => ['error', { paths: [TOKENS_CSS], patterns }];

export default [
	{
		ignores: ['build/**', 'node_modules/**', 'static/**', 'tests/artifacts/**', '.svelte-kit/**']
	},
	...(js ? [js.configs.recommended] : []),
	...(full ? ts.configs.recommended : []),
	...(full ? svelte.configs.recommended : []),
	{
		languageOptions: {
			ecmaVersion: 'latest',
			sourceType: 'module',
			globals: globals ? { ...globals.browser, ...globals.node } : {}
		}
	},
	...(full
		? [
				{
					files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
					languageOptions: { parserOptions: { parser: ts.parser, extraFileExtensions: ['.svelte'] } }
				},
				// TypeScript resolves names itself (DOM, node and the ambient
				// declarations); no-undef would only repeat it, wrongly
				{ files: ['**/*.ts', '**/*.svelte'], rules: { 'no-undef': 'off' } }
			]
		: []),
	// Rule 3: tokens.css is imported exactly once, by the root layout.
	{
		files: [`src/**/*.${CODE}`],
		ignores: ['src/routes/+layout.svelte'],
		rules: { 'no-restricted-imports': restrict() }
	},
	// Rule 1: design system surfaces never reach into stream state
	// ($lib/ops/**, $lib/gods-eye/** — one-way: pages → shell).
	{
		files: [
			`src/lib/shell/**/*.${CODE}`,
			`src/lib/primitives/**/*.${CODE}`,
			`src/lib/design/**/*.${CODE}`,
			...(full ? ['src/routes/\\(deck\\)/+layout.svelte'] : [])
		],
		rules: {
			'no-restricted-imports': restrict([
				'$lib/ops',
				'$lib/ops/*',
				'$lib/ops/**',
				'$lib/gods-eye',
				'$lib/gods-eye/*',
				'$lib/gods-eye/**'
			])
		}
	},
	// Rule 2: public/comms surfaces never import the legacy data-service
	// stores ($stores/** aliases included).
	{
		files: [
			`src/lib/citizen/**/*.${CODE}`,
			`src/lib/comms/**/*.${CODE}`,
			`src/routes/\\(public\\)/**/*.${CODE}`,
			`src/routes/\\(deck\\)/chat/**/*.${CODE}`,
			`src/routes/\\(deck\\)/comms/**/*.${CODE}`
		],
		rules: {
			'no-restricted-imports': restrict([
				'$lib/stores',
				'$lib/stores/*',
				'$lib/stores/**',
				'$stores',
				'$stores/*',
				'$stores/**',
				'$lib/api/data-service'
			])
		}
	}
];
