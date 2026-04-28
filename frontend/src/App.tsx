import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Badge, Button, Card, ConfigProvider, Flex, Input, Layout, Select, Space, Spin, Switch, Tag, Typography, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { createApiClient, type ApiClient } from './api/client';
import { createBrowserEventSourceFactory, taskEventsUrl, type EventSourceFactory, type TaskEventSubscription } from './api/taskEvents';
import type { CapabilityResponse, ChatMode, ReasoningEffort, TaskEventEnvelope, TaskSummaryResponse } from './api/types';
import { parseAssistantTextArtifact, parseCapabilityArtifactDisplays, summarizeCapabilityArtifactDisplays, type CapabilityArtifactDisplay } from './domain/artifacts';
import { applyTaskEvent, createInitialTaskEventState, createSubmittingTaskState, isTaskActive, markTaskCompleted, markTaskFailed, markWaitingInputRequired, type TaskEventState } from './domain/taskEvents';
import { SqlQueryResultCard } from './components/SqlQueryResultCard';
import { MarkdownText } from './components/MarkdownText';
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
  reasoningRequested?: boolean;
  reasoningComplete?: boolean;
  reasoningContent?: string;
  activityText?: string;
  artifactDisplays?: CapabilityArtifactDisplay[];
  finalContentLoaded?: boolean;
  interruptPrompt?: PendingInterrupt;
}

type AssistantMessagePatch = Partial<Pick<ConversationMessage, 'content' | 'mode' | 'reasoningRequested' | 'reasoningComplete' | 'reasoningContent' | 'activityText' | 'artifactDisplays' | 'finalContentLoaded' | 'interruptPrompt'>>;

interface PendingInterrupt {
  taskId: string;
  interruptId: string;
  question: string;
  requiredFields: Record<string, unknown>;
  mode: ChatMode;
}

const DEFAULT_ACCOUNT_ID = 'web-user';
const CONVERSATION_STORAGE_KEY = 'maf.frontend.conversation_id';
const WAITING_INPUT_CHECK_DELAY_MS = 8_000;
const INTERRUPT_FIELD_LABELS: Record<string, string> = {
  crop: '作物类型',
  missing_info: '补充信息',
  region: '地区',
  route_id: '查询范围',
  year_range: '年份范围',
};
const INTERRUPT_OPTION_LABELS: Record<string, string> = {
  approval_variety_db: '审定品种库',
  corn: '玉米',
  cotton: '棉花',
  genotype_db: '基因型数据库',
  rice: '水稻',
  soybean: '大豆',
  wheat: '小麦',
};
const REASONING_EFFORT_OPTIONS: { label: string; value: ReasoningEffort }[] = [
  { label: '最底', value: 'minimal' },
  { label: '低', value: 'low' },
  { label: '中', value: 'medium' },
  { label: '高', value: 'high' },
];

