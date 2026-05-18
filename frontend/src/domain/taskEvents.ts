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
  status: 'pending' | 'running' | 'completed' | 'failed';
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
  errorMessage: string | null;
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
    errorMessage: null,
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
  return ['submitting', 'accepted', 'running', 'streaming', 'loading_artifacts', 'cancelling'].includes(phase);
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
    case 'main_agent.output_delta': {
      if (!isVisibleMainAgentResponse(event.payload)) return withEvent;
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      return { ...withEvent, phase: 'streaming', statusText: '正在生成答案', currentActivityText: null, assistantText: `${state.assistantText}${delta}`, errorMessage: null };
    }
    case 'main_agent.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      return { ...withEvent, phase: 'streaming', statusText: '正在思考并生成答案', currentActivityText: null, reasoningText: `${state.reasoningText}${delta}`, errorMessage: null };
    }
    case 'main_agent.output_final':
      if (!isVisibleMainAgentResponse(event.payload)) return withEvent;
      return { ...withEvent, phase: state.phase === 'idle' ? 'running' : state.phase, statusText: '回答生成完成，正在收尾', currentActivityText: null, errorMessage: null };
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
    case 'node.failed':
      return {
        ...withEvent,
        phase: 'failed',
        statusText: '本次任务未完成',
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
  if (capabilityId.startsWith('main_agent.')) return '主代理';
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
  if (code === 'guard_token_missing') return '查询安全校验未通过，请调整问题后重试。';
  if (code === 'db_transient_error') return '数据库暂时不可用，请稍后重试。';
  return '本次任务未完成，请调整问题后重试。';
}
