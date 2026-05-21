import { describe, expect, it, vi } from 'vitest';
import { ApiError, createApiClient } from './client';

describe('createApiClient', () => {
  it('uses cookie credentials for auth and business requests', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' } }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.me();

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/me', expect.objectContaining({ credentials: 'same-origin' }));
  });


  it('adds bearer authorization headers when configured', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' } }), { status: 200 }));
    const api = createApiClient({ fetcher, accessToken: 'maf_tok_client' });

    await api.me();

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/me', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer maf_tok_client' }),
    }));
  });

  it('manages api tokens without putting ids in the URL', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      token_id: 'tok-1',
      client_name: 'client',
      scopes: ['conversation:read'],
      expires_at: '2026-05-21T12:00:00',
      access_token: 'maf_tok_secret',
    }), { status: 201 }));
    const api = createApiClient({ fetcher });

    await api.createApiToken({ clientName: 'client', scopes: ['conversation:read'], ttlSeconds: 3600 });
    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/api-tokens', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ client_name: 'client', scopes: ['conversation:read'], ttl_seconds: 3600 }),
    }));

    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ token_id: 'tok-1', revoked: true }), { status: 200 }));
    await api.revokeApiToken('tok-1');
    expect(fetcher).toHaveBeenLastCalledWith('/api/v1/auth/api-tokens', expect.objectContaining({
      method: 'DELETE',
      body: JSON.stringify({ token_id: 'tok-1' }),
    }));
  });

  it('logs in with username password and captcha fields', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' } }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.login({ username: 'alice', password: 'secret', captchaId: 'cap-1', captchaCode: '1234' });

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/login', expect.objectContaining({ method: 'POST' }));
    expect(JSON.parse(fetcher.mock.calls[0][1].body as string)).toEqual({
      username: 'alice',
      password: 'secret',
      captcha_id: 'cap-1',
      captcha_code: '1234',
    });
  });

  it('registers with username password and captcha fields', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'charlie' } }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.register({ username: 'charlie', password: 'charlie1', captchaId: 'cap-1', captchaCode: '1234' });

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/register', expect.objectContaining({ method: 'POST' }));
    expect(JSON.parse(fetcher.mock.calls[0][1].body as string)).toEqual({
      username: 'charlie',
      password: 'charlie1',
      captcha_id: 'cap-1',
      captcha_code: '1234',
    });
  });

  it('lists public capabilities', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ capabilities: [{ capability_id: 'skill.data_lookup', name: 'data-lookup', description: '查询', version: '1', status: 'active', kind: 'skill', source: 'skill', source_path: 'data-lookup/SKILL.md' }] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    const result = await api.listCapabilities();

    expect(fetcher).toHaveBeenCalledWith('/api/v1/capabilities', expect.any(Object));
    expect(result.capabilities[0]).toMatchObject({ capability_id: 'skill.data_lookup', kind: 'skill' });
  });

  it('submits normal chat with capability_id null', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', accountId: 'acc-1', content: '你好', mode: 'chat' });

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/chat-messages', expect.objectContaining({ method: 'POST' }));
    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.conversation_id).toBe('conv-1');
    expect(body.capability_id).toBeNull();
  });


  it('submits an explicit capability as a forced route while preserving metadata', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({
      conversationId: 'conv-1',
      accountId: 'acc-1',
      content: '查询龙粳33',
      mode: 'chat',
      capabilityId: 'skill.data_lookup',
      metadata: { upload_ids: ['upl-1'], forced_by_slash_command: true, slash_command: '/data-lookup' },
    });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body).toMatchObject({
      routing_mode: 'force_capability',
      capability_id: 'skill.data_lookup',
      metadata: {
        upload_ids: ['upl-1'],
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
        deep_thinking: false,
        main_agent_reasoning_effort: 'medium',
      },
    });
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
    expect(fetcher).toHaveBeenLastCalledWith('/api/v1/tasks/interrupts/answer', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ task_id: 'task-1', interrupt_id: 'interrupt-1', answer_payload: { crop: '水稻' } }),
    }));
  });

  it('lists conversations and conversation messages for history restore', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversations: [] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.listConversations();
    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations', expect.any(Object));

    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ conversation_id: 'conv-1', messages: [] }), { status: 200 }));
    await api.listConversationMessages('conv-1');
    expect(fetcher).toHaveBeenLastCalledWith('/api/v1/conversations/conv-1/messages', expect.any(Object));
  });

  it('lists unfinished conversation tasks for the composer stop action', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', tasks: [] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.listConversationTasks('conv-1', 'unfinished');

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/conv-1/tasks?scope=unfinished', expect.any(Object));
  });

  it('deletes a conversation by conversation id', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      conversation_id: 'conv-1',
      deleted: true,
      cancelled_task_ids: [],
      deleted_counts: { conversation: 1 },
    }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.deleteConversation('conv-1');

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations', expect.objectContaining({
      method: 'DELETE',
      body: JSON.stringify({ conversation_id: 'conv-1' }),
    }));
  });

  it('renames a conversation by conversation id', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      conversation_id: 'conv-1',
      account_id: 'alice',
      status: 'active',
      current_task_id: null,
      title: '新会话名称',
      created_at: null,
      updated_at: null,
    }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.renameConversation('conv-1', '新会话名称');

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ conversation_id: 'conv-1', title: '新会话名称' }),
    }));
  });

  it('exposes only public UI modes', () => {
    const api = createApiClient();
    expect(api.uiModes.map((mode) => mode.capabilityId)).toEqual([null]);
  });


  it('uploads a JSON or CSV file with multipart form data', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      upload_id: 'upl-1',
      conversation_id: 'conv-1',
      filename: 'materials.csv',
      content_type: 'text/csv',
      file_type: 'csv',
      size_bytes: 24,
      sha256: 'hash',
      expires_at: '2026-05-07T10:00:00',
      preview: { row_count: 1, columns: ['ped_id', 'design_check'], shape: 'table' },
    }), { status: 201 }));
    const api = createApiClient({ fetcher });
    const file = new File(['ped_id,design_check\nA,0\n'], 'materials.csv', { type: 'text/csv' });

    const result = await api.uploadConversationFile('conv-1', file);

    expect(result.upload_id).toBe('upl-1');
    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/uploads', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
    }));
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get('conversation_id')).toBe('conv-1');
    expect(init.headers).toBeUndefined();
  });



  it('lists and deletes uploaded conversation files', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', uploads: [] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.listConversationUploads('conv-1');
    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/conv-1/uploads', expect.any(Object));

    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ upload_id: 'upl-1', deleted: true }), { status: 200 }));
    const deleted = await api.deleteConversationUpload('conv-1', 'upl-1');
    expect(deleted.deleted).toBe(true);
    expect(fetcher).toHaveBeenLastCalledWith('/api/v1/conversations/uploads', expect.objectContaining({
      method: 'DELETE',
      body: JSON.stringify({ conversation_id: 'conv-1', upload_id: 'upl-1' }),
    }));
  });


  it('adds bearer authorization headers to multipart uploads when configured', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      upload_id: 'upl-1',
      conversation_id: 'conv-1',
      filename: 'materials.csv',
      content_type: 'text/csv',
      file_type: 'csv',
      size_bytes: 24,
      sha256: 'hash',
      expires_at: '2026-05-07T10:00:00',
      preview: { row_count: 1, columns: ['ped_id'], shape: 'table' },
    }), { status: 201 }));
    const api = createApiClient({ fetcher, accessToken: 'maf_tok_client' });
    const file = new File(['a,b\n'], 'materials.csv', { type: 'text/csv' });

    await api.uploadConversationFile('conv-1', file);

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/uploads', expect.objectContaining({
      headers: { Authorization: 'Bearer maf_tok_client' },
    }));
  });

});
