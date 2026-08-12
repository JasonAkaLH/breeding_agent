import { Button, Modal, Space, Typography } from 'antd';
import type { MCPApprovalDecision, MCPApprovalState } from '../domain/taskEvents';

interface Props {
  approval: MCPApprovalState | null;
  submitting?: boolean;
  onDecision(decision: MCPApprovalDecision): void | Promise<void>;
}

export function MCPApprovalDialog({ approval, submitting = false, onDecision }: Props) {
  const open = Boolean(approval?.pending);
  const toolName = approval?.toolDisplayName || '该 MCP 工具';
  const serverName = approval?.serverDisplayName || '当前 MCP 服务';

  return (
    <Modal
      open={open}
      title="MCP 工具授权"
      aria-describedby="mcp-approval-description"
      closable={false}
      maskClosable={false}
      keyboard={false}
      footer={null}
      focusTriggerAfterClose
    >
      <Typography.Paragraph id="mcp-approval-description">
        {serverName} 请求调用“{toolName}”。请确认本次任务是否允许执行。
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary">
        “始终允许”只适用于当前工具及当前安全版本；工具结构或安全配置变化后会自动失效。
      </Typography.Paragraph>
      <Space wrap>
        <Button aria-label="仅允许一次" autoFocus loading={submitting} onClick={() => void onDecision('allow_once')}>
          仅允许一次
        </Button>
        <Button aria-label="始终允许" type="primary" loading={submitting} onClick={() => void onDecision('always_allow')}>
          始终允许
        </Button>
        <Button aria-label="拒绝" danger disabled={submitting} onClick={() => void onDecision('deny')}>
          拒绝
        </Button>
      </Space>
    </Modal>
  );
}
