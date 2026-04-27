import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Badge, Button, Card, ConfigProvider, Flex, Input, Layout, Radio, Space, Spin, Tag, Typography, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { createApiClient, type ApiClient } from './api/client';
import { createBrowserEventSourceFactory, taskEventsUrl, type EventSourceFactory, type TaskEventSubscription } from './api/taskEvents';
import type { ArtifactResponse, CapabilityResponse, ChatMode, TaskEventEnvelope } from './api/types';
import { parseAssistantTextArtifact, parseSqlQueryArtifacts, type SqlQueryDisplayModel } from './domain/artifacts';
import { applyTaskEvent, createInitialTaskEventState, createSubmittingTaskState, isTaskActive, markTaskCompleted, markTaskFailed, markWaitingInputUnsupported, type TaskEventState } from './domain/taskEvents';
import { SqlQueryResultCard } from './components/SqlQueryResultCard';
import './styles.css';

interface AppProps {
  apiClient?: ApiClient;
  eventSourceFactory?: EventSourceFactory;
  waitingInputCheckDelayMs?: number;
}

type MessageRole = 'user' | 'assistant';

interface ConversationMessage {
  id: string;
  role: MessageRole;
  content: string;
  mode: ChatMode;
  result?: SqlQueryDisplayModel;
}

const DEFAULT_ACCOUNT_ID = 'web-user';
const CONVERSATION_STORAGE_KEY = 'maf.frontend.conversation_id';
const WAITING_INPUT_CHECK_DELAY_MS = 8_000;

