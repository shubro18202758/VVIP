<script lang="ts">
	/**
	 * AuditRow — FeedItem + a source chip (which rung of the audit ladder
	 * produced the row: ws-event / ws-convoy / ndjson-chat / audit-poll /
	 * operator). Every row is labelled; the ledger never hides its origin.
	 */
	import FeedItem, { type FeedLevel, type FeedSource } from './FeedItem.svelte';

	interface Props {
		ts: number;
		actor: string;
		actorKind: 'human' | 'machine';
		level: FeedLevel;
		text: string;
		detail?: unknown;
		source: FeedSource;
		lang?: string;
	}

	let { ts, actor, actorKind, level, text, detail, source, lang = 'en' }: Props = $props();
</script>

<div data-prim="audit-row" class="audit-row">
	<FeedItem {ts} {actor} {actorKind} {level} {text} {detail} {source} {lang} />
</div>

<style>
	/* display:contents keeps FeedItem as the direct flex child of the feed
	 * column; the wrapper exists only to carry the audit-row identity. */
	.audit-row {
		display: contents;
	}
</style>
