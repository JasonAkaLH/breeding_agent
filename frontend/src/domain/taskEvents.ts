import type { TaskEventEnvelope } from '../api/types';

export type TaskPhase =
  | 'idle'
  | 'submitting'
  | 'accepted'
  | 'running'
  | 'streaming'
  | 'loading_artifacts'
  | 'completed'
  | 'cancelling'
  | 'cancelled'
  | 'failed'
  | 'waiting_for_input';

export interface SkillStatusLine {
  key: string;
  nodeId: string | null;
  capabilityId: string;
  label: string;
  statusText: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked';
}

export interface CapabilityFallbackNotice {
  scope: 'full' | 'partial';
  reasonCode: 'capability_missing' | 'skill_missing' | 'forced_skill_missing' | 'mcp_missing';
  missingCapabilitySummary: string;
  attemptedCapabilitySummary?: string;
  fallbackContentScope: string;
  artifactGenerationAllowed: false;
}

export type MCPApprovalDecision = 'allow_once' | 'always_allow' | 'deny';
export type MCPCallStatus = 'running' | 'still_running' | 'completed' | 'failed' | 'cancelled' | 'unknown';

export interface MCPDiscoveryState {
  status: 'started' | 'completed' | 'failed';
  serverDisplayName: string;
  availableToolCount: number | null;
  retried: boolean;
  errorCode: string | null;
}

export interface MCPQueueState {
  queued: boolean;
  position: number | null;
  serverDisplayName: string;
}

export interface MCPApprovalState {
  interruptId: string;
  safeCallRef: string;
  serverDisplayName: string;
  toolDisplayName: string;
  decision: MCPApprovalDecision | null;
  pending: boolean;
}

export interface MCPCallState {
  safeCallRef: string;
  serverDisplayName: string;
  toolDisplayName: string;
  status: MCPCallStatus;
  elapsedSeconds: number | null;
  nextPromptAfterSeconds: number | null;
  errorCode: string | null;
}

export interface MCPInputState {
  interruptId: string;
  safeCallRef: string;
  question: string;
  fieldNames: string[];
  pending: boolean;
}

export interface MCPRemoteTaskState {
  safeTaskRef: string;
  status: string;
  serverDisplayName: string;
  toolDisplayName: string;
}

export interface MCPAvailabilityState {
  status: 'unavailable';
  reasonCode: string | null;
}

export interface MCPTaskState {
  serverDisplayName: string | null;
  discovery: MCPDiscoveryState | null;
  queue: MCPQueueState | null;
  approval: MCPApprovalState | null;
  calls: MCPCallState[];
  input: MCPInputState | null;
  remoteTask: MCPRemoteTaskState | null;
  availability: MCPAvailabilityState | null;
}

export interface TaskEventState {
  phase: TaskPhase;
  statusText: string;
  currentCapabilityId: string | null;
  currentCapabilityLabel: string | null;
  currentActivityText: string | null;
  skillStatuses: SkillStatusLine[];
  assistantText: string;
  reasoningText: string;
  memoryReasoningText: string;
  plannerReasoningText: string;
  interruptReasoningText: string;
  skillReasoningText: string;
  answerReasoningText: string;
  errorMessage: string | null;
  fallbackNotice: CapabilityFallbackNotice | null;
  mcp: MCPTaskState;
  seenEventIds: string[];
}

export function createInitialTaskEventState(): TaskEventState {
  return {
    phase: 'idle',
    statusText: '准备就绪',
    currentCapabilityId: null,
    currentCapabilityLabel: null,
    currentActivityText: null,
    skillStatuses: [],
    assistantText: '',
    reasoningText: '',
    memoryReasoningText: '',
    plannerReasoningText: '',
    interruptReasoningText: '',
    skillReasoningText: '',
    answerReasoningText: '',
    errorMessage: null,
    fallbackNotice: null,
    mcp: {
      serverDisplayName: null,
      discovery: null,
      queue: null,
      approval: null,
      calls: [],
      input: null,
      remoteTask: null,
      availability: null,
    },
    seenEventIds: [],
  };
}

export function createSubmittingTaskState(): TaskEventState {
  return {
    ...createInitialTaskEventState(),
    phase: 'submitting',
    statusText: '提交中',
  };
}

export function createRestoringTaskState(): TaskEventState {
  return {
    ...createInitialTaskEventState(),
    phase: 'running',
    statusText: '正在恢复任务状态',
    currentActivityText: '正在同步任务输出',
  };
}

export function taskProgressDisplayText(state: TaskEventState): string {
  return state.currentActivityText ?? state.statusText;
}

