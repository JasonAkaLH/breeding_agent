import { describe, expect, it, vi } from 'vitest';
import {
  createFetchTaskEventSourceFactory,
  parseTaskEventData,
  taskEventsUrl,
} from './taskEvents';

async function waitUntil(assertion: () => void): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  }
  throw lastError;
}

describe('taskEvents', () => {
  it('parses valid task event envelopes and ignores malformed data', () => {
    expect(parseTaskEventData('not json')).toBeNull();
    expect(parseTaskEventData(JSON.stringify({ event_type: 'task.completed' }))).toBeNull();
    expect(parseTaskEventData(JSON.stringify({
      event_id: 'evt-1',
      event_type: 'task.completed',
      task_id: 'task-1',
      payload: {},
    }))).toMatchObject({ event_id: 'evt-1', event_type: 'task.completed' });
  });

  it('builds task event URLs without query tokens', () => {
    expect(taskEventsUrl('task/id', 'https://api.example')).toBe('https://api.example/api/v1/tasks/task%2Fid/events');
  });

  it('supports Bearer authenticated fetch streams for cross-site clients', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: task.accepted\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-1","event_type":"task.accepted","task_id":"task-1","payload":{}}\n\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-2","event_type":"task.completed","task_id":"task-1","payload":{}}\n\n'));
        controller.close();
      },
    });
    const fetcher = vi.fn(async () => new Response(stream, { status: 200 }));
    const onMessage = vi.fn();
    const onError = vi.fn();
    const factory = createFetchTaskEventSourceFactory({
      fetcher,
      accessToken: 'maf_tok_client',
      credentials: 'omit',
    });

    factory('https://api.example/api/v1/tasks/task-1/events', { onMessage, onError });

    await waitUntil(() => expect(onMessage).toHaveBeenCalledTimes(2));
    expect(fetcher).toHaveBeenCalledWith('https://api.example/api/v1/tasks/task-1/events', expect.objectContaining({
      method: 'GET',
      credentials: 'omit',
      headers: expect.objectContaining({
        Accept: 'text/event-stream',
        Authorization: 'Bearer maf_tok_client',
      }),
    }));
    expect(onError).not.toHaveBeenCalled();
  });

  it('drops stream events whose task_id does not match the subscribed task', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"event_id":"evt-wrong","event_type":"main_agent.output_delta","task_id":"task-other","payload":{"delta":"leak"}}\n\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-right","event_type":"main_agent.output_delta","task_id":"task-1","payload":{"delta":"ok"}}\n\n'));
        controller.close();
      },
    });
    const fetcher = vi.fn(async () => new Response(stream, { status: 200 }));
    const onMessage = vi.fn();
    const onError = vi.fn();
    const factory = createFetchTaskEventSourceFactory({ fetcher, accessToken: 'maf_tok_client' });

    factory('https://api.example/api/v1/tasks/task-1/events', { onMessage, onError });

    await waitUntil(() => expect(onMessage).toHaveBeenCalledTimes(1));
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ event_id: 'evt-right', task_id: 'task-1' }));
    expect(onError).not.toHaveBeenCalled();
  });
});
