import { Button } from 'antd';
import type { PendingInterrupt } from '../domain/interrupts';
import { MarkdownText } from './MarkdownText';

export function InterruptQuestionText({ interrupt }: { interrupt: PendingInterrupt }) {
  return <MarkdownText content={interrupt.question} />;
}

export function InterruptComposerStatus({ onCancel, cancelling }: { onCancel: () => void; cancelling: boolean }) {
  return (
    <div className="interrupt-composer-status" role="status" aria-live="polite">
      <span className="interrupt-composer-status-text">等待补充 · 下一条消息将继续当前任务</span>
      <Button danger type="text" size="small" aria-label="结束任务" onClick={onCancel} loading={cancelling}>
        结束任务
      </Button>
    </div>
  );
}
