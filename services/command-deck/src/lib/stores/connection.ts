/**
 * Connection state store — tracks loading/error/freshness for each data feed.
 * Consumed by UI components to render loading skeletons, error banners,
 * and stale-data indicators without breaking visual layout.
 */

import { writable, derived } from 'svelte/store';

// ─── Types ───────────────────────────────────────────────────────────────────

export type FeedState = 'idle' | 'loading' | 'connected' | 'error';

export interface FeedStatus {
	state: FeedState;
	lastUpdated: number; // epoch ms, 0 = never updated
	error: string | null;
	fetchCount: number;
}

const INITIAL_STATUS: FeedStatus = {
	state: 'idle',
	lastUpdated: 0,
	error: null,
	fetchCount: 0,
};

// ─── Per-Feed Stores ─────────────────────────────────────────────────────────

export const trafficFeed = writable<FeedStatus>({ ...INITIAL_STATUS });
export const healthFeed = writable<FeedStatus>({ ...INITIAL_STATUS });
export const anomalyFeed = writable<FeedStatus>({ ...INITIAL_STATUS });
export const predictionFeed = writable<FeedStatus>({ ...INITIAL_STATUS });
export const roadNetworkFeed = writable<FeedStatus>({ ...INITIAL_STATUS });

// ─── Derived: Overall Connection Health ──────────────────────────────────────

const ALL_FEEDS = [trafficFeed, healthFeed, anomalyFeed, predictionFeed, roadNetworkFeed];

/** True once every feed has received at least one successful response */
export const initialLoadComplete = derived(ALL_FEEDS, (feeds) =>
	feeds.every((f) => f.fetchCount > 0),
);

/** True if any feed is currently fetching */
export const anyLoading = derived(ALL_FEEDS, (feeds) =>
	feeds.some((f) => f.state === 'loading'),
);

/** True if any feed is in an error state */
export const anyError = derived(ALL_FEEDS, (feeds) =>
	feeds.some((f) => f.state === 'error'),
);

/** Feeds currently in error state */
export const errorFeeds = derived(ALL_FEEDS, (feeds) =>
	feeds.filter((f) => f.state === 'error'),
);

/** Oldest successful update across all feeds (0 if none have updated) */
export const oldestUpdate = derived(ALL_FEEDS, (feeds) => {
	const updated = feeds.filter((f) => f.lastUpdated > 0).map((f) => f.lastUpdated);
	return updated.length > 0 ? Math.min(...updated) : 0;
});

// ─── Feed State Helpers ──────────────────────────────────────────────────────

type FeedStore = typeof trafficFeed;

export function markLoading(feed: FeedStore): void {
	feed.update((s) => ({ ...s, state: 'loading', error: null }));
}

export function markSuccess(feed: FeedStore): void {
	feed.update((s) => ({
		...s,
		state: 'connected',
		lastUpdated: Date.now(),
		error: null,
		fetchCount: s.fetchCount + 1,
	}));
}

export function markError(feed: FeedStore, error: string): void {
	feed.update((s) => ({ ...s, state: 'error', error }));
}
