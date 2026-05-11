import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { ReloadOutlined } from '@ant-design/icons';
import { Alert, Badge, Button, Card, ConfigProvider, Flex, Input, Layout, Popover, Select, Space, Spin, Switch, Tag, Typography, theme, type ThemeConfig } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { createApiClient, type ApiClient } from './api/client';
import { createBrowserEventSourceFactory, taskEventsUrl, type EventSourceFactory, type TaskEventSubscription } from './api/taskEvents';
import type { ChatMode, ConversationSummaryResponse, MessageResponse, ReasoningEffort, TaskEventEnvelope, UploadFileResponse, UserResponse } from './api/types';
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

interface TransientNotice {
  id: number;
  message: string;
  type: 'success' | 'warning';
}

const CONVERSATION_STORAGE_KEY_PREFIX = 'maf.frontend.conversation_id';
const WAITING_INPUT_CHECK_DELAY_MS = 8_000;
const TRANSIENT_NOTICE_DURATION_MS = 5_000;
const CONVERSATION_AUTO_FOLLOW_THRESHOLD_PX = 32;
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
const AGRICULTURE_THEME: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#2f7d32',
    colorPrimaryHover: '#3f8f44',
    colorPrimaryActive: '#1f5e28',
    colorPrimaryBg: '#e8f3e4',
    colorPrimaryBorder: '#b9d7b4',
    colorInfo: '#2f7d32',
    colorSuccess: '#2f7d32',
    colorLink: '#2f7d32',
    colorBgBase: '#fffaf0',
    colorBgLayout: '#f6f0e4',
    colorBgContainer: '#fffaf0',
    colorBgElevated: '#fffdf7',
    colorBorder: '#e7dcc6',
    colorText: '#263328',
    colorTextSecondary: '#6f725a',
    borderRadius: 12,
    fontFamily: 'Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  },
};

function isConversationViewAtBottom(element: HTMLElement) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= CONVERSATION_AUTO_FOLLOW_THRESHOLD_PX;
}

