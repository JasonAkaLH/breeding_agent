import { describe, expect, it, vi } from 'vitest';
import { ApiError, createApiClient } from './client';

describe('createApiClient', () => {
  it('uses same-origin requests while auth is carried by bearer headers when present', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' }, access_token: 'maf_tok_login' }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.me();

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/me', expect.objectContaining({ credentials: 'same-origin' }));
  });


  it('adds bearer authorization headers when configured', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' }, access_token: 'maf_tok_login' }), { status: 200 }));
    const api = createApiClient({ fetcher, accessToken: 'maf_tok_client' });

    await api.me();

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/me', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer maf_tok_client' }),
    }));
  });

  it('logs in with username only and receives a bearer token', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' }, access_token: 'maf_tok_login' }), { status: 200 }));
    const api = createApiClient({ fetcher });

    const result = await api.login({ username: 'alice' });

    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/login', expect.objectContaining({ method: 'POST' }));
    expect(JSON.parse(fetcher.mock.calls[0][1].body as string)).toEqual({ username: 'alice' });
    expect(result.access_token).toBe('maf_tok_login');
  });


  it('refreshes the current bearer token through Authorization', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' }, access_token: 'maf_tok_refreshed' }), { status: 200 }));
    const api = createApiClient({ fetcher, accessToken: 'maf_tok_old' });

    const result = await api.refreshToken();

    expect(result.access_token).toBe('maf_tok_refreshed');
    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/refresh-token', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ Authorization: 'Bearer maf_tok_old' }),
    }));
  });

  it('does not expose legacy token-management clients', () => {
    const api = createApiClient();
    expect('createCaptcha' in api).toBe(false);
    expect('register' in api).toBe(false);
    expect('createApiToken' in api).toBe(false);
    expect('listApiTokens' in api).toBe(false);
    expect('revokeApiToken' in api).toBe(false);
  });

  it('lists public capabilities', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ capabilities: [{ capability_id: 'skill.data_lookup', name: 'data-lookup', display_name: '数据查询', description: '查询', version: '1', status: 'active', kind: 'skill', source: 'skill', source_path: 'data-lookup/SKILL.md' }] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    const result = await api.listCapabilities();

    expect(fetcher).toHaveBeenCalledWith('/api/v1/capabilities', expect.any(Object));
    expect(result.capabilities[0]).toMatchObject({ capability_id: 'skill.data_lookup', display_name: '数据查询', kind: 'skill' });
  });

  it('prefixes JSON requests with the configured subpath base URL', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ capabilities: [] }), { status: 200 }));
    const api = createApiClient({ fetcher, baseUrl: '/seedpilot' });

    await api.listCapabilities();

    expect(fetcher).toHaveBeenCalledWith('/seedpilot/api/v1/capabilities', expect.any(Object));
  });

  it('normalizes a trailing slash in the subpath base URL', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ user: { username: 'alice' }, access_token: 'maf_tok_login' }), { status: 200 }));
    const api = createApiClient({ fetcher, baseUrl: '/seedpilot/' });

    await api.me();

    expect(fetcher).toHaveBeenCalledWith('/seedpilot/api/v1/auth/me', expect.any(Object));
  });

  it('loads model edition choices from backend config', async () => {
    const response = {
      default_model_edition: 'deepseek-v4-flash-260425',
      options: [
        {
          value: 'deepseek-v4-flash-260425',
          label: 'DeepSeek V4 Flash',
          reasoning_efforts: {
            default: 'minimal',
            disabled_default: 'minimal',
            options: [
              { value: 'minimal', label: '最低', allow_when_thinking_disabled: true },
              { value: 'high', label: '高', allow_when_thinking_disabled: false },
              { value: 'max', label: '最高', allow_when_thinking_disabled: false },
            ],
          },
        },
        {
          value: 'deepseek-v4-pro-260425',
          label: 'DeepSeek V4 Pro',
          reasoning_efforts: {
            default: 'minimal',
            disabled_default: 'minimal',
            options: [
              { value: 'minimal', label: '最低', allow_when_thinking_disabled: true },
              { value: 'high', label: '高', allow_when_thinking_disabled: false },
              { value: 'max', label: '最高', allow_when_thinking_disabled: false },
            ],
          },
        },
      ],
    };
    const fetcher = vi.fn(async () => new Response(JSON.stringify(response), { status: 200 }));
    const api = createApiClient({ fetcher });

    await expect(api.getModelEditions()).resolves.toEqual(response);
    expect(fetcher).toHaveBeenCalledWith('/api/v1/config/model-editions', expect.any(Object));
  });

  it('submits normal chat with capability_id null', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', content: '你好', mode: 'chat' });

    expect(fetcher).toHaveBeenCalledWith('/api/v1/conversations/chat-messages', expect.objectContaining({ method: 'POST' }));
    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.conversation_id).toBe('conv-1');
    expect(body.capability_id).toBeNull();
    expect(body).not.toHaveProperty('model_edition');
  });

  it('submits selected model edition as a top-level request field', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({
      conversationId: 'conv-1',
      content: '用 pro 模型回答',
      mode: 'chat',
      modelEdition: 'deepseek-v4-pro-260425',
    });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.model_edition).toBe('deepseek-v4-pro-260425');
    expect(body.metadata).not.toHaveProperty('model_edition');
  });


  it('submits slash soft binding through main agent while preserving metadata', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({
      conversationId: 'conv-1',
      content: '查询龙粳33',
      mode: 'chat',
      capabilityId: 'main_agent.respond',
      metadata: {
        upload_ids: ['upl-1'],
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
        soft_skill_binding: { capability_id: 'skill.data_lookup', command: '/data-lookup' },
      },
    });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body).toMatchObject({
      routing_mode: 'force_capability',
      capability_id: 'main_agent.respond',
      metadata: {
        upload_ids: ['upl-1'],
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
        soft_skill_binding: { capability_id: 'skill.data_lookup', command: '/data-lookup' },
        deep_thinking: false,
      },
    });
    expect(body.metadata).not.toHaveProperty('main_agent_reasoning_effort');
  });

  it('omits reasoning effort when App does not provide one', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', content: '深入分析', mode: 'chat', deepThinking: true });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.metadata).toMatchObject({
      deep_thinking: true,
    });
    expect(body.metadata).not.toHaveProperty('main_agent_reasoning_effort');
  });

  it('passes App-provided reasoning effort even when thinking is disabled', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', content: '分析', mode: 'chat', reasoningEffort: 'max' });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.metadata).toMatchObject({
      deep_thinking: false,
      main_agent_reasoning_effort: 'max',
    });
  });

  it('submits selected reasoning effort only when deep thinking is enabled', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ conversation_id: 'conv-1', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' }), { status: 202 }));
    const api = createApiClient({ fetcher });

    await api.submitMessage({ conversationId: 'conv-1', content: '分析', mode: 'chat', deepThinking: true, reasoningEffort: 'max' });

    const body = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(body.metadata).toMatchObject({
      deep_thinking: true,
      main_agent_reasoning_effort: 'max',
    });
  });


  it('maps 409 busy conversation to business friendly error', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ detail: 'Conversation is busy' }), { status: 409 }));
    const api = createApiClient({ fetcher });

    await expect(api.submitMessage({ conversationId: 'conv-1', content: '你好', mode: 'chat' })).rejects.toMatchObject({
      userMessage: expect.stringContaining('当前会话已有任务'),
    });
  });

  it('lists task interrupts', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ task_id: 'task-1', interrupts: [] }), { status: 200 }));
    const api = createApiClient({ fetcher });

    await api.listInterrupts('task-1');
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/tasks/task-1/interrupts');
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
      username: 'alice',
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


  it('uploads a supported conversation file with multipart form data', async () => {
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

  it('prefixes multipart uploads with the configured subpath base URL', async () => {
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
    const api = createApiClient({ fetcher, baseUrl: '/seedpilot' });
    const file = new File(['a,b\n'], 'materials.csv', { type: 'text/csv' });

    await api.uploadConversationFile('conv-1', file);

    expect(fetcher).toHaveBeenCalledWith('/seedpilot/api/v1/conversations/uploads', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
    }));
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




  it('clears auth state when multipart upload is unauthorized', async () => {
    const onUnauthorized = vi.fn();
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ detail: 'expired' }), { status: 401 }));
    const api = createApiClient({ fetcher, accessToken: 'maf_tok_client', onUnauthorized });
    const file = new File(['a,b\n'], 'materials.csv', { type: 'text/csv' });

    await expect(api.uploadConversationFile('conv-1', file)).rejects.toMatchObject({ status: 401 });

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
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

  it('downloads artifacts through fetch with bearer authorization instead of URL tokens', async () => {
    const fetcher = vi.fn(async () => new Response('file-content', { status: 200 }));
    const createObjectUrl = vi.fn(() => 'blob:artifact-download');
    const revokeObjectUrl = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const originalCreateObjectUrl = URL.createObjectURL;
    const originalRevokeObjectUrl = URL.revokeObjectURL;
    URL.createObjectURL = createObjectUrl as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = revokeObjectUrl as unknown as typeof URL.revokeObjectURL;
    try {
      const api = createApiClient({ fetcher, accessToken: 'maf_tok_client' });

      await api.downloadArtifact('art-file-1', 'layout.html');

      expect(fetcher).toHaveBeenCalledWith('/api/v1/artifacts/art-file-1/download', expect.objectContaining({
        method: 'GET',
        credentials: 'same-origin',
        headers: { Authorization: 'Bearer maf_tok_client' },
      }));
      expect(createObjectUrl).toHaveBeenCalledOnce();
      expect(click).toHaveBeenCalledOnce();
      expect(revokeObjectUrl).toHaveBeenCalledWith('blob:artifact-download');
    } finally {
      URL.createObjectURL = originalCreateObjectUrl;
      URL.revokeObjectURL = originalRevokeObjectUrl;
      click.mockRestore();
    }
  });

  it('prefixes artifact downloads with the configured subpath base URL', async () => {
    const fetcher = vi.fn(async () => new Response('file-content', { status: 200 }));
    const originalCreateObjectUrl = URL.createObjectURL;
    const originalRevokeObjectUrl = URL.revokeObjectURL;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    URL.createObjectURL = vi.fn(() => 'blob:artifact-download') as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
    try {
      const api = createApiClient({ fetcher, baseUrl: '/seedpilot' });

      await api.downloadArtifact('art-file-1', 'layout.html');

      expect(fetcher).toHaveBeenCalledWith('/seedpilot/api/v1/artifacts/art-file-1/download', expect.objectContaining({
        method: 'GET',
        credentials: 'same-origin',
      }));
    } finally {
      URL.createObjectURL = originalCreateObjectUrl;
      URL.revokeObjectURL = originalRevokeObjectUrl;
      click.mockRestore();
    }
  });

});
