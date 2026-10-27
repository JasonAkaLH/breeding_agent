import type {
  CancelTaskResponse,
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
  CreateMCPServerRequest,
  MCPCallControlResponse,
  MCPDeleteServerResponse,
  MCPServerListResponse,
  MCPServerResponse,
  MCPToolGrantListResponse,
  MessageAcceptedResponse,
  ModelEdition,
  ModelEditionsResponse,
  ReasoningEffort,
  RoutingMode,
  PatchMCPServerRequest,
  SubmitMessageRequest,
  TaskArtifactsResponse,
  TaskGraphResponse,
  TaskSummaryResponse,
  UploadFileResponse,
  UploadListResponse,
} from './types';
import type { KnowledgeBatchResult, KnowledgeDocument, KnowledgeGrant, KnowledgeImport, KnowledgeLibrary, KnowledgeMetadata, KnowledgeSelection } from './knowledgeTypes';

export interface KnowledgeApi {
  listKnowledgeLibraries(): Promise<{ libraries: KnowledgeLibrary[] }>;
  createKnowledgeLibrary(input: { name: string; description: string }): Promise<KnowledgeLibrary>;
  patchKnowledgeLibrary(libraryId: string, input: { name: string; description: string; revision: number }): Promise<KnowledgeLibrary>;
  deleteKnowledgeLibrary(libraryId: string, revision?: number): Promise<KnowledgeLibrary | undefined>;
  listKnowledgeDocuments(libraryId: string): Promise<{ documents: KnowledgeDocument[] }>;
  patchKnowledgeDocument(libraryId: string, documentId: string, input: { metadata: KnowledgeMetadata; revision: number }): Promise<KnowledgeDocument>;
  deleteKnowledgeDocument(libraryId: string, documentId: string, revision?: number): Promise<KnowledgeDocument | undefined>;
  listKnowledgeImports(libraryId: string): Promise<{ imports: KnowledgeImport[] }>;
  uploadKnowledgeImports(libraryId: string, files: File[], replacementDocumentId?: string): Promise<KnowledgeBatchResult>;
  patchKnowledgeImport(libraryId: string, importId: string, input: { metadata: KnowledgeMetadata; revision: number }): Promise<KnowledgeImport>;
  confirmKnowledgeImport(libraryId: string, importId: string, revision: number): Promise<KnowledgeImport>;
  confirmKnowledgeImports(libraryId: string, imports: Array<{ import_id: string; revision: number }>): Promise<KnowledgeBatchResult>;
  retryKnowledgeImport(libraryId: string, importId: string): Promise<KnowledgeImport>;
  discardKnowledgeImport(libraryId: string, importId: string, revision?: number): Promise<void>;
  readKnowledgeContent(libraryId: string, resource: 'index' | 'documents' | 'imports', resourceId?: string): Promise<string>;
  downloadKnowledgeDocument(libraryId: string, documentId: string, filename: string): Promise<void>;
  downloadKnowledgeSource(libraryId: string, resource: 'documents' | 'imports', resourceId: string, filename: string): Promise<void>;
  listKnowledgeGrants(libraryId: string): Promise<{ grants: KnowledgeGrant[] }>;
  createKnowledgeGrant(libraryId: string, username: string): Promise<KnowledgeGrant>;
  deleteKnowledgeGrant(libraryId: string, grantId: string): Promise<void>;
  getKnowledgeSelection(conversationId: string): Promise<KnowledgeSelection>;
  saveKnowledgeSelection(conversationId: string, selection: KnowledgeSelection): Promise<KnowledgeSelection>;
}

export interface UiModeOption {
  key: ChatMode;
  label: string;
  capabilityId: string | null;
}

export interface SubmitMessageInput {
  conversationId: string;
  content: string;
  mode: ChatMode;
  routingMode: RoutingMode;
  modelEdition?: ModelEdition;
  deepThinking?: boolean;
  reasoningEffort?: ReasoningEffort;
  clientMessageId?: string;
  metadata?: Record<string, unknown>;
  capabilityId?: string | null;
}

export interface ApiClient extends KnowledgeApi {
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
  listMCPServers(): Promise<MCPServerListResponse>;
  createMCPServer(input: CreateMCPServerRequest): Promise<MCPServerResponse>;
  getMCPServer(serverId: string): Promise<MCPServerResponse>;
  patchMCPServer(serverId: string, input: PatchMCPServerRequest): Promise<MCPServerResponse>;
  testMCPServer(serverId: string): Promise<MCPServerResponse>;
  deleteMCPServer(serverId: string): Promise<MCPDeleteServerResponse | undefined>;
  listMCPGrants(): Promise<MCPToolGrantListResponse>;
  deleteMCPGrant(grantId: string): Promise<void>;
  clearMCPServerGrants(serverId: string): Promise<void>;
  continueMCPCall(taskId: string, callRef: string): Promise<MCPCallControlResponse>;
  cancelMCPCall(taskId: string, callRef: string): Promise<MCPCallControlResponse>;
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
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  const knowledgePath = (libraryId: string) => `/api/v1/knowledge/libraries/${encodeURIComponent(libraryId)}`;
  const contentPath = (libraryId: string, resource: 'index' | 'documents' | 'imports', resourceId?: string) => (
    resource === 'index' ? `${knowledgePath(libraryId)}/index` : `${knowledgePath(libraryId)}/${resource}/${encodeURIComponent(resourceId ?? '')}/content`
  );
  async function rawRequest(path: string, init?: RequestInit): Promise<Response> {
    const response = await fetcher(`${baseUrl}${path}`, { ...init, credentials: 'same-origin', headers: authHeaders() });
    if (!response.ok) {
      if (response.status === 401) options.onUnauthorized?.();
      throw await toApiError(response);
    }
    return response;
  }

