import type {
  CancelTaskResponse,
  CapabilityListResponse,
  ChatMode,
  MessageAcceptedResponse,
  SubmitMessageRequest,
  TaskArtifactsResponse,
  TaskGraphResponse,
  TaskSummaryResponse,
} from './types';

export interface UiModeOption {
  key: ChatMode;
  label: string;
  capabilityId: string | null;
}

export interface SubmitMessageInput {
  conversationId: string;
  accountId: string;
  content: string;
  mode: ChatMode;
  clientMessageId?: string;
  metadata?: Record<string, unknown>;
}

export interface ApiClient {
  uiModes: UiModeOption[];
  listCapabilities(): Promise<CapabilityListResponse>;
  submitMessage(input: SubmitMessageInput): Promise<MessageAcceptedResponse>;
  getTask(taskId: string): Promise<TaskSummaryResponse>;
  cancelTask(taskId: string): Promise<CancelTaskResponse>;
  getTaskArtifacts(taskId: string): Promise<TaskArtifactsResponse>;
  getTaskGraph(taskId: string): Promise<TaskGraphResponse>;
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
  { key: 'sql_query', label: '数据库查询（SQLQuery）', capabilityId: 'sql_query.query' },
];

interface CreateApiClientOptions {
  baseUrl?: string;
  fetcher?: typeof fetch;
}

export function createApiClient(options: CreateApiClientOptions = {}): ApiClient {
  const baseUrl = normalizeBaseUrl(options.baseUrl ?? import.meta.env.VITE_API_BASE_URL ?? '');
  const fetcher = options.fetcher ?? fetch.bind(globalThis);

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetcher(`${baseUrl}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
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
    listCapabilities: () => request<CapabilityListResponse>('/api/v1/capabilities'),
    submitMessage: (input) => {
      const mode = UI_MODES.find((candidate) => candidate.key === input.mode);
      if (!mode) {
        throw new ApiError(0, null, '当前对话模式不可用，请刷新后重试。');
      }
      const body: SubmitMessageRequest = {
        account_id: input.accountId,
        content: input.content,
        routing_mode: 'auto',
        capability_id: mode.capabilityId,
        client_message_id: input.clientMessageId ?? null,
        metadata: input.metadata ?? {},
      };
      return request<MessageAcceptedResponse>(`/api/v1/conversations/${encodeURIComponent(input.conversationId)}/messages`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },
    getTask: (taskId) => request<TaskSummaryResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}`),
    cancelTask: (taskId) => request<CancelTaskResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
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
  if (status === 409) {
    return '当前会话已有任务运行中，请等待完成或取消后再继续。';
  }
  if (status === 400) {
    return '当前模式不可用，请刷新能力目录后重试。';
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
