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

/**
 * Operator surfaces (command theme). VEER CITY COMMAND — the fused God's-eye
 * picture of the whole city — leads; the VVIP corridor module ('/') is one
 * module of it, the situation wall the decision desk around the same canvas.
 * The design sheet (/design) is a developer page: its nav entry shows only
 * under the vite dev server (the route itself stays — smokes use it).
 */
const DEV_BUILD: boolean = import.meta.env?.DEV === true;

export const DECK_NAV: readonly NavItem[] = [
	{ href: '/gods-eye', key: 'nav_city_command', glyph: '◎' },
	{ href: '/', key: 'nav_dashboard', glyph: '◈' },
	{ href: '/ops', key: 'nav_ops', glyph: '⬢' },
	{ href: '/comms', key: 'nav_comms', glyph: '✎' },
	{ href: '/chat', key: 'nav_chat', glyph: '◇' },
	...(DEV_BUILD ? [{ href: '/design', key: 'nav_design', glyph: '▤' }] : [])
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