function App({ apiClient, eventSourceFactory, waitingInputCheckDelayMs = WAITING_INPUT_CHECK_DELAY_MS }: AppProps) {
  const api = useMemo(() => apiClient ?? createApiClient(), [apiClient]);
  const createEventSource = useMemo(() => eventSourceFactory ?? createBrowserEventSourceFactory(), [eventSourceFactory]);
  const [conversationId] = useState(() => loadOrCreateConversationId());
  const mode: ChatMode = 'chat';
  const [deepThinking, setDeepThinking] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('medium');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [taskState, setTaskState] = useState<TaskEventState>(createInitialTaskEventState());
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [currentAssistantId, setCurrentAssistantId] = useState<string | null>(null);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingInterrupt | null>(null);
  const [unfinishedTasks, setUnfinishedTasks] = useState<TaskSummaryResponse[]>([]);
  const [taskListLoading, setTaskListLoading] = useState(false);
  const [taskListExpanded, setTaskListExpanded] = useState(false);
  const [stoppingTaskIds, setStoppingTaskIds] = useState<Set<string>>(() => new Set());
  const [capabilities, setCapabilities] = useState<CapabilityResponse[]>([]);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const subscriptionRef = useRef<TaskEventSubscription | null>(null);
  const taskPresentationModesRef = useRef<Map<string, ChatMode>>(new Map());
  const pendingAssistantPatchesRef = useRef<Map<string, AssistantMessagePatch>>(new Map());

  const refreshUnfinishedTasks = useCallback(async (showLoading = false) => {
    if (showLoading) setTaskListLoading(true);
    try {
      const result = await api.listConversationTasks(conversationId);
      setUnfinishedTasks(result.tasks);
    } catch {
      if (showLoading) setGlobalError('未完成任务列表刷新失败，请稍后重试。');
    } finally {
      if (showLoading) setTaskListLoading(false);
    }
  }, [api, conversationId]);

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

  useEffect(() => {
    void refreshUnfinishedTasks();
    const interval = window.setInterval(() => {
      void refreshUnfinishedTasks();
    }, 5_000);
    return () => {
      window.clearInterval(interval);
    };
  }, [refreshUnfinishedTasks]);

  const active = isTaskActive(taskState.phase);
  const sqlHint = /查询|品种|基因型|审定|数据库/.test(input);

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
        const shouldStop = await detectPendingInterrupt(currentTaskId);
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

  async function detectPendingInterrupt(taskId: string): Promise<boolean> {
    try {
      const graph = await api.getTaskGraph(taskId);
      if (graph.nodes.some((node) => node.status === 'waiting_for_input')) {
        const interrupts = await api.listInterrupts(taskId).catch(() => ({ task_id: taskId, interrupts: [] }));
        const openInterrupt = interrupts.interrupts.find((interrupt) => interrupt.status === 'open');
        if (!openInterrupt) {
          setTaskState((state) => ({
            ...state,
            statusText: '正在等待任务给出补充信息',
            errorMessage: null,
          }));
          return false;
        }
        const interruptionMode = taskPresentationModesRef.current.get(taskId) ?? mode;
        const pending: PendingInterrupt = {
          taskId,
          interruptId: openInterrupt.interrupt_id,
          question: openInterrupt.question,
          requiredFields: openInterrupt.required_fields,
          mode: interruptionMode,
        };
        setPendingInterrupt(pending);
        if (currentAssistantId) {
          updateAssistantMessage(currentAssistantId, {
            content: '',
            interruptPrompt: pending,
            mode: interruptionMode,
            artifactDisplays: undefined,
            finalContentLoaded: undefined,
          });
        }
        setTaskState((state) => markWaitingInputRequired(state));
        subscriptionRef.current?.close();
        return true;
      }
      const task = await api.getTask(taskId);
      if (['completed', 'failed', 'cancelled'].includes(task.status)) {
        return true;
      }
    } catch {
      // The task graph is a best-effort fallback for resumable interrupts.
      // Keep the current running state if the fallback check fails.
    }
    return false;
  }

  async function handleSubmit() {
    const content = input.trim();
    if (!content || active) return;
    setGlobalError(null);
    if (pendingInterrupt) {
      await handleInterruptAnswer(content, pendingInterrupt);
      return;
    }
    const userMessage: ConversationMessage = { id: makeClientId('user'), role: 'user', content, mode };
    const assistantMessage: ConversationMessage = {
      id: makeClientId('assistant'),
      role: 'assistant',
      content: '',
      mode,
      reasoningRequested: deepThinking,
    };
    setMessages((current) => [...current, userMessage, applyPendingAssistantPatch(assistantMessage)]);
    setCurrentAssistantId(assistantMessage.id);
    setInput('');
    setTaskState(createSubmittingTaskState());

    try {
      const accepted = await api.submitMessage({
        conversationId,
        accountId: DEFAULT_ACCOUNT_ID,
        content,
        mode,
        deepThinking,
        reasoningEffort,
      });
      taskPresentationModesRef.current.set(accepted.task_id, mode);
      setCurrentTaskId(accepted.task_id);
      subscribeToTask(accepted.task_id, assistantMessage.id);
      void refreshUnfinishedTasks();
    } catch (error) {
      setTaskState((state) => markTaskFailed(state, friendlyError(error)));
      setGlobalError(friendlyError(error));
    }
  }

  async function handleInterruptAnswer(content: string, interrupt: PendingInterrupt) {
    const userMessage: ConversationMessage = { id: makeClientId('user'), role: 'user', content, mode: interrupt.mode };
    const assistantMessage: ConversationMessage = {
      id: makeClientId('assistant'),
      role: 'assistant',
      content: '已收到补充信息，继续当前任务...',
      mode: interrupt.mode,
      reasoningRequested: false,
    };
    setMessages((current) => [...current, userMessage, applyPendingAssistantPatch(assistantMessage)]);
    setCurrentAssistantId(assistantMessage.id);
    setInput('');
    setTaskState((state) => ({
      ...state,
      phase: 'running',
      statusText: '补充信息已提交，正在继续任务',
      assistantText: '',
      reasoningText: '',
      errorMessage: null,
    }));

    try {
      await api.answerInterrupt(interrupt.taskId, interrupt.interruptId, buildInterruptAnswerPayload(interrupt, content));
      taskPresentationModesRef.current.set(interrupt.taskId, interrupt.mode);
      setPendingInterrupt(null);
      setCurrentTaskId(interrupt.taskId);
      subscribeToTask(interrupt.taskId, assistantMessage.id);
      void refreshUnfinishedTasks();
    } catch (error) {
      setTaskState((state) => markTaskFailed(state, friendlyError(error)));
      setGlobalError(friendlyError(error));
    }
  }

  function subscribeToTask(taskId: string, assistantId: string) {
    subscriptionRef.current?.close();
    subscriptionRef.current = createEventSource(taskEventsUrl(taskId), {
      onMessage: (event) => handleTaskEvent(event, taskId, assistantId),
      onError: () => handleEventStreamError(taskId, assistantId),
    });
  }

  function handleTaskEvent(event: TaskEventEnvelope, taskId: string, assistantId: string) {
    setTaskState((previous) => {
      const next = applyTaskEvent(previous, event);
      if (next.assistantText !== previous.assistantText) {
        updateAssistantStreamingContent(assistantId, next.assistantText);
      }
      if (next.reasoningText !== previous.reasoningText) {
        updateAssistantMessage(assistantId, { reasoningContent: next.reasoningText });
      }
      if (next.currentActivityText !== previous.currentActivityText) {
        updateAssistantMessage(assistantId, { activityText: next.currentActivityText ?? undefined });
      }
      if (['task.failed', 'node.failed', 'sql_query.sql_guard_blocked'].includes(event.event_type)) {
        subscriptionRef.current?.close();
        taskPresentationModesRef.current.delete(taskId);
      }
      if (event.event_type === 'task.cancelled') {
        subscriptionRef.current?.close();
        taskPresentationModesRef.current.delete(taskId);
        void refreshUnfinishedTasks();
      }
      return next;
    });
    if (event.event_type === 'task.completed') {
      updateAssistantMessage(assistantId, { reasoningComplete: true });
      void loadArtifacts(taskId, assistantId);
    } else if (event.event_type === 'task.failed') {
      void refreshUnfinishedTasks();
    }
  }

  async function handleEventStreamError(taskId: string, assistantId: string) {
    setGlobalError('事件流暂时中断，正在尝试查询任务状态。');
    try {
      const task = await api.getTask(taskId);
      if (task.status === 'completed') {
        await loadArtifacts(taskId, assistantId);
      } else if (task.status === 'failed') {
        setTaskState((state) => markTaskFailed(state, '本次任务未完成，请调整问题后重试。'));
      } else if (task.status === 'cancelled') {
        setTaskState((state) => ({ ...state, phase: 'cancelled', statusText: '任务已取消' }));
      }
    } catch {
      setGlobalError('事件流中断，任务状态暂时无法确认。');
    }
  }

  async function loadArtifacts(taskId: string, assistantId: string) {
    try {
      const response = await api.getTaskArtifacts(taskId);
      const artifactDisplays = parseCapabilityArtifactDisplays(response.artifacts);
      const fallbackText = parseAssistantTextArtifact(response.artifacts);
      const artifactSummary = summarizeCapabilityArtifactDisplays(artifactDisplays);
      if (fallbackText || artifactDisplays.length > 0) {
        updateAssistantMessage(assistantId, {
          content: fallbackText ?? artifactSummary,
          artifactDisplays: artifactDisplays.length > 0 ? artifactDisplays : undefined,
          finalContentLoaded: true,
        });
      }
      updateAssistantMessage(assistantId, { activityText: undefined });
      setTaskState((state) => markTaskCompleted(state));
      setCurrentTaskId(null);
      taskPresentationModesRef.current.delete(taskId);
      subscriptionRef.current?.close();
      void refreshUnfinishedTasks();
    } catch {
      setTaskState((state) => markTaskCompleted(state, '任务已完成，但结果加载失败'));
      setGlobalError('结果加载失败，可稍后重试。');
      void refreshUnfinishedTasks();
    }
  }

  async function handleStopTask(taskId: string) {
    setStoppingTaskIds((current) => new Set(current).add(taskId));
    const isCurrentTask = currentTaskId === taskId;
    if (isCurrentTask) {
      setTaskState((state) => ({ ...state, phase: 'cancelling', statusText: '停止请求已发送' }));
    }
    try {
      await api.cancelTask(taskId);
      if (isCurrentTask) {
        setPendingInterrupt(null);
        setCurrentTaskId(null);
        subscriptionRef.current?.close();
        setTaskState((state) => ({ ...state, phase: 'cancelled', statusText: '任务已取消', errorMessage: null }));
      }
      await refreshUnfinishedTasks();
    } catch (error) {
      setGlobalError(friendlyError(error));
    } finally {
      setStoppingTaskIds((current) => {
        const next = new Set(current);
        next.delete(taskId);
        return next;
      });
    }
  }

  async function handleCancel() {
    if (!currentTaskId) return;
    await handleStopTask(currentTaskId);
  }

  function updateAssistantMessage(messageId: string, patch: AssistantMessagePatch) {
    setMessages((current) => {
      let found = false;
      const next = current.map((message) => {
        if (message.id !== messageId) return message;
        found = true;
        return { ...message, ...patch };
      });
      if (!found) {
        pendingAssistantPatchesRef.current.set(messageId, {
          ...(pendingAssistantPatchesRef.current.get(messageId) ?? {}),
          ...patch,
        });
      }
      return next;
    });
  }

  function updateAssistantStreamingContent(messageId: string, content: string) {
    setMessages((current) => {
      let found = false;
      const next = current.map((message) => {
        if (message.id !== messageId) return message;
        found = true;
        if (message.finalContentLoaded) return message;
        return { ...message, content };
      });
      if (!found) {
        pendingAssistantPatchesRef.current.set(messageId, {
          ...(pendingAssistantPatchesRef.current.get(messageId) ?? {}),
          content,
        });
      }
      return next;
    });
  }

  function applyPendingAssistantPatch(message: ConversationMessage): ConversationMessage {
    const patch = pendingAssistantPatchesRef.current.get(message.id);
    if (!patch) return message;
    pendingAssistantPatchesRef.current.delete(message.id);
    return { ...message, ...patch };
  }

  const interactionLocked = active || Boolean(pendingInterrupt);
  const inputPlaceholder = pendingInterrupt ? interruptAnswerPlaceholder(pendingInterrupt) : '请输入你的问题，主代理会自动选择能力并规划执行。';

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
              {active && currentAssistantId && !taskState.assistantText && !taskState.currentActivityText ? (
                <div className="assistant-pending"><Spin size="small" /> <span>等待任务事件...</span></div>
              ) : null}
            </div>
          </Card>
          <TaskStatusBanner state={taskState} />
          <TaskListPanel
            tasks={unfinishedTasks}
            loading={taskListLoading}
            expanded={taskListExpanded}
            stoppingTaskIds={stoppingTaskIds}
            onToggleExpanded={() => setTaskListExpanded((expanded) => !expanded)}
            onRefresh={() => void refreshUnfinishedTasks(true)}
            onStop={(taskId) => void handleStopTask(taskId)}
          />
          <Card className="composer-card">
            {sqlHint ? <Alert className="mode-hint" type="info" showIcon message="主代理会自动判断是否需要调用 SQLQuery，无需手动切换模式。" /> : null}
            <Space direction="vertical" size="middle" className="composer-space">
              {pendingInterrupt ? <InterruptInputBanner interrupt={pendingInterrupt} onCancel={handleCancel} cancelling={taskState.phase === 'cancelling'} /> : null}
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
                placeholder={inputPlaceholder}
                autoSize={{ minRows: 3, maxRows: 8 }}
                disabled={active && taskState.phase !== 'cancelling'}
              />
              <Flex justify="space-between" align="center" gap="middle">
                <Space wrap size="middle" align="center">
                  <Typography.Text type="secondary">当前模式：自动规划</Typography.Text>
                  <Space size="small" align="center">
                    <Typography.Text type="secondary">思考强度</Typography.Text>
                    <Select
                      aria-label="思考强度"
                      value={reasoningEffort}
                      options={REASONING_EFFORT_OPTIONS}
                      onChange={setReasoningEffort}
                      disabled={interactionLocked}
                      size="small"
                      style={{ width: 104 }}
                    />
                  </Space>
                  <Space size="small" align="center">
                    <Typography.Text type="secondary">深度思考</Typography.Text>
                    <Switch
                      aria-label="深度思考"
                      checked={deepThinking}
                      onChange={setDeepThinking}
                      checkedChildren="开"
                      unCheckedChildren="关"
                      disabled={interactionLocked}
                    />
                  </Space>
                </Space>
                <Space>
                  {active && currentTaskId ? (
                    <Button
                      danger
                      aria-label="取消任务"
                      onClick={handleCancel}
                      loading={taskState.phase === 'cancelling' || stoppingTaskIds.has(currentTaskId)}
                    >
                      取消任务
                    </Button>
                  ) : null}
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
      <Typography.Paragraph type="secondary">直接描述你的问题即可；主代理会自动判断是否需要调用 SQLQuery 等能力并组织结果。</Typography.Paragraph>
    </div>
  );
}

