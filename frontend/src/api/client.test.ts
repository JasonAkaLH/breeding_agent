import { describe, expect, it, vi } from 'vitest';
import { ApiError, createApiClient } from './client';

describe('createApiClient', () => {
  it('submits normal chat with capability_id null', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', accountId: 'acc-1', content: '你好', mode: 'chat' });

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/conv-1/messages', expect.objectContaining({ method: 'POST' }));
    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.capability_id).toBeNull();
  });

  it('submits deep thinking metadata without changing reasoning effort', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', accountId: 'acc-1', content: '深入分析', mode: 'chat', deepThinking: true });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.metadata).toMatchObject({
      deep_thinking: true,
      main_agent_reasoning_effort: 'medium',
    });
  });

  it('submits selected reasoning effort metadata independently', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', accountId: 'acc-1', content: '分析', mode: 'chat', reasoningEffort: 'high' });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.metadata).toMatchObject({
      deep_thinking: false,
      main_agent_reasoning_effort: 'high',
    });
  });

  it('submits SQLQuery mode with sql_query.query only', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', accountId: 'acc-1', content: '查询龙粳33', mode: 'sql_query' });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.capability_id).toBe('sql_query.query');
    expect(JSON.stringify(body)).not.toContain('sql_query.sql_generate');
  });

  it('maps 409 busy conversation to business friendly error', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ detail: 'Conversation is busy' }), { status: 409 }));
    const api = createApiClient({ fetcher });

    await expect(api.submitMessage({ conversationId: 'conv-1', accountId: 'acc-1', content: '你好', mode: 'chat' })).rejects.toMatchObject({
      userMessage: expect.stringContaining('当前会话已有任务'),
    });
  });

  it('lists and answers task interrupts', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ task_id: 'task-1', interrupts: [] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.listInterrupts('task-1');
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/tasks/task-1/interrupts');

    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ interrupt_id: 'interrupt-1', status: 'answered', node_id: 'node-1', answer_payload: { crop: '水稻' } }), { status: 202 }));
    await api.answerInterrupt('task-1', 'interrupt-1', { crop: '水稻' });
    expect(fetcher).toHaveBeenLastCalledWith('/api/v1/tasks/task-1/interrupts/interrupt-1/answer', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ answer_payload: { crop: '水稻' } }),
    }));
  });

  it('lists unfinished conversation tasks for the task panel', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', tasks: [] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.listConversationTasks('conv-1');

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/conv-1/tasks?scope=unfinished', expect.any(Object));
  });

  it('exposes only public UI modes', () => {
    const api = createApiClient();
    expect(api.uiModes.map((mode) => mode.capabilityId)).toEqual([null, 'sql_query.query']);
  });
});
