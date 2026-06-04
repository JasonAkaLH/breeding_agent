import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from 'react';
import { CopyOutlined, ExclamationCircleFilled, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, ConfigProvider, Flex, Input, Layout, Popover, Select, Space, Spin, Switch, Tag, Typography, theme, type ThemeConfig } from 'antd';
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
}

interface SheetSelectionField {
  required_upload_ids: string[];
  options_by_upload_id: Record<string, string[]>;
  labels_by_upload_id?: Record<string, string>;
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
const INTERRUPT_FIELD_LABELS: Record<string, string> = {
  blocks: '区组数/重复数',
  ck_spec: 'CK 起始位置和间隔',
  crop: '作物类型',
  design: '设计类型',
  field_data: '田间表型数据文件',
  file_path: '图片/PDF 文件',
  material_data: '试验材料文件',
  missing_info: '补充信息',
  ncols: '田块列数',
  query: '查询问题',
  region: '地区',
  rice_input: '水稻 VCF/gene_check 文件',
  sample: '样本名',
  samples: '样本列表',
  upload_sheet_selections: 'Excel sheet 选择',
  variety: '品种名称',
  route_id: '查询范围',
  year_range: '年份范围',
};
const INTERRUPT_OPTION_LABELS: Record<string, string> = {
  corn: '玉米',
  cotton: '棉花',
  rice: '水稻',
  soybean: '大豆',
  wheat: '小麦',
};
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
  const [sheetSelections, setSheetSelections] = useState<Record<string, string>>({});
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

  useEffect(() => {
    setSheetSelections({});
  }, [pendingInterrupt?.interruptId]);

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
  const pendingSheetSelectionField = pendingInterrupt ? interruptSheetSelectionField(pendingInterrupt) : null;
  const canSubmitSheetSelectionAnswer = pendingSheetSelectionField !== null && sheetSelectionComplete(pendingSheetSelectionField, sheetSelections);
  const canSubmitUploadOnlyInterruptAnswer = pendingInterruptAcceptsUpload && pendingUploads.length > 0;
  const canUploadInCurrentComposer = !active && (!pendingInterrupt || pendingInterruptAcceptsUpload);
  const canSubmitComposer = !slashInputBlocked && (
    Boolean(input.trim())
    || selectedSkillCommand !== null
    || directSlashParse.kind === 'matched'
    || canSubmitUploadOnlyInterruptAnswer
    || canSubmitSheetSelectionAnswer
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
    if (!content && intent.kind !== 'ready' && !canSubmitUploadOnlyInterruptAnswer && !canSubmitSheetSelectionAnswer) return;
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
    const selectedSheets = sheetField ? selectedSheetPayload(sheetField, sheetSelections) : {};
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
      reasoningRequested: false,
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
      await api.answerInterrupt(interrupt.taskId, interrupt.interruptId, buildInterruptAnswerPayload(interrupt, content, uploads, selectedSheets));
      if (!isCurrentRestoreGeneration(generation, targetConversationId)) return;
      taskPresentationModesRef.current.set(interrupt.taskId, interrupt.mode);
      setPendingUploads([]);
      setPendingInterrupt(null);
      updateCurrentTaskId(interrupt.taskId);
      subscribeToTask(interrupt.taskId, assistantMessage.id, generation, targetConversationId);
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
      };
      setPendingInterrupt(pending);
      updateAssistantMessage(assistantId, {
        content: '',
        interruptPrompt: pending,
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
          interruptPrompt: previousPendingInterrupt,
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
        aria-label="选择 JSON、CSV、VCF、图片或 PDF 文件"
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
            <div ref={conversationListRef} className="conversation-list" aria-label="对话内容" onScroll={handleConversationScroll}>
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
              <div className="chat-upload-drop-hint" aria-hidden={!draggingUpload}>释放文件以上传到当前对话</div>
              {pendingInterrupt ? <InterruptInputBanner interrupt={pendingInterrupt} onCancel={handleCancel} cancelling={taskState.phase === 'cancelling'} /> : null}
              {pendingSheetSelectionField ? (
                <SheetSelectionControls
                  field={pendingSheetSelectionField}
                  value={sheetSelections}
                  onChange={setSheetSelections}
                />
              ) : null}
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
                          {upload.file_type === 'spreadsheet' ? ' · Excel' : ''}
                          {upload.file_type === 'vcf' ? ' · VCF' : ''}
                          {upload.preview.source_encoding ? ` · ${upload.preview.source_encoding}` : ''}
                          {typeof upload.preview.row_count === 'number' ? ` · ${upload.preview.row_count} 行` : ''}
                          {upload.preview.columns.length > 0 ? ` · ${upload.preview.columns.slice(0, 3).join('/')}` : ''}
                          {upload.preview.requires_sheet_selection ? ' · 需选择 sheet' : ''}
                          {upload.preview.columns_truncated || upload.preview.excel_sheets_truncated ? ' · 已裁剪摘要' : ''}
                          <Button
                            type="link"
                            danger
                            size="small"
                            aria-label={`删除文件 ${upload.filename}`}
                            loading={deletingUploadIds.has(upload.upload_id)}
                            disabled={active}
                            onClick={() => void handleDeleteUpload(upload)}
                          >
                            删除
                          </Button>
                        </Tag>
                      ))}
                    </Space>
                  ) : null}
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
                  <input
                    ref={uploadInputRef}
                    className="file-input-hidden"
                    aria-label="上传 JSON、CSV、Excel、VCF、图片或 PDF 文件"
                    type="file"
                    accept=".json,.csv,.xlsx,.xls,.vcf,.vcf.gz,.png,.jpg,.jpeg,.pdf,application/json,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,image/png,image/jpeg,application/pdf"
                    disabled={!canUploadInCurrentComposer || uploadingFile}
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

