import { Button, Tag, Typography } from 'antd';
import type { UploadFileResponse } from '../api/types';
import {
  draftAttachmentStatusText,
  draftAttachmentTypeLabel,
  formatFileSize,
  uploadFileSummaryParts,
  type DraftAttachment,
} from '../domain/attachments';

export function DraftAttachmentCard({
  attachment,
  disabled,
  onDelete,
}: {
  attachment: DraftAttachment;
  disabled: boolean;
  onDelete: () => void;
}) {
  const summaryParts = [draftAttachmentTypeLabel(attachment), formatFileSize(attachment.sizeBytes)];
  if (attachment.errorMessage) summaryParts.push(attachment.errorMessage);
  const tagColor = attachment.status === 'failed' ? 'red' : attachment.status === 'uploading' ? 'blue' : 'orange';

  return (
    <div className="conversation-file-card">
      <div className="conversation-file-card-header">
        <div className="conversation-file-card-title">
          <Typography.Text strong ellipsis={{ tooltip: attachment.filename }} className="conversation-file-name">
            {attachment.filename}
          </Typography.Text>
          <Typography.Text type={attachment.status === 'failed' ? 'danger' : 'secondary'} className="conversation-file-meta">
            {summaryParts.join(' · ')}
          </Typography.Text>
        </div>
        <Tag color={tagColor} className="conversation-file-ready-tag">{draftAttachmentStatusText(attachment.status)}</Tag>
      </div>
      <div className="conversation-file-actions">
        <Typography.Text type="secondary" className="conversation-file-size">
          {formatFileSize(attachment.sizeBytes)}
        </Typography.Text>
        <Button
          danger
          type="text"
          size="small"
          disabled={disabled || attachment.status === 'uploading'}
          aria-label={`删除文件 ${attachment.filename}`}
          onClick={onDelete}
        >
          删除
        </Button>
      </div>
    </div>
  );
}

export function ConversationFileCard({
  upload,
  deleting,
  disabled,
  onDelete,
}: {
  upload: UploadFileResponse;
  deleting: boolean;
  disabled: boolean;
  onDelete: () => void;
}) {
  const summaryParts = uploadFileSummaryParts(upload);
  const columns = upload.preview.columns ?? [];

  return (
    <div className="conversation-file-card">
      <div className="conversation-file-card-header">
        <div className="conversation-file-card-title">
          <Typography.Text strong ellipsis={{ tooltip: upload.filename }} className="conversation-file-name">
            {upload.filename}
          </Typography.Text>
          <Typography.Text type="secondary" className="conversation-file-meta">
            {summaryParts.join(' · ')}
          </Typography.Text>
        </div>
        <Tag color="green" className="conversation-file-ready-tag">Skill 可用</Tag>
      </div>
      {columns.length > 3 ? (
        <Typography.Text type="secondary" className="conversation-file-extra">
          另有 {columns.length - 3} 个字段可在 Skill 中读取
        </Typography.Text>
      ) : null}
      <div className="conversation-file-actions">
        <Typography.Text type="secondary" className="conversation-file-size">
          {formatFileSize(upload.size_bytes)}
        </Typography.Text>
        <Button
          danger
          type="text"
          size="small"
          loading={deleting}
          disabled={disabled || deleting}
          aria-label={`删除文件 ${upload.filename}`}
          onClick={onDelete}
        >
          删除
        </Button>
      </div>
    </div>
  );
}
