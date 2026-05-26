import type {
  CancelTaskResponse,
  AnswerInterruptResponse,
  AuthTokenResponse,
  AuthUserResponse,
  CapabilityListResponse,
  ChatMode,
  ConversationListResponse,
  ConversationMessagesResponse,
  ConversationSummaryResponse,
  DeleteConversationResponse,
  DeleteUploadResponse,
  LogoutResponse,
  TaskInterruptsResponse,
  TaskListResponse,
  MessageAcceptedResponse,
  ModelEdition,
  ModelEditionsResponse,
  ReasoningEffort,
  SubmitMessageRequest,
  TaskArtifactsResponse,
  TaskGraphResponse,
  TaskSummaryResponse,
  UploadFileResponse,
  UploadListResponse,
} from './types';

export interface UiModeOption {
  key: ChatMode;
  label: string;
  capabilityId: string | null;
}

export interface SubmitMessageInput {
  conversationId: string;
  content: string;
  mode: ChatMode;
  modelEdition?: ModelEdition;
  deepThinking?: boolean;
  reasoningEffort?: ReasoningEffort;
  clientMessageId?: string;
  metadata?: Record<string, unknown>;
  capabilityId?: string | null;
}

export interface ApiClient {
  uiModes: UiModeOption[];
  login(input: { username: string }): Promise<AuthTokenResponse>;
  logout(): Promise<LogoutResponse>;
  me(): Promise<AuthUserResponse>;
  refreshToken(): Promise<AuthTokenResponse>;
  listConversationUploads(conversationId: string): Promise<UploadListResponse>;
  deleteConversationUpload(conversationId: string, uploadId: string): Promise<DeleteUploadResponse>;
  uploadConversationFile(conversationId: string, file: File): Promise<UploadFileResponse>;
  listCapabilities(): Promise<CapabilityListResponse>;
  getModelEditions(): Promise<ModelEditionsResponse>;
  submitMessage(input: SubmitMessageInput): Promise<MessageAcceptedResponse>;
  listConversations(): Promise<ConversationListResponse>;
  listConversationMessages(conversationId: string): Promise<ConversationMessagesResponse>;
  deleteConversation(conversationId: string): Promise<DeleteConversationResponse>;
  renameConversation(conversationId: string, title: string): Promise<ConversationSummaryResponse>;
  listConversationTasks(conversationId: string, scope?: 'unfinished' | 'all'): Promise<TaskListResponse>;
  getTask(taskId: string): Promise<TaskSummaryResponse>;
  cancelTask(taskId: string): Promise<CancelTaskResponse>;
  getTaskArtifacts(taskId: string): Promise<TaskArtifactsResponse>;
  downloadArtifact(artifactId: string, filename: string): Promise<void>;
  getTaskGraph(taskId: string): Promise<TaskGraphResponse>;
  listInterrupts(taskId: string): Promise<TaskInterruptsResponse>;
  answerInterrupt(taskId: string, interruptId: string, answerPayload: Record<string, unknown>): Promise<AnswerInterruptResponse>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly userMessage: string;

  constructor(status: number, detail: unknown, userMessage: string) {
    super(userMessage);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.userMessage = userMessage;
  }
}

export const UI_MODES: UiModeOption[] = [
  { key: 'chat', label: '普通对话', capabilityId: null },
];
interface CreateApiClientOptions {
  baseUrl?: string;
  fetcher?: typeof fetch;
  accessToken?: string;
  authHeaderProvider?: () => string | null | undefined;
  onUnauthorized?: () => void;
}