function TaskListPanel({
  tasks,
  loading,
  expanded,
  stoppingTaskIds,
  onToggleExpanded,
  onRefresh,
  onStop,
}: {
  tasks: TaskSummaryResponse[];
  loading: boolean;
  expanded: boolean;
  stoppingTaskIds: Set<string>;
  onToggleExpanded: () => void;
  onRefresh: () => void;
  onStop: (taskId: string) => void;
}) {
  return (
    <Card
      className={`task-list-card ${expanded ? 'task-list-card-expanded' : 'task-list-card-collapsed'}`}
      title={(
        <Space size="small">
          <span>未完成任务</span>
          <Tag color={tasks.length > 0 ? 'orange' : 'default'}>{tasks.length}</Tag>
        </Space>
      )}
      extra={(
        <Space size="small">
          <Button size="small" aria-label={expanded ? '收起未完成任务' : '展开未完成任务'} onClick={onToggleExpanded}>
            {expanded ? '收起' : '展开'}
          </Button>
          <Button size="small" onClick={onRefresh} loading={loading}>刷新</Button>
        </Space>
      )}
    >
      {!expanded ? null : tasks.length === 0 ? (
        <Typography.Text type="secondary">暂无未完成任务</Typography.Text>
      ) : (
        <Space direction="vertical" size="small" className="task-list-items">
          {tasks.map((task) => {
            const stopping = stoppingTaskIds.has(task.task_id);
            const stopDisabled = stopping || task.cancel_requested || task.status === 'cancelling';
            return (
              <div className="task-list-item" key={task.task_id}>
                <div className="task-list-main">
                  <Typography.Text strong>{task.summary?.trim() || `任务 ${shortTaskId(task.task_id)}`}</Typography.Text>
                  <Space size={[6, 6]} wrap className="task-list-meta">
                    <Tag color={taskStatusColor(task.status)}>{taskStatusLabel(task.status)}</Tag>
                    <Typography.Text type="secondary">{capabilityLabel(task.requested_capability_id)}</Typography.Text>
                    <Typography.Text type="secondary">活动节点 {task.active_node_count}</Typography.Text>
                    <Typography.Text type="secondary">ID {shortTaskId(task.task_id)}</Typography.Text>
                  </Space>
                </div>
                <Button
                  danger
                  size="small"
                  aria-label={`停止任务 ${task.task_id}`}
                  disabled={stopDisabled}
                  loading={stopping}
                  onClick={() => onStop(task.task_id)}
                >
                  停止任务
                </Button>
              </div>
            );
          })}
        </Space>
      )}
    </Card>
  );
}

