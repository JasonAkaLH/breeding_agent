import { describe, expect, it } from 'vitest';
import { applyTaskEvent, createInitialTaskEventState } from './taskEvents';
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
  it('maps accepted and running events to business states', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('task.accepted'));
    expect(state.phase).toBe('accepted');
    expect(state.statusText).toContain('已提交');

    state = applyTaskEvent(state, event('node.started', { capability_id: 'sql_query.sql_execute_readonly' }, 'node-1'));
    expect(state.phase).toBe('running');
    expect(state.statusText).toContain('检索数据库');
  });

  it('appends main-agent deltas once by event id', () => {
    let state = createInitialTaskEventState();
    const delta = event('main_agent.output_delta', { delta: '你好', ordinal: 1 }, 'evt-delta-1');
    state = applyTaskEvent(state, delta);
    state = applyTaskEvent(state, delta);
    expect(state.phase).toBe('streaming');
    expect(state.assistantText).toBe('你好');
  });

  it('moves to artifact loading when the task completes', () => {
    const state = applyTaskEvent(createInitialTaskEventState(), event('task.completed'));
    expect(state.phase).toBe('loading_artifacts');
    expect(state.statusText).toContain('整理结果');
  });

  it('maps cancellation and guard failures to friendly states', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('task.cancellation_requested'));
    expect(state.phase).toBe('cancelling');

    state = applyTaskEvent(state, event('task.cancelled'));
    expect(state.phase).toBe('cancelled');

    state = applyTaskEvent(createInitialTaskEventState(), event('node.failed', { code: 'write_pattern_detected' }, 'guard-node-failed', 'task-1:sql_guard'));
    expect(state.phase).toBe('failed');
    expect(state.errorMessage).toContain('只读查询安全边界');

    state = applyTaskEvent(createInitialTaskEventState(), event('sql_query.sql_guard_blocked'));
    expect(state.phase).toBe('failed');
    expect(state.errorMessage).toContain('只读查询安全边界');
  });


  it('keeps SQL Guard blocked message when a later task.failed event arrives', () => {
    let state = createInitialTaskEventState();
    state = applyTaskEvent(state, event('node.failed', { code: 'write_pattern_detected' }, 'guard-node-failed', 'task-1:sql_guard'));
    state = applyTaskEvent(state, event('task.failed', {}, 'task-failed'));
    expect(state.phase).toBe('failed');
    expect(state.errorMessage).toContain('只读查询安全边界');
  });

  it('ignores audit/debug events that should not be visible by default', () => {
    const initial = createInitialTaskEventState();
    const state = applyTaskEvent(initial, event('main_agent.llm_call', { provider: 'hidden' }));
    expect(state).toEqual(initial);
  });
});
