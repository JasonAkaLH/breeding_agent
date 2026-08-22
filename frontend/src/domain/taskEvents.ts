import type { MCPResultArtifactProjection, TaskEventEnvelope } from '../api/types';

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

export interface AgentWaitingState {
  interruptId: string;
  nodeId: string | null;
  reasonKind: 'mcp_approval' | 'mcp_elicitation' | 'mcp_remote_task' | 'skill_input';
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
  reasonCode: 'no_user_scoped_server';
}

export interface MCPExecutionUnknownState {
  projectionId: string;
  intentId: string;
  callId: string;
  reasonCode: 'trusted_terminal_result_absent';
  noReplay: true;
  nodeId: string;
  projectionRevision: 0;
  intentRevision: number;
  unknownEventId: string;
  taskFailedEventId: string;
  unknownTerminalAt: string;
  createdAt: string;
}

export interface MCPExecutionResolutionState {
  resolutionEventId: string;
  resultReceiptId: string;
  fromIntentRevision: number;
  toIntentRevision: number;
  resolvedAt: string;
  createdAt: string;
}

export interface MCPLateResultState {
  projectionId: string;
  resultReceiptId: string;
  resultPayloadSha256: string;
  terminalState: 'completed' | 'failed' | 'cancelled';
  safeResultRef: string | null;
  safeResultRefSha256: string | null;
  safeErrorCode: string | null;
  resolvedAt: string;
  taskRemainsFailed: true;
  nodeRemainsFailed: true;
  noReplay: true;
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
  executionUnknown: MCPExecutionUnknownState | null;
  executionResolution: MCPExecutionResolutionState | null;
  lateResult: MCPLateResultState | null;
  resultArtifactProjections: MCPResultArtifactProjection[];
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
  agentWaiting: AgentWaitingState[];
  agentRemainingWaitCount: number;
  mcp: MCPTaskState;
  seenEventIds: string[];
  eventFingerprints: Record<string, string>;
  pendingEvents: TaskEventEnvelope[];
  eventSyncError: string | null;
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
    agentWaiting: [],
    agentRemainingWaitCount: 0,
    mcp: {
      serverDisplayName: null,
      discovery: null,
      queue: null,
      approval: null,
      calls: [],
      input: null,
      remoteTask: null,
      availability: null,
      executionUnknown: null,
      executionResolution: null,
      lateResult: null,
      resultArtifactProjections: [],
    },
    seenEventIds: [],
    eventFingerprints: {},
    pendingEvents: [],
    eventSyncError: null,
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

export function prepareTaskEventResync(state: TaskEventState): TaskEventState {
  return {
    ...state,
    mcp: {
      ...state.mcp,
      executionUnknown: null,
      executionResolution: null,
      lateResult: null,
    },
    seenEventIds: [],
    eventFingerprints: {},
    pendingEvents: [],
    eventSyncError: null,
    agentWaiting: [],
    agentRemainingWaitCount: 0,
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
  if (!isClosedCP7Event(event)) {
    return isCP7ContractEvent(event)
      ? { ...state, eventSyncError: '任务事件格式不完整，请重新同步任务历史。' }
      : state;
  }
  if (!isCP7ContractEvent(event) && !isKnownTaskEventType(event.event_type)) {
    return state;
  }
  const fingerprint = taskEventFingerprint(event);
  const previousFingerprint = state.eventFingerprints[event.event_id];
  if (previousFingerprint) {
    return previousFingerprint === fingerprint
      ? state
      : { ...state, eventSyncError: '任务事件内容发生冲突，请重新同步任务历史。' };
  }
  const withFingerprint = {
    ...state,
    eventFingerprints: { ...state.eventFingerprints, [event.event_id]: fingerprint },
  };
  const predecessorId = predecessorEventId(event);
  if (predecessorId && !state.seenEventIds.includes(predecessorId)) {
    return { ...withFingerprint, pendingEvents: [...state.pendingEvents, event] };
  }
  if (!hasValidConsumedChainBinding(withFingerprint, event)) {
    return { ...withFingerprint, eventSyncError: '任务事件链绑定不一致，请重新同步任务历史。' };
  }
  return drainPendingTaskEvents(reduceTaskEvent(withFingerprint, event));
}

export function replayTaskEvents(state: TaskEventState, events: TaskEventEnvelope[]): TaskEventState {
  const replayed = [...events]
    .sort(compareTaskEvents)
    .reduce((current, event) => applyTaskEvent(current, event), state);
  return replayed.pendingEvents.length === 0
    ? replayed
    : { ...replayed, eventSyncError: '任务事件历史缺少前序记录，请稍后重新同步。' };
}

function reduceTaskEvent(state: TaskEventState, event: TaskEventEnvelope): TaskEventState {
  const withEvent = {
    ...state,
    seenEventIds: [...state.seenEventIds, event.event_id],
    pendingEvents: state.pendingEvents.filter((pending) => pending.event_id !== event.event_id),
  };

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
    case 'agent.run.waiting': {
      const interruptId = event.payload.interrupt_id as string;
      const nodeId = event.node_id;
      const waiting = withEvent.agentWaiting.filter((item) => (
        item.interruptId !== interruptId
        && (nodeId === null || item.nodeId !== nodeId)
      ));
      waiting.push({
        interruptId,
        nodeId,
        reasonKind: event.payload.reason_kind as AgentWaitingState['reasonKind'],
      });
      return {
        ...withEvent,
        phase: 'waiting_for_input',
        statusText: '任务等待补充信息',
        currentActivityText: '任务仍有待处理的补充信息或授权',
        errorMessage: '请处理当前请求；完成后若仍有待处理项，系统会继续显示。',
        agentWaiting: waiting,
        agentRemainingWaitCount: event.payload.remaining_count as number,
      };
    }
    case 'agent.run.resumed': {
      const remainingCount = event.payload.remaining_count as number;
      const waiting = remainingCount === 0
        ? []
        : event.node_id === null
          ? withEvent.agentWaiting
          : withEvent.agentWaiting.filter((item) => item.nodeId !== event.node_id);
      return {
        ...withEvent,
        phase: remainingCount > 0 ? 'waiting_for_input' : 'running',
        statusText: remainingCount > 0 ? '任务仍有待处理请求' : '正在继续任务',
        currentActivityText: remainingCount > 0 ? '请继续处理剩余请求' : '已恢复执行',
        errorMessage: remainingCount > 0 ? '当前请求处理完成后，仍需处理剩余请求。' : null,
        agentWaiting: waiting,
        agentRemainingWaitCount: remainingCount,
      };
    }
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
    case 'agent.reasoning_delta': {
      const delta = event.payload.delta as string;
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
      return applyMCPCallEvent(withEvent, event);
    case 'mcp.execution_status_unknown':
      return {
        ...withEvent,
        statusText: 'MCP 工具执行结果无法确认',
        currentActivityText: null,
        mcp: {
          ...withEvent.mcp,
          executionUnknown: {
            projectionId: event.payload.projection_id as string,
            intentId: event.payload.intent_id as string,
            callId: event.payload.call_id as string,
            reasonCode: 'trusted_terminal_result_absent',
            noReplay: true,
            nodeId: event.payload.node_id as string,
            projectionRevision: 0,
            intentRevision: event.payload.intent_revision as number,
            unknownEventId: event.event_id,
            taskFailedEventId: expectedUnknownTaskFailedEventId(event.payload.call_id as string, event.payload.intent_revision as number),
            unknownTerminalAt: event.payload.unknown_terminal_at as string,
            createdAt: event.created_at as string,
          },
        },
        errorMessage: '服务重启后无法确认该工具是否完成；系统不会自动重复调用。',
      };
    case 'mcp.execution_status_resolution':
      return {
        ...withEvent,
        mcp: {
          ...withEvent.mcp,
          executionResolution: {
            resolutionEventId: event.event_id,
            resultReceiptId: event.payload.result_receipt_id as string,
            fromIntentRevision: event.payload.from_intent_revision as number,
            toIntentRevision: event.payload.to_intent_revision as number,
            resolvedAt: event.payload.resolved_at as string,
            createdAt: event.created_at as string,
          },
        },
      };
    case 'mcp.late_terminal_result_recovered':
      return {
        ...withEvent,
        phase: 'failed',
        statusText: '任务仍未完成，但已恢复可信迟到结果',
        currentActivityText: null,
        mcp: {
          ...withEvent.mcp,
          lateResult: {
            projectionId: event.payload.projection_id as string,
            resultReceiptId: event.payload.result_receipt_id as string,
            resultPayloadSha256: event.payload.result_payload_sha256 as string,
            terminalState: event.payload.terminal_state as MCPLateResultState['terminalState'],
            safeResultRef: event.payload.safe_result_ref as string | null,
            safeResultRefSha256: event.payload.safe_result_ref_sha256 as string | null,
            safeErrorCode: event.payload.safe_error_code as string | null,
            resolvedAt: event.payload.resolved_at as string,
            taskRemainsFailed: true,
            nodeRemainsFailed: true,
            noReplay: true,
          },
        },
        errorMessage: '任务仍因未知执行状态失败，但已恢复可信迟到结果；不会恢复任务或继续调度。',
      };
    case 'mcp.input_required':
    case 'mcp.input_submitted':
      return applyMCPInputEvent(withEvent, event);
    case 'mcp.remote_task_status_changed':
      return applyMCPRemoteTaskEvent(withEvent, event);
    case 'mcp.result_artifact_projection': {
      const projections = foldMCPResultArtifactProjections([
        ...withEvent.mcp.resultArtifactProjections,
        event.payload,
      ]);
      if (projections === null) {
        return {
          ...withEvent,
          mcp: { ...withEvent.mcp, resultArtifactProjections: [] },
          eventSyncError: 'MCP 完整结果文件状态存在冲突，请重新同步任务历史。',
        };
      }
      return {
        ...withEvent,
        mcp: { ...withEvent.mcp, resultArtifactProjections: projections },
      };
    }
    case 'mcp.runtime_unavailable':
      return {
        ...withEvent,
        statusText: '当前任务的 MCP 暂不可用',
        mcp: {
          ...withEvent.mcp,
          availability: {
            status: 'unavailable',
            reasonCode: 'no_user_scoped_server',
          },
        },
        errorMessage: '当前用户没有可用的 MCP Server；该任务不会改道或重放。',
      };
    case 'task.completed':
      return {
        ...withEvent,
        phase: 'loading_artifacts',
        statusText: '任务完成，正在整理结果',
        currentActivityText: null,
        skillStatuses: markRunningSkillStatusesCompleted(withEvent.skillStatuses),
        agentWaiting: [],
        agentRemainingWaitCount: 0,
        errorMessage: null,
      };
    case 'agent.run.completed':
      return {
        ...withEvent,
        statusText: 'Agent 执行完成，正在同步任务结果',
        currentActivityText: null,
        agentWaiting: [],
        agentRemainingWaitCount: 0,
        errorMessage: null,
      };
    case 'agent.run.failed':
      return {
        ...withEvent,
        statusText: 'Agent 执行未完成，正在同步任务状态',
        currentActivityText: null,
        agentWaiting: [],
        agentRemainingWaitCount: 0,
      };
    case 'agent.run.cancelled':
      return {
        ...withEvent,
        statusText: 'Agent 已取消，正在同步任务状态',
        currentActivityText: null,
        agentWaiting: [],
        agentRemainingWaitCount: 0,
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

const CP7_EVENT_TYPES = new Set([
  'mcp.runtime_unavailable',
  'mcp.execution_status_unknown',
  'mcp.execution_status_resolution',
  'mcp.late_terminal_result_recovered',
]);

const KNOWN_TASK_EVENT_TYPES = new Set([
  'task.accepted', 'task.graph_created', 'node.started', 'node.completed',
  'node.waiting_for_input', 'node.ready_to_resume', 'node.resuming',
  'main_agent.output_delta', 'main_agent.reasoning_delta', 'planner.reasoning_delta',
  'memory.reasoning_delta', 'interrupt.reasoning_delta', 'soft_skill.reasoning_delta',
  'main_agent.output_final', 'capability.missing_fallback', 'mcp.server_routed',
  'mcp.discovery_started', 'mcp.discovery_completed', 'mcp.discovery_failed',
  'mcp.queue_entered', 'mcp.queue_left', 'mcp.tool_approval_required',
  'mcp.tool_approval_decided', 'mcp.tool_call_started', 'mcp.tool_call_still_running',
  'mcp.tool_call_completed', 'mcp.tool_call_failed', 'mcp.tool_call_cancelled',
  'mcp.input_required', 'mcp.input_submitted', 'mcp.remote_task_status_changed',
  'mcp.result_artifact_projection',
  'task.completed', 'task.cancellation_requested', 'task.cancelled', 'node.cancelled',
  'node.blocked_by_cancellation', 'node.orphaned', 'node.failed', 'task.failed',
  'skill.progress',
  'agent.run.waiting', 'agent.run.resumed', 'agent.run.completed',
  'agent.run.failed', 'agent.run.cancelled', 'agent.reasoning_delta',
]);

const AGENT_FRONTEND_EVENT_TYPES = new Set([
  'agent.run.waiting',
  'agent.run.resumed',
  'agent.run.completed',
  'agent.run.failed',
  'agent.run.cancelled',
  'agent.reasoning_delta',
]);

const AGENT_OUTCOMES = new Set([
  'aborted', 'acquired', 'cancelled', 'completed', 'duplicate', 'failed',
  'lease_conflict', 'lease_lost', 'rejected', 'renewed', 'resumed', 'waiting',
]);

const AGENT_WAITING_REASONS = new Set([
  'mcp_approval', 'mcp_elicitation', 'mcp_remote_task', 'skill_input',
]);

function isKnownTaskEventType(eventType: string): boolean {
  return KNOWN_TASK_EVENT_TYPES.has(eventType);
}

export function isClosedCP7Event(event: TaskEventEnvelope): boolean {
  const payload = event.payload;
  if (!isRecord(payload)) return false;
  if (AGENT_FRONTEND_EVENT_TYPES.has(event.event_type)) {
    return isClosedAgentFrontendEvent(event, payload);
  }
  if (event.event_type === 'mcp.result_artifact_projection') {
    const projection = parseMCPResultArtifactProjection(payload);
    return projection !== null
      && event.event_id.startsWith('mcp-result-artifact-projection:v1:')
      && event.event_id.endsWith(`:${projection.status}:${projection.reason_code}`);
  }
  if (event.event_type === 'mcp.runtime_unavailable') {
    return hasExactKeys(payload, ['status', 'reason_code'])
      && payload.status === 'unavailable'
      && payload.reason_code === 'no_user_scoped_server'
      && event.event_id === `mcp-no-server:v1:${event.task_id}:01-runtime-unavailable`;
  }
  if (event.event_type === 'mcp.execution_status_unknown') {
    return hasExactKeys(payload, [
      'schema', 'projection_id', 'intent_id', 'call_id', 'task_id', 'node_id',
      'projection_revision', 'intent_revision', 'unknown_terminal_at', 'reason_code',
      'no_replay', 'result_receipt_id', 'predecessor_event_id',
    ])
      && payload.schema === 'maf.user_mcp.execution_status_unknown.v1'
      && hasIdentityFields(payload, event)
      && isNonEmptyString(payload.intent_id)
      && isNonEmptyString(payload.call_id)
      && isNonEmptyString(payload.projection_id)
      && payload.projection_id === `mcp-terminal-projection:v1:${payload.call_id}`
      && payload.projection_revision === 0
      && isNonNegativeInteger(payload.intent_revision)
      && isNonEmptyString(payload.unknown_terminal_at)
      && payload.reason_code === 'trusted_terminal_result_absent'
      && payload.no_replay === true
      && payload.result_receipt_id === null
      && payload.predecessor_event_id === null
      && event.event_id === expectedUnknownEventId(payload.call_id, payload.intent_revision)
      && sameInstant(event.created_at, payload.unknown_terminal_at);
  }
  if (event.event_type === 'task.failed' && isUnknownTaskFailedPayload(payload)) {
    return hasExactKeys(payload, [
      'schema', 'projection_id', 'call_id', 'task_id', 'node_id', 'code', 'no_replay',
      'unknown_event_id', 'predecessor_event_id',
    ])
      && payload.schema === 'maf.user_mcp.unknown_task_failed.v1'
      && hasIdentityFields(payload, event)
      && isNonEmptyString(payload.projection_id)
      && isNonEmptyString(payload.call_id)
      && payload.code === 'execution_status_unknown'
      && payload.no_replay === true
      && isNonEmptyString(payload.unknown_event_id)
      && payload.predecessor_event_id === payload.unknown_event_id
      && event.event_id === expectedUnknownTaskFailedEventId(payload.call_id, unknownIntentRevision(payload.unknown_event_id));
  }
  if (event.event_type === 'task.failed' && isNoServerTaskFailedPayload(payload)) {
    return hasExactKeys(payload, ['code'])
      && payload.code === 'mcp_runtime_unavailable'
      && event.event_id === `mcp-no-server:v1:${event.task_id}:02-task-failed`;
  }
  if (event.event_type === 'mcp.execution_status_resolution') {
    return hasExactKeys(payload, [
      'schema', 'projection_id', 'intent_id', 'call_id', 'task_id', 'node_id',
      'unknown_event_id', 'task_failed_event_id', 'result_receipt_id',
      'from_projection_revision', 'to_projection_revision', 'from_intent_revision',
      'to_intent_revision', 'unknown_terminal_at', 'resolved_at', 'predecessor_event_id',
    ])
      && payload.schema === 'maf.user_mcp.execution_status_resolution.v1'
      && hasIdentityFields(payload, event)
      && allNonEmptyStrings(payload, [
        'projection_id', 'intent_id', 'call_id', 'unknown_event_id', 'task_failed_event_id',
        'result_receipt_id', 'unknown_terminal_at', 'resolved_at',
      ])
      && payload.from_projection_revision === 0
      && payload.to_projection_revision === 1
      && isNonNegativeInteger(payload.from_intent_revision)
      && isNonNegativeInteger(payload.to_intent_revision)
      && payload.to_intent_revision === (payload.from_intent_revision as number) + 1
      && payload.predecessor_event_id === payload.task_failed_event_id
      && event.event_id === expectedResolutionEventId(payload.call_id)
      && sameInstant(event.created_at, payload.resolved_at);
  }
  if (event.event_type === 'mcp.late_terminal_result_recovered') {
    const terminalState = payload.terminal_state;
    const completed = terminalState === 'completed';
    return hasExactKeys(payload, [
      'schema', 'projection_id', 'intent_id', 'call_id', 'task_id', 'node_id',
      'unknown_event_id', 'resolution_event_id', 'result_receipt_id',
      'result_payload_sha256', 'projection_revision', 'terminal_state', 'safe_result_ref',
      'safe_result_ref_sha256', 'safe_error_code', 'resolved_at', 'task_remains_failed',
      'node_remains_failed', 'no_replay', 'predecessor_event_id',
    ])
      && payload.schema === 'maf.user_mcp.late_terminal_result_recovered.v1'
      && hasIdentityFields(payload, event)
      && allNonEmptyStrings(payload, [
        'projection_id', 'intent_id', 'call_id', 'unknown_event_id', 'resolution_event_id',
        'result_receipt_id', 'result_payload_sha256', 'resolved_at',
      ])
      && payload.projection_revision === 1
      && (terminalState === 'completed' || terminalState === 'failed' || terminalState === 'cancelled')
      && (completed
        ? isNonEmptyString(payload.safe_result_ref) && isNonEmptyString(payload.safe_result_ref_sha256) && payload.safe_error_code === null
        : payload.safe_result_ref === null && payload.safe_result_ref_sha256 === null && isNonEmptyString(payload.safe_error_code))
      && payload.task_remains_failed === true
      && payload.node_remains_failed === true
      && payload.no_replay === true
      && payload.predecessor_event_id === payload.resolution_event_id
      && event.event_id === expectedCorrectionEventId(payload.call_id);
  }
  return true;
}

function isClosedAgentFrontendEvent(
  event: TaskEventEnvelope,
  payload: Record<string, unknown>,
): boolean {
  if (event.event_type === 'agent.reasoning_delta') {
    return hasExactKeys(payload, ['delta', 'ordinal', 'sample_id'])
      && isNonEmptyString(payload.delta)
      && isNonNegativeInteger(payload.ordinal)
      && isNonEmptyString(payload.sample_id);
  }
  if (event.event_type === 'agent.run.waiting') {
    return hasExactKeys(payload, ['interrupt_id', 'reason_kind', 'remaining_count'])
      && isNonEmptyString(payload.interrupt_id)
      && AGENT_WAITING_REASONS.has(String(payload.reason_kind))
      && isNonNegativeInteger(payload.remaining_count);
  }
  if (event.event_type === 'agent.run.resumed') {
    return hasExactKeys(payload, ['outcome', 'remaining_count'])
      && AGENT_OUTCOMES.has(String(payload.outcome))
      && isNonNegativeInteger(payload.remaining_count);
  }
  return hasExactKeys(payload, [
    'compaction_count', 'duration_seconds', 'outcome', 'sample_count', 'tool_call_count',
  ])
    && isNonNegativeInteger(payload.compaction_count)
    && isNonNegativeFiniteNumber(payload.duration_seconds)
    && AGENT_OUTCOMES.has(String(payload.outcome))
    && isNonNegativeInteger(payload.sample_count)
    && isNonNegativeInteger(payload.tool_call_count);
}

export function foldMCPResultArtifactProjections(
  values: readonly unknown[],
): MCPResultArtifactProjection[] | null {
  if (values.length > 120) return null;
  const grouped = new Map<string, Map<MCPResultArtifactProjection['status'], MCPResultArtifactProjection>>();
  for (const value of values) {
    const projection = parseMCPResultArtifactProjection(value);
    if (!projection) return null;
    let byStatus = grouped.get(projection.safe_call_ref);
    if (!byStatus) {
      if (grouped.size >= 20) return null;
      byStatus = new Map();
      grouped.set(projection.safe_call_ref, byStatus);
    }
    const current = byStatus.get(projection.status);
    if (!current || projectionReasonPriority(projection) > projectionReasonPriority(current)) {
      byStatus.set(projection.status, projection);
    }
  }
  const folded: MCPResultArtifactProjection[] = [];
  for (const safeCallRef of [...grouped.keys()].sort()) {
    const byStatus = grouped.get(safeCallRef)!;
    if (byStatus.has('ready') && byStatus.has('permanent_failure')) return null;
    const selected = byStatus.get('ready') ?? byStatus.get('permanent_failure') ?? byStatus.get('deferred');
    if (selected) folded.push(selected);
  }
  return folded;
}

function parseMCPResultArtifactProjection(value: unknown): MCPResultArtifactProjection | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    'schema', 'safe_call_ref', 'status', 'reason_code', 'artifact_count',
  ])) return null;
  if (
    value.schema !== 'maf.user_mcp.result_artifact_projection.v1'
    || typeof value.safe_call_ref !== 'string'
    || !/^[0-9a-f]{64}$/.test(value.safe_call_ref)
    || !['ready', 'deferred', 'permanent_failure'].includes(String(value.status))
    || !['promoted', 'already_promoted', 'capacity_unavailable', 'projection_failed', 'source_expired'].includes(String(value.reason_code))
    || (value.artifact_count !== 0 && value.artifact_count !== 1)
  ) return null;
  const projection = value as unknown as MCPResultArtifactProjection;
  const validReason = projection.status === 'ready'
    ? ['promoted', 'already_promoted'].includes(projection.reason_code)
    : projection.status === 'deferred'
      ? ['capacity_unavailable', 'projection_failed'].includes(projection.reason_code)
      : ['projection_failed', 'source_expired'].includes(projection.reason_code);
  if (!validReason || (projection.status === 'ready') !== (projection.artifact_count === 1)) return null;
  return projection;
}

function projectionReasonPriority(projection: MCPResultArtifactProjection): number {
  if (projection.status === 'ready') return projection.reason_code === 'promoted' ? 1 : 0;
  if (projection.status === 'deferred') return projection.reason_code === 'projection_failed' ? 1 : 0;
  return projection.reason_code === 'source_expired' ? 1 : 0;
}

function isCP7ContractEvent(event: TaskEventEnvelope): boolean {
  return CP7_EVENT_TYPES.has(event.event_type)
    || (event.event_type === 'task.failed'
      && (isUnknownTaskFailedPayload(event.payload) || isNoServerTaskFailedPayload(event.payload)));
}

function isUnknownTaskFailedPayload(payload: Record<string, unknown>): boolean {
  return payload.schema === 'maf.user_mcp.unknown_task_failed.v1'
    || payload.code === 'execution_status_unknown';
}

function isNoServerTaskFailedPayload(payload: Record<string, unknown>): boolean {
  return payload.code === 'mcp_runtime_unavailable';
}

function predecessorEventId(event: TaskEventEnvelope): string | null {
  if (!isCP7ContractEvent(event)) return null;
  if (event.event_type === 'task.failed' && isNoServerTaskFailedPayload(event.payload)) {
    return `mcp-no-server:v1:${event.task_id}:01-runtime-unavailable`;
  }
  return typeof event.payload.predecessor_event_id === 'string'
    ? event.payload.predecessor_event_id
    : null;
}

function drainPendingTaskEvents(state: TaskEventState): TaskEventState {
  let current = state;
  while (true) {
    const ready = current.pendingEvents
      .filter((event) => {
        const predecessorId = predecessorEventId(event);
        return predecessorId === null || current.seenEventIds.includes(predecessorId);
      })
      .sort(compareTaskEvents)[0];
    if (!ready) return current;
    if (!hasValidConsumedChainBinding(current, ready)) {
      return {
        ...current,
        pendingEvents: current.pendingEvents.filter((event) => event.event_id !== ready.event_id),
        eventSyncError: '任务事件链绑定不一致，请重新同步任务历史。',
      };
    }
    current = reduceTaskEvent(current, ready);
  }
}

function hasValidConsumedChainBinding(state: TaskEventState, event: TaskEventEnvelope): boolean {
  if (!isCP7ContractEvent(event) || event.event_type === 'mcp.runtime_unavailable') return true;
  if (event.event_type === 'mcp.execution_status_unknown') return true;
  if (event.event_type === 'task.failed' && isNoServerTaskFailedPayload(event.payload)) {
    return state.mcp.availability?.status === 'unavailable';
  }
  const unknown = state.mcp.executionUnknown;
  if (!unknown) return false;
  if (event.event_type === 'task.failed') {
    return event.event_id === unknown.taskFailedEventId
      && event.payload.unknown_event_id === unknown.unknownEventId
      && event.payload.projection_id === unknown.projectionId
      && event.payload.call_id === unknown.callId
      && event.payload.task_id === event.task_id
      && event.payload.node_id === unknown.nodeId
      && isStrictlyAfter(event.created_at, unknown.createdAt);
  }
  if (event.event_type === 'mcp.execution_status_resolution') {
    return event.payload.projection_id === unknown.projectionId
      && event.payload.intent_id === unknown.intentId
      && event.payload.call_id === unknown.callId
      && event.payload.node_id === unknown.nodeId
      && event.payload.unknown_event_id === unknown.unknownEventId
      && event.payload.task_failed_event_id === unknown.taskFailedEventId
      && event.payload.from_projection_revision === unknown.projectionRevision
      && event.payload.from_intent_revision === unknown.intentRevision
      && event.payload.unknown_terminal_at === unknown.unknownTerminalAt
      && isStrictlyAfter(event.created_at, unknown.createdAt);
  }
  if (event.event_type === 'mcp.late_terminal_result_recovered') {
    const resolution = state.mcp.executionResolution;
    return resolution !== null
      && event.payload.projection_id === unknown.projectionId
      && event.payload.intent_id === unknown.intentId
      && event.payload.call_id === unknown.callId
      && event.payload.node_id === unknown.nodeId
      && event.payload.unknown_event_id === unknown.unknownEventId
      && event.payload.resolution_event_id === resolution.resolutionEventId
      && event.payload.result_receipt_id === resolution.resultReceiptId
      && event.payload.projection_revision === 1
      && event.payload.resolved_at === resolution.resolvedAt
      && isStrictlyAfter(event.created_at, resolution.createdAt);
  }
  return true;
}

function compareTaskEvents(left: TaskEventEnvelope, right: TaskEventEnvelope): number {
  const byCreatedAt = (left.created_at ?? '').localeCompare(right.created_at ?? '');
  return byCreatedAt || left.event_id.localeCompare(right.event_id);
}

function taskEventFingerprint(event: TaskEventEnvelope): string {
  return canonicalJson({
    event_type: event.event_type,
    conversation_id: event.conversation_id,
    task_id: event.task_id,
    node_id: event.node_id,
    payload: event.payload,
    created_at: event.created_at,
  });
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function hasExactKeys(payload: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(payload).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function hasIdentityFields(payload: Record<string, unknown>, event: TaskEventEnvelope): boolean {
  return payload.task_id === event.task_id
    && typeof payload.node_id === 'string'
    && payload.node_id === event.node_id;
}

function allNonEmptyStrings(payload: Record<string, unknown>, keys: string[]): boolean {
  return keys.every((key) => isNonEmptyString(payload[key]));
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isNonNegativeFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function expectedUnknownEventId(callId: unknown, intentRevision: unknown): string {
  return `mcp-execution-status-unknown:v1:${String(callId)}:${String(intentRevision)}:01-unknown`;
}

function expectedUnknownTaskFailedEventId(callId: unknown, intentRevision: unknown): string {
  return `mcp-execution-status-unknown:v1:${String(callId)}:${String(intentRevision)}:02-task-failed`;
}

function expectedResolutionEventId(callId: unknown): string {
  return `mcp-late-terminal:v1:${String(callId)}:1:01-resolution`;
}

function expectedCorrectionEventId(callId: unknown): string {
  return `mcp-late-terminal:v1:${String(callId)}:1:02-correction`;
}

function unknownIntentRevision(eventId: unknown): number {
  if (typeof eventId !== 'string') return -1;
  const match = eventId.match(/^mcp-execution-status-unknown:v1:[^:]+:(\d+):01-unknown$/);
  return match ? Number(match[1]) : -1;
}

function sameInstant(left: unknown, right: unknown): boolean {
  const leftMicros = instantMicros(left);
  const rightMicros = instantMicros(right);
  return leftMicros !== null && leftMicros === rightMicros;
}

function isStrictlyAfter(left: unknown, right: unknown): boolean {
  const leftMicros = instantMicros(left);
  const rightMicros = instantMicros(right);
  return leftMicros !== null && rightMicros !== null && leftMicros > rightMicros;
}

function instantMicros(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const match = value.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/);
  if (!match) return null;
  const seconds = Date.parse(`${match[1]}${match[3]}`);
  if (!Number.isFinite(seconds)) return null;
  const fractionMicros = Number((match[2] ?? '').padEnd(6, '0'));
  return seconds * 1_000 + fractionMicros;
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