export function markTaskCompleted(state: TaskEventState, statusText = '任务已完成'): TaskEventState {
  return { ...state, phase: 'completed', statusText, currentCapabilityId: null, currentCapabilityLabel: null, currentActivityText: null, errorMessage: null };
}

export function markTaskFailed(state: TaskEventState, errorMessage: string): TaskEventState {
  return { ...state, phase: 'failed', statusText: '本次任务未完成', currentCapabilityId: null, currentCapabilityLabel: null, currentActivityText: null, errorMessage };
}

export function markWaitingInputRequired(state: TaskEventState): TaskEventState {
  return {
    ...state,
    phase: 'waiting_for_input',
    statusText: '任务等待补充信息',
    currentActivityText: '任务等待补充信息，请在输入框继续回答',
    errorMessage: '请直接在输入框补充回答，你的下一条回复会继续当前任务。',
  };
}

export function isTaskActive(phase: TaskPhase): boolean {
  return ['submitting', 'accepted', 'running', 'streaming', 'cancelling'].includes(phase);
}

export function applyTaskEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  if (state.seenEventIds.includes(event.event_id)) {
    return state;
  }
  const withEvent = { ...state, seenEventIds: [...state.seenEventIds, event.event_id] };

  switch (event.event_type) {
    case 'task.accepted':
      return { ...withEvent, phase: 'accepted', statusText: '任务已提交', currentCapabilityId: null, currentCapabilityLabel: null, currentActivityText: null, errorMessage: null };
    case 'task.graph_created':
      return {
        ...withEvent,
        phase: 'running',
        statusText: '正在规划/准备执行',
        currentActivityText: '正在规划并准备执行能力',
        errorMessage: null,
      };
    case 'node.started': {
      const activity = nodeActivity(event.payload);
      const skillStatuses = isSkillCapability(activity.capabilityId)
        ? upsertSkillStatusLine(withEvent.skillStatuses, {
          key: skillStatusKey(event, event.payload),
          nodeId: event.node_id,
          capabilityId: activity.capabilityId,
          label: activity.capabilityLabel,
          statusText: activity.stepText,
          status: 'running',
        })
        : withEvent.skillStatuses;
      return {
        ...withEvent,
        phase: 'running',
        statusText: activity.stepText,
        currentCapabilityId: activity.capabilityId,
        currentCapabilityLabel: activity.capabilityLabel,
        currentActivityText: `正在执行 ${activity.capabilityLabel}：${activity.stepText}`,
        skillStatuses,
        errorMessage: null,
      };
    }
    case 'node.completed': {
      const activity = nodeActivity(event.payload);
      const skillStatuses = isSkillCapability(activity.capabilityId)
        ? updateExistingSkillStatusLine(withEvent.skillStatuses, skillStatusKey(event, event.payload), {
          status: 'completed',
          statusText: '已完成',
        })
        : withEvent.skillStatuses;
      return {
        ...withEvent,
        phase: state.phase === 'streaming' ? 'streaming' : 'running',
        statusText: '正在整理执行结果',
        currentCapabilityId: activity.capabilityId,
        currentCapabilityLabel: activity.capabilityLabel,
        currentActivityText: `${activity.capabilityLabel} 已完成，正在整理执行结果`,
        skillStatuses,
        errorMessage: null,
      };
    }
    case 'node.waiting_for_input':
      return markWaitingInputRequired(withEvent);
    case 'node.ready_to_resume':
      return markNodeResumeProgress(withEvent, event, '补充信息已提交', '准备恢复执行', '已收到补充信息，准备恢复执行');
    case 'node.resuming':
      return markNodeResumeProgress(withEvent, event, '正在恢复执行', '正在恢复执行', '正在恢复执行');
    case 'main_agent.output_delta': {
      if (!isVisibleMainAgentResponse(event.payload)) return withEvent;
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      return { ...withEvent, phase: 'streaming', statusText: '正在生成答案', currentActivityText: null, assistantText: `${state.assistantText}${delta}`, errorMessage: null };
    }
    case 'main_agent.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      const answerReasoningText = `${state.answerReasoningText}${delta}`;
      return {
        ...withEvent,
        phase: 'streaming',
        statusText: '正在思考并生成答案',
        currentActivityText: null,
        answerReasoningText,
        reasoningText: composeReasoningText({
          memory: state.memoryReasoningText,
          planner: state.plannerReasoningText,
          interrupt: state.interruptReasoningText,
          skill: state.skillReasoningText,
          answer: answerReasoningText,
        }),
        errorMessage: null,
      };
    }
    case 'planner.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      const plannerReasoningText = `${state.plannerReasoningText}${delta}`;
      return {
        ...withEvent,
        phase: 'streaming',
        statusText: '正在规划并思考',
        currentActivityText: null,
        plannerReasoningText,
        reasoningText: composeReasoningText({
          memory: state.memoryReasoningText,
          planner: plannerReasoningText,
          interrupt: state.interruptReasoningText,
          skill: state.skillReasoningText,
          answer: state.answerReasoningText,
        }),
        errorMessage: null,
      };
    }
    case 'memory.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      const memoryReasoningText = `${state.memoryReasoningText}${delta}`;
      return {
        ...withEvent,
        phase: 'streaming',
        statusText: '正在整理记忆并思考',
        currentActivityText: null,
        memoryReasoningText,
        reasoningText: composeReasoningText({
          memory: memoryReasoningText,
          planner: state.plannerReasoningText,
          interrupt: state.interruptReasoningText,
          skill: state.skillReasoningText,
          answer: state.answerReasoningText,
        }),
        errorMessage: null,
      };
    }
    case 'interrupt.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      const interruptReasoningText = `${state.interruptReasoningText}${delta}`;
      return {
        ...withEvent,
        phase: 'streaming',
        statusText: '正在理解补充信息',
        currentActivityText: null,
        interruptReasoningText,
        reasoningText: composeReasoningText({
          memory: state.memoryReasoningText,
          planner: state.plannerReasoningText,
          interrupt: interruptReasoningText,
          skill: state.skillReasoningText,
          answer: state.answerReasoningText,
        }),
        errorMessage: null,
      };
    }
    case 'soft_skill.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      const skillReasoningText = `${state.skillReasoningText}${delta}`;
      return {
        ...withEvent,
        phase: 'streaming',
        statusText: '正在判断 Skill 并思考',
        currentActivityText: null,
        skillReasoningText,
        reasoningText: composeReasoningText({
          memory: state.memoryReasoningText,
          planner: state.plannerReasoningText,
          interrupt: state.interruptReasoningText,
          skill: skillReasoningText,
          answer: state.answerReasoningText,
        }),
        errorMessage: null,
      };
    }
    case 'main_agent.output_final':
      if (!isVisibleMainAgentResponse(event.payload)) return withEvent;
      return { ...withEvent, phase: state.phase === 'idle' ? 'running' : state.phase, statusText: '回答生成完成，正在收尾', currentActivityText: null, errorMessage: null };
    case 'capability.missing_fallback': {
      const fallbackNotice = parseCapabilityFallbackNotice(event.payload);
      if (!fallbackNotice) return withEvent;
      return {
        ...withEvent,
        fallbackNotice,
        statusText: '已切换为能力缺口 LLM 回答',
        currentActivityText: state.assistantText ? null : '当前没有匹配能力，正在生成带披露的 LLM 回答',
        errorMessage: null,
      };
    }
    case 'mcp.server_routed': {
      const serverDisplayName = safeDisplayName(event.payload, 'server_display_name');
      return {
        ...withEvent,
        phase: 'running',
        statusText: serverDisplayName ? `正在连接 ${serverDisplayName}` : '正在选择 MCP 服务',
        currentActivityText: serverDisplayName ? `已选择 MCP 服务：${serverDisplayName}` : '已选择 MCP 服务',
        mcp: { ...withEvent.mcp, serverDisplayName: serverDisplayName || null },
        errorMessage: null,
      };
    }
    case 'mcp.discovery_started':
    case 'mcp.discovery_completed':
    case 'mcp.discovery_failed':
      return applyMCPDiscoveryEvent(withEvent, event);
    case 'mcp.queue_entered':
    case 'mcp.queue_left':
      return applyMCPQueueEvent(withEvent, event);
    case 'mcp.tool_approval_required':
    case 'mcp.tool_approval_decided':
      return applyMCPApprovalEvent(withEvent, event);
    case 'mcp.tool_call_started':
    case 'mcp.tool_call_still_running':
    case 'mcp.tool_call_completed':
    case 'mcp.tool_call_failed':
    case 'mcp.tool_call_cancelled':
    case 'mcp.execution_status_unknown':
      return applyMCPCallEvent(withEvent, event);
    case 'mcp.input_required':
    case 'mcp.input_submitted':
      return applyMCPInputEvent(withEvent, event);
    case 'mcp.remote_task_status_changed':
      return applyMCPRemoteTaskEvent(withEvent, event);
    case 'mcp.runtime_unavailable':
      return {
        ...withEvent,
        statusText: '当前任务的 MCP 暂不可用',
        mcp: {
          ...withEvent.mcp,
          availability: {
            status: 'unavailable',
            reasonCode: safeCode(event.payload.reason_code),
          },
        },
        errorMessage: '当前灰度或回滚配置未为该任务分配 MCP 执行路径；已有任务不会改道或重放。',
      };
    case 'task.completed':
      return {
        ...withEvent,
        phase: 'loading_artifacts',
        statusText: '任务完成，正在整理结果',
        currentActivityText: null,
        skillStatuses: markRunningSkillStatusesCompleted(withEvent.skillStatuses),
        errorMessage: null,
      };
    case 'task.cancellation_requested':
      return { ...withEvent, phase: 'cancelling', statusText: '取消请求已发送', currentActivityText: '正在取消当前任务', errorMessage: null };
    case 'task.cancelled':
      return { ...withEvent, phase: 'cancelled', statusText: '任务已取消', currentCapabilityId: null, currentCapabilityLabel: null, currentActivityText: null, errorMessage: null };
    case 'node.cancelled':
      return markNodeInterruptedLine(withEvent, event, 'cancelled', '已取消');
    case 'node.blocked_by_cancellation':
      return markNodeInterruptedLine(withEvent, event, 'blocked', '已被取消阻断');
    case 'node.orphaned':
      return markNodeInterruptedLine(withEvent, event, 'blocked', '已被重规划跳过');
    case 'node.failed':
      return {
        ...withEvent,
        phase: state.phase,
        statusText: state.statusText,
        currentActivityText: null,
        skillStatuses: markSkillStatusFailed(withEvent.skillStatuses, event),
        errorMessage: failureMessage(event.payload, event.node_id),
      };
    case 'task.failed':
      return {
        ...withEvent,
        phase: 'failed',
        statusText: '本次任务未完成',
        currentCapabilityId: null,
        currentCapabilityLabel: null,
        currentActivityText: null,
        errorMessage: state.errorMessage ?? failureMessage(event.payload, event.node_id),
      };
    case 'skill.progress': {
      const progress = skillProgressActivity(event.payload);
      if (!progress) return withEvent;
      const skillStatuses = upsertSkillStatusLine(withEvent.skillStatuses, {
        key: skillStatusKey(event, event.payload),
        nodeId: event.node_id,
        capabilityId: progress.capabilityId,
        label: progress.capabilityLabel,
        statusText: progress.stepText,
        status: 'running',
      });
      return {
        ...withEvent,
        phase: 'running',
        statusText: progress.stepText,
        currentCapabilityId: progress.capabilityId,
        currentCapabilityLabel: progress.capabilityLabel,
        currentActivityText: `正在执行 ${progress.capabilityLabel}：${progress.stepText}`,
        skillStatuses,
        errorMessage: null,
      };
    }
    default:
      return state;
  }
}

function applyMCPDiscoveryEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const status = event.event_type === 'mcp.discovery_started'
    ? 'started'
    : event.event_type === 'mcp.discovery_completed' ? 'completed' : 'failed';
  const serverDisplayName = safeDisplayName(event.payload, 'server_display_name') || state.mcp.serverDisplayName || '';
  const discovery: MCPDiscoveryState = {
    status,
    serverDisplayName,
    availableToolCount: safeNonNegativeInteger(event.payload.available_tool_count ?? event.payload.tool_count),
    retried: event.payload.retried === true || event.payload.will_retry === true,
    errorCode: safeCode(event.payload.error_code ?? event.payload.code),
  };
  const statusText = status === 'started'
    ? '正在发现 MCP 工具'
    : status === 'completed' ? 'MCP 工具发现完成' : 'MCP 工具发现失败';
  return {
    ...state,
    phase: 'running',
    statusText,
    currentActivityText: serverDisplayName ? `${serverDisplayName}：${statusText}` : statusText,
    mcp: { ...state.mcp, serverDisplayName: serverDisplayName || null, discovery },
    errorMessage: status === 'failed' && !discovery.retried ? '当前 MCP 服务不可用，正在寻找其他方案。' : null,
  };
}

function applyMCPQueueEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const queued = event.event_type === 'mcp.queue_entered';
  const serverDisplayName = safeDisplayName(event.payload, 'server_display_name') || state.mcp.serverDisplayName || '';
  const queue: MCPQueueState = {
    queued,
    position: queued ? safeNonNegativeInteger(event.payload.position ?? event.payload.queue_position) : null,
    serverDisplayName,
  };
  return {
    ...state,
    phase: 'running',
    statusText: queued ? '正在等待 MCP 执行资源' : '已获得 MCP 执行资源',
    currentActivityText: queued && queue.position !== null ? `MCP 排队位置：${queue.position}` : null,
    mcp: { ...state.mcp, queue },
    errorMessage: null,
  };
}

function applyMCPApprovalEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const previous = state.mcp.approval;
  const safeCallRef = safeReference(event.payload.safe_call_ref ?? event.payload.call_ref) || previous?.safeCallRef || '';
  const decision = safeApprovalDecision(event.payload.decision);
  const approval: MCPApprovalState = {
    interruptId: safeReference(event.payload.interrupt_id) || previous?.interruptId || '',
    safeCallRef,
    serverDisplayName: safeDisplayName(event.payload, 'server_display_name') || previous?.serverDisplayName || '',
    toolDisplayName: safeDisplayName(event.payload, 'tool_display_name') || safeDisplayName(event.payload, 'tool_name') || previous?.toolDisplayName || '',
    decision: event.event_type === 'mcp.tool_approval_decided' ? decision : null,
    pending: event.event_type === 'mcp.tool_approval_required',
  };
  return {
    ...state,
    phase: approval.pending ? 'waiting_for_input' : 'running',
    statusText: approval.pending ? '等待 MCP 工具授权' : 'MCP 工具授权已处理',
    currentActivityText: approval.pending ? `请确认是否允许调用 ${approval.toolDisplayName || '该工具'}` : null,
    mcp: { ...state.mcp, approval },
    errorMessage: null,
  };
}

function applyMCPCallEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const safeCallRef = safeReference(event.payload.safe_call_ref ?? event.payload.call_ref);
  if (!safeCallRef) return state;
  const previous = state.mcp.calls.find((call) => call.safeCallRef === safeCallRef);
  const status = mcpCallStatus(event.event_type);
  const call: MCPCallState = {
    safeCallRef,
    serverDisplayName: safeDisplayName(event.payload, 'server_display_name') || previous?.serverDisplayName || '',
    toolDisplayName: safeDisplayName(event.payload, 'tool_display_name') || safeDisplayName(event.payload, 'tool_name') || previous?.toolDisplayName || '',
    status,
    elapsedSeconds: safeNonNegativeInteger(event.payload.elapsed_seconds) ?? previous?.elapsedSeconds ?? null,
    nextPromptAfterSeconds: safeNonNegativeInteger(event.payload.next_prompt_after_seconds) ?? previous?.nextPromptAfterSeconds ?? null,
    errorCode: safeCode(event.payload.error_code ?? event.payload.code) ?? previous?.errorCode ?? null,
  };
  const calls = upsertMCPCall(state.mcp.calls, call);
  const labels: Record<MCPCallStatus, string> = {
    running: '正在调用 MCP 工具',
    still_running: 'MCP 工具仍在运行',
    completed: 'MCP 工具调用完成',
    failed: 'MCP 工具调用失败',
    cancelled: 'MCP 工具调用已取消',
    unknown: 'MCP 工具执行结果无法确认',
  };
  return {
    ...state,
    phase: status === 'running' || status === 'still_running' ? 'running' : state.phase,
    statusText: labels[status],
    currentActivityText: call.toolDisplayName ? `${call.toolDisplayName}：${labels[status]}` : labels[status],
    mcp: { ...state.mcp, calls },
    errorMessage: status === 'unknown'
      ? '服务重启后无法确认该工具是否完成；系统不会自动重复调用。'
      : status === 'failed' ? 'MCP 工具调用失败，正在寻找其他方案。' : null,
  };
}

function applyMCPInputEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const previous = state.mcp.input;
  const pending = event.event_type === 'mcp.input_required';
  const fieldNames = Array.isArray(event.payload.field_names)
    ? event.payload.field_names.filter((value): value is string => typeof value === 'string').slice(0, 50)
    : previous?.fieldNames ?? [];
  const input: MCPInputState = {
    interruptId: safeReference(event.payload.interrupt_id) || previous?.interruptId || '',
    safeCallRef: safeReference(event.payload.safe_call_ref ?? event.payload.call_ref) || previous?.safeCallRef || '',
    question: safeText(event.payload.question, 2000) || previous?.question || '',
    fieldNames,
    pending,
  };
  return {
    ...state,
    phase: pending ? 'waiting_for_input' : 'running',
    statusText: pending ? 'MCP 工具等待补充信息' : 'MCP 补充信息已提交',
    currentActivityText: pending ? input.question || '请补充工具执行所需信息' : '已收到补充信息，准备继续执行',
    mcp: { ...state.mcp, input },
    errorMessage: null,
  };
}

function applyMCPRemoteTaskEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const safeTaskRef = safeReference(event.payload.safe_task_ref ?? event.payload.remote_task_ref);
  if (!safeTaskRef) return state;
  const remoteTask: MCPRemoteTaskState = {
    safeTaskRef,
    status: safeCode(event.payload.status) ?? 'unknown',
    serverDisplayName: safeDisplayName(event.payload, 'server_display_name'),
    toolDisplayName: safeDisplayName(event.payload, 'tool_display_name') || safeDisplayName(event.payload, 'tool_name'),
  };
  return {
    ...state,
    phase: 'running',
    statusText: `远程 MCP 任务：${remoteTask.status}`,
    currentActivityText: remoteTask.toolDisplayName ? `${remoteTask.toolDisplayName}：${remoteTask.status}` : null,
    mcp: { ...state.mcp, remoteTask },
    errorMessage: null,
  };
}