  async function downloadFile(path: string, filename: string): Promise<void> {
    const blob = await (await rawRequest(path)).blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.rel = 'noreferrer';
    try { document.body.appendChild(link); link.click(); }
    finally { link.remove(); URL.revokeObjectURL(url); }
  }

  return {
    uiModes: UI_MODES,
    listKnowledgeLibraries: () => request('/api/v1/knowledge/libraries'),
    createKnowledgeLibrary: (input) => request('/api/v1/knowledge/libraries', { method: 'POST', body: JSON.stringify(input) }),
    patchKnowledgeLibrary: (id, input) => request(knowledgePath(id), { method: 'PATCH', body: JSON.stringify(input) }),
    deleteKnowledgeLibrary: (id, revision) => request(`${knowledgePath(id)}${revision === undefined ? '' : `?revision=${revision}`}`, { method: 'DELETE' }),
    listKnowledgeDocuments: (id) => request(`${knowledgePath(id)}/documents`),
    patchKnowledgeDocument: (id, doc, input) => request(`${knowledgePath(id)}/documents/${encodeURIComponent(doc)}`, { method: 'PATCH', body: JSON.stringify(input) }),
    deleteKnowledgeDocument: (id, doc, revision) => request(`${knowledgePath(id)}/documents/${encodeURIComponent(doc)}${revision === undefined ? '' : `?revision=${revision}`}`, { method: 'DELETE' }),
    listKnowledgeImports: (id) => request(`${knowledgePath(id)}/imports`),
    uploadKnowledgeImports: async (id, files, replacement) => {
      const data = new FormData();
      files.forEach((file) => data.append('files', file));
      if (replacement) data.append('replacement_document_id', replacement);
      const response = await (await rawRequest(`${knowledgePath(id)}/imports`, { method: 'POST', body: data })).json() as { imports: KnowledgeBatchResult['results'] };
      return { results: response.imports };
    },
    patchKnowledgeImport: (id, item, input) => request(`${knowledgePath(id)}/imports/${encodeURIComponent(item)}`, { method: 'PATCH', body: JSON.stringify(input) }),
    confirmKnowledgeImport: (id, item, revision) => request(`${knowledgePath(id)}/imports/${encodeURIComponent(item)}/confirm`, { method: 'POST', body: JSON.stringify({ revision }) }),
    confirmKnowledgeImports: (id, imports) => request(`${knowledgePath(id)}/imports/confirm`, { method: 'POST', body: JSON.stringify({ imports }) }),
    retryKnowledgeImport: (id, item) => request(`${knowledgePath(id)}/imports/${encodeURIComponent(item)}/retry`, { method: 'POST' }),
    discardKnowledgeImport: (id, item, revision) => request(`${knowledgePath(id)}/imports/${encodeURIComponent(item)}${revision === undefined ? '' : `?revision=${revision}`}`, { method: 'DELETE' }),
    readKnowledgeContent: async (id, resource, item) => {
      const response = await rawRequest(contentPath(id, resource, item));
      return resource === 'index' ? ((await response.json()) as { text: string }).text : response.text();
    },
    downloadKnowledgeDocument: (id, doc, filename) => downloadFile(`${contentPath(id, 'documents', doc)}?download=true`, filename),
    downloadKnowledgeSource: (id, resource, item, filename) => downloadFile(`${knowledgePath(id)}/${resource}/${encodeURIComponent(item)}/source`, filename),
    listKnowledgeGrants: (id) => request(`${knowledgePath(id)}/grants`),
    createKnowledgeGrant: (id, username) => request(`${knowledgePath(id)}/grants`, { method: 'POST', body: JSON.stringify({ subject_type: 'user', subject_id: username, role: 'reader' }) }),
    deleteKnowledgeGrant: (id, grant) => request(`${knowledgePath(id)}/grants/${encodeURIComponent(grant)}`, { method: 'DELETE' }),
    getKnowledgeSelection: (id) => request(`/api/v1/conversations/${encodeURIComponent(id)}/knowledge-selection`),
    saveKnowledgeSelection: (id, selection) => request(`/api/v1/conversations/${encodeURIComponent(id)}/knowledge-selection`, { method: 'PUT', body: JSON.stringify({ mode: selection.mode, library_ids: selection.library_ids }) }),
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
    submitMessage: async (input) => {
      const mode = UI_MODES.find((candidate) => candidate.key === input.mode);
      if (!mode) {
        throw new ApiError(0, null, '当前对话模式不可用，请刷新后重试。');
      }
      const capabilityId = input.capabilityId ?? null;
      validateSubmitRouting(input.routingMode, capabilityId, input.metadata);
      const deepThinking = input.deepThinking ?? false;
      const metadata: Record<string, unknown> = {
        ...(input.metadata ?? {}),
        deep_thinking: deepThinking,
      };
      if (input.reasoningEffort) {
        metadata.main_agent_reasoning_effort = input.reasoningEffort;
      }
      const body: SubmitMessageRequest = {
        conversation_id: input.conversationId,
        content: input.content,
        routing_mode: input.routingMode,
        capability_id: capabilityId,
        client_message_id: input.clientMessageId ?? null,
        ...(input.modelEdition ? { model_edition: input.modelEdition } : {}),
        metadata,
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
    listMCPServers: () => request<MCPServerListResponse>('/api/v1/mcp/servers'),
    createMCPServer: (input) => request<MCPServerResponse>('/api/v1/mcp/servers', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
    getMCPServer: (serverId) => request<MCPServerResponse>(`/api/v1/mcp/servers/${encodeURIComponent(serverId)}`),
    patchMCPServer: (serverId, input) => request<MCPServerResponse>(
      `/api/v1/mcp/servers/${encodeURIComponent(serverId)}`,
      { method: 'PATCH', body: JSON.stringify(input) },
    ),
    testMCPServer: (serverId) => request<MCPServerResponse>(
      `/api/v1/mcp/servers/${encodeURIComponent(serverId)}/test`,
      { method: 'POST' },
    ),
    deleteMCPServer: (serverId) => request<MCPDeleteServerResponse | undefined>(
      `/api/v1/mcp/servers/${encodeURIComponent(serverId)}`,
      { method: 'DELETE' },
    ),
    listMCPGrants: () => request<MCPToolGrantListResponse>('/api/v1/mcp/grants'),
    deleteMCPGrant: (grantId) => request<void>(`/api/v1/mcp/grants/${encodeURIComponent(grantId)}`, { method: 'DELETE' }),
    clearMCPServerGrants: (serverId) => request<void>(
      `/api/v1/mcp/servers/${encodeURIComponent(serverId)}/grants`,
      { method: 'DELETE' },
    ),
    continueMCPCall: (taskId, callRef) => request<MCPCallControlResponse>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/mcp-calls/${encodeURIComponent(callRef)}/continue`,
      { method: 'POST' },
    ),
    cancelMCPCall: (taskId, callRef) => request<MCPCallControlResponse>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/mcp-calls/${encodeURIComponent(callRef)}/cancel`,
      { method: 'POST' },
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

function validateSubmitRouting(
  routingMode: RoutingMode,
  capabilityId: string | null,
  metadata: Record<string, unknown> | undefined,
): void {
  const mcpBinding = metadata?.mcp_server_binding;
  const hasMCPBinding = mcpBinding !== undefined;
  const validMCPBinding = isRecord(mcpBinding)
    && Object.keys(mcpBinding).length === 1
    && typeof mcpBinding.server_id === 'string'
    && mcpBinding.server_id.trim().length > 0;
  const valid = routingMode === 'auto'
    ? capabilityId === null && !hasMCPBinding
    : routingMode === 'hint'
      ? Boolean(capabilityId?.startsWith('skill.')) && !hasMCPBinding
      : routingMode === 'force_capability'
        && capabilityId !== null
        && (capabilityId === 'mcp.dispatch' ? validMCPBinding : !hasMCPBinding);
  if (!valid) {
    throw new ApiError(0, null, '提交路由参数组合无效，请刷新后重试。');
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown = null;
  try {
    detail = await response.json();
  } catch {
    detail = await response.text().catch(() => null);
  }
  return new ApiError(response.status, detail, friendlyErrorMessage(response.status, detail));
}

function friendlyErrorMessage(status: number, detail: unknown): string {
  const code = apiErrorCode(detail);
  if (code === 'mcp_bound_server_unavailable') {
    return '所选 MCP Server 已不可用，请刷新列表后重新选择。';
  }
  if (code === 'mcp_feature_unavailable') {
    return 'MCP 功能暂不可用，请稍后重试。';
  }
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

function apiErrorCode(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const detail = (value as { detail?: unknown }).detail;
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return '';
  const code = (detail as { code?: unknown }).code;
  return typeof code === 'string' ? code : '';
}

export function normalizeBaseUrl(value: string): string {
  if (!value || value === '/') {
    return '';
  }
  return value.endsWith('/') ? value.slice(0, -1) : value;
}
