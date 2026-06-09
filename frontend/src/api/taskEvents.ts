import { normalizeBaseUrl } from './client';
import type { TaskEventEnvelope } from './types';

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

const TERMINAL_TASK_EVENT_TYPES = new Set(['task.completed', 'task.failed', 'task.cancelled']);

export function parseTaskEventData(data: string): TaskEventEnvelope | null {
  try {
    const parsed = JSON.parse(data) as TaskEventEnvelope;
    if (!parsed || typeof parsed.event_type !== 'string' || typeof parsed.event_id !== 'string') {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function createBrowserEventSourceFactory(): EventSourceFactory {
  return (url, handlers) => {
    const expectedTaskId = taskIdFromEventsUrl(url);
    const source = new EventSource(url);
    source.onmessage = (event) => {
      dispatchParsedTaskEvent(event.data, handlers, expectedTaskId);
    };
    source.onerror = (event) => {
      handlers.onError(event);
    };

    // sse-starlette sets named events. Handle both named and default delivery.
    const knownEvents = [
      'auth.invalidated',
      'task.accepted',
      'task.graph_created',
      'task.graph_updated',
      'task.replan_started',
      'task.replan_rejected',
      'task.replan_available',
      'node.started',
      'node.completed',
      'node.failed',
      'node.waiting_for_input',
      'node.cancelled',
      'node.blocked_by_cancellation',
      'node.orphaned',
      'node.ready_to_resume',
      'node.resuming',
      'task.completed',
      'task.failed',
      'task.cancelled',
      'task.cancellation_requested',
      'main_agent.output_delta',
      'main_agent.output_final',
      'main_agent.reasoning_delta',
      'skill.progress',
      'task.interrupt_answered',
      'task.interrupt_clarification_answered',
      'task.interrupt_turn_planned',
      'task.interrupt_turn_processed',
      'task.interrupt_question_answered',
      'mcp.long_task_started',
      'mcp.long_task_progress',
      'mcp.long_task_status',
      'mcp.long_task_reconnected',
      'mcp.long_task_completed',
      'mcp.long_task_failed',
      'mcp.long_task_cancel_requested',
      'mcp.long_task_cancelled',
      'artifact.download_denied',
      'artifact.download_gone',
      'artifact.downloaded',
    ];
    for (const eventName of knownEvents) {
      source.addEventListener(eventName, (event) => {
        dispatchParsedTaskEvent((event as MessageEvent).data, handlers, expectedTaskId);
      });
    }
    return { close: () => source.close() };
  };
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
