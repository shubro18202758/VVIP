// @ts-check
/**
 * Flat ESLint config — the R4 import-boundary rules, owned by
 * design-shell. `eslint ^9.16` is already a devDependency and
 * `lint: eslint .` already scripted (package.json).
 *
 * WHAT THIS CONFIG ENFORCES (the import graph):
 *   1. design/shell/primitives + (deck)/+layout.svelte may never import
 *      $lib/ops/** or $lib/gods-eye/** (one-way: pages → shell).
 *   2. citizen/comms + (public)/** + (deck)/chat|comms may never import
 *      $lib/stores/** or $lib/api/data-service (legacy feeds stay legacy).
 *   3. only the root layout may import $lib/design/tokens.css.
 *
 * STATED DOWNGRADE (honest): eslint-plugin-svelte is NOT installed in this
 * tree, so ESLint covers .ts/.js/.svelte.ts modules; the SAME boundary
 * rules for .svelte files are enforced textually by the import-boundary
 * grep suite in tests/design-tokens.mjs (which also carries the hex
 * sweep eslint cannot express). Mono-for-mutable-numbers and orange-area
 * discipline remain REVIEW-enforced (eslint cannot see rendered semantics).
 */
export default [
	{
		ignores: ['build/**', 'node_modules/**', 'static/**', 'tests/artifacts/**', '.svelte-kit/**']
	},
	// Default: baseline JS linting so `eslint .` runs clean and meaningful.
	// STATED LIMIT: @typescript-eslint / eslint-plugin-svelte are NOT
	// installed in this tree, so ESLint itself covers .js/.mjs only — the
	// import-boundary rules for .ts and .svelte are enforced textually (and
	// proven to fire) by the boundary suite in tests/design-tokens.mjs.
	{
		files: ['**/*.{js,mjs,cjs}'],
		languageOptions: {
			ecmaVersion: 'latest',
			sourceType: 'module'
		},
		rules: {}
	},
	// Rule 3 (default deny, then allow the one exception below): tokens.css
	// is imported exactly once, by the root layout.
	// Rule 3: tokens.css is imported exactly once, by the root layout.
	// (JS surface here; the .svelte twin — root layout is the sole legal
	// importer — is enforced by tests/design-tokens.mjs.)
	{
		files: ['src/**/*.{js,mjs,cjs}'],
		rules: {
			'no-restricted-imports': [
				'error',
				{
					paths: [
						{
							name: '$lib/design/tokens.css',
							message:
								'tokens.css is imported once by the root layout; consume var(--v-*) or tokens.ts instead.'
						}
					]
				}
			]
		}
	},
	// (The root layout is the one legal tokens.css importer; it is .svelte,
	// so its exemption lives in the textual twin, not here.)
	// Rule 1: design system surfaces never reach into stream state
	// ($lib/ops/**, $lib/gods-eye/** — one-way: pages → shell). The globs
	// cover what eslint can parse; the identical rule for .ts/.svelte is
	// enforced (and proven to fire) by tests/design-tokens.mjs.
	{
		files: [
			'src/lib/shell/**/*.{js,mjs,cjs}',
			'src/lib/primitives/**/*.{js,mjs,cjs}',
			'src/lib/design/**/*.{js,mjs,cjs}'
		],
		rules: {
			'no-restricted-imports': [
				'error',
				{
					patterns: [
						'$lib/ops',
						'$lib/ops/*',
						'$lib/ops/**',
						'$lib/gods-eye',
						'$lib/gods-eye/*',
						'$lib/gods-eye/**'
					]
				}
			]
		}
	},
	// Rule 2: new public/comms surfaces never import the legacy
	// data-service stores ($stores/** aliases included); .ts/.svelte twin
	// in tests/design-tokens.mjs.
	{
		files: [
			'src/lib/citizen/**/*.{js,mjs,cjs}',
			'src/lib/comms/**/*.{js,mjs,cjs}',
			'src/routes/(public)/**/*.{js,mjs,cjs}',
			'src/routes/(deck)/chat/**/*.{js,mjs,cjs}',
			'src/routes/(deck)/comms/**/*.{js,mjs,cjs}'
		],
		rules: {
			'no-restricted-imports': [
				'error',
				{
					patterns: [
						'$lib/stores',
						'$lib/stores/*',
						'$lib/stores/**',
						'$stores',
						'$stores/*',
						'$stores/**',
						'$lib/api/data-service'
					]
				}
			]
		}
	}
];
