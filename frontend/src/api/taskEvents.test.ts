import { describe, expect, it, vi } from 'vitest';
import {
  createBrowserEventSourceFactory,
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

  it('parses CP7 events only when their payload matches the closed schema', () => {
    const unavailable = {
      event_id: 'mcp-no-server:v1:task-1:01-runtime-unavailable',
      event_type: 'mcp.runtime_unavailable',
      task_id: 'task-1',
      payload: { status: 'unavailable', reason_code: 'no_user_scoped_server' },
    };
    expect(parseTaskEventData(JSON.stringify(unavailable))).toMatchObject(unavailable);
    expect(parseTaskEventData(JSON.stringify({
      ...unavailable,
      payload: { ...unavailable.payload, detail: 'must-not-pass' },
    }))).toBeNull();

    const unknown = {
      event_id: 'mcp-execution-status-unknown:v1:call-1:4:01-unknown',
      conversation_id: 'conv-1',
      event_type: 'mcp.execution_status_unknown',
      task_id: 'task-1',
      node_id: 'node-1',
      created_at: '2026-04-27T00:00:00Z',
      payload: {
        schema: 'maf.user_mcp.execution_status_unknown.v1',
        projection_id: 'mcp-terminal-projection:v1:call-1',
        intent_id: 'intent-1',
        call_id: 'call-1',
        task_id: 'task-1',
        node_id: 'node-1',
        projection_revision: 0,
        intent_revision: 4,
        unknown_terminal_at: '2026-04-27T00:00:00Z',
        reason_code: 'trusted_terminal_result_absent',
        no_replay: true,
        result_receipt_id: null,
        predecessor_event_id: null,
      },
    };
    expect(parseTaskEventData(JSON.stringify(unknown))).toMatchObject({ event_id: unknown.event_id });
    expect(parseTaskEventData(JSON.stringify({
      ...unknown,
      payload: { ...unknown.payload, no_replay: false },
    }))).toBeNull();

    const projection = {
      event_id: 'mcp-result-artifact-projection:v1:artifact-1:deferred:capacity_unavailable',
      conversation_id: 'conv-1',
      event_type: 'mcp.result_artifact_projection',
      task_id: 'task-1',
      node_id: 'node-1',
      created_at: '2026-04-27T00:00:00Z',
      payload: {
        schema: 'maf.user_mcp.result_artifact_projection.v1',
        safe_call_ref: 'a'.repeat(64),
        status: 'deferred',
        reason_code: 'capacity_unavailable',
        artifact_count: 0,
      },
    };
    expect(parseTaskEventData(JSON.stringify(projection))).toMatchObject({ event_id: projection.event_id });
    expect(parseTaskEventData(JSON.stringify({
      ...projection,
      payload: { ...projection.payload, result_ref: 'must-not-pass' },
    }))).toBeNull();
  });

  it('parses only closed Agent frontend event payloads', () => {
    const waiting = {
      event_id: 'agent-waiting-1',
      conversation_id: 'conv-1',
      event_type: 'agent.run.waiting',
      task_id: 'task-1',
      node_id: 'node-1',
      created_at: '2026-04-27T00:00:00Z',
      payload: {
        interrupt_id: 'interrupt-1',
        reason_kind: 'skill_input',
        remaining_count: 2,
      },
    };
    const reasoning = {
      ...waiting,
      event_id: 'agent-reasoning-1',
      event_type: 'agent.reasoning_delta',
      payload: { delta: '瞬时思考', ordinal: 0, sample_id: 'sample-1' },
    };

    expect(parseTaskEventData(JSON.stringify(waiting))).toMatchObject(waiting);
    expect(parseTaskEventData(JSON.stringify(reasoning))).toMatchObject(reasoning);
    expect(parseTaskEventData(JSON.stringify({
      ...waiting,
      payload: { ...waiting.payload, prompt: 'must-not-pass' },
    }))).toBeNull();
    expect(parseTaskEventData(JSON.stringify({
      ...reasoning,
      payload: { ...reasoning.payload, ordinal: -1 },
    }))).toBeNull();
  });

  it('builds task event URLs without query tokens', () => {
    expect(taskEventsUrl('task/id', 'https://api.example')).toBe('https://api.example/api/v1/tasks/task%2Fid/events');
  });

  it('builds task event URLs below a subpath base URL', () => {
    expect(taskEventsUrl('task-1', '/seedpilot')).toBe('/seedpilot/api/v1/tasks/task-1/events');
  });

  it('supports Bearer authenticated fetch streams for cross-site clients', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: task.accepted\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-1","event_type":"task.accepted","task_id":"task-1","payload":{}}\n\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-2","event_type":"agent.run.completed","task_id":"task-1","payload":{"compaction_count":0,"duration_seconds":0,"outcome":"completed","sample_count":1,"tool_call_count":0}}\n\n'));
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
        controller.enqueue(encoder.encode('data: {"event_id":"evt-wrong","event_type":"agent.reasoning_delta","task_id":"task-other","payload":{"delta":"leak","ordinal":1,"sample_id":"sample-wrong"}}\n\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-right","event_type":"agent.reasoning_delta","task_id":"task-1","payload":{"delta":"ok","ordinal":1,"sample_id":"sample-right"}}\n\n'));
        controller.enqueue(encoder.encode('data: {"event_id":"evt-terminal","event_type":"agent.run.completed","task_id":"task-1","payload":{"compaction_count":0,"duration_seconds":0,"outcome":"completed","sample_count":1,"tool_call_count":0}}\n\n'));
        controller.close();
      },
    });
    const fetcher = vi.fn(async () => new Response(stream, { status: 200 }));
    const onMessage = vi.fn();
    const onError = vi.fn();
    const factory = createFetchTaskEventSourceFactory({ fetcher, accessToken: 'maf_tok_client' });

    factory('https://api.example/api/v1/tasks/task-1/events', { onMessage, onError });

    await waitUntil(() => expect(onMessage).toHaveBeenCalledTimes(2));
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ event_id: 'evt-right', task_id: 'task-1' }));
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ event_id: 'evt-terminal', task_id: 'task-1' }));
    expect(onMessage).not.toHaveBeenCalledWith(expect.objectContaining({ event_id: 'evt-wrong' }));
    expect(onError).not.toHaveBeenCalled();
  });

  it('reports an unterminated fetch stream EOF so callers can reconnect', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"event_id":"evt-active","event_type":"task.accepted","task_id":"task-1","payload":{}}\n\n'));
        controller.close();
      },
    });
    const fetcher = vi.fn(async () => new Response(stream, { status: 200 }));
    const onMessage = vi.fn();
    const onError = vi.fn();
    const factory = createFetchTaskEventSourceFactory({ fetcher, accessToken: 'maf_tok_client' });

    factory('https://api.example/api/v1/tasks/task-1/events', { onMessage, onError });

    await waitUntil(() => expect(onMessage).toHaveBeenCalledTimes(1));
    await waitUntil(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
    expect(String((onError.mock.calls[0][0] as Error).message)).toContain('closed before a terminal task event');
  });

  it('listens for documented named frontend events from browser EventSource', () => {
    const listeners = new Map<string, (event: MessageEvent) => void>();
    class FakeEventSource {
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      constructor(public readonly url: string) {}
      addEventListener(eventName: string, listener: EventListenerOrEventListenerObject) {
        listeners.set(eventName, listener as (event: MessageEvent) => void);
      }
      close() {}
    }
    vi.stubGlobal('EventSource', FakeEventSource);
    const onMessage = vi.fn();
    const onError = vi.fn();

    try {
      createBrowserEventSourceFactory()('https://api.example/api/v1/tasks/task-1/events', { onMessage, onError });
      for (const eventName of [
        'auth.invalidated',
        'mcp.long_task_progress',
        'mcp.long_task_cancelled',
        'mcp.server_routed',
        'mcp.discovery_started',
        'mcp.discovery_completed',
        'mcp.discovery_failed',
        'mcp.tool_approval_required',
        'mcp.tool_approval_decided',
        'mcp.tool_call_started',
        'mcp.tool_call_still_running',
        'mcp.tool_call_completed',
        'mcp.tool_call_failed',
        'mcp.tool_call_cancelled',
        'mcp.runtime_unavailable',
        'mcp.execution_status_unknown',
        'mcp.execution_status_resolution',
        'mcp.late_terminal_result_recovered',
        'mcp.queue_entered',
        'mcp.queue_left',
        'mcp.input_required',
        'mcp.input_submitted',
        'mcp.remote_task_status_changed',
        'task.interrupt_clarification_answered',
        'node.waiting_for_input',
        'node.cancelled',
        'node.blocked_by_cancellation',
        'node.ready_to_resume',
        'node.resuming',
        'agent.reasoning_delta',
        'agent.run.waiting',
        'agent.run.resumed',
        'agent.run.completed',
        'agent.run.failed',
        'agent.run.cancelled',
      ]) {
        expect(listeners.get(eventName), eventName).toBeDefined();
      }
      const waitingListener = listeners.get('node.waiting_for_input');
      expect(waitingListener).toBeDefined();
      waitingListener?.(new MessageEvent('node.waiting_for_input', {
        data: JSON.stringify({
          event_id: 'evt-waiting',
          event_type: 'node.waiting_for_input',
          task_id: 'task-1',
          payload: { interrupt_id: 'interrupt-1' },
        }),
      }));
      listeners.get('node.ready_to_resume')?.(new MessageEvent('node.ready_to_resume', {
        data: JSON.stringify({
          event_id: 'evt-ready',
          event_type: 'node.ready_to_resume',
          task_id: 'task-1',
          payload: { interrupt_id: 'interrupt-1' },
        }),
      }));
    } finally {
      vi.unstubAllGlobals();
    }

    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ event_type: 'node.waiting_for_input' }));
    listeners.get('task.interrupt_clarification_answered')?.(new MessageEvent('task.interrupt_clarification_answered', {
      data: JSON.stringify({
        event_id: 'evt-clarification',
        event_type: 'task.interrupt_clarification_answered',
        task_id: 'task-1',
        payload: { interrupt_id: 'interrupt-1' },
      }),
    }));

    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ event_type: 'node.ready_to_resume' }));
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ event_type: 'task.interrupt_clarification_answered' }));
    expect(onError).not.toHaveBeenCalled();
  });
});
