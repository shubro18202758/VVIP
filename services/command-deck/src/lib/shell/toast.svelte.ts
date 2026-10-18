/**
 * Toast store — user-action feedback ONLY (doctrine: calm is the product).
 * System chatter belongs in the audit stream, never here.
 *
 *   import { toasts } from '$lib/shell/toast.svelte';
 *   toasts.push({ kind: 'act', title: 'Decision approved', body: 'PI-2207 · e-challan' });
 */

export type ToastKind = 'info' | 'act' | 'error';

export interface ToastSpec {
	kind: ToastKind;
	title: string;
	body?: string;
	/** Auto-dismiss after this many ms; 0 = sticky until dismissed. */
	ttl?: number;
}

export interface ToastItem extends ToastSpec {
	id: number;
}

class ToastStore {
	/** $state class field — Svelte does not deep-proxy class instances,
	 * so reactivity must live on the field itself. */
	items: ToastItem[] = $state([]);
	private seq = 0;
	/** Cap from the motion doctrine — at most 3 visible. */
	private static readonly MAX = 3;

	push(spec: ToastSpec): number {
		const id = ++this.seq;
		const ttl = spec.ttl ?? 4000;
		this.items = [{ ...spec, ttl, id }, ...this.items].slice(0, ToastStore.MAX);
		if (ttl > 0 && typeof setTimeout === 'function') {
			setTimeout(() => this.dismiss(id), ttl);
		}
		return id;
	}

	dismiss(id: number): void {
		this.items = this.items.filter((t) => t.id !== id);
	}

	clear(): void {
		this.items = [];
	}
}

export const toasts = new ToastStore();
