import { describe, expect, it } from 'vitest';
import { applyTaskEvent, createInitialTaskEventState, createRestoringTaskState, foldMCPResultArtifactProjections, isTaskActive, markTaskCompleted, markTaskFailed, markWaitingInputRequired, parseCapabilityFallbackNotice, taskProgressDisplayText } from './taskEvents';
import type { TaskEventEnvelope } from '../api/types';

function event(event_type: string, payload: Record<string, unknown> = {}, event_id = event_type, node_id: string | null = null): TaskEventEnvelope {
  return {
    event_id,
    conversation_id: 'conv-1',
    task_id: 'task-1',
    node_id,
    event_type,
    payload,
    created_at: '2026-04-27T00:00:00',
  };
}

const unknownEventId = 'mcp-execution-status-unknown:v1:call-1:4:01-unknown';
const failedEventId = 'mcp-execution-status-unknown:v1:call-1:4:02-task-failed';
const resolutionEventId = 'mcp-late-terminal:v1:call-1:1:01-resolution';
const correctionEventId = 'mcp-late-terminal:v1:call-1:1:02-correction';

function terminalProjectionEvents(): TaskEventEnvelope[] {
  return [
    { ...event('mcp.execution_status_unknown', {
      schema: 'maf.user_mcp.execution_status_unknown.v1',
      projection_id: 'mcp-terminal-projection:v1:call-1',
      intent_id: 'intent-1',
      call_id: 'call-1',
      task_id: 'task-1',
      node_id: 'node-1',
      projection_revision: 0,
      intent_revision: 4,
      unknown_terminal_at: '2026-04-27T00:00:00Z',
      reason_code: 'trusted_terminal_result_absent',
      no_replay: true,
      result_receipt_id: null,
      predecessor_event_id: null,
    }, unknownEventId, 'node-1'), created_at: '2026-04-27T00:00:00Z' },
    { ...event('task.failed', {
      schema: 'maf.user_mcp.unknown_task_failed.v1',
      projection_id: 'mcp-terminal-projection:v1:call-1',
      call_id: 'call-1',
      task_id: 'task-1',
      node_id: 'node-1',
      code: 'execution_status_unknown',
      no_replay: true,
      unknown_event_id: unknownEventId,
      predecessor_event_id: unknownEventId,
    }, failedEventId, 'node-1'), created_at: '2026-04-27T00:00:00.000001Z' },
    { ...event('mcp.execution_status_resolution', {
      schema: 'maf.user_mcp.execution_status_resolution.v1',
      projection_id: 'mcp-terminal-projection:v1:call-1',
      intent_id: 'intent-1',
      call_id: 'call-1',
      task_id: 'task-1',
      node_id: 'node-1',
      unknown_event_id: unknownEventId,
      task_failed_event_id: failedEventId,
      result_receipt_id: 'receipt-1',
      from_projection_revision: 0,
      to_projection_revision: 1,
      from_intent_revision: 4,
      to_intent_revision: 5,
      unknown_terminal_at: '2026-04-27T00:00:00Z',
      resolved_at: '2026-04-27T00:01:00Z',
      predecessor_event_id: failedEventId,
    }, resolutionEventId, 'node-1'), created_at: '2026-04-27T00:01:00Z' },
    { ...event('mcp.late_terminal_result_recovered', {
      schema: 'maf.user_mcp.late_terminal_result_recovered.v1',
      projection_id: 'mcp-terminal-projection:v1:call-1',
      intent_id: 'intent-1',
      call_id: 'call-1',
      task_id: 'task-1',
      node_id: 'node-1',
      unknown_event_id: unknownEventId,
      resolution_event_id: resolutionEventId,
      result_receipt_id: 'receipt-1',
      result_payload_sha256: 'sha256:result',
      projection_revision: 1,
      terminal_state: 'completed',
      safe_result_ref: 'artifact:safe-result',
      safe_result_ref_sha256: 'sha256:ref',
      safe_error_code: null,
      resolved_at: '2026-04-27T00:01:00Z',
      task_remains_failed: true,
      node_remains_failed: true,
      no_replay: true,
      predecessor_event_id: resolutionEventId,
    }, correctionEventId, 'node-1'), created_at: '2026-04-27T00:01:00.000001Z' },
  ];
}

