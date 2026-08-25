import { normalizeBaseUrl } from './client';
import type { TaskEventEnvelope } from './types';
import { isClosedCP7Event } from '../domain/taskEvents';

export interface TaskEventHandlers {
  onMessage(event: TaskEventEnvelope): void;
  onError(error: unknown): void;
}

export interface TaskEventSubscription {
  close(): void;
}

export type EventSourceFactory = (url: string, handlers: TaskEventHandlers) => TaskEventSubscription;

export interface FetchTaskEventSourceOptions {
  fetcher?: typeof fetch;
  accessToken?: string;
  authHeaderProvider?: () => string | null | undefined;
  credentials?: RequestCredentials;
}

const TERMINAL_TASK_EVENT_TYPES = new Set([
  'task.completed',
  'task.failed',
  'task.cancelled',
  'agent.run.completed',
  'agent.run.failed',
  'agent.run.cancelled',
]);

export function parseTaskEventData(data: string): TaskEventEnvelope | null {
  try {
    const parsed = JSON.parse(data) as unknown;
    if (!isTaskEventEnvelope(parsed) || !isClosedCP7Event(parsed)) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function isTaskEventEnvelope(value: unknown): value is TaskEventEnvelope {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const event = value as Record<string, unknown>;
  return typeof event.event_id === 'string'
    && event.event_id.length > 0
    && typeof event.task_id === 'string'
    && event.task_id.length > 0
    && typeof event.event_type === 'string'
    && event.event_type.length > 0
    && typeof event.payload === 'object'
    && event.payload !== null
    && !Array.isArray(event.payload)
    && (event.conversation_id === undefined || typeof event.conversation_id === 'string')
    && (event.node_id === undefined || event.node_id === null || typeof event.node_id === 'string')
    && (event.created_at === undefined || event.created_at === null || typeof event.created_at === 'string');
}

export function createFetchTaskEventSourceFactory(options: FetchTaskEventSourceOptions = {}): EventSourceFactory {
  const fetcher = options.fetcher ?? fetch.bind(globalThis);
  return (url, handlers) => {
    const controller = new AbortController();
    let closed = false;
    const token = options.authHeaderProvider?.() ?? options.accessToken;
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };
    const expectedTaskId = taskIdFromEventsUrl(url);

    void fetcher(url, {
      method: 'GET',
      credentials: options.credentials ?? 'same-origin',
      headers,
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok || !response.body) {
        throw new Error(`Task event stream failed with status ${response.status}`);
      }
      const sawTerminalEvent = await readTaskEventStream(response.body, handlers, () => closed, expectedTaskId);
      if (!closed && !sawTerminalEvent) {
        handlers.onError(new Error('Task event stream closed before a terminal task event.'));
      }
    }).catch((error) => {
      if (!closed) {
        handlers.onError(error);
      }
    });

    return {
      close: () => {
        closed = true;
        controller.abort();
      },
    };
  };
}

async function readTaskEventStream(
  body: ReadableStream<Uint8Array>,
  handlers: TaskEventHandlers,
  isClosed: () => boolean,
  expectedTaskId: string | null = null,
): Promise<boolean> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sawTerminalEvent = false;
  try {
    while (!isClosed()) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      buffer = normalizeSseNewlines(buffer);
      let separatorIndex = buffer.indexOf('\n\n');
      while (separatorIndex >= 0) {
        const block = buffer.slice(0, separatorIndex);
        buffer = buffer.slice(separatorIndex + 2);
        sawTerminalEvent = dispatchTaskEventBlock(block, handlers, expectedTaskId) || sawTerminalEvent;
        separatorIndex = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    buffer = normalizeSseNewlines(buffer);
    if (buffer.trim()) {
      sawTerminalEvent = dispatchTaskEventBlock(buffer, handlers, expectedTaskId) || sawTerminalEvent;
    }
    return sawTerminalEvent;
  } finally {
    reader.releaseLock();
  }
}

function normalizeSseNewlines(value: string): string {
  return value.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function dispatchTaskEventBlock(block: string, handlers: TaskEventHandlers, expectedTaskId: string | null = null): boolean {
  const data = block
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, ''))
    .join('\n');
  if (!data) {
    return false;
  }
  const event = dispatchParsedTaskEvent(data, handlers, expectedTaskId);
  return event ? TERMINAL_TASK_EVENT_TYPES.has(event.event_type) : false;
}

function dispatchParsedTaskEvent(data: string, handlers: TaskEventHandlers, expectedTaskId: string | null): TaskEventEnvelope | null {
  const parsed = parseTaskEventData(data);
  if (parsed && (!expectedTaskId || parsed.task_id === expectedTaskId)) {
    handlers.onMessage(parsed);
    return parsed;
  }
  return null;
}

function taskIdFromEventsUrl(url: string): string | null {
  try {
    const parsedUrl = new URL(url, 'https://local.invalid');
    const match = parsedUrl.pathname.match(/\/api\/v1\/tasks\/([^/]+)\/events$/);
    return match ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

export function taskEventsUrl(taskId: string, baseUrl = import.meta.env.VITE_API_BASE_URL ?? ''): string {
  return `${normalizeBaseUrl(baseUrl)}/api/v1/tasks/${encodeURIComponent(taskId)}/events`;
}