function buildInterruptAnswerPayload(
  interrupt: PendingInterrupt,
  content: string,
  uploads: UploadFileResponse[] = [],
  selectedSheets: Record<string, string> = {},
): Record<string, unknown> {
  if (interruptSheetSelectionField(interrupt)) {
    return { upload_sheet_selections: selectedSheets };
  }
  const fieldNames = interruptVisibleFieldNames(interrupt);
  const uploadIds = uploads.map((upload) => upload.upload_id);
  const filenames = uploads.map((upload) => upload.filename);
  const payload: Record<string, unknown> = {};
  if (fieldNames.length === 1) {
    payload[fieldNames[0]] = uploadIds.length > 0
      ? { text: content, upload_ids: uploadIds, filenames }
      : content;
  } else {
    payload.answer = content;
  }
  if (uploadIds.length > 0) {
    payload.upload_ids = uploadIds;
  }
  return payload;
}

function uploadAnswerDisplayText(uploads: UploadFileResponse[]): string {
  if (uploads.length === 0) return '';
  return `已上传文件：${uploads.map((upload) => upload.filename).join('、')}`;
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

function InterruptPromptCard({ interrupt }: { interrupt: PendingInterrupt }) {
  if (interruptIsScalarDialogue(interrupt)) {
    return <Typography.Paragraph className="interrupt-card-question">{interrupt.question}</Typography.Paragraph>;
  }
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
      <Typography.Text className="interrupt-card-hint" type="secondary">{interruptAcceptsUpload(interrupt) ? '可直接上传文件，或回复文字后继续当前任务。' : '回复后将继续当前任务。'}</Typography.Text>
    </section>
  );
}