describe('applyTaskEvent', () => {
  it('folds MCP result artifact projection events with terminal state precedence', () => {
    const safeCallRef = 'a'.repeat(64);
    let state = applyTaskEvent(createInitialTaskEventState(), event(
      'mcp.result_artifact_projection',
      {
        schema: 'maf.user_mcp.result_artifact_projection.v1',
        safe_call_ref: safeCallRef,
        status: 'deferred',
        reason_code: 'capacity_unavailable',
        artifact_count: 0,
      },
      'mcp-result-artifact-projection:v1:artifact-1:deferred:capacity_unavailable',
      'node-1',
    ));
    state = applyTaskEvent(state, event(
      'mcp.result_artifact_projection',
      {
        schema: 'maf.user_mcp.result_artifact_projection.v1',
        safe_call_ref: safeCallRef,
        status: 'ready',
        reason_code: 'promoted',
        artifact_count: 1,
      },
      'mcp-result-artifact-projection:v1:artifact-1:ready:promoted',
      'node-1',
    ));

    expect(state.mcp.resultArtifactProjections).toEqual([
      expect.objectContaining({ safe_call_ref: safeCallRef, status: 'ready', reason_code: 'promoted' }),
    ]);
    expect(state.eventSyncError).toBeNull();
  });

  it('fails closed when one MCP call has both ready and permanent projection authority', () => {
    const safeCallRef = 'b'.repeat(64);
    const ready = {
      schema: 'maf.user_mcp.result_artifact_projection.v1',
      safe_call_ref: safeCallRef,
      status: 'ready',
      reason_code: 'promoted',
      artifact_count: 1,
    } as const;
    const permanent = {
      ...ready,
      status: 'permanent_failure',
      reason_code: 'source_expired',
      artifact_count: 0,
    } as const;

    expect(foldMCPResultArtifactProjections([ready, permanent])).toBeNull();
  });
  it('creates a restoring task state for refresh recovery', () => {
    const state = createRestoringTaskState();

    expect(state.phase).toBe('running');
    expect(state.statusText).toContain('恢复任务状态');
    expect(state.currentActivityText).toContain('同步任务输出');
    expect(state.assistantText).toBe('');
    expect(state.reasoningText).toBe('');
    expect(state.seenEventIds).toEqual([]);
  });

  it('replays Agent reasoning and skill progress from restoring state', () => {
    let state = createRestoringTaskState();
    state = applyTaskEvent(state, event('agent.reasoning_delta', { delta: '先分析', ordinal: 1, sample_id: 'sample-restore' }, 'restore-reasoning'));
    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.example', skill_name: 'ExampleSkill', label: '正在处理文件' }, 'restore-skill', 'node-skill'));

    expect(state.assistantText).toBe('');
    expect(state.reasoningText).toBe('先分析');
    expect(state.skillStatuses).toEqual([
      expect.objectContaining({ capabilityId: 'skill.example', label: 'ExampleSkill', statusText: '正在处理文件', status: 'running' }),
    ]);
  });

  it('maps accepted and running events to business states', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('task.accepted'));
    expect(state.phase).toBe('accepted');
    expect(state.statusText).toContain('已提交');
    expect(taskProgressDisplayText(state)).toBe('任务已提交');

    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', domain_kind: 'data_query', stage: 'execute_query' }, 'node-1'));
    expect(state.phase).toBe('running');
    expect(state.statusText).toContain('检索数据');
    expect(state.currentActivityText).toBe('正在执行 data-query：正在检索数据');
    expect(state.currentCapabilityLabel).toBe('data-query');
    expect(taskProgressDisplayText(state)).toBe('正在执行 data-query：正在检索数据');

    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.data_query', domain_kind: 'data_query', stage: 'filter_results' }, 'node-filter'));
    expect(state.statusText).toContain('筛选查询结果');
  });

  it('uses generic capability ids for future capabilities in activity text', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('node.started', { capability_id: 'report.generate' }, 'node-report'));

    expect(state.phase).toBe('running');
    expect(state.currentActivityText).toBe('正在执行 report.generate：正在处理');
    expect(state.currentCapabilityLabel).toBe('report.generate');
  });

  it('falls back to the skill capability id when a skill event has no skill name', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('skill.progress', { capability_id: 'skill.data_query', stage: 'execute_query' }, 'skill-progress'));

    expect(state.currentActivityText).toBe('正在执行 skill.data_query：正在检索数据');
    expect(state.currentCapabilityLabel).toBe('skill.data_query');
  });

  it('uses skill_name from node activity payload when available', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('node.started', { capability_id: 'skill.report_query', skill_name: 'report-query' }, 'skill-node-started'));

    expect(state.currentActivityText).toBe('正在执行 report-query：正在处理');
    expect(state.currentCapabilityLabel).toBe('report-query');
  });

  it('tracks skill status lines independently for parallel skill events', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-start', 'node-sql'));
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.rcbd', skill_name: 'RCBD' }, 'rcbd-start', 'node-rcbd'));

    expect(state.skillStatuses).toEqual([
      expect.objectContaining({ key: 'node-sql', nodeId: 'node-sql', capabilityId: 'skill.data_query', label: 'data-query', status: 'running', statusText: '正在处理' }),
      expect.objectContaining({ key: 'node-rcbd', nodeId: 'node-rcbd', capabilityId: 'skill.rcbd', label: 'RCBD', status: 'running', statusText: '正在处理' }),
    ]);

    const previousRows = state.skillStatuses;
    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', stage: 'execute_query' }, 'sql-progress', 'node-sql'));

    expect(state.skillStatuses).not.toBe(previousRows);
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ label: 'data-query', status: 'running', statusText: '正在检索数据' }));
    expect(state.skillStatuses[1]).toEqual(expect.objectContaining({ label: 'RCBD', status: 'running', statusText: '正在处理' }));
  });

  it('lazy-creates skill status lines from progress and keeps duplicate events idempotent', () => {
    const progress = event('skill.progress', { capability_id: 'skill.data_query', skill_name: 'data-query', stage: 'execute_query' }, 'progress-once', 'node-query');
    let state = applyTaskEvent(createInitialTaskEventState(), progress);
    state = applyTaskEvent(state, progress);

    expect(state.skillStatuses).toHaveLength(1);
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ key: 'node-query', label: 'data-query', status: 'running', statusText: '正在检索数据' }));
    expect(state.seenEventIds.filter((eventId) => eventId === 'progress-once')).toHaveLength(1);
  });

  it('marks matching skill status lines completed and closes running rows on task completion', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-start', 'node-sql'));
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.rcbd', skill_name: 'RCBD' }, 'rcbd-start', 'node-rcbd'));
    state = applyTaskEvent(state, event('node.completed', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-complete', 'node-sql'));

    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ label: 'data-query', status: 'completed', statusText: '已完成' }));
    expect(state.skillStatuses[1]).toEqual(expect.objectContaining({ label: 'RCBD', status: 'running' }));

    state = applyTaskEvent(state, event('task.completed', {}, 'task-complete'));

    expect(state.skillStatuses).toEqual([
      expect.objectContaining({ label: 'data-query', status: 'completed', statusText: '已完成' }),
      expect.objectContaining({ label: 'RCBD', status: 'completed', statusText: '已完成' }),
    ]);
  });

  it('marks existing skill rows failed without making node failure a task terminal state', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-start', 'node-sql'));
    state = applyTaskEvent(state, event('node.failed', { code: 'db_transient_error' }, 'sql-failed', 'node-sql'));

    expect(state.phase).toBe('running');
    expect(state.skillStatuses).toHaveLength(1);
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ label: 'data-query', status: 'failed', statusText: '失败' }));
    expect(state.errorMessage).toContain('数据库暂时不可用');

    const unknownFailure = applyTaskEvent(createInitialTaskEventState(), event('node.failed', { code: 'db_transient_error' }, 'unknown-failed', 'node-unknown'));
    expect(unknownFailure.phase).toBe('idle');
    expect(unknownFailure.skillStatuses).toEqual([]);
    expect(unknownFailure.errorMessage).toContain('数据库暂时不可用');
  });

  it('marks node cancellation and blocked events on matching skill rows', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-start', 'node-sql'));
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.rcbd', skill_name: 'RCBD' }, 'rcbd-start', 'node-rcbd'));
    state = applyTaskEvent(state, event('node.cancelled', { capability_id: 'skill.data_query' }, 'sql-cancelled', 'node-sql'));
    state = applyTaskEvent(state, event('node.blocked_by_cancellation', { capability_id: 'skill.rcbd' }, 'rcbd-blocked', 'node-rcbd'));

    expect(state.phase).toBe('running');
    expect(state.skillStatuses).toEqual([
      expect.objectContaining({ key: 'node-sql', label: 'data-query', status: 'cancelled', statusText: '已取消' }),
      expect.objectContaining({ key: 'node-rcbd', label: 'RCBD', status: 'blocked', statusText: '已被取消阻断' }),
    ]);
  });

  it('maps interrupt resume node events to non-terminal running progress', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.ready_to_resume', { capability_id: 'skill.data_query', skill_name: 'data-query', interrupt_id: 'interrupt-1' }, 'ready-to-resume', 'node-sql'));

    expect(state.phase).toBe('running');
    expect(state.statusText).toBe('补充信息已提交');
    expect(state.currentActivityText).toBe('data-query 已收到补充信息，准备恢复执行');
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ key: 'node-sql', label: 'data-query', status: 'running', statusText: '准备恢复执行' }));

    state = applyTaskEvent(state, event('node.resuming', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'resuming', 'node-sql'));

    expect(state.phase).toBe('running');
    expect(state.statusText).toBe('正在恢复执行');
    expect(state.currentActivityText).toBe('data-query 正在恢复执行');
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ key: 'node-sql', label: 'data-query', status: 'running', statusText: '正在恢复执行' }));
  });

  it('maps Agent Run terminal events to public terminal phases', () => {
    const terminalPayload = {
      compaction_count: 0,
      duration_seconds: 0,
      sample_count: 1,
      tool_call_count: 0,
    };
    const completed = applyTaskEvent(createInitialTaskEventState(), event(
      'agent.run.completed',
      { ...terminalPayload, outcome: 'completed' },
      'agent-completed',
    ));
    const failed = applyTaskEvent(createInitialTaskEventState(), event(
      'agent.run.failed',
      { ...terminalPayload, outcome: 'failed' },
      'agent-failed',
    ));
    const cancelled = applyTaskEvent(createInitialTaskEventState(), event(
      'agent.run.cancelled',
      { ...terminalPayload, outcome: 'cancelled' },
      'agent-cancelled',
    ));

    expect(completed.phase).toBe('loading_artifacts');
    expect(failed.phase).toBe('failed');
    expect(cancelled.phase).toBe('cancelled');
  });

  it('does not create skill rows for main-agent nodes and uses fallback keys when node ids are missing', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'agent.final_output' }, 'main-start', 'node-main'));
    expect(state.skillStatuses).toEqual([]);

    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.report', skill_name: 'ReportSkill' }, 'fallback-start'));
    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.report', skill_name: 'ReportSkill', label: '正在整理报告' }, 'fallback-progress'));

    expect(state.skillStatuses).toHaveLength(1);
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ key: 'skill.report::ReportSkill', nodeId: null, label: 'ReportSkill', statusText: '正在整理报告' }));
  });

  it('renders transient Agent reasoning once without treating it as assistant output', () => {
    let state = createInitialTaskEventState();
    const delta = event('agent.reasoning_delta', {
      delta: '继续分析工具结果',
      ordinal: 0,
      sample_id: 'sample-1',
    }, 'agent-reasoning-1');

    state = applyTaskEvent(state, delta);
    state = applyTaskEvent(state, delta);

    expect(state.reasoningText).toBe('继续分析工具结果');
    expect(state.answerReasoningText).toBe('继续分析工具结果');
    expect(state.answerReasoningSampleId).toBe('sample-1');
    expect(state.answerReasoningSampleStart).toBe(0);
    expect(state.reasoningTruncated).toBe(false);
    expect(state.assistantText).toBe('');
    expect(state.seenEventIds).toEqual(['agent-reasoning-1']);
  });

  it('rolls back only the failed Agent sample and ignores stale resets', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('agent.reasoning_delta', {
      delta: '已完成的分析。', ordinal: 0, sample_id: 'sample-1',
    }, 'agent-reasoning-1'));
    state = applyTaskEvent(state, event('agent.reasoning_delta', {
      delta: '尝试调用工具。', ordinal: 1, sample_id: 'sample-2',
    }, 'agent-reasoning-2'));
    const reset = event('agent.reasoning_reset', { sample_id: 'sample-2' }, 'agent-reset-1');

    state = applyTaskEvent(state, reset);
    const afterDuplicate = applyTaskEvent(state, reset);
    const afterStale = applyTaskEvent(afterDuplicate, event(
      'agent.reasoning_reset', { sample_id: 'sample-1' }, 'agent-reset-stale',
    ));

    expect(state.answerReasoningText).toBe('已完成的分析。');
    expect(state.reasoningText).toBe('已完成的分析。');
    expect(state.answerReasoningSampleId).toBeNull();
    expect(afterDuplicate).toBe(state);
    expect(afterStale.answerReasoningText).toBe('已完成的分析。');
  });

  it('keeps the global truncation marker when the current sample is reset', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('agent.reasoning_delta', {
      delta: '上一次分析。', ordinal: 0, sample_id: 'sample-1',
    }, 'agent-reasoning-1'));
    state = applyTaskEvent(state, event('agent.reasoning_delta', {
      delta: '本次失败分析。', ordinal: 1, sample_id: 'sample-2',
    }, 'agent-reasoning-2'));
    state = applyTaskEvent(state, event('agent.reasoning_delta', {
      delta: '思考内容过长，已截断', ordinal: 2, sample_id: 'sample-2',
    }, 'agent-reasoning-marker'));
    state = applyTaskEvent(state, event(
      'agent.reasoning_reset', { sample_id: 'sample-2' }, 'agent-reset-1',
    ));
    state = applyTaskEvent(state, event('agent.reasoning_delta', {
      delta: '不应继续展示', ordinal: 3, sample_id: 'sample-2',
    }, 'agent-reasoning-after-limit'));

    expect(state.answerReasoningText).toBe('上一次分析。');
    expect(state.reasoningTruncated).toBe(true);
    expect(state.reasoningText).toBe('上一次分析。思考内容过长，已截断');
  });

  it.each([524_287, 524_288])(
    'keeps %i UTF-8 bytes without a defensive truncation marker',
    (size) => {
      const state = applyTaskEvent(createInitialTaskEventState(), event('agent.reasoning_delta', {
        delta: 'a'.repeat(size), ordinal: 0, sample_id: `sample-${size}`,
      }, `agent-reasoning-${size}`));

      expect(state.reasoningTruncated).toBe(false);
      expect(new TextEncoder().encode(state.reasoningText)).toHaveLength(size);
    },
  );

  it('truncates 524,289 UTF-8 bytes to the exact bounded display size', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('agent.reasoning_delta', {
      delta: 'a'.repeat(524_289), ordinal: 0, sample_id: 'sample-over-limit',
    }, 'agent-reasoning-over-limit'));

    expect(state.reasoningTruncated).toBe(true);
    expect(new TextEncoder().encode(state.reasoningText)).toHaveLength(524_288);
    expect(state.reasoningText.endsWith('思考内容过长，已截断')).toBe(true);
  });

  it('caps Agent reasoning at a valid UTF-8 boundary and adds one marker', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('agent.reasoning_delta', {
      delta: '你'.repeat(174_763), ordinal: 0, sample_id: 'sample-large',
    }, 'agent-reasoning-large'));

    expect(state.reasoningTruncated).toBe(true);
    expect(state.answerReasoningText).not.toContain('�');
    expect(new TextEncoder().encode(state.reasoningText).length).toBeLessThanOrEqual(524_288);
    expect(state.reasoningText.match(/思考内容过长，已截断/g)).toHaveLength(1);
  });

  it('tracks multiple Agent waits by interrupt and node until each is resumed', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('agent.run.waiting', {
      interrupt_id: 'interrupt-1',
      reason_kind: 'skill_input',
      remaining_count: 2,
    }, 'agent-waiting-1', 'node-1'));
    state = applyTaskEvent(state, event('agent.run.waiting', {
      interrupt_id: 'interrupt-2',
      reason_kind: 'mcp_approval',
      remaining_count: 2,
    }, 'agent-waiting-2', 'node-2'));

    expect(state.phase).toBe('waiting_for_input');
    expect(state.agentRemainingWaitCount).toBe(2);
    expect(state.agentWaiting).toEqual([
      { interruptId: 'interrupt-1', nodeId: 'node-1', reasonKind: 'skill_input' },
      { interruptId: 'interrupt-2', nodeId: 'node-2', reasonKind: 'mcp_approval' },
    ]);

    state = applyTaskEvent(state, event('agent.run.resumed', {
      outcome: 'resumed',
      remaining_count: 1,
    }, 'agent-resumed-1', 'node-1'));
    expect(state.phase).toBe('waiting_for_input');
    expect(state.agentWaiting).toEqual([
      { interruptId: 'interrupt-2', nodeId: 'node-2', reasonKind: 'mcp_approval' },
    ]);

    state = applyTaskEvent(state, event('agent.run.resumed', {
      outcome: 'resumed',
      remaining_count: 0,
    }, 'agent-resumed-2', 'node-2'));
    expect(state.phase).toBe('running');
    expect(state.agentWaiting).toEqual([]);
    expect(state.agentRemainingWaitCount).toBe(0);
  });

  it('deduplicates Agent replay and requests resync on same-id payload conflict', () => {
    const waiting = event('agent.run.waiting', {
      interrupt_id: 'interrupt-1',
      reason_kind: 'skill_input',
      remaining_count: 1,
    }, 'agent-waiting-conflict', 'node-1');
    const once = applyTaskEvent(createInitialTaskEventState(), waiting);
    const duplicate = applyTaskEvent(once, waiting);
    const conflicted = applyTaskEvent(duplicate, {
      ...waiting,
      payload: { ...waiting.payload, remaining_count: 2 },
    });

    expect(duplicate).toBe(once);
    expect(conflicted.agentWaiting).toHaveLength(1);
    expect(conflicted.eventSyncError).toContain('内容发生冲突');
  });

  it('safely ignores Agent audit and tool-result events', () => {
    const initial = createInitialTaskEventState();
    const afterAudit = applyTaskEvent(initial, event('agent.sample.completed', {
      duration_seconds: 1,
      outcome: 'completed',
      sample_id: 'sample-1',
      tool_count: 1,
      usage_status: 'available',
    }, 'agent-audit-1'));
    const afterToolResult = applyTaskEvent(afterAudit, event('agent.tool_result.committed', {
      call_id: 'call-1',
      status: 'completed',
      error_code: null,
      artifact_count: 1,
      result_digest: 'a'.repeat(64),
    }, 'agent-tool-result-1'));

    expect(afterAudit).toBe(initial);
    expect(afterToolResult).toBe(initial);
    expect(afterToolResult.assistantText).toBe('');
  });

  it('groups Agent, memory, and interrupt reasoning in display order', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('memory.reasoning_delta', { delta: '先查历史', ordinal: 1 }, 'memory-reasoning-1'));
    state = applyTaskEvent(state, event('interrupt.reasoning_delta', { delta: '理解补参', ordinal: 1 }, 'interrupt-reasoning-1'));
    state = applyTaskEvent(state, event('agent.reasoning_delta', { delta: '生成回答', ordinal: 1, sample_id: 'sample-answer' }, 'answer-reasoning-1'));

    expect(state.memoryReasoningText).toBe('先查历史');
    expect(state.interruptReasoningText).toBe('理解补参');
    expect(state.answerReasoningText).toBe('生成回答');
    expect(state.reasoningText).toBe(
      '### 记忆思考\n先查历史\n\n### 补参思考\n理解补参\n\n### Agent思考\n生成回答',
    );
  });

  it('stores sanitized capability fallback notices from SSE events', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('capability.missing_fallback', {
      enabled: true,
      scope: 'full',
      reason_code: 'skill_missing',
      missing_capability_summary: '缺少田间图 Skill',
      fallback_content_scope: '只能给出手工建议',
      artifact_generation_allowed: false,
      disclosure_required: true,
      handler: 'must-not-leak',
    }, 'fallback-notice'));

    expect(state.fallbackNotice?.reasonCode).toBe('skill_missing');
    expect(state.fallbackNotice?.missingCapabilitySummary).toBe('缺少田间图 Skill');
    expect(JSON.stringify(state.fallbackNotice)).not.toContain('handler');
  });

  it('parses assistant history fallback metadata and rejects invalid payloads', () => {
    expect(parseCapabilityFallbackNotice({
      capability_missing_fallback: {
        enabled: true,
        scope: 'partial',
        reason_code: 'capability_missing',
        missing_capability_summary: '缺少绘图能力',
        attempted_capability_summary: '已完成数据查询',
        fallback_content_scope: '只补充绘图建议',
        artifact_generation_allowed: false,
        disclosure_required: true,
      },
    })?.scope).toBe('partial');
    expect(parseCapabilityFallbackNotice({ enabled: true, scope: 'full' })).toBeNull();
  });

  it('moves to artifact loading when the task completes', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('task.completed'));
    expect(state.phase).toBe('loading_artifacts');
    expect(state.statusText).toContain('整理结果');
    expect(isTaskActive(state.phase)).toBe(false);
  });

  it('maps missing required skill failures to a concrete user-facing message', () => {
    const state = applyTaskEvent(
      createInitialTaskEventState(),
      event('task.failed', { code: 'required_skill_missing' }, 'missing-skill-failed'),
    );

    expect(state.phase).toBe('failed');
    expect(state.errorMessage).toContain('没有可用 Skill');
  });

  it('marks waiting-input tasks as a resumable clarification state', () => {
    const state = markWaitingInputRequired(createInitialTaskEventState());
    expect(state.phase).toBe('waiting_for_input');
    expect(state.statusText).toContain('等待补充信息');
    expect(state.errorMessage).toContain('下一条回复会继续当前任务');
  });

  it('maps node waiting-for-input events to a resumable clarification state', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event(
      'node.waiting_for_input',
      { capability_id: 'agent.final_output', interrupt_id: 'interrupt-1', reason_code: 'lookup_target_missing' },
      'waiting-event',
      'node-main',
    ));

    expect(state.phase).toBe('waiting_for_input');
    expect(state.statusText).toContain('等待补充信息');
    expect(state.seenEventIds).toEqual(['waiting-event']);
  });

  it('maps cancellation and guard failures to friendly states', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('task.cancellation_requested'));
    expect(state.phase).toBe('cancelling');

    state = applyTaskEvent(state, event('task.cancelled'));
    expect(state.phase).toBe('cancelled');

    state = applyTaskEvent(createInitialTaskEventState(), event('node.failed', { code: 'write_pattern_detected' }, 'guard-node-failed', 'task-1:query_guard'));
    expect(state.phase).toBe('idle');
    expect(state.errorMessage).toContain('只读查询安全边界');

  });

  it('keeps node data access failures non-terminal until a task failure event arrives', () => {
    let state = applyTaskEvent(createInitialTaskEventState(), event('node.failed', { code: 'data_access_deadline_exceeded' }, 'timeout-failed'));
    expect(state.phase).toBe('idle');
    expect(state.errorMessage).toContain('数据库查询超时');

    state = applyTaskEvent(createInitialTaskEventState(), event('node.failed', { code: 'data_access_result_too_large' }, 'too-large-failed'));
    expect(state.phase).toBe('idle');
    expect(state.errorMessage).toContain('查询结果内容过大');
  });

  it('redacts SQL query internal failures to a vague server error', () => {
    const state = applyTaskEvent(
      createInitialTaskEventState(),
      event('node.failed', { capability_id: ['skill', 'sql_query'].join('.'), code: 'db_unknown_column' }, 'sqlquery-failed', 'node-sqlquery'),
    );

    expect(state.errorMessage).toBe('服务器内部错误，请稍后重试。');
  });

  it('keeps SQL query guard blocks as safety-boundary guidance', () => {
    const state = applyTaskEvent(
      createInitialTaskEventState(),
      event('node.failed', { capability_id: ['skill', 'sql_query'].join('.'), code: 'write_pattern_detected' }, 'sqlquery-guard-failed', 'node-sqlquery'),
    );

    expect(state.errorMessage).toContain('只读查询安全边界');
  });


  it('keeps SQL Guard blocked message when a later task.failed event arrives', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.failed', { code: 'write_pattern_detected' }, 'guard-node-failed', 'task-1:query_guard'));
    state = applyTaskEvent(state, event('task.failed', {}, 'task-failed'));
    expect(state.phase).toBe('failed');
    expect(state.errorMessage).toContain('只读查询安全边界');
  });

  it('ignores audit/debug events that should not be visible by default', () => {
    const initial = createInitialTaskEventState();
    const state = applyTaskEvent(initial, event('agent.internal_debug', { provider: 'hidden' }));
    expect(state).toEqual(initial);
  });

  it('tracks discovery and fair-queue state using safe display fields only', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('mcp.server_routed', {
      server_display_name: '育种数据',
      endpoint_url: 'https://must-not-leak.test',
    }, 'mcp-routed'));
    state = applyTaskEvent(state, event('mcp.discovery_completed', {
      server_display_name: '育种数据',
      available_tool_count: 3,
      full_tool_catalog: [{ name: 'must-not-leak' }],
    }, 'mcp-discovered'));
    state = applyTaskEvent(state, event('mcp.queue_entered', {
      server_display_name: '育种数据',
      queue_position: 2,
      credential: 'must-not-leak',
    }, 'mcp-queued'));

    expect(state.mcp.serverDisplayName).toBe('育种数据');
    expect(state.mcp.discovery).toMatchObject({ status: 'completed', availableToolCount: 3 });
    expect(state.mcp.queue).toMatchObject({ queued: true, position: 2 });
    expect(JSON.stringify(state.mcp)).not.toContain('must-not-leak');
    expect(JSON.stringify(state.mcp)).not.toContain('endpoint_url');
  });

  it('merges long-running MCP call updates by safe_call_ref', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('mcp.tool_call_started', {
      safe_call_ref: 'call-safe-1',
      server_display_name: '育种数据',
      tool_display_name: '查询品系',
      raw_jsonrpc_id: 'must-not-leak',
      arguments: { password: 'must-not-leak' },
    }, 'mcp-call-start'));
    state = applyTaskEvent(state, event('mcp.tool_call_still_running', {
      safe_call_ref: 'call-safe-1',
      elapsed_seconds: 120,
      next_prompt_after_seconds: 120,
    }, 'mcp-call-running'));

    expect(state.mcp.calls).toHaveLength(1);
    expect(state.mcp.calls[0]).toMatchObject({
      safeCallRef: 'call-safe-1',
      toolDisplayName: '查询品系',
      status: 'still_running',
      elapsedSeconds: 120,
    });
    expect(JSON.stringify(state.mcp.calls)).not.toContain('must-not-leak');

    state = applyTaskEvent(state, terminalProjectionEvents()[0]);
    expect(state.mcp.calls).toHaveLength(1);
    expect(state.mcp.calls[0].status).toBe('still_running');
    expect(state.mcp.executionUnknown).toMatchObject({ callId: 'call-1', noReplay: true });
    expect(state.errorMessage).toContain('不会自动重复调用');
  });

  it('tracks approval, elicitation, and remote task state without protocol identifiers', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('mcp.tool_approval_required', {
      interrupt_id: 'interrupt-1',
      safe_call_ref: 'call-safe-1',
      server_display_name: '育种数据',
      tool_display_name: '查询品系',
      input_schema: { secret: true },
    }, 'mcp-approval'));
    expect(state.mcp.approval).toMatchObject({ pending: true, interruptId: 'interrupt-1', toolDisplayName: '查询品系' });
    expect(state.phase).toBe('waiting_for_input');

    state = applyTaskEvent(state, event('mcp.input_required', {
      interrupt_id: 'interrupt-2',
      safe_call_ref: 'call-safe-1',
      question: '请选择试验年份',
      field_names: ['year'],
      requestState: 'must-not-leak',
    }, 'mcp-input'));
    state = applyTaskEvent(state, event('mcp.remote_task_status_changed', {
      safe_task_ref: 'task-safe-1',
      status: 'working',
      tool_display_name: '查询品系',
      remote_task_id: 'must-not-leak',
    }, 'mcp-remote-task'));

    expect(state.mcp.input).toMatchObject({ pending: true, fieldNames: ['year'] });
    expect(state.mcp.remoteTask).toMatchObject({ safeTaskRef: 'task-safe-1', status: 'working' });
    expect(JSON.stringify(state.mcp)).not.toContain('must-not-leak');
  });

  it.each([
    ['task.completed', {}],
    ['task.failed', { code: 'failed' }],
    ['task.cancelled', {}],
    ['agent.run.completed', { compaction_count: 0, duration_seconds: 0, outcome: 'completed', sample_count: 1, tool_call_count: 0 }],
    ['agent.run.failed', { compaction_count: 0, duration_seconds: 0, outcome: 'failed', sample_count: 1, tool_call_count: 0 }],
    ['agent.run.cancelled', { compaction_count: 0, duration_seconds: 0, outcome: 'cancelled', sample_count: 1, tool_call_count: 0 }],
  ])('clears MCP approval when %s is consumed', (eventType, payload) => {
    let state = applyTaskEvent(createInitialTaskEventState(), event('mcp.tool_approval_required', {
      interrupt_id: 'approval-terminal-clear',
      safe_call_ref: 'a'.repeat(64),
      server_display_name: 'OCR服务',
      tool_display_name: 'start_parse_job',
    }, 'approval-before-terminal'));

    state = applyTaskEvent(state, event(eventType, payload, `terminal-${eventType}`));

    expect(state.mcp.approval).toBeNull();
  });

  it('clears MCP approval in imperative completed and failed convergence helpers', () => {
    const waiting = applyTaskEvent(createInitialTaskEventState(), event('mcp.tool_approval_required', {
      interrupt_id: 'approval-helper-clear',
      safe_call_ref: 'b'.repeat(64),
      server_display_name: 'OCR服务',
      tool_display_name: 'start_parse_job',
    }, 'approval-before-helper'));

    expect(markTaskCompleted(waiting).mcp.approval).toBeNull();
    expect(markTaskFailed(waiting, 'failed').mcp.approval).toBeNull();
  });

  it('shows a recoverable unavailable state without promising cross-path fallback', () => {
    const state = applyTaskEvent(
      createInitialTaskEventState(),
      event('mcp.runtime_unavailable', {
        status: 'unavailable',
        reason_code: 'no_user_scoped_server',
      }, 'mcp-no-server:v1:task-1:01-runtime-unavailable'),
    );

    expect(state.mcp.availability).toEqual({
      status: 'unavailable',
      reasonCode: 'no_user_scoped_server',
    });
    expect(state.errorMessage).toContain('不会改道或重放');
  });

  it('buffers predecessor gaps, deduplicates replay, and keeps late results separate from the failed task', () => {
    const [unknown, failed, resolution, correction] = terminalProjectionEvents();
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, correction);
    state = applyTaskEvent(state, resolution);
    expect(state.pendingEvents.map((item) => item.event_id)).toEqual([correctionEventId, resolutionEventId]);
    expect(state.mcp.lateResult).toBeNull();

    state = applyTaskEvent(state, unknown);
    state = applyTaskEvent(state, failed);
    expect(state.pendingEvents).toEqual([]);
    expect(state.seenEventIds).toEqual([unknownEventId, failedEventId, resolutionEventId, correctionEventId]);
    expect(state.phase).toBe('failed');
    expect(state.mcp.lateResult).toMatchObject({
      terminalState: 'completed',
      safeResultRef: 'artifact:safe-result',
      taskRemainsFailed: true,
      noReplay: true,
    });

    const duplicate = applyTaskEvent(state, correction);
    expect(duplicate).toBe(state);
  });

  it('flags same event_id payload conflicts as recoverable sync errors', () => {
    const [unknown] = terminalProjectionEvents();
    const conflict = { ...unknown, payload: { ...unknown.payload, intent_id: 'intent-conflict' } };
    let state = applyTaskEvent(createInitialTaskEventState(), unknown);
    state = applyTaskEvent(state, conflict);
    expect(state.eventSyncError).toContain('冲突');
  });

  it('rejects a closed resolution whose cross-event identity binding disagrees with the consumed unknown event', () => {
    const [unknown, failed, resolution] = terminalProjectionEvents();
    const mismatched = { ...resolution, payload: { ...resolution.payload, intent_id: 'intent-other' } };
    let state = applyTaskEvent(createInitialTaskEventState(), unknown);
    state = applyTaskEvent(state, failed);
    state = applyTaskEvent(state, mismatched);

    expect(state.phase).toBe('failed');
    expect(state.mcp.executionResolution).toBeNull();
    expect(state.mcp.lateResult).toBeNull();
    expect(state.eventSyncError).toContain('绑定不一致');
  });

  it('accepts only the deterministic no-server failure after its unavailable predecessor', () => {
    let state = applyTaskEvent(createInitialTaskEventState(), event(
      'mcp.runtime_unavailable',
      { status: 'unavailable', reason_code: 'no_user_scoped_server' },
      'mcp-no-server:v1:task-1:01-runtime-unavailable',
    ));
    state = applyTaskEvent(state, event(
      'task.failed',
      { code: 'mcp_runtime_unavailable' },
      'mcp-no-server:v1:task-1:02-task-failed',
    ));

    expect(state.phase).toBe('failed');
    expect(state.seenEventIds).toEqual([
      'mcp-no-server:v1:task-1:01-runtime-unavailable',
      'mcp-no-server:v1:task-1:02-task-failed',
    ]);
  });

  it('rejects malformed closed events and unknown fields without changing task phase', () => {
    const malformed = terminalProjectionEvents()[0];
    const state = applyTaskEvent(createInitialTaskEventState(), {
      ...malformed,
      payload: { ...malformed.payload, unexpected: true },
    });
    expect(state.phase).toBe('idle');
    expect(state.seenEventIds).toEqual([]);
    expect(state.eventSyncError).toContain('格式不完整');
  });
});
