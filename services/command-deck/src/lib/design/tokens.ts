/**
 * Typed mirror of tokens.css — for code that cannot read CSS custom
 * properties: Cesium/deck.gl color constructors, SVG attributes, canvas 2D,
 * and tests. Everything else must consume var(--v-*) directly.
 *
 * PARITY CONTRACT: every key here exists in tokens.css under BOTH
 * [data-theme='command'] and [data-theme='daylight'] with the same value
 * (tests/design-tokens.mjs asserts it — extend both files together, never
 * one). Values are written to match the CSS text after whitespace
 * collapsing; hex is lowercase in both.
 */

export type Band = 'GREEN' | 'AMBER' | 'RED' | 'BLACK';

export type ThemeName = 'command' | 'daylight';

export type AuditColorLevel = 'info' | 'decision' | 'tool' | 'model' | 'band' | 'error';

export const TOKENS: Record<ThemeName, Record<string, string>> = {
	command: {
		'--v-canvas': '#0a0f1a',
		'--v-surface-1': '#101828',
		'--v-surface-2': '#16213a',
		'--v-surface-3': '#1c2a47',
		'--v-surface-sink': '#070b12',
		'--v-paper': '#101828',
		'--v-line-1': 'rgba(255, 255, 255, 0.08)',
		'--v-line-2': 'rgba(255, 255, 255, 0.14)',
		'--v-line-select': 'rgba(86, 180, 245, 0.35)',
		'--v-ink-1': '#e8eef8',
		'--v-ink-2': '#9aa7bd',
		'--v-ink-3': '#7d8aa5',
		'--v-ink-4': '#39455c',
		'--v-act': '#ea580c',
		'--v-act-2': '#f97316',
		'--v-act-focus': '#fb923c',
		'--v-act-veil': 'rgba(234, 88, 12, 0.14)',
		'--v-act-text': '#fb923c',
		'--v-band-green': '#2fbf71',
		'--v-band-amber': '#f5a524',
		'--v-band-red': '#e5484d',
		'--v-band-black-fill': 'rgba(11, 15, 23, 0.55)',
		'--v-band-black-hairline': '#e5484d',
		'--v-band-veil': 'rgba(245, 165, 36, 0.14)',
		'--v-data': '#56b4f5',
		'--v-tool': '#a78bfa',
		'--v-glass-bg': 'rgba(10, 15, 26, 0.82)',
		'--v-glass-border': 'rgba(148, 175, 215, 0.24)',
		'--v-glass-blur': '14px',
		'--v-glow-act': 'rgba(249, 115, 22, 0.3)',
		'--v-flow-light': '#84cc16',
		'--v-veil': 'rgba(10, 15, 26, 0.08)',
		'--v-font-ui':
			"Inter, 'Noto Sans', 'Noto Sans Devanagari', 'Noto Sans Gujarati', system-ui, sans-serif",
		'--v-font-display':
			"Outfit, Inter, 'Noto Sans', 'Noto Sans Devanagari', 'Noto Sans Gujarati', system-ui, sans-serif",
		'--v-font-mono': "'JetBrains Mono', 'Noto Sans Mono', ui-monospace, monospace",
		'--v-text-2xs': '12px',
		'--v-text-xs': '12px',
		'--v-text-sm': '13px',
		'--v-text-base': '13px',
		'--v-text-md': '16px',
		'--v-text-lg': '20px',
		'--v-text-xl': '28px',
		'--v-weight-status': '600',
		'--v-tracking-status': '0.08em',
		'--v-space-1': '4px',
		'--v-space-2': '8px',
		'--v-space-3': '12px',
		'--v-space-4': '16px',
		'--v-space-5': '20px',
		'--v-space-6': '24px',
		'--v-space-7': '32px',
		'--v-space-8': '40px',
		'--v-ctl-sm': '24px',
		'--v-ctl-md': '28px',
		'--v-ctl-lg': '32px',
		'--v-ctl-touch': '44px',
		'--v-radius-chip': '4px',
		'--v-radius-btn': '6px',
		'--v-radius-panel': '10px',
		'--v-radius-modal': '14px',
		'--v-radius-pill': '999px',
		'--v-pad-panel': '12px',
		'--v-gap-panel': '8px',
		'--v-ease': 'cubic-bezier(0.2, 0, 0, 1)',
		'--v-dur-out': '150ms',
		'--v-dur-in': '300ms',
		'--v-dur-wipe': '600ms',
		'--v-dur-trace': '1200ms',
		'--v-dur-comet': '300ms',
		'--v-dur-stagger': '120ms',
		'--v-dur-pulse': '200ms',
		'--v-dur-lift': '2s',
		'--v-dur-breathe': '2s',
		'--v-dur-sweep': '3.5s'
	},
	daylight: {
		'--v-canvas': '#edeae3',
		'--v-surface-1': '#ffffff',
		'--v-surface-2': '#f6f3ee',
		'--v-surface-3': '#fcfbf8',
		'--v-surface-sink': '#e5e1d8',
		'--v-paper': '#faf9f7',
		'--v-line-1': 'rgba(26, 31, 46, 0.12)',
		'--v-line-2': 'rgba(26, 31, 46, 0.2)',
		'--v-line-select': 'rgba(194, 65, 12, 0.35)',
		'--v-ink-1': '#1a1f2e',
		'--v-ink-2': '#4a5468',
		'--v-ink-3': '#5b6478',
		'--v-ink-4': '#98a0b3',
		'--v-act': '#ea580c',
		'--v-act-2': '#c2410c',
		'--v-act-focus': '#c2410c',
		'--v-act-veil': 'rgba(234, 88, 12, 0.1)',
		'--v-act-text': '#c2410c',
		'--v-band-green': '#2f9e6f',
		'--v-band-amber': '#b27a1e',
		'--v-band-red': '#c24444',
		'--v-band-black-fill': 'rgba(26, 31, 46, 0.4)',
		'--v-band-black-hairline': '#c24444',
		'--v-band-veil': 'rgba(178, 122, 30, 0.12)',
		'--v-data': '#2e6fae',
		'--v-tool': '#6d5bb8',
		'--v-glass-bg': 'rgba(255, 255, 255, 0.92)',
		'--v-glass-border': 'rgba(26, 31, 46, 0.18)',
		'--v-glass-blur': '10px',
		'--v-glow-act': 'rgba(194, 65, 12, 0.16)',
		'--v-flow-light': '#5f8f2a',
		'--v-veil': 'rgba(26, 31, 46, 0.08)',
		'--v-font-ui':
			"Inter, 'Noto Sans', 'Noto Sans Devanagari', 'Noto Sans Gujarati', system-ui, sans-serif",
		'--v-font-display':
			"Outfit, Inter, 'Noto Sans', 'Noto Sans Devanagari', 'Noto Sans Gujarati', system-ui, sans-serif",
		'--v-font-mono': "'JetBrains Mono', 'Noto Sans Mono', ui-monospace, monospace",
		'--v-text-2xs': '13px',
		'--v-text-xs': '13px',
		'--v-text-sm': '14px',
		'--v-text-base': '16px',
		'--v-text-md': '16px',
		'--v-text-lg': '20px',
		'--v-text-xl': '28px',
		'--v-weight-status': '600',
		'--v-tracking-status': '0.08em',
		'--v-space-1': '4px',
		'--v-space-2': '8px',
		'--v-space-3': '12px',
		'--v-space-4': '16px',
		'--v-space-5': '20px',
		'--v-space-6': '24px',
		'--v-space-7': '32px',
		'--v-space-8': '40px',
		'--v-ctl-sm': '24px',
		'--v-ctl-md': '28px',
		'--v-ctl-lg': '32px',
		'--v-ctl-touch': '44px',
		'--v-radius-chip': '4px',
		'--v-radius-btn': '6px',
		'--v-radius-panel': '10px',
		'--v-radius-modal': '14px',
		'--v-radius-pill': '999px',
		'--v-pad-panel': '16px',
		'--v-gap-panel': '12px',
		'--v-ease': 'cubic-bezier(0.2, 0, 0, 1)',
		'--v-dur-out': '150ms',
		'--v-dur-in': '300ms',
		'--v-dur-wipe': '600ms',
		'--v-dur-trace': '1200ms',
		'--v-dur-comet': '300ms',
		'--v-dur-stagger': '120ms',
		'--v-dur-pulse': '200ms',
		'--v-dur-lift': '2s',
		'--v-dur-breathe': '2s',
		'--v-dur-sweep': '3.5s'
	}
};