function App({ apiClient, eventSourceFactory, waitingInputCheckDelayMs = WAITING_INPUT_CHECK_DELAY_MS }: AppProps) {
  const api = useMemo(() => apiClient ?? createApiClient(), [apiClient]);
  const createEventSource = useMemo(() => eventSourceFactory ?? createBrowserEventSourceFactory(), [eventSourceFactory]);
  const [authUser, setAuthUser] = useState<UserResponse | null | undefined>(undefined);
  const [conversationId, setConversationId] = useState('');
  const mode: ChatMode = 'chat';
  const [deepThinking, setDeepThinking] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('medium');
  const [input, setInput] = useState('');
  const [pendingUploads, setPendingUploads] = useState<UploadFileResponse[]>([]);
  const [uploadingFile, setUploadingFile] = useState(false);
  const [draggingUpload, setDraggingUpload] = useState(false);
  const [deletingUploadIds, setDeletingUploadIds] = useState<Set<string>>(() => new Set());
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [taskState, setTaskState] = useState<TaskEventState>(createInitialTaskEventState());
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [currentAssistantId, setCurrentAssistantId] = useState<string | null>(null);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingInterrupt | null>(null);
  const [conversationHistory, setConversationHistory] = useState<ConversationSummaryResponse[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [deletingConversationIds, setDeletingConversationIds] = useState<Set<string>>(() => new Set());
  const [renamingConversationIds, setRenamingConversationIds] = useState<Set<string>>(() => new Set());
  const [transientNotice, setTransientNotice] = useState<TransientNotice | null>(null);
  const subscriptionRef = useRef<TaskEventSubscription | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const conversationListRef = useRef<HTMLDivElement | null>(null);
  const shouldFollowConversationRef = useRef(true);
  const lastAutoFollowConversationIdRef = useRef(conversationId);
  const composingInputRef = useRef(false);
  const taskPresentationModesRef = useRef<Map<string, ChatMode>>(new Map());
  const pendingAssistantPatchesRef = useRef<Map<string, AssistantMessagePatch>>(new Map());
  const transientNoticeIdRef = useRef(0);

  function showTransientNotice(message: string, type: TransientNotice['type'] = 'warning') {
    transientNoticeIdRef.current += 1;
    setTransientNotice({ id: transientNoticeIdRef.current, message, type });
  }

  function clearTransientNotice() {
    setTransientNotice(null);
  }

  useEffect(() => {
    let mounted = true;
    api.me()
      .then((result) => {
        if (!mounted) return;
        setAuthUser(result.user);
        setConversationId(loadOrCreateConversationId(result.user.username));
      })
      .catch(() => {
        if (mounted) setAuthUser(null);
      });
    return () => {
      mounted = false;
    };
  }, [api]);

  useEffect(() => {
    if (!transientNotice) return undefined;
    const timeoutId = window.setTimeout(() => {
      setTransientNotice((current) => (
        current?.id === transientNotice.id ? null : current
      ));
    }, TRANSIENT_NOTICE_DURATION_MS);
    return () => window.clearTimeout(timeoutId);
  }, [transientNotice]);

  const refreshConversationHistory = useCallback(async () => {
    if (!authUser) return;
    setHistoryLoading(true);
    try {
      const result = await api.listConversations();
      setConversationHistory(result.conversations);
    } catch {
      showTransientNotice('历史会话加载失败，请稍后重试。');
    } finally {
      setHistoryLoading(false);
    }
  }, [api, authUser]);

  useEffect(() => {
    if (!authUser) return;
    return () => {
      subscriptionRef.current?.close();
    };
  }, [authUser]);

  useEffect(() => {
    if (!authUser) return;
    void refreshConversationHistory();
  }, [authUser, refreshConversationHistory]);

  const refreshConversationUploads = useCallback(async (targetConversationId = conversationId) => {
    if (!authUser || !targetConversationId) return;
    try {
      const result = await api.listConversationUploads(targetConversationId);
      setPendingUploads(result.uploads);
    } catch {
      setPendingUploads([]);
    }
  }, [api, authUser, conversationId]);

  useEffect(() => {
    void refreshConversationUploads();
  }, [refreshConversationUploads]);

  const active = isTaskActive(taskState.phase);

  const handleConversationScroll = useCallback(() => {
    const conversationList = conversationListRef.current;
    if (!conversationList) return;
    shouldFollowConversationRef.current = isConversationViewAtBottom(conversationList);
  }, []);

  useLayoutEffect(() => {
    const conversationList = conversationListRef.current;
    if (!conversationList) return;

    const conversationChanged = lastAutoFollowConversationIdRef.current !== conversationId;
    if (conversationChanged) {
      lastAutoFollowConversationIdRef.current = conversationId;
      shouldFollowConversationRef.current = true;
    }

    if (messages.length === 0 && !active) return;
    if (!shouldFollowConversationRef.current) return;

    conversationList.scrollTop = conversationList.scrollHeight;
    shouldFollowConversationRef.current = true;
  }, [active, conversationId, currentAssistantId, messages]);

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

  async function handleLogin(user: UserResponse) {
    setAuthUser(user);
    const nextConversationId = loadOrCreateConversationId(user.username);
    setConversationId(nextConversationId);
    setMessages([]);
    setPendingUploads([]);
    setTaskState(createInitialTaskEventState());
    setCurrentTaskId(null);
    setPendingInterrupt(null);
    try {
      const result = await api.listConversations();
      setConversationHistory(result.conversations);
    } catch {
      showTransientNotice('历史会话加载失败，请稍后重试。');
    }
  }

  async function handleLogout() {
    await api.logout().catch(() => undefined);
    subscriptionRef.current?.close();
    setAuthUser(null);
    setConversationId('');
    setMessages([]);
    setConversationHistory([]);
    setPendingUploads([]);
    setCurrentTaskId(null);
    setCurrentAssistantId(null);
    setPendingInterrupt(null);
    setTaskState(createInitialTaskEventState());
    setDeletingConversationIds(new Set());
    setRenamingConversationIds(new Set());
  }

  function handleAccountSettings() {
    showTransientNotice('用户账户设置功能会在后续版本开放。');
  }

  function resetConversationWorkspace(nextConversationId: string) {
    setConversationId(nextConversationId);
    setMessages([]);
    setInput('');
    setPendingUploads([]);
    setCurrentTaskId(null);
    setCurrentAssistantId(null);
    setPendingInterrupt(null);
    setTaskState(createInitialTaskEventState());
    taskPresentationModesRef.current.clear();
    pendingAssistantPatchesRef.current.clear();
  }

  async function handleNewConversation() {
    if (!authUser || active) return;
    const next = createConversationId();
    saveConversationId(authUser.username, next);
    resetConversationWorkspace(next);
  }

  async function handleSelectConversation(nextConversationId: string) {
    if (!authUser || active) return;
    saveConversationId(authUser.username, nextConversationId);
    if (nextConversationId !== conversationId) {
      setConversationId(nextConversationId);
    }
    setTaskState(createInitialTaskEventState());
    setCurrentTaskId(null);
    setCurrentAssistantId(null);
    setPendingInterrupt(null);
    subscriptionRef.current?.close();
    try {
      const result = await api.listConversationMessages(nextConversationId);
      setMessages(result.messages.map(messageFromHistory).filter((message): message is ConversationMessage => message !== null));
    } catch {
      showTransientNotice('历史消息加载失败，请稍后重试。');
    }
  }

  async function handleDeleteConversation(targetConversationId: string) {
    if (!authUser || deletingConversationIds.has(targetConversationId)) return;
    const conversation = conversationHistory.find((item) => item.conversation_id === targetConversationId);
    const title = conversation?.title?.trim() || targetConversationId.slice(0, 18);
    if (!window.confirm(`确定删除历史会话“${title}”吗？如果该会话有正在进行中的任务，系统会自动停止后再删除。`)) {
      return;
    }
    clearTransientNotice();
    setDeletingConversationIds((current) => new Set(current).add(targetConversationId));
    try {
      const result = await api.deleteConversation(targetConversationId);
      setConversationHistory((current) => current.filter((item) => item.conversation_id !== targetConversationId));
      if (targetConversationId === conversationId) {
        subscriptionRef.current?.close();
        subscriptionRef.current = null;
        const next = createConversationId();
        saveConversationId(authUser.username, next);
        resetConversationWorkspace(next);
      }
      showTransientNotice(result.cancelled_task_ids.length > 0
        ? '历史会话已删除，相关运行中任务已自动停止。'
        : '历史会话已删除。', 'success');
    } catch (error) {
      showTransientNotice(friendlyError(error));
    } finally {
      setDeletingConversationIds((current) => {
        const next = new Set(current);
        next.delete(targetConversationId);
        return next;
      });
    }
  }

  async function handleRenameConversation(targetConversationId: string) {
    if (!authUser || renamingConversationIds.has(targetConversationId)) return;
    const conversation = conversationHistory.find((item) => item.conversation_id === targetConversationId);
    const currentTitle = conversation?.title?.trim() || targetConversationId.slice(0, 18);
    const nextTitle = window.prompt('请输入新的会话名称', currentTitle);
    if (nextTitle === null) return;
    const trimmedTitle = nextTitle.trim();
    if (!trimmedTitle) {
      showTransientNotice('会话名称不能为空。');
      return;
    }
    clearTransientNotice();
    setRenamingConversationIds((current) => new Set(current).add(targetConversationId));
    try {
      const renamed = await api.renameConversation(targetConversationId, trimmedTitle);
      setConversationHistory((current) => current.map((item) => (
        item.conversation_id === targetConversationId ? renamed : item
      )));
    } catch (error) {
      showTransientNotice(friendlyError(error));
    } finally {
      setRenamingConversationIds((current) => {
        const next = new Set(current);
        next.delete(targetConversationId);
        return next;
      });
    }
  }

  async function handleSubmit() {
    const content = input.trim();
    if (!authUser || !conversationId || !content || active) return;
    clearTransientNotice();
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
        content,
        mode,
        deepThinking,
        reasoningEffort,
        metadata: pendingUploads.length > 0 ? { upload_ids: pendingUploads.map((upload) => upload.upload_id) } : undefined,
      });
      taskPresentationModesRef.current.set(accepted.task_id, mode);
      setCurrentTaskId(accepted.task_id);
      subscribeToTask(accepted.task_id, assistantMessage.id);
    } catch (error) {
      setTaskState((state) => markTaskFailed(state, friendlyError(error)));
      showTransientNotice(friendlyError(error));
    }
  }

  function isComposerImeConfirming(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean {
    const reactEvent = event as ReactKeyboardEvent<HTMLTextAreaElement> & { isComposing?: boolean };
    const nativeEvent = event.nativeEvent as KeyboardEvent & { keyCode?: number };
    return composingInputRef.current || reactEvent.isComposing === true || nativeEvent.isComposing === true || nativeEvent.keyCode === 229;
  }

  async function handleUploadFile(file: File | undefined) {
    if (!authUser || !conversationId || !file || interactionLocked || uploadingFile) return;
    clearTransientNotice();
    setUploadingFile(true);
    try {
      const uploaded = await api.uploadConversationFile(conversationId, file);
      setPendingUploads((current) => [...current, uploaded]);
    } catch (error) {
      showTransientNotice(friendlyError(error));
    } finally {
      setUploadingFile(false);
      if (uploadInputRef.current) {
        uploadInputRef.current.value = '';
      }
    }
  }

  async function handleDeleteUpload(upload: UploadFileResponse) {
    if (!conversationId || deletingUploadIds.has(upload.upload_id)) return;
    setDeletingUploadIds((current) => new Set(current).add(upload.upload_id));
    try {
      await api.deleteConversationUpload(conversationId, upload.upload_id);
      setPendingUploads((current) => current.filter((item) => item.upload_id !== upload.upload_id));
    } catch (error) {
      showTransientNotice(friendlyError(error));
    } finally {
      setDeletingUploadIds((current) => {
        const next = new Set(current);
        next.delete(upload.upload_id);
        return next;
      });
    }
  }

  function isFileDrag(event: DragEvent<HTMLDivElement>): boolean {
    const types = Array.from(event.dataTransfer.types ?? []);
    return types.length === 0 || types.includes('Files') || event.dataTransfer.files.length > 0;
  }

  function canAcceptDraggedUpload(): boolean {
    return Boolean(authUser && conversationId && !interactionLocked && !uploadingFile);
  }

  function handleUploadDragEnter(event: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    if (canAcceptDraggedUpload()) setDraggingUpload(true);
  }

  function handleUploadDragOver(event: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = canAcceptDraggedUpload() ? 'copy' : 'none';
    if (canAcceptDraggedUpload()) setDraggingUpload(true);
  }

  function handleUploadDragLeave(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return;
    setDraggingUpload(false);
  }

  function handleUploadDrop(event: DragEvent<HTMLDivElement>) {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    setDraggingUpload(false);
    if (!canAcceptDraggedUpload()) return;
    void handleUploadFile(event.dataTransfer.files?.[0]);
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
    } catch (error) {
      setTaskState((state) => markTaskFailed(state, friendlyError(error)));
      showTransientNotice(friendlyError(error));
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
      }
      return next;
    });
    if (event.event_type === 'task.completed') {
      updateAssistantMessage(assistantId, { reasoningComplete: true });
      void loadArtifacts(taskId, assistantId);
    }
  }

  async function handleEventStreamError(taskId: string, assistantId: string) {
    showTransientNotice('事件流暂时中断，正在尝试查询任务状态。');
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
      showTransientNotice('事件流中断，任务状态暂时无法确认。');
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
      void refreshConversationHistory();
    } catch {
      setTaskState((state) => markTaskCompleted(state, '任务已完成，但结果加载失败'));
      showTransientNotice('结果加载失败，可稍后重试。');
    }
  }

  async function handleCancel() {
    if (!currentTaskId) return;
    const taskId = currentTaskId;
    const previousPhase = taskState.phase;
    setTaskState((state) => ({ ...state, phase: 'cancelling', statusText: '取消请求已发送' }));
    try {
      await api.cancelTask(taskId);
      setPendingInterrupt(null);
      setCurrentTaskId(null);
      subscriptionRef.current?.close();
      setTaskState((state) => ({ ...state, phase: 'cancelled', statusText: '任务已取消', errorMessage: null }));
    } catch (error) {
      const message = friendlyError(error);
      showTransientNotice(message);
      setTaskState((state) => ({
        ...state,
        phase: previousPhase,
        statusText: '取消任务失败，请稍后重试',
        errorMessage: message,
      }));
    }
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
  const composerMenuContent = (
    <Space direction="vertical" size="middle" className="composer-menu">
      <Button
        block
        aria-label="选择 JSON 或 CSV 文件"
        onClick={() => uploadInputRef.current?.click()}
        disabled={interactionLocked || uploadingFile}
        loading={uploadingFile}
      >
        上传 JSON / CSV 文件
      </Button>
      <Space size="small" align="center" className="composer-menu-row">
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
      <Space size="small" align="center" className="composer-menu-row">
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
      {active && currentTaskId ? (
        <Button
          danger
          block
          aria-label="取消任务"
          onClick={handleCancel}
          loading={taskState.phase === 'cancelling'}
        >
          取消任务
        </Button>
      ) : null}
    </Space>
  );

  if (authUser === undefined) {
    return (
      <ConfigProvider locale={zhCN} theme={AGRICULTURE_THEME}>
        <Layout className="app-shell auth-shell">
          <Space direction="vertical" align="center">
            <Spin />
            <Typography.Text type="secondary">正在检查登录状态...</Typography.Text>
          </Space>
        </Layout>
      </ConfigProvider>
    );
  }

  if (authUser === null) {
    return (
      <ConfigProvider locale={zhCN} theme={AGRICULTURE_THEME}>
        <LoginPage api={api} onLogin={handleLogin} />
      </ConfigProvider>
    );
  }

  return (
    <ConfigProvider locale={zhCN} theme={AGRICULTURE_THEME}>
      <Layout className="app-shell app-chat-layout">
        <aside className="app-sidebar" aria-label="历史会话侧边栏">
          <div className="sidebar-brand">
            <Typography.Title level={3} className="app-title">小奥Agent</Typography.Title>
            <Typography.Text type="secondary">AI育种助手</Typography.Text>
          </div>
          <ConversationHistoryPanel
            conversations={conversationHistory}
            activeConversationId={conversationId}
            loading={historyLoading}
            interactionLocked={interactionLocked}
            onRefresh={refreshConversationHistory}
            onNewConversation={handleNewConversation}
            onSelectConversation={handleSelectConversation}
            onDeleteConversation={handleDeleteConversation}
            onRenameConversation={handleRenameConversation}
            deletingConversationIds={deletingConversationIds}
            renamingConversationIds={renamingConversationIds}
          />
          <SidebarUserCard
            user={authUser}
            onAccountSettings={handleAccountSettings}
            onLogout={handleLogout}
          />
        </aside>
        {transientNotice ? (
          <div className="toast-notice" role="status" aria-live="polite">
            <Alert
              type={transientNotice.type}
              showIcon
              closable
              onClose={clearTransientNotice}
              message={transientNotice.message}
            />
          </div>
        ) : null}
        <main className="chat-workspace" aria-label="对话工作区">
          <div className="task-status-floating" aria-label="任务进程悬浮胶囊">
            <TaskStatusDropdown state={taskState} />
          </div>
          <section className="app-content" aria-label="当前对话面板">
            <div ref={conversationListRef} className="conversation-list" aria-label="对话内容" onScroll={handleConversationScroll}>
              {messages.length === 0 ? <EmptyWelcome /> : messages.map((message) => <MessageBubble key={message.id} message={message} />)}
              {active && currentAssistantId && !taskState.assistantText && !taskState.currentActivityText ? (
                <div className="assistant-pending"><Spin size="small" /> <span>等待任务事件...</span></div>
              ) : null}
            </div>
            <div
              className={`chat-floating-stack${draggingUpload ? ' chat-floating-stack-dragging' : ''}`}
              role="region"
              aria-label="拖拽上传区"
              onDragEnter={handleUploadDragEnter}
              onDragOver={handleUploadDragOver}
              onDragLeave={handleUploadDragLeave}
              onDrop={handleUploadDrop}
            >
              <div className="chat-upload-drop-hint" aria-hidden={!draggingUpload}>释放文件以上传到当前对话</div>
              {pendingInterrupt ? <InterruptInputBanner interrupt={pendingInterrupt} onCancel={handleCancel} cancelling={taskState.phase === 'cancelling'} /> : null}
              <Card
                className={`composer-card floating-composer${draggingUpload ? ' floating-composer-dragging' : ''}`}
                role="region"
                aria-label="悬浮发送栏"
              >
                <Space direction="vertical" size="small" className="composer-space">
                  {pendingUploads.length > 0 ? (
                    <Space size={[8, 8]} wrap aria-label="暂存区文件列表" className="composer-attachments">
                      {pendingUploads.map((upload) => (
                        <Tag key={upload.upload_id} className="upload-file-tag">
                          {upload.filename}
                          {typeof upload.preview.row_count === 'number' ? ` · ${upload.preview.row_count} 行` : ''}
                          {upload.preview.columns.length > 0 ? ` · ${upload.preview.columns.slice(0, 3).join('/')}` : ''}
                          <Button
                            type="link"
                            danger
                            size="small"
                            aria-label={`删除文件 ${upload.filename}`}
                            loading={deletingUploadIds.has(upload.upload_id)}
                            disabled={interactionLocked}
                            onClick={() => void handleDeleteUpload(upload)}
                          >
                            删除
                          </Button>
                        </Tag>
                      ))}
                    </Space>
                  ) : null}
                  <div className="send-row" role="group" aria-label="消息发送栏">
                    <Input.TextArea
                      aria-label="请输入问题"
                      value={input}
                      onChange={(event) => setInput(event.target.value)}
                      onCompositionStart={() => {
                        composingInputRef.current = true;
                      }}
                      onCompositionEnd={() => {
                        composingInputRef.current = false;
                      }}
                      onPressEnter={(event) => {
                        if (!event.shiftKey && !isComposerImeConfirming(event)) {
                          event.preventDefault();
                          void handleSubmit();
                        }
                      }}
                      placeholder={inputPlaceholder}
                      autoSize={{ minRows: 1, maxRows: 5 }}
                      disabled={active && taskState.phase !== 'cancelling'}
                    />
                    <Popover
                      content={composerMenuContent}
                      trigger="click"
                      placement="topRight"
                      overlayClassName="composer-menu-popover"
                    >
                      <Button
                        aria-label="打开输入功能菜单"
                        className="composer-action-button composer-plus-button"
                      >
                        +
                      </Button>
                    </Popover>
                    <Button
                      type="primary"
                      aria-label="发送"
                      className="composer-send-button"
                      onClick={handleSubmit}
                      disabled={!input.trim() || active || uploadingFile}
                    >
                      发送
                    </Button>
                  </div>
                  <input
                    ref={uploadInputRef}
                    className="file-input-hidden"
                    aria-label="上传 JSON 或 CSV 文件"
                    type="file"
                    accept=".json,.csv,application/json,text/csv"
                    disabled={interactionLocked || uploadingFile}
                    onChange={(event) => void handleUploadFile(event.target.files?.[0])}
                  />
                </Space>
              </Card>
            </div>
          </section>
        </main>
      </Layout>
    </ConfigProvider>
  );
}

