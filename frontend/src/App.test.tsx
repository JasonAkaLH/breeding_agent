import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
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
    createCaptcha: vi.fn(async () => ({ captcha_id: 'cap-1', image_svg: '<svg><text>1234</text></svg>', expires_in_seconds: 300 })),
    login: vi.fn(async () => ({ user: { username: 'alice' } })),
    register: vi.fn(async () => ({ user: { username: 'charlie' } })),
    logout: vi.fn(async () => ({ logged_out: true })),
    me: vi.fn(async () => ({ user: { username: 'alice' } })),
    listCapabilities: vi.fn(async () => ({ capabilities: [{ capability_id: 'sql_query.query', name: 'SQLQuery', description: 'SQL', version: '1', status: 'active' }] })),
    submitMessage: vi.fn(async () => ({ conversation_id: 'conv-test', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' })),
    listConversationUploads: vi.fn(async () => ({ conversation_id: 'conv-test', uploads: [] })),
    deleteConversationUpload: vi.fn(async (conversationId, uploadId) => ({ upload_id: uploadId, deleted: true })),
    uploadConversationFile: vi.fn(async (_conversationId, file) => ({
      upload_id: 'upl-1',
      conversation_id: 'conv-test',
      filename: file.name,
      content_type: file.type || 'text/csv',
      file_type: file.name.endsWith('.json') ? 'json' : 'csv',
      size_bytes: file.size,
      sha256: 'hash',
      expires_at: '2026-05-07T10:00:00',
      preview: { row_count: 1, columns: ['ped_id', 'design_check'], shape: 'table' },
    })),
    cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
    listConversations: vi.fn(async () => ({ conversations: [] })),
    listConversationMessages: vi.fn(async () => ({ conversation_id: 'conv-test', messages: [] })),
    deleteConversation: vi.fn(async () => ({
      conversation_id: 'conv-test',
      deleted: true,
      cancelled_task_ids: [],
      deleted_counts: { conversation: 1 },
    })),
    renameConversation: vi.fn(async (conversationId, title) => ({
      conversation_id: conversationId,
      account_id: 'alice',
      status: 'active',
      current_task_id: null,
      title,
      created_at: null,
      updated_at: null,
    })),
    listConversationTasks: vi.fn(async () => ({ conversation_id: 'conv-test', tasks: [] })),
    listInterrupts: vi.fn(async () => ({ task_id: 'task-1', interrupts: [] })),
    answerInterrupt: vi.fn(async () => ({ interrupt_id: 'interrupt-1', status: 'answered', node_id: 'node-1', answer_payload: {} })),
    getTask: vi.fn(),
    getTaskArtifacts: vi.fn(async () => ({ task_id: 'task-1', artifacts: [] })),
    getTaskGraph: vi.fn(),
    ...overrides,
  };
}

async function renderAuthed(ui: ReactElement) {
  render(ui);
  await screen.findByText('小奥Agent');
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

function makeSequencedEventSourceFactory(eventBatches: TaskEventEnvelope[][]): EventSourceFactory {
  let index = 0;
  return (_url, handlers) => {
    const events = eventBatches[Math.min(index, eventBatches.length - 1)] ?? [];
    index += 1;
    queueMicrotask(() => {
      for (const item of events) {
        handlers.onMessage(item);
      }
    });
    return { close: vi.fn() };
  };
}

describe('App', () => {
  it('shows login page when no session exists and logs in with captcha', async () => {
    const api = makeApi({
      me: vi.fn(async () => {
        throw new Error('unauthenticated');
      }),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('登录小奥Agent')).toBeInTheDocument();
    expect(api.createCaptcha).toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'alice-password' } });
    fireEvent.change(screen.getByLabelText('4位验证码'), { target: { value: '1234' } });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => expect(api.login).toHaveBeenCalledWith({
      username: 'alice',
      password: 'alice-password',
      captchaId: 'cap-1',
      captchaCode: '1234',
    }));
    expect(await screen.findByText('小奥Agent')).toBeInTheDocument();
    expect(screen.getByText('user: alice')).toBeInTheDocument();
  });

  it('creates a new user from the login page with a letter and digit password', async () => {
    const api = makeApi({
      me: vi.fn(async () => {
        throw new Error('unauthenticated');
      }),
      register: vi.fn(async () => ({ user: { username: 'charlie' } })),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('登录小奥Agent')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '创建新用户' }));
    expect(screen.getByText('创建小奥Agent用户')).toBeInTheDocument();
    expect(screen.getByText('密码至少 8 位，并且必须同时包含字母和数字。')).toBeInTheDocument();

    const submitButton = screen.getByRole('button', { name: '创建用户并登录' });
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'charlie' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'letters-only' } });
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'letters-only' } });
    fireEvent.change(screen.getByLabelText('4位验证码'), { target: { value: '1234' } });
    expect(submitButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'charlie1' } });
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'charlie1' } });
    fireEvent.click(submitButton);

    await waitFor(() => expect(api.register).toHaveBeenCalledWith({
      username: 'charlie',
      password: 'charlie1',
      captchaId: 'cap-1',
      captchaCode: '1234',
    }));
    expect(await screen.findByText('小奥Agent')).toBeInTheDocument();
    expect(screen.getByText('user: charlie')).toBeInTheDocument();
  });

  it('keeps task progress collapsed in the header until the user opens it', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(screen.getByRole('button', { name: /任务进程/ })).toBeInTheDocument();
    expect(screen.queryByText('准备就绪')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /任务进程/ }));

    expect(await screen.findByText('准备就绪')).toBeInTheDocument();
  });

  it('loads historical messages for the selected user-owned conversation', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          account_id: 'alice',
          status: 'active',
          current_task_id: null,
          title: '历史问题',
          created_at: null,
          updated_at: null,
        }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-history', stream_status: null, created_at: null },
          { message_id: 'msg-assistant', conversation_id: 'conv-history', role: 'assistant', content: '以前的回答', task_id: 'task-history', stream_status: 'complete', created_at: null },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));

    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith('conv-history'));
    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    expect(await screen.findByText('以前的回答')).toBeInTheDocument();
  });

  it('reloads the active historical conversation and continues with the same conversation id', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          account_id: 'alice',
          status: 'active',
          current_task_id: null,
          title: '历史问题',
          created_at: null,
          updated_at: null,
        }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-history', stream_status: null, created_at: null },
          { message_id: 'msg-assistant', conversation_id: 'conv-history', role: 'assistant', content: '以前的回答', task_id: 'task-history', stream_status: 'complete', created_at: null },
        ],
      })),
      submitMessage: vi.fn(async () => ({ conversation_id: 'conv-history', message_id: 'msg-follow-up', task_id: 'task-follow-up', status: 'accepted' })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    expect(screen.getByText('conversation: conv-history')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));

    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith('conv-history'));
    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    expect(await screen.findByText('以前的回答')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '继续问一个问题' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: 'conv-history',
      content: '继续问一个问题',
    })));
  });

  it('deletes a historical conversation after confirmation and removes it from the list', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          account_id: 'alice',
          status: 'active',
          current_task_id: null,
          title: '历史问题',
          created_at: null,
          updated_at: null,
        }],
      })),
      deleteConversation: vi.fn(async () => ({
        conversation_id: 'conv-history',
        deleted: true,
        cancelled_task_ids: [],
        deleted_counts: { conversation: 1, message: 2, task: 1 },
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除历史会话 历史问题' }));

    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith('conv-history'));
    expect(screen.queryByRole('button', { name: '历史问题' })).not.toBeInTheDocument();
    expect(await screen.findByText(/历史会话已删除/)).toBeInTheDocument();
    confirm.mockRestore();
  });

  it('renames a historical conversation and updates the visible title', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('新的会话名称');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          account_id: 'alice',
          status: 'active',
          current_task_id: null,
          title: '旧会话名称',
          created_at: null,
          updated_at: null,
        }],
      })),
      renameConversation: vi.fn(async () => ({
        conversation_id: 'conv-history',
        account_id: 'alice',
        status: 'active',
        current_task_id: null,
        title: '新的会话名称',
        created_at: null,
        updated_at: null,
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '重命名历史会话 旧会话名称' }));

    await waitFor(() => expect(api.renameConversation).toHaveBeenCalledWith('conv-history', '新的会话名称'));
    expect(await screen.findByRole('button', { name: '新的会话名称' })).toBeInTheDocument();
    prompt.mockRestore();
  });

  it('does not call rename API when the user cancels conversation rename', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue(null);
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          account_id: 'alice',
          status: 'active',
          current_task_id: null,
          title: '旧会话名称',
          created_at: null,
          updated_at: null,
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '重命名历史会话 旧会话名称' }));

    expect(api.renameConversation).not.toHaveBeenCalled();
    prompt.mockRestore();
  });

  it('switches to a new empty conversation after deleting the current conversation', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          account_id: 'alice',
          status: 'active',
          current_task_id: null,
          title: '历史问题',
          created_at: null,
          updated_at: null,
        }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-history', stream_status: null, created_at: null },
        ],
      })),
      deleteConversation: vi.fn(async () => ({
        conversation_id: 'conv-history',
        deleted: true,
        cancelled_task_ids: ['task-running'],
        deleted_counts: { conversation: 1, message: 1, task: 1 },
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(screen.getByText('conversation: conv-history')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));
    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '删除历史会话 历史问题' }));

    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith('conv-history'));
    expect(screen.queryByText('以前的问题')).not.toBeInTheDocument();
    expect(screen.getByText('开始一次业务问答')).toBeInTheDocument();
    expect(screen.queryByText('conversation: conv-history')).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it('submits normal chat and renders streaming answer', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
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

  it('does not submit while IME composition is confirming text with Enter', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: 'block' } });
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', charCode: 13 });

    expect(api.submitMessage).not.toHaveBeenCalled();

    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', charCode: 13 });

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledTimes(1));
  });

  it('lists temporary uploads and deletes one from the backend memory area', async () => {
    const api = makeApi({
      listConversationUploads: vi.fn(async () => ({
        conversation_id: 'conv-test',
        uploads: [{
          upload_id: 'upl-existing',
          conversation_id: 'conv-test',
          filename: 'existing.csv',
          content_type: 'text/csv',
          file_type: 'csv',
          size_bytes: 24,
          sha256: 'hash',
          expires_at: '2026-05-07T10:00:00',
          preview: { row_count: 1, columns: ['ped_id'], shape: 'table' },
        }],
      })),
      deleteConversationUpload: vi.fn(async () => ({ upload_id: 'upl-existing', deleted: true })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    await screen.findByText(/existing.csv/);
    fireEvent.click(screen.getByRole('button', { name: '删除文件 existing.csv' }));

    await waitFor(() => expect(api.deleteConversationUpload).toHaveBeenCalledWith(expect.any(String), 'upl-existing'));
    await waitFor(() => expect(screen.queryByText(/existing.csv/)).not.toBeInTheDocument());
  });

  it('uploads a CSV file and submits its upload id with the next message', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'materials.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON 或 CSV 文件'), { target: { files: [file] } });

    await screen.findByText(/materials.csv/);
    await screen.findByText(/1 行/);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用这个文件做3个区组RCBD' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalledWith(expect.any(String), file));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      metadata: { upload_ids: ['upl-1'] },
    })));
    expect(screen.getByText(/materials.csv/)).toBeInTheDocument();
  });

  it('uploads a CSV file by drag and drop', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'dragged.csv', { type: 'text/csv' });
    fireEvent.drop(screen.getByRole('button', { name: '拖拽上传 JSON 或 CSV 文件' }), {
      dataTransfer: { files: [file] },
    });

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalledWith(expect.any(String), file));
    expect(await screen.findByText(/dragged.csv/)).toBeInTheDocument();
  });

  it('submits deep thinking flag from the switch next to current mode', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

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
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
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
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    expect(screen.queryByLabelText('对话模式')).not.toBeInTheDocument();
    expect(screen.getByText('当前模式：自动规划')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    expect(screen.queryByText('主代理会自动判断是否需要调用 SQLQuery，无需手动切换模式。')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'chat' })));
    expect(await screen.findByText(/主代理已自动调用 SQLQuery/)).toBeInTheDocument();
    expect(await screen.findByText('SQLQuery 查询结果')).toBeInTheDocument();
    expect(screen.getByText('查询已完成，共返回 1 行结果。')).toBeInTheDocument();
    expect(screen.getByText('原始表格预览默认隐藏')).toBeInTheDocument();
    expect(screen.queryByText(/select secret/i)).not.toBeInTheDocument();
  });



  it('renders downloadable Skill output files as a unified attachment card', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'main_agent_text:1', producer_node_id: 'task-1:main_agent.respond', artifact_type: 'text', storage_ref: '已生成文件。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'art-file-1', producer_node_id: 'task-1:main_agent.respond', artifact_type: 'file', storage_ref: '', summary: 'HTML 布局', is_complete: true, created_at: null, filename: 'layout.html', mime_type: 'text/html', size_bytes: 12, download_url: '/api/v1/artifacts/art-file-1/download', source_file_count: 1, archive_format: null, retention_status: 'active' },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '生成文件' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('已生成文件。')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText('生成文件').length).toBeGreaterThan(0));
    expect(screen.getByText('layout.html')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /下\s*载/ });
    expect(link).toHaveAttribute('href', '/api/v1/artifacts/art-file-1/download');
  });

  it('shows the upstream capability currently being executed inside the assistant bubble', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
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
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

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
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} waitingInputCheckDelayMs={1} />);

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

  it('keeps the final assistant answer visible with capability results after interrupt resume', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:intent_route', capability_id: 'sql_query.intent_route', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
        { node_id: 'task-1:main_agent.respond', capability_id: 'main_agent.respond', status: 'pending', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const completedGraph = {
      ...waitingGraph,
      nodes: waitingGraph.nodes.map((node) => ({ ...node, status: 'completed' })),
    };
    const api = makeApi({
      getTaskGraph: vi.fn()
        .mockResolvedValueOnce(waitingGraph)
        .mockResolvedValue(completedGraph),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'task-1:intent_route',
          question: '请选择查询范围。',
          reason_code: 'route_not_resolved',
          required_fields: { route_id: { options: ['approval_variety_db'] } },
          status: 'open',
        }],
      })),
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'main_agent_text:1', producer_node_id: 'task-1:main_agent.respond', artifact_type: 'text', storage_ref: '最终主代理回答：没有找到符合条件的记录。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'filtered_query_result:1', producer_node_id: 'task-1:query_data:result_filtering', artifact_type: 'json', storage_ref: JSON.stringify({ columns: ['variety_name'], rows: [], row_count: 0, truncated: false }), summary: 'filtered', is_complete: true, created_at: null },
        ],
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [event('task.accepted')],
        [
          event('main_agent.output_delta', { delta: '流式主代理回答', ordinal: 1 }, 'delta-resumed-1'),
          event('task.completed', {}, 'task-completed-resumed'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询适合宁夏种植的棉花' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('region', { name: '需要补充信息' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '审定品种库' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.answerInterrupt).toHaveBeenCalledWith('task-1', 'interrupt-1', { route_id: '审定品种库' }));
    await waitFor(() => expect(api.getTaskArtifacts).toHaveBeenCalled());
    expect(await screen.findByText(/最终主代理回答/)).toBeInTheDocument();
    expect(await screen.findByText('SQLQuery 查询结果')).toBeInTheDocument();
    expect(screen.getByText('查询已完成，共返回 0 行结果。')).toBeInTheDocument();
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
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} waitingInputCheckDelayMs={1} />);

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

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('未完成任务')).toBeInTheDocument();
    expect(screen.queryByText('查询龙粳33')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '展开未完成任务' }));
    expect(await screen.findByText('查询龙粳33')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '停止任务 task-running' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-running'));
    await waitFor(() => expect(screen.getByText('暂无未完成任务')).toBeInTheDocument());
  });

});
