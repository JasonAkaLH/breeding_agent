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
});
