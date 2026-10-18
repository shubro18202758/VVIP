/**
 * Shell navigation — the URLs of the rebuilt deck. design-shell owns the
 * route topology; content streams only rewrite pages placed here, so this
 * list is the single source of nav truth (no link ever 404s).
 *
 * Labels are i18n keys from $lib/i18n (packs.ts registers en/hi/gu).
 */

export interface NavItem {
	href: string;
	/** i18n key for the label. */
	key: string;
	/** One-glyph glyph; symbology outranks iconography. */
	glyph: string;
}

/** Operator surfaces (command theme). */
export const DECK_NAV: readonly NavItem[] = [
	{ href: '/', key: 'nav_dashboard', glyph: '◈' },
	{ href: '/ops', key: 'nav_ops', glyph: '⬢' },
	{ href: '/gods-eye', key: 'nav_gods_eye', glyph: '◎' },
	{ href: '/comms', key: 'nav_comms', glyph: '✎' },
	{ href: '/chat', key: 'nav_chat', glyph: '◇' },
	{ href: '/design', key: 'nav_design', glyph: '▤' }
];

/** Citizen/driver surfaces (daylight theme) — linked from the operator rail. */
export const PUBLIC_NAV: readonly NavItem[] = [
	{ href: '/citizen', key: 'nav_citizen', glyph: '◉' },
	{ href: '/driver', key: 'nav_driver', glyph: '✚' }
];

/** Search index for the command bar: nav targets only (chrome's own data). */
export const NAV_INDEX: readonly { href: string; key: string; terms: string }[] = [
	...DECK_NAV,
	...PUBLIC_NAV
].map((n) => ({
	href: n.href,
	key: n.key,
	terms: `${n.href} ${n.key} ${n.glyph}`.toLowerCase()
}));
