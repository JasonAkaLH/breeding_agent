import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MCPRuntimeStatus } from './MCPRuntimeStatus';
import type { MCPTaskState } from '../domain/taskEvents';

describe('MCPRuntimeStatus', () => {
  it('updates one safe call card and exposes long-call controls', () => {
    const onContinue = vi.fn();
    const onCancel = vi.fn();
    const mcp: MCPTaskState = {
      serverDisplayName: '育种数据',
      discovery: { status: 'completed', serverDisplayName: '育种数据', availableToolCount: 3, retried: false, errorCode: null },
      queue: { queued: false, position: null, serverDisplayName: '育种数据' },
      approval: null,
      calls: [{
        safeCallRef: 'call-safe-1',
        serverDisplayName: '育种数据',
        toolDisplayName: '查询品系',
        status: 'still_running',
        elapsedSeconds: 120,
        nextPromptAfterSeconds: 120,
        errorCode: null,
      }],
      input: null,
      remoteTask: null,
      availability: null,
      executionUnknown: null,
      executionResolution: null,
      lateResult: null,
    };

    render(<MCPRuntimeStatus taskId="task-1" mcp={mcp} onContinue={onContinue} onCancel={onCancel} />);

    expect(screen.getByRole('region', { name: 'MCP 运行状态' })).toBeInTheDocument();
    expect(screen.getAllByText('查询品系')).toHaveLength(1);
    expect(screen.getByText('已运行 120 秒')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '继续等待' }));
    fireEvent.click(screen.getByRole('button', { name: '停止当前工具' }));
    expect(onContinue).toHaveBeenCalledWith('task-1', 'call-safe-1');
    expect(onCancel).toHaveBeenCalledWith('task-1', 'call-safe-1');
  });

  it('explains unavailable rollout routing without implying fallback or retry', () => {
    const mcp: MCPTaskState = {
      serverDisplayName: null,
      discovery: null,
      queue: null,
      approval: null,
      calls: [],
      input: null,
      remoteTask: null,
      availability: { status: 'unavailable', reasonCode: 'no_user_scoped_server' },
      executionUnknown: null,
      executionResolution: null,
      lateResult: null,
    };

    render(<MCPRuntimeStatus taskId="task-unavailable" mcp={mcp} />);

    expect(screen.getAllByText('当前任务的 MCP 暂不可用')).toHaveLength(2);
    expect(screen.getByText(/不会切换到另一条 MCP 链路或自动重试/)).toBeInTheDocument();
  });

  it('announces unknown execution ahead of stale active-call text, disables replay controls, and exposes resync', () => {
    const onContinue = vi.fn();
    const onCancel = vi.fn();
    const onResync = vi.fn();
    const mcp: MCPTaskState = {
      serverDisplayName: '育种数据', discovery: null, queue: null, approval: null,
      calls: [{ safeCallRef: 'safe-1', serverDisplayName: '育种数据', toolDisplayName: '查询品系', status: 'still_running', elapsedSeconds: 120, nextPromptAfterSeconds: 120, errorCode: null }],
      input: null, remoteTask: null, availability: null,
      executionUnknown: { projectionId: 'projection-1', intentId: 'intent-1', callId: 'call-1', reasonCode: 'trusted_terminal_result_absent', noReplay: true, nodeId: 'node-1', projectionRevision: 0, intentRevision: 4, unknownEventId: 'unknown-1', taskFailedEventId: 'failed-1', unknownTerminalAt: '2026-04-27T00:00:00Z', createdAt: '2026-04-27T00:00:00Z' },
      executionResolution: null, lateResult: null,
    };

    render(<MCPRuntimeStatus taskId="task-1" mcp={mcp} syncError="历史缺少前序记录" onContinue={onContinue} onCancel={onCancel} onResync={onResync} />);

    expect(screen.getByRole('status')).toHaveTextContent('MCP 工具执行结果无法确认，任务不会自动重放');
    expect(screen.getByRole('button', { name: '继续等待' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '停止当前工具' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '重新同步' }));
    expect(onResync).toHaveBeenCalledWith('task-1');
  });
});
