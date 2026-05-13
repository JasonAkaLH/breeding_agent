import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import type { ApiClient } from './api/client';
import type { TaskEventEnvelope } from './api/types';
import type { EventSourceFactory, TaskEventHandlers } from './api/taskEvents';
import { WELCOME_PROMPTS } from './domain/welcomePrompts';

function makeApi(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    uiModes: [
      { key: 'chat', label: '普通对话', capabilityId: null },
    ],
    createCaptcha: vi.fn(async () => ({ captcha_id: 'cap-1', image_svg: '<svg><text>1234</text></svg>', expires_in_seconds: 300 })),
    login: vi.fn(async () => ({ user: { username: 'alice' } })),
    register: vi.fn(async () => ({ user: { username: 'charlie' } })),
    logout: vi.fn(async () => ({ logged_out: true })),
    me: vi.fn(async () => ({ user: { username: 'alice' } })),
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

function makeErrorEventSourceFactory(): EventSourceFactory {
  return (_url, handlers) => {
    handlers.onError(new Error('stream disconnected'));
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
    expect(screen.queryByText('user: alice')).not.toBeInTheDocument();
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
    expect(screen.queryByText('user: charlie')).not.toBeInTheDocument();
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
    expect(within(sendRow).queryByRole('button', { name: '选择 JSON 或 CSV 文件' })).not.toBeInTheDocument();
    expect(sendBar).toHaveClass('floating-composer');

    fireEvent.click(inputMenuButton);
    expect(await screen.findByRole('button', { name: '选择 JSON 或 CSV 文件' })).toBeInTheDocument();
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

  it('renders history entries as flat rows with hover-revealed actions', async () => {
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
          account_id: 'alice',
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

  it('shows deleted conversation feedback in a transient popup for five seconds', async () => {
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
      reasoningEffort: 'medium',
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
    const link = screen.getByRole('link', { name: /下\s*载/ });
    expect(link).toHaveAttribute('href', '/api/v1/artifacts/art-file-1/download');
  });

  it('shows the upstream capability currently being executed inside the assistant bubble', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('node.started', { capability_id: 'skill.data_query' }, 'sql-skill-started'),
      event('skill.progress', { capability_id: 'skill.data_query', domain_kind: 'data_query', stage: 'execute_query' }, 'sql-execute-progress'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询龙粳33' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const progressText = await screen.findByText('正在执行 Skill：正在检索数据');
    const assistantBubble = progressText.closest('.message-assistant') as HTMLElement;
    expect(assistantBubble).not.toBeNull();
    expect(assistantBubble.querySelector('.activity-notice')).not.toBeNull();
    expect(assistantBubble.querySelector('.ant-spin')).not.toBeNull();
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
          event('skill.progress', { capability_id: 'skill.data_query', domain_kind: 'data_query', stage: 'execute_query' }, 'execute-after-interrupt'),
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
    const resumedProgress = await screen.findByText('正在执行 Skill：正在检索数据');
    const resumedBubble = resumedProgress.closest('.message-assistant') as HTMLElement;
    expect(resumedBubble).not.toBeNull();
    expect(resumedBubble.querySelector('.activity-notice')).not.toBeNull();
    expect(resumedBubble.querySelector('.ant-spin')).not.toBeNull();
    expect(screen.queryByText('已收到补充信息，继续当前任务...')).not.toBeInTheDocument();
    expect(api.cancelTask).not.toHaveBeenCalled();
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
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
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

});