function App({ apiClient, eventSourceFactory, waitingInputCheckDelayMs = WAITING_INPUT_CHECK_DELAY_MS }: AppProps) {
  const api = useMemo(() => apiClient ?? createApiClient(), [apiClient]);
  const createEventSource = useMemo(() => eventSourceFactory ?? createBrowserEventSourceFactory(), [eventSourceFactory]);
  const [conversationId] = useState(() => loadOrCreateConversationId());
  const [mode, setMode] = useState<ChatMode>('chat');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [taskState, setTaskState] = useState<TaskEventState>(createInitialTaskEventState());
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [currentAssistantId, setCurrentAssistantId] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityResponse[]>([]);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const subscriptionRef = useRef<TaskEventSubscription | null>(null);

  useEffect(() => {
    let mounted = true;
    api.listCapabilities()
      .then((result) => {
        if (mounted) setCapabilities(result.capabilities);
      })
      .catch(() => {
        if (mounted) setGlobalError('能力目录加载失败，仍可尝试提交普通对话。');
      });
    return () => {
      mounted = false;
      subscriptionRef.current?.close();
    };
  }, [api]);

  const active = isTaskActive(taskState.phase);
  const sqlHint = mode === 'chat' && /查询|品种|基因型|审定|数据库/.test(input);

  useEffect(() => {
    if (!currentTaskId || !active || taskState.phase === 'cancelling') return;
    let stopped = false;
    let inFlight = false;
    let attempts = 0;
    const maxAttempts = 30;
    const delay = Math.max(waitingInputCheckDelayMs, 1);

    const poll = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      attempts += 1;
      try {
        const shouldStop = await detectUnsupportedInterrupt(currentTaskId);
        stopped = shouldStop || attempts >= maxAttempts;
      } finally {
        inFlight = false;
      }
    };

    void poll();
    const interval = window.setInterval(() => {
      void poll();
    }, delay);
    return () => {
      stopped = true;
      window.clearInterval(interval);
    };
  }, [active, api, currentTaskId, taskState.phase, waitingInputCheckDelayMs]);

  async function detectUnsupportedInterrupt(taskId: string): Promise<boolean> {
    try {
      const graph = await api.getTaskGraph(taskId);
      if (graph.nodes.some((node) => node.status === 'waiting_for_input')) {
        setTaskState((state) => markWaitingInputUnsupported(state));
        setCurrentTaskId(null);
        subscriptionRef.current?.close();
        return true;
      }
      const task = await api.getTask(taskId);
      if (['completed', 'failed', 'cancelled'].includes(task.status)) {
        return true;
      }
    } catch {
      // The task graph is a best-effort fallback for unsupported interrupts.
      // Keep the current running state if the fallback check fails.
    }
    return false;
  }

  async function handleSubmit() {
    const content = input.trim();
    if (!content || active) return;
    setGlobalError(null);
    const userMessage: ConversationMessage = { id: makeClientId('user'), role: 'user', content, mode };
    const assistantMessage: ConversationMessage = { id: makeClientId('assistant'), role: 'assistant', content: '', mode };
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setCurrentAssistantId(assistantMessage.id);
    setInput('');
    setTaskState(createSubmittingTaskState());

    try {
      const accepted = await api.submitMessage({
        conversationId,
        accountId: DEFAULT_ACCOUNT_ID,
        content,
        mode,
      });
      setCurrentTaskId(accepted.task_id);
      subscribeToTask(accepted.task_id, assistantMessage.id, mode);
    } catch (error) {
      setTaskState((state) => markTaskFailed(state, friendlyError(error)));
      setGlobalError(friendlyError(error));
    }
  }

  function subscribeToTask(taskId: string, assistantId: string, submittedMode: ChatMode) {
    subscriptionRef.current?.close();
    subscriptionRef.current = createEventSource(taskEventsUrl(taskId), {
      onMessage: (event) => handleTaskEvent(event, taskId, assistantId, submittedMode),
      onError: () => handleEventStreamError(taskId, assistantId, submittedMode),
    });
  }

  function handleTaskEvent(event: TaskEventEnvelope, taskId: string, assistantId: string, submittedMode: ChatMode) {
    setTaskState((previous) => {
      const next = applyTaskEvent(previous, event);
      if (next.assistantText !== previous.assistantText) {
        updateAssistantMessage(assistantId, { content: next.assistantText });
      }
      if (['task.failed', 'node.failed', 'sql_query.sql_guard_blocked'].includes(event.event_type)) {
        subscriptionRef.current?.close();
      }
      if (event.event_type === 'task.cancelled') {
        subscriptionRef.current?.close();
      }
      return next;
    });
    if (event.event_type === 'task.completed') {
      void loadArtifacts(taskId, assistantId, submittedMode);
    }
  }

  async function handleEventStreamError(taskId: string, assistantId: string, submittedMode: ChatMode) {
    setGlobalError('事件流暂时中断，正在尝试查询任务状态。');
    try {
      const task = await api.getTask(taskId);
      if (task.status === 'completed') {
        await loadArtifacts(taskId, assistantId, submittedMode);
      } else if (task.status === 'failed') {
        setTaskState((state) => markTaskFailed(state, '本次任务未完成，请调整问题后重试。'));
      } else if (task.status === 'cancelled') {
        setTaskState((state) => ({ ...state, phase: 'cancelled', statusText: '任务已取消' }));
      }
    } catch {
      setGlobalError('事件流中断，任务状态暂时无法确认。');
    }
  }

  async function loadArtifacts(taskId: string, assistantId: string, submittedMode: ChatMode) {
    try {
      const response = await api.getTaskArtifacts(taskId);
      if (submittedMode === 'sql_query') {
        const result = parseSqlQueryArtifacts(response.artifacts);
        updateAssistantMessage(assistantId, { content: result.summary, result });
      } else {
        const fallbackText = parseAssistantTextArtifact(response.artifacts);
        if (fallbackText) updateAssistantMessage(assistantId, { content: fallbackText });
      }
      setTaskState((state) => markTaskCompleted(state));
      setCurrentTaskId(null);
      subscriptionRef.current?.close();
    } catch {
      setTaskState((state) => markTaskCompleted(state, '任务已完成，但结果加载失败'));
      setGlobalError('结果加载失败，可稍后重试。');
    }
  }

  async function handleCancel() {
    if (!currentTaskId) return;
    setTaskState((state) => ({ ...state, phase: 'cancelling', statusText: '取消请求已发送' }));
    try {
      await api.cancelTask(currentTaskId);
    } catch (error) {
      setGlobalError(friendlyError(error));
    }
  }

  function updateAssistantMessage(messageId: string, patch: Partial<Pick<ConversationMessage, 'content' | 'result'>>) {
    setMessages((current) => current.map((message) => (message.id === messageId ? { ...message, ...patch } : message)));
  }

  const modeOptions = api.uiModes.map((option) => ({ label: option.label, value: option.key }));

  return (
    <ConfigProvider locale={zhCN} theme={{ algorithm: theme.defaultAlgorithm }}>
      <Layout className="app-shell">
        <Layout.Header className="app-header">
          <Flex justify="space-between" align="center" gap="middle">
            <div>
              <Typography.Title level={3} className="app-title">业务对话台</Typography.Title>
              <Typography.Text type="secondary">内部业务用户入口 · FastAPI/SSE</Typography.Text>
            </div>
            <Space wrap>
              <CapabilityBadge capabilityId="main_agent.respond" label="主代理" capabilities={capabilities} />
              <CapabilityBadge capabilityId="sql_query.query" label="SQLQuery" capabilities={capabilities} />
              <Tag color="geekblue">conversation: {conversationId.slice(0, 12)}</Tag>
            </Space>
          </Flex>
        </Layout.Header>
        <Layout.Content className="app-content">
          {globalError ? <Alert className="top-alert" type="warning" showIcon closable onClose={() => setGlobalError(null)} message={globalError} /> : null}
          <Card className="conversation-card" styles={{ body: { padding: 0 } }}>
            <div className="conversation-list" aria-label="对话内容">
              {messages.length === 0 ? <EmptyWelcome /> : messages.map((message) => <MessageBubble key={message.id} message={message} />)}
              {active && currentAssistantId && !taskState.assistantText ? (
                <div className="assistant-pending"><Spin size="small" /> <span>等待任务事件...</span></div>
              ) : null}
            </div>
          </Card>
          <TaskStatusBanner state={taskState} />
          <Card className="composer-card">
            {sqlHint ? <Alert className="mode-hint" type="info" showIcon message="这可能适合使用数据库查询（SQLQuery）模式。" /> : null}
            <Space direction="vertical" size="middle" className="composer-space">
              <Radio.Group aria-label="对话模式" value={mode} options={modeOptions} onChange={(event) => setMode(event.target.value)} disabled={active} />
              <Input.TextArea
                aria-label="请输入问题"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onPressEnter={(event) => {
                  if (!event.shiftKey) {
                    event.preventDefault();
                    void handleSubmit();
                  }
                }}
                placeholder="请输入你的问题。数据库查询请切换到 SQLQuery 模式。"
                autoSize={{ minRows: 3, maxRows: 8 }}
                disabled={active && taskState.phase !== 'cancelling'}
              />
              <Flex justify="space-between" align="center" gap="middle">
                <Typography.Text type="secondary">当前模式：{api.uiModes.find((item) => item.key === mode)?.label}</Typography.Text>
                <Space>
                  {active && currentTaskId ? <Button danger aria-label="取消任务" onClick={handleCancel} loading={taskState.phase === 'cancelling'}>取消任务</Button> : null}
                  <Button type="primary" aria-label="发送" onClick={handleSubmit} disabled={!input.trim() || active}>发送</Button>
                </Space>
              </Flex>
            </Space>
          </Card>
        </Layout.Content>
      </Layout>
    </ConfigProvider>
  );
}