function taskStatusLabel(status: string): string {
  if (status === 'accepted') return '已提交';
  if (status === 'planning') return '规划中';
  if (status === 'running') return '运行中';
  if (status === 'cancelling') return '停止中';
  return status;
}

function taskStatusColor(status: string): string {
  if (status === 'running') return 'processing';
  if (status === 'cancelling') return 'red';
  if (status === 'accepted' || status === 'planning') return 'blue';
  return 'default';
}

function capabilityLabel(capabilityId: string | null): string {
  if (capabilityId === 'sql_query.query' || capabilityId === 'sql_query') return 'SQLQuery';
  if (capabilityId === null) return '自动规划';
  if (capabilityId.startsWith('main_agent')) return '主代理';
  return capabilityId;
}

function shortTaskId(taskId: string): string {
  return taskId.length > 12 ? `${taskId.slice(0, 12)}…` : taskId;
}

function buildInterruptAnswerPayload(interrupt: PendingInterrupt, content: string): Record<string, unknown> {
  const fieldNames = Object.keys(interrupt.requiredFields ?? {});
  if (fieldNames.length === 1) {
    return { [fieldNames[0]]: content };
  }
  return { answer: content };
}

function InterruptPromptCard({ interrupt }: { interrupt: PendingInterrupt }) {
  const fieldLabels = interruptFieldLabels(interrupt);
  const optionLabels = interruptOptionLabels(interrupt);
  return (
    <section className="interrupt-card" role="region" aria-label="需要补充信息">
      <div className="interrupt-card-title">需要补充信息</div>
      <Typography.Paragraph className="interrupt-card-question">{interrupt.question}</Typography.Paragraph>
      {fieldLabels.length > 0 ? (
        <div className="interrupt-card-row">
          <Typography.Text type="secondary">需要补充：</Typography.Text>
          <Space size={[6, 6]} wrap>{fieldLabels.map((field) => <Tag key={field} color="blue">{field}</Tag>)}</Space>
        </div>
      ) : null}
      {optionLabels.length > 0 ? (
        <div className="interrupt-card-row">
          <Typography.Text type="secondary">可选示例：</Typography.Text>
          <Space size={[6, 6]} wrap>{optionLabels.map((option) => <Tag key={option}>{option}</Tag>)}</Space>
        </div>
      ) : null}
      <Typography.Text className="interrupt-card-hint" type="secondary">回复后将继续当前任务。</Typography.Text>
    </section>
  );
}

