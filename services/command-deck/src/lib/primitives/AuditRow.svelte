<script lang="ts">
	/**
	 * AuditRow — FeedItem + a source chip: the feed kind of a server row
	 * (event / enforcement / anpr / dispatch / signals / ama / brain) or the
	 * rung of a local one (ndjson-chat / audit-poll / operator). Every row is
	 * labelled; the ledger never hides its origin, and an emulated row
	 * carries the EMULATED pill.
	 */
	import FeedItem, { type FeedLevel, type FeedSource } from './FeedItem.svelte';

	interface Props {
		ts: number;
		actor: string;
		actorKind: 'human' | 'machine' | 'system';
		level: FeedLevel;
		text: string;
		detail?: unknown;
		source: FeedSource;
		emulated?: boolean;
		/** Its numbers came from a SIMULATED feed (C-Q). */
		simulated?: boolean;
		rowId?: string | null;
		lang?: string;
	}

	let { ts, actor, actorKind, level, text, detail, source, emulated = false, simulated = false, rowId = null, lang = 'en' }: Props = $props();
</script>

<div data-prim="audit-row" class="audit-row">
	<FeedItem {ts} {actor} {actorKind} {level} {text} {detail} {source} {emulated} {simulated} {rowId} {lang} />
</div>

<style>
	/* display:contents keeps FeedItem as the direct flex child of the feed
	 * column; the wrapper exists only to carry the audit-row identity. */
	.audit-row {
		display: contents;
	}
</style>
