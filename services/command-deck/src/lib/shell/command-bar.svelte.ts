/**
 * CommandBar state — the R3 shell↔store contract.
 *
 * design-shell OWNS this module; every (deck)/(public) page assigns the
 * fields it owns inside an $effect (ops pages: band/lang/onShare from the
 * ops store + share-link; placeholders assign lang only). Import direction
 * is one-way: pages → $lib/shell/**. design-shell never imports
 * $lib/ops/** or $lib/gods-eye/** (eslint-enforced).
 *
 * A page that assigns nothing renders calm chrome — band:null shows an
 * explicit 'band —' mono chip, so a hollow state cannot hide.
 */
import type { Band } from '$lib/design/tokens';

export interface CommandBarState {
	/** City situation band, or null when not yet known ('band —' chip). */
	band: Band | null;
	/** Where the band came from, e.g. 'situation_log' (title/tooltip only). */
	bandSource: string | null;
	/** UI language ('en' | 'hi' | 'gu'); drives the shell's lang switch. */
	lang: string;
	/** Called when the operator flips the shell lang switch (or null). */
	onLangChange: ((lang: string) => void) | null;
	/** Called when the operator presses SHARE (share-link freeze), or null. */
	onShare: (() => void) | null;
}

export const commandBar = $state<CommandBarState>({
	band: null,
	bandSource: null,
	lang: 'en',
	onLangChange: null,
	onShare: null
});
