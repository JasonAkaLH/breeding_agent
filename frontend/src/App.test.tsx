import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App, { mergeHistoryWithLiveFallbackNotices } from './App';
import type { ApiClient } from './api/client';
import type { ConversationMessagesResponse, DeleteConversationResponse, TaskEventEnvelope } from './api/types';
import type { EventSourceFactory, TaskEventHandlers } from './api/taskEvents';
import { COMPOSER_PLACEHOLDERS } from './domain/composerPlaceholders';
import { WELCOME_PROMPTS } from './domain/welcomePrompts';

vi.mock('./components/MathFormula', () => ({
  MathFormula: ({ language, source, display, fallbackSource }: {
    language: string;
    source: string;
    display: boolean;
    fallbackSource: string;
  }) => (
    <span
      data-testid="app-formula"
      data-language={language}
      data-display={String(display)}
      data-source={source}
    >
      {fallbackSource}
    </span>
  ),
}));

const deepseekReasoningEfforts = {
  options: [
    { value: 'minimal', label: '最低' },
    { value: 'low', label: '低' },
    { value: 'medium', label: '中' },
    { value: 'high', label: '高' },
    { value: 'xhigh', label: '更高' },
    { value: 'max', label: '最高' },
  ],
  thinking: {
    enabled: { default: 'high', supported: ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'] },
    disabled: { default: 'minimal', supported: ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'] },
  },
};

const doubaoReasoningEfforts = {
  options: [
    { value: 'minimal', label: '最低' },
    { value: 'low', label: '低' },
    { value: 'medium', label: '中' },
    { value: 'high', label: '高' },
  ],
  thinking: {
    enabled: { default: 'high', supported: ['minimal', 'low', 'medium', 'high'] },
    disabled: { default: 'minimal', supported: ['minimal'] },
  },
};

const forceThinkingReasoningEfforts = {
  options: [
    { value: 'high', label: '高' },
  ],
  thinking: {
    enabled: { default: 'high', supported: ['high'] },
    disabled: { default: null, supported: [] },
  },
};

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
      ],
    })),
    getModelEditions: vi.fn(async () => ({
      default_model_edition: 'deepseek-v4-flash-260425',
      options: [
        { value: 'deepseek-v4-flash-260425', label: 'DeepSeek V4 Flash', reasoning_efforts: deepseekReasoningEfforts },
        { value: 'deepseek-v4-pro-260425', label: 'DeepSeek V4 Pro', reasoning_efforts: deepseekReasoningEfforts },
      ],
    })),
    listConversationUploads: vi.fn(async () => ({ conversation_id: 'conv-test', uploads: [] })),
    deleteConversationUpload: vi.fn(async (conversationId, uploadId) => ({ upload_id: uploadId, deleted: true })),
    uploadConversationFile: vi.fn(async (_conversationId, file) => ({
      upload_id: 'upl-1',
      conversation_id: 'conv-test',
      filename: file.name,
      content_type: file.type || 'application/octet-stream',
      file_type: file.name.endsWith('.vcf') || file.name.endsWith('.vcf.gz')
        ? 'vcf'
        : file.name.endsWith('.json')
          ? 'json'
          : 'csv',
      size_bytes: file.size,
      sha256: 'hash',
      expires_at: '2026-05-07T10:00:00',
      preview: file.name.endsWith('.vcf') || file.name.endsWith('.vcf.gz')
        ? { row_count: null, columns: [], shape: 'binary' }
        : { row_count: 1, columns: ['ped_id', 'design_check'], shape: 'table' },
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
    listMCPServers: vi.fn(async () => ({ servers: [] })),
    createMCPServer: vi.fn(),
    getMCPServer: vi.fn(),
    patchMCPServer: vi.fn(),
    testMCPServer: vi.fn(),
    deleteMCPServer: vi.fn(),
    listMCPGrants: vi.fn(async () => ({ grants: [] })),
    deleteMCPGrant: vi.fn(async () => undefined),
    clearMCPServerGrants: vi.fn(async () => undefined),
    continueMCPCall: vi.fn(),
    cancelMCPCall: vi.fn(),
    getTask: vi.fn(),
    getTaskArtifacts: vi.fn(async () => ({ task_id: 'task-1', artifacts: [] })),
    downloadArtifact: vi.fn(async () => undefined),
    getTaskGraph: vi.fn(),
    ...overrides,
  };
}

async function renderAuthed(ui: ReactElement) {
  render(ui);
  await screen.findByText('育种助手');
}

async function openConversationFilesDrawer(expectedFileCount?: number) {
  if (typeof expectedFileCount === 'number') {
    await screen.findByRole('button', { name: new RegExp(`当前 ${expectedFileCount} 个已保存文件`) });
  }
  fireEvent.click(screen.getByRole('button', { name: /打开当前对话文件面板/ }));
}

function getConversationFilesDrawer() {
  return screen.getByRole('dialog', { name: '当前对话文件' });
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

function terminalProjectionEvents(): TaskEventEnvelope[] {
  const unknownId = 'mcp-execution-status-unknown:v1:call-1:4:01-unknown';
  const failedId = 'mcp-execution-status-unknown:v1:call-1:4:02-task-failed';
  const resolutionId = 'mcp-late-terminal:v1:call-1:1:01-resolution';
  return [
    { ...event('mcp.execution_status_unknown', {
      schema: 'maf.user_mcp.execution_status_unknown.v1', projection_id: 'mcp-terminal-projection:v1:call-1', intent_id: 'intent-1', call_id: 'call-1', task_id: 'task-1', node_id: 'node-1', projection_revision: 0, intent_revision: 4, unknown_terminal_at: '2026-04-27T00:00:00Z', reason_code: 'trusted_terminal_result_absent', no_replay: true, result_receipt_id: null, predecessor_event_id: null,
    }, unknownId, 'node-1'), created_at: '2026-04-27T00:00:00Z' },
    { ...event('task.failed', {
      schema: 'maf.user_mcp.unknown_task_failed.v1', projection_id: 'mcp-terminal-projection:v1:call-1', call_id: 'call-1', task_id: 'task-1', node_id: 'node-1', code: 'execution_status_unknown', no_replay: true, unknown_event_id: unknownId, predecessor_event_id: unknownId,
    }, failedId, 'node-1'), created_at: '2026-04-27T00:00:00.000001Z' },
    { ...event('mcp.execution_status_resolution', {
      schema: 'maf.user_mcp.execution_status_resolution.v1', projection_id: 'mcp-terminal-projection:v1:call-1', intent_id: 'intent-1', call_id: 'call-1', task_id: 'task-1', node_id: 'node-1', unknown_event_id: unknownId, task_failed_event_id: failedId, result_receipt_id: 'receipt-1', from_projection_revision: 0, to_projection_revision: 1, from_intent_revision: 4, to_intent_revision: 5, unknown_terminal_at: '2026-04-27T00:00:00Z', resolved_at: '2026-04-27T00:01:00Z', predecessor_event_id: failedId,
    }, resolutionId, 'node-1'), created_at: '2026-04-27T00:01:00Z' },
    { ...event('mcp.late_terminal_result_recovered', {
      schema: 'maf.user_mcp.late_terminal_result_recovered.v1', projection_id: 'mcp-terminal-projection:v1:call-1', intent_id: 'intent-1', call_id: 'call-1', task_id: 'task-1', node_id: 'node-1', unknown_event_id: unknownId, resolution_event_id: resolutionId, result_receipt_id: 'receipt-1', result_payload_sha256: 'sha256:result', projection_revision: 1, terminal_state: 'completed', safe_result_ref: 'artifact:safe-result', safe_result_ref_sha256: 'sha256:ref', safe_error_code: null, resolved_at: '2026-04-27T00:01:00Z', task_remains_failed: true, node_remains_failed: true, no_replay: true, predecessor_event_id: resolutionId,
    }, 'mcp-late-terminal:v1:call-1:1:02-correction', 'node-1'), created_at: '2026-04-27T00:01:00.000001Z' },
  ];
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

function makeEndingEventSourceFactory(events: TaskEventEnvelope[]) {
  let subscriptionCount = 0;
  const factory: EventSourceFactory = (_url, handlers) => {
    subscriptionCount += 1;
    queueMicrotask(() => {
      for (const item of events) handlers.onMessage(item);
      handlers.onError(new Error('replay ended'));
    });
    return { close: vi.fn() };
  };
  return { factory, subscriptionCount: () => subscriptionCount };
}

function makeErrorEventSourceFactory(): EventSourceFactory {
  return (_url, handlers) => {
    handlers.onError(new Error('stream disconnected'));
    return { close: vi.fn() };
  };
}

async function expectComposerFocused() {
  const input = screen.getByLabelText('请输入问题');
  await waitFor(() => expect(document.activeElement).toBe(input));
  return input;
}

describe('App', () => {
  it('merges live fallback notices only onto matching assistant task history', () => {
    const notice = {
      scope: 'full' as const,
      reasonCode: 'skill_missing',
      missingCapabilitySummary: '缺少绘图 Skill',
      fallbackContentScope: '只能给出手工建议',
    };

    const merged = mergeHistoryWithLiveFallbackNotices(
      [
        { id: 'assistant-other', kind: 'chat', role: 'assistant', content: '其它回答', mode: 'chat', taskId: 'task-other' },
        { id: 'assistant-target', kind: 'chat', role: 'assistant', content: '目标回答', mode: 'chat', taskId: 'task-target' },
      ],
      [
        { id: 'live', kind: 'chat', role: 'assistant', content: 'live', mode: 'chat', taskId: 'task-target', fallbackNotice: notice },
      ],
    );

    expect(merged[0].fallbackNotice).toBeUndefined();
    expect(merged[1].fallbackNotice).toEqual(notice);
  });

  it('canonically merges live and history MCP result artifact projections by call', () => {
    const deferred = {
      schema: 'maf.user_mcp.result_artifact_projection.v1' as const,
      safe_call_ref: 'a'.repeat(64),
      status: 'deferred' as const,
      reason_code: 'projection_failed' as const,
      artifact_count: 0 as const,
    };
    const ready = {
      ...deferred,
      status: 'ready' as const,
      reason_code: 'promoted' as const,
      artifact_count: 1 as const,
    };
    const permanent = {
      ...deferred,
      safe_call_ref: 'b'.repeat(64),
      status: 'permanent_failure' as const,
      reason_code: 'source_expired' as const,
    };

    const merged = mergeHistoryWithLiveFallbackNotices(
      [{
        id: 'history', kind: 'chat', role: 'assistant', content: 'done', mode: 'chat',
        taskId: 'task-target', mcpResultArtifactProjections: [ready],
      }],
      [{
        id: 'live', kind: 'chat', role: 'assistant', content: 'live', mode: 'chat',
        taskId: 'task-target', mcpResultArtifactProjections: [deferred, permanent],
      }],
    );

    expect(merged[0].mcpResultArtifactProjections).toEqual([ready, permanent]);
  });

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

    expect(await screen.findByText('登录育种助手')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    await waitFor(() => expect(api.login).toHaveBeenCalledWith({ username: 'alice' }));
    expect(localStorage.getItem('maf.frontend.access_token')).toBe('maf_tok_login');
    expect(await screen.findByText('育种助手')).toBeInTheDocument();
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
    expect(within(sidebar).getByText('SeedPilot')).toBeInTheDocument();
    expect(within(sidebar).queryByText('AI育种助手')).not.toBeInTheDocument();
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
    expect(await screen.findByRole('dialog', { name: 'MCP 服务与授权' })).toBeInTheDocument();
    expect(await screen.findByText('尚未配置 MCP 服务')).toBeInTheDocument();
    expect(api.listMCPServers).toHaveBeenCalledTimes(2);
    expect(api.listMCPGrants).toHaveBeenCalledTimes(1);
    expect(conversationList).toBeInTheDocument();
    expect(conversationList.parentElement).toHaveClass('app-content');
    expect(conversationList.closest('.conversation-card')).toBeNull();
    expect(screen.queryByText('主代理可用')).not.toBeInTheDocument();
    expect(screen.queryByText('数据查询可用')).not.toBeInTheDocument();
    expect(screen.queryByText(/user:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/conversation:/)).not.toBeInTheDocument();
    expect(COMPOSER_PLACEHOLDERS).toContain(within(sendRow).getByLabelText('请输入问题').getAttribute('placeholder'));
    const sendButton = within(sendRow).getByRole('button', { name: '发送' });
    const inputMenuButton = within(sendRow).getByRole('button', { name: '打开输入功能菜单' });
    expect(sendButton).toBeInTheDocument();
    expect(inputMenuButton).toBeInTheDocument();
    expect(sendButton.querySelector('img')).toHaveAttribute('src', '/pics/send-up-arrow-button.svg?v=20260511-arrow-balanced');
    expect(inputMenuButton.querySelector('img')).toHaveAttribute('src', '/pics/input-menu-plus-button.svg');
    expect(sendButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '发送');
    expect(inputMenuButton.closest('[data-tooltip]')).toHaveAttribute('data-tooltip', '打开输入功能菜单');
    expect(within(sendRow).queryByRole('button', { name: '选择 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件' })).not.toBeInTheDocument();
    expect(sendBar).toHaveClass('floating-composer');

    fireEvent.click(inputMenuButton);
    expect(await screen.findByRole('button', { name: '选择 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件' })).toBeInTheDocument();
    const uploadInput = screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件') as HTMLInputElement;
    expect(uploadInput.getAttribute('accept')).toContain('.tsv');
    expect(uploadInput.getAttribute('accept')).toContain('text/tab-separated-values');
    expect(uploadInput.getAttribute('accept')).toContain('.vcf');
    expect(uploadInput.getAttribute('accept')).toContain('.vcf.gz');
    await waitFor(() => expect(screen.getAllByLabelText('思考强度').length).toBeGreaterThan(0));
    expect(await screen.findByLabelText('深度思考')).toBeInTheDocument();
  });

  it('composer safe autofocus focuses the idle composer after workspace restore', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const input = await expectComposerFocused();
    expect(input).not.toBeDisabled();
  });

  it('composer safe autofocus waits for workspace restore before focusing an apparently idle composer', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const conversations = deferred<Awaited<ReturnType<ApiClient['listConversations']>>>();
    const api = makeApi({
      listConversations: vi.fn(() => conversations.promise),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '正在运行的问题', task_id: 'task-running', stream_status: null, created_at: null },
        ],
      })),
      getTask: vi.fn(async () => taskSummary('task-running', 'running')),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    await waitFor(() => expect(api.listConversations).toHaveBeenCalled());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    const input = screen.getByLabelText('请输入问题');
    expect(document.activeElement).not.toBe(input);

    conversations.resolve({
      conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-running', title: '历史问题', created_at: null, updated_at: null }],
    });
    expect(await screen.findByText('正在运行的问题')).toBeInTheDocument();
    await waitFor(() => expect(input).toBeDisabled());
  });

  it('shows stream interruption feedback as a five-second popup instead of an inline prompt box', async () => {
    const api = makeApi({
      getTask: vi.fn(async () => ({
        task_id: 'task-1',
        conversation_id: 'conv-test',
        status: 'failed',
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
    (document.activeElement as HTMLElement | null)?.blur();

    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith('conv-history'));
    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    const assistantAnswer = await screen.findByText('以前的回答');
    expect(assistantAnswer).toBeInTheDocument();
    const assistantBubble = assistantAnswer.closest('.message-assistant') as HTMLElement;
    expect(within(assistantBubble).getByText('SeedPilot')).toBeInTheDocument();
  });

  it('renders file_upload history as safe cards and hides internal system messages', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-history-files',
          username: 'alice',
          status: 'active',
          current_task_id: null,
          title: '文件历史',
          created_at: null,
          updated_at: null,
        }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-files',
        messages: [
          {
            message_id: 'file_upload:upl-pending',
            conversation_id: 'conv-history-files',
            role: 'system',
            content: 'MALICIOUS_RAW_CONTENT storage_key=/tmp/secret content_base64=abc',
            task_id: null,
            stream_status: 'complete',
            created_at: null,
            message_type: 'file_upload',
            metadata: {
              upload_id: 'upl-pending',
              filename: 'pending.csv',
              description_status: 'pending',
              file_status: 'active',
              storage_key: '/tmp/secret/pending.csv',
              content_base64: 'abc',
            },
          },
          {
            message_id: 'file_upload:upl-failed',
            conversation_id: 'conv-history-files',
            role: 'system',
            content: 'SHOULD_NOT_RENDER',
            task_id: null,
            stream_status: 'complete',
            created_at: null,
            message_type: 'file_upload',
            metadata: {
              upload_id: 'upl-failed',
              filename: 'failed.csv',
              description_status: 'failed',
              file_status: 'active',
            },
          },
          {
            message_id: 'file_upload:upl-deleted',
            conversation_id: 'conv-history-files',
            role: 'system',
            content: 'DELETED_RAW_CONTENT',
            task_id: null,
            stream_status: 'complete',
            created_at: null,
            message_type: 'file_upload',
            metadata: {
              upload_id: 'upl-deleted',
              filename: 'deleted.csv',
              description_status: 'ready',
              description_summary: 'deleted summary should not matter',
              file_status: 'deleted',
            },
          },
          {
            message_id: 'sys-internal',
            conversation_id: 'conv-history-files',
            role: 'system',
            content: 'INTERNAL_SECRET_MESSAGE',
            task_id: null,
            stream_status: 'complete',
            created_at: null,
            message_type: 'internal_note',
            metadata: {},
          },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '文件历史' }));

    const pendingCard = (await screen.findByText('pending.csv')).closest('.message-file-upload') as HTMLElement;
    expect(pendingCard).toHaveClass('message-user');
    expect(within(pendingCard).getByText('你')).toBeInTheDocument();
    expect(screen.getByLabelText('已上传文件 pending.csv')).toBeInTheDocument();
    expect(screen.getByText('failed.csv')).toBeInTheDocument();
    expect(screen.getByText('deleted.csv')).toBeInTheDocument();
    expect(screen.queryByText('文件摘要生成中')).not.toBeInTheDocument();
    expect(screen.queryByText('摘要不可用')).not.toBeInTheDocument();
    expect(screen.queryByText('文件已删除 / 不可再用于任务')).not.toBeInTheDocument();
    expect(screen.queryByText('upload_id: upl-pending')).not.toBeInTheDocument();
    expect(screen.queryByText(/MALICIOUS_RAW_CONTENT/)).not.toBeInTheDocument();
    expect(screen.queryByText(/SHOULD_NOT_RENDER/)).not.toBeInTheDocument();
    expect(screen.queryByText(/INTERNAL_SECRET_MESSAGE/)).not.toBeInTheDocument();
    expect(screen.queryByText(/storage_key/)).not.toBeInTheDocument();
    expect(screen.queryByText(/content_base64/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制' })).not.toBeInTheDocument();
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

  it('restores capability fallback notice from assistant history metadata', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-fallback-history',
          username: 'alice',
          status: 'active',
          current_task_id: null,
          title: '能力缺口历史',
          created_at: null,
          updated_at: null,
        }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-fallback-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-fallback-history', role: 'user', content: '生成田间图文件', task_id: 'task-fallback', stream_status: null, created_at: null },
          {
            message_id: 'msg-assistant',
            conversation_id: 'conv-fallback-history',
            role: 'assistant',
            content: '【能力缺口说明】缺少田间图 Skill。以下是手工建议。',
            task_id: 'task-fallback',
            stream_status: 'complete',
            created_at: null,
            metadata: {
              capability_missing_fallback: {
                enabled: true,
                scope: 'full',
                reason_code: 'skill_missing',
                missing_capability_summary: '缺少田间图 Skill',
                fallback_content_scope: '只能给出手工建议',
                artifact_generation_allowed: false,
                disclosure_required: true,
                handler: 'must-not-render',
              },
            },
          },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '能力缺口历史' }));

    expect(await screen.findByText('能力缺口')).toBeInTheDocument();
    expect(screen.getAllByText(/缺少田间图 Skill/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/只能给出手工建议/)).toBeInTheDocument();
    expect(screen.queryByText(/must-not-render/)).not.toBeInTheDocument();
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

  it('restores visible interrupt assistant messages from conversation history', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history-interrupt');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history-interrupt', username: 'alice', status: 'active', current_task_id: null, title: '补参历史', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-interrupt',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history-interrupt', role: 'user', content: '帮我设计 RCBD', task_id: 'task-history', stream_status: null, created_at: null },
          { message_id: 'msg-interrupt', conversation_id: 'conv-history-interrupt', role: 'assistant', content: '请提供试验的区组数（重复次数）。', task_id: 'task-history', stream_status: 'interrupt_visible', created_at: null },
          { message_id: 'msg-answer', conversation_id: 'conv-history-interrupt', role: 'user', content: '3次重复', task_id: 'task-history', stream_status: null, created_at: null },
          { message_id: 'msg-final', conversation_id: 'conv-history-interrupt', role: 'assistant', content: '已生成 RCBD 设计。', task_id: 'task-history', stream_status: 'complete', created_at: null },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(await screen.findByRole('button', { name: '补参历史' }));

    expect(await screen.findByText('请提供试验的区组数（重复次数）。')).toBeInTheDocument();
    expect(screen.getByText('已生成 RCBD 设计。')).toBeInTheDocument();
    expect(screen.queryByText(/等待补充 · 下一条消息将继续当前任务/)).not.toBeInTheDocument();
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
      event('node.started', { capability_id: 'agent.final_output' }, 'restore-node', 'node-main'),
      event('agent.reasoning_delta', { delta: '恢复分析', ordinal: 1, sample_id: 'sample-restore' }, 'restore-reasoning'),
    ].map((item) => ({ ...item, task_id: 'task-running' }));
    const eventSource = makeInspectableEventSourceFactory(events);

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('以前的问题')).toBeInTheDocument();
    expect(await screen.findByText('恢复分析')).toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).toBeDisabled();
    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
    expect(api.getTask).toHaveBeenCalledWith('task-running');
    expect(eventSource.urls).toEqual(['/api/v1/tasks/task-running/events']);
  });

  it('restores a failed MCP terminal projection and replays its late result without reopening the task', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-1', title: '迟到结果', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({ conversation_id: 'conv-history', messages: [] })),
      getTask: vi.fn(async () => ({
        ...taskSummary('task-1', 'failed'),
        mcp_terminal_projection: { projection_id: 'mcp-terminal-projection:v1:call-1' },
      })),
    });
    const eventSource = makeInspectableEventSourceFactory(terminalProjectionEvents());

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('已恢复可信迟到结果')).toBeInTheDocument();
    expect(screen.getAllByText(/任务仍因未知执行状态失败/)).toHaveLength(2);
    expect(screen.getByLabelText('请输入问题')).not.toBeDisabled();
    expect(eventSource.urls).toEqual(['/api/v1/tasks/task-1/events']);
  });

  it.each([
    ['correction only', [terminalProjectionEvents()[3]]],
    ['resolution and correction', terminalProjectionEvents().slice(2)],
  ])('shows a bounded recoverable sync error when terminal replay ends with a predecessor gap: %s', async (_name, replayEvents) => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{ conversation_id: 'conv-history', username: 'alice', status: 'active', current_task_id: 'task-1', title: '缺失前序', created_at: null, updated_at: null }],
      })),
      listConversationMessages: vi.fn(async () => ({ conversation_id: 'conv-history', messages: [] })),
      getTask: vi.fn(async () => ({
        ...taskSummary('task-1', 'failed'),
        mcp_terminal_projection: { projection_id: 'mcp-terminal-projection:v1:call-1' },
      })),
    });
    const eventSource = makeEndingEventSourceFactory(replayEvents);

    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('任务事件需要重新同步')).toBeInTheDocument();
    expect(screen.getByText(/缺少前序记录/)).toBeInTheDocument();
    expect(screen.queryByText('已恢复可信迟到结果')).not.toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1_100));
    });
    expect(eventSource.subscriptionCount()).toBe(1);
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
      { ...event('agent.reasoning_delta', { delta: '登录后恢复分析', ordinal: 1, sample_id: 'sample-login' }, 'restore-login-reasoning'), task_id: 'task-running' },
    ]);

    render(<App apiClient={api} eventSourceFactory={eventSource.factory} />);

    expect(await screen.findByText('登录育种助手')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));

    expect(await screen.findByText('登录前的问题')).toBeInTheDocument();
    expect(await screen.findByText('登录后恢复分析')).toBeInTheDocument();
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
      submitMessage: vi.fn(async () => ({ conversation_id: 'conv-history', message_id: 'msg-resume', task_id: 'task-running', status: 'accepted', action: 'interrupt_resumed', interrupt_id: 'interrupt-1' })),
    });

    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeInspectableEventSourceFactory([
        {
          ...event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'restored-waiting-event', 'node-wait'),
          task_id: 'task-running',
        },
      ]).factory}
      waitingInputCheckDelayMs={1}
    />);

    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
    expect(await screen.findByText(/等待补充/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: 'conv-history',
      content: '水稻',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1' },
    })));
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

  it('opens a fresh cancel event subscription even when the running stream is still open', async () => {
    const urls: string[] = [];
    const handlers: TaskEventHandlers[] = [];
    const closeFns: Array<ReturnType<typeof vi.fn>> = [];
    const eventSourceFactory: EventSourceFactory = (url, streamHandlers) => {
      urls.push(url);
      handlers.push(streamHandlers);
      const close = vi.fn();
      closeFns.push(close);
      return { close };
    };
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
      getTask: vi.fn(async () => taskSummary('task-1', 'cancelling')),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '停止订阅兜底测试' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalled());
    await waitFor(() => expect(urls).toEqual(['/api/v1/tasks/task-1/events']));

    fireEvent.click(screen.getByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    await waitFor(() => expect(urls).toEqual([
      '/api/v1/tasks/task-1/events',
      '/api/v1/tasks/task-1/events',
    ]));
    expect(closeFns[0]).toHaveBeenCalled();

    await act(async () => {
      handlers[1].onMessage(event('task.cancelled'));
    });

    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).not.toBeDisabled();
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

  it('composer safe autofocus waits during same-conversation history reload', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history');
    let streamHandlers: TaskEventHandlers | null = null;
    const reloadedConversations = deferred<Awaited<ReturnType<ApiClient['listConversations']>>>();
    const initialConversations = {
      conversations: [{
        conversation_id: 'conv-history',
        username: 'alice',
        status: 'active',
        current_task_id: null,
        title: '历史问题',
        created_at: null,
        updated_at: null,
      }],
    };
    const api = makeApi({
      listConversations: vi.fn()
        .mockResolvedValueOnce(initialConversations)
        .mockResolvedValueOnce(initialConversations)
        .mockReturnValueOnce(reloadedConversations.promise),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history', role: 'user', content: '以前的问题', task_id: 'task-history', stream_status: null, created_at: null },
          { message_id: 'msg-assistant', conversation_id: 'conv-history', role: 'assistant', content: '以前的回答', task_id: 'task-history', stream_status: 'complete', created_at: null },
        ],
      })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);
    await expectComposerFocused();

    const input = screen.getByLabelText('请输入问题');
    fireEvent.change(input, { target: { value: '完成后刷新同一会话' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('task.completed'));
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(api.getTaskArtifacts).toHaveBeenCalledWith('task-1'));
    await expectComposerFocused();

    input.blur();
    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));
    (document.activeElement as HTMLElement | null)?.blur();
    await waitFor(() => expect(api.listConversations).toHaveBeenCalledTimes(3));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(document.activeElement).not.toBe(input);
    reloadedConversations.resolve({
      conversations: [{
        conversation_id: 'conv-history',
        username: 'alice',
        status: 'active',
        current_task_id: null,
        title: '历史问题',
        created_at: null,
        updated_at: null,
      }],
    });
    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith('conv-history'));
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

  it('submits normal chat and renders transient Agent reasoning', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('agent.reasoning_delta', { delta: '先分析。', ordinal: 1, sample_id: 'sample-chat' }, 'reasoning-1'),
      event('task.completed'),
    ])} />);

    await waitFor(() => expect(api.getModelEditions).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      mode: 'chat',
      deepThinking: false,
      reasoningEffort: 'high',
    })));
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
  });

  it('renders formulas in user and transient Agent reasoning surfaces', async () => {
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    let streamHandlers: TaskEventHandlers | null = null;
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };

    try {
      await renderAuthed(<App apiClient={makeApi()} eventSourceFactory={eventSourceFactory} />);
      fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用户 $u$' } });
      fireEvent.click(screen.getByRole('button', { name: '发送' }));
      await waitFor(() => expect(streamHandlers).not.toBeNull());
      expect(await screen.findByTestId('app-formula')).toHaveAttribute('data-source', 'u');

      await act(async () => {
        streamHandlers?.onMessage(event('agent.reasoning_delta', { delta: '推理 $r$', ordinal: 1, sample_id: 'sample-formula' }, 'formula-reasoning'));
      });
      expect(screen.getAllByTestId('app-formula').map((formula) => formula.dataset.source)).toEqual(['u', 'r']);

      await act(async () => {
        streamHandlers?.onMessage(event('task.completed', {}, 'formula-complete'));
      });
      await waitFor(() => expect(screen.getAllByTestId('app-formula').map((formula) => formula.dataset.source)).toEqual(['u', 'r']));
    } finally {
      if (originalClipboard) {
        Object.defineProperty(navigator, 'clipboard', originalClipboard);
      } else {
        delete (navigator as { clipboard?: unknown }).clipboard;
      }
    }
  });

  it('composer safe autofocus focuses the composer after task completion', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    const input = screen.getByLabelText('请输入问题');
    fireEvent.change(input, { target: { value: '完成后继续输入' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('task.completed'));
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(api.getTaskArtifacts).toHaveBeenCalledWith('task-1'));
    await expectComposerFocused();
  });

  it('composer safe autofocus does not steal focus from a focused history control', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '不要抢历史焦点' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    const sidebar = screen.getByRole('complementary', { name: '历史会话侧边栏' });
    const refreshButton = within(sidebar).getByRole('button', { name: '刷新历史会话' });
    refreshButton.focus();
    expect(document.activeElement).toBe(refreshButton);
    await act(async () => {
      streamHandlers?.onMessage(event('task.completed'));
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(api.getTaskArtifacts).toHaveBeenCalledWith('task-1'));
    expect(document.activeElement).toBe(refreshButton);
  });

  it('composer safe autofocus focuses the composer after task cancellation reaches terminal state', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消后继续输入' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));
    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    await act(async () => {
      streamHandlers?.onMessage(event('task.cancelled'));
    });

    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
    await expectComposerFocused();
  });

  it('composer safe autofocus does not focus while cancellation is still pending', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
      getTask: vi.fn(async () => taskSummary('task-1', 'cancelling')),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消中不聚焦' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    const sidebar = screen.getByRole('complementary', { name: '历史会话侧边栏' });
    const refreshButton = within(sidebar).getByRole('button', { name: '刷新历史会话' });
    refreshButton.focus();
    fireEvent.click(screen.getByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(screen.getByText('正在停止当前对话任务')).toBeInTheDocument();
    expect(document.activeElement).toBe(refreshButton);
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

  it('keeps the task stream open after a node failure until a task failure event arrives', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const close = vi.fn();
    const api = makeApi();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '触发节点失败后重排' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());

    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('node.started', { capability_id: 'skill.data_lookup', skill_name: 'data-lookup' }, 'node-started', 'node-data'));
      streamHandlers?.onMessage(event('node.failed', { code: 'data_access_deadline_exceeded' }, 'node-failed', 'node-data'));
    });

    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
    expect(screen.queryByLabelText('任务失败')).not.toBeInTheDocument();
    expect(close).not.toHaveBeenCalled();

    await act(async () => {
      streamHandlers?.onMessage(event('task.failed', {}, 'task-failed'));
    });

    expect(await screen.findByLabelText('任务失败')).toBeInTheDocument();
    expect(close).toHaveBeenCalled();
  });

  it('waits for reducer consumption before applying terminal side effects to an out-of-order MCP chain', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const close = vi.fn();
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close };
    };
    await renderAuthed(<App apiClient={makeApi()} eventSourceFactory={eventSourceFactory} />);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '乱序终态' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    const [unknown, failed, resolution, correction] = terminalProjectionEvents();

    await act(async () => {
      streamHandlers?.onMessage(correction);
      streamHandlers?.onMessage(resolution);
      streamHandlers?.onMessage(failed);
    });
    expect(close).not.toHaveBeenCalled();
    expect(screen.queryByText('已恢复可信迟到结果')).not.toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(unknown);
    });
    expect(await screen.findByText('已恢复可信迟到结果')).toBeInTheDocument();
    expect(screen.getByLabelText('任务失败')).toBeInTheDocument();
    expect(close).not.toHaveBeenCalled();
  });

  it('composer safe autofocus focuses the composer when an interrupt prompt is ready', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      getTaskGraph: vi.fn(async () => ({
        task_id: 'task-1',
        nodes: [{ node_id: 'task-1:skill_data_query', capability_id: 'skill.data_query', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null }],
        edges: [],
      })),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'task-1:skill_data_query',
          question: '请补充作物类型。',
          reason_code: 'crop_not_resolved',
          required_fields: { crop: { options: ['rice'] } },
          status: 'open',
        }],
      })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} waitingInputCheckDelayMs={1} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询基因型' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-autofocus', 'task-1:skill_data_query'));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await screen.findByText('请补充作物类型。')).toBeInTheDocument();
    await expectComposerFocused();
  });

  it('composer safe autofocus waits until interrupt details are loaded before focusing', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const interruptResult = deferred<Awaited<ReturnType<ApiClient['listInterrupts']>>>();
    const api = makeApi({
      listInterrupts: vi.fn(() => interruptResult.promise),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} waitingInputCheckDelayMs={1} />);

    const sidebar = screen.getByRole('complementary', { name: '历史会话侧边栏' });
    const refreshButton = within(sidebar).getByRole('button', { name: '刷新历史会话' });
    refreshButton.focus();
    expect(document.activeElement).toBe(refreshButton);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '等待补充信息' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    refreshButton.focus();
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-details', 'task-1:skill_data_query'));
      await Promise.resolve();
    });

    expect(api.listInterrupts).toHaveBeenCalledWith('task-1');
    expect(document.activeElement).toBe(refreshButton);
    interruptResult.resolve({
      task_id: 'task-1',
      interrupts: [{
        interrupt_id: 'interrupt-1',
        conversation_id: 'conv-test',
        task_id: 'task-1',
        node_id: 'task-1:skill_data_query',
        question: '请补充作物类型。',
        reason_code: 'crop_not_resolved',
        required_fields: { crop: { options: ['rice'] } },
        status: 'open',
      }],
    });
    await screen.findByText('请补充作物类型。');
    expect(document.activeElement).toBe(refreshButton);
  });

  it('submits composer input within the 10000 character cap', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const input = screen.getByLabelText('请输入问题');
    const longPrompt = `请完整分析以下长文本：${'不要截断这段输入。'.repeat(800)}`;
    expect(input).toHaveAttribute('maxlength', '10000');
    expect(input).toHaveAttribute('wrap', 'soft');

    fireEvent.change(input, { target: { value: longPrompt } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: longPrompt,
    })));
  });

  it('opens the slash Skill picker and submits a required Skill call from the badge', async () => {
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
      capabilityId: 'skill.data_lookup',
      metadata: expect.objectContaining({
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
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

  it('submits direct slash command input as a required Skill call with cleaned content', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    const input = screen.getByLabelText('请输入问题');

    fireEvent.change(input, { target: { value: '/data-lookup 查询龙粳33' } });
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', charCode: 13 });

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '查询龙粳33',
      capabilityId: 'skill.data_lookup',
      metadata: expect.objectContaining({
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
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
      capabilityId: 'skill.data_lookup',
      metadata: expect.objectContaining({
        forced_by_slash_command: true,
        slash_command: '/data-lookup',
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

  it('submits uploaded files and required Skill metadata together', async () => {
    const api = makeApi({
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-test',
        messages: [{
          message_id: 'file_upload:upl-1',
          conversation_id: 'conv-test',
          role: 'system',
          content: 'raw uploaded content should not render',
          task_id: null,
          stream_status: 'complete',
          created_at: null,
          message_type: 'file_upload',
          metadata: {
            upload_id: 'upl-1',
            filename: 'materials.csv',
            description_status: 'ready',
            description_summary: 'Materials summary',
            file_status: 'active',
          },
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'materials.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });

    await screen.findByText(/materials.csv/);
    await screen.findByText(/待发送/);
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/materials.csv/)).not.toBeInTheDocument();
    expect(await screen.findByText(/当前还没有已保存文件/)).toBeInTheDocument();
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '/mini-breedstat-rcbd 用这个文件做3个区组RCBD' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '用这个文件做3个区组RCBD',
      capabilityId: 'skill.mini_breedstat_rcbd',
      metadata: expect.objectContaining({
        upload_ids: ['upl-1'],
        forced_by_slash_command: true,
        slash_command: '/mini-breedstat-rcbd',
      }),
    })));
    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith(expect.any(String)));
    expect(await screen.findByLabelText('已上传文件 materials.csv')).toBeInTheDocument();
    expect(screen.queryByText('Materials summary')).not.toBeInTheDocument();
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
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-test',
        messages: [{
          message_id: 'file_upload:upl-existing',
          conversation_id: 'conv-test',
          role: 'system',
          content: 'raw deleted content should not render',
          task_id: null,
          stream_status: 'complete',
          created_at: null,
          message_type: 'file_upload',
          metadata: {
            upload_id: 'upl-existing',
            filename: 'existing.csv',
            description_status: 'ready',
            file_status: 'deleted',
          },
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    await openConversationFilesDrawer(1);
    await screen.findByText(/existing.csv/);
    fireEvent.click(screen.getByRole('button', { name: '删除文件 existing.csv' }));

    await waitFor(() => expect(api.deleteConversationUpload).toHaveBeenCalledWith(expect.any(String), 'upl-existing'));
    await waitFor(() => expect(within(getConversationFilesDrawer()).queryByText(/existing.csv/)).not.toBeInTheDocument());
    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith(expect.any(String)));
    expect(await screen.findByLabelText('已上传文件 existing.csv')).toBeInTheDocument();
    expect(screen.queryByText('文件已删除 / 不可再用于任务')).not.toBeInTheDocument();
  });

  it('labels saved TSV uploads by filename while preserving csv file type', async () => {
    const api = makeApi({
      listConversationUploads: vi.fn(async () => ({
        conversation_id: 'conv-test',
        uploads: [{
          upload_id: 'upl-tsv',
          conversation_id: 'conv-test',
          filename: 'phenotype.tsv',
          content_type: 'text/tab-separated-values',
          file_type: 'csv',
          size_bytes: 42,
          sha256: 'hash-tsv',
          expires_at: '2026-05-07T10:00:00',
          preview: { row_count: 1, columns: ['FID', 'IID', 'RootAngle_deg'], shape: 'table' },
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    await openConversationFilesDrawer(1);
    const drawer = getConversationFilesDrawer();
    expect(await within(drawer).findByText(/phenotype.tsv/)).toBeInTheDocument();
    expect(within(drawer).getByText(/TSV · 1 行 · FID\/IID\/RootAngle_deg/)).toBeInTheDocument();
  });

  it('labels saved TSV uploads by MIME type when filename has no suffix', async () => {
    const api = makeApi({
      listConversationUploads: vi.fn(async () => ({
        conversation_id: 'conv-test',
        uploads: [{
          upload_id: 'upl-tsv-mime',
          conversation_id: 'conv-test',
          filename: 'phenotype',
          content_type: 'text/tsv',
          file_type: 'csv',
          size_bytes: 24,
          sha256: 'hash-tsv-mime',
          expires_at: '2026-05-07T10:00:00',
          preview: { row_count: 1, columns: ['FID', 'IID'], shape: 'table' },
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    await openConversationFilesDrawer(1);
    const drawer = getConversationFilesDrawer();
    expect(await within(drawer).findByText(/phenotype/)).toBeInTheDocument();
    expect(within(drawer).getByText(/TSV · 1 行 · FID\/IID/)).toBeInTheDocument();
  });

  it('deletes a draft attachment locally without calling backend delete', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'draft.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });

    await screen.findByText(/draft.csv/);
    await screen.findByText(/待发送/);
    fireEvent.click(screen.getByRole('button', { name: '删除文件 draft.csv' }));

    expect(api.deleteConversationUpload).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText(/draft.csv/)).not.toBeInTheDocument());
  });

  it('labels TSV draft attachments before uploading', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['FID\tIID\n0\tCML103\n'], 'phenotype.tsv', { type: 'text/tab-separated-values' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });

    expect(await screen.findByText(/phenotype.tsv/)).toBeInTheDocument();
    expect(await screen.findByText(/TSV ·/)).toBeInTheDocument();
    expect(await screen.findByText(/待发送/)).toBeInTheDocument();
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
  });

  it('labels TSV draft attachments by MIME type before uploading', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['FID\tIID\n0\tCML103\n'], 'phenotype', { type: 'text/tsv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });

    expect(await screen.findByText(/^phenotype$/)).toBeInTheDocument();
    expect(await screen.findByText(/TSV ·/)).toBeInTheDocument();
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
  });

  it('clears draft attachments when switching conversations without backend side effects', async () => {
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
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'switch-draft.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });
    await screen.findByText(/switch-draft.csv/);
    await screen.findByRole('button', { name: /当前 0 个已保存文件/ });

    fireEvent.click(await screen.findByRole('button', { name: '历史问题' }));

    await waitFor(() => expect(api.listConversationMessages).toHaveBeenCalledWith('conv-history'));
    await screen.findByRole('button', { name: /当前 0 个已保存文件/ });
    expect(screen.queryByText(/switch-draft.csv/)).not.toBeInTheDocument();
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    expect(api.deleteConversationUpload).not.toHaveBeenCalled();
  });

  it('keeps a CSV file as a draft and uploads it only when submitting the next message', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'materials.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });

    await screen.findByText(/materials.csv/);
    await screen.findByText(/待发送/);
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/materials.csv/)).not.toBeInTheDocument();
    expect(await screen.findByText(/当前还没有已保存文件/)).toBeInTheDocument();
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector('.ant-drawer-close') as HTMLElement);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用这个文件做3个区组RCBD' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalledWith(expect.any(String), file));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      metadata: { upload_ids: ['upl-1'] },
    })));
    await waitFor(() => expect(screen.queryByText(/待发送/)).not.toBeInTheDocument());
    await openConversationFilesDrawer(1);
    expect(await screen.findByText(/materials.csv/)).toBeInTheDocument();
    expect(await screen.findByText(/Skill 可用/)).toBeInTheDocument();
  });

  it('uploads a VCF.GZ file and submits its upload id with the next message', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    const file = new File(['vcf-bytes'], 'sample.vcf.gz', { type: 'application/gzip' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });

    await screen.findByText(/sample.vcf.gz/);
    await screen.findByText(/VCF/);
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/sample.vcf.gz/)).not.toBeInTheDocument();
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector('.ant-drawer-close') as HTMLElement);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '分析这个水稻 VCF' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalledWith(expect.any(String), file));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      metadata: { upload_ids: ['upl-1'] },
    })));
  });

  it('attaches a CSV file by drag and drop without uploading immediately', async () => {
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

    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    expect(await screen.findByText(/dragged.csv/)).toBeInTheDocument();
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/dragged.csv/)).not.toBeInTheDocument();
    expect(uploadDropZone).not.toHaveClass('chat-floating-stack-dragging');
  });

  it('does not create a chat bubble when draft upload fails before submit', async () => {
    const api = makeApi({
      uploadConversationFile: vi.fn(async () => {
        throw new Error('upload failed');
      }),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'broken.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用这个文件做分析' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalled());
    expect(api.submitMessage).not.toHaveBeenCalled();
    expect(document.querySelector('.message-user')).toBeNull();
    expect(screen.queryByText('提交中')).not.toBeInTheDocument();
    expect(await screen.findByText(/broken.csv/)).toBeInTheDocument();
    expect(await screen.findByText(/待重试/)).toBeInTheDocument();
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/broken.csv/)).not.toBeInTheDocument();
  });

  it('rolls back already uploaded draft files when a later draft upload fails', async () => {
    const firstUpload = {
      upload_id: 'upl-first',
      conversation_id: 'conv-test',
      filename: 'first.csv',
      content_type: 'text/csv',
      file_type: 'csv' as const,
      size_bytes: 24,
      sha256: 'hash-first',
      expires_at: '2026-05-07T10:00:00',
      preview: { row_count: 1, columns: ['ped_id'], shape: 'table' },
    };
    const api = makeApi({
      uploadConversationFile: vi.fn()
        .mockResolvedValueOnce(firstUpload)
        .mockRejectedValueOnce(new Error('second upload failed')),
      deleteConversationUpload: vi.fn(async (_conversationId, uploadId) => ({ upload_id: uploadId, deleted: true })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const firstFile = new File(['ped_id\nA\n'], 'first.csv', { type: 'text/csv' });
    const secondFile = new File(['ped_id\nB\n'], 'second.csv', { type: 'text/csv' });
    const uploadInput = screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件');
    fireEvent.change(uploadInput, { target: { files: [firstFile] } });
    fireEvent.change(uploadInput, { target: { files: [secondFile] } });
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '分析这两个文件' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalledTimes(2));
    expect(api.submitMessage).not.toHaveBeenCalled();
    await waitFor(() => expect(api.deleteConversationUpload).toHaveBeenCalledWith(expect.any(String), 'upl-first'));
    expect(await screen.findByText(/first.csv/)).toBeInTheDocument();
    expect(await screen.findByText(/second.csv/)).toBeInTheDocument();
    expect(await screen.findByText(/待重试/)).toBeInTheDocument();
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/first.csv/)).not.toBeInTheDocument();
    expect(within(getConversationFilesDrawer()).queryByText(/second.csv/)).not.toBeInTheDocument();
  });

  it('refreshes saved resources when submit fails and rollback delete fails', async () => {
    const residualUpload = {
      upload_id: 'upl-1',
      conversation_id: 'conv-test',
      filename: 'residual.csv',
      content_type: 'text/csv',
      file_type: 'csv' as const,
      size_bytes: 24,
      sha256: 'hash',
      expires_at: '2026-05-07T10:00:00',
      preview: { row_count: 1, columns: ['ped_id'], shape: 'table' },
    };
    const api = makeApi({
      listConversationUploads: vi.fn()
        .mockResolvedValueOnce({ conversation_id: 'conv-test', uploads: [] })
        .mockResolvedValue({ conversation_id: 'conv-test', uploads: [residualUpload] }),
      submitMessage: vi.fn(async () => {
        throw new Error('submit failed');
      }),
      deleteConversationUpload: vi.fn(async () => {
        throw new Error('delete failed');
      }),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const file = new File(['ped_id,design_check\nA,0\n'], 'residual.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用这个文件做分析' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.deleteConversationUpload).toHaveBeenCalledWith(expect.any(String), 'upl-1'));
    await waitFor(() => expect(api.listConversationUploads).toHaveBeenCalledTimes(2));
    await openConversationFilesDrawer(1);
    expect(await screen.findByText(/residual.csv/)).toBeInTheDocument();
    expect(await screen.findByText(/Skill 可用/)).toBeInTheDocument();
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
      reasoningEffort: 'high',
    })));
  });

  it('keeps current thinking and effort when starting a new conversation', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    fireEvent.click(await screen.findByLabelText('深度思考'));
    const effortSelect = screen.getAllByLabelText('思考强度')[0];
    fireEvent.mouseDown((effortSelect.closest('.ant-select') as HTMLElement).querySelector('.ant-select-selector') as HTMLElement);
    fireEvent.click(await screen.findByText('最高'));

    const previousConversationId = localStorage.getItem('maf.frontend.conversation_id.alice');
    expect(previousConversationId).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '新建对话' }));
    await waitFor(() => expect(localStorage.getItem('maf.frontend.conversation_id.alice')).not.toBe(previousConversationId));
    const nextConversationId = localStorage.getItem('maf.frontend.conversation_id.alice');

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '继承当前设置' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: nextConversationId,
      deepThinking: true,
      reasoningEffort: 'max',
    })));
  });


  it('allows reasoning effort selection while deep thinking is off', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    await waitFor(() => expect(screen.getAllByLabelText('思考强度').length).toBeGreaterThan(0));
    const effortSelect = screen.getAllByLabelText('思考强度')[0];
    expect(effortSelect).not.toHaveClass('ant-select-disabled');
    fireEvent.mouseDown((effortSelect.closest('.ant-select') as HTMLElement).querySelector('.ant-select-selector') as HTMLElement);
    fireEvent.click(await screen.findByText('最高'));

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '普通回答' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      deepThinking: false,
      reasoningEffort: 'max',
    })));
  });

  it('keeps a supported effort when deep thinking is turned off', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    const thinkingSwitch = await screen.findByLabelText('深度思考');
    fireEvent.click(thinkingSwitch);
    fireEvent.click(thinkingSwitch);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '关闭思考但保留高强度' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      deepThinking: false,
      reasoningEffort: 'high',
    })));
  });

  it('renders model-specific Doubao reasoning effort options', async () => {
    const api = makeApi({
      getModelEditions: vi.fn(async () => ({
        default_model_edition: 'doubao-seed-2-1-pro-260628',
        options: [
          { value: 'doubao-seed-2-1-pro-260628', label: '豆包Seed 2.1 Pro', reasoning_efforts: doubaoReasoningEfforts },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    fireEvent.click(await screen.findByLabelText('深度思考'));
    const effortSelect = screen.getAllByLabelText('思考强度')[0].closest('.ant-select') as HTMLElement;
    fireEvent.mouseDown(effortSelect.querySelector('.ant-select-selector') as HTMLElement);

    expect(await screen.findByText('低')).toBeInTheDocument();
    expect(await screen.findByText('中')).toBeInTheDocument();
    expect(screen.queryByText('最高')).not.toBeInTheDocument();
  });

  it('falls back to the Doubao disabled default and keeps the select enabled', async () => {
    const api = makeApi({
      getModelEditions: vi.fn(async () => ({
        default_model_edition: 'doubao-seed-2-1-pro-260628',
        options: [
          { value: 'doubao-seed-2-1-pro-260628', label: '豆包Seed 2.1 Pro', reasoning_efforts: doubaoReasoningEfforts },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    const thinkingSwitch = await screen.findByLabelText('深度思考');
    fireEvent.click(thinkingSwitch);
    const effortSelect = screen.getAllByLabelText('思考强度')[0];
    fireEvent.mouseDown((effortSelect.closest('.ant-select') as HTMLElement).querySelector('.ant-select-selector') as HTMLElement);
    fireEvent.click(await screen.findByText('高'));
    fireEvent.click(thinkingSwitch);

    expect(effortSelect).not.toHaveClass('ant-select-disabled');
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '豆包关闭思考' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      deepThinking: false,
      reasoningEffort: 'minimal',
    })));
  });

  it('falls back when switching to a model that does not support the current disabled effort', async () => {
    const api = makeApi({
      getModelEditions: vi.fn(async () => ({
        default_model_edition: 'deepseek-v4-flash-260425',
        options: [
          { value: 'deepseek-v4-flash-260425', label: 'DeepSeek V4 Flash', reasoning_efforts: deepseekReasoningEfforts },
          { value: 'doubao-seed-2-1-pro-260628', label: '豆包Seed 2.1 Pro', reasoning_efforts: doubaoReasoningEfforts },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    const effortSelect = screen.getAllByLabelText('思考强度')[0];
    fireEvent.mouseDown((effortSelect.closest('.ant-select') as HTMLElement).querySelector('.ant-select-selector') as HTMLElement);
    fireEvent.click(await screen.findByText('最高'));

    const modelSelect = screen.getAllByLabelText('模型版本')[0];
    fireEvent.mouseDown((modelSelect.closest('.ant-select') as HTMLElement).querySelector('.ant-select-selector') as HTMLElement);
    fireEvent.click(await screen.findByText('豆包Seed 2.1 Pro'));

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '切换模型' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      modelEdition: 'doubao-seed-2-1-pro-260628',
      deepThinking: false,
      reasoningEffort: 'minimal',
    })));
  });

  it('forces deep thinking when the selected model has no disabled-safe effort', async () => {
    const api = makeApi({
      getModelEditions: vi.fn(async () => ({
        default_model_edition: 'force-thinking-model',
        options: [
          { value: 'force-thinking-model', label: 'Force Thinking', reasoning_efforts: forceThinkingReasoningEfforts },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    expect(await screen.findByLabelText('深度思考：已开启')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '强制思考' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      deepThinking: true,
      reasoningEffort: 'high',
    })));
  });

  it('blocks submission when the model reasoning config is invalid', async () => {
    const api = makeApi({
      getModelEditions: vi.fn(async () => ({
        default_model_edition: 'invalid-model',
        options: [
          {
            value: 'invalid-model',
            label: 'Invalid Model',
            reasoning_efforts: {
              options: [],
              thinking: {
                enabled: { default: 'high', supported: ['high'] },
                disabled: { default: null, supported: [] },
              },
            },
          },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    expect(await screen.findByText('模型 reasoning_efforts 配置缺失或非法，请刷新或联系管理员。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '不应提交' } });

    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(api.submitMessage).not.toHaveBeenCalled();
  });

  it('shows a reasoning box placeholder when deep thinking is enabled but no reasoning content arrives', async () => {
    const api = makeApi();
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.completed'),
    ])} />);

    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    fireEvent.click(await screen.findByLabelText('深度思考'));
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '请深度思考' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByText('思考内容');
    expect(await screen.findByText(/等待模型返回 reasoning_content|本次模型未返回 reasoning_content/)).toBeInTheDocument();
  });



  it('restores data query artifact cards from conversation history message artifacts', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history-artifacts');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-history-artifacts', username: 'alice', status: 'active', current_task_id: null, title: '历史结果', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-artifacts',
        messages: [
          { message_id: 'msg-user', conversation_id: 'conv-history-artifacts', role: 'user', content: '查询隆平高科', task_id: 'task-history', stream_status: null, created_at: null },
          {
            message_id: 'msg-assistant',
            conversation_id: 'conv-history-artifacts',
            role: 'assistant',
            content: '最终回答文本',
            task_id: 'task-history',
            stream_status: 'complete',
            created_at: null,
            artifacts: [
              { artifact_id: 'filtered_query_result:history', producer_node_id: 'task-history:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ artifact_role: 'filtered_query_result', columns: ['品种名称'], rows: [{ 品种名称: '隆平381' }], row_count: 1, truncated: false }), summary: 'filtered', is_complete: true, created_at: null },
            ],
          },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('最终回答文本')).toBeInTheDocument();
    expect(await screen.findByText('数据查询结果')).toBeInTheDocument();
    expect(screen.getByText('数据查询已完成，共返回 1 行结果。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '展开原始表格' }));
    expect(await screen.findByText('隆平381')).toBeInTheDocument();
  });

  it('restores MCP raw-result artifact failure notices from assistant history', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history-mcp-artifact');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-history-mcp-artifact', username: 'alice', status: 'active', current_task_id: null, title: 'MCP 历史结果', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-mcp-artifact',
        messages: [
          {
            message_id: 'msg-assistant-mcp',
            conversation_id: 'conv-history-mcp-artifact',
            role: 'assistant',
            content: '工具调用已完成。',
            task_id: 'task-history-mcp',
            stream_status: 'complete',
            created_at: null,
            mcp_result_artifact_projections: [{
              schema: 'maf.user_mcp.result_artifact_projection.v1',
              safe_call_ref: 'a'.repeat(64),
              status: 'permanent_failure',
              reason_code: 'source_expired',
              artifact_count: 0,
            }],
          },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('工具调用已完成，但完整结果文件未能保留')).toBeInTheDocument();
    expect(screen.queryByText('source_expired')).not.toBeInTheDocument();
  });

  it('moves a live MCP raw-result artifact notice into the completed assistant message', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({ task_id: 'task-1', artifacts: [] })),
    });
    const projectionEvent = event(
      'mcp.result_artifact_projection',
      {
        schema: 'maf.user_mcp.result_artifact_projection.v1',
        safe_call_ref: 'a'.repeat(64),
        status: 'deferred',
        reason_code: 'projection_failed',
        artifact_count: 0,
      },
      'mcp-result-artifact-projection:v1:artifact-live:deferred:projection_failed',
      'node-mcp',
    );

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      projectionEvent,
      event('task.completed'),
    ])} />);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '运行 MCP 工具' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('工具调用已完成，完整结果文件正在生成，可稍后刷新')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole('region', { name: 'MCP 运行状态' })).not.toBeInTheDocument();
    });
    expect(screen.getAllByText('工具调用已完成，完整结果文件正在生成，可稍后刷新')).toHaveLength(1);
  });

  it('keeps restored task artifact cards after reloading conversation messages on completion', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-restored-artifacts');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-restored-artifacts', username: 'alice', status: 'active', current_task_id: 'task-1', title: '恢复中任务', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn()
        .mockResolvedValueOnce({
          conversation_id: 'conv-restored-artifacts',
          messages: [
            { message_id: 'msg-user', conversation_id: 'conv-restored-artifacts', role: 'user', content: '查询隆平高科', task_id: 'task-1', stream_status: null, created_at: null },
          ],
        })
        .mockResolvedValue({
          conversation_id: 'conv-restored-artifacts',
          messages: [
            { message_id: 'msg-user', conversation_id: 'conv-restored-artifacts', role: 'user', content: '查询隆平高科', task_id: 'task-1', stream_status: null, created_at: null },
            {
              message_id: 'task-1:assistant',
              conversation_id: 'conv-restored-artifacts',
              role: 'assistant',
              content: '历史最终回答',
              task_id: 'task-1',
              stream_status: 'complete',
              created_at: null,
              artifacts: [
                { artifact_id: 'filtered_query_result:restored-history', producer_node_id: 'task-1:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ artifact_role: 'filtered_query_result', columns: ['品种名称'], rows: [{ 品种名称: '隆平381' }], row_count: 1, truncated: false }), summary: 'filtered', is_complete: true, created_at: null },
              ],
            },
          ],
        }),
      getTask: vi.fn(async () => taskSummary('task-1', 'running')),
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'agent-artifact:task-1:final', producer_node_id: 'agent-node:task-1:final', artifact_type: 'text', storage_ref: '实时最终回答', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'filtered_query_result:live', producer_node_id: 'task-1:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ artifact_role: 'filtered_query_result', columns: ['品种名称'], rows: [{ 品种名称: '隆平381' }], row_count: 1, truncated: false }), summary: 'filtered', is_complete: true, created_at: null },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    await waitFor(() => expect(api.getTaskArtifacts).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('历史最终回答')).toBeInTheDocument();
    expect(await screen.findByText('数据查询结果')).toBeInTheDocument();
    expect(screen.getByText('数据查询已完成，共返回 1 行结果。')).toBeInTheDocument();
  });

  it('restores downloadable file artifact cards from conversation history', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history-file');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-history-file', username: 'alice', status: 'active', current_task_id: null, title: '历史文件', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-file',
        messages: [
          {
            message_id: 'task-file:assistant',
            conversation_id: 'conv-history-file',
            role: 'assistant',
            content: '已生成文件。',
            task_id: 'task-file',
            stream_status: 'complete',
            created_at: null,
            artifacts: [
              { artifact_id: 'art-file-history', producer_node_id: 'task-file:agent.final_output', artifact_type: 'file', storage_ref: '', summary: 'HTML 布局', is_complete: true, created_at: null, filename: 'layout.html', mime_type: 'text/html', size_bytes: 12, download_url: '/api/v1/artifacts/art-file-history/download', source_file_count: 1, archive_format: null, retention_status: 'active' },
            ],
          },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('layout.html')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /下\s*载/ }));
    expect(api.downloadArtifact).toHaveBeenCalledWith('art-file-history', 'layout.html');
  });

  it('restores OCR raw text artifact cards from conversation history', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history-ocr');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-history-ocr', username: 'alice', status: 'active', current_task_id: null, title: '历史 OCR', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-ocr',
        messages: [
          {
            message_id: 'task-ocr:assistant',
            conversation_id: 'conv-history-ocr',
            role: 'assistant',
            content: '图片识别完成。',
            task_id: 'task-ocr',
            stream_status: 'complete',
            created_at: null,
            artifacts: [
              {
                artifact_id: 'task-ocr:ocr_raw_text',
                producer_node_id: 'task-ocr:ocr:skill_execute',
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
          },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    const summary = await screen.findByText('图片识别完成。');
    const assistantMessage = summary.closest('.message-assistant') as HTMLElement;
    expect(assistantMessage).not.toBeNull();
    const messageBody = assistantMessage.querySelector('.message-body') as HTMLElement;
    expect(within(messageBody).getByText('OCR 回传原文：scan.png')).toBeInTheDocument();
    expect(messageBody.querySelector('.ocr-raw-text-content')?.textContent).toBe('品种：龙粳33\n处理：A1');
  });

  it('does not render internal query artifacts from conversation history', async () => {
    localStorage.setItem('maf.frontend.conversation_id.alice', 'conv-history-internal');
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [
          { conversation_id: 'conv-history-internal', username: 'alice', status: 'active', current_task_id: null, title: '内部产物', created_at: null, updated_at: null },
        ],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-history-internal',
        messages: [
          {
            message_id: 'task-internal:assistant',
            conversation_id: 'conv-history-internal',
            role: 'assistant',
            content: '只有文本回答',
            task_id: 'task-internal',
            stream_status: 'complete',
            created_at: null,
            artifacts: [
              { artifact_id: 'generated_sql:1', producer_node_id: 'task-internal:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ artifact_role: 'generated_sql', sql: 'SELECT secret' }), summary: 'generated SQL', is_complete: true, created_at: null },
              { artifact_id: 'guard_report:1', producer_node_id: 'task-internal:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ artifact_role: 'guard_report', guard_report: { status: 'passed' } }), summary: 'guard', is_complete: true, created_at: null },
            ],
          },
        ],
      })),
    });

    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);

    expect(await screen.findByText('只有文本回答')).toBeInTheDocument();
    expect(screen.queryByText('数据查询结果')).not.toBeInTheDocument();
    expect(screen.queryByText(/SELECT secret/)).not.toBeInTheDocument();
  });

  it('submits database questions through automatic planning without manual mode selection', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'agent-artifact:task-1:final', producer_node_id: 'agent.final_output', artifact_type: 'text', storage_ref: '主代理已自动调用数据查询能力，龙粳33共 1 行。', summary: 'final', is_complete: true, created_at: null },
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
          { artifact_id: 'agent-artifact:task-1:final', producer_node_id: 'agent-node:task-1:final', artifact_type: 'text', storage_ref: '已生成文件。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'art-file-1', producer_node_id: 'agent-node:task-1:final', artifact_type: 'file', storage_ref: '', summary: 'HTML 布局', is_complete: true, created_at: null, filename: 'layout.html', mime_type: 'text/html', size_bytes: 12, download_url: '/api/v1/artifacts/art-file-1/download', source_file_count: 1, archive_format: null, retention_status: 'active' },
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

  it('renders only the typed MCP business result with accessible expansion and no raw download', async () => {
    const rawResult = '{"authorization":"raw-secret"}';
    const businessText = `业务返回-${'结果'.repeat(180)}`;
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          {
            artifact_id: 'opaque-result-artifact',
            producer_node_id: 'task-1:mcp-tool',
            artifact_type: 'mcp_result',
            storage_ref: rawResult,
            summary: 'MCP 工具结果',
            is_complete: true,
            created_at: null,
            mcp_business_result: {
              schema: 'maf.mcp.business_result_view.v1',
              availability: 'ready',
              outcome: 'succeeded',
              primary: { kind: 'text', text: businessText, truncated: true },
              content_metadata: [{ kind: 'image', mime_type: 'image/png', byte_size: 24, sha256: `sha256:${'a'.repeat(64)}` }],
              projection_truncated: true,
            },
          },
        ],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '调用 MCP' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const title = await screen.findByText('MCP 工具结果');
    const messageBody = title.closest('.message-assistant')?.querySelector('.message-body') as HTMLElement;
    expect(messageBody.querySelector('.markdown-content')).not.toBeInTheDocument();
    expect(messageBody.querySelector('.mcp-business-result-content')?.textContent).toBe(businessText);
    const expand = within(messageBody).getByRole('button', { name: '展开业务结果' });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    expect(expand).toHaveAttribute('aria-controls', expect.stringContaining('mcp-business-result-'));
    fireEvent.click(expand);
    expect(expand).toHaveAttribute('aria-expanded', 'true');
    expect(within(messageBody).getByText('图片 · image/png · 24 bytes')).toBeInTheDocument();
    expect(within(messageBody).getByText('结果已按安全展示预算截断。')).toBeInTheDocument();
    expect(messageBody).not.toHaveTextContent('raw-secret');
    expect(within(messageBody).queryByRole('button', { name: /下\s*载/ })).not.toBeInTheDocument();
    expect(api.downloadArtifact).not.toHaveBeenCalled();
  });

  it('renders OCR raw text artifacts as a collapsible card inside the assistant bubble', async () => {
    const api = makeApi({
      getTaskArtifacts: vi.fn(async () => ({
        task_id: 'task-1',
        artifacts: [
          { artifact_id: 'agent-artifact:task-1:final', producer_node_id: 'agent-node:task-1:final', artifact_type: 'text', storage_ref: '主代理总结：图片中包含品种和处理信息。', summary: 'final', is_complete: true, created_at: null },
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

  it('renders multiple skill status lines and completes them with the Agent task', async () => {
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
      streamHandlers?.onMessage(event('task.completed', {}, 'task-completed'));
    });

    expect(await screen.findByText('data-query：已完成')).toBeInTheDocument();
    expect(await screen.findByText('RCBD：已完成')).toBeInTheDocument();
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

  it('loads the interrupt prompt from node.waiting_for_input SSE and submits the next input as interrupt answer', async () => {
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
          question: '请补充要查询的作物类型 $q$。',
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
        [
          event('task.accepted', {}, 'accepted-before-interrupt'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-interrupt', 'task-1:skill_data_query'),
        ],
        [
          event('task.accepted', {}, 'accepted-after-interrupt'),
          event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', domain_kind: 'data_query', stage: 'execute_query' }, 'execute-after-interrupt', 'node-resumed-query'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询基因型' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.listInterrupts).toHaveBeenCalledWith('task-1'));
    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    const composer = screen.getByRole('region', { name: '悬浮发送栏' });
    expect(within(composer).getByText(/下一条消息将继续当前任务/)).toBeInTheDocument();
    expect(document.querySelector('.interrupt-input-banner')).not.toBeInTheDocument();
    expect(document.querySelector('.interrupt-composer-status')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('请补充要查询的作物类型 $q$。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '结束任务' })).toBeInTheDocument();
    expect(await screen.findByText(/请补充要查询的作物类型/)).toBeInTheDocument();
    expect(screen.getByTestId('app-formula')).toHaveAttribute('data-source', 'q');
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '水稻',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1' },
    })));
    const resumedProgress = await screen.findByText('data-query：正在检索数据');
    const resumedMessage = resumedProgress.closest('.message-assistant') as HTMLElement;
    expect(resumedMessage).not.toBeNull();
    expect(resumedProgress.closest('.skill-status-lines')).not.toBeNull();
    expect(resumedProgress.closest('.message-body')).toBeNull();
    expect(screen.queryByText('已收到补充信息，继续当前任务...')).not.toBeInTheDocument();
    expect(api.cancelTask).not.toHaveBeenCalled();
  });

  it('presents Agent multi-waiting interrupts one at a time and keeps the task waiting', async () => {
    const firstInterrupt = {
      interrupt_id: 'interrupt-agent-1',
      conversation_id: 'conv-test',
      task_id: 'task-1',
      node_id: 'node-agent-1',
      question: '请先补充作物类型。',
      reason_code: 'crop_not_resolved',
      required_fields: { crop: { options: ['rice', 'corn'] } },
      status: 'open',
    };
    const secondInterrupt = {
      ...firstInterrupt,
      interrupt_id: 'interrupt-agent-2',
      node_id: 'node-agent-2',
      question: '请继续补充试验地点。',
      reason_code: 'location_not_resolved',
      required_fields: { location: { type: 'string' } },
    };
    const listInterrupts = vi.fn()
      .mockResolvedValueOnce({ task_id: 'task-1', interrupts: [firstInterrupt, secondInterrupt] })
      .mockResolvedValue({ task_id: 'task-1', interrupts: [secondInterrupt] });
    const api = makeApi({
      listInterrupts,
      getTaskGraph: vi.fn(async () => ({ task_id: 'task-1', nodes: [], edges: [] })),
      getTask: vi.fn(async () => ({
        task_id: 'task-1',
        conversation_id: 'conv-test',
        status: 'running',
        root_node_id: null,
        active_node_count: 2,
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
        [
          event('task.graph_created', { node_count: 0, edge_count: 0, root_node_id: null }, 'agent-graph-created'),
          event('agent.run.waiting', {
            interrupt_id: 'interrupt-agent-1', reason_kind: 'skill_input', remaining_count: 2,
          }, 'agent-waiting-1', 'node-agent-1'),
        ],
        [
          event('agent.run.resumed', { outcome: 'resumed', remaining_count: 1 }, 'agent-resumed-1', 'node-agent-1'),
          event('agent.run.waiting', {
            interrupt_id: 'interrupt-agent-2', reason_kind: 'skill_input', remaining_count: 1,
          }, 'agent-waiting-2', 'node-agent-2'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '开始多补参任务' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByPlaceholderText('请先补充作物类型。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenLastCalledWith(expect.objectContaining({
      content: '水稻',
      metadata: { interrupt_id: 'interrupt-agent-1' },
    })));
    expect(await screen.findByPlaceholderText('请继续补充试验地点。')).toBeInTheDocument();
    expect(screen.getByText(/下一条消息将继续当前任务/)).toBeInTheDocument();
    expect(screen.queryByText('任务已完成')).not.toBeInTheDocument();
    await expectComposerFocused();
  });

  it('keeps an upload-accepting interrupt open when draft upload fails', async () => {
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
      uploadConversationFile: vi.fn(async () => {
        throw new Error('interrupt upload failed');
      }),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-artifact-interrupt'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-artifact-interrupt', 'task-1:skill_field_design'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '生成随机区组设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText(/试验设计智能体 还缺少/)).toBeInTheDocument();
    vi.mocked(api.submitMessage).mockClear();
    const file = new File(['ped_id,hyb_check,set\nA01,0,S1\n'], 'interrupt-broken.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.uploadConversationFile).toHaveBeenCalled());
    expect(api.submitMessage).not.toHaveBeenCalled();
    expect(screen.getByText(/试验设计智能体 还缺少/)).toBeInTheDocument();
    expect(screen.queryByText('（已上传文件）')).not.toBeInTheDocument();
    expect(screen.queryByText('补充信息已提交，正在继续任务')).not.toBeInTheDocument();
    expect(await screen.findByText(/interrupt-broken.csv/)).toBeInTheDocument();
    expect(await screen.findByText(/待重试/)).toBeInTheDocument();
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/interrupt-broken.csv/)).not.toBeInTheDocument();
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
        [
          event('task.accepted', {}, 'accepted-before-artifact-interrupt'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-artifact-interrupt', 'task-1:skill_field_design'),
        ],
        [event('task.accepted', {}, 'accepted-after-artifact-interrupt')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '生成随机区组设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(await screen.findByText(/试验设计智能体 还缺少/)).toBeInTheDocument();
    const uploadInput = screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件') as HTMLInputElement;
    expect(uploadInput).not.toBeDisabled();
    const file = new File(['ped_id,hyb_check,set\nA01,0,S1\n'], 'materials.csv', { type: 'text/csv' });
    fireEvent.change(uploadInput, { target: { files: [file] } });

    await screen.findByText(/materials.csv/);
    await screen.findByText(/待发送/);
    await openConversationFilesDrawer(0);
    expect(within(getConversationFilesDrawer()).queryByText(/materials.csv/)).not.toBeInTheDocument();
    fireEvent.click(document.querySelector('.ant-drawer-close') as HTMLElement);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1', upload_ids: ['upl-1'] },
    })));
  });

  it('renders file-selection ambiguity as natural-language interrupt with replacement upload enabled', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:file_selection', capability_id: 'agent.final_output', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
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
          node_id: 'task-1:file_selection',
          question: '我找到了多个可能的数据文件：\n\n1. materials.csv（upl-a1b2，120 行）\n2. materials.csv（upl-c3d4，2000 行）\n\n你可以直接回复 upload_id，或说“用 120 行那个”。',
          reason_code: 'file_selection_ambiguous',
          required_fields: {
            _file_selection: {
              version: 1,
              type: 'conversation_file_selection',
              presentation: 'natural_language',
              allow_multiple: false,
            },
            file_selection_answer: {
              type: 'text',
              description: '请说明要使用哪个文件。',
            },
            replacement_file: {
              type: 'artifact',
              required: false,
              accepts_upload: true,
              description: '如候选文件都不合适，可上传新文件并说明“用这个”。',
            },
          },
          status: 'open',
        }],
      })),
      submitMessage: vi.fn(async () => ({ conversation_id: 'conv-test', message_id: 'msg-file-select', task_id: 'task-1', status: 'accepted', action: 'interrupt_resumed', interrupt_id: 'interrupt-1' })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-file-selection'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-file-selection', 'task-1:file_selection'),
        ],
        [event('task.accepted', {}, 'accepted-after-file-selection')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '继续用刚才的数据' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(await screen.findByText(/我找到了多个可能的数据文件/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/你可以直接回复 upload_id/)).toBeInTheDocument();
    const uploadInput = screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件') as HTMLInputElement;
    expect(uploadInput).not.toBeDisabled();

    const file = new File(['ped_id,hyb_check,set\nA01,0,S1\n'], 'replacement.csv', { type: 'text/csv' });
    fireEvent.change(uploadInput, { target: { files: [file] } });
    await screen.findByText(/replacement.csv/);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用这个' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '用这个',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1', upload_ids: ['upl-1'] },
    })));
  });

  it('renders sheet selection interrupt and submits upload_sheet_selections only', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:sheet_selection', capability_id: 'agent.final_output', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
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
      submitMessage: vi.fn(async () => ({ conversation_id: 'conv-test', message_id: 'msg-sheet', task_id: 'task-1', status: 'accepted', action: 'interrupt_resumed', interrupt_id: 'interrupt-1' })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-sheet-interrupt'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-sheet-interrupt', 'task-1:sheet_selection'),
        ],
        [event('task.accepted', {}, 'accepted-after-sheet-interrupt')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '用多 sheet Excel 做设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(await screen.findByText('检测到多 sheet Excel，请选择。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: 'Beta' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: 'Beta',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1', upload_sheet_selections: { 'upl-book': 'Beta' } },
    })));
  });

  it('submits v2 slot interrupt answers as raw DTOs without business parameter parsing', async () => {
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
          question: '对角线增广设计还差列数。请回复列数，例如 12 列。',
          reason_code: 'missing_v2_slot_input',
          required_fields: {
            _slot_collection_ref: {
              schema_version: 2,
              collection_id: 'slot-1',
              task_id: 'task-1',
              node_id: 'task-1:skill_field_design',
              kind: 'input_collection',
              status: 'waiting_for_user',
              round: 1,
              revision: 0,
              selected_schema_id: 'diagonal',
              selected_entrypoint: 'run',
              missing: ['ncols'],
              invalid: [],
              last_question: '对角线增广设计还差列数。请回复列数，例如 12 列。',
              slots: [
                { name: 'ncols', label: '列数', type: 'integer', status: 'missing', required_now: true },
              ],
            },
          },
          status: 'open',
        }],
      })),
      submitMessage: vi.fn(async () => ({ conversation_id: 'conv-test', message_id: 'msg-v2', task_id: 'task-1', status: 'accepted', action: 'interrupt_resumed', interrupt_id: 'interrupt-1' })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-v2-slot-interrupt'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-v2-slot-interrupt', 'task-1:skill_field_design'),
        ],
        [event('task.accepted', {}, 'accepted-after-v2-slot-interrupt')],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '做对角线增广设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('对角线增广设计还差列数。请回复列数，例如 12 列。')).toBeInTheDocument();
    expect(screen.queryByText('_slot_collection_ref')).not.toBeInTheDocument();
    expect(screen.getByText(/等待补充 · 下一条消息将继续当前任务/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '12列' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '12列',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1' },
    })));
    const payload = vi.mocked(api.submitMessage).mock.calls.at(-1)?.[0];
    expect(payload).not.toHaveProperty('design');
    expect(payload).not.toHaveProperty('ncols');
    expect(payload?.metadata).not.toHaveProperty('design');
    expect(payload?.metadata).not.toHaveProperty('ncols');
    expect(screen.getByText('12列')).toBeInTheDocument();
    expect(screen.queryByText('design=对角线增广')).not.toBeInTheDocument();
  });

  it('keeps a v2 interrupt open when the answer API returns a clarification response', async () => {
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
          question: '对角线增广设计还差列数。请回复列数，例如 12 列。',
          reason_code: 'missing_v2_slot_input',
          required_fields: {
            _slot_collection_ref: {
              schema_version: 2,
              collection_id: 'slot-1',
              task_id: 'task-1',
              node_id: 'task-1:skill_field_design',
              kind: 'input_collection',
              status: 'waiting_for_user',
              round: 1,
              revision: 0,
              selected_schema_id: 'diagonal',
              selected_entrypoint: 'run',
              missing: ['ncols'],
              invalid: [],
              last_question: '对角线增广设计还差列数。请回复列数，例如 12 列。',
              slots: [
                { name: 'ncols', label: '列数', type: 'integer', status: 'missing', required_now: true },
              ],
            },
          },
          status: 'open',
        }],
      })),
      submitMessage: vi.fn(async () => ({
        conversation_id: 'conv-test',
        message_id: 'msg-clarify',
        task_id: 'task-1',
        status: 'accepted',
        action: 'interrupt_clarification_answer',
        interrupt_id: 'interrupt-1',
        assistant_message: '列数表示田块布局的总列数，例如 12 列。当前任务仍在等待你的正式答案。',
        answer_payload: { client_request_id: 'interrupt-answer-clarify' },
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-v2-clarification'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-v2-clarification', 'task-1:skill_field_design'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '做对角线增广设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('对角线增广设计还差列数。请回复列数，例如 12 列。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '这个列数应该填什么格式？' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '这个列数应该填什么格式？',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1' },
    })));
    expect(await screen.findByText('列数表示田块布局的总列数，例如 12 列。当前任务仍在等待你的正式答案。')).toBeInTheDocument();
    expect(screen.getByText(/等待补充 · 下一条消息将继续当前任务/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '12列' } });
    expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled();
    expect(api.getTaskArtifacts).not.toHaveBeenCalled();
  });


  it('keeps a v2 interrupt open for mixed/schema-switch processed responses when resume is false', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_field_design', capability_id: 'skill.field_design', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
      ],
      edges: [],
    };
    const initialInterrupt = {
      interrupt_id: 'interrupt-1',
      conversation_id: 'conv-test',
      task_id: 'task-1',
      node_id: 'task-1:skill_field_design',
      question: '请补充：列数。',
      reason_code: 'missing_v2_slot_input',
      required_fields: {
        _slot_collection_ref: {
          schema_version: 2,
          collection_id: 'slot-1',
          task_id: 'task-1',
          node_id: 'task-1:skill_field_design',
          kind: 'input_collection',
          status: 'waiting_for_user',
          round: 1,
          revision: 0,
          selected_schema_id: 'diagonal',
          selected_entrypoint: 'run',
          missing: ['ncols'],
          invalid: [],
          last_question: '请补充：列数。',
          slots: [{ name: 'ncols', label: '列数', type: 'integer', status: 'missing', required_now: true }],
        },
      },
      status: 'open',
    };
    const refreshedInterrupt = {
      ...initialInterrupt,
      question: '已记录 12 列。还需要补充：材料数据。',
      required_fields: {
        _slot_collection_ref: {
          ...initialInterrupt.required_fields._slot_collection_ref,
          revision: 1,
          missing: ['material_data'],
          last_question: '已记录 12 列。还需要补充：材料数据。',
          slots: [
            { name: 'ncols', label: '列数', type: 'integer', status: 'resolved', required_now: false },
            { name: 'material_data', label: '材料数据', type: 'file', status: 'missing', required_now: true },
          ],
        },
      },
    };
    const listInterrupts = vi.fn()
      .mockResolvedValueOnce({ task_id: 'task-1', interrupts: [initialInterrupt] })
      .mockResolvedValue({ task_id: 'task-1', interrupts: [refreshedInterrupt] });
    const api = makeApi({
      getTaskGraph: vi.fn(async () => waitingGraph),
      listInterrupts,
      submitMessage: vi.fn(async () => ({
        conversation_id: 'conv-test',
        message_id: 'msg-mixed',
        task_id: 'task-1',
        status: 'accepted',
        action: 'interrupt_mixed_processed',
        interrupt_id: 'interrupt-1',
        assistant_message: '列数是田块布局的总列数；当前 interrupt 继续等待。',
        answer_payload: { client_request_id: 'user-mixed', will_resume: false, requires_confirmation: false },
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-v2-mixed'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-v2-mixed', 'task-1:skill_field_design'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '做对角线增广设计' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('请补充：列数。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '12列，列数是什么意思？' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('列数是田块布局的总列数；当前 interrupt 继续等待。')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText('请输入问题')).toHaveAttribute('placeholder', '已记录 12 列。还需要补充：材料数据。'));
    expect(screen.getByText(/等待补充 · 下一条消息将继续当前任务/)).toBeInTheDocument();
    expect(listInterrupts).toHaveBeenCalledTimes(2);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '13列' } });
    expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled();
    expect(api.getTaskArtifacts).not.toHaveBeenCalled();
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
          question: '水稻品种分析还差品种名称。例如可以回复：龙粳31。',
          reason_code: 'missing_variety',
          required_fields: {
            variety: { type: 'string', description: '例如 龙粳31' },
            _slot_collection: {
              schema_version: 1,
              collection_id: 'slot-1',
              round: 1,
              missing: ['variety'],
              slots: [{ name: 'variety', type: 'string', status: 'missing', label: '品种名称' }],
            },
          },
          status: 'open',
        }],
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted', {}, 'accepted-before-scalar-interrupt'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-scalar-interrupt', 'task-1:skill_rice_genie'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查品种信息' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('水稻品种分析还差品种名称。例如可以回复：龙粳31。')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(screen.queryByText('需要补充：')).not.toBeInTheDocument();
    expect(screen.queryByText('_slot_collection')).not.toBeInTheDocument();
    expect(screen.getByText(/等待补充 · 下一条消息将继续当前任务/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开输入功能菜单' }));
    expect(screen.getByRole('button', { name: '选择 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件' })).toBeDisabled();
    expect(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件')).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(api.uploadConversationFile).not.toHaveBeenCalled();
    expect(api.submitMessage).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '龙粳31' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(api.submitMessage).toHaveBeenLastCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '龙粳31',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1' },
    })));
  });

  it('keeps the final assistant answer visible with capability results after interrupt resume', async () => {
    const waitingGraph = {
      task_id: 'task-1',
      nodes: [
        { node_id: 'task-1:skill_data_query', capability_id: 'skill.data_query', status: 'waiting_for_input', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
        { node_id: 'agent-node:task-1:final', capability_id: 'agent.final_output', status: 'pending', criticality: 'required', dependency_type: 'hard', assigned_instance_id: null, started_at: null, finished_at: null },
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
          { artifact_id: 'agent-artifact:task-1:final', producer_node_id: 'agent-node:task-1:final', artifact_type: 'text', storage_ref: '最终主代理回答：没有找到符合条件的记录。', summary: 'final', is_complete: true, created_at: null },
          { artifact_id: 'filtered_query_result:1', producer_node_id: 'task-1:skill_data_query', artifact_type: 'json', storage_ref: JSON.stringify({ columns: ['variety_name'], rows: [], row_count: 0, truncated: false }), summary: 'filtered', is_complete: true, created_at: null },
        ],
      })),
    });
    await renderAuthed(<App
      apiClient={api}
      eventSourceFactory={makeSequencedEventSourceFactory([
        [
          event('task.accepted'),
          event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-final-answer-interrupt', 'task-1:skill_data_query'),
        ],
        [
          event('task.completed', {}, 'task-completed-resumed'),
        ],
      ])}
      waitingInputCheckDelayMs={1}
    />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询适合宁夏种植的棉花' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(await screen.findByText('请选择查询范围。')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '审定品种库' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      conversationId: expect.stringMatching(/^conv-/),
      content: '审定品种库',
      mode: 'chat',
      clientMessageId: expect.stringMatching(/^user-/),
      metadata: { interrupt_id: 'interrupt-1' },
    })));
    await waitFor(() => expect(api.getTaskArtifacts).toHaveBeenCalled());
    expect(await screen.findByText(/最终主代理回答/)).toBeInTheDocument();
    expect(await screen.findByText('数据查询结果')).toBeInTheDocument();
    expect(screen.getByText('数据查询已完成，共返回 0 行结果。')).toBeInTheDocument();
  });

  it('keeps graph-only waiting_for_input locked without calling interrupts before SSE', async () => {
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

    await waitFor(() => expect(api.getTaskGraph).toHaveBeenCalled());
    expect(api.listInterrupts).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: '需要补充信息' })).not.toBeInTheDocument();
    expect(screen.queryByText('正在等待任务给出补充信息')).not.toBeInTheDocument();
    expect(screen.getByLabelText('请输入问题')).toBeDisabled();
    expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '停止' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '水稻' } });
    expect(api.submitMessage).toHaveBeenCalledTimes(1);
  });

  it('reconnects the task event stream after a transient SSE error while the task is still active', async () => {
    const subscriptions: TaskEventHandlers[] = [];
    const api = makeApi({
      getTask: vi.fn(async () => taskSummary('task-1', 'running')),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      subscriptions.push(handlers);
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '需要重连' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));

    await act(async () => {
      subscriptions[0].onError(new Error('stream dropped'));
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(api.getTask).toHaveBeenCalledWith('task-1'));

    await waitFor(() => expect(subscriptions).toHaveLength(2), { timeout: 2_000 });
    await act(async () => {
      subscriptions[1].onMessage(event('agent.reasoning_delta', { delta: '重连后的分析', ordinal: 1, sample_id: 'sample-reconnect' }, 'reasoning-after-reconnect'));
    });
    expect(await screen.findByText('重连后的分析')).toBeInTheDocument();
  });

  it('keeps reconnecting the task event stream when status recovery is temporarily unavailable', async () => {
    const subscriptions: TaskEventHandlers[] = [];
    const api = makeApi({
      getTask: vi.fn(async () => {
        throw new Error('status endpoint unavailable');
      }),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      subscriptions.push(handlers);
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '状态接口短暂失败也要重连' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));

    await act(async () => {
      subscriptions[0].onError(new Error('stream dropped'));
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(api.getTask).toHaveBeenCalledWith('task-1'));
    await waitFor(() => expect(subscriptions).toHaveLength(2), { timeout: 2_000 });
    await act(async () => {
      subscriptions[1].onMessage(event('agent.reasoning_delta', { delta: '状态接口恢复前的分析', ordinal: 1, sample_id: 'sample-status-recovery' }, 'reasoning-after-status-failure'));
    });
    expect(await screen.findByText('状态接口恢复前的分析')).toBeInTheDocument();
  });

  it('resubscribes after MCP approval and surfaces the next Tool approval', async () => {
    const firstInterrupt = {
      interrupt_id: 'mcp-approval-capabilities',
      conversation_id: 'conv-test',
      task_id: 'task-1',
      node_id: 'node-mcp',
      question: 'Allow OCR服务 to call get_ocr_capabilities?',
      reason_code: 'mcp_tool_approval_required',
      required_fields: {
        mcp_tool_approval: { options: ['allow_once', 'always_allow', 'deny'] },
      },
      status: 'open',
    };
    const secondInterrupt = {
      ...firstInterrupt,
      interrupt_id: 'mcp-approval-start-job',
      question: 'Allow OCR服务 to call start_parse_job?',
    };
    const api = makeApi({
      listInterrupts: vi.fn()
        .mockResolvedValueOnce({ task_id: 'task-1', interrupts: [firstInterrupt] })
        .mockResolvedValue({ task_id: 'task-1', interrupts: [secondInterrupt] }),
    });
    const batches = [
      [
        event('task.accepted', {}, 'mcp-task-accepted'),
        event('mcp.tool_approval_required', {
          interrupt_id: firstInterrupt.interrupt_id,
          safe_call_ref: 'safe-call-capabilities',
          server_display_name: 'OCR服务',
          tool_display_name: 'get_ocr_capabilities',
        }, 'mcp-approval-capabilities-event', 'node-mcp'),
        event('node.waiting_for_input', {
          interrupt_id: firstInterrupt.interrupt_id,
        }, 'mcp-waiting-capabilities-event', 'node-mcp'),
      ],
      [
        event('mcp.tool_approval_required', {
          interrupt_id: secondInterrupt.interrupt_id,
          safe_call_ref: 'safe-call-start-job',
          server_display_name: 'OCR服务',
          tool_display_name: 'start_parse_job',
        }, 'mcp-approval-start-job-event', 'node-mcp'),
        event('node.waiting_for_input', {
          interrupt_id: secondInterrupt.interrupt_id,
        }, 'mcp-waiting-start-job-event', 'node-mcp'),
      ],
    ];
    const subscriptions: TaskEventHandlers[] = [];
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      const batch = batches[subscriptions.length] ?? [];
      subscriptions.push(handlers);
      queueMicrotask(() => {
        for (const item of batch) handlers.onMessage(item);
      });
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '识别图片' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const firstApprovalDialog = await screen.findByRole('dialog', { name: 'MCP 工具授权' });
    expect(within(firstApprovalDialog).getByText(/get_ocr_capabilities/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '始终允许' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledTimes(2));
    expect(api.submitMessage).toHaveBeenLastCalledWith(expect.objectContaining({
      metadata: {
        interrupt_id: firstInterrupt.interrupt_id,
        mcp_tool_approval: 'always_allow',
      },
    }));
    await waitFor(() => expect(subscriptions).toHaveLength(2));
    const secondApprovalDialog = await screen.findByRole('dialog', { name: 'MCP 工具授权' });
    expect(within(secondApprovalDialog).getByText(/start_parse_job/)).toBeInTheDocument();
    await waitFor(() => expect(api.listInterrupts).toHaveBeenCalledTimes(2));
  });

  it('keeps MCP approval pending when approval submission fails', async () => {
    const api = makeApi({
      submitMessage: vi.fn()
        .mockResolvedValueOnce({ conversation_id: 'conv-test', message_id: 'msg-1', task_id: 'task-1', status: 'accepted' })
        .mockRejectedValueOnce(new Error('approval failed')),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'mcp-approval-1',
          conversation_id: 'conv-test',
          task_id: 'task-1',
          node_id: 'node-mcp',
          question: 'Allow OCR服务 to call start_parse_job?',
          reason_code: 'mcp_tool_approval_required',
          required_fields: {
            mcp_tool_approval: { options: ['allow_once', 'always_allow', 'deny'] },
          },
          status: 'open',
        }],
      })),
    });
    const subscriptions: TaskEventHandlers[] = [];
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      subscriptions.push(handlers);
      if (subscriptions.length === 1) {
        queueMicrotask(() => {
          handlers.onMessage(event('mcp.tool_approval_required', {
            interrupt_id: 'mcp-approval-1',
            safe_call_ref: 'safe-call-1',
            server_display_name: 'OCR服务',
            tool_display_name: 'start_parse_job',
          }, 'mcp-approval-1-event', 'node-mcp'));
          handlers.onMessage(event('node.waiting_for_input', {
            interrupt_id: 'mcp-approval-1',
          }, 'mcp-waiting-1-event', 'node-mcp'));
        });
      }
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '识别图片' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    const approvalDialog = await screen.findByRole('dialog', { name: 'MCP 工具授权' });
    expect(within(approvalDialog).getByText(/start_parse_job/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '仅允许一次' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('dialog', { name: 'MCP 工具授权' })).toBeInTheDocument();
    expect(subscriptions).toHaveLength(1);
  });

  it('retries loading open interrupts after a waiting-input event when the interrupt list lags', async () => {
    const api = makeApi({
      listInterrupts: vi.fn()
        .mockResolvedValueOnce({ task_id: 'task-1', interrupts: [] })
        .mockResolvedValueOnce({
          task_id: 'task-1',
          interrupts: [{
            interrupt_id: 'interrupt-1',
            task_id: 'task-1',
            node_id: 'task-1:skill_data_query',
            question: '请补充作物类型',
            required_fields: { crop: { options: ['rice'] } },
            status: 'open',
            created_at: null,
            answered_at: null,
          }],
        }),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([
      event('task.accepted'),
      event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-retry', 'task-1:skill_data_query'),
    ])} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '查询基因型' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.listInterrupts).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
  });

  it('does not let a stale waiting-input retry overwrite a terminal task event', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      listInterrupts: vi.fn(async () => ({ task_id: 'task-1', interrupts: [] })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '等待事件后马上失败' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());

    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-terminal', 'task-1:skill_data_query'));
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(api.listInterrupts).toHaveBeenCalledTimes(1));

    await act(async () => {
      streamHandlers?.onMessage(event('task.failed', {}, 'task-failed-before-waiting-retry'));
    });
    expect(await screen.findByLabelText('任务失败')).toBeInTheDocument();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1_800));
    });
    expect(api.listInterrupts).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('任务失败')).toBeInTheDocument();
    expect(screen.queryByText('正在等待任务给出补充信息')).not.toBeInTheDocument();
  });

  it('does not let a pending waiting-input retry overwrite a cancelling task', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      listInterrupts: vi.fn(async () => ({ task_id: 'task-1', interrupts: [] })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '等待 retry 后收到取消请求' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());

    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
      streamHandlers?.onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-cancelling', 'task-1:skill_data_query'));
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(api.listInterrupts).toHaveBeenCalledTimes(1));

    await act(async () => {
      streamHandlers?.onMessage(event('task.cancellation_requested', { status: 'cancelling' }, 'task-cancelling-before-retry'));
    });
    expect(screen.getByText('正在取消当前任务')).toBeInTheDocument();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1_800));
    });
    expect(api.listInterrupts).toHaveBeenCalledTimes(1);
    expect(screen.getByText('正在取消当前任务')).toBeInTheDocument();
    expect(screen.queryByText('正在等待任务给出补充信息')).not.toBeInTheDocument();
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

  it('waits for the task.cancelled event before marking a cancel request terminal', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消前保持事件驱动' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(screen.getByText('正在停止当前对话任务')).toBeInTheDocument();
    expect(screen.queryByText('任务已取消')).not.toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(event('task.cancelled', {}, 'task-cancelled'));
    });

    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
  });

  it('renders a cancelled task bubble with a red cancelled icon instead of a spinner', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消后显示终态图标' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));
    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));

    await act(async () => {
      streamHandlers?.onMessage(event('task.cancelled', {}, 'task-cancelled-icon'));
    });

    const cancelledText = await screen.findByText('任务已取消');
    const notice = cancelledText.closest('.activity-notice') as HTMLElement;
    expect(notice).not.toBeNull();
    expect(notice).toHaveClass('activity-notice-failed');
    expect(within(notice).getByLabelText('任务已取消')).toBeInTheDocument();
    expect(notice.querySelector('.ant-spin')).toBeNull();
    expect(screen.queryByText('正在停止当前对话任务')).not.toBeInTheDocument();
  });

  it('settles a cancel request from a terminal cancel response without waiting for SSE', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelled', accepted: true })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消接口直接返回终态' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument();
    expect(screen.queryByText('正在停止当前对话任务')).not.toBeInTheDocument();
  });

  it('reconciles a cancelling task from getTask when terminal SSE is missed', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
      getTaskGraph: vi.fn(async () => { throw new Error('graph unavailable'); }),
      getTask: vi.fn()
        .mockResolvedValueOnce({ ...taskSummary('task-1', 'running'), conversation_id: 'conv-test' })
        .mockResolvedValue({ ...taskSummary('task-1', 'cancelled'), conversation_id: 'conv-test' }),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消后事件丢失' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
    await waitFor(() => expect(api.getTask).toHaveBeenCalledWith('task-1'));
    expect(screen.queryByText('正在停止当前对话任务')).not.toBeInTheDocument();
  });

  it('resubscribes after cancelling a waiting-input interrupt and waits for task.cancelled', async () => {
    const subscriptions: TaskEventHandlers[] = [];
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          task_id: 'task-1',
          node_id: 'task-1:skill_data_query',
          question: '请补充作物类型',
          required_fields: { crop: { options: ['rice'] } },
          status: 'open',
          created_at: null,
          answered_at: null,
        }],
      })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      subscriptions.push(handlers);
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '等待时取消任务' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));
    await act(async () => {
      subscriptions[0].onMessage(event('task.accepted'));
      subscriptions[0].onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-cancel', 'task-1:skill_data_query'));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '结束任务' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    await waitFor(() => expect(subscriptions).toHaveLength(2));
    expect(screen.getByText('正在停止当前对话任务')).toBeInTheDocument();
    expect(screen.queryByText('任务已取消')).not.toBeInTheDocument();

    await act(async () => {
      subscriptions[1].onMessage(event('task.cancelled', {}, 'task-cancelled-after-waiting-cancel'));
    });

    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
  });

  it('reconciles a waiting-input cancel from getTask when terminal SSE is missed', async () => {
    const subscriptions: TaskEventHandlers[] = [];
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
      getTaskGraph: vi.fn(async () => { throw new Error('graph unavailable'); }),
      getTask: vi.fn()
        .mockResolvedValueOnce({ ...taskSummary('task-1', 'running'), conversation_id: 'conv-test' })
        .mockResolvedValue({ ...taskSummary('task-1', 'cancelled'), conversation_id: 'conv-test' }),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          task_id: 'task-1',
          node_id: 'task-1:skill_data_query',
          question: '请补充作物类型',
          required_fields: { crop: { options: ['rice'] } },
          status: 'open',
          created_at: null,
          answered_at: null,
        }],
      })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      subscriptions.push(handlers);
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '等待输入时取消且事件丢失' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));
    await act(async () => {
      subscriptions[0].onMessage(event('task.accepted'));
      subscriptions[0].onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-cancel-fallback', 'task-1:skill_data_query'));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '结束任务' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
    expect(screen.queryByText('请补充作物类型')).not.toBeInTheDocument();
    expect(screen.queryByText('正在等待任务给出补充信息')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument();
  });

  it('restores the waiting-input prompt when cancelling from an interrupt fails', async () => {
    const subscriptions: TaskEventHandlers[] = [];
    const api = makeApi({
      cancelTask: vi.fn(async () => { throw new Error('cancel unavailable'); }),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-1',
          task_id: 'task-1',
          node_id: 'task-1:skill_data_query',
          question: '请补充作物类型',
          required_fields: { crop: { options: ['rice'] } },
          status: 'open',
          created_at: null,
          answered_at: null,
        }],
      })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      subscriptions.push(handlers);
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '等待输入时取消失败' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));
    await act(async () => {
      subscriptions[0].onMessage(event('task.accepted'));
      subscriptions[0].onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-1' }, 'waiting-before-cancel-failure', 'task-1:skill_data_query'));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '结束任务' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('请补充作物类型')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '结束任务' })).toBeInTheDocument();
    expect(screen.queryByText('任务已取消')).not.toBeInTheDocument();
  });

  it('does not let stale non-terminal events override a terminal cancel state', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelled', accepted: true })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消后收到陈旧事件' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));
    expect(await screen.findByText('任务已取消')).toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(event('task.cancellation_requested', { status: 'cancelling' }, 'stale-cancel-request'));
      streamHandlers?.onMessage(event('node.started', { capability_id: 'agent.final_output' }, 'stale-node-started', 'n1'));
      streamHandlers?.onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-stale' }, 'stale-waiting', 'n1'));
    });

    expect(screen.getByText('任务已取消')).toBeInTheDocument();
    expect(screen.queryByText('正在停止当前对话任务')).not.toBeInTheDocument();
    expect(api.listInterrupts).not.toHaveBeenCalled();
  });

  it('ignores stale non-terminal events while cancelling so polling can settle the terminal state', async () => {
    let streamHandlers: TaskEventHandlers | null = null;
    const terminalStatus = deferred<ReturnType<typeof taskSummary>>();
    const api = makeApi({
      cancelTask: vi.fn(async () => ({ task_id: 'task-1', status: 'cancelling', accepted: true })),
      getTaskGraph: vi.fn(async () => { throw new Error('graph unavailable'); }),
      getTask: vi.fn(async () => ({ ...await terminalStatus.promise, conversation_id: 'conv-test' })),
      listInterrupts: vi.fn(async () => ({
        task_id: 'task-1',
        interrupts: [{
          interrupt_id: 'interrupt-stale',
          task_id: 'task-1',
          node_id: 'task-1:stale',
          question: '请补充作物类型',
          required_fields: { crop: { options: ['rice'] } },
          status: 'open',
          created_at: null,
          answered_at: null,
        }],
      })),
    });
    const eventSourceFactory: EventSourceFactory = (_url, handlers) => {
      streamHandlers = handlers;
      return { close: vi.fn() };
    };
    await renderAuthed(<App apiClient={api} eventSourceFactory={eventSourceFactory} />);

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '取消中收到陈旧事件' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(streamHandlers).not.toBeNull());
    await act(async () => {
      streamHandlers?.onMessage(event('task.accepted'));
    });
    fireEvent.click(await screen.findByRole('button', { name: '停止' }));

    await waitFor(() => expect(api.cancelTask).toHaveBeenCalledWith('task-1'));
    expect(screen.getByText('正在停止当前对话任务')).toBeInTheDocument();

    await act(async () => {
      streamHandlers?.onMessage(event('node.started', { capability_id: 'agent.final_output' }, 'stale-start-during-cancel', 'n1'));
      streamHandlers?.onMessage(event('node.waiting_for_input', { interrupt_id: 'interrupt-stale' }, 'stale-wait-during-cancel', 'n1'));
      await Promise.resolve();
    });

    expect(screen.getByText('正在停止当前对话任务')).toBeInTheDocument();
    expect(screen.queryByText('请补充作物类型')).not.toBeInTheDocument();
    expect(api.listInterrupts).not.toHaveBeenCalled();

    await act(async () => {
      terminalStatus.resolve(taskSummary('task-1', 'cancelled'));
      await Promise.resolve();
    });
    expect(await screen.findByText('任务已取消')).toBeInTheDocument();
    expect(screen.queryByText('正在停止当前对话任务')).not.toBeInTheDocument();
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

  it('selects a dollar MCP Server command and submits only its stable server id', async () => {
    const api = makeApi({
      listMCPServers: vi.fn(async () => ({
        servers: [{
          server_id: 'mcp-ocr',
          display_name: 'OCR服务',
          routing_description: '识别图片和PDF',
          endpoint_url: 'https://secret.example.test/rpc',
          transport: 'streamable_http',
          protocol_preference: 'auto',
          auth_type: 'bearer',
          auth_metadata: { header_name: 'Authorization' },
          enabled: true,
          health_status: 'available',
          credential_configured: true,
          config_version: 1,
          security_version: 1,
          last_tested_at: null,
          last_test_error_code: null,
          created_at: '2026-08-17T00:00:00Z',
          updated_at: '2026-08-17T00:00:00Z',
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    await waitFor(() => expect(api.listMCPServers).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '$' } });
    expect(api.listMCPServers).toHaveBeenCalledTimes(1);
    const listbox = await screen.findByRole('listbox', { name: 'MCP Server 命令列表' });
    expect(within(listbox).getByText('$OCR服务')).toBeInTheDocument();
    expect(listbox).not.toHaveTextContent('secret.example.test');
    fireEvent.click(within(listbox).getByRole('option'));
    expect(await screen.findByLabelText('已选择 MCP Server')).toHaveTextContent('$OCR服务');

    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '识别这份材料' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '识别这份材料',
      capabilityId: 'mcp.dispatch',
      metadata: { mcp_server_binding: { server_id: 'mcp-ocr' } },
    })));
    expect(screen.queryByLabelText('已选择 MCP Server')).not.toBeInTheDocument();
  });

  it('clears the one-shot MCP Server badge after submit failure and refreshes candidates', async () => {
    const server = {
      server_id: 'mcp-ocr', display_name: 'OCR服务', routing_description: 'OCR',
      endpoint_url: 'https://secret.example.test', transport: 'streamable_http', protocol_preference: 'auto',
      auth_type: 'none', auth_metadata: {}, enabled: true, health_status: 'available', credential_configured: false,
      config_version: 1, security_version: 1, last_tested_at: null, last_test_error_code: null,
      created_at: '2026-08-17T00:00:00Z', updated_at: '2026-08-17T00:00:00Z',
    };
    const api = makeApi({
      listMCPServers: vi.fn(async () => ({ servers: [server] })),
      submitMessage: vi.fn(async () => { throw new Error('submit failed'); }),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '$' } });
    fireEvent.click(within(await screen.findByRole('listbox', { name: 'MCP Server 命令列表' })).getByRole('option'));
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '识别' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByLabelText('已选择 MCP Server')).not.toBeInTheDocument());
    await waitFor(() => expect(api.listMCPServers).toHaveBeenCalledTimes(2));
  });

  it('allows a selected MCP Server to submit an attachment without task text', async () => {
    const api = makeApi({
      listMCPServers: vi.fn(async () => ({
        servers: [{
          server_id: 'mcp-ocr', display_name: 'OCR服务', routing_description: 'OCR',
          endpoint_url: 'https://secret.example.test', transport: 'streamable_http', protocol_preference: 'auto',
          auth_type: 'none', auth_metadata: {}, enabled: true, health_status: 'available', credential_configured: false,
          config_version: 1, security_version: 1, last_tested_at: null, last_test_error_code: null,
          created_at: '2026-08-17T00:00:00Z', updated_at: '2026-08-17T00:00:00Z',
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([event('task.completed')])} />);
    fireEvent.change(screen.getByLabelText('请输入问题'), { target: { value: '$OCR服务' } });
    fireEvent.click((await screen.findByRole('listbox', { name: 'MCP Server 命令列表' })).querySelector('[role="option"]') as HTMLElement);
    const file = new File(['image'], 'scan.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText('上传 JSON、CSV、TSV、Excel、TXT、VCF、图片或 PDF 文件'), { target: { files: [file] } });
    await screen.findByText(/scan.png/);

    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(api.submitMessage).toHaveBeenCalledWith(expect.objectContaining({
      content: '',
      capabilityId: 'mcp.dispatch',
      metadata: {
        upload_ids: ['upl-1'],
        mcp_server_binding: { server_id: 'mcp-ocr' },
      },
    })));
  });

  it('restores a safe MCP Server badge from user message history', async () => {
    const api = makeApi({
      listConversations: vi.fn(async () => ({
        conversations: [{
          conversation_id: 'conv-mcp-history',
          username: 'alice',
          status: 'active',
          current_task_id: null,
          title: 'MCP历史',
          created_at: null,
          updated_at: null,
        }],
      })),
      listConversationMessages: vi.fn(async () => ({
        conversation_id: 'conv-mcp-history',
        messages: [{
          message_id: 'msg-mcp',
          conversation_id: 'conv-mcp-history',
          role: 'user',
          content: '识别材料',
          task_id: 'task-mcp',
          stream_status: null,
          created_at: null,
          metadata: {
            mcp_server_badge: {
              server_id: 'mcp-ocr',
              display_name: 'OCR服务',
              command: '$OCR服务',
              binding_mode: 'explicit_command',
            },
          },
        }],
      })),
    });
    await renderAuthed(<App apiClient={api} eventSourceFactory={makeEventSourceFactory([])} />);
    fireEvent.click(await screen.findByRole('button', { name: 'MCP历史' }));

    expect(await screen.findByText('$OCR服务')).toBeInTheDocument();
    expect(screen.getByText('识别材料')).toBeInTheDocument();
  });

});
