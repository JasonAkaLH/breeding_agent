import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react';
import { CopyOutlined, ExclamationCircleFilled, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, ConfigProvider, Drawer, Flex, Input, Layout, Popover, Select, Space, Spin, Switch, Tag, Typography, theme, type ThemeConfig } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import { createApiClient, type ApiClient } from './api/client';
import { createFetchTaskEventSourceFactory, taskEventsUrl, type EventSourceFactory, type TaskEventSubscription } from './api/taskEvents';
import type { AuthTokenResponse, ChatMode, ConversationSummaryResponse, MessageResponse, ModelEdition, ModelEditionOption, ReasoningEffort, TaskEventEnvelope, TaskSummaryResponse, UploadFileResponse, UserResponse } from './api/types';
import { parseAssistantTextArtifact, parseCapabilityArtifactDisplays, summarizeCapabilityArtifactDisplays, type CapabilityArtifactDisplay } from './domain/artifacts';
import { deriveSlashCommands, isSlashInput, parseDirectSlashCommand, slashMenuCandidates, slashSubmitIntent, type SlashCommand } from './domain/slashCommands';
import { pickWelcomePrompt } from './domain/welcomePrompts';
import {
  applyTaskEvent,
  createInitialTaskEventState,
  createRestoringTaskState,
  createSubmittingTaskState,
  isTaskActive,
  markTaskCompleted,
  markTaskFailed,
  markWaitingInputRequired,
  taskProgressDisplayText,
  type SkillStatusLine,
  type TaskEventState,
} from './domain/taskEvents';
import { DataQueryResultCard } from './components/DataQueryResultCard';
import { MarkdownText } from './components/MarkdownText';
import SlashCommandMenu from './components/SlashCommandMenu';
import './styles.css';

const INPUT_MENU_BUTTON_IMAGE = '/pics/input-menu-plus-button.svg';
const SEND_BUTTON_IMAGE = '/pics/send-up-arrow-button.svg?v=20260511-arrow-balanced';
const ACCOUNT_SETTINGS_BUTTON_IMAGE = '/pics/account-settings-gear-button.svg?v=20260511-gear-visible';

interface AppProps {
  apiClient?: ApiClient;
  eventSourceFactory?: EventSourceFactory;
  waitingInputCheckDelayMs?: number;
}

type MessageRole = 'user' | 'assistant';
type ActivityNoticeStatus = 'pending' | 'failed' | 'cancelled';

interface ConversationMessage {
  id: string;
  role: MessageRole;
  content: string;
  mode: ChatMode;
  reasoningRequested?: boolean;
  reasoningComplete?: boolean;
  reasoningContent?: string;
  activityText?: string;
  activityStatus?: ActivityNoticeStatus;
  skillStatuses?: SkillStatusLine[];
  artifactDisplays?: CapabilityArtifactDisplay[];
  finalContentLoaded?: boolean;
  replyCompleted?: boolean;
  interruptPrompt?: PendingInterrupt;
}

type AssistantMessagePatch = Partial<Pick<ConversationMessage, 'content' | 'mode' | 'reasoningRequested' | 'reasoningComplete' | 'reasoningContent' | 'activityText' | 'activityStatus' | 'skillStatuses' | 'artifactDisplays' | 'finalContentLoaded' | 'replyCompleted' | 'interruptPrompt'>>;
type FileArtifactResult = Extract<CapabilityArtifactDisplay, { kind: 'file' }>['result'];

interface PendingInterrupt {
  taskId: string;
  interruptId: string;
  question: string;
  requiredFields: Record<string, unknown>;
  mode: ChatMode;
  naturalLanguage?: boolean;
}

interface SheetSelectionField {
  required_upload_ids: string[];
  options_by_upload_id: Record<string, string[]>;
  labels_by_upload_id?: Record<string, string>;
}

interface SlotCollectionRefSlot {
  name: string;
  label: string;
  type: string;
  status: string;
  requiredNow: boolean;
}

interface SlotCollectionRefField {
  collectionId: string;
  slots: SlotCollectionRefSlot[];
}

interface TransientNotice {
  id: number;
  message: string;
  type: 'success' | 'warning';
}

const COMPOSER_READY_PHASES = new Set(['idle', 'completed', 'failed', 'cancelled']);
const CANCELLATION_OR_TERMINAL_PHASES = new Set(['cancelling', 'completed', 'failed', 'cancelled']);
const INTERACTIVE_FOCUS_SELECTOR = [
  'button',
  'input',
  'select',
  'a[href]',
  '[contenteditable="true"]',
  '[role="button"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[role="combobox"]',
  '[role="dialog"]',
  '[role="listbox"]',
  '[role="textbox"]',
  '.ant-popover',
  '.ant-select-dropdown',
  '.ant-dropdown',
  '.ant-modal',
  '.composer-menu-popover',
  '.history-row',
  '.history-actions',
].join(',');

function resolveComposerTextArea(
  textAreaRef: RefObject<TextAreaRef | null>,
  composerRoot: HTMLElement | null,
): HTMLTextAreaElement | null {
  return textAreaRef.current?.resizableTextArea?.textArea
    ?? composerRoot?.querySelector<HTMLTextAreaElement>('textarea[aria-label="请输入问题"]')
    ?? null;
}

function isHiddenMeasurementTextArea(element: Element): boolean {
  return element.getAttribute('aria-hidden') === 'true' || element.getAttribute('name') === 'hiddenTextarea';
}

function canAutoFocusComposer(textArea: HTMLTextAreaElement, composerRoot: HTMLElement | null): boolean {
  const activeElement = document.activeElement;
  if (!activeElement || activeElement === document.body || activeElement === textArea || isHiddenMeasurementTextArea(activeElement)) {
    return true;
  }
  if (composerRoot?.contains(activeElement)) {
    return Boolean(activeElement.closest('.composer-send-button')) && !activeElement.closest('.composer-stop-button');
  }
  return activeElement.closest(INTERACTIVE_FOCUS_SELECTOR) === null;
}

function focusTextAreaWithoutScroll(textArea: HTMLTextAreaElement) {
  try {
    textArea.focus({ preventScroll: true });
  } catch {
    textArea.focus();
  }
}