function CapabilityBadge({ capabilityId, label, capabilities }: { capabilityId: string; label: string; capabilities: CapabilityResponse[] }) {
  const found = capabilities.find((capability) => capability.capability_id === capabilityId || (capabilityId === 'main_agent.respond' && capability.capability_id === 'main_agent.respond'));
  const active = found?.status === 'active';
  return <Badge status={active ? 'success' : 'default'} text={`${label}${active ? '可用' : '未确认'}`} />;
}

function EmptyWelcome() {
  return (
    <div className="empty-welcome">
      <Typography.Title level={4}>开始一次业务问答</Typography.Title>
      <Typography.Paragraph type="secondary">普通对话会进入主代理；品种、基因型、审定等数据库问题请切换到 SQLQuery。</Typography.Paragraph>
    </div>
  );
}

function MessageBubble({ message }: { message: ConversationMessage }) {
  const className = message.role === 'user' ? 'message message-user' : 'message message-assistant';
  return (
    <div className={className}>
      <div className="message-meta">{message.role === 'user' ? '你' : message.mode === 'sql_query' ? 'SQLQuery' : '主代理'}</div>
      <div className="message-body">
        {message.result ? (
          <SqlQueryResultCard result={message.result} />
        ) : message.content ? (
          <Typography.Paragraph>{message.content}</Typography.Paragraph>
        ) : (
          <Typography.Text type="secondary">正在等待回答...</Typography.Text>
        )}
      </div>
    </div>
  );
}

function TaskStatusBanner({ state }: { state: TaskEventState }) {
  const type = state.phase === 'failed' ? 'error' : state.phase === 'cancelled' ? 'warning' : state.phase === 'completed' ? 'success' : 'info';
  return <Alert className="task-status" type={type} showIcon message={state.statusText} description={state.errorMessage ?? undefined} />;
}

function loadOrCreateConversationId(): string {
  const existing = localStorage.getItem(CONVERSATION_STORAGE_KEY);
  if (existing) return existing;
  const created = `conv-web-${crypto.randomUUID?.() ?? Math.random().toString(16).slice(2)}`;
  localStorage.setItem(CONVERSATION_STORAGE_KEY, created);
  return created;
}

function makeClientId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID?.() ?? Math.random().toString(16).slice(2)}`;
}

function friendlyError(error: unknown): string {
  if (error && typeof error === 'object' && 'userMessage' in error && typeof (error as { userMessage?: unknown }).userMessage === 'string') {
    return (error as { userMessage: string }).userMessage;
  }
  return '请求未完成，请稍后重试。';
}

export default App;