function LoginPage({ api, onLogin }: { api: ApiClient; onLogin: (user: UserResponse) => void | Promise<void> }) {
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [captchaCode, setCaptchaCode] = useState('');
  const [captchaId, setCaptchaId] = useState('');
  const [captchaSvg, setCaptchaSvg] = useState('');
  const [loadingCaptcha, setLoadingCaptcha] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshCaptcha = useCallback(async () => {
    setLoadingCaptcha(true);
    try {
      const challenge = await api.createCaptcha();
      setCaptchaId(challenge.captcha_id);
      setCaptchaSvg(challenge.image_svg);
      setCaptchaCode('');
    } catch {
      setError('验证码加载失败，请刷新重试。');
    } finally {
      setLoadingCaptcha(false);
    }
  }, [api]);

  useEffect(() => {
    void refreshCaptcha();
  }, [refreshCaptcha]);

  const trimmedUsername = username.trim();
  const passwordHasLetterAndDigit = /[A-Za-z]/.test(password) && /\d/.test(password);
  const passwordPolicyOk = password.length >= 8 && passwordHasLetterAndDigit;
  const registerPasswordMismatch = authMode === 'register' && confirmPassword.length > 0 && password !== confirmPassword;
  const canSubmit = authMode === 'login'
    ? Boolean(trimmedUsername && password && captchaCode.length === 4 && captchaId)
    : Boolean(trimmedUsername && passwordPolicyOk && password === confirmPassword && captchaCode.length === 4 && captchaId);

  function switchAuthMode(nextMode: 'login' | 'register') {
    setAuthMode(nextMode);
    setPassword('');
    setConfirmPassword('');
    setCaptchaCode('');
    setError(null);
  }

  async function submitAuth() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = authMode === 'login' ? await api.login({
        username: trimmedUsername,
        password,
        captchaId,
        captchaCode,
      }) : await api.register({
        username: trimmedUsername,
        password,
        captchaId,
        captchaCode,
      });
      await onLogin(result.user);
    } catch {
      setError(authMode === 'login' ? '登录失败，请检查用户名、密码和验证码。' : '创建用户失败，请检查用户名、密码规则和验证码。');
      void refreshCaptcha();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Layout className="app-shell auth-shell">
      <Card className="login-card" title={authMode === 'login' ? '登录小奥Agent' : '创建小奥Agent用户'}>
        <Space direction="vertical" size="middle" className="login-form">
          {error ? <Alert type="error" showIcon message={error} /> : null}
          <Input
            aria-label="用户名"
            placeholder="用户名"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
          <Input.Password
            aria-label="密码"
            placeholder="密码"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={authMode === 'login' ? 'current-password' : 'new-password'}
          />
          {authMode === 'register' ? (
            <>
              <Input.Password
                aria-label="确认密码"
                placeholder="确认密码"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                onPressEnter={() => void submitAuth()}
                autoComplete="new-password"
              />
              <Typography.Text type={passwordPolicyOk ? 'success' : 'secondary'}>
                密码至少 8 位，并且必须同时包含字母和数字。
              </Typography.Text>
              {registerPasswordMismatch ? <Typography.Text type="danger">两次输入的密码不一致。</Typography.Text> : null}
            </>
          ) : null}
          <Space align="center" className="captcha-row">
            <Input
              aria-label="4位验证码"
              placeholder="4位验证码"
              value={captchaCode}
              maxLength={4}
              onChange={(event) => setCaptchaCode(event.target.value.replace(/\D/g, '').slice(0, 4))}
              onPressEnter={() => void submitAuth()}
            />
            <div className="captcha-image" aria-label="验证码图片" dangerouslySetInnerHTML={{ __html: captchaSvg }} />
            <Button onClick={() => void refreshCaptcha()} loading={loadingCaptcha}>刷新</Button>
          </Space>
          <Button
            type="primary"
            block
            onClick={() => void submitAuth()}
            loading={submitting}
            disabled={!canSubmit}
          >
            {authMode === 'login' ? '登录' : '创建用户并登录'}
          </Button>
          {authMode === 'login' ? (
            <Button type="link" block onClick={() => switchAuthMode('register')}>创建新用户</Button>
          ) : (
            <Button type="link" block onClick={() => switchAuthMode('login')}>返回登录</Button>
          )}
        </Space>
      </Card>
    </Layout>
  );
}

