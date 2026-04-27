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
    const source = new EventSource(url);
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
      'sql_query.sql_guard_passed',
      'sql_query.sql_guard_blocked',
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

export function taskEventsUrl(taskId: string, baseUrl = import.meta.env.VITE_API_BASE_URL ?? ''): string {
  return `${normalizeBaseUrl(baseUrl)}/api/v1/tasks/${encodeURIComponent(taskId)}/events`;
}