function upsertMCPCall(calls: MCPCallState[], next: MCPCallState): MCPCallState[] {
  const index = calls.findIndex((call) => call.safeCallRef === next.safeCallRef);
  if (index < 0) return [...calls, next];
  return calls.map((call, candidateIndex) => candidateIndex === index ? next : call);
}

function mcpCallStatus(eventType: string): MCPCallStatus {
  if (eventType === 'mcp.tool_call_started') return 'running';
  if (eventType === 'mcp.tool_call_still_running') return 'still_running';
  if (eventType === 'mcp.tool_call_completed') return 'completed';
  if (eventType === 'mcp.tool_call_failed') return 'failed';
  if (eventType === 'mcp.tool_call_cancelled') return 'cancelled';
  return 'unknown';
}

function safeDisplayName(payload: Record<string, unknown>, key: string): string {
  return safeText(payload[key], 200);
}

function safeText(value: unknown, maxLength: number): string {
  return typeof value === 'string' ? value.trim().slice(0, maxLength) : '';
}

function safeReference(value: unknown): string {
  const reference = safeText(value, 256);
  return /^[A-Za-z0-9._:-]+$/.test(reference) ? reference : '';
}

function safeCode(value: unknown): string | null {
  const code = safeText(value, 100);
  return code && /^[A-Za-z0-9._:-]+$/.test(code) ? code : null;
}

function safeNonNegativeInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function safeApprovalDecision(value: unknown): MCPApprovalDecision | null {
  return value === 'allow_once' || value === 'always_allow' || value === 'deny' ? value : null;
}

function composeReasoningText(parts: {
  memory: string;
  planner: string;
  interrupt: string;
  skill: string;
  answer: string;
}): string {
  if (!parts.memory && !parts.planner && !parts.interrupt && !parts.skill) {
    return parts.answer;
  }
  return [
    ['记忆思考', parts.memory],
    ['规划思考', parts.planner],
    ['补参思考', parts.interrupt],
    ['Skill思考', parts.skill],
    ['回答思考', parts.answer],
  ]
    .filter(([, text]) => text)
    .map(([label, text]) => `### ${label}\n${text}`)
    .join('\n\n');
}

function markNodeResumeProgress(
  state: TaskEventState,
  event: TaskEventEnvelope,
  statusText: string,
  rowStatusText: string,
  activitySuffix: string,
): TaskEventState {
  const activity = nodeActivity(event.payload);
  const skillStatuses = isSkillCapability(activity.capabilityId)
    ? upsertSkillStatusLine(state.skillStatuses, {
      key: skillStatusKey(event, event.payload),
      nodeId: event.node_id,
      capabilityId: activity.capabilityId,
      label: activity.capabilityLabel,
      statusText: rowStatusText,
      status: 'running',
    })
    : state.skillStatuses;
  return {
    ...state,
    phase: 'running',
    statusText,
    currentCapabilityId: activity.capabilityId,
    currentCapabilityLabel: activity.capabilityLabel,
    currentActivityText: `${activity.capabilityLabel} ${activitySuffix}`,
    skillStatuses,
    errorMessage: null,
  };
}

function markNodeInterruptedLine(
  state: TaskEventState,
  event: TaskEventEnvelope,
  status: Extract<SkillStatusLine['status'], 'cancelled' | 'blocked'>,
  statusText: string,
): TaskEventState {
  const activity = nodeActivity(event.payload);
  const key = skillStatusKey(event, event.payload);
  const updated = updateExistingSkillStatusLine(state.skillStatuses, key, { status, statusText });
  if (updated !== state.skillStatuses) {
    return { ...state, currentActivityText: null, skillStatuses: updated };
  }
  if (!isSkillCapability(activity.capabilityId)) {
    return { ...state, currentActivityText: null };
  }
  const skillStatuses = upsertSkillStatusLine(state.skillStatuses, {
    key,
    nodeId: event.node_id,
    capabilityId: activity.capabilityId,
    label: activity.capabilityLabel,
    statusText,
    status,
  });
  return { ...state, currentActivityText: null, skillStatuses };
}