export function createApiClient(options: CreateApiClientOptions = {}): ApiClient {
  const baseUrl = normalizeBaseUrl(options.baseUrl ?? import.meta.env.VITE_API_BASE_URL ?? '');
  const fetcher = options.fetcher ?? fetch.bind(globalThis);

  function authHeaders(): Record<string, string> {
    const token = options.authHeaderProvider?.() ?? options.accessToken;
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetcher(`${baseUrl}${path}`, {
      ...init,
      credentials: init?.credentials ?? 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
        ...(init?.headers ?? {}),
      },
    });
    if (!response.ok) {
      if (response.status === 401) {
        options.onUnauthorized?.();
      }
      throw await toApiError(response);
    }
    return (await response.json()) as T;
  }

  return {
    uiModes: UI_MODES,
    login: (input) => request<AuthTokenResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: input.username }),
    }),
    logout: () => request<LogoutResponse>('/api/v1/auth/logout', { method: 'POST' }),
    me: () => request<AuthUserResponse>('/api/v1/auth/me'),
    refreshToken: () => request<AuthTokenResponse>('/api/v1/auth/refresh-token', { method: 'POST' }),
    listConversationUploads: (conversationId) => request<UploadListResponse>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}/uploads`,
    ),
    deleteConversationUpload: (conversationId, uploadId) => request<DeleteUploadResponse>(
      '/api/v1/conversations/uploads',
      { method: 'DELETE', body: JSON.stringify({ conversation_id: conversationId, upload_id: uploadId }) },
    ),
    listCapabilities: () => request<CapabilityListResponse>('/api/v1/capabilities'),
    getModelEditions: () => request<ModelEditionsResponse>('/api/v1/config/model-editions'),
    uploadConversationFile: async (conversationId, file) => {
      const formData = new FormData();
      formData.append('conversation_id', conversationId);
      formData.append('file', file);
      const multipartAuthHeaders = authHeaders();
      const response = await fetcher(`${baseUrl}/api/v1/conversations/uploads`, {
        method: 'POST',
        credentials: 'same-origin',
        ...(Object.keys(multipartAuthHeaders).length > 0 ? { headers: multipartAuthHeaders } : {}),
        body: formData,
      });
      if (!response.ok) {
        if (response.status === 401) {
          options.onUnauthorized?.();
        }
        throw await toApiError(response);
      }
      return (await response.json()) as UploadFileResponse;
    },
    submitMessage: (input) => {
      const mode = UI_MODES.find((candidate) => candidate.key === input.mode);
      if (!mode) {
        throw new ApiError(0, null, '当前对话模式不可用，请刷新后重试。');
      }
      const explicitCapabilityId = input.capabilityId ?? null;
      const capabilityId = explicitCapabilityId || mode.capabilityId;
      const deepThinking = input.deepThinking ?? false;
      const reasoningEffort = deepThinking ? (input.reasoningEffort ?? 'minimal') : 'minimal';
      const body: SubmitMessageRequest = {
        conversation_id: input.conversationId,
        content: input.content,
        routing_mode: capabilityId ? 'force_capability' : 'auto',
        capability_id: capabilityId,
        client_message_id: input.clientMessageId ?? null,
        ...(input.modelEdition ? { model_edition: input.modelEdition } : {}),
        metadata: {
          ...(input.metadata ?? {}),
          deep_thinking: deepThinking,
          main_agent_reasoning_effort: reasoningEffort,
        },
      };
      return request<MessageAcceptedResponse>('/api/v1/conversations/chat-messages', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },
    listConversations: () => request<ConversationListResponse>('/api/v1/conversations'),
    listConversationMessages: (conversationId) => request<ConversationMessagesResponse>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
    ),
    deleteConversation: (conversationId) => request<DeleteConversationResponse>(
      '/api/v1/conversations',
      { method: 'DELETE', body: JSON.stringify({ conversation_id: conversationId }) },
    ),
    renameConversation: (conversationId, title) => request<ConversationSummaryResponse>(
      '/api/v1/conversations',
      { method: 'PATCH', body: JSON.stringify({ conversation_id: conversationId, title }) },
    ),
    listConversationTasks: (conversationId, scope = 'unfinished') => request<TaskListResponse>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}/tasks?scope=${encodeURIComponent(scope)}`,
    ),
    getTask: (taskId) => request<TaskSummaryResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}`),
    cancelTask: (taskId) => request<CancelTaskResponse>('/api/v1/tasks/cancel', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
    }),
    listInterrupts: (taskId) => request<TaskInterruptsResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/interrupts`),
    answerInterrupt: (taskId, interruptId, answerPayload) => request<AnswerInterruptResponse>(
      '/api/v1/tasks/interrupts/answer',
      { method: 'POST', body: JSON.stringify({ task_id: taskId, interrupt_id: interruptId, answer_payload: answerPayload }) },
    ),
    getTaskArtifacts: (taskId) => request<TaskArtifactsResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/artifacts`),
    downloadArtifact: async (artifactId, filename) => {
      const response = await fetcher(`${baseUrl}/api/v1/artifacts/${encodeURIComponent(artifactId)}/download`, {
        method: 'GET',
        credentials: 'same-origin',
        headers: authHeaders(),
      });
      if (!response.ok) {
        if (response.status === 401) {
          options.onUnauthorized?.();
        }
        throw await toApiError(response);
      }
      const blob = await response.blob();
      if (typeof document === 'undefined' || typeof URL.createObjectURL !== 'function') {
        return;
      }
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename || artifactId;
      link.rel = 'noreferrer';
      try {
        document.body.appendChild(link);
        link.click();
      } finally {
        link.remove();
        URL.revokeObjectURL(url);
      }
    },
    getTaskGraph: (taskId) => request<TaskGraphResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/graph`),
  };
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown = null;
  try {
    detail = await response.json();
  } catch {
    detail = await response.text().catch(() => null);
  }
  return new ApiError(response.status, detail, friendlyErrorMessage(response.status));
}

function friendlyErrorMessage(status: number): string {
  if (status === 401) {
    return '登录已失效，请重新登录。';
  }
  if (status === 409) {
    return '当前会话已有任务运行中，请等待完成或取消后再继续。';
  }
  if (status === 400) {
    return '请求参数不正确，请检查后重试。';
  }
  if (status === 404) {
    return '任务不存在或已过期，请重新提交问题。';
  }
  return '请求未完成，请稍后重试。';
}

export function normalizeBaseUrl(value: string): string {
  if (!value || value === '/') {
    return '';
  }
  return value.endsWith('/') ? value.slice(0, -1) : value;
}
