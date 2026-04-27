import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import type { ApiClient } from './api/client';
import type { TaskEventEnvelope } from './api/types';
import type { EventSourceFactory } from './api/taskEvents';

function makeApi(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    uiModes: [
      { key: 'chat', label: '普通对话', capabilityId: null },
      { key: 'sql_query', label: '数据库查询（SQLQuery）', capabilityId: 'sql_query.query' },
    ],
    listCapabilities: vi.fn(async () => ({ capabilities: [{ capability_id: 'sql_query.query', name: 'SQLQuery', description: 'SQL', version: '1', status: 'active' }] })),
    submitMessage: vi.fn(async () => ({ conversation_id: 'conv-test', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' })),
    cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
    getTask: vi.fn(),
    getTaskArtifacts: vi.fn(async () => ({ task_id: 'task-1', artifacts: [] })),
    getTaskGraph: vi.fn(),
    ...overrides,
  };
}

function event(event_type: string, payload: Record<string, unknown> = {}, event_id = event_type): TaskEventEnvelope {
  return {
    event_id,
    conversation_id: 'conv-test',
    task_id: 'task-1',
    node_id: null,
    event_type,
    payload,
    created_at: '2026-04-27T00:00:00',
  };
}

function makeEventSourceFactory(events: TaskEventEnvelope[]): EventSourceFactory {
  return (_url, handlers) => {
    queueMicrotask(() => {
      for (const item of events) {
        handlers.onMessage(item);
      }
    });
    return { close: vi.fn() };
  };
}

describe('App', () => {
  it('submits normal chat and renders streaming answer', async () => {
    const api = makeApi();
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('main_agent.output_delta', { delta: '你好，', ordinal: 1 }, 'delta-1'),
      event('main_agent.output_delta', { delta: '已接通。', ordinal: 2 }, 'delta-2'),
      event('task.completed'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'chat' })));
    await screen.findByText('你好，已接通。');
  });

  it('submits SQLQuery mode and renders summary table without SQL', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'result_summary:1', producer_node_id: 'summary', artifact_type: 'summary', storage_ref: JSON.stringify({ summary: '共 1 行。', row_count: 1 }), summary: '共 1 行。', is_complete: true, created_at: null },
          { artifact_id: 'query_result_preview:1', producer_node_id: 'execute', artifact_type: 'json', storage_ref: JSON.stringify({ sql: 'select secret', columns: ['variety_name'], rows: [{ variety_name: '龙粳33' }], row_count: 1 }), summary: 'preview', is_complete: true, created_at: null },
        ],
      })),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByLabelText('数据库查询（SQLQuery）'));
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'sql_query' })));
    expect((await screen.findAllByText('共 1 行。')).length).toBeGreaterThan(0);
    expect(await screen.findByText('龙粳33')).toBeInTheDocument();
    expect(screen.queryByText(/select secret/i)).not.toBeInTheDocument();
  });

  it('shows a friendly busy-conversation error', async () => {
    const api = makeApi({
      submitMessage: vi.fn(async () => {
        const error = new Error('busy') as Error & { userMessage: string };
        error.userMessage = '当前会话已有任务运行中，请等待完成或取消后再继续。';
        throw error;
      }),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect((await screen.findAllByText(/当前会话已有任务运行中/)).length).toBeGreaterThan(0);
  });

  it('keeps polling until graph has waiting_for_input and then shows unsupported interrupt guidance', async () => {
    const runningGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:sql_generate', capability_id: 'sql_query.sql_generate', status: 'running', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const waitingGraph = {
      ...runningGraph,
      nodes: [{ ...runningGraph.nodes[0], status: 'waiting_for_input' }],
    };
    const api = makeApi({
      getTaskGraph: vi.fn()
        .mockResolvedValueOnce(runningGraph)
        .mockResolvedValueOnce(waitingGraph),
      getTask: vi.fn(async () => ({
        task_id: 'task-1',
        conversation_id: 'conv-test',
        status: 'running',
        root_node_id: null,
        active_node_count: 1,
        completed_node_count: 0,
        failed_node_count: 0,
        cancel_requested: false,
        created_at: null,
        updated_at: null,
      })),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} waitingInputCheckDelayMs={1} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询基因型' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.getTaskGraph).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/当前前端版本暂不支持继续该任务/)).toBeInTheDocument();
  });

});