function isSkillCapability(capabilityId: unknown): capabilityId is string {
  return typeof capabilityId === 'string' && capabilityId.startsWith('skill.');
}

function isVisibleMainAgentResponse(payload: Record<string, unknown>): boolean {
  const responseRole = typeof payload.response_role === 'string' ? payload.response_role : null;
  return responseRole === null || responseRole === 'final';
}

function skillStatusKey(event: TaskEventEnvelope, payload: Record<string, unknown>): string {
  const payloadNodeId = typeof payload.node_id === 'string' && payload.node_id.trim() ? payload.node_id.trim() : '';
  if (event.node_id) return event.node_id;
  if (payloadNodeId) return payloadNodeId;
  const capabilityId = typeof payload.capability_id === 'string' ? payload.capability_id : 'skill';
  const skillName = typeof payload.skill_name === 'string' && payload.skill_name.trim() ? payload.skill_name.trim() : capabilityId;
  return `${capabilityId}::${skillName}`;
}

function upsertSkillStatusLine(statuses: SkillStatusLine[], next: SkillStatusLine): SkillStatusLine[] {
  const index = statuses.findIndex((status) => status.key === next.key);
  if (index < 0) return [...statuses, next];
  const current = statuses[index];
  if (skillStatusLineEquals(current, next)) return statuses;
  return statuses.map((status, currentIndex) => (currentIndex === index ? next : status));
}

function updateExistingSkillStatusLine(
  statuses: SkillStatusLine[],
  key: string,
  patch: Partial<Pick<SkillStatusLine, 'status' | 'statusText'>>,
): SkillStatusLine[] {
  const index = statuses.findIndex((status) => status.key === key);
  if (index < 0) return statuses;
  const next = { ...statuses[index], ...patch };
  if (skillStatusLineEquals(statuses[index], next)) return statuses;
  return statuses.map((status, currentIndex) => (currentIndex === index ? next : status));
}

function markRunningSkillStatusesCompleted(statuses: SkillStatusLine[]): SkillStatusLine[] {
  let changed = false;
  const next = statuses.map((status) => {
    if (status.status !== 'running' && status.status !== 'pending') return status;
    changed = true;
    return { ...status, status: 'completed' as const, statusText: '已完成' };
  });
  return changed ? next : statuses;
}

function markSkillStatusFailed(statuses: SkillStatusLine[], event: TaskEventEnvelope): SkillStatusLine[] {
  const key = skillStatusKey(event, event.payload);
  const updated = updateExistingSkillStatusLine(statuses, key, { status: 'failed', statusText: '失败' });
  if (updated !== statuses) return updated;

  const activity = nodeActivity(event.payload);
  if (!isSkillCapability(activity.capabilityId)) return statuses;
  return upsertSkillStatusLine(statuses, {
    key,
    nodeId: event.node_id,
    capabilityId: activity.capabilityId,
    label: activity.capabilityLabel,
    statusText: '失败',
    status: 'failed',
  });
}

function skillStatusLineEquals(left: SkillStatusLine, right: SkillStatusLine): boolean {
  return left.key === right.key
    && left.nodeId === right.nodeId
    && left.capabilityId === right.capabilityId
    && left.label === right.label
    && left.statusText === right.statusText
    && left.status === right.status;
}

function nodeActivity(payload: Record<string, unknown>): { capabilityId: string; capabilityLabel: string; stepText: string } {
  const capabilityId = typeof payload.capability_id === 'string' ? payload.capability_id : '';
  return {
    capabilityId,
    capabilityLabel: capabilityLabel(capabilityId, payload),
    stepText: nodeStatusText(capabilityId),
  };
}

function capabilityLabel(capabilityId: string, payload: Record<string, unknown> = {}): string {
  const skillName = typeof payload.skill_name === 'string' ? payload.skill_name.trim() : '';
  if (skillName) return skillName;
  if (capabilityId.startsWith('main_agent.')) return 'SeedPilot';
  if (capabilityId.startsWith('skill.')) return capabilityId;
  return capabilityId || '能力';
}

