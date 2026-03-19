/**
 * LLM Chat state store — manages conversation with the convoy-brain agent.
 * Supports streaming responses, thought chain visualization, and MCP tool status.
 */

import { writable, derived } from 'svelte/store';
import type {
	ChatMessage,
	ThoughtStep,
	ToolCallStatus,
	ToolCallState,
} from '$lib/types';

// ─── Core Stores ────────────────────────────────────────────────────────────

export const messages = writable<ChatMessage[]>([]);
export const isStreaming = writable(false);
export const sessionId = writable<string | null>(null);
export const connectionStatus = writable<'connected' | 'connecting' | 'disconnected'>('disconnected');

// ─── Display Controls ───────────────────────────────────────────────────────

export const showThoughts = writable(true);
export const showToolCalls = writable(true);
export const autoScroll = writable(true);

// ─── Derived Stores ─────────────────────────────────────────────────────────

export const messageCount = derived(messages, ($m) => $m.length);

export const activeToolCalls = derived(messages, ($msgs) => {
	const last = $msgs[$msgs.length - 1];
	if (!last || last.role !== 'assistant') return [];
	return last.toolCalls.filter((tc) => tc.state === 'running' || tc.state === 'pending');
});

export const lastAssistantMessage = derived(messages, ($msgs) => {
	for (let i = $msgs.length - 1; i >= 0; i--) {
		if ($msgs[i].role === 'assistant') return $msgs[i];
	}
	return null;
});

// ─── Actions ────────────────────────────────────────────────────────────────

let messageIdCounter = 0;

function nextId(): string {
	return `msg-${++messageIdCounter}-${Date.now()}`;
}

export function addUserMessage(content: string): string {
	const id = nextId();
	messages.update((m) => [
		...m,
		{
			id,
			role: 'user',
			content,
			timestamp: Date.now(),
			thoughts: [],
			toolCalls: [],
			isStreaming: false,
		},
	]);
	return id;
}

export function startAssistantMessage(): string {
	const id = nextId();
	isStreaming.set(true);
	messages.update((m) => [
		...m,
		{
			id,
			role: 'assistant',
			content: '',
			timestamp: Date.now(),
			thoughts: [],
			toolCalls: [],
			isStreaming: true,
		},
	]);
	return id;
}

export function appendToMessage(msgId: string, chunk: string): void {
	messages.update((m) =>
		m.map((msg) =>
			msg.id === msgId ? { ...msg, content: msg.content + chunk } : msg,
		),
	);
}

export function addThought(msgId: string, thought: ThoughtStep): void {
	messages.update((m) =>
		m.map((msg) =>
			msg.id === msgId
				? { ...msg, thoughts: [...msg.thoughts, thought] }
				: msg,
		),
	);
}

export function addToolCall(msgId: string, toolCall: ToolCallStatus): void {
	messages.update((m) =>
		m.map((msg) =>
			msg.id === msgId
				? { ...msg, toolCalls: [...msg.toolCalls, toolCall] }
				: msg,
		),
	);
}

export function updateToolCallState(
	msgId: string,
	callId: string,
	state: ToolCallState,
	result?: unknown,
	durationMs?: number,
): void {
	messages.update((m) =>
		m.map((msg) =>
			msg.id === msgId
				? {
						...msg,
						toolCalls: msg.toolCalls.map((tc) =>
							tc.callId === callId
								? { ...tc, state, result: result ?? tc.result, durationMs: durationMs ?? tc.durationMs }
								: tc,
						),
					}
				: msg,
		),
	);
}

export function finishStreaming(msgId: string): void {
	isStreaming.set(false);
	messages.update((m) =>
		m.map((msg) =>
			msg.id === msgId ? { ...msg, isStreaming: false } : msg,
		),
	);
}

export function clearChat(): void {
	messages.set([]);
	isStreaming.set(false);
	sessionId.set(null);
}