/** Token name for a band's value color (BLACK's hairline is band-red). */
export const BAND_TOKEN: Record<Band, { fill: string; edge: string }> = {
	GREEN: { fill: '--v-band-green', edge: '--v-band-green' },
	AMBER: { fill: '--v-band-amber', edge: '--v-band-amber' },
	RED: { fill: '--v-band-red', edge: '--v-band-red' },
	BLACK: { fill: '--v-band-black-fill', edge: '--v-band-black-hairline' }
};

/** Concrete band color for canvas code (CSS cannot cross into WebGL). */
export function bandColor(band: Band, theme: ThemeName = 'command'): string {
	const t = TOKENS[theme];
	if (band === 'BLACK') return t['--v-band-black-fill'] ?? '#0b0f17';
	return t[BAND_TOKEN[band].fill] ?? '#2fbf71';
}

/** Concrete edge/hairline color for a band (BLACK renders a red hairline). */
export function bandEdgeColor(band: Band, theme: ThemeName = 'command'): string {
	const t = TOKENS[theme];
	return t[BAND_TOKEN[band].edge] ?? t['--v-band-red'] ?? '#e5484d';
}

/**
 * Concrete color for an audit level (replaces the per-file maps):
 * decision→data · tool→tool · band→amber · model→ink-4 · info→ink-3 ·
 * error→band-red. Human-actor edges use --v-act by doctrine, not this map.
 */
