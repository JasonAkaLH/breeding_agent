import type { UploadFileResponse } from '../api/types';

export type DraftAttachmentStatus = 'draft' | 'uploading' | 'failed';

export interface DraftAttachment {
  localId: string;
  file: File;
  filename: string;
  contentType: string;
  sizeBytes: number;
  status: DraftAttachmentStatus;
  errorMessage?: string;
}

export interface UploadedDraftAttachment {
  draft: DraftAttachment;
  upload: UploadFileResponse;
}

export function uploadAnswerDisplayText(uploads: UploadFileResponse[]): string {
  if (uploads.length === 0) return '';
  return `已上传文件：${uploads.map((upload) => upload.filename).join('、')}`;
}

export function isTsvContentType(contentType: string | null | undefined): boolean {
  const normalized = (contentType || '').split(';', 1)[0].trim().toLowerCase();
  return normalized === 'text/tab-separated-values' || normalized === 'text/tsv';
}

export function uploadFileTypeLabel(upload: UploadFileResponse): string {
  if (upload.filename.toLowerCase().endsWith('.tsv') || isTsvContentType(upload.content_type)) return 'TSV';
  switch (upload.file_type) {
    case 'spreadsheet':
      return 'Excel';
    case 'text':
      return 'TXT';
    case 'vcf':
      return 'VCF';
    case 'image':
      return '图片';
    case 'pdf':
      return 'PDF';
    case 'json':
      return 'JSON';
    case 'csv':
      return 'CSV';
    default:
      return upload.file_type || '文件';
  }
}

export function uploadFileSummaryParts(upload: UploadFileResponse): string[] {
  const preview = upload.preview;
  const columns = preview.columns ?? [];
  const parts = [uploadFileTypeLabel(upload)];
  if (preview.source_encoding) parts.push(preview.source_encoding);
  if (typeof preview.row_count === 'number') parts.push(`${preview.row_count} 行`);
  if (columns.length > 0) parts.push(columns.slice(0, 3).join('/'));
  if (preview.requires_sheet_selection) parts.push('需选择 sheet');
  if (preview.columns_truncated || preview.excel_sheets_truncated) parts.push('已裁剪摘要');
  return parts;
}

export function formatFileSize(sizeBytes: number): string {
  if (!Number.isFinite(sizeBytes) || sizeBytes < 0) return '未知大小';
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`;
}

export function mergeUploadsById(current: UploadFileResponse[], additions: UploadFileResponse[]): UploadFileResponse[] {
  if (additions.length === 0) return current;
  const seen = new Set(current.map((upload) => upload.upload_id));
  const merged = current.slice();
  for (const upload of additions) {
    if (seen.has(upload.upload_id)) continue;
    seen.add(upload.upload_id);
    merged.push(upload);
  }
  return merged;
}

export function draftAttachmentTypeLabel(attachment: DraftAttachment): string {
  const filename = attachment.filename.toLowerCase();
  if (filename.endsWith('.vcf') || filename.endsWith('.vcf.gz')) return 'VCF';
  if (filename.endsWith('.xlsx') || filename.endsWith('.xls')) return 'Excel';
  if (filename.endsWith('.json')) return 'JSON';
  if (filename.endsWith('.tsv') || isTsvContentType(attachment.contentType)) return 'TSV';
  if (filename.endsWith('.csv')) return 'CSV';
  if (filename.endsWith('.txt')) return 'TXT';
  if (filename.endsWith('.pdf')) return 'PDF';
  if (filename.endsWith('.png') || filename.endsWith('.jpg') || filename.endsWith('.jpeg')) return '图片';
  return attachment.contentType || '文件';
}

export function draftAttachmentStatusText(status: DraftAttachmentStatus): string {
  switch (status) {
    case 'uploading':
      return '上传中';
    case 'failed':
      return '待重试';
    default:
      return '待发送';
  }
}
