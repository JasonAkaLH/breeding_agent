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
    const source = new EventSource(url, { withCredentials: true });
    source.onmessage = (event) => {
      const parsed = parseTaskEventData(event.data);
      if (parsed) {
        handlers.onMessage(parsed);
      }
    };
    source.onerror = (event) => {
      handlers.onError(event);
    };

    // sse-starlette sets named events. Handle both named and default delivery.
    const knownEvents = [
      'task.accepted',
      'task.graph_created',
      'node.started',
      'node.completed',
      'node.failed',
      'node.cancelled',
      'node.blocked_by_cancellation',
      'task.completed',
      'task.failed',
      'task.cancelled',
      'task.cancellation_requested',
      'main_agent.output_delta',
      'main_agent.output_final',
      'main_agent.reasoning_delta',
      'skill.progress',
      'task.interrupt_answered',
    ];
    for (const eventName of knownEvents) {
      source.addEventListener(eventName, (event) => {
        const parsed = parseTaskEventData((event as MessageEvent).data);
        if (parsed) {
          handlers.onMessage(parsed);
        }
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

    void fetcher(url, {
      method: 'GET',
      credentials: options.credentials ?? 'same-origin',
      headers,
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok || !response.body) {
        throw new Error(`Task event stream failed with status ${response.status}`);
      }
      await readTaskEventStream(response.body, handlers, () => closed);
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
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
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
        dispatchTaskEventBlock(block, handlers);
        separatorIndex = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    buffer = normalizeSseNewlines(buffer);
    if (buffer.trim()) {
      dispatchTaskEventBlock(buffer, handlers);
    }
  } finally {
    reader.releaseLock();
  }
}

function normalizeSseNewlines(value: string): string {
  return value.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function dispatchTaskEventBlock(block: string, handlers: TaskEventHandlers): void {
  const data = block
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, ''))
    .join('\n');
  if (!data) {
    return;
  }
  const parsed = parseTaskEventData(data);
  if (parsed) {
    handlers.onMessage(parsed);
  }
}

export function taskEventsUrl(taskId: string, baseUrl = import.meta.env.VITE_API_BASE_URL ?? ''): string {
  return `${normalizeBaseUrl(baseUrl)}/api/v1/tasks/${encodeURIComponent(taskId)}/events`;
}