export function auditLevelColor(level: AuditColorLevel, theme: ThemeName = 'command'): string {
	const t = TOKENS[theme];
	switch (level) {
		case 'decision':
			return t['--v-data'] ?? '#56b4f5';
		case 'tool':
			return t['--v-tool'] ?? '#a78bfa';
		case 'band':
			return t['--v-band-amber'] ?? '#f5a524';
		case 'model':
			return t['--v-ink-4'] ?? '#39455c';
		case 'error':
			return t['--v-band-red'] ?? '#e5484d';
		case 'info':
		default:
			return t['--v-ink-3'] ?? '#7d8aa5';
	}
}

/** The theme currently set on <html> (defaults to command). */
export function currentTheme(): ThemeName {
	if (typeof document !== 'undefined' && document.documentElement.dataset['theme'] === 'daylight') {
		return 'daylight';
	}
	return 'command';
}

/**
 * Threat-ladder color (the restored VVIP template idiom, expressed ONLY in
 * existing token slots — no second palette): nominal→band-green ·
 * guarded→data · moderate→band-amber · elevated→act · critical→band-red.
 */
export type ThreatLevel = 'nominal' | 'guarded' | 'moderate' | 'elevated' | 'critical';

export function threatColor(level: ThreatLevel, theme: ThemeName = 'command'): string {
	const t = TOKENS[theme];
	switch (level) {
		case 'nominal':
			return t['--v-band-green'] ?? '#2fbf71';
		case 'guarded':
			return t['--v-data'] ?? '#56b4f5';
		case 'moderate':
			return t['--v-band-amber'] ?? '#f5a524';
		case 'elevated':
			return t['--v-act'] ?? '#ea580c';
		case 'critical':
		default:
			return t['--v-band-red'] ?? '#e5484d';
	}
}

/**
 * Live-congestion flow ramp (the template's signature segment coloring):
 * free→band-green · light→flow-light · moderate→band-amber ·
 * heavy→act · severe→band-red. Accepts a 0..1 congestion index.
 */
export function flowColor(congestionIdx: number, theme: ThemeName = 'command'): string {
	const t = TOKENS[theme];
	if (congestionIdx <= 0.2) return t['--v-band-green'] ?? '#2fbf71';
	if (congestionIdx <= 0.4) return t['--v-flow-light'] ?? '#84cc16';
	if (congestionIdx <= 0.6) return t['--v-band-amber'] ?? '#f5a524';
	if (congestionIdx <= 0.8) return t['--v-act'] ?? '#ea580c';
	return t['--v-band-red'] ?? '#e5484d';
}

/**
 * Resolve one CSS custom property at runtime — canvas/SVG code path.
 * In the browser this reads the live computed value (so theme flips are
 * honored); outside it falls back to the typed mirror.
 */
export function cssVar(name: string, theme?: ThemeName): string {
	if (typeof document !== 'undefined' && !theme) {
		const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
		if (v) return v;
	}
	return (TOKENS[theme ?? currentTheme()] ?? {})[name] ?? '';
}
