import { describe, expect, it } from 'vitest';
import { applyTaskEvent, createInitialTaskEventState, createRestoringTaskState, isTaskActive, markWaitingInputRequired, parseCapabilityFallbackNotice, taskProgressDisplayText } from './taskEvents';
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

describe('applyTaskEvent', () => {
  it('creates a restoring task state for refresh recovery', () => {
    const state = createRestoringTaskState();

    expect(state.phase).toBe('running');
    expect(state.statusText).toContain('恢复任务状态');
    expect(state.currentActivityText).toContain('同步任务输出');
    expect(state.assistantText).toBe('');
    expect(state.reasoningText).toBe('');
    expect(state.seenEventIds).toEqual([]);
  });

  it('replays visible output, reasoning, and skill progress from restoring state', () => {
    let state = createRestoringTaskState();
    state = applyTaskEvent(state, event('main_agent.output_delta', { delta: '已生成内容', response_role: 'final' }, 'restore-output'));
    state = applyTaskEvent(state, event('main_agent.reasoning_delta', { delta: '先分析' }, 'restore-reasoning'));
    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.example', skill_name: 'ExampleSkill', label: '正在处理文件' }, 'restore-skill', 'node-skill'));

    expect(state.assistantText).toBe('已生成内容');
    expect(state.reasoningText).toBe('先分析');
    expect(state.skillStatuses).toEqual([
      expect.objectContaining({ capabilityId: 'skill.example', label: 'ExampleSkill', statusText: '正在处理文件', status: 'running' }),
    ]);
  });

  it('keeps intermediate main-agent deltas hidden during restore replay', () => {
    const state = applyTaskEvent(createRestoringTaskState(), event('main_agent.output_delta', { delta: '中间结果', response_role: 'intermediate' }, 'restore-intermediate'));

    expect(state.assistantText).toBe('');
    expect(state.seenEventIds).toEqual(['restore-intermediate']);
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

  it('marks node cancellation, blocked, and orphaned events on matching skill rows', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.data_query', skill_name: 'data-query' }, 'sql-start', 'node-sql'));
    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.rcbd', skill_name: 'RCBD' }, 'rcbd-start', 'node-rcbd'));
    state = applyTaskEvent(state, event('node.cancelled', { capability_id: 'skill.data_query' }, 'sql-cancelled', 'node-sql'));
    state = applyTaskEvent(state, event('node.blocked_by_cancellation', { capability_id: 'skill.rcbd' }, 'rcbd-blocked', 'node-rcbd'));
    state = applyTaskEvent(state, event('node.orphaned', { capability_id: 'skill.report', skill_name: 'ReportSkill' }, 'report-orphaned', 'node-report'));

    expect(state.phase).toBe('running');
    expect(state.skillStatuses).toEqual([
      expect.objectContaining({ key: 'node-sql', label: 'data-query', status: 'cancelled', statusText: '已取消' }),
      expect.objectContaining({ key: 'node-rcbd', label: 'RCBD', status: 'blocked', statusText: '已被取消阻断' }),
      expect.objectContaining({ key: 'node-report', label: 'ReportSkill', status: 'blocked', statusText: '已被重规划跳过' }),
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

  it('does not create skill rows for main-agent nodes and uses fallback keys when node ids are missing', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.started', { capability_id: 'main_agent.respond' }, 'main-start', 'node-main'));
    expect(state.skillStatuses).toEqual([]);

    state = applyTaskEvent(state, event('node.started', { capability_id: 'skill.report', skill_name: 'ReportSkill' }, 'fallback-start'));
    state = applyTaskEvent(state, event('skill.progress', { capability_id: 'skill.report', skill_name: 'ReportSkill', label: '正在整理报告' }, 'fallback-progress'));

    expect(state.skillStatuses).toHaveLength(1);
    expect(state.skillStatuses[0]).toEqual(expect.objectContaining({ key: 'skill.report::ReportSkill', nodeId: null, label: 'ReportSkill', statusText: '正在整理报告' }));
  });

  it('appends main-agent deltas once by event id', () => {
    let state = createInitialTaskEventState();
    const delta = event('main_agent.output_delta', { delta: '你好', ordinal: 1 }, 'evt-delta-1');
    state = applyTaskEvent(state, delta);
    state = applyTaskEvent(state, delta);
    expect(state.phase).toBe('streaming');
    expect(state.assistantText).toBe('你好');
  });

  it('only appends final or legacy main-agent response events to the visible assistant text', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('main_agent.output_delta', { delta: '中间回答', ordinal: 1, response_role: 'intermediate' }, 'intermediate-delta'));
    state = applyTaskEvent(state, event('main_agent.output_final', { response_role: 'intermediate' }, 'intermediate-final'));

    expect(state.phase).toBe('idle');
    expect(state.assistantText).toBe('');
    expect(state.seenEventIds).toEqual(['intermediate-delta', 'intermediate-final']);

    state = applyTaskEvent(state, event('main_agent.output_delta', { delta: '最终回答', ordinal: 1, response_role: 'final' }, 'final-delta'));
    state = applyTaskEvent(state, event('main_agent.output_delta', { delta: '，兼容旧事件', ordinal: 2 }, 'legacy-delta'));

    expect(state.phase).toBe('streaming');
    expect(state.assistantText).toBe('最终回答，兼容旧事件');
  });

  it('appends main-agent reasoning deltas separately once by event id', () => {
    let state = createInitialTaskEventState();
    const delta = event('main_agent.reasoning_delta', { delta: '先分析', ordinal: 1 }, 'evt-reasoning-1');
    state = applyTaskEvent(state, delta);
    state = applyTaskEvent(state, delta);
    expect(state.phase).toBe('streaming');
    expect(state.reasoningText).toBe('先分析');
    expect(state.answerReasoningText).toBe('先分析');
    expect(state.memoryReasoningText).toBe('');
    expect(state.plannerReasoningText).toBe('');
    expect(state.interruptReasoningText).toBe('');
    expect(state.skillReasoningText).toBe('');
    expect(state.assistantText).toBe('');
  });

  it('groups all streaming reasoning buckets in display order', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('memory.reasoning_delta', { delta: '先查历史', ordinal: 1 }, 'memory-reasoning-1'));
    state = applyTaskEvent(state, event('planner.reasoning_delta', { delta: '再定计划', ordinal: 1 }, 'planner-reasoning-1'));
    state = applyTaskEvent(state, event('interrupt.reasoning_delta', { delta: '理解补参', ordinal: 1 }, 'interrupt-reasoning-1'));
    state = applyTaskEvent(state, event('soft_skill.reasoning_delta', { delta: '判断Skill', ordinal: 1 }, 'soft-skill-reasoning-1'));
    state = applyTaskEvent(state, event('main_agent.reasoning_delta', { delta: '生成回答', ordinal: 1 }, 'answer-reasoning-1'));

    expect(state.memoryReasoningText).toBe('先查历史');
    expect(state.plannerReasoningText).toBe('再定计划');
    expect(state.interruptReasoningText).toBe('理解补参');
    expect(state.skillReasoningText).toBe('判断Skill');
    expect(state.answerReasoningText).toBe('生成回答');
    expect(state.reasoningText).toBe(
      '### 记忆思考\n先查历史\n\n### 规划思考\n再定计划\n\n### 补参思考\n理解补参\n\n### Skill思考\n判断Skill\n\n### 回答思考\n生成回答',
    );
  });

  it('labels planner reasoning separately from final-answer reasoning', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('planner.reasoning_delta', { delta: '先规划', ordinal: 1 }, 'planner-reasoning-1'));

    expect(state.phase).toBe('streaming');
    expect(state.statusText).toContain('规划');
    expect(state.plannerReasoningText).toBe('先规划');
    expect(state.reasoningText).toBe('### 规划思考\n先规划');

    state = applyTaskEvent(state, event('main_agent.reasoning_delta', { delta: '再回答', ordinal: 1 }, 'answer-reasoning-1'));

    expect(state.answerReasoningText).toBe('再回答');
    expect(state.reasoningText).toBe('### 规划思考\n先规划\n\n### 回答思考\n再回答');
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
      { capability_id: 'main_agent.respond', interrupt_id: 'interrupt-1', reason_code: 'lookup_target_missing' },
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
    const state = applyTaskEvent(initial, event('main_agent.llm_call', { provider: 'hidden' }));
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

    state = applyTaskEvent(state, event('mcp.execution_status_unknown', {
      safe_call_ref: 'call-safe-1',
      code: 'mcp_execution_status_unknown',
    }, 'mcp-call-unknown'));
    expect(state.mcp.calls).toHaveLength(1);
    expect(state.mcp.calls[0].status).toBe('unknown');
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
});