function SheetSelectionControls({
  field,
  value,
  onChange,
}: {
  field: SheetSelectionField;
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  return (
    <Card size="small" className="sheet-selection-card" title="选择 Excel sheet">
      <Space direction="vertical" size="small" className="sheet-selection-space">
        {field.required_upload_ids.map((uploadId) => {
          const options = field.options_by_upload_id[uploadId] ?? [];
          const label = field.labels_by_upload_id?.[uploadId] ?? uploadId;
          return (
            <div key={uploadId} className="sheet-selection-row">
              <Typography.Text>{label}</Typography.Text>
              <select
                aria-label={`选择 ${label} 的 sheet`}
                value={value[uploadId] || ''}
                onChange={(event) => onChange({ ...value, [uploadId]: event.target.value })}
                style={{ minWidth: 180 }}
              >
                <option value="">选择 sheet</option>
                {options.map((option) => <option key={option} value={option}>{option}</option>)}
              </select>
            </div>
          );
        })}
      </Space>
    </Card>
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
  return interruptVisibleFieldNames(interrupt).map((field) => INTERRUPT_FIELD_LABELS[field] ?? field);
}

function interruptAcceptsUpload(interrupt: PendingInterrupt): boolean {
  return interruptVisibleFieldValues(interrupt).some((field) => {
    if (!field || typeof field !== 'object') return false;
    const metadata = field as { accepts_upload?: unknown; type?: unknown };
    return metadata.accepts_upload === true || ['artifact', 'file', 'data'].includes(String(metadata.type ?? ''));
  });
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

function sheetSelectionComplete(field: SheetSelectionField, selections: Record<string, string>): boolean {
  return field.required_upload_ids.every((uploadId) => {
    const selected = selections[uploadId];
    return Boolean(selected && (field.options_by_upload_id[uploadId] ?? []).includes(selected));
  });
}

function selectedSheetPayload(field: SheetSelectionField, selections: Record<string, string>): Record<string, string> {
  const payload: Record<string, string> = {};
  for (const uploadId of field.required_upload_ids) {
    const selected = selections[uploadId];
    if (selected && (field.options_by_upload_id[uploadId] ?? []).includes(selected)) {
      payload[uploadId] = selected;
    }
  }
  return payload;
}

function interruptFieldSummary(interrupt: PendingInterrupt): string {
  const labels = interruptFieldLabels(interrupt);
  return labels.length > 0 ? labels.join('、') : '补充信息';
}

function interruptOptionLabels(interrupt: PendingInterrupt): string[] {
  return interruptVisibleFieldValues(interrupt)
    .flatMap((field) => extractInterruptOptions(field))
    .map((option) => INTERRUPT_OPTION_LABELS[option] ?? option);
}

function interruptVisibleFieldNames(interrupt: PendingInterrupt): string[] {
  return Object.keys(interrupt.requiredFields ?? {}).filter((field) => !isReservedInterruptField(field));
}

function interruptVisibleFieldValues(interrupt: PendingInterrupt): unknown[] {
  return Object.entries(interrupt.requiredFields ?? {})
    .filter(([field]) => !isReservedInterruptField(field))
    .map(([, value]) => value);
}

function isReservedInterruptField(field: string): boolean {
  return field.startsWith('_');
}

function interruptIsScalarDialogue(interrupt: PendingInterrupt): boolean {
  if (!interrupt.requiredFields || !Object.prototype.hasOwnProperty.call(interrupt.requiredFields, '_slot_collection')) return false;
  const fields = interruptVisibleFieldValues(interrupt);
  if (fields.length === 0) return false;
  if (interruptSheetSelectionField(interrupt) || interruptAcceptsUpload(interrupt)) return false;
  return fields.every((field) => {
    if (!field || typeof field !== 'object') return true;
    const metadata = field as { type?: unknown };
    return !['artifact', 'file', 'data', 'sheet_selection'].includes(String(metadata.type ?? ''));
  });
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
  if (interruptSheetSelectionField(interrupt)) {
    return '请选择 Excel sheet 后发送';
  }
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
          <InterruptPromptCard interrupt={message.interruptPrompt} />
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
        <pre className={`ocr-raw-text-content ${expanded ? 'ocr-raw-text-content-expanded' : ''}`}>
          {result.rawText}
        </pre>
      </Space>
    </Card>
  );
}

function FileArtifactCard({
  result,
  onDownloadArtifact,
}: {
  result: FileArtifactResult;
  onDownloadArtifact: (result: FileArtifactResult) => void;
}) {
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
