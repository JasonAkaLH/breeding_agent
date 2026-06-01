import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import type { ApiClient } from './api/client';
import type { ConversationMessagesResponse, DeleteConversationResponse, TaskEventEnvelope } from './api/types';
import type { EventSourceFactory, TaskEventHandlers } from './api/taskEvents';
import { WELCOME_PROMPTS } from './domain/welcomePrompts';

function makeApi(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    uiModes: [
      { key: 'chat', label: '普通对话', capabilityId: null },
    ],
    login: vi.fn(async () => ({ user: { username: 'alice' }, access_token: 'maf_tok_login' })),
    logout: vi.fn(async () => ({ logged_out: true })),
    me: vi.fn(async () => ({ user: { username: 'alice' } })),
    submitMessage: vi.fn(async () => ({ conversation_id: 'conv-test', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' })),
    listCapabilities: vi.fn(async () => ({
      capabilities: [
        { capability_id: 'skill.data_lookup', name: 'data-lookup', display_name: '数据查询', description: '只读数据库查询', version: '1', status: 'active', kind: 'skill', source: 'skill', source_path: 'data-lookup/SKILL.md' },
        { capability_id: 'skill.mini_breedstat_rcbd', name: 'mini-breedstat-rcbd', display_name: '试验设计', description: '生成 RCBD 随机区组设计', version: '1', status: 'active', kind: 'skill', source: 'skill', source_path: 'mini_breedstat_rcbd_skill/SKILL.md' },
        { capability_id: 'main_agent.respond', name: '普通对话', description: '主代理', version: '1', status: 'active', kind: 'builtin', source: 'builtin', source_path: '' },
      ],
    })),
    getModelEditions: vi.fn(async () => ({
      default_model_edition: 'deepseek-v4-flash-260425',
      options: [
        { value: 'deepseek-v4-flash-260425', label: 'DeepSeek V4 Flash' },
        { value: 'deepseek-v4-pro-260425', label: 'DeepSeek V4 Pro' },
      ],
    })),
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
      username: 'alice',
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
    downloadArtifact: vi.fn(async () => undefined),
    getTaskGraph: vi.fn(),
    ...overrides,
  };
}

async function renderAuthed(ui: ReactElement) {
  render(ui);
  await screen.findByText('小奥Agent');
}

function event(event_type: string, payload: Record<string, unknown> = {}, event_id = event_type, node_id: string | null = null): TaskEventEnvelope {
  return {
    event_id,
    conversation_id: 'conv-test',
    task_id: 'task-1',
    node_id,
    event_type,
    payload,
    created_at: '2026-04-27T00:00:00',
  };
}

function taskSummary(taskId: string, status: string) {
  return {
    task_id: taskId,
    conversation_id: 'conv-history',
    status,
    root_node_id: `${taskId}:root`,
    summary: null,
    requested_capability_id: null,
    active_node_count: status === 'completed' || status === 'failed' || status === 'cancelled' ? 0 : 1,
    completed_node_count: status === 'completed' ? 1 : 0,
    failed_node_count: status === 'failed' ? 1 : 0,
    cancel_requested: status === 'cancelling' || status === 'cancelled',
    created_at: null,
    updated_at: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
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

function makeInspectableEventSourceFactory(events: TaskEventEnvelope[] = []) {
  const urls: string[] = [];
  const factory: EventSourceFactory = (url, handlers) => {
    urls.push(url);
    queueMicrotask(() => {
      for (const item of events) {
        handlers.onMessage(item);
      }
    });
    return { close: vi.fn() };
  };
  return { factory, urls };
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

function makeErrorEventSourceFactory(): EventSourceFactory {
  return (_url, handlers) => {
    handlers.onError(new Error('stream disconnected'));
    return { close: vi.fn() };
  };
}

describe('App', () => {
  afterEach(() => {
    localStorage.clear();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('shows login page when no token exists and logs in with username only', async () => {
    const api = makeApi({
      me: vi.fn(async () => {
        throw new Error('unauthenticated');
      }),
    });
    render(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('登录小奥Agent')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => expect(api.login).toHaveBeenCalledWith({ username: 'alice' }));
    expect(localStorage.getItem('maf.frontend.access_token')).toBe('maf_tok_login');
    expect(await screen.findByText('小奥Agent')).toBeInTheDocument();
    expect(screen.queryByText('user: alice')).not.toBeInTheDocument();
  });

  it('does not render the old top-right task progress capsule', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(screen.queryByText('业务对话')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('任务进程悬浮胶囊')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /任务进程/ })).not.toBeInTheDocument();
    expect(screen.queryByText('准备就绪')).not.toBeInTheDocument();
  });

  it('renders history in a left sidebar and keeps a minimal floating send bar in the chat workspace', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const sidebar = screen.getByRole('complementary', { name: '历史会话侧边栏' });
    const workspace = screen.getByRole('main', { name: '对话工作区' });
    const sendBar = within(workspace).getByRole('region', { name: '悬浮发送栏' });
    const sendRow = within(sendBar).getByRole('group', { name: '消息发送栏' });
    const conversationList = within(workspace).getByLabelText('对话内容');

    expect(within(workspace).queryByText('业务对话')).not.toBeInTheDocument();
    expect(within(workspace).queryByLabelText('任务进程悬浮胶囊')).not.toBeInTheDocument();
    expect(within(sidebar).getByText('历史会话')).toBeInTheDocument();
    const historyRefreshButton = within(sidebar).getByRole('button', { name: '刷新历史会话' });
    expect(historyRefreshButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '刷新历史会话');
    const userCard = within(sidebar).getByRole('region', { name: '用户信息与账户操作' });
    expect(within(userCard).queryByText('用户信息')).not.toBeInTheDocument();
    expect(within(userCard).getByText('alice')).toBeInTheDocument();
    const accountSettingsButton = within(userCard).getByRole('button', { name: '用户账户设置' });
    expect(accountSettingsButton).toBeInTheDocument();
    expect(within(userCard).queryByText('用户账户设置')).not.toBeInTheDocument();
    expect(accountSettingsButton.querySelector('img')).toHaveAttribute('src', '/pics/account-settings-gear-button.svg?v=20260511-gear-visible');
    expect(accountSettingsButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '用户账户设置');
    expect(within(userCard).getByRole('button', { name: '退出登录' })).toBeInTheDocument();
    expect(within(workspace).queryByRole('button', { name: '退出登录' })).not.toBeInTheDocument();
    fireEvent.click(accountSettingsButton);
    expect(await screen.findByText('用户账户设置功能会在后续版本开放。')).toBeInTheDocument();
    expect(conversationList).toBeInTheDocument();
    expect(conversationList.parentElement).toHaveClass('app-content');
    expect(conversationList.closest('.conversation-card')).toBeNull();
    expect(screen.queryByText('主代理可用')).not.toBeInTheDocument();
    expect(screen.queryByText('数据查询可用')).not.toBeInTheDocument();
    expect(screen.queryByText(/user:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/conversation:/)).not.toBeInTheDocument();
    expect(within(sendRow).getByLabelText('请输入问题')).toHaveAttribute('placeholder', '从这里开始...');
    const sendButton = within(sendRow).getByRole('button', { name: '发送' });
    const inputMenuButton = within(sendRow).getByRole('button', { name: '打开输入功能菜单' });
    expect(sendButton).toBeInTheDocument();
    expect(inputMenuButton).toBeInTheDocument();
    expect(sendButton.querySelector('img')).toHaveAttribute('src', '/pics/send-up-arrow-button.svg?v=20260511-arrow-balanced');
    expect(inputMenuButton.querySelector('img')).toHaveAttribute('src', '/pics/input-menu-plus-button.svg');
    expect(sendButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '发送');
    expect(inputMenuButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '打开输入功能菜单');
    expect(within(sendRow).queryByRole('button', { name: '选择 JSON、CSV、图片或 PDF 文件' })).not.toBeInTheDocument();
    expect(sendBar).toHaveClass('floating-composer');

    fireEvent.click(inputMenuButton);
    expect(await screen.findByRole('button', { name: '选择 JSON、CSV、图片或 PDF 文件' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByLabelText('思考强度').length).toBeGreaterThan(0));
    expect(await screen.findByLabelText('深度思考')).toBeInTheDocument();
  });

  it('shows stream interruption feedback as a five-second popup instead of an inline prompt box', async () => {
    const api = makeApi({
      getTask: vi.fn(async () => ({
        task_id: 'task-1',
        conversation_id: 'conv-test',
        status: 'running',
        root_node_id: 'task-1:main',
        summary: null,
        requested_capability_id: null,
        active_node_count: 1,
        completed_node_count: 0,
        failed_node_count: 0,
        cancel_requested: false,
        created_at: null,
        updated_at: null,
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeErrorEventSourceFactory()} />);

    vi.useFakeTimers();
    try {
      fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
      fireEvent.click(screen.getByRole('button', { name: '发送' }));

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(api.getTask).toHaveBeenCalledWith('task-1');
      const noticeText = screen.getByText('事件流暂时中断，正在尝试查询任务状态。');
      expect(noticeText.closest('.toast-notice')).not.toBeNull();
      expect(noticeText.closest('.app-content')).toBeNull();

      await act(async () => {
        vi.advanceTimersByTime(4_999);
      });
      expect(screen.getByText('事件流暂时中断，正在尝试查询任务状态。')).toBeInTheDocument();

      await act(async () => {
        vi.advanceTimersByTime(1);
      });
      expect(screen.queryByText('事件流暂时中断，正在尝试查询任务状态。')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('loads historical messages for the selected user-owned conversation', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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

  it('shows an icon-only copy action below completed assistant replies and copies their text', async () => {
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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

    try {
      await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

      fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));

      const assistantText = await screen.findByText('以前的回答');
      const assistantBubble = assistantText.closest('.message-assistant') as HTMLElement;
      expect(assistantBubble).not.toBeNull();
      const copyButton = within(assistantBubble).getByRole('button', { name: '复制' });
      expect(copyButton).not.toHaveTextContent('复制');
      expect(copyButton.querySelector('svg')).not.toBeNull();
      expect(copyButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '复制');

      fireEvent.click(copyButton);

      await waitFor(() => expect(writeText).toHaveBeenCalledWith('以前的回答'));
    } finally {
      if (originalClipboard) {
        Object.defineProperty(navigator, 'clipboard', originalClipboard);
      } else {
        delete (navigator as { clipboard?: unknown }).clipboard;
      }
    }
  });

  it('falls back to document copy when clipboard write is rejected', async () => {
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    const originalExecCommand = document.execCommand;
    const writeText = vi.fn(async () => {
      throw new Error('clipboard denied');
    });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    document.execCommand = execCommand;
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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
          { message_id: 'msg-assistant', conversation_id: 'conv-history', role: 'assistant', content: 'fallback 复制内容', task_id: 'task-history', stream_status: 'complete', created_at: null },
        ],
      })),
    });

    try {
      await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

      fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));
      const assistantText = await screen.findByText('fallback 复制内容');
      const assistantBubble = assistantText.closest('.message-assistant') as HTMLElement;
      fireEvent.click(within(assistantBubble).getByRole('button', { name: '复制' }));

      await waitFor(() => expect(writeText).toHaveBeenCalledWith('fallback 复制内容'));
      await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'));
    } finally {
      if (originalClipboard) {
        Object.defineProperty(navigator, 'clipboard', originalClipboard);
      } else {
        delete (navigator as { clipboard?: unknown }).clipboard;
      }
      if (originalExecCommand) {
        document.execCommand = originalExecCommand;
      } else {
        delete (document as { execCommand?: unknown }).execCommand;
      }
    }
  });

  it('does not show the copy action for historical assistant replies that are not complete', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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
          { message_id: 'msg-assistant', conversation_id: 'conv-history', role: 'assistant', content: '还没完成的历史回答', task_id: 'task-history', stream_status: 'streaming', created_at: null },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));

    const assistantText = await screen.findByText('还没完成的历史回答');
    const assistantBubble = assistantText.closest('.message-assistant') as HTMLElement;
    expect(assistantBubble).not.toBeNull();
    expect(within(assistantBubble).queryByRole('button', { name: '复制' })).not.toBeInTheDocument();
  });

  it('does not restore transient skill status lines when loading history', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: null, title: '历史问题', created_at: null, updated_at: null }],
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

    expect(await screen.findByText('以前的回答')).toBeInTheDocument();
    expect(document.querySelector('.skill-status-lines')).toBeNull();
  });

  it('restores an active current task after automatic session recovery', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-running', title: '历史问题', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-running', stream_status: null, created_at: null },
        ],
      })),
      getTask: vi.fn(async () => taskSummary('task-running', 'running')),
    });
    const events = [
      event('task.accepted', {}, 'restore-accepted'),
      event('task.graph_created', {}, 'restore-graph'),
      event('node.started', { capability_id: 'main_agent.respond' }, 'restore-node', 'node-main'),
      event('main_agent.output_delta', { delta: '已生成内容', response_role: 'final' }, 'restore-output'),
    ].map((item) => ({ ...item, task_id: 'task-running' }));
    const eventSource = makeInspectableEventSourceFactory(events);

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    expect(await screen.findByText('已生成内容')).toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).toBeDisabled();
    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
    expect(api.getTask).toHaveBeenCalledWith('task-running');
    expect(eventSource.urls).toEqual(['/api/v1/tasks/task-running/events']);
  });

  it('restores an active current task after explicit login', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      me: vi.fn(async () => {
        throw new Error('unauthenticated');
      }),
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-running', title: '历史问题', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '登录前的问题', task_id: 'task-running', stream_status: null, created_at: null },
        ],
      })),
      getTask: vi.fn(async () => taskSummary('task-running', 'running')),
    });
    const eventSource = makeInspectableEventSourceFactory([
      { ...event('main_agent.output_delta', { delta: '登录后恢复内容', response_role: 'final' }, 'restore-login-output'), task_id: 'task-running' },
    ]);

    render(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('登录小奥Agent')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    expect(await screen.findByText('登录前的问题')).toBeInTheDocument();
    expect(await screen.findByText('登录后恢复内容')).toBeInTheDocument();
    expect(api.getTask).toHaveBeenCalledWith('task-running');
  });

  it('self-heals a completed current task by showing formal history instead of a restoring bubble', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-completed', title: '历史问题', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn()
        .mockResolvedValueOnce({
          conversation_id: 'conv-history',
          messages: [
            { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-completed', stream_status: null, created_at: null },
          ],
        })
        .mockResolvedValueOnce({
          conversation_id: 'conv-history',
          messages: [
            { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-completed', stream_status: null, created_at: null },
            { message_id: 'msg-assistant', conversation_id: 'conv-history', role: 'assistant', content: '正式历史回答', task_id: 'task-completed', stream_status: 'complete', created_at: null },
          ],
        }),
      getTask: vi.fn(async () => taskSummary('task-completed', 'completed')),
    });
    const eventSource = makeInspectableEventSourceFactory([]);

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('正式历史回答')).toBeInTheDocument();
    expect(screen.queryByText(/正在恢复任务状态/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).not.toBeDisabled();
    expect(eventSource.urls).toEqual([]);
  });

  it('does not restore unfinished tasks when current_task_id is null', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: null, title: '历史问题', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '没有当前任务的问题', task_id: 'task-old', stream_status: null, created_at: null },
        ],
      })),
      listConversationTasks: vi.fn(async () => ({ conversation_id: 'conv-history', tasks: [taskSummary('task-old', 'running')] })),
      getTask: vi.fn(async () => taskSummary('task-old', 'running')),
    });
    const eventSource = makeInspectableEventSourceFactory([]);

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('没有当前任务的问题')).toBeInTheDocument();
    expect(api.getTask).not.toHaveBeenCalled();
    expect(api.listConversationTasks).not.toHaveBeenCalled();
    expect(eventSource.urls).toEqual([]);
    expect(screen.getByLabelText('请输入问题')).not.toBeDisabled();
  });

  it('fails safe and releases input when current task status is unknown', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-paused', title: '历史问题', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '未知状态问题', task_id: 'task-paused', stream_status: null, created_at: null },
        ],
      })),
      getTask: vi.fn(async () => taskSummary('task-paused', 'paused')),
    });
    const eventSource = makeInspectableEventSourceFactory([]);

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('未知状态问题')).toBeInTheDocument();
    expect(await screen.findByText(/任务状态暂不支持恢复/)).toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).not.toBeDisabled();
    expect(eventSource.urls).toEqual([]);
  });

  it('restores waiting-for-input state and continues the same task after an answer', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-running', title: '历史问题', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '需要补充的问题', task_id: 'task-running', stream_status: null, created_at: null },
        ],
      })),
      getTask: vi.fn(async () => taskSummary('task-running', 'running')),
      getTaskGraph: vi.fn(async () => ({
        task_id: 'task-running',
        nodes: [{
          node_id: 'node-wait',
          capability_id: 'skill.example',
          status: 'waiting_for_input',
          criticality: 'required',
          dependency_type: 'all_success',
          assigned_instance_id: null,
          started_at: null,
          finished_at: null,
        }],
        edges: [],
      })),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-running',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          task_id: 'task-running',
          node_id: 'node-wait',
          question: '请补充作物类型',
          required_fields: { crop: { options: ['rice'] } },
          status: 'open',
          created_at: null,
          answered_at: null,
        }],
      })),
      answerInterrupt: vi.fn(async () => ({ interrupt_id: 'interrupt-1', status: 'answered', node_id: 'node-wait', answer_payload: { crop: '水稻' } })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeInspectableEventSourceFactory([]).factory} waitingInputCheckDelayMs={1} />);

    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
    expect(await screen.findByText(/当前任务等待补充信息/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.answerInterrupt).toHaveBeenCalledWith('task-running', 'interrupt-1', { crop: '水稻' }));
  });

  it('keeps the active conversation switch guard while a task is running', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-other', username: 'alice', status: 'active', current_task_id: null, title: '另一个会话', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-other',
        messages: [
          { message_id: 'other-msg', conversation_id: 'conv-other', role: 'user', content: '其它会话内容', task_id: null, stream_status: null, created_at: null },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '保持当前任务' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('button', { name: '停止' })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '另一个会话' }));

    expect(api.listConversationMessages).not.toHaveBeenCalledWith('conv-other');
    expect(screen.queryByText('其它会话内容')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
  });

  it('ignores stale conversation restore responses after switching conversations', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-a');
    const convA = deferred<ConversationMessagesResponse>();
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-a', username: 'alice', status: 'active', current_task_id: null, title: '会话 A', created_at: null, updated_at: null },
          { conversation_id: 'conv-b', username: 'alice', status: 'active', current_task_id: null, title: '会话 B', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async (conversationId) => {
        if (conversationId === 'conv-a') {
          return convA.promise;
        }
        return {
          conversation_id: 'conv-b',
          messages: [
            { message_id: 'msg-b', conversation_id: 'conv-b', role: 'user', content: 'B 的内容', task_id: null, stream_status: null, created_at: null },
          ],
        };
      }),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);
    fireEvent.click(await screen.findByRole('button', { name: '会话 B' }));
    convA.resolve({
      conversation_id: 'conv-a',
      messages: [
        { message_id: 'msg-a', conversation_id: 'conv-a', role: 'user', content: 'A 的过期内容', task_id: null, stream_status: null, created_at: null },
      ],
    });

    expect(await screen.findByText('B 的内容')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('A 的过期内容')).not.toBeInTheDocument());
  });

  it('renders history entries as flat rows with hover-revealed actions', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
          status: 'active',
          current_task_id: null,
          title: '历史问题',
          created_at: null,
          updated_at: null,
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const historyItem = await screen.findByRole('button', { name: '历史问题' });
    expect(historyItem).toHaveClass('history-item');
    expect(historyItem).not.toHaveClass('ant-btn');
    expect(within(historyItem).getByText('历史问题')).toHaveClass('history-item-title');

    const row = historyItem.closest('.history-row') as HTMLElement;
    expect(row).not.toBeNull();
    const actions = within(row).getByLabelText('历史会话操作 历史问题');
    expect(actions).toHaveClass('history-actions');
    expect(within(actions).getByRole('button', { name: '重命名历史会话 历史问题' })).toBeInTheDocument();
    expect(within(actions).getByRole('button', { name: '删除历史会话 历史问题' })).toBeInTheDocument();
  });

  it('renders the history header without a count and uses an icon-only refresh action', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
          status: 'active',
          current_task_id: null,
          title: '历史问题',
          created_at: null,
          updated_at: null,
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const sidebar = screen.getByRole('complementary', { name: '历史会话侧边栏' });
    const historyHeader = within(sidebar).getByText('历史会话').closest('.ant-card-head') as HTMLElement;
    expect(historyHeader).not.toBeNull();
    expect(within(historyHeader).queryByText('1')).not.toBeInTheDocument();
    expect(historyHeader.querySelector('.ant-tag')).toBeNull();

    const refreshButton = within(historyHeader).getByRole('button', { name: '刷新历史会话' });
    expect(refreshButton).toHaveClass('history-refresh-button');
    expect(refreshButton).not.toHaveTextContent('刷新');
  });

  it('reloads the active historical conversation and continues with the same conversation id', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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

    expect(screen.queryByText('conversation: conv-history')).not.toBeInTheDocument();
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

  it('shows a spinner only on the target history item while deletion is pending', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const pendingDelete = deferred<DeleteConversationResponse>();
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          {
            conversation_id: 'conv-target',
            username: 'alice',
            status: 'active',
            current_task_id: null,
            title: '待删除会话',
            created_at: null,
            updated_at: null,
          },
          {
            conversation_id: 'conv-other',
            username: 'alice',
            status: 'active',
            current_task_id: null,
            title: '其他会话',
            created_at: null,
            updated_at: null,
          },
        ],
      })),
      deleteConversation: vi.fn(() => pendingDelete.promise),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除历史会话 待删除会话' }));

    expect(await screen.findByRole('status', { name: '正在删除历史会话 待删除会话' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^待删除会话/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: '重命名历史会话 待删除会话' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '删除历史会话 待删除会话' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '其他会话' })).not.toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '其他会话' }));
    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith('conv-other'));

    pendingDelete.resolve({
      conversation_id: 'conv-target',
      deleted: true,
      cancelled_task_ids: [],
      deleted_counts: { conversation: 1 },
      delete_status: 'completed',
      runner_id: 'delete-test',
      started_at: null,
      finished_at: null,
      error_code: null,
    });
    await waitFor(() => expect(screen.queryByRole('button', { name: '待删除会话' })).not.toBeInTheDocument());
    confirm.mockRestore();
  });

  it('deletes a historical conversation after confirmation and removes it from the list', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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

  it('shows deleted conversation feedback in a transient popup for five seconds', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
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
    const deleteButton = await screen.findByRole('button', { name: '删除历史会话 历史问题' });

    vi.useFakeTimers();
    try {
      fireEvent.click(deleteButton);

      await act(async () => {
        await Promise.resolve();
      });
      expect(api.deleteConversation).toHaveBeenCalledWith('conv-history');
      const notice = screen.getByRole('status');
      expect(within(notice).getByText('历史会话已删除。')).toBeInTheDocument();

      await act(async () => {
        vi.advanceTimersByTime(4_999);
      });
      expect(screen.getByRole('status')).toBeInTheDocument();

      await act(async () => {
        vi.advanceTimersByTime(1);
      });
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
      confirm.mockRestore();
    }
  });

  it('renames a historical conversation and updates the visible title', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('新的会话名称');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history',
          username: 'alice',
          status: 'active',
          current_task_id: null,
          title: '旧会话名称',
          created_at: null,
          updated_at: null,
        }],
      })),
      renameConversation: vi.fn(async () => ({
        conversation_id: 'conv-history',
        username: 'alice',
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
          username: 'alice',
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
          username: 'alice',
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

    expect(screen.queryByText('conversation: conv-history')).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));
    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '删除历史会话 历史问题' }));

    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith('conv-history'));
    expect(screen.queryByText('以前的问题')).not.toBeInTheDocument();
    const welcomeHeading = screen.getByRole('heading', { level: 4 });
    expect(WELCOME_PROMPTS).toContain(welcomeHeading.textContent);
    expect(screen.queryByText('直接描述你的问题即可。')).not.toBeInTheDocument();
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

    await waitFor(() => expect(api.getModelEditions).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'chat' })));
    expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ modelEdition: 'deepseek-v4-flash-260425' }));
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

  it('renders a failed task bubble with a red error icon instead of a spinner', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('node.started', { capability_id: 'skill.data_lookup', skill_name: 'data-lookup' }, 'node-started', 'node-data'),
      event('node.failed', { code: 'data_access_deadline_exceeded' }, 'node-failed', 'node-data'),
      event('task.failed', {}, 'task-failed'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '触发失败' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const failureText = await screen.findByText('数据库查询超时，请稍后重试或缩小查询范围。');
    const notice = failureText.closest('.activity-notice') as HTMLElement;
    expect(notice).not.toBeNull();
    expect(notice).toHaveClass('activity-notice-failed');
    expect(within(notice).getByLabelText('任务失败')).toBeInTheDocument();
    expect(notice.querySelector('.ant-spin')).toBeNull();
    expect(screen.queryByText('正在等待回答...')).not.toBeInTheDocument();
  });

  it('submits long composer input without a character cap', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const input = screen.getByLabelText('请输入问题');
    const longPrompt = `请完整分析以下长文本：${'不要截断这段输入。'.repeat(800)}`;
    expect(input).not.toHaveAttribute('maxlength');
    expect(input).toHaveAttribute('wrap', 'soft');

    fireEvent.change(input, { target: { value: longPrompt } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: longPrompt,
    })));
  });

  it('only follows streaming output when the conversation view is already at the bottom', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    const conversationList = screen.getByLabelText('对话内容') as HTMLDivElement;
    Object.defineProperty(conversationList, 'scrollHeight', { configurable: true, value: 1200 });
    Object.defineProperty(conversationList, 'clientHeight', { configurable: true, value: 600 });
    conversationList.scrollTop = 600;
    fireEvent.scroll(conversationList);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '生成一段长回答' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalled());
    expect(conversationList.scrollTop).toBe(1200);

    Object.defineProperty(conversationList, 'scrollHeight', { configurable: true, value: 1600 });
    await act(async () => {
      streamHandlers?.onMessage(event('main_agent.output_delta', { delta: '第一段内容。', ordinal: 1 }, 'delta-1'));
    });

    await screen.findByText('第一段内容。');
    expect(conversationList.scrollTop).toBe(1600);

    Object.defineProperty(conversationList, 'scrollHeight', { configurable: true, value: 2000 });
    conversationList.scrollTop = 200;
    fireEvent.scroll(conversationList);
    await act(async () => {
      streamHandlers?.onMessage(event('main_agent.output_delta', { delta: '继续生成。', ordinal: 2 }, 'delta-2'));
    });

    await screen.findByText(/第一段内容。继续生成。/);
    expect(conversationList.scrollTop).toBe(200);

    conversationList.scrollTop = 1400;
    fireEvent.scroll(conversationList);
    Object.defineProperty(conversationList, 'scrollHeight', { configurable: true, value: 2400 });
    await act(async () => {
      streamHandlers?.onMessage(event('main_agent.output_delta', { delta: '回到底部后继续。', ordinal: 3 }, 'delta-3'));
    });

    await screen.findByText(/回到底部后继续。/);
    expect(conversationList.scrollTop).toBe(2400);
  });


  it('opens the slash Skill picker, selects a Skill with the keyboard, and submits a soft-bound main-agent route from the badge', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    const input = screen.getByLabelText('请输入问题');

    await waitFor(() => expect(api.listCapabilities).toHaveBeenCalled());
    fireEvent.change(input, { target: { value: '/data' } });

    expect(await screen.findByRole('listbox', { name: 'Skill 命令列表' })).toBeInTheDocument();
    expect(screen.getByText('/data-lookup')).toBeInTheDocument();
    expect(screen.getByText('数据查询')).toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', charCode: 13 });

    expect(await screen.findByRole('status', { name: '已选择 Skill' })).toHaveTextContent('/data-lookup');
    expect(screen.getByRole('status', { name: '已选择 Skill' })).toHaveTextContent('数据查询');
    fireEvent.change(input, { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '查询龙粳33',
      capabilityId: 'main_agent.respond',
      metadata: expect.objectContaining({
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
        soft_skill_binding: { capability_id: 'skill.data_lookup', command: '/data-lookup' },
      }),
    })));
  });

  it('removes the selected slash Skill badge and returns to auto routing', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: '/data' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', charCode: 13 });
    fireEvent.click(await screen.findByRole('button', { name: '取消 Skill /data-lookup' }));

    expect(screen.queryByRole('status', { name: '已选择 Skill' })).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: '普通问题' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '普通问题',
      capabilityId: null,
    })));
  });

  it('supports mouse selection and Escape closing for the slash Skill picker', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: '/' } });
    expect(await screen.findByRole('listbox', { name: 'Skill 命令列表' })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' });
    expect(screen.queryByRole('listbox', { name: 'Skill 命令列表' })).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: '' } });
    fireEvent.change(input, { target: { value: '/' } });
    fireEvent.click(await screen.findByText('/mini-breedstat-rcbd'));
    expect(await screen.findByRole('status', { name: '已选择 Skill' })).toHaveTextContent('/mini-breedstat-rcbd');
    expect(screen.getByRole('status', { name: '已选择 Skill' })).toHaveTextContent('试验设计');
  });

  it('submits direct slash command input as a soft-bound main-agent call with cleaned content', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: '/data-lookup 查询龙粳33' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', charCode: 13 });

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '查询龙粳33',
      capabilityId: 'main_agent.respond',
      metadata: expect.objectContaining({
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
        soft_skill_binding: { capability_id: 'skill.data_lookup', command: '/data-lookup' },
      }),
    })));
  });

  it('allows exact direct slash command submit with empty args', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: '/data-lookup' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '',
      capabilityId: 'main_agent.respond',
      metadata: expect.objectContaining({
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
        soft_skill_binding: { capability_id: 'skill.data_lookup', command: '/data-lookup' },
      }),
    })));
  });

  it('blocks unknown slash command input instead of submitting it as normal chat', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: '/unknown 查询龙粳33' } });
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(api.submitMessage).not.toHaveBeenCalled();
    expect(await screen.findByText('未找到 Skill')).toBeInTheDocument();
  });

  it('submits uploaded files and slash soft-binding metadata together', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'materials.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、Excel、图片或 PDF 文件'), { target: { files: [file] } });

    await screen.findByText(/materials.csv/);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '/mini-breedstat-rcbd 用这个文件做3个区组RCBD' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '用这个文件做3个区组RCBD',
      capabilityId: 'main_agent.respond',
      metadata: expect.objectContaining({
        upload_ids: ['upl-1'],
        forced_by_slash_command: true,
        slash_command: '/mini-breedstat-rcbd',
        soft_skill_binding: { capability_id: 'skill.mini_breedstat_rcbd', command: '/mini-breedstat-rcbd' },
      }),
    })));
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
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、Excel、图片或 PDF 文件'), { target: { files: [file] } });

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
    const uploadDropZone = screen.getByRole('region', { name: '拖拽上传区' });

    fireEvent.dragOver(uploadDropZone, {
      dataTransfer: { files: [file], types: ['Files'] },
    });
    expect(uploadDropZone).toHaveClass('chat-floating-stack-dragging');

    fireEvent.drop(uploadDropZone, {
      dataTransfer: { files: [file] },
    });

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalledWith(expect.any(String), file));
    expect(await screen.findByText(/dragged.csv/)).toBeInTheDocument();
    expect(uploadDropZone).not.toHaveClass('chat-floating-stack-dragging');
  });

  it('submits deep thinking flag from the composer function menu', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    await waitFor(() => expect(screen.getAllByLabelText('思考强度').length).toBeGreaterThan(0));
    fireEvent.click(await screen.findByLabelText('深度思考'));
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '请深入分析' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'chat',
      deepThinking: true,
      reasoningEffort: 'minimal',
    })));
  });


  it('disables reasoning effort selection while deep thinking is off', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    await waitFor(() => expect(screen.getAllByLabelText('思考强度').length).toBeGreaterThan(0));
    const effortSelect = screen.getAllByLabelText('思考强度')[0];
    expect(effortSelect).toHaveClass('ant-select-disabled');

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '普通回答' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      deepThinking: false,
      reasoningEffort: 'minimal',
    })));
  });

  it('shows a reasoning box placeholder when deep thinking is enabled but no reasoning content arrives', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('main_agent.output_delta', { delta: '最终回答', ordinal: 1 }, 'delta-1'),
      event('task.completed'),
    ])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    fireEvent.click(await screen.findByLabelText('深度思考'));
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
          { artifact_id: 'main_agent_text:1', producer_node_id: 'main_agent.respond', artifact_type: 'text', storage_ref: '主代理已自动调用数据查询能力，龙粳33共 1 行。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'query_result_preview:1', producer_node_id: 'task-1:query_data:execute_query', artifact_type: 'json', storage_ref: JSON.stringify({ columns: ['variety_name'], rows: [{ variety_name: '龙粳33' }], row_count: 1, truncated: false }), summary: 'preview', is_complete: true, created_at: null },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    expect(screen.queryByLabelText('对话模式')).not.toBeInTheDocument();
    expect(screen.queryByText('当前模式：自动规划')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    expect(screen.queryByText('主代理会自动判断是否需要调用数据查询能力，无需手动切换模式。')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({ mode: 'chat' })));
    expect(await screen.findByText(/主代理已自动调用数据查询能力/)).toBeInTheDocument();
    expect(await screen.findByText('数据查询结果')).toBeInTheDocument();
    expect(screen.getByText('数据查询已完成，共返回 1 行结果。')).toBeInTheDocument();
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
    const download = screen.getByRole('button', { name: /下\s*载/ });
    fireEvent.click(download);
    expect(api.downloadArtifact).toHaveBeenCalledWith('art-file-1', 'layout.html');
  });

  it('renders OCR raw text artifacts as a collapsible card inside the assistant bubble', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'main_agent_text:1', producer_node_id: 'task-1:main_agent.respond', artifact_type: 'text', storage_ref: '主代理总结：图片中包含品种和处理信息。', summary: 'final', is_complete: true, created_at: null },
          {
            artifact_id: 'task-1:skill_display:abc:ocr_raw_text',
            producer_node_id: 'task-1:ocr:skill_execute',
            artifact_type: 'json',
            storage_ref: JSON.stringify({
              domain_kind: 'ocr',
              artifact_role: 'ocr_raw_text',
              raw_text: '品种：龙粳33\n处理：A1',
              filename: 'scan.png',
              status: 'succeeded',
            }),
            summary: 'OCR 回传原文：scan.png',
            is_complete: true,
            created_at: null,
          },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '识别图片' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const summary = await screen.findByText('主代理总结：图片中包含品种和处理信息。');
    const assistantMessage = summary.closest('.message-assistant') as HTMLElement;
    expect(assistantMessage).not.toBeNull();
    const messageBody = assistantMessage.querySelector('.message-body') as HTMLElement;
    expect(within(messageBody).getByText('OCR 回传原文：scan.png')).toBeInTheDocument();
    expect(within(messageBody).getByText('scan.png')).toBeInTheDocument();
    expect(messageBody.querySelector('.ocr-raw-text-content')?.textContent).toBe('品种：龙粳33\n处理：A1');
    const toggle = within(messageBody).getByRole('button', { name: '展开原文' });
    fireEvent.click(toggle);
    expect(within(messageBody).getByRole('button', { name: '收起原文' })).toBeInTheDocument();
  });

  it('shows skill progress as a lightweight status line outside the assistant bubble', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-skill-started', 'node-sql'),
      event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', domain_kind: 'data_query', stage: 'execute_query' }, 'sql-execute-progress', 'node-sql'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const progressText = await screen.findByText('data-query：正在检索数据');
    const assistantMessage = progressText.closest('.message-assistant') as HTMLElement;
    expect(assistantMessage).not.toBeNull();
    expect(progressText.closest('.skill-status-lines')).not.toBeNull();
    expect(progressText.closest('.message-body')).toBeNull();
    expect(assistantMessage.querySelector('.skill-status-lines')).not.toBeNull();
    expect(within(assistantMessage.querySelector('.message-body') as HTMLElement).queryByText(/data-query/)).not.toBeInTheDocument();
  });

  it('renders multiple skill status lines while keeping the final answer in the assistant bubble', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询并生成设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalled());
    await waitFor(() => expect(streamHandlers).not.toBeNull());

    await act(async () => {
      streamHandlers?.onMessage(event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-start', 'node-sql'));
      streamHandlers?.onMessage(event('node.started', { capability_id: 'skill.rcbd', skill_name: 'RCBD' }, 'rcbd-start', 'node-rcbd'));
      streamHandlers?.onMessage(event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', stage: 'execute_query' }, 'sql-progress', 'node-sql'));
      streamHandlers?.onMessage(event('skill.progress', { capability_id: 'skill.rcbd', skill_name: 'RCBD', label: '正在读取材料清单' }, 'rcbd-progress', 'node-rcbd'));
    });

    const sqlStatus = await screen.findByText('data-query：正在检索数据');
    const rcbdStatus = await screen.findByText('RCBD：正在读取材料清单');
    const assistantMessage = sqlStatus.closest('.message-assistant') as HTMLElement;
    expect(assistantMessage).not.toBeNull();
    expect(rcbdStatus.closest('.message-assistant')).toBe(assistantMessage);
    expect(assistantMessage.querySelectorAll('.skill-status-line')).toHaveLength(2);
    expect(sqlStatus.closest('.message-body')).toBeNull();
    expect(rcbdStatus.closest('.message-body')).toBeNull();

    await act(async () => {
      streamHandlers?.onMessage(event('main_agent.output_delta', { delta: '最终汇总回答', ordinal: 1, response_role: 'final' }, 'final-delta', 'node-final'));
      streamHandlers?.onMessage(event('main_agent.output_final', { response_role: 'final' }, 'final-output', 'node-final'));
      streamHandlers?.onMessage(event('task.completed', {}, 'task-completed'));
    });

    const finalAnswer = await screen.findByText('最终汇总回答');
    expect(finalAnswer.closest('.message-body')).not.toBeNull();
    expect(await screen.findByText('data-query：已完成')).toBeInTheDocument();
    expect(await screen.findByText('RCBD：已完成')).toBeInTheDocument();
  });

  it('does not show the assistant copy action until the reply is completed', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalled());
    await waitFor(() => expect(streamHandlers).not.toBeNull());

    await act(async () => {
      streamHandlers?.onMessage(event('main_agent.output_delta', { delta: '流式主代理回答', ordinal: 1 }));
    });
    const streamingBubble = (await screen.findByText('流式主代理回答')).closest('.message-assistant') as HTMLElement;
    expect(streamingBubble).not.toBeNull();
    expect(within(streamingBubble).queryByRole('button', { name: '复制' })).not.toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(event('task.completed'));
    });
    expect(await within(streamingBubble).findByRole('button', { name: '复制' })).toBeInTheDocument();
  });

  it('replaces the waiting-for-event hint with live task progress inside the assistant bubble', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalled());
    const submittingBubble = screen.getByText('提交中').closest('.message-assistant') as HTMLElement;
    expect(submittingBubble).not.toBeNull();
    expect(submittingBubble.querySelector('.ant-spin')).not.toBeNull();
    expect(screen.queryByText('等待任务事件...')).not.toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    expect((await screen.findByText('任务已提交')).closest('.message-assistant')).not.toBeNull();
    expect(screen.queryByText('等待任务事件...')).not.toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(event('task.graph_created', {}, 'graph-created'));
    });
    expect((await screen.findByText('正在规划并准备执行能力')).closest('.message-assistant')).not.toBeNull();
    expect(screen.queryByText('等待任务事件...')).not.toBeInTheDocument();
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

    vi.useFakeTimers();
    try {
      fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '你好' } });
      fireEvent.click(screen.getByRole('button', { name: '发送' }));

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      const noticeText = screen.getByText(/当前会话已有任务运行中/);
      expect(noticeText.closest('.toast-notice')).not.toBeNull();
      expect(noticeText.closest('.app-content')).toBeNull();

      await act(async () => {
        vi.advanceTimersByTime(5_000);
      });
      expect(screen.queryByText(/当前会话已有任务运行中/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps polling until graph has waiting_for_input and submits the next input as interrupt answer', async () => {
    const runningGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_data_query', capability_id: 'skill.data_query', status: 'running', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
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
          node_id: 'task-1:skill_data_query',
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
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [event('task.accepted', {}, 'accepted-before-interrupt')],
        [
          event('task.accepted', {}, 'accepted-after-interrupt'),
          event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', domain_kind: 'data_query', stage: 'execute_query' }, 'execute-after-interrupt', 'node-resumed-query'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

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
    const resumedProgress = await screen.findByText('data-query：正在检索数据');
    const resumedMessage = resumedProgress.closest('.message-assistant') as HTMLElement;
    expect(resumedMessage).not.toBeNull();
    expect(resumedProgress.closest('.skill-status-lines')).not.toBeNull();
    expect(resumedProgress.closest('.message-body')).toBeNull();
    expect(screen.queryByText('已收到补充信息，继续当前任务...')).not.toBeInTheDocument();
    expect(api.cancelTask).not.toHaveBeenCalled();
  });

  it('allows uploading a file while answering an artifact interrupt', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_field_design', capability_id: 'skill.field_design', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const api = makeApi({
      getTaskGraph: vi.fn(async () => waitingGraph),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'task-1:skill_field_design',
          question: '试验设计智能体 还缺少：试验材料 CSV/JSON 文件。请上传对应文件后继续。',
          reason_code: 'missing_material_data',
          required_fields: { material_data: { type: 'artifact', accepts_upload: true, description: '请上传试验材料文件。' } },
          status: 'open',
        }],
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [event('task.accepted', {}, 'accepted-before-artifact-interrupt')],
        [event('task.accepted', {}, 'accepted-after-artifact-interrupt')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '生成随机区组设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('region', { name: '需要补充信息' })).toBeInTheDocument();
    expect(screen.getByText('试验材料文件')).toBeInTheDocument();
    expect(screen.getByText('可直接上传文件，或回复文字后继续当前任务。')).toBeInTheDocument();
    const uploadInput = screen.getByLabelText('上传 JSON、CSV、Excel、图片或 PDF 文件') as HTMLInputElement;
    expect(uploadInput).not.toBeDisabled();
    const file = new File(['ped_id,hyb_check,set\nA01,0,S1\n'], 'materials.csv', { type: 'text/csv' });
    fireEvent.change(uploadInput, { target: { files: [file] } });

    await screen.findByText(/materials.csv/);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.answerInterrupt).toHaveBeenCalledWith('task-1', 'interrupt-1', {
      material_data: { text: '', upload_ids: ['upl-1'], filenames: ['materials.csv'] },
      upload_ids: ['upl-1'],
    }));
  });

  it('renders sheet selection interrupt and submits upload_sheet_selections only', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:sheet_selection', capability_id: 'main_agent.respond', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const api = makeApi({
      getTaskGraph: vi.fn(async () => waitingGraph),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'task-1:sheet_selection',
          question: '检测到多 sheet Excel，请选择。',
          reason_code: 'sheet_selection_required',
          required_fields: {
            upload_sheet_selections: {
              type: 'sheet_selection',
              required_upload_ids: ['upl-book'],
              options_by_upload_id: { 'upl-book': ['Alpha', 'Beta'] },
              labels_by_upload_id: { 'upl-book': 'materials.xlsx' },
            },
          },
          status: 'open',
        }],
      })),
      answerInterrupt: vi.fn(async () => ({
        interrupt_id: 'interrupt-1',
        status: 'answered',
        node_id: 'task-1:sheet_selection',
        answer_payload: { upload_sheet_selections: { 'upl-book': 'Beta' } },
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [event('task.accepted', {}, 'accepted-before-sheet-interrupt')],
        [event('task.accepted', {}, 'accepted-after-sheet-interrupt')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用多 sheet Excel 做设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('region', { name: '需要补充信息' })).toBeInTheDocument();
    expect(screen.getByText('Excel sheet 选择')).toBeInTheDocument();
    expect(screen.getByText('选择 Excel sheet')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('选择 materials.xlsx 的 sheet'), { target: { value: 'Beta' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.answerInterrupt).toHaveBeenCalledWith('task-1', 'interrupt-1', {
      upload_sheet_selections: { 'upl-book': 'Beta' },
    }));
  });

  it('does not allow upload-only answers for scalar interrupts', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_rice_genie', capability_id: 'skill.rice_genie', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const api = makeApi({
      getTaskGraph: vi.fn(async () => waitingGraph),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'task-1:skill_rice_genie',
          question: '请补充品种名称。',
          reason_code: 'missing_variety',
          required_fields: { variety: { type: 'string', description: '例如 龙粳31' } },
          status: 'open',
        }],
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [event('task.accepted', {}, 'accepted-before-scalar-interrupt')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查品种信息' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('region', { name: '需要补充信息' })).toBeInTheDocument();
    expect(screen.getByText('品种名称')).toBeInTheDocument();
    expect(screen.getByText('回复后将继续当前任务。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    expect(screen.getByRole('button', { name: '选择 JSON、CSV、图片或 PDF 文件' })).toBeDisabled();
    expect(screen.getByLabelText('上传 JSON、CSV、Excel、图片或 PDF 文件')).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    expect(api.answerInterrupt).not.toHaveBeenCalled();
  });

  it('keeps the final assistant answer visible with capability results after interrupt resume', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_data_query', capability_id: 'skill.data_query', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
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
          node_id: 'task-1:skill_data_query',
          question: '请选择查询范围。',
          reason_code: 'route_not_resolved',
          required_fields: { route_id: { options: ['dataset_a'] } },
          status: 'open',
        }],
      })),
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'main_agent_text:1', producer_node_id: 'task-1:main_agent.respond', artifact_type: 'text', storage_ref: '最终主代理回答：没有找到符合条件的记录。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'filtered_query_result:1', producer_node_id: 'task-1:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ columns: ['variety_name'], rows: [], row_count: 0, truncated: false }), summary: 'filtered', is_complete: true, created_at: null },
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
    expect(await screen.findByText('数据查询结果')).toBeInTheDocument();
    expect(screen.getByText('数据查询已完成，共返回 0 行结果。')).toBeInTheDocument();
  });

  it('keeps the task locked while waiting_for_input has no open interrupt yet', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_data_query', capability_id: 'skill.data_query', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
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
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    expect(api.answerInterrupt).not.toHaveBeenCalled();
    expect(api.submitMessage).toHaveBeenCalledTimes(1);
  });

  it('does not render the unfinished task list and still lets the user cancel the current task', async () => {
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} />);

    expect(screen.queryByText('未完成任务')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '展开未完成任务' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开输入功能菜单' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消任务' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(screen.queryByText('暂无未完成任务')).not.toBeInTheDocument();
  });

  it('replaces the send button with a stop button while a conversation task is active and cancels all unfinished tasks', async () => {
    const api = makeApi({
      listConversationTasks: vi.fn(async () => ({
        conversation_id: 'conv-test',
        tasks: [
          {
            task_id: 'task-1',
            conversation_id: 'conv-test',
            status: 'running',
            root_node_id: 'task-1:main',
            summary: '第一个任务',
            requested_capability_id: null,
            active_node_count: 1,
            completed_node_count: 0,
            failed_node_count: 0,
            cancel_requested: false,
            created_at: null,
            updated_at: null,
          },
          {
            task_id: 'task-2',
            conversation_id: 'conv-test',
            status: 'accepted',
            root_node_id: 'task-2:main',
            summary: '第二个任务',
            requested_capability_id: null,
            active_node_count: 0,
            completed_node_count: 0,
            failed_node_count: 0,
            cancel_requested: false,
            created_at: null,
            updated_at: null,
          },
        ],
      })),
      cancelTask: vi.fn(async (taskId) => ({ task_id: taskId, status: 'cancelling', accepted: true })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const stopButton = await screen.findByRole('button', { name: '停止' });
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
    fireEvent.click(stopButton);

    await waitFor(() => expect(api.listConversationTasks).toHaveBeenCalledWith(expect.any(String), 'unfinished'));
    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledTimes(2));
    expect(api.cancelTask).toHaveBeenNthCalledWith(1, 'task-1');
    expect(api.cancelTask).toHaveBeenNthCalledWith(2, 'task-2');
  });

  it('continues stopping remaining conversation tasks when one cancel request fails', async () => {
    const api = makeApi({
      listConversationTasks: vi.fn(async () => ({
        conversation_id: 'conv-test',
        tasks: [
          {
            task_id: 'task-1',
            conversation_id: 'conv-test',
            status: 'running',
            root_node_id: 'task-1:main',
            summary: '第一个任务',
            requested_capability_id: null,
            active_node_count: 1,
            completed_node_count: 0,
            failed_node_count: 0,
            cancel_requested: false,
            created_at: null,
            updated_at: null,
          },
          {
            task_id: 'task-2',
            conversation_id: 'conv-test',
            status: 'running',
            root_node_id: 'task-2:main',
            summary: '第二个任务',
            requested_capability_id: null,
            active_node_count: 1,
            completed_node_count: 0,
            failed_node_count: 0,
            cancel_requested: false,
            created_at: null,
            updated_at: null,
          },
        ],
      })),
      cancelTask: vi.fn(async (taskId) => {
        if (taskId === 'task-1') {
          throw new Error('cancel task-1 failed');
        }
        return { task_id: taskId, status: 'cancelling', accepted: true };
      }),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.accepted')])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledTimes(2));
    expect(api.cancelTask).toHaveBeenNthCalledWith(1, 'task-1');
    expect(api.cancelTask).toHaveBeenNthCalledWith(2, 'task-2');
    expect(await screen.findByText('请求未完成，请稍后重试。')).toBeInTheDocument();
  });

});
