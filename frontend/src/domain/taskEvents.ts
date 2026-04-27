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
  | 'waiting_input_unsupported';

export interface TaskEventState {
  phase: TaskPhase;
  statusText: string;
  assistantText: string;
  errorMessage: string | null;
  seenEventIds: string[];
}

export function createInitialTaskEventState(): TaskEventState {
  return {
    phase: 'idle',
    statusText: '准备就绪',
    assistantText: '',
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

export function markTaskCompleted(state: TaskEventState, statusText = '任务已完成'): TaskEventState {
  return { ...state, phase: 'completed', statusText, errorMessage: null };
}

export function markTaskFailed(state: TaskEventState, errorMessage: string): TaskEventState {
  return { ...state, phase: 'failed', statusText: '本次任务未完成', errorMessage };
}

export function markWaitingInputUnsupported(state: TaskEventState): TaskEventState {
  return {
    ...state,
    phase: 'waiting_input_unsupported',
    statusText: '任务需要补充信息',
    errorMessage: '当前前端版本暂不支持继续该任务，请重新提交更完整的问题。',
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
      return { ...withEvent, phase: 'accepted', statusText: '任务已提交', errorMessage: null };
    case 'task.graph_created':
      return { ...withEvent, phase: 'running', statusText: '正在规划/准备执行', errorMessage: null };
    case 'node.started':
      return { ...withEvent, phase: 'running', statusText: nodeStatusText(event.payload.capability_id), errorMessage: null };
    case 'node.completed':
      return { ...withEvent, phase: state.phase === 'streaming' ? 'streaming' : 'running', statusText: '正在整理执行结果', errorMessage: null };
    case 'main_agent.output_delta': {
      const delta = typeof event.payload.delta === 'string' ? event.payload.delta : '';
      return { ...withEvent, phase: 'streaming', statusText: '正在生成答案', assistantText: `${state.assistantText}${delta}`, errorMessage: null };
    }
    case 'main_agent.output_final':
      return { ...withEvent, phase: state.phase === 'idle' ? 'running' : state.phase, statusText: '回答生成完成，正在收尾', errorMessage: null };
    case 'task.completed':
      return { ...withEvent, phase: 'loading_artifacts', statusText: '任务完成，正在整理结果', errorMessage: null };
    case 'task.cancellation_requested':
      return { ...withEvent, phase: 'cancelling', statusText: '取消请求已发送', errorMessage: null };
    case 'task.cancelled':
      return { ...withEvent, phase: 'cancelled', statusText: '任务已取消', errorMessage: null };
    case 'node.failed':
      return { ...withEvent, phase: 'failed', statusText: '本次任务未完成', errorMessage: failureMessage(event.payload, event.node_id) };
    case 'task.failed':
      return {
        ...withEvent,
        phase: 'failed',
        statusText: '本次任务未完成',
        errorMessage: state.errorMessage ?? failureMessage(event.payload, event.node_id),
      };
    case 'sql_query.sql_guard_blocked':
      return { ...withEvent, phase: 'failed', statusText: '查询未执行', errorMessage: '当前查询不符合只读查询安全边界，请改用查询类问题。' };
    default:
      return state;
  }
}

function nodeStatusText(value: unknown): string {
  const capabilityId = typeof value === 'string' ? value : '';
  if (capabilityId === 'sql_query.intent_route') return '正在理解查询意图';
  if (capabilityId === 'sql_query.schema_context_prepare' || capabilityId === 'sql_query.sql_generate') return '正在准备数据库查询';
  if (capabilityId === 'sql_query.sql_guard') return '正在检查查询安全边界';
  if (capabilityId === 'sql_query.sql_execute_readonly') return '正在检索数据库';
  if (capabilityId === 'sql_query.result_summarize') return '正在整理查询结果';
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