function skillProgressActivity(payload: Record<string, unknown>): { capabilityId: string; capabilityLabel: string; stepText: string } | null {
  const capabilityId = typeof payload.capability_id === 'string' ? payload.capability_id : 'skill';
  const stage = typeof payload.stage === 'string' ? payload.stage : '';
  const label = typeof payload.label === 'string' && payload.label.trim() ? payload.label.trim() : dataQueryStageText(stage);
  return { capabilityId, capabilityLabel: capabilityLabel(capabilityId, payload), stepText: label };
}

function dataQueryStageText(stage: string): string {
  if (stage === 'understand_query') return '正在理解查询意图';
  if (stage === 'prepare_query') return '正在准备数据查询';
  if (stage === 'check_safety') return '正在检查查询安全边界';
  if (stage === 'execute_query') return '正在检索数据';
  if (stage === 'filter_results') return '正在筛选查询结果';
  return '正在处理数据查询';
}

function nodeStatusText(capabilityId: string): string {
  if (capabilityId === 'main_agent.respond') return '正在生成回答';
  return '正在处理';
}

const QUERY_GUARD_BLOCK_CODES = new Set([
  'empty_sql',
  'multiple_statements',
  'statement_root_denied',
  'write_pattern_detected',
  'system_schema_access_denied',
  'table_not_in_route_whitelist',
  'limit_missing',
  'query_shape_exceeded',
]);

function failureMessage(payload: Record<string, unknown>, nodeId: string | null): string {
  const code = typeof payload.code === 'string' ? payload.code : '';
  if (QUERY_GUARD_BLOCK_CODES.has(code)) {
    return '当前查询不符合只读查询安全边界，请改用查询类问题。';
  }
  if (isSqlQueryFailure(payload, nodeId)) {
    return '服务器内部错误，请稍后重试。';
  }
  if (code === 'guard_token_missing') return '查询安全校验未通过，请调整问题后重试。';
  if (code === 'db_transient_error') return '数据库暂时不可用，请稍后重试。';
  if (code === 'required_skill_missing') return '当前没有可用 Skill 执行该请求，请先启用或注册对应 Skill。';
  if (code === 'data_access_deadline_exceeded') return '数据库查询超时，请稍后重试或缩小查询范围。';
  if (code === 'data_access_result_too_large' || code === 'data_access_column_limit_exceeded') return '查询结果内容过大，请缩小查询范围后重试。';
  return '本次任务未完成，请调整问题后重试。';
}

function isSqlQueryFailure(payload: Record<string, unknown>, nodeId: string | null): boolean {
  const capabilityId = typeof payload.capability_id === 'string' ? payload.capability_id : '';
  const skillName = typeof payload.skill_name === 'string' ? payload.skill_name : '';
  const domainKind = typeof payload.domain_kind === 'string' ? payload.domain_kind : '';
  const sqlCapabilityId = ['skill', 'sql_query'].join('.');
  const sqlSkillName = ['sql', 'query'].join('-');
  return capabilityId === sqlCapabilityId
    || skillName === sqlSkillName
    || domainKind === 'sql_query'
    || Boolean(nodeId?.includes(sqlCapabilityId));
}


export function parseCapabilityFallbackNotice(input: unknown): CapabilityFallbackNotice | null {
  const root = isRecord(input) && isRecord(input.capability_missing_fallback)
    ? input.capability_missing_fallback
    : input;
  if (!isRecord(root) || root.enabled !== true) return null;
  const scope = root.scope === 'partial' ? 'partial' : root.scope === 'full' ? 'full' : null;
  const reasonCode = isReasonCode(root.reason_code) ? root.reason_code : null;
  const missingCapabilitySummary = typeof root.missing_capability_summary === 'string'
    ? root.missing_capability_summary.trim()
    : '';
  const fallbackContentScope = typeof root.fallback_content_scope === 'string'
    ? root.fallback_content_scope.trim()
    : '';
  if (!scope || !reasonCode || !missingCapabilitySummary || !fallbackContentScope) return null;
  if (root.artifact_generation_allowed !== false || root.disclosure_required !== true) return null;
  const attemptedCapabilitySummary = typeof root.attempted_capability_summary === 'string'
    ? root.attempted_capability_summary.trim()
    : '';
  return {
    scope,
    reasonCode,
    missingCapabilitySummary,
    ...(attemptedCapabilitySummary ? { attemptedCapabilitySummary } : {}),
    fallbackContentScope,
    artifactGenerationAllowed: false,
  };
}

function isReasonCode(value: unknown): value is CapabilityFallbackNotice['reasonCode'] {
  return value === 'capability_missing'
    || value === 'skill_missing'
    || value === 'forced_skill_missing'
    || value === 'mcp_missing';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
