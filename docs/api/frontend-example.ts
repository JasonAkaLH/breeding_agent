// 浏览器 TypeScript 示例；无第三方依赖。BASE_URL 不包含 /api/v1。
export interface TaskEvent {
  event_id: string;
  event_type: string;
  task_id: string;
  conversation_id: string;
  node_id: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
}

export function newId(): string {
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export const terminalEvents = new Set([
  'agent.run.completed', 'agent.run.failed', 'agent.run.cancelled',
  'task.completed', 'task.failed', 'task.cancelled',
]);

// chunk 不是一条完整事件。按行拆分，保留跨 chunk 的 UTF-8、CRLF 和多行 data。
export async function* decodeEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<TaskEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let data: string[] = [];
  let finished = false;
  try {
    while (!finished) {
      const { value, done } = await reader.read();
      finished = done;
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      while (true) {
        const newline = /\r\n|\r|\n/.exec(buffer);
        if (!newline) break;
        if (!done && newline[0] === '\r' && newline.index === buffer.length - 1) break;
        const line = buffer.slice(0, newline.index);
        buffer = buffer.slice(newline.index + newline[0].length);
        if (line === '') {
          if (data.length) {
            const event: unknown = JSON.parse(data.join('\n'));
            data = [];
            if (typeof event !== 'object' || event === null) throw new Error('Invalid SSE event');
            const candidate = event as Partial<TaskEvent>;
            if (typeof candidate.event_id !== 'string' || !candidate.event_id
              || typeof candidate.task_id !== 'string' || !candidate.task_id
              || typeof candidate.event_type !== 'string' || !candidate.event_type
              || !candidate.payload || typeof candidate.payload !== 'object'
              || Array.isArray(candidate.payload)) throw new Error('Invalid SSE envelope');
            yield candidate as TaskEvent;
          }
        } else if (line.startsWith('data:')) {
          data.push(line.slice(5).replace(/^ /, ''));
        }
        // 注释心跳、id、event 等行无需参与 JSON 拼接；以 envelope 的 event_id/event_type 为准。
      }
    }
    // 未以空行结束的帧不完整，留给调用方通过任务查询/重新订阅恢复。
  } finally {
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function createApi(baseUrl: string) {
  const base = baseUrl.trim().replace(/\/+$/, '');
  let token = '';

  async function check(response: Response): Promise<void> {
    if (response.ok) return;
    const text = await response.text();
    let detail: unknown = text || response.statusText;
    try { detail = JSON.parse(text).detail ?? detail; } catch { /* 代理可能返回文本/HTML。 */ }
    if (response.status === 401) token = '';
    throw new ApiError(response.status, detail);
  }

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (token) headers.set('Authorization', `Bearer ${token}`);
    headers.set('Accept', 'application/json');
    if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    const response = await fetch(`${base}${path}`, { ...init, headers });
    await check(response);
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }

  async function login(username: string) {
    const result = await request<{ access_token: string; user: { username: string } }>(
      '/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ username }) },
    );
    token = result.access_token;
    return result.user;
  }

  async function* events(taskId: string, signal?: AbortSignal): AsyncGenerator<TaskEvent> {
    const response = await fetch(`${base}/api/v1/tasks/${encodeURIComponent(taskId)}/events`, {
      headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' }, signal,
    });
    await check(response);
    if (!response.body || !response.headers.get('Content-Type')?.includes('text/event-stream')) {
      await response.body?.cancel();
      throw new Error('Expected text/event-stream');
    }
    for await (const event of decodeEvents(response.body)) {
      if (event.task_id !== taskId) continue;
      if (event.event_type === 'auth.invalidated') {
        token = '';
        throw new ApiError(401, 'Authentication expired');
      }
      yield event;
    }
  }

  return { request, login, events, setToken(value: string) { token = value; } };
}

// 首次接入：传入测试用户名，onEvent 更新界面。需要中止订阅时传入 AbortController.signal。
// 这是普通对话示例；等待输入时由界面按文档另行提交补参/审批，不能把等待误判为完成。
export async function firstConversation(
  baseUrl: string,
  username: string,
  onEvent: (event: TaskEvent) => void,
  signal?: AbortSignal,
) {
  const api = createApi(baseUrl);
  await api.login(username);
  const conversationId = newId();
  const clientMessageId = newId(); // 同一次消息的网络重试必须复用这个 ID 和原请求体。
  const accepted = await api.request<{ task_id: string; conversation_id: string }>(
    '/api/v1/conversations/chat-messages', {
      method: 'POST', signal,
      body: JSON.stringify({ conversation_id: conversationId, client_message_id: clientMessageId,
        content: '你好，请介绍一下你能帮助我做什么。', routing_mode: 'auto', capability_id: null,
        metadata: { deep_thinking: false } }),
    },
  );
  const seen = new Set<string>(); // 对于后续重新订阅同一 task，请保留此集合。
  let terminal = false;
  let streamError: unknown;
  try {
    for await (const event of api.events(accepted.task_id, signal)) {
      if (seen.has(event.event_id)) continue;
      seen.add(event.event_id);
      onEvent(event);
      if (terminalEvents.has(event.event_type)) { terminal = true; break; }
    }
  } catch (error) {
    if (signal?.aborted || (error instanceof ApiError && [401, 404].includes(error.status))) throw error;
    streamError = error;
  }
  const task = await api.request<{ status: string }>(`/api/v1/tasks/${encodeURIComponent(accepted.task_id)}`);
  if (!terminal && !['completed', 'failed', 'cancelled'].includes(task.status)) {
    throw new Error(`事件流已断开；保存 task_id=${accepted.task_id}，按断线恢复章节重新订阅，不要重新发消息。`, { cause: streamError });
  }
  const [history, artifacts] = await Promise.all([
    api.request<{ messages: Array<{ role: string; content: string; task_id: string | null; stream_status: string | null }> }>(
      `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`),
    api.request<{ artifacts: unknown[] }>(`/api/v1/tasks/${encodeURIComponent(accepted.task_id)}/artifacts`),
  ]);
  return { conversationId, taskId: accepted.task_id, task, history, artifacts };
}
