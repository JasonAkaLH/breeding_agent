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

export interface TaskEventState {
  phase: TaskPhase;
  statusText: string;
  currentCapabilityId: string | null;
  currentCapabilityLabel: string | null;
  currentActivityText: string | null;
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
      const activity = nodeActivity(event.payload.capability_id);
      return {
        ...withEvent,
        phase: 'running',
        statusText: activity.stepText,
        currentCapabilityId: activity.capabilityId,
        currentCapabilityLabel: activity.capabilityLabel,
        currentActivityText: `正在执行 ${activity.capabilityLabel}：${activity.stepText}`,
        errorMessage: null,
      };
    }
    case 'node.completed': {
      const activity = nodeActivity(event.payload.capability_id);
      return {
        ...withEvent,
        phase: state.phase === 'streaming' ? 'streaming' : 'running',
        statusText: '正在整理执行结果',
        currentCapabilityId: activity.capabilityId,
        currentCapabilityLabel: activity.capabilityLabel,
        currentActivityText: `${activity.capabilityLabel} 已完成，正在整理执行结果`,
        errorMessage: null,
      };
    }
    case 'main_agent.output_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      return { ...withEvent, phase: 'streaming', statusText: '正在生成答案', currentActivityText: null, assistantText: `${state.assistantText}${delta}`, errorMessage: null };
    }
    case 'main_agent.reasoning_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      return { ...withEvent, phase: 'streaming', statusText: '正在思考并生成答案', currentActivityText: null, reasoningText: `${state.reasoningText}${delta}`, errorMessage: null };
    }
    case 'main_agent.output_final':
      return { ...withEvent, phase: state.phase === 'idle' ? 'running' : state.phase, statusText: '回答生成完成，正在收尾', currentActivityText: null, errorMessage: null };
    case 'task.completed':
      return { ...withEvent, phase: 'loading_artifacts', statusText: '任务完成，正在整理结果', currentActivityText: null, errorMessage: null };
    case 'task.cancellation_requested':
      return { ...withEvent, phase: 'cancelling', statusText: '取消请求已发送', currentActivityText: '正在取消当前任务', errorMessage: null };
    case 'task.cancelled':
      return { ...withEvent, phase: 'cancelled', statusText: '任务已取消', currentCapabilityId: null, currentCapabilityLabel: null, currentActivityText: null, errorMessage: null };
    case 'node.failed':
      return { ...withEvent, phase: 'failed', statusText: '本次任务未完成', currentActivityText: null, errorMessage: failureMessage(event.payload, event.node_id) };
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
    case 'sql_query.sql_guard_blocked':
      return { ...withEvent, phase: 'failed', statusText: '查询未执行', currentActivityText: null, errorMessage: '当前查询不符合只读查询安全边界，请改用查询类问题。' };
    default:
      return state;
  }
}

function nodeActivity(value: unknown): { capabilityId: string; capabilityLabel: string; stepText: string } {
  const capabilityId = typeof value === 'string' ? value : '';
  return {
    capabilityId,
    capabilityLabel: capabilityLabel(capabilityId),
    stepText: nodeStatusText(capabilityId),
  };
}

function capabilityLabel(capabilityId: string): string {
  if (capabilityId.startsWith('sql_query.')) return 'SQLQuery';
  if (capabilityId.startsWith('main_agent.')) return '主代理';
  return capabilityId || '能力';
}

function nodeStatusText(capabilityId: string): string {
  if (capabilityId === 'sql_query.intent_route') return '正在理解查询意图';
  if (capabilityId === 'sql_query.schema_context_prepare' || capabilityId === 'sql_query.sql_generate') return '正在准备数据库查询';
  if (capabilityId === 'sql_query.sql_guard') return '正在检查查询安全边界';
  if (capabilityId === 'sql_query.sql_execute_readonly') return '正在检索数据库';
  if (capabilityId === 'sql_query.result_filtering') return '正在筛选查询结果';
  if (capabilityId === 'main_agent.respond') return '正在生成回答';
  return '正在处理';
}

const SQL_GUARD_BLOCK_CODES = new Set([
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
  if (SQL_GUARD_BLOCK_CODES.has(code) || nodeId?.includes(':sql_guard')) {
    return '当前查询不符合只读查询安全边界，请改用查询类问题。';
  }
  if (code === 'guard_token_missing') return '查询安全校验未通过，请调整问题后重试。';
  if (code === 'db_transient_error') return '数据库暂时不可用，请稍后重试。';
  return '本次任务未完成，请调整问题后重试。';
}