function InterruptInputBanner({ interrupt, onCancel, cancelling }: { interrupt: PendingInterrupt; onCancel: () => void; cancelling: boolean }) {
  return (
    <div className="interrupt-input-banner" role="status">
      <span>当前任务等待补充信息：{interruptFieldSummary(interrupt)}。你的下一条消息会继续这个任务。</span>
      <Button danger size="small" aria-label="取消当前任务" onClick={onCancel} loading={cancelling}>取消当前任务</Button>
    </div>
  );
}

function interruptFieldLabels(interrupt: PendingInterrupt): string[] {
  return Object.keys(interrupt.requiredFields ?? {}).map((field) => INTERRUPT_FIELD_LABELS[field] ?? field);
}

function interruptFieldSummary(interrupt: PendingInterrupt): string {
  const labels = interruptFieldLabels(interrupt);
  return labels.length > 0 ? labels.join('、') : '补充信息';
}

function interruptOptionLabels(interrupt: PendingInterrupt): string[] {
  return Object.values(interrupt.requiredFields ?? {})
    .flatMap((field) => extractInterruptOptions(field))
    .map((option) => INTERRUPT_OPTION_LABELS[option] ?? option);
}

function extractInterruptOptions(field: unknown): string[] {
  if (Array.isArray(field)) {
    return field.filter((item): item is string => typeof item === 'string');
  }
  if (field && typeof field === 'object' && 'options' in field) {
    const options = (field as { options?: unknown }).options;
    if (Array.isArray(options)) {
      return options.filter((item): item is string => typeof item === 'string');
    }
  }
  return [];
}

