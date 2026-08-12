import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MCPApprovalDialog } from './MCPApprovalDialog';

describe('MCPApprovalDialog', () => {
  it('offers exactly the three authorization decisions without showing call arguments', () => {
    const onDecision = vi.fn();
    render(<MCPApprovalDialog approval={{
      interruptId: 'interrupt-1',
      safeCallRef: 'call-safe-1',
      serverDisplayName: '育种数据',
      toolDisplayName: '查询品系',
      decision: null,
      pending: true,
    }} onDecision={onDecision} />);

    expect(screen.getByRole('dialog', { name: 'MCP 工具授权' })).toBeInTheDocument();
    expect(screen.getByText(/育种数据.*查询品系/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '仅允许一次' }));
    fireEvent.click(screen.getByRole('button', { name: '始终允许' }));
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }));

    expect(onDecision.mock.calls.map(([decision]) => decision)).toEqual(['allow_once', 'always_allow', 'deny']);
  });
});
