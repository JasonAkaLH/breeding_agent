import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    listConversationTasks: vi.fn(async () => ({ conversation_id: 'conv-test', tasks: [] })),
    listInterrupts: vi.fn(async () => ({ task_id: 'task-1', interrupts: [] })),
    answerInterrupt: vi.fn(async () => ({ interrupt_id: 'interrupt-1', status: 'answered', node_id: 'node-1', answer_payload: {} })),
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
      event('main_agent.reasoning_delta', { delta: '先分析。', ordinal: 1 }, 'reasoning-1'),
      event('main_agent.output_delta', { delta: '**你好**，', ordinal: 1 }, 'delta-1'),
      event('main_agent.output_delta', { delta: '已接通。', ordinal: 2 }, 'delta-2'),
      event('task.completed'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'chat' })));
    await screen.findByText('思考内容');
    await screen.findByText('先分析。');
    const reasoningBox = screen.getByText('思考内容').closest('.reasoning-box');
    expect(reasoningBox).toHaveClass('reasoning-box-collapsed');
    fireEvent.click(within(reasoningBox as HTMLElement).getByRole('button', { name: '展开思考内容' }));
    expect(reasoningBox).toHaveClass('reasoning-box-expanded');
    fireEvent.click(within(reasoningBox as HTMLElement).getByRole('button', { name: '收起思考内容' }));
    expect(reasoningBox).toHaveClass('reasoning-box-collapsed');
    await waitFor(() => expect(screen.getAllByText('你好').length).toBeGreaterThan(0));
    await screen.findByText(/已接通。/);
  });

  it('submits deep thinking flag from the switch next to current mode', async () => {
    const api = makeApi();
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    expect(screen.getAllByLabelText('思考强度').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByLabelText('深度思考'));
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '请深入分析' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'chat',
      deepThinking: true,
      reasoningEffort: 'medium',
    })));
  });

  it('shows a reasoning box placeholder when deep thinking is enabled but no reasoning content arrives', async () => {
    const api = makeApi();
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('main_agent.output_delta', { delta: '最终回答', ordinal: 1 }, 'delta-1'),
      event('task.completed'),
    ])} />);

    fireEvent.click(screen.getByLabelText('深度思考'));
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '请深度思考' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByText('思考内容');
    expect(await screen.findByText(/等待模型返回 reasoning_content|本次模型未返回 reasoning_content/)).toBeInTheDocument();
    await screen.findByText('最终回答');
  });

  it('submits database questions through automatic planning without manual mode selection', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'main_agent_text:1', producer_node_id: 'main_agent.respond', artifact_type: 'text', storage_ref: '主代理已自动调用 SQLQuery，龙粳33共 1 行。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'query_result_preview:1', producer_node_id: 'task-1:query_data:sql_execute_readonly', artifact_type: 'json', storage_ref: JSON.stringify({ columns: ['variety_name'], rows: [{ variety_name: '龙粳33' }], row_count: 1, truncated: false }), summary: 'preview', is_complete: true, created_at: null },
        ],
      })),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    expect(screen.queryByLabelText('对话模式')).not.toBeInTheDocument();
    expect(screen.getByText('当前模式：自动规划')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'chat' })));
    expect(await screen.findByText(/主代理已自动调用 SQLQuery/)).toBeInTheDocument();
    expect(await screen.findByText('SQLQuery 查询结果')).toBeInTheDocument();
    expect(screen.getByText('查询已完成，共返回 1 行结果。')).toBeInTheDocument();
    expect(screen.getByText('原始表格预览默认隐藏')).toBeInTheDocument();
    expect(screen.queryByText(/select secret/i)).not.toBeInTheDocument();
  });

  it('shows the upstream capability currently being executed inside the assistant bubble', async () => {
    const api = makeApi();
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('node.started', { capability_id: 'sql_query.intent_route' }, 'sql-intent-started'),
      event('node.started', { capability_id: 'sql_query.sql_execute_readonly' }, 'sql-execute-started'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('正在执行 SQLQuery：正在检索数据库')).toBeInTheDocument();
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

  it('keeps polling until graph has waiting_for_input and submits the next input as interrupt answer', async () => {
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
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'task-1:intent_route',
          question: '请补充要查询的作物类型。',
          reason_code: 'crop_not_resolved',
          required_fields: { crop: { options: ['corn', 'rice', 'cotton', 'wheat', 'soybean'] } },
          status: 'open',
        }],
      })),
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
    expect(await screen.findByRole('region', { name: '需要补充信息' })).toBeInTheDocument();
    expect(screen.getByText('回复后将继续当前任务。')).toBeInTheDocument();
    expect(screen.getByText('作物类型')).toBeInTheDocument();
    expect(screen.getByText('玉米')).toBeInTheDocument();
    expect(screen.getByText('水稻')).toBeInTheDocument();
    expect(screen.getByText(/你的下一条消息会继续这个任务/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText('请输入作物类型，例如“水稻”')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消当前任务' })).toBeInTheDocument();
    expect(await screen.findByText(/请补充要查询的作物类型/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(api.answerInterrupt).toHaveBeenCalledWith('task-1', 'interrupt-1', { crop: '水稻' }));
    expect(screen.getByText('已收到补充信息，继续当前任务...')).toBeInTheDocument();
    expect(api.cancelTask).not.toHaveBeenCalled();
  });

  it('keeps the task locked while waiting_for_input has no open interrupt yet', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:intent_route', capability_id: 'sql_query.intent_route', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const api = makeApi({
      getTaskGraph: vi.fn(async () => waitingGraph),
      listInterrupts: vi.fn(async () => ({ task_id: 'task-1', interrupts: [] })),
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

    await waitFor(() => expect(api.listInterrupts).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(api.answerInterrupt).not.toHaveBeenCalled();
    expect(api.submitMessage).toHaveBeenCalledTimes(1);
  });

  it('shows unfinished task list and lets the user stop a listed task', async () => {
    const api = makeApi({
      listConversationTasks: vi.fn()
        .mockResolvedValueOnce({
          conversation_id: 'conv-test',
          tasks: [{
            task_id: 'task-running',
            conversation_id: 'conv-test',
            status: 'running',
            root_node_id: 'task-running:main',
            active_node_count: 1,
            completed_node_count: 0,
            failed_node_count: 0,
            cancel_requested: false,
            summary: '查询龙粳33',
            requested_capability_id: 'sql_query.query',
            created_at: '2026-04-27T00:00:00',
            updated_at: '2026-04-27T00:00:01',
          }],
        })
        .mockResolvedValue({
          conversation_id: 'conv-test',
          tasks: [],
        }),
      cancelTask: vi.fn(async () => ({ task_id: 'task-running', status: 'cancelling', accepted: true })),
    });

    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('未完成任务')).toBeInTheDocument();
    expect(screen.queryByText('查询龙粳33')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '展开未完成任务' }));
    expect(await screen.findByText('查询龙粳33')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '停止任务 task-running' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-running'));
    await waitFor(() => expect(screen.getByText('暂无未完成任务')).toBeInTheDocument());
  });

});