function interruptAnswerPlaceholder(interrupt: PendingInterrupt): string {
  const labels = interruptFieldLabels(interrupt);
  const options = interruptOptionLabels(interrupt);
  if (labels.length === 1 && options.length > 0) {
    const preferred = options.includes('水稻') ? '水稻' : options[0];
    return `请输入${labels[0]}，例如“${preferred}”`;
  }
  if (labels.length === 1) {
    return `请输入${labels[0]}`;
  }
  if (labels.length > 1) {
    return `请补充${labels.join('、')}，回复后将继续当前任务`;
  }
  return '请补充当前任务所需信息';
}

function MessageBubble({ message }: { message: ConversationMessage }) {
  const className = message.role === 'user' ? 'message message-user' : 'message message-assistant';
  const shouldShowContent = Boolean(message.content);
  return (
    <div className={className}>
      <div className="message-meta">{message.role === 'user' ? '你' : message.mode === 'sql_query' ? 'SQLQuery' : '主代理'}</div>
      <div className="message-body">
        {message.role === 'assistant' && (message.reasoningRequested || message.reasoningContent) ? (
          <ReasoningBox content={message.reasoningContent ?? ''} complete={message.reasoningComplete} />
        ) : null}
        {message.interruptPrompt ? (
          <InterruptPromptCard interrupt={message.interruptPrompt} />
        ) : shouldShowContent || message.artifactDisplays?.length ? (
          <>
            {shouldShowContent ? <MarkdownText content={message.content} /> : null}
            {message.artifactDisplays?.map((display) => <CapabilityArtifactPanel key={capabilityArtifactDisplayKey(display)} display={display} />)}
          </>
        ) : message.activityText ? (
          <ActivityNotice text={message.activityText} />
        ) : (
          <ActivityNotice text="正在等待回答..." />
        )}
      </div>
    </div>
  );
}

