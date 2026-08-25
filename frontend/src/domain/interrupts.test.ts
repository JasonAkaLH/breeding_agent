import { describe, expect, it } from 'vitest';
import type { UploadFileResponse } from '../api/types';
import {
  interruptAcceptsUpload,
  interruptAnswerPlaceholder,
  interruptSheetSelectionField,
  interruptSlotCollectionRef,
  interruptSlotCollectionRefSlots,
  interruptSubmitMetadata,
  interruptVisibleFieldNames,
  interruptVisibleFieldValues,
  isInterruptKeepOpenResponse,
  isNaturalLanguageInterrupt,
  isReservedInterruptField,
  selectedSheetPayload,
  sheetSelectionDisplayText,
  type PendingInterrupt,
} from './interrupts';

function pending(requiredFields: Record<string, unknown> = {}, question = '需要补充什么？'): PendingInterrupt {
  return {
    taskId: 'task-1',
    interruptId: 'interrupt-1',
    question,
    requiredFields,
    mode: 'chat',
  };
}

describe('interrupt domain', () => {
  it('builds submit metadata without adding empty optional fields', () => {
    expect(interruptSubmitMetadata(pending())).toEqual({ interrupt_id: 'interrupt-1' });
    expect(interruptSubmitMetadata(
      pending(),
      [{ upload_id: 'upload-1' } as UploadFileResponse],
      { 'upload-1': 'Sheet1' },
    )).toEqual({
      interrupt_id: 'interrupt-1',
      upload_ids: ['upload-1'],
      upload_sheet_selections: { 'upload-1': 'Sheet1' },
    });
  });

  it('keeps existing keep-open response decisions', () => {
    expect(isInterruptKeepOpenResponse({ action: 'clarification_answer' })).toBe(true);
    expect(isInterruptKeepOpenResponse({ action: 'interrupt_mixed_processed', answer_payload: { will_resume: true } })).toBe(false);
    expect(isInterruptKeepOpenResponse({ action: 'interrupt_schema_switched', answer_payload: { requires_confirmation: true } })).toBe(true);
    expect(isInterruptKeepOpenResponse({ action: 'resumed' })).toBe(false);
  });

  it('detects natural-language presentation only on the reserved resolution fields', () => {
    expect(isNaturalLanguageInterrupt({
      _file_selection: { presentation: 'natural_language' },
    })).toBe(true);
    expect(isNaturalLanguageInterrupt({
      input: { presentation: 'natural_language' },
    })).toBe(false);
  });

  it('parses sheet choices and preserves labels, matching, and display order', () => {
    const interrupt = pending({
      upload_sheet_selections: {
        required_upload_ids: ['upload-1', '', 42, 'upload-2'],
        options_by_upload_id: {
          'upload-1': ['Sheet1', 'Sheet2'],
          'upload-2': ['Data'],
        },
        labels_by_upload_id: {
          'upload-1': '文件一',
          'upload-2': '文件二',
        },
      },
    });

    const field = interruptSheetSelectionField(interrupt);
    expect(field).toEqual({
      required_upload_ids: ['upload-1', 'upload-2'],
      options_by_upload_id: {
        'upload-1': ['Sheet1', 'Sheet2'],
        'upload-2': ['Data'],
      },
      labels_by_upload_id: {
        'upload-1': '文件一',
        'upload-2': '文件二',
      },
    });
    expect(selectedSheetPayload(field!, '文件一=sheet2；文件二：data')).toEqual({
      'upload-1': 'Sheet2',
      'upload-2': 'Data',
    });
    expect(sheetSelectionDisplayText(field, { 'upload-1': 'Sheet2', 'upload-2': 'Data' }))
      .toBe('已选择 sheet：文件一=Sheet2；文件二=Data');
  });

  it('filters reserved fields and recognizes visible upload fields', () => {
    const interrupt = pending({
      _internal: { type: 'file' },
      note: { type: 'string' },
      artifact: { accepts_upload: true },
    });

    expect(isReservedInterruptField('_internal')).toBe(true);
    expect(interruptVisibleFieldNames(interrupt)).toEqual(['note', 'artifact']);
    expect(interruptVisibleFieldValues(interrupt)).toEqual([{ type: 'string' }, { accepts_upload: true }]);
    expect(interruptAcceptsUpload(interrupt)).toBe(true);
  });

  it('normalizes slot references and prefers required or invalid slots', () => {
    const interrupt = pending({
      _slot_collection_ref: {
        collection_id: 'collection-1',
        slots: [
          { name: 'reference', label: '参考文件', type: 'file', status: 'missing', required_now: true },
          { name: 'note', type: 'string', status: 'ready' },
          { label: 'ignored' },
        ],
      },
    });

    expect(interruptSlotCollectionRef(interrupt)).toEqual({
      collectionId: 'collection-1',
      slots: [
        { name: 'reference', label: '参考文件', type: 'file', status: 'missing', requiredNow: true },
        { name: 'note', label: 'note', type: 'string', status: 'ready', requiredNow: false },
      ],
    });
    expect(interruptSlotCollectionRefSlots(interrupt)).toEqual([
      { name: 'reference', label: '参考文件', type: 'file', status: 'missing', requiredNow: true },
    ]);
    expect(interruptAcceptsUpload(interrupt)).toBe(true);
  });

  it('keeps the existing answer placeholder fallback', () => {
    expect(interruptAnswerPlaceholder(pending({}, '  请选择 sheet  '))).toBe('请选择 sheet');
    expect(interruptAnswerPlaceholder(pending({}, '   '))).toBe('请补充当前任务所需信息');
  });
});
