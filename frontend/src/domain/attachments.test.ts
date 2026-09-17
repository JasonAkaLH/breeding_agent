import { describe, expect, it } from 'vitest';
import type { UploadFileResponse } from '../api/types';
import {
  draftAttachmentStatusText,
  draftAttachmentTypeLabel,
  formatFileSize,
  isTsvContentType,
  mergeUploadsById,
  uploadAnswerDisplayText,
  uploadFileSummaryParts,
  uploadFileTypeLabel,
  type DraftAttachment,
} from './attachments';

function upload(overrides: Partial<UploadFileResponse> = {}): UploadFileResponse {
  return {
    upload_id: 'upload-1',
    conversation_id: 'conversation-1',
    filename: 'data.csv',
    content_type: 'text/csv',
    file_type: 'csv',
    size_bytes: 12,
    sha256: 'sha256',
    expires_at: '2099-01-01T00:00:00Z',
    preview: { columns: [] },
    ...overrides,
  };
}

function draft(overrides: Partial<DraftAttachment> = {}): DraftAttachment {
  return {
    localId: 'draft-1',
    file: {} as File,
    filename: 'data.csv',
    contentType: 'text/csv',
    sizeBytes: 12,
    status: 'draft',
    ...overrides,
  };
}

describe('attachment domain', () => {
  it('keeps upload answer display text and empty behavior', () => {
    expect(uploadAnswerDisplayText([])).toBe('');
    expect(uploadAnswerDisplayText([
      upload(),
      upload({ upload_id: 'upload-2', filename: 'notes.txt' }),
    ])).toBe('已上传文件：data.csv、notes.txt');
  });

  it('normalizes TSV content types and file labels', () => {
    expect(isTsvContentType(' Text/TSV; charset=utf-8 ')).toBe(true);
    expect(uploadFileTypeLabel(upload({ filename: 'data.tsv', file_type: 'text' }))).toBe('TSV');
    expect(draftAttachmentTypeLabel(draft({ filename: 'variants.vcf.gz' }))).toBe('VCF');
  });

  it('preserves upload summary ordering and truncation notices', () => {
    expect(uploadFileSummaryParts(upload({
      file_type: 'spreadsheet',
      preview: {
        columns: ['one', 'two', 'three', 'four'],
        source_encoding: 'utf-8',
        row_count: 4,
        requires_sheet_selection: true,
        columns_truncated: true,
      },
    }))).toEqual(['Excel', 'utf-8', '4 行', 'one/two/three', '需选择 sheet', '已裁剪摘要']);
  });

  it('formats sizes and draft statuses at the existing boundaries', () => {
    expect(formatFileSize(-1)).toBe('未知大小');
    expect(formatFileSize(1023)).toBe('1023 B');
    expect(formatFileSize(1024)).toBe('1.0 KB');
    expect(formatFileSize(1024 * 1024)).toBe('1.0 MB');
    expect(['待发送', '上传中', '待重试']).toEqual([
      draftAttachmentStatusText('draft'),
      draftAttachmentStatusText('uploading'),
      draftAttachmentStatusText('failed'),
    ]);
  });

  it('merges uploads by id without mutating either input', () => {
    const current = [upload()];
    const duplicate = upload({ filename: 'ignored.csv' });
    const addition = upload({ upload_id: 'upload-2', filename: 'new.csv' });

    const merged = mergeUploadsById(current, [duplicate, addition]);

    expect(merged).toEqual([current[0], addition]);
    expect(current).toHaveLength(1);
    expect(mergeUploadsById(current, [])).toBe(current);
  });
});