function CapabilityArtifactPanel({ display }: { display: CapabilityArtifactDisplay }) {
  if (display.kind === 'sql_query') {
    return <SqlQueryResultCard result={display.result} />;
  }
  return null;
}

function capabilityArtifactDisplayKey(display: CapabilityArtifactDisplay): string {
  if (display.kind === 'sql_query') {
    return `${display.kind}:${display.result.sourceArtifactIds.join(',')}`;
  }
  return 'capability-artifact';
}

function ActivityNotice({ text }: { text: string }) {
  return (
    <div className="activity-notice" role="status" aria-live="polite">
      <Spin size="small" />
      <Typography.Text type="secondary">{text}</Typography.Text>
    </div>
  );
}

function ReasoningBox({ content, complete }: { content: string; complete?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const collapsed = Boolean(content && complete && !expanded);
  const placeholder = complete ? '本次模型未返回 reasoning_content。' : '等待模型返回 reasoning_content...';
  return (
    <section className={`reasoning-box ${collapsed ? 'reasoning-box-collapsed' : 'reasoning-box-expanded'}`} aria-label="思考内容">
      <div className="reasoning-box-header">
        <span>思考内容</span>
        {content && complete ? (
          <Button
            type="link"
            size="small"
            className="reasoning-toggle"
            aria-label={expanded ? '收起思考内容' : '展开思考内容'}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? '收起' : '展开'}
          </Button>
        ) : null}
      </div>
      <div className="reasoning-box-content">
        {content ? <MarkdownText content={content} /> : <Typography.Text type="secondary">{placeholder}</Typography.Text>}
      </div>
    </section>
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
