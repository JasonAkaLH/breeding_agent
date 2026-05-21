import type {
  CancelTaskResponse,
  AnswerInterruptResponse,
  ApiTokenListResponse,
  AuthUserResponse,
  CaptchaChallengeResponse,
  CreateApiTokenResponse,
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
  ReasoningEffort,
  RevokeApiTokenResponse,
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
  accountId?: string;
  content: string;
  mode: ChatMode;
  deepThinking?: boolean;
  reasoningEffort?: ReasoningEffort;
  clientMessageId?: string;
  metadata?: Record<string, unknown>;
  capabilityId?: string | null;
}

export interface ApiClient {
  uiModes: UiModeOption[];
  createCaptcha(): Promise<CaptchaChallengeResponse>;
  login(input: { username: string; password: string; captchaId: string; captchaCode: string }): Promise<AuthUserResponse>;
  register(input: { username: string; password: string; captchaId: string; captchaCode: string }): Promise<AuthUserResponse>;
  logout(): Promise<LogoutResponse>;
  me(): Promise<AuthUserResponse>;
  createApiToken(input: { clientName: string; scopes: string[]; ttlSeconds?: number | null }): Promise<CreateApiTokenResponse>;
  listApiTokens(): Promise<ApiTokenListResponse>;
  revokeApiToken(tokenId: string): Promise<RevokeApiTokenResponse>;
  listConversationUploads(conversationId: string): Promise<UploadListResponse>;
  deleteConversationUpload(conversationId: string, uploadId: string): Promise<DeleteUploadResponse>;
  uploadConversationFile(conversationId: string, file: File): Promise<UploadFileResponse>;
  listCapabilities(): Promise<CapabilityListResponse>;
  submitMessage(input: SubmitMessageInput): Promise<MessageAcceptedResponse>;
  listConversations(): Promise<ConversationListResponse>;
  listConversationMessages(conversationId: string): Promise<ConversationMessagesResponse>;
  deleteConversation(conversationId: string): Promise<DeleteConversationResponse>;
  renameConversation(conversationId: string, title: string): Promise<ConversationSummaryResponse>;
  listConversationTasks(conversationId: string, scope?: 'unfinished' | 'all'): Promise<TaskListResponse>;
  getTask(taskId: string): Promise<TaskSummaryResponse>;
  cancelTask(taskId: string): Promise<CancelTaskResponse>;
  getTaskArtifacts(taskId: string): Promise<TaskArtifactsResponse>;
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
      throw await toApiError(response);
    }
    return (await response.json()) as T;
  }

  return {
    uiModes: UI_MODES,
    createCaptcha: () => request<CaptchaChallengeResponse>('/api/v1/auth/captcha', { method: 'POST' }),
    login: (input) => request<AuthUserResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        username: input.username,
        password: input.password,
        captcha_id: input.captchaId,
        captcha_code: input.captchaCode,
      }),
    }),
    register: (input) => request<AuthUserResponse>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify({
        username: input.username,
        password: input.password,
        captcha_id: input.captchaId,
        captcha_code: input.captchaCode,
      }),
    }),
    logout: () => request<LogoutResponse>('/api/v1/auth/logout', { method: 'POST' }),
    me: () => request<AuthUserResponse>('/api/v1/auth/me'),
    createApiToken: (input) => request<CreateApiTokenResponse>('/api/v1/auth/api-tokens', {
      method: 'POST',
      body: JSON.stringify({
        client_name: input.clientName,
        scopes: input.scopes,
        ttl_seconds: input.ttlSeconds ?? null,
      }),
    }),
    listApiTokens: () => request<ApiTokenListResponse>('/api/v1/auth/api-tokens'),
    revokeApiToken: (tokenId) => request<RevokeApiTokenResponse>('/api/v1/auth/api-tokens', {
      method: 'DELETE',
      body: JSON.stringify({ token_id: tokenId }),
    }),
    listConversationUploads: (conversationId) => request<UploadListResponse>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}/uploads`,
    ),
    deleteConversationUpload: (conversationId, uploadId) => request<DeleteUploadResponse>(
      '/api/v1/conversations/uploads',
      { method: 'DELETE', body: JSON.stringify({ conversation_id: conversationId, upload_id: uploadId }) },
    ),
    listCapabilities: () => request<CapabilityListResponse>('/api/v1/capabilities'),
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
      const body: SubmitMessageRequest = {
        conversation_id: input.conversationId,
        account_id: input.accountId ?? 'session-user',
        content: input.content,
        routing_mode: capabilityId ? 'force_capability' : 'auto',
        capability_id: capabilityId,
        client_message_id: input.clientMessageId ?? null,
        metadata: {
          ...(input.metadata ?? {}),
          deep_thinking: input.deepThinking ?? false,
          main_agent_reasoning_effort: input.reasoningEffort ?? 'medium',
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