function ConversationHistoryPanel({
  conversations,
  activeConversationId,
  loading,
  interactionLocked,
  deletingConversationIds,
  renamingConversationIds,
  onRefresh,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation,
  onRenameConversation,
}: {
  conversations: ConversationSummaryResponse[];
  activeConversationId: string;
  loading: boolean;
  interactionLocked: boolean;
  deletingConversationIds: Set<string>;
  renamingConversationIds: Set<string>;
  onRefresh: () => void;
  onNewConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => void;
  onRenameConversation: (conversationId: string) => void;
}) {
  return (
    <Card
      className="history-card"
      title="历史会话"
      extra={(
        <Space size="small">
          <Button
            className="history-refresh-button"
            size="small"
            type="text"
            shape="circle"
            aria-label="刷新历史会话"
            title="刷新"
            icon={<ReloadOutlined aria-hidden="true" />}
            onClick={onRefresh}
            loading={loading}
          />
          <Button size="small" type="primary" onClick={onNewConversation} disabled={interactionLocked}>新建对话</Button>
        </Space>
      )}
    >
      {conversations.length === 0 ? (
        <Typography.Text type="secondary">暂无历史会话。当前对话发送消息后会自动归档到你的用户名下。</Typography.Text>
      ) : (
        <div className="history-list" role="list" aria-label="历史会话列表">
          {conversations.map((conversation) => {
            const title = conversation.title?.trim() || conversation.conversation_id.slice(0, 18);
            const active = conversation.conversation_id === activeConversationId;
            return (
              <div key={conversation.conversation_id} className="history-row" role="listitem">
                <button
                  type="button"
                  className={`history-item${active ? ' history-item-active' : ''}`}
                  disabled={interactionLocked}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => onSelectConversation(conversation.conversation_id)}
                >
                  <span className="history-item-title">{title}</span>
                </button>
                <div className="history-actions" aria-label={`历史会话操作 ${title}`}>
                  <Button
                    type="text"
                    size="small"
                    aria-label={`重命名历史会话 ${title}`}
                    loading={renamingConversationIds.has(conversation.conversation_id)}
                    disabled={loading}
                    onClick={() => onRenameConversation(conversation.conversation_id)}
                  >
                    重命名
                  </Button>
                  <Button
                    danger
                    type="text"
                    size="small"
                    aria-label={`删除历史会话 ${title}`}
                    loading={deletingConversationIds.has(conversation.conversation_id)}
                    disabled={loading}
                    onClick={() => onDeleteConversation(conversation.conversation_id)}
                  >
                    删除
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function SidebarUserCard({
  user,
  onAccountSettings,
  onLogout,
}: {
  user: UserResponse;
  onAccountSettings: () => void;
  onLogout: () => void;
}) {
  return (
    <Card
      className="sidebar-user-card"
      role="region"
      aria-label="用户信息与账户操作"
      title="用户信息"
    >
      <Space direction="vertical" size="small" className="sidebar-user-stack">
        <div>
          <Typography.Text type="secondary">当前用户</Typography.Text>
          <Typography.Text strong className="sidebar-username">{user.username}</Typography.Text>
        </div>
        <Space.Compact block>
          <Button block onClick={onAccountSettings}>用户账户设置</Button>
          <Button block danger onClick={onLogout}>退出登录</Button>
        </Space.Compact>
      </Space>
    </Card>
  );
}

function EmptyWelcome() {
  return (
    <div className="empty-welcome">
      <Typography.Title level={4}>开始一次业务问答</Typography.Title>
      <Typography.Paragraph type="secondary">直接描述你的问题即可。</Typography.Paragraph>
    </div>
  );
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
          <Space size={[6, 6]} wrap>{fieldLabels.map((field) => <Tag key={field} color="green">{field}</Tag>)}</Space>
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
  if (display.kind === 'file') {
    return <FileArtifactCard result={display.result} />;
  }
  return null;
}

function capabilityArtifactDisplayKey(display: CapabilityArtifactDisplay): string {
  if (display.kind === 'sql_query') {
    return `${display.kind}:${display.result.sourceArtifactIds.join(',')}`;
  }
  if (display.kind === 'file') {
    return `${display.kind}:${display.result.artifactId}`;
  }
  return 'capability-artifact';
}

function FileArtifactCard({ result }: { result: Extract<CapabilityArtifactDisplay, { kind: 'file' }>['result'] }) {
  return (
    <Card size="small" className="capability-card" title="生成文件">
      <Space direction="vertical" size="small">
        <Space wrap>
          <Typography.Text strong>{result.filename}</Typography.Text>
          <Tag>{result.mimeType}</Tag>
          {result.archiveFormat === 'zip' ? <Tag color="green">ZIP</Tag> : null}
          {result.sourceFileCount && result.sourceFileCount > 1 ? <Tag>{result.sourceFileCount} 个文件</Tag> : null}
        </Space>
        <Typography.Text type="secondary">{result.summary}</Typography.Text>
        <Button href={result.downloadUrl} target="_blank" rel="noreferrer">
          下载
        </Button>
      </Space>
    </Card>
  );
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

function TaskStatusDropdown({ state }: { state: TaskEventState }) {
  const type = taskStatusAlertType(state);
  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      title="任务进程"
      content={(
        <div className="task-status-popover">
          <Alert
            className="task-status-detail"
            type={type}
            showIcon
            message={state.statusText}
            description={state.errorMessage ?? undefined}
          />
          {state.currentActivityText ? <Typography.Text type="secondary">{state.currentActivityText}</Typography.Text> : null}
        </div>
      )}
    >
      <Button size="small" className="task-status-capsule" aria-label="任务进程">
        <Space size="small">
          <Badge status={taskStatusBadgeStatus(state)} />
          <span>任务进程</span>
          <Tag color={taskStatusTagColor(state)}>{taskStatusPhaseLabel(state)}</Tag>
        </Space>
      </Button>
    </Popover>
  );
}

function taskStatusAlertType(state: TaskEventState): 'success' | 'info' | 'warning' | 'error' {
  if (state.phase === 'failed') return 'error';
  if (state.phase === 'cancelled') return 'warning';
  if (state.phase === 'completed') return 'success';
  return 'info';
}

function taskStatusBadgeStatus(state: TaskEventState): 'success' | 'processing' | 'default' | 'error' | 'warning' {
  if (state.phase === 'failed') return 'error';
  if (state.phase === 'cancelled' || state.phase === 'cancelling' || state.phase === 'waiting_for_input') return 'warning';
  if (isTaskActive(state.phase)) return 'processing';
  if (state.phase === 'completed') return 'success';
  return 'default';
}

function taskStatusTagColor(state: TaskEventState): string {
  if (state.phase === 'failed') return 'red';
  if (state.phase === 'cancelled' || state.phase === 'cancelling' || state.phase === 'waiting_for_input') return 'orange';
  if (isTaskActive(state.phase)) return 'processing';
  if (state.phase === 'completed') return 'green';
  return 'default';
}

function taskStatusPhaseLabel(state: TaskEventState): string {
  if (state.phase === 'idle') return '空闲';
  if (state.phase === 'completed') return '完成';
  if (state.phase === 'failed') return '失败';
  if (state.phase === 'cancelled') return '已取消';
  if (state.phase === 'waiting_for_input') return '待补充';
  if (state.phase === 'cancelling') return '停止中';
  return '运行中';
}

function messageFromHistory(message: MessageResponse): ConversationMessage | null {
  if (message.role !== 'user' && message.role !== 'assistant') return null;
  return {
    id: message.message_id,
    role: message.role,
    content: message.content,
    mode: 'chat',
    finalContentLoaded: message.role === 'assistant',
  };
}

function conversationStorageKey(username: string): string {
  return `${CONVERSATION_STORAGE_KEY_PREFIX}.${username}`;
}

function loadOrCreateConversationId(username: string): string {
  const existing = localStorage.getItem(conversationStorageKey(username));
  if (existing) return existing;
  const created = createConversationId();
  saveConversationId(username, created);
  return created;
}

function saveConversationId(username: string, conversationId: string): void {
  localStorage.setItem(conversationStorageKey(username), conversationId);
}

function createConversationId(): string {
  return `conv-web-${crypto.randomUUID?.() ?? Math.random().toString(16).slice(2)}`;
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