const AUTH_TOKEN_STORAGE_KEY = 'maf.frontend.access_token';
const CONVERSATION_STORAGE_KEY_PREFIX = 'maf.frontend.conversation_id';
const WAITING_INPUT_CHECK_DELAY_MS = 8_000;
const WAITING_INTERRUPT_RETRY_DELAY_MS = 250;
const WAITING_INTERRUPT_MAX_RETRIES = 6;
const EVENT_STREAM_RECONNECT_DELAY_MS = 1_000;
const CANCEL_RECONCILE_DELAY_MS = 250;
const CANCEL_RECONCILE_MAX_ATTEMPTS = 10;
const TRANSIENT_NOTICE_DURATION_MS = 5_000;
const CONVERSATION_AUTO_FOLLOW_THRESHOLD_PX = 32;
const ACTIVE_TASK_STATUSES = new Set(['accepted', 'planning', 'running', 'cancelling']);
const TERMINAL_TASK_EVENT_TYPES = new Set(['task.completed', 'task.failed', 'task.cancelled']);
const TERMINAL_TASK_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const REASONING_EFFORT_OPTIONS: { label: string; value: ReasoningEffort }[] = [
  { label: '最低', value: 'minimal' },
  { label: '高', value: 'high' },
  { label: '最高', value: 'max' },
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
  const [authUser, setAuthUser] = useState<UserResponse | null | undefined>(undefined);
  const tokenRef = useRef<string | null>(readStoredAccessToken());
  const clearStoredAuth = useCallback(() => {
    tokenRef.current = null;
    writeStoredAccessToken(null);
  }, []);
  const api = useMemo(() => apiClient ?? createApiClient({
    authHeaderProvider: () => tokenRef.current,
    onUnauthorized: () => {
      clearStoredAuth();
      setAuthUser(null);
    },
  }), [apiClient, clearStoredAuth]);
  const createEventSource = useMemo(() => eventSourceFactory ?? createFetchTaskEventSourceFactory({
    authHeaderProvider: () => tokenRef.current,
  }), [eventSourceFactory]);
  const [conversationId, setConversationId] = useState('');
  const mode: ChatMode = 'chat';
  const [modelEditionOptions, setModelEditionOptions] = useState<ModelEditionOption[]>([]);
  const [defaultModelEdition, setDefaultModelEdition] = useState<ModelEdition | null>(null);
  const [modelEdition, setModelEdition] = useState<ModelEdition | null>(null);
  const [deepThinking, setDeepThinking] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('minimal');
  const [input, setInput] = useState('');
  const [skillCommands, setSkillCommands] = useState<SlashCommand[]>([]);
  const [slashMenuOpen, setSlashMenuOpen] = useState(false);
  const [slashMenuActiveIndex, setSlashMenuActiveIndex] = useState(0);
  const [selectedSkillCommand, setSelectedSkillCommand] = useState<SlashCommand | null>(null);
  const [pendingUploads, setPendingUploads] = useState<UploadFileResponse[]>([]);
  const [fileDrawerOpen, setFileDrawerOpen] = useState(false);
  const [uploadingFile, setUploadingFile] = useState(false);
  const [draggingUpload, setDraggingUpload] = useState(false);
  const [deletingUploadIds, setDeletingUploadIds] = useState<Set<string>>(() => new Set());
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [taskState, setTaskState] = useState<TaskEventState>(createInitialTaskEventState());
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [currentAssistantId, setCurrentAssistantId] = useState<string | null>(null);
  const currentTaskIdRef = useRef<string | null>(null);
  const currentAssistantIdRef = useRef<string | null>(null);
  const taskPhaseRef = useRef(taskState.phase);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingInterrupt | null>(null);
  const [conversationHistory, setConversationHistory] = useState<ConversationSummaryResponse[]>([]);
  const [restoredWorkspaceConversationId, setRestoredWorkspaceConversationId] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [deletingConversationIds, setDeletingConversationIds] = useState<Set<string>>(() => new Set());
  const [renamingConversationIds, setRenamingConversationIds] = useState<Set<string>>(() => new Set());
  const [transientNotice, setTransientNotice] = useState<TransientNotice | null>(null);
  const subscriptionRef = useRef<TaskEventSubscription | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const conversationListRef = useRef<HTMLDivElement | null>(null);
  const composerRootRef = useRef<HTMLDivElement | null>(null);
  const composerTextAreaRef = useRef<TextAreaRef | null>(null);
  const lastComposerAutofocusKeyRef = useRef<string | null>(null);
  const shouldFollowConversationRef = useRef(true);
  const lastAutoFollowConversationIdRef = useRef(conversationId);
  const conversationIdRef = useRef(conversationId);
  const restoreGenerationRef = useRef(0);
  const initializedWorkspaceConversationIdRef = useRef<string | null>(null);
  const composingInputRef = useRef(false);
  const taskPresentationModesRef = useRef<Map<string, ChatMode>>(new Map());
  const restoredTaskIdsRef = useRef<Set<string>>(new Set());
  const localTaskRuntimeActiveRef = useRef(false);
  const pendingAssistantPatchesRef = useRef<Map<string, AssistantMessagePatch>>(new Map());
  const handledWaitingInputEventIdsRef = useRef<Set<string>>(new Set());
  const waitingInputRetryTimersRef = useRef<Map<string, number>>(new Map());
  const eventStreamReconnectTimerRef = useRef<number | null>(null);
  const transientNoticeIdRef = useRef(0);

  function showTransientNotice(message: string, type: TransientNotice['type'] = 'warning') {
    transientNoticeIdRef.current += 1;
    setTransientNotice({ id: transientNoticeIdRef.current, message, type });
  }

  function clearTransientNotice() {
    setTransientNotice(null);
  }

  function clearEventStreamReconnectTimer() {
    if (eventStreamReconnectTimerRef.current === null) return;
    window.clearTimeout(eventStreamReconnectTimerRef.current);
    eventStreamReconnectTimerRef.current = null;
  }

  function clearWaitingInputRetryTimer(eventId: string) {
    const timerId = waitingInputRetryTimersRef.current.get(eventId);
    if (timerId === undefined) return;
    window.clearTimeout(timerId);
    waitingInputRetryTimersRef.current.delete(eventId);
  }

  function clearWaitingInputRetryTimers() {
    for (const timerId of waitingInputRetryTimersRef.current.values()) {
      window.clearTimeout(timerId);
    }
    waitingInputRetryTimersRef.current.clear();
  }

  function updateCurrentTaskId(nextTaskId: string | null) {
    currentTaskIdRef.current = nextTaskId;
    setCurrentTaskId(nextTaskId);
  }

  function updateCurrentAssistantId(nextAssistantId: string | null) {
    currentAssistantIdRef.current = nextAssistantId;
    setCurrentAssistantId(nextAssistantId);
  }

  function setActiveConversationId(nextConversationId: string) {
    if (conversationIdRef.current !== nextConversationId) {
      setRestoredWorkspaceConversationId(null);
    }
    conversationIdRef.current = nextConversationId;
    setConversationId(nextConversationId);
  }

  function beginRestoreGeneration(): number {
    restoreGenerationRef.current += 1;
    return restoreGenerationRef.current;
  }

  function isCurrentRestoreGeneration(generation: number, targetConversationId: string): boolean {
    return restoreGenerationRef.current === generation && conversationIdRef.current === targetConversationId;
  }

  useEffect(() => {
    taskPhaseRef.current = taskState.phase;
  }, [taskState.phase]);

  useEffect(() => {
    let mounted = true;
    api.me()
      .then((result) => {
        if (!mounted) return;
        setAuthUser(result.user);
        setActiveConversationId(loadOrCreateConversationId(result.user.username));
      })
      .catch(() => {
        if (mounted) setAuthUser(null);
      });
    return () => {
      mounted = false;
    };
  }, [api]);

  useEffect(() => {
    if (!authUser) {
      setSkillCommands([]);
      setSlashMenuOpen(false);
      setSelectedSkillCommand(null);
      return undefined;
    }
    let mounted = true;
    api.listCapabilities()
      .then((result) => {
        if (!mounted) return;
        setSkillCommands(deriveSlashCommands(result.capabilities));
      })
      .catch(() => {
        if (!mounted) return;
        setSkillCommands([]);
        showTransientNotice('Skill 列表加载失败，请刷新重试。');
      });
    return () => {
      mounted = false;
    };
  }, [api, authUser]);

  useEffect(() => {
    if (!authUser) {
      setModelEditionOptions([]);
      setDefaultModelEdition(null);
      setModelEdition(null);
      return undefined;
    }
    let mounted = true;
    api.getModelEditions()
      .then((result) => {
        if (!mounted) return;
        setModelEditionOptions(result.options);
        setDefaultModelEdition(result.default_model_edition);
        setModelEdition((current) => {
          if (current && result.options.some((option) => option.value === current)) {
            return current;
          }
          return result.default_model_edition ?? result.options[0]?.value ?? null;
        });
      })
      .catch(() => {
        if (!mounted) return;
        setModelEditionOptions([]);
        setDefaultModelEdition(null);
        setModelEdition(null);
        showTransientNotice('模型版本配置加载失败，请刷新重试。');
      });
    return () => {
      mounted = false;
    };
  }, [api, authUser]);

  useEffect(() => {
    if (!transientNotice) return undefined;
    const timeoutId = window.setTimeout(() => {
      setTransientNotice((current) => (
        current?.id === transientNotice.id ? null : current
      ));
    }, TRANSIENT_NOTICE_DURATION_MS);
    return () => window.clearTimeout(timeoutId);
  }, [transientNotice]);

  const refreshConversationHistory = useCallback(async (): Promise<ConversationSummaryResponse[] | null> => {
    if (!authUser) return [];
    setHistoryLoading(true);
    try {
      const result = await api.listConversations();
      setConversationHistory(result.conversations);
      return result.conversations;
    } catch {
      showTransientNotice('历史会话加载失败，请稍后重试。');
      return null;
    } finally {
      setHistoryLoading(false);
    }
  }, [api, authUser]);

  const active = isTaskActive(taskState.phase);
  const composerDisabled = active && taskState.phase !== 'cancelling';
  const composerWorkspaceReady = Boolean(conversationId) && restoredWorkspaceConversationId === conversationId;
  const composerInputReady = COMPOSER_READY_PHASES.has(taskState.phase)
    || (taskState.phase === 'waiting_for_input' && pendingInterrupt !== null);
  const composerAutoFocusEnabled = Boolean(authUser) && composerWorkspaceReady && !composerDisabled && composerInputReady && taskState.phase !== 'cancelling';
  const composerAutofocusKey = composerAutoFocusEnabled
    ? [
      conversationId || 'new',
      taskState.phase,
      pendingInterrupt?.interruptId ?? 'no-interrupt',
      messages.length,
    ].join(':')
    : null;

  useEffect(() => {
    if (!authUser) return;
    return () => {
      subscriptionRef.current?.close();
      clearEventStreamReconnectTimer();
      clearWaitingInputRetryTimers();
    };
  }, [authUser]);

  useEffect(() => {
    if (!composerAutoFocusEnabled || composerAutofocusKey === null) return;
    if (lastComposerAutofocusKeyRef.current === composerAutofocusKey) return;
    lastComposerAutofocusKeyRef.current = composerAutofocusKey;
    const frame = window.requestAnimationFrame(() => {
      if (composingInputRef.current) return;
      const textArea = resolveComposerTextArea(composerTextAreaRef, composerRootRef.current);
      if (!textArea || textArea.disabled || taskPhaseRef.current === 'cancelling') return;
      const phase = taskPhaseRef.current;
      const phaseInputReady = COMPOSER_READY_PHASES.has(phase)
        || (phase === 'waiting_for_input' && pendingInterrupt !== null);
      if (!phaseInputReady || !canAutoFocusComposer(textArea, composerRootRef.current)) return;
      focusTextAreaWithoutScroll(textArea);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [composerAutoFocusEnabled, composerAutofocusKey, pendingInterrupt]);

  useEffect(() => {
    if (!authUser || !conversationId || active || localTaskRuntimeActiveRef.current) return;
    if (initializedWorkspaceConversationIdRef.current === conversationId) return;
    const generation = beginRestoreGeneration();
    void initializeConversationWorkspace(conversationId, generation);
  }, [authUser, conversationId, active]);

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
        const shouldStop = await pollTaskGraphFallback(currentTaskId);
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

  useEffect(() => {
    if (!currentTaskId || !currentAssistantId || taskState.phase !== 'cancelling') return;
    let stopped = false;
    let timer: ReturnType<typeof window.setTimeout> | null = null;
    let attempts = 0;
    const taskId = currentTaskId;
    const assistantId = currentAssistantId;
    const generation = restoreGenerationRef.current;
    const targetConversationId = conversationIdRef.current;

    const poll = async () => {
      if (stopped) return;
      attempts += 1;
      try {
        const task = await api.getTask(taskId);
        const reconciled = await reconcileTerminalTaskStatus(task, taskId, assistantId, generation, targetConversationId);
        if (reconciled || attempts >= CANCEL_RECONCILE_MAX_ATTEMPTS) {
          stopped = true;
          return;
        }
      } catch {
        if (attempts >= CANCEL_RECONCILE_MAX_ATTEMPTS) {
          stopped = true;
          return;
        }
      }
      timer = window.setTimeout(() => {
        timer = null;
        void poll();
      }, CANCEL_RECONCILE_DELAY_MS);
    };

    void poll();
    return () => {
      stopped = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [api, currentAssistantId, currentTaskId, taskState.phase]);

  async function pollTaskGraphFallback(taskId: string): Promise<boolean> {
    try {
      await api.getTaskGraph(taskId);
      const task = await api.getTask(taskId);
      const assistantId = currentAssistantIdRef.current;
      if (assistantId && isTerminalTaskStatus(task.status)) {
        return await reconcileTerminalTaskStatus(
          task,
          taskId,
          assistantId,
          restoreGenerationRef.current,
          conversationIdRef.current,
        );
      }
    } catch {
      // The task graph is a best-effort fallback for resumable interrupts.
      // Keep the current running state if the fallback check fails.
    }
    return false;
  }

  async function loadConversationMessages(targetConversationId: string): Promise<ConversationMessage[]> {
    const result = await api.listConversationMessages(targetConversationId);
    return result.messages.map(messageFromHistory).filter((message): message is ConversationMessage => message !== null);
  }

  async function initializeConversationWorkspace(targetConversationId: string, generation: number): Promise<void> {
    const conversations = await refreshConversationHistory();
    if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
    if (conversations === null) return;
    initializedWorkspaceConversationIdRef.current = targetConversationId;
    const summary = conversations.find((item) => item.conversation_id === targetConversationId);
    if (!summary) {
      clearCurrentTaskRuntime({ closeSubscription: true });
      setMessages([]);
      setRestoredWorkspaceConversationId(targetConversationId);
      return;
    }
    try {
      const loadedMessages = await loadConversationMessages(targetConversationId);
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      setMessages(loadedMessages);
      await restoreCurrentConversationTask(summary, generation);
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      setRestoredWorkspaceConversationId(targetConversationId);
    } catch {
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      showTransientNotice('历史消息加载失败，请稍后重试。');
      clearCurrentTaskRuntime({ closeSubscription: true });
    }
  }

  function clearCurrentTaskRuntime({ closeSubscription = true }: { closeSubscription?: boolean } = {}) {
    if (closeSubscription) {
      subscriptionRef.current?.close();
      subscriptionRef.current = null;
    }
    setTaskState(createInitialTaskEventState());
    updateCurrentTaskId(null);
    updateCurrentAssistantId(null);
    setPendingInterrupt(null);
    localTaskRuntimeActiveRef.current = false;
    restoredTaskIdsRef.current.clear();
    pendingAssistantPatchesRef.current.clear();
    handledWaitingInputEventIdsRef.current.clear();
    clearEventStreamReconnectTimer();
    clearWaitingInputRetryTimers();
  }

  function isActiveTaskStatus(status: string): boolean {
    return ACTIVE_TASK_STATUSES.has(status);
  }

  function isTerminalTaskStatus(status: string): boolean {
    return TERMINAL_TASK_STATUSES.has(status);
  }

  function shouldIgnoreNonTerminalTaskEvent(eventType: string): boolean {
    if (eventType === 'task.cancellation_requested') return false;
    return !TERMINAL_TASK_EVENT_TYPES.has(eventType)
      && CANCELLATION_OR_TERMINAL_PHASES.has(taskPhaseRef.current);
  }

  function isTaskCancellationOrTerminalPhase(): boolean {
    return CANCELLATION_OR_TERMINAL_PHASES.has(taskPhaseRef.current);
  }

  function markTaskCancelledState(state: TaskEventState): TaskEventState {
    return {
      ...state,
      phase: 'cancelled',
      statusText: '任务已取消',
      currentCapabilityId: null,
      currentCapabilityLabel: null,
      currentActivityText: null,
      errorMessage: null,
    };
  }

  function settleCancelledTask(taskId: string, assistantId: string) {
    taskPhaseRef.current = 'cancelled';
    clearWaitingInputRetryTimers();
    clearEventStreamReconnectTimer();
    setPendingInterrupt(null);
    updateAssistantMessage(assistantId, {
      interruptPrompt: undefined,
      activityText: '任务已取消',
      activityStatus: 'cancelled',
    });
    localTaskRuntimeActiveRef.current = false;
    setTaskState((state) => markTaskCancelledState(state));
    updateCurrentTaskId(null);
    restoredTaskIdsRef.current.delete(taskId);
    taskPresentationModesRef.current.delete(taskId);
    subscriptionRef.current?.close();
    subscriptionRef.current = null;
    void refreshConversationHistory();
  }

  async function reconcileTerminalTaskStatus(
    task: Pick<TaskSummaryResponse, 'task_id' | 'status'>,
    expectedTaskId: string,
    assistantId: string,
    generation: number,
    targetConversationId: string,
  ): Promise<boolean> {
    if (task.task_id !== expectedTaskId) return false;
    if (!isCurrentRestoreGeneration(generation, targetConversationId)) return true;
    if (!isTerminalTaskStatus(task.status)) return false;
    clearEventStreamReconnectTimer();
    setPendingInterrupt(null);
    if (task.status === 'completed') {
      taskPhaseRef.current = 'loading_artifacts';
      clearWaitingInputRetryTimers();
      updateAssistantMessage(assistantId, { interruptPrompt: undefined });
      updateAssistantMessage(assistantId, { reasoningComplete: true, replyCompleted: true });
      await loadArtifacts(expectedTaskId, assistantId);
      return true;
    }
    if (task.status === 'failed') {
      taskPhaseRef.current = 'failed';
      clearWaitingInputRetryTimers();
      localTaskRuntimeActiveRef.current = false;
      setTaskState((state) => markTaskFailed(state, '本次任务未完成，请调整问题后重试。'));
      updateAssistantMessage(assistantId, {
        interruptPrompt: undefined,
        activityText: '本次任务未完成',
        activityStatus: 'failed',
      });
      updateCurrentTaskId(null);
      restoredTaskIdsRef.current.delete(expectedTaskId);
      taskPresentationModesRef.current.delete(expectedTaskId);
      subscriptionRef.current?.close();
      subscriptionRef.current = null;
      void refreshConversationHistory();
      return true;
    }
    if (task.status === 'cancelled') {
      settleCancelledTask(expectedTaskId, assistantId);
      return true;
    }
    return false;
  }

  async function restoreCurrentConversationTask(conversation: ConversationSummaryResponse, generation: number): Promise<void> {
    const targetConversationId = conversation.conversation_id;
    const taskId = conversation.current_task_id;
    if (!taskId) {
      clearCurrentTaskRuntime({ closeSubscription: true });
      return;
    }
    let task: TaskSummaryResponse;
    try {
      task = await api.getTask(taskId);
    } catch {
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      showTransientNotice('任务状态暂时无法恢复，请刷新历史消息。');
      clearCurrentTaskRuntime({ closeSubscription: true });
      return;
    }
    if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
    if (isTerminalTaskStatus(task.status)) {
      clearCurrentTaskRuntime({ closeSubscription: true });
      if (task.status === 'completed') {
        try {
          const loadedMessages = await loadConversationMessages(targetConversationId);
          if (isCurrentRestoreGeneration(generation, targetConversationId)) {
            setMessages(loadedMessages);
          }
        } catch {
          if (isCurrentRestoreGeneration(generation, targetConversationId)) {
            showTransientNotice('历史消息加载失败，请稍后重试。');
          }
        }
      } else {
        showTransientNotice(task.status === 'cancelled' ? '当前任务已取消。' : '当前任务未完成，请调整问题后重试。');
      }
      return;
    }
    if (!isActiveTaskStatus(task.status)) {
      showTransientNotice('任务状态暂不支持恢复，请刷新历史消息。');
      clearCurrentTaskRuntime({ closeSubscription: true });
      return;
    }
    const restoringState = createRestoringTaskState();
    const restoredAssistantId = `restored-assistant-${taskId}`;
    const restoredAssistantMessage: ConversationMessage = {
      id: restoredAssistantId,
      role: 'assistant',
      content: '',
      mode,
      reasoningRequested: false,
      activityText: taskProgressDisplayText(restoringState),
    };
    restoredTaskIdsRef.current.add(taskId);
    taskPresentationModesRef.current.set(taskId, mode);
    setMessages((current) => (
      current.some((message) => message.id === restoredAssistantId)
        ? current
        : [...current, applyPendingAssistantPatch(restoredAssistantMessage)]
    ));
    updateCurrentTaskId(taskId);
    updateCurrentAssistantId(restoredAssistantId);
    setPendingInterrupt(null);
    setTaskState(restoringState);
    subscribeToTask(taskId, restoredAssistantId, generation, targetConversationId);
  }

  async function handleLogin(result: AuthTokenResponse) {
    tokenRef.current = result.access_token;
    writeStoredAccessToken(result.access_token);
    setAuthUser(result.user);
    const nextConversationId = loadOrCreateConversationId(result.user.username);
    initializedWorkspaceConversationIdRef.current = null;
    setRestoredWorkspaceConversationId(null);
    setActiveConversationId(nextConversationId);
    setModelEdition(defaultModelEdition);
    setMessages([]);
    setPendingUploads([]);
    clearCurrentTaskRuntime({ closeSubscription: true });
  }

  async function handleLogout() {
    await api.logout().catch(() => undefined);
    clearStoredAuth();
    const targetConversationId = conversationId;
    const generation = beginRestoreGeneration();
    subscriptionRef.current?.close();
    setAuthUser(null);
    initializedWorkspaceConversationIdRef.current = null;
    setRestoredWorkspaceConversationId(null);
    setActiveConversationId('');
    setMessages([]);
    setConversationHistory([]);
    setPendingUploads([]);
    updateCurrentTaskId(null);
    updateCurrentAssistantId(null);
    setPendingInterrupt(null);
    setTaskState(createInitialTaskEventState());
    setDeletingConversationIds(new Set());
    setRenamingConversationIds(new Set());
  }

  function handleAccountSettings() {
    showTransientNotice('用户账户设置功能会在后续版本开放。');
  }

  function resetConversationWorkspace(nextConversationId: string) {
    beginRestoreGeneration();
    subscriptionRef.current?.close();
    subscriptionRef.current = null;
    initializedWorkspaceConversationIdRef.current = null;
    setRestoredWorkspaceConversationId(null);
    setActiveConversationId(nextConversationId);
    setMessages([]);
    setInput('');
    setModelEdition(defaultModelEdition);
    setPendingUploads([]);
    updateCurrentTaskId(null);
    updateCurrentAssistantId(null);
    setPendingInterrupt(null);
    setTaskState(createInitialTaskEventState());
    taskPresentationModesRef.current.clear();
    restoredTaskIdsRef.current.clear();
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
    const generation = beginRestoreGeneration();
    setRestoredWorkspaceConversationId(null);
    clearCurrentTaskRuntime({ closeSubscription: true });
    if (nextConversationId !== conversationId) {
      initializedWorkspaceConversationIdRef.current = null;
      setActiveConversationId(nextConversationId);
      return;
    }
    void initializeConversationWorkspace(nextConversationId, generation);
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
    const deletingCurrentConversation = targetConversationId === conversationId;
    if (deletingCurrentConversation) {
      subscriptionRef.current?.close();
      subscriptionRef.current = null;
      const next = createConversationId();
      saveConversationId(authUser.username, next);
      resetConversationWorkspace(next);
    }
    try {
      const result = await api.deleteConversation(targetConversationId);
      setConversationHistory((current) => current.filter((item) => item.conversation_id !== targetConversationId));
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

  const slashCandidates = useMemo(
    () => (slashMenuOpen ? slashMenuCandidates(input, skillCommands) : []),
    [input, skillCommands, slashMenuOpen],
  );
  const normalizedSlashMenuActiveIndex = slashCandidates.length === 0
    ? 0
    : Math.min(slashMenuActiveIndex, slashCandidates.length - 1);
  const directSlashParse = useMemo(() => parseDirectSlashCommand(input, skillCommands), [input, skillCommands]);
  const slashInputBlocked = !selectedSkillCommand && (directSlashParse.kind === 'not_found' || directSlashParse.kind === 'conflict');
  const pendingInterruptAcceptsUpload = pendingInterrupt !== null && interruptAcceptsUpload(pendingInterrupt);
  const canSubmitUploadOnlyInterruptAnswer = pendingInterruptAcceptsUpload && pendingUploads.length > 0;
  const canUploadInCurrentComposer = !active && (!pendingInterrupt || pendingInterruptAcceptsUpload);
  const canSubmitComposer = !slashInputBlocked && (
    Boolean(input.trim())
    || selectedSkillCommand !== null
    || directSlashParse.kind === 'matched'
    || canSubmitUploadOnlyInterruptAnswer
  );
  const slashMenuEmptyMessage = skillCommands.length === 0 ? '暂无可用 Skill' : '未找到 Skill';

  function handleComposerInputChange(value: string) {
    setInput(value);
    if (isSlashInput(value)) {
      setSlashMenuOpen(true);
      setSlashMenuActiveIndex(0);
    } else {
      setSlashMenuOpen(false);
    }
  }

  function selectSlashCommand(command: SlashCommand) {
    setSelectedSkillCommand(command);
    setInput((current) => {
      const parsed = parseDirectSlashCommand(current, [command]);
      if (parsed.kind === 'matched') return parsed.content;
      if (isSlashInput(current)) return current.trimStart().replace(/^\/[^\s]*\s*/, '');
      return current;
    });
    setSlashMenuOpen(false);
    setSlashMenuActiveIndex(0);
    clearTransientNotice();
  }

  function handleSlashKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean {
    if (!slashMenuOpen && !isSlashInput(input)) return false;
    if (event.key === 'Escape') {
      event.preventDefault();
      setSlashMenuOpen(false);
      return true;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setSlashMenuOpen(true);
      setSlashMenuActiveIndex((current) => (slashCandidates.length === 0 ? 0 : (current + 1) % slashCandidates.length));
      return true;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setSlashMenuOpen(true);
      setSlashMenuActiveIndex((current) => (slashCandidates.length === 0 ? 0 : (current - 1 + slashCandidates.length) % slashCandidates.length));
      return true;
    }
    if (event.key === 'Enter') {
      const parsed = parseDirectSlashCommand(input, skillCommands);
      if (parsed.kind === 'matched' && /\s/.test(input.trimStart().slice(parsed.command.command.length))) {
        return false;
      }
      if (slashMenuOpen && slashCandidates.length > 0) {
        event.preventDefault();
        selectSlashCommand(slashCandidates[normalizedSlashMenuActiveIndex]);
        return true;
      }
      if (parsed.kind === 'not_found' || parsed.kind === 'conflict') {
        event.preventDefault();
        setSlashMenuOpen(true);
        showTransientNotice(parsed.kind === 'conflict' ? `命令 ${parsed.command} 存在冲突，请从列表中选择具体 Skill。` : `未找到 Skill：${parsed.command}`);
        return true;
      }
    }
    return false;
  }

  async function handleSubmit() {
    const intent = slashSubmitIntent(input, skillCommands, selectedSkillCommand);
    if (intent.kind === 'blocked') {
      setSlashMenuOpen(true);
      showTransientNotice(intent.reason === 'conflict' ? `命令 ${intent.command} 存在冲突，请从列表中选择具体 Skill。` : `未找到 Skill：${intent.command}`);
      return;
    }
    const content = intent.content;
    const forcedCommand = intent.kind === 'ready' ? intent.command : null;
    const forcedCapabilityId = intent.kind === 'ready' ? intent.capabilityId : null;
    const forcedMetadata = intent.kind === 'ready' ? intent.metadata : {};
    const targetConversationId = authUser ? (conversationId || loadOrCreateConversationId(authUser.username)) : '';
    if (!authUser || !targetConversationId || active) return;
    if (!conversationId) {
      setActiveConversationId(targetConversationId);
    }
    if (!content && intent.kind !== 'ready' && !canSubmitUploadOnlyInterruptAnswer) return;
    clearTransientNotice();
    if (pendingInterrupt && intent.kind === 'ready') {
      setSlashMenuOpen(true);
      showTransientNotice('当前任务正在等待补充信息。请先回答补充问题或取消当前任务，再使用新的 Skill 命令。');
      return;
    }
    if (pendingInterrupt) {
      await handleInterruptAnswer(content, pendingInterrupt);
      return;
    }
    localTaskRuntimeActiveRef.current = true;
    const generation = beginRestoreGeneration();
    initializedWorkspaceConversationIdRef.current = targetConversationId;
    setRestoredWorkspaceConversationId(targetConversationId);
    const displayContent = content || (intent.kind === 'ready' ? intent.command.command : content);
    const userMessage: ConversationMessage = { id: makeClientId('user'), role: 'user', content: displayContent, mode };
    const assistantMessage: ConversationMessage = {
      id: makeClientId('assistant'),
      role: 'assistant',
      content: '',
      mode,
      reasoningRequested: deepThinking,
      activityText: taskProgressDisplayText(createSubmittingTaskState()),
    };
    setMessages((current) => [...current, userMessage, applyPendingAssistantPatch(assistantMessage)]);
    updateCurrentAssistantId(assistantMessage.id);
    setInput('');
    setTaskState(createSubmittingTaskState());

    try {
      const accepted = await api.submitMessage({
        conversationId: targetConversationId,
        content,
        mode,
        modelEdition: modelEdition ?? undefined,
        deepThinking,
        reasoningEffort: deepThinking ? reasoningEffort : 'minimal',
        capabilityId: forcedCapabilityId,
        metadata: {
          ...(pendingUploads.length > 0 ? { upload_ids: pendingUploads.map((upload) => upload.upload_id) } : {}),
          ...forcedMetadata,
        },
      });
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      taskPresentationModesRef.current.set(accepted.task_id, mode);
      updateCurrentTaskId(accepted.task_id);
      setSelectedSkillCommand(null);
      setSlashMenuOpen(false);
      subscribeToTask(accepted.task_id, assistantMessage.id, generation, targetConversationId);
    } catch (error) {
      localTaskRuntimeActiveRef.current = false;
      const message = friendlyError(error);
      if (forcedCommand) {
        setSelectedSkillCommand(null);
        setSlashMenuOpen(false);
        void api.listCapabilities()
          .then((result) => setSkillCommands(deriveSlashCommands(result.capabilities)))
          .catch(() => undefined);
      }
      setTaskState((state) => markTaskFailed(state, message));
      showTransientNotice(forcedCommand ? `${message} Skill 列表可能已更新，请重新选择。` : message);
    }
  }

  function isComposerImeConfirming(event: ReactKeyboardEvent<HTMLTextAreaElement>): boolean {
    const reactEvent = event as ReactKeyboardEvent<HTMLTextAreaElement> & { isComposing?: boolean };
    const nativeEvent = event.nativeEvent as KeyboardEvent & { keyCode?: number };
    return composingInputRef.current || reactEvent.isComposing === true || nativeEvent.isComposing === true || nativeEvent.keyCode === 229;
  }

  async function handleUploadFile(file: File | undefined) {
    if (!authUser || !conversationId || !file || !canUploadInCurrentComposer || uploadingFile) return;
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

  async function handleDownloadArtifact(result: FileArtifactResult) {
    try {
      await api.downloadArtifact(result.artifactId, result.filename);
    } catch (error) {
      showTransientNotice(friendlyError(error));
    }
  }

  function isFileDrag(event: DragEvent<HTMLDivElement>): boolean {
    const types = Array.from(event.dataTransfer.types ?? []);
    return types.length === 0 || types.includes('Files') || event.dataTransfer.files.length > 0;
  }

  function canAcceptDraggedUpload(): boolean {
    return Boolean(authUser && conversationId && canUploadInCurrentComposer && !uploadingFile);
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
    const uploads = interruptAcceptsUpload(interrupt) ? pendingUploads.slice() : [];
    const sheetField = interruptSheetSelectionField(interrupt);
    const selectedSheets = sheetField ? selectedSheetPayload(sheetField, content) : {};
    const targetConversationId = conversationIdRef.current;
    localTaskRuntimeActiveRef.current = true;
    const generation = beginRestoreGeneration();
    const resumeProgressText = '补充信息已提交，正在继续任务';
    const displayContent = content || uploadAnswerDisplayText(uploads) || sheetSelectionDisplayText(sheetField, selectedSheets);
    const userMessage: ConversationMessage = { id: makeClientId('user'), role: 'user', content: displayContent, mode: interrupt.mode };
    const assistantMessage: ConversationMessage = {
      id: makeClientId('assistant'),
      role: 'assistant',
      content: '',
      mode: interrupt.mode,
      reasoningRequested: deepThinking,
      activityText: resumeProgressText,
    };
    setMessages((current) => [...current, userMessage, applyPendingAssistantPatch(assistantMessage)]);
    updateCurrentAssistantId(assistantMessage.id);
    setInput('');
    setTaskState((state) => ({
      ...state,
      phase: 'running',
      statusText: resumeProgressText,
      assistantText: '',
      reasoningText: '',
      errorMessage: null,
    }));

    try {
      const response = await api.submitMessage({
        conversationId: targetConversationId,
        content,
        mode: interrupt.mode,
        modelEdition: modelEdition ?? undefined,
        deepThinking,
        reasoningEffort: deepThinking ? reasoningEffort : 'minimal',
        clientMessageId: userMessage.id,
        metadata: interruptSubmitMetadata(interrupt, uploads, selectedSheets),
      });
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      if (isInterruptKeepOpenResponse(response)) {
        const assistantContent = typeof response.assistant_message === 'string' && response.assistant_message.trim() ? response.assistant_message : '我已经理解这是一个追问，当前任务仍会等待你补充正式信息。';
        updateAssistantMessage(assistantMessage.id, {
          content: assistantContent,
          activityText: undefined,
          replyCompleted: true,
        });
        setTaskState((state) =>
          markWaitingInputRequired({
            ...state,
            assistantText: '',
            reasoningText: '',
            errorMessage: null,
          }),
        );
        const keepOpenTaskId = response.task_id || interrupt.taskId;
        let refreshedInterrupt = interrupt;
        try {
          const interrupts = await api.listInterrupts(keepOpenTaskId);
          const openInterrupt = interrupts.interrupts.find((item) => item.status === 'open' && item.interrupt_id === (response.interrupt_id || interrupt.interruptId)) ?? interrupts.interrupts.find((item) => item.status === 'open');
          if (openInterrupt) {
            refreshedInterrupt = {
              taskId: keepOpenTaskId,
              interruptId: openInterrupt.interrupt_id,
              question: openInterrupt.question,
              requiredFields: openInterrupt.required_fields,
              mode: interrupt.mode,
              naturalLanguage: isNaturalLanguageInterrupt(openInterrupt.required_fields),
            };
          }
        } catch {
          refreshedInterrupt = interrupt;
        }
        setPendingInterrupt(refreshedInterrupt);
        updateCurrentTaskId(keepOpenTaskId);
        taskPresentationModesRef.current.set(keepOpenTaskId, interrupt.mode);
        return;
      }
      const resumedTaskId = response.task_id || interrupt.taskId;
      taskPresentationModesRef.current.set(resumedTaskId, interrupt.mode);
      setPendingUploads([]);
      setPendingInterrupt(null);
      updateCurrentTaskId(resumedTaskId);
      subscribeToTask(resumedTaskId, assistantMessage.id, generation, targetConversationId);
    } catch (error) {
      localTaskRuntimeActiveRef.current = false;
      setTaskState((state) => markTaskFailed(state, friendlyError(error)));
      showTransientNotice(friendlyError(error));
    }
  }

  function subscribeToTask(
    taskId: string,
    assistantId: string,
    generation = restoreGenerationRef.current,
    targetConversationId = conversationIdRef.current,
  ) {
    clearEventStreamReconnectTimer();
    subscriptionRef.current?.close();
    subscriptionRef.current = createEventSource(taskEventsUrl(taskId), {
      onMessage: (event) => {
        if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
        if (event.task_id !== taskId) return;
        handleTaskEvent(event, taskId, assistantId, generation, targetConversationId);
      },
      onError: () => {
        if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
        handleEventStreamError(taskId, assistantId, generation, targetConversationId);
      },
    });
  }

  function handleTaskEvent(
    event: TaskEventEnvelope,
    taskId: string,
    assistantId: string,
    generation: number,
    targetConversationId: string,
  ) {
    if (TERMINAL_TASK_EVENT_TYPES.has(event.event_type)) {
      clearWaitingInputRetryTimers();
    }
    if (event.event_type === 'task.cancellation_requested') {
      taskPhaseRef.current = 'cancelling';
      clearWaitingInputRetryTimers();
      setPendingInterrupt(null);
      updateAssistantMessage(assistantId, { interruptPrompt: undefined });
    }
    if (shouldIgnoreNonTerminalTaskEvent(event.event_type)) {
      return;
    }
    setTaskState((previous) => {
      if (CANCELLATION_OR_TERMINAL_PHASES.has(previous.phase) && !TERMINAL_TASK_EVENT_TYPES.has(event.event_type)) {
        return previous;
      }
      const next = applyTaskEvent(previous, event);
      const previousProgressText = taskProgressDisplayText(previous);
      const nextProgressText = taskProgressDisplayText(next);
      if (next.skillStatuses !== previous.skillStatuses) {
        updateAssistantMessage(assistantId, { skillStatuses: next.skillStatuses });
      }
      if (next.assistantText !== previous.assistantText) {
        updateAssistantStreamingContent(assistantId, next.assistantText);
        if (next.assistantText) {
          updateAssistantMessage(assistantId, { activityText: undefined });
        }
      }
      if (next.reasoningText !== previous.reasoningText) {
        updateAssistantMessage(assistantId, { reasoningContent: next.reasoningText });
      }
      if (next.phase === 'failed') {
        updateAssistantMessage(assistantId, {
          activityText: next.errorMessage ?? next.statusText,
          activityStatus: 'failed',
        });
      } else if (nextProgressText !== previousProgressText) {
        updateAssistantMessage(assistantId, {
          activityText: assistantActivityText(next, nextProgressText),
          activityStatus: next.phase === 'cancelled' ? 'cancelled' : 'pending',
        });
      }
      if (event.event_type === 'task.failed') {
        taskPhaseRef.current = 'failed';
        setPendingInterrupt(null);
        updateAssistantMessage(assistantId, { interruptPrompt: undefined });
        localTaskRuntimeActiveRef.current = false;
        subscriptionRef.current?.close();
        subscriptionRef.current = null;
        updateCurrentTaskId(null);
        restoredTaskIdsRef.current.delete(taskId);
        taskPresentationModesRef.current.delete(taskId);
      }
      if (event.event_type === 'task.cancelled') {
        taskPhaseRef.current = 'cancelled';
        setPendingInterrupt(null);
        updateAssistantMessage(assistantId, { interruptPrompt: undefined });
        localTaskRuntimeActiveRef.current = false;
        subscriptionRef.current?.close();
        subscriptionRef.current = null;
        updateCurrentTaskId(null);
        restoredTaskIdsRef.current.delete(taskId);
        taskPresentationModesRef.current.delete(taskId);
      }
      return next;
    });
    if (event.event_type === 'task.completed') {
      taskPhaseRef.current = 'loading_artifacts';
      updateAssistantMessage(assistantId, { reasoningComplete: true, replyCompleted: true });
      void loadArtifacts(taskId, assistantId);
    }
    if (event.event_type === 'node.waiting_for_input') {
      void loadPendingInterruptFromWaitingEvent(event, taskId, assistantId, generation, targetConversationId);
    }
  }

  function isCurrentTaskEventStream(
    taskId: string,
    assistantId: string,
    generation: number,
    targetConversationId: string,
  ): boolean {
    return isCurrentRestoreGeneration(generation, targetConversationId)
      && currentTaskIdRef.current === taskId
      && currentAssistantIdRef.current === assistantId;
  }

  async function loadPendingInterruptFromWaitingEvent(
    event: TaskEventEnvelope,
    taskId: string,
    assistantId: string,
    generation: number,
    targetConversationId: string,
    attempt = 0,
  ) {
    if (!isCurrentTaskEventStream(taskId, assistantId, generation, targetConversationId)) return;
    if (isTaskCancellationOrTerminalPhase()) return;
    if (attempt === 0) {
      if (handledWaitingInputEventIdsRef.current.has(event.event_id)) return;
      handledWaitingInputEventIdsRef.current.add(event.event_id);
    }
    const payloadInterruptId = typeof event.payload.interrupt_id === 'string' ? event.payload.interrupt_id : null;
    try {
      const interrupts = await api.listInterrupts(taskId);
      if (!isCurrentTaskEventStream(taskId, assistantId, generation, targetConversationId)) return;
      const openInterrupts = interrupts.interrupts.filter((interrupt) => interrupt.status === 'open');
      const openInterrupt =
        (payloadInterruptId ? openInterrupts.find((interrupt) => interrupt.interrupt_id === payloadInterruptId) : undefined)
        ?? (event.node_id ? openInterrupts.find((interrupt) => interrupt.node_id === event.node_id) : undefined)
        ?? openInterrupts[0];
      if (!openInterrupt) {
        scheduleWaitingInputInterruptRetry(event, taskId, assistantId, generation, targetConversationId, attempt);
        return;
      }
      clearWaitingInputRetryTimer(event.event_id);
      const interruptionMode = taskPresentationModesRef.current.get(taskId) ?? mode;
      const pending: PendingInterrupt = {
        taskId,
        interruptId: openInterrupt.interrupt_id,
        question: openInterrupt.question,
        requiredFields: openInterrupt.required_fields,
        mode: interruptionMode,
        naturalLanguage: isNaturalLanguageInterrupt(openInterrupt.required_fields),
      };
      setPendingInterrupt(pending);
      updateAssistantMessage(assistantId, {
        content: pending.naturalLanguage ? pending.question : '',
        interruptPrompt: pending.naturalLanguage ? undefined : pending,
        mode: interruptionMode,
        artifactDisplays: undefined,
        finalContentLoaded: undefined,
      });
      setTaskState((state) => markWaitingInputRequired(state));
      subscriptionRef.current?.close();
      subscriptionRef.current = null;
    } catch {
      if (!scheduleWaitingInputInterruptRetry(event, taskId, assistantId, generation, targetConversationId, attempt)) {
        showTransientNotice('补充信息请求已到达，但暂时无法加载表单，请稍后重试。');
      }
    }
  }

  function scheduleWaitingInputInterruptRetry(
    event: TaskEventEnvelope,
    taskId: string,
    assistantId: string,
    generation: number,
    targetConversationId: string,
    attempt: number,
  ): boolean {
    if (!isCurrentTaskEventStream(taskId, assistantId, generation, targetConversationId)) {
      clearWaitingInputRetryTimer(event.event_id);
      return true;
    }
    if (isTaskCancellationOrTerminalPhase()) {
      clearWaitingInputRetryTimer(event.event_id);
      return true;
    }
    if (attempt >= WAITING_INTERRUPT_MAX_RETRIES) {
      handledWaitingInputEventIdsRef.current.delete(event.event_id);
      clearWaitingInputRetryTimer(event.event_id);
      const waitingProgressText = '正在等待任务给出补充信息';
      setTaskState((state) => ({
        ...state,
        phase: 'running',
        statusText: waitingProgressText,
        currentActivityText: waitingProgressText,
        errorMessage: null,
      }));
      updateAssistantMessage(assistantId, { activityText: waitingProgressText });
      return false;
    }
    clearWaitingInputRetryTimer(event.event_id);
    const retryTimer = window.setTimeout(() => {
      waitingInputRetryTimersRef.current.delete(event.event_id);
      if (!isCurrentTaskEventStream(taskId, assistantId, generation, targetConversationId)) return;
      if (isTaskCancellationOrTerminalPhase()) return;
      void loadPendingInterruptFromWaitingEvent(event, taskId, assistantId, generation, targetConversationId, attempt + 1);
    }, WAITING_INTERRUPT_RETRY_DELAY_MS);
    waitingInputRetryTimersRef.current.set(event.event_id, retryTimer);
    return true;
  }

  async function handleEventStreamError(
    taskId: string,
    assistantId: string,
    generation = restoreGenerationRef.current,
    targetConversationId = conversationIdRef.current,
  ) {
    showTransientNotice('事件流暂时中断，正在尝试查询任务状态。');
    try {
      const task = await api.getTask(taskId);
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      if (await reconcileTerminalTaskStatus(task, taskId, assistantId, generation, targetConversationId)) {
        return;
      }
      if (isActiveTaskStatus(task.status)) {
        scheduleTaskEventReconnect(taskId, assistantId, generation, targetConversationId);
      }
    } catch {
      showTransientNotice('事件流中断，任务状态暂时无法确认。');
      if (isCurrentTaskEventStream(taskId, assistantId, generation, targetConversationId)) {
        scheduleTaskEventReconnect(taskId, assistantId, generation, targetConversationId);
      }
    }
  }

  function scheduleTaskEventReconnect(
    taskId: string,
    assistantId: string,
    generation: number,
    targetConversationId: string,
  ) {
    clearEventStreamReconnectTimer();
    eventStreamReconnectTimerRef.current = window.setTimeout(() => {
      eventStreamReconnectTimerRef.current = null;
      if (!isCurrentTaskEventStream(taskId, assistantId, generation, targetConversationId)) return;
      subscribeToTask(taskId, assistantId, generation, targetConversationId);
    }, EVENT_STREAM_RECONNECT_DELAY_MS);
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
          replyCompleted: true,
        });
      }
      updateAssistantMessage(assistantId, { activityText: undefined });
      taskPhaseRef.current = 'completed';
      setTaskState((state) => markTaskCompleted(state));
      updateCurrentTaskId(null);
      localTaskRuntimeActiveRef.current = false;
      taskPresentationModesRef.current.delete(taskId);
      subscriptionRef.current?.close();
      subscriptionRef.current = null;
      if (restoredTaskIdsRef.current.has(taskId)) {
        restoredTaskIdsRef.current.delete(taskId);
        const targetConversationId = conversationIdRef.current;
        try {
          const loadedMessages = await loadConversationMessages(targetConversationId);
          if (conversationIdRef.current === targetConversationId) {
            setMessages(loadedMessages);
          }
        } catch {
          showTransientNotice('任务已完成，但历史消息刷新失败，请稍后重试。');
        }
      }
      void refreshConversationHistory();
    } catch {
      localTaskRuntimeActiveRef.current = false;
      taskPhaseRef.current = 'completed';
      setTaskState((state) => markTaskCompleted(state, '任务已完成，但结果加载失败'));
      showTransientNotice('结果加载失败，可稍后重试。');
    }
  }

  async function handleCancel() {
    if (!conversationId && !currentTaskId) return;
    const previousPhase = taskState.phase;
    const previousPendingInterrupt = pendingInterrupt;
    const taskIdForCancelObservation = currentTaskIdRef.current ?? currentTaskId;
    const assistantIdForCancelObservation = currentAssistantIdRef.current ?? currentAssistantId;
    const cancelObservationGeneration = restoreGenerationRef.current;
    const cancelObservationConversationId = conversationIdRef.current;
    taskPhaseRef.current = 'cancelling';
    clearWaitingInputRetryTimers();
    setPendingInterrupt(null);
    setTaskState((state) => ({
      ...state,
      phase: 'cancelling',
      statusText: '取消请求已发送',
      currentActivityText: '正在停止当前对话任务',
    }));
    if (currentAssistantId) {
      updateAssistantMessage(currentAssistantId, {
        interruptPrompt: undefined,
        activityText: '正在停止当前对话任务',
        activityStatus: 'pending',
      });
    }
    try {
      const taskIds = await collectConversationTaskIdsToCancel();
      const cancelErrors: unknown[] = [];
      const cancelResponses: Array<Pick<TaskSummaryResponse, 'task_id' | 'status'>> = [];
      for (const taskId of taskIds) {
        try {
          cancelResponses.push(await api.cancelTask(taskId));
        } catch (error) {
          cancelErrors.push(error);
        }
      }
      if (cancelErrors.length > 0) {
        throw cancelErrors[0];
      }
      setPendingInterrupt(null);
      setTaskState((state) => ({
        ...state,
        phase: 'cancelling',
        statusText: '取消请求已发送',
        currentActivityText: '正在停止当前对话任务',
        errorMessage: null,
      }));
      if (currentAssistantId) {
        updateAssistantMessage(currentAssistantId, {
          interruptPrompt: undefined,
          activityText: '正在停止当前对话任务',
          activityStatus: 'pending',
        });
      }
      const taskIdToResubscribe = taskIdForCancelObservation ?? currentTaskIdRef.current;
      const assistantIdToResubscribe = assistantIdForCancelObservation ?? currentAssistantIdRef.current;
      const currentCancelResponse = cancelResponses.find((response) => response.task_id === taskIdToResubscribe);
      if (
        taskIdToResubscribe
        && assistantIdToResubscribe
        && currentCancelResponse
        && await reconcileTerminalTaskStatus(
          currentCancelResponse,
          taskIdToResubscribe,
          assistantIdToResubscribe,
          cancelObservationGeneration,
          cancelObservationConversationId,
        )
      ) {
        return;
      }
      if (taskPhaseRef.current === 'cancelling' && taskIdToResubscribe && assistantIdToResubscribe) {
        subscribeToTask(
          taskIdToResubscribe,
          assistantIdToResubscribe,
          cancelObservationGeneration,
          cancelObservationConversationId,
        );
      }
    } catch (error) {
      const message = friendlyError(error);
      showTransientNotice(message);
      taskPhaseRef.current = previousPhase;
      if (previousPendingInterrupt) {
        setPendingInterrupt(previousPendingInterrupt);
      }
      setTaskState((state) => ({
        ...state,
        phase: previousPhase,
        statusText: '取消任务失败，请稍后重试',
        errorMessage: message,
      }));
      if (currentAssistantId && previousPendingInterrupt) {
        updateAssistantMessage(currentAssistantId, {
          content: previousPendingInterrupt.naturalLanguage ? previousPendingInterrupt.question : undefined,
          interruptPrompt: previousPendingInterrupt.naturalLanguage ? undefined : previousPendingInterrupt,
          activityText: undefined,
          activityStatus: undefined,
        });
      }
    }
  }

  async function collectConversationTaskIdsToCancel(): Promise<string[]> {
    const orderedIds: string[] = [];
    const pushTaskId = (taskId: string | null | undefined) => {
      if (taskId && !orderedIds.includes(taskId)) {
        orderedIds.push(taskId);
      }
    };

    if (conversationId) {
      const listed = await api.listConversationTasks(conversationId, 'unfinished');
      for (const task of listed.tasks) {
        pushTaskId(task.task_id);
      }
    }
    pushTaskId(currentTaskIdRef.current ?? currentTaskId);
    return orderedIds;
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
  const inputPlaceholder = pendingInterrupt ? interruptAnswerPlaceholder(pendingInterrupt) : '从这里开始...';
  const composerMenuContent = (
    <Space direction="vertical" size="middle" className="composer-menu">
      <Button
        block
        aria-label="选择 JSON、CSV、Excel、TXT、VCF、图片或 PDF 文件"
        onClick={() => uploadInputRef.current?.click()}
        disabled={!canUploadInCurrentComposer || uploadingFile}
        loading={uploadingFile}
      >
        上传文件
      </Button>
      <Space size="small" align="center" className="composer-menu-row">
        <Typography.Text type="secondary">模型版本</Typography.Text>
        <Select
          aria-label="模型版本"
          value={modelEdition ?? undefined}
          options={modelEditionOptions}
          onChange={(value) => setModelEdition(value)}
          disabled={interactionLocked || modelEditionOptions.length === 0}
          size="small"
          style={{ width: 176 }}
        />
      </Space>
      <Space size="small" align="center" className="composer-menu-row">
        <Typography.Text type="secondary">思考强度</Typography.Text>
        <Select
          aria-label="思考强度"
          value={reasoningEffort}
          options={REASONING_EFFORT_OPTIONS}
          onChange={setReasoningEffort}
          disabled={interactionLocked || !deepThinking}
          size="small"
          style={{ width: 104 }}
        />
      </Space>
      <Space size="small" align="center" className="composer-menu-row">
        <Typography.Text type="secondary">深度思考</Typography.Text>
        <Switch
          aria-label="深度思考"
          checked={deepThinking}
          onChange={(checked) => {
            setDeepThinking(checked);
            if (!checked) {
              setReasoningEffort('minimal');
            }
          }}
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
          <section className="app-content" aria-label="当前对话面板">
            <div
              ref={conversationListRef}
              className={`conversation-list${pendingInterrupt ? ' conversation-list-with-interrupt-composer' : ''}`}
              aria-label="对话内容"
              onScroll={handleConversationScroll}
            >
              {messages.length === 0 ? <EmptyWelcome key={conversationId} /> : messages.map((message) => (
                <MessageBubble key={message.id} message={message} onDownloadArtifact={handleDownloadArtifact} />
              ))}
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
              <div className="chat-upload-drop-hint" aria-hidden={!draggingUpload}>
                释放文件以上传到当前对话
              </div>
              <Card
                className={`composer-card floating-composer${draggingUpload ? ' floating-composer-dragging' : ''}`}
                role="region"
                aria-label="悬浮发送栏"
              >
                <Space direction="vertical" size="small" className="composer-space">
                  {selectedSkillCommand ? (
                    <div className="selected-skill-command" role="status" aria-label="已选择 Skill">
                      <span>将使用 <strong>{selectedSkillCommand.command}</strong> {selectedSkillCommand.displayName}</span>
                      <Button
                        type="link"
                        size="small"
                        aria-label={`取消 Skill ${selectedSkillCommand.command}`}
                        disabled={active}
                        onClick={() => setSelectedSkillCommand(null)}
                      >
                        取消
                      </Button>
                    </div>
                  ) : null}
                  {slashMenuOpen ? (
                    <SlashCommandMenu
                      candidates={slashCandidates}
                      activeIndex={normalizedSlashMenuActiveIndex}
                      emptyMessage={slashMenuEmptyMessage}
                      onSelect={selectSlashCommand}
                    />
                  ) : null}
                  <div className={`composer-input-stack${pendingInterrupt ? ' composer-input-stack-interrupt' : ''}`}>
                    {pendingInterrupt ? <InterruptComposerStatus onCancel={handleCancel} cancelling={taskState.phase === 'cancelling'} /> : null}
                    <div ref={composerRootRef} className="send-row" role="group" aria-label="消息发送栏">
                      <Input.TextArea
                        ref={composerTextAreaRef}
                        aria-label="请输入问题"
                        value={input}
                        onChange={(event) => handleComposerInputChange(event.target.value)}
                        onCompositionStart={() => {
                          composingInputRef.current = true;
                        }}
                        onCompositionEnd={() => {
                          composingInputRef.current = false;
                        }}
                        onPressEnter={(event) => {
                          if (!event.shiftKey && !isComposerImeConfirming(event)) {
                            if (handleSlashKeyDown(event)) return;
                            event.preventDefault();
                            void handleSubmit();
                          }
                        }}
                        onKeyDown={(event) => {
                          if (event.key !== 'Enter' && !isComposerImeConfirming(event)) {
                            handleSlashKeyDown(event);
                          }
                        }}
                        placeholder={inputPlaceholder}
                        autoSize={{ minRows: 1, maxRows: 5 }}
                        wrap="soft"
                        disabled={composerDisabled}
                      />
                      <Popover
                        content={composerMenuContent}
                        trigger="click"
                        placement="topRight"
                        overlayClassName="composer-menu-popover"
                      >
                        <span className="button-tooltip-anchor composer-button-tooltip-anchor" data-tooltip="打开输入功能菜单">
                          <Button
                            aria-label="打开输入功能菜单"
                            className="composer-action-button composer-image-button composer-plus-button"
                          >
                            <img
                              aria-hidden="true"
                              alt=""
                              className="composer-button-image"
                              draggable={false}
                              src={INPUT_MENU_BUTTON_IMAGE}
                            />
                          </Button>
                        </span>
                      </Popover>
                      {active ? (
                        <span className="button-tooltip-anchor composer-button-tooltip-anchor" data-tooltip="停止">
                          <Button
                            danger
                            type="primary"
                            aria-label="停止"
                            className="composer-send-button composer-stop-button"
                            onClick={handleCancel}
                            loading={taskState.phase === 'cancelling'}
                            disabled={taskState.phase === 'submitting' && !currentTaskId}
                          >
                            <span aria-hidden="true" className="composer-stop-icon" />
                          </Button>
                        </span>
                      ) : (
                        <span className="button-tooltip-anchor composer-button-tooltip-anchor" data-tooltip="发送">
                          <Button
                            type="primary"
                            aria-label="发送"
                            className="composer-send-button composer-image-button"
                            onClick={handleSubmit}
                            disabled={!canSubmitComposer || uploadingFile}
                          >
                            <img
                              aria-hidden="true"
                              alt=""
                              className="composer-button-image"
                              draggable={false}
                              src={SEND_BUTTON_IMAGE}
                            />
                          </Button>
                        </span>
                      )}
                    </div>
                  </div>
                  <input
                    ref={uploadInputRef}
                    className="file-input-hidden"
                    aria-label="上传 JSON、CSV、Excel、TXT、VCF、图片或 PDF 文件"
                    type="file"
                    accept=".json,.csv,.xlsx,.xls,.txt,.vcf,.vcf.gz,.png,.jpg,.jpeg,.pdf,application/json,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/plain,image/png,image/jpeg,application/pdf"
                    disabled={!canUploadInCurrentComposer || uploadingFile}
                    onChange={(event) => void handleUploadFile(event.target.files?.[0])}
                  />
                </Space>
              </Card>
            </div>
            <Button
              type="primary"
              className="conversation-files-fab"
              aria-label={`打开当前对话文件面板，当前 ${pendingUploads.length} 个文件`}
              onClick={() => setFileDrawerOpen(true)}
              loading={uploadingFile}
            >
              <span aria-hidden="true">📎</span>
              <span>文件</span>
              {pendingUploads.length > 0 ? <span className="conversation-files-fab-count">{pendingUploads.length}</span> : null}
            </Button>
            <Drawer
              title="当前对话文件"
              placement="right"
              open={fileDrawerOpen}
              onClose={() => setFileDrawerOpen(false)}
              width={360}
              className="conversation-files-drawer"
              rootClassName="conversation-files-drawer-root"
            >
              <Space direction="vertical" size="middle" className="conversation-files-drawer-content">
                <Button
                  block
                  aria-label="选择 JSON、CSV、Excel、TXT、VCF、图片或 PDF 文件"
                  onClick={() => uploadInputRef.current?.click()}
                  disabled={!canUploadInCurrentComposer || uploadingFile}
                  loading={uploadingFile}
                >
                  上传文件
                </Button>
                {pendingUploads.length > 0 ? (
                  <Space direction="vertical" size="small" className="conversation-file-list" aria-label="当前对话文件列表">
                    {pendingUploads.map((upload) => (
                      <ConversationFileCard
                        key={upload.upload_id}
                        upload={upload}
                        deleting={deletingUploadIds.has(upload.upload_id)}
                        disabled={active}
                        onDelete={() => void handleDeleteUpload(upload)}
                      />
                    ))}
                  </Space>
                ) : (
                  <div className="conversation-files-empty">
                    <Typography.Text type="secondary">当前对话还没有上传文件。</Typography.Text>
                    <Typography.Text type="secondary">上传后，文件会保存在本地并可供本对话里的 Skill 使用。</Typography.Text>
                  </div>
                )}
              </Space>
            </Drawer>
          </section>
        </main>
      </Layout>
    </ConfigProvider>
  );
}

function LoginPage({ api, onLogin }: { api: ApiClient; onLogin: (result: AuthTokenResponse) => void | Promise<void> }) {
  const [username, setUsername] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmedUsername = username.trim();
  const canSubmit = Boolean(trimmedUsername);

  async function submitAuth() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.login({ username: trimmedUsername });
      await onLogin(result);
    } catch {
      setError('登录失败，请检查用户名后重试。');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Layout className="app-shell auth-shell">
      <Card className="login-card" title="登录小奥Agent">
        <Space direction="vertical" size="middle" className="login-form">
          {error ? <Alert type="error" showIcon message={error} /> : null}
          <Input
            aria-label="用户名"
            placeholder="用户名"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            onPressEnter={() => void submitAuth()}
            autoComplete="username"
          />
          <Typography.Text type="secondary">输入内部用户名后会签发当前浏览器的 Authorization Bearer token。</Typography.Text>
          <Button
            type="primary"
            block
            onClick={() => void submitAuth()}
            loading={submitting}
            disabled={!canSubmit}
          >
            登录
          </Button>
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
          <span className="button-tooltip-anchor" data-tooltip="刷新历史会话">
            <Button
              className="history-refresh-button"
              size="small"
              type="text"
              shape="circle"
              aria-label="刷新历史会话"
              icon={<ReloadOutlined aria-hidden="true" />}
              onClick={onRefresh}
              loading={loading}
            />
          </span>
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
            const isDeleting = deletingConversationIds.has(conversation.conversation_id);
            return (
              <div key={conversation.conversation_id} className={`history-row${isDeleting ? ' history-row-deleting' : ''}`} role="listitem">
                <button
                  type="button"
                  className={`history-item${active ? ' history-item-active' : ''}`}
                  disabled={interactionLocked || isDeleting}
                  aria-current={active ? 'page' : undefined}
                  aria-busy={isDeleting ? 'true' : undefined}
                  onClick={() => onSelectConversation(conversation.conversation_id)}
                >
                  <span className="history-item-title">{title}</span>
                  {isDeleting ? (
                    <span className="history-delete-indicator" role="status" aria-label={`正在删除历史会话 ${title}`}>
                      <Spin size="small" />
                    </span>
                  ) : null}
                </button>
                <div className="history-actions" aria-label={`历史会话操作 ${title}`}>
                  <Button
                    type="text"
                    size="small"
                    aria-label={`重命名历史会话 ${title}`}
                    loading={renamingConversationIds.has(conversation.conversation_id)}
                    disabled={loading || isDeleting}
                    onClick={() => onRenameConversation(conversation.conversation_id)}
                  >
                    重命名
                  </Button>
                  <Button
                    danger
                    type="text"
                    size="small"
                    aria-label={`删除历史会话 ${title}`}
                    loading={isDeleting}
                    disabled={loading || isDeleting}
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
    >
      <Space direction="vertical" size="small" className="sidebar-user-stack">
        <div>
          <Typography.Text type="secondary">当前用户</Typography.Text>
          <Typography.Text strong className="sidebar-username">{user.username}</Typography.Text>
        </div>
        <div className="sidebar-user-actions">
          <span className="button-tooltip-anchor sidebar-user-tooltip-anchor" data-tooltip="用户账户设置">
            <Button
              type="text"
              aria-label="用户账户设置"
              className="sidebar-account-settings-button"
              onClick={onAccountSettings}
            >
              <img
                src={ACCOUNT_SETTINGS_BUTTON_IMAGE}
                alt=""
                aria-hidden="true"
                draggable={false}
                className="sidebar-account-settings-image"
              />
            </Button>
          </span>
          <Button className="sidebar-logout-button" danger onClick={onLogout}>退出登录</Button>
        </div>
      </Space>
    </Card>
  );
}

function EmptyWelcome() {
  const welcomePrompt = useMemo(() => pickWelcomePrompt(), []);

  return (
    <div className="empty-welcome">
      <Typography.Title level={4}>{welcomePrompt}</Typography.Title>
    </div>
  );
}

function interruptSubmitMetadata(interrupt: PendingInterrupt, uploads: UploadFileResponse[] = [], selectedSheets: Record<string, string> = {}): Record<string, unknown> {
  const metadata: Record<string, unknown> = {
    interrupt_id: interrupt.interruptId,
  };
  const uploadIds = uploads.map((upload) => upload.upload_id);
  if (uploadIds.length > 0) {
    metadata.upload_ids = uploadIds;
  }
  if (Object.keys(selectedSheets).length > 0) {
    metadata.upload_sheet_selections = selectedSheets;
  }
  return metadata;
}

function uploadAnswerDisplayText(uploads: UploadFileResponse[]): string {
  if (uploads.length === 0) return '';
  return `已上传文件：${uploads.map((upload) => upload.filename).join('、')}`;
}

function uploadFileTypeLabel(upload: UploadFileResponse): string {
  switch (upload.file_type) {
    case 'spreadsheet':
      return 'Excel';
    case 'text':
      return 'TXT';
    case 'vcf':
      return 'VCF';
    case 'image':
      return '图片';
    case 'pdf':
      return 'PDF';
    case 'json':
      return 'JSON';
    case 'csv':
      return 'CSV';
    default:
      return upload.file_type || '文件';
  }
}

function uploadFileSummaryParts(upload: UploadFileResponse): string[] {
  const preview = upload.preview;
  const columns = preview.columns ?? [];
  const parts = [uploadFileTypeLabel(upload)];
  if (preview.source_encoding) parts.push(preview.source_encoding);
  if (typeof preview.row_count === 'number') parts.push(`${preview.row_count} 行`);
  if (columns.length > 0) parts.push(columns.slice(0, 3).join('/'));
  if (preview.requires_sheet_selection) parts.push('需选择 sheet');
  if (preview.columns_truncated || preview.excel_sheets_truncated) parts.push('已裁剪摘要');
  return parts;
}

function formatFileSize(sizeBytes: number): string {
  if (!Number.isFinite(sizeBytes) || sizeBytes < 0) return '未知大小';
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`;
}

function ConversationFileCard({
  upload,
  deleting,
  disabled,
  onDelete,
}: {
  upload: UploadFileResponse;
  deleting: boolean;
  disabled: boolean;
  onDelete: () => void;
}) {
  const summaryParts = uploadFileSummaryParts(upload);
  const columns = upload.preview.columns ?? [];

  return (
    <div className="conversation-file-card">
      <div className="conversation-file-card-header">
        <div className="conversation-file-card-title">
          <Typography.Text strong ellipsis={{ tooltip: upload.filename }} className="conversation-file-name">
            {upload.filename}
          </Typography.Text>
          <Typography.Text type="secondary" className="conversation-file-meta">
            {summaryParts.join(' · ')}
          </Typography.Text>
        </div>
        <Tag color="green" className="conversation-file-ready-tag">Skill 可用</Tag>
      </div>
      {columns.length > 3 ? (
        <Typography.Text type="secondary" className="conversation-file-extra">
          另有 {columns.length - 3} 个字段可在 Skill 中读取
        </Typography.Text>
      ) : null}
      <div className="conversation-file-actions">
        <Typography.Text type="secondary" className="conversation-file-size">
          {formatFileSize(upload.size_bytes)}
        </Typography.Text>
        <Button
          danger
          type="text"
          size="small"
          loading={deleting}
          disabled={disabled || deleting}
          aria-label={`删除文件 ${upload.filename}`}
          onClick={onDelete}
        >
          删除
        </Button>
      </div>
    </div>
  );
}

function sheetSelectionDisplayText(field: SheetSelectionField | null, selections: Record<string, string>): string {
  if (!field) return '';
  const parts = field.required_upload_ids
    .map((uploadId) => {
      const label = field.labels_by_upload_id?.[uploadId] ?? uploadId;
      const sheet = selections[uploadId];
      return sheet ? `${label}=${sheet}` : '';
    })
    .filter(Boolean);
  return parts.length > 0 ? `已选择 sheet：${parts.join('；')}` : '';
}

function isInterruptKeepOpenResponse(response: { action?: string | null; answer_payload?: Record<string, unknown> | null }): boolean {
  const action = response.action || '';
  if (action === 'interrupt_clarification_answer' || action === 'clarification_answer') return true;
  if (action === 'interrupt_mixed_processed' || action === 'interrupt_schema_switched') {
    const payload = response.answer_payload || {};
    return payload.will_resume !== true || payload.requires_confirmation === true;
  }
  return false;
}

function InterruptQuestionText({ interrupt }: { interrupt: PendingInterrupt }) {
  return <MarkdownText content={interrupt.question} />;
}

function isNaturalLanguageInterrupt(requiredFields: Record<string, unknown>): boolean {
  const resolution = requiredFields._sql_query_resolution;
  if (!resolution || typeof resolution !== 'object') return false;
  return (resolution as { presentation?: unknown }).presentation === 'natural_language';
}

function InterruptComposerStatus({ onCancel, cancelling }: { onCancel: () => void; cancelling: boolean }) {
  return (
    <div className="interrupt-composer-status" role="status" aria-live="polite">
      <span className="interrupt-composer-status-text">等待补充 · 下一条消息将继续当前任务</span>
      <Button danger type="text" size="small" aria-label="结束任务" onClick={onCancel} loading={cancelling}>
        结束任务
      </Button>
    </div>
  );
}

function interruptAcceptsUpload(interrupt: PendingInterrupt): boolean {
  const hasVisibleUploadField = interruptVisibleFieldValues(interrupt).some((field) => {
    if (!field || typeof field !== 'object') return false;
    const metadata = field as { accepts_upload?: unknown; type?: unknown };
    return metadata.accepts_upload === true || ['artifact', 'file', 'data'].includes(String(metadata.type ?? ''));
  });
  if (hasVisibleUploadField) return true;
  return interruptSlotCollectionRefSlots(interrupt).some((slot) => ['artifact', 'file', 'data'].includes(slot.type));
}

function interruptSheetSelectionField(interrupt: PendingInterrupt): SheetSelectionField | null {
  const raw = interrupt.requiredFields?.upload_sheet_selections;
  if (!raw || typeof raw !== 'object') return null;
  const field = raw as {
    required_upload_ids?: unknown;
    options_by_upload_id?: unknown;
    labels_by_upload_id?: unknown;
  };
  if (!Array.isArray(field.required_upload_ids) || !field.options_by_upload_id || typeof field.options_by_upload_id !== 'object') {
    return null;
  }
  const requiredUploadIds = field.required_upload_ids.filter((item): item is string => typeof item === 'string' && item.length > 0);
  const optionsByUploadId: Record<string, string[]> = {};
  for (const [uploadId, options] of Object.entries(field.options_by_upload_id as Record<string, unknown>)) {
    if (Array.isArray(options)) {
      optionsByUploadId[uploadId] = options.filter((item): item is string => typeof item === 'string' && item.length > 0);
    }
  }
  const labelsByUploadId: Record<string, string> = {};
  if (field.labels_by_upload_id && typeof field.labels_by_upload_id === 'object') {
    for (const [uploadId, label] of Object.entries(field.labels_by_upload_id as Record<string, unknown>)) {
      if (typeof label === 'string' && label.length > 0) labelsByUploadId[uploadId] = label;
    }
  }
  return {
    required_upload_ids: requiredUploadIds,
    options_by_upload_id: optionsByUploadId,
    labels_by_upload_id: labelsByUploadId,
  };
}

function selectedSheetPayload(field: SheetSelectionField, answerText: string): Record<string, string> {
  const payload: Record<string, string> = {};
  const answer = answerText.trim();
  if (!answer) return {};
  if (field.required_upload_ids.length === 1) {
    const uploadId = field.required_upload_ids[0];
    const options = field.options_by_upload_id[uploadId] ?? [];
    const matched = options.find((option) => option === answer)
      ?? options.find((option) => option.toLowerCase() === answer.toLowerCase());
    return { [uploadId]: matched ?? answer };
  }

  const labelsByUploadId = field.labels_by_upload_id ?? {};
  const parts = answer.split(/[;\n；]+/).map((part) => part.trim()).filter(Boolean);
  for (const part of parts) {
    const match = part.match(/^(.+?)[=:：]\s*(.+)$/);
    if (!match) continue;
    const rawLabel = match[1].trim();
    const rawSheet = match[2].trim();
    const uploadId = field.required_upload_ids.find((candidate) => (
      candidate === rawLabel || labelsByUploadId[candidate] === rawLabel
    ));
    if (!uploadId || !rawSheet) continue;
    const options = field.options_by_upload_id[uploadId] ?? [];
    const matched = options.find((option) => option === rawSheet)
      ?? options.find((option) => option.toLowerCase() === rawSheet.toLowerCase());
    payload[uploadId] = matched ?? rawSheet;
  }
  return payload;
}

function interruptVisibleFieldNames(interrupt: PendingInterrupt): string[] {
  return Object.keys(interrupt.requiredFields ?? {}).filter((field) => !isReservedInterruptField(field));
}

function interruptVisibleFieldValues(interrupt: PendingInterrupt): unknown[] {
  return Object.entries(interrupt.requiredFields ?? {})
    .filter(([field]) => !isReservedInterruptField(field))
    .map(([, value]) => value);
}

function interruptSlotCollectionRef(interrupt: PendingInterrupt): SlotCollectionRefField | null {
  const raw = interrupt.requiredFields?._slot_collection_ref;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const field = raw as { collection_id?: unknown; slots?: unknown };
  const collectionId = typeof field.collection_id === 'string' ? field.collection_id : '';
  if (!collectionId) return null;
  const slots: SlotCollectionRefSlot[] = [];
  if (Array.isArray(field.slots)) {
    for (const rawSlot of field.slots) {
      if (!rawSlot || typeof rawSlot !== 'object' || Array.isArray(rawSlot)) continue;
      const slot = rawSlot as {
        name?: unknown;
        label?: unknown;
        type?: unknown;
        status?: unknown;
        required_now?: unknown;
      };
      const name = typeof slot.name === 'string' && slot.name.length > 0 ? slot.name : '';
      if (!name) continue;
      const label = typeof slot.label === 'string' && slot.label.length > 0 ? slot.label : name;
      slots.push({
        name,
        label,
        type: typeof slot.type === 'string' ? slot.type : 'string',
        status: typeof slot.status === 'string' ? slot.status : '',
        requiredNow: slot.required_now === true,
      });
    }
  }
  return { collectionId, slots };
}

function interruptSlotCollectionRefSlots(interrupt: PendingInterrupt): SlotCollectionRefSlot[] {
  const ref = interruptSlotCollectionRef(interrupt);
  if (!ref) return [];
  const activeSlots = ref.slots.filter((slot) => slot.requiredNow || ['missing', 'invalid'].includes(slot.status));
  return activeSlots.length > 0 ? activeSlots : ref.slots;
}

function isReservedInterruptField(field: string): boolean {
  return field.startsWith('_');
}

function interruptAnswerPlaceholder(interrupt: PendingInterrupt): string {
  return interrupt.question.trim() || '请补充当前任务所需信息';
}

function assistantActivityText(state: TaskEventState, progressText: string): string | undefined {
  if (state.assistantText) return undefined;
  if (!state.skillStatuses.length) return progressText;
  if (state.currentCapabilityId?.startsWith('skill.')) return undefined;
  return progressText;
}

function MessageBubble({
  message,
  onDownloadArtifact,
}: {
  message: ConversationMessage;
  onDownloadArtifact: (result: FileArtifactResult) => void;
}) {
  const className = message.role === 'user' ? 'message message-user' : 'message message-assistant';
  const shouldShowContent = Boolean(message.content);
  const shouldShowReasoning = message.role === 'assistant' && (message.reasoningRequested || message.reasoningContent);
  const shouldShowAssistantActions = message.role === 'assistant'
    && (Boolean(message.finalContentLoaded) || Boolean(message.replyCompleted))
    && Boolean(message.content.trim());
  return (
    <div className={className}>
      <div className="message-meta">{message.role === 'user' ? '你' : '主代理'}</div>
      {message.role === 'assistant' ? <SkillStatusLines statuses={message.skillStatuses} /> : null}
      <div className="message-body">
        {shouldShowReasoning ? (
          <ReasoningBox content={message.reasoningContent ?? ''} complete={message.reasoningComplete} />
        ) : null}
        {message.interruptPrompt ? (
          <InterruptQuestionText interrupt={message.interruptPrompt} />
        ) : shouldShowContent || message.artifactDisplays?.length ? (
          <>
            {shouldShowContent ? <MarkdownText content={message.content} /> : null}
            {message.artifactDisplays?.map((display) => (
              <CapabilityArtifactPanel
                key={capabilityArtifactDisplayKey(display)}
                display={display}
                onDownloadArtifact={onDownloadArtifact}
              />
            ))}
          </>
        ) : message.activityText ? (
          <ActivityNotice text={message.activityText} status={message.activityStatus} />
        ) : (
          <ActivityNotice text="正在等待回答..." />
        )}
      </div>
      {shouldShowAssistantActions ? (
        <div className="message-actions" aria-label="回复操作">
          <span className="button-tooltip-anchor message-action-tooltip-anchor" data-tooltip="复制">
            <Button
              type="text"
              size="small"
              shape="circle"
              className="message-action-button"
              aria-label="复制"
              icon={<CopyOutlined aria-hidden="true" />}
              onClick={() => {
                void copyTextToClipboard(message.content);
              }}
            />
          </span>
        </div>
      ) : null}
    </div>
  );
}

function SkillStatusLines({ statuses }: { statuses?: SkillStatusLine[] }) {
  if (!statuses?.length) return null;
  return (
    <div className="skill-status-lines" role="status" aria-live="polite">
      {statuses.map((status) => (
        <div
          key={status.key}
          className={`skill-status-line ${status.status === 'failed' ? 'skill-status-line-failed' : ''}`}
        >
          <span>{status.label}：{status.statusText}</span>
        </div>
      ))}
    </div>
  );
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (!text) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // Fall through to the legacy copy path when browser clipboard permissions reject the call.
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.top = '-9999px';
  textarea.style.opacity = '0';
  try {
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
  } finally {
    textarea.remove();
  }
}

function CapabilityArtifactPanel({
  display,
  onDownloadArtifact,
}: {
  display: CapabilityArtifactDisplay;
  onDownloadArtifact: (result: FileArtifactResult) => void;
}) {
  if (display.kind === 'data_query') {
    return <DataQueryResultCard result={display.result} />;
  }
  if (display.kind === 'ocr_raw_text') {
    return <OcrRawTextCard result={display.result} />;
  }
  if (display.kind === 'file') {
    return <FileArtifactCard result={display.result} onDownloadArtifact={onDownloadArtifact} />;
  }
  return null;
}

function capabilityArtifactDisplayKey(display: CapabilityArtifactDisplay): string {
  if (display.kind === 'data_query') {
    return `${display.kind}:${display.result.sourceArtifactIds.join(',')}`;
  }
  if (display.kind === 'file') {
    return `${display.kind}:${display.result.artifactId}`;
  }
  if (display.kind === 'ocr_raw_text') {
    return `${display.kind}:${display.result.artifactId}`;
  }
  return 'capability-artifact';
}

function OcrRawTextCard({ result }: { result: Extract<CapabilityArtifactDisplay, { kind: 'ocr_raw_text' }>['result'] }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <Card
      size="small"
      className="capability-card ocr-raw-text-card"
      title={result.title || 'OCR 回传原文'}
      extra={(
        <Button type="link" size="small" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起原文' : '展开原文'}
        </Button>
      )}
    >
      <Space direction="vertical" size="small" className="ocr-raw-text-stack">
        <Space wrap size="small">
          {result.filename ? <Tag>{result.filename}</Tag> : null}
          {result.status ? <Tag color={result.status === 'succeeded' ? 'green' : undefined}>{result.status}</Tag> : null}
        </Space>
        <pre className={`ocr-raw-text-content ${expanded ? 'ocr-raw-text-content-expanded' : ''}`}>{result.rawText}</pre>
      </Space>
    </Card>
  );
}

function FileArtifactCard({ result, onDownloadArtifact }: { result: FileArtifactResult; onDownloadArtifact: (result: FileArtifactResult) => void }) {
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
        <Button onClick={() => onDownloadArtifact(result)}>
          下载
        </Button>
      </Space>
    </Card>
  );
}

function ActivityNotice({ text, status = 'pending' }: { text: string; status?: ActivityNoticeStatus }) {
  const terminalWarning = status === 'failed' || status === 'cancelled';
  const iconLabel = status === 'cancelled' ? '任务已取消' : '任务失败';
  return (
    <div className={`activity-notice ${terminalWarning ? 'activity-notice-failed' : ''}`} role="status" aria-live="polite">
      {terminalWarning ? <ExclamationCircleFilled className="activity-notice-error-icon" aria-label={iconLabel} /> : <Spin size="small" />}
      <Typography.Text type={terminalWarning ? 'danger' : 'secondary'}>{text}</Typography.Text>
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
        {content ? <MarkdownText content={content} /> : <Typography.Text className="reasoning-placeholder" type="secondary">{placeholder}</Typography.Text>}
      </div>
    </section>
  );
}

function messageFromHistory(message: MessageResponse): ConversationMessage | null {
  if (message.role !== 'user' && message.role !== 'assistant') return null;
  const assistantReplyCompleted = message.role === 'assistant' && message.stream_status === 'complete';
  const artifactDisplays = message.role === 'assistant'
    ? parseCapabilityArtifactDisplays(message.artifacts ?? [])
    : [];
  return {
    id: message.message_id,
    role: message.role,
    content: message.content,
    mode: 'chat',
    finalContentLoaded: assistantReplyCompleted || undefined,
    replyCompleted: assistantReplyCompleted || undefined,
    artifactDisplays: artifactDisplays.length > 0 ? artifactDisplays : undefined,
  };
}

function conversationStorageKey(username: string): string {
  return `${CONVERSATION_STORAGE_KEY_PREFIX}.${username}`;
}

function readStoredAccessToken(): string | null {
  try {
    return window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStoredAccessToken(token: string | null): void {
  try {
    if (token) {
      window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
    } else {
      window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    }
  } catch {
    // Ignore storage failures; auth state still lives in memory for this tab.
  }
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
