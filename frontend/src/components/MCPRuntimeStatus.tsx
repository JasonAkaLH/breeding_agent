import { Alert, Button, Card, List, Space, Tag, Typography } from 'antd';
import type { MCPCallState, MCPTaskState } from '../domain/taskEvents';

interface Props {
  taskId: string;
  mcp: MCPTaskState;
  busyCallRef?: string | null;
  onContinue?(taskId: string, safeCallRef: string): void | Promise<void>;
  onCancel?(taskId: string, safeCallRef: string): void | Promise<void>;
}

export function MCPRuntimeStatus({ taskId, mcp, busyCallRef = null, onContinue, onCancel }: Props) {
  const visible = Boolean(
    mcp.serverDisplayName
    || mcp.discovery
    || mcp.queue
    || mcp.approval
    || mcp.calls.length
    || mcp.input
    || mcp.remoteTask,
  );
  if (!visible) return null;

  return (
    <Card size="small" title="MCP 运行状态" role="region" aria-label="MCP 运行状态">
      <div role="status" aria-live="polite" aria-atomic="true">
        {runtimeAnnouncement(mcp)}
      </div>
      {mcp.discovery ? (
        <Typography.Paragraph>
          工具发现：<Tag color={mcp.discovery.status === 'failed' ? 'red' : mcp.discovery.status === 'completed' ? 'green' : 'blue'}>
            {discoveryLabel(mcp.discovery.status)}
          </Tag>
          {mcp.discovery.availableToolCount !== null ? `可用工具 ${mcp.discovery.availableToolCount} 个` : null}
        </Typography.Paragraph>
      ) : null}
      {mcp.queue?.queued ? (
        <Alert
          type="info"
          showIcon
          message={mcp.queue.position === null ? '正在等待 MCP 执行资源' : `正在排队，当前位置 ${mcp.queue.position}`}
        />
      ) : null}
      {mcp.input?.pending ? (
        <Alert type="warning" showIcon message="工具等待补充信息" description={mcp.input.question || '请在输入框补充所需信息。'} />
      ) : null}
      {mcp.remoteTask ? (
        <Typography.Paragraph>
          远程任务：<Tag>{mcp.remoteTask.status}</Tag>
        </Typography.Paragraph>
      ) : null}
      <List
        size="small"
        dataSource={mcp.calls}
        locale={{ emptyText: '尚未开始工具调用' }}
        renderItem={(call) => (
          <List.Item actions={callActions(taskId, call, busyCallRef, onContinue, onCancel)}>
            <List.Item.Meta
              title={call.toolDisplayName || 'MCP 工具'}
              description={<CallDescription call={call} />}
            />
          </List.Item>
        )}
      />
    </Card>
  );
}

function CallDescription({ call }: { call: MCPCallState }) {
  return (
    <Space wrap>
      <Tag color={callColor(call.status)}>{callStatusLabel(call.status)}</Tag>
      {call.serverDisplayName ? <Typography.Text type="secondary">{call.serverDisplayName}</Typography.Text> : null}
      {call.elapsedSeconds !== null ? <Typography.Text type="secondary">已运行 {call.elapsedSeconds} 秒</Typography.Text> : null}
    </Space>
  );
}

function callActions(
  taskId: string,
  call: MCPCallState,
  busyCallRef: string | null,
  onContinue: Props['onContinue'],
  onCancel: Props['onCancel'],
) {
  if (call.status !== 'still_running') return [];
  const busy = busyCallRef === call.safeCallRef;
  return [
    <Button key="continue" size="small" loading={busy} disabled={!onContinue} onClick={() => void onContinue?.(taskId, call.safeCallRef)}>
      继续等待
    </Button>,
    <Button key="cancel" size="small" danger disabled={busy || !onCancel} onClick={() => void onCancel?.(taskId, call.safeCallRef)}>
      停止当前工具
    </Button>,
  ];
}

function runtimeAnnouncement(mcp: MCPTaskState): string {
  const active = [...mcp.calls].reverse().find((call) => call.status === 'running' || call.status === 'still_running');
  if (mcp.input?.pending) return 'MCP 工具等待补充信息';
  if (mcp.approval?.pending) return 'MCP 工具等待授权';
  if (mcp.queue?.queued) return mcp.queue.position === null ? 'MCP 调用正在排队' : `MCP 调用排队位置 ${mcp.queue.position}`;
  if (active) return `${active.toolDisplayName || 'MCP 工具'}${callStatusLabel(active.status)}`;
  if (mcp.remoteTask) return `远程 MCP 任务状态 ${mcp.remoteTask.status}`;
  return mcp.discovery ? `MCP 工具发现${discoveryLabel(mcp.discovery.status)}` : 'MCP 已就绪';
}

function discoveryLabel(status: NonNullable<MCPTaskState['discovery']>['status']): string {
  return status === 'started' ? '进行中' : status === 'completed' ? '已完成' : '失败';
}

function callStatusLabel(status: MCPCallState['status']): string {
  if (status === 'running') return '运行中';
  if (status === 'still_running') return '仍在运行';
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已取消';
  return '结果无法确认';
}

function callColor(status: MCPCallState['status']): string {
  if (status === 'completed') return 'green';
  if (status === 'failed' || status === 'unknown') return 'red';
  if (status === 'cancelled') return 'default';
  return 'blue';
}
