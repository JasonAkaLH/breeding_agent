import type { ChatMode, UploadFileResponse } from '../api/types';

export interface PendingInterrupt {
  taskId: string;
  interruptId: string;
  question: string;
  requiredFields: Record<string, unknown>;
  mode: ChatMode;
  naturalLanguage?: boolean;
}

export interface SheetSelectionField {
  required_upload_ids: string[];
  options_by_upload_id: Record<string, string[]>;
  labels_by_upload_id?: Record<string, string>;
}

export interface SlotCollectionRefSlot {
  name: string;
  label: string;
  type: string;
  status: string;
  requiredNow: boolean;
}

export interface SlotCollectionRefField {
  collectionId: string;
  slots: SlotCollectionRefSlot[];
}

export function interruptSubmitMetadata(interrupt: PendingInterrupt, uploads: UploadFileResponse[] = [], selectedSheets: Record<string, string> = {}): Record<string, unknown> {
  const metadata: Record<string, unknown> = {
    interrupt_id: interrupt.interruptId,
  };
  const uploadIds = uploads.map((upload) => upload.upload_id);
  if (uploadIds.length > 0) {
    metadata.upload_ids = uploadIds;
  }
  if (Object.keys(selectedSheets).length > 0) {
    metadata.upload_sheet_selections = selectedSheets;
  }
  return metadata;
}

export function sheetSelectionDisplayText(field: SheetSelectionField | null, selections: Record<string, string>): string {
  if (!field) return '';
  const parts = field.required_upload_ids
    .map((uploadId) => {
      const label = field.labels_by_upload_id?.[uploadId] ?? uploadId;
      const sheet = selections[uploadId];
      return sheet ? `${label}=${sheet}` : '';
    })
    .filter(Boolean);
  return parts.length > 0 ? `已选择 sheet：${parts.join('；')}` : '';
}

export function isInterruptKeepOpenResponse(response: { action?: string | null; answer_payload?: Record<string, unknown> | null }): boolean {
  const action = response.action || '';
  if (action === 'interrupt_clarification_answer' || action === 'clarification_answer') return true;
  if (action === 'interrupt_mixed_processed' || action === 'interrupt_schema_switched') {
    const payload = response.answer_payload || {};
    return payload.will_resume !== true || payload.requires_confirmation === true;
  }
  return false;
}

export function isNaturalLanguageInterrupt(requiredFields: Record<string, unknown>): boolean {
  return hasNaturalLanguagePresentation(requiredFields._sql_query_resolution)
    || hasNaturalLanguagePresentation(requiredFields._file_selection);
}

export function hasNaturalLanguagePresentation(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  return (value as { presentation?: unknown }).presentation === 'natural_language';
}

export function interruptAcceptsUpload(interrupt: PendingInterrupt): boolean {
  const hasVisibleUploadField = interruptVisibleFieldValues(interrupt).some((field) => {
    if (!field || typeof field !== 'object') return false;
    const metadata = field as { accepts_upload?: unknown; type?: unknown };
    return metadata.accepts_upload === true || ['artifact', 'file', 'data'].includes(String(metadata.type ?? ''));
  });
  if (hasVisibleUploadField) return true;
  return interruptSlotCollectionRefSlots(interrupt).some((slot) => ['artifact', 'file', 'data'].includes(slot.type));
}

export function interruptSheetSelectionField(interrupt: PendingInterrupt): SheetSelectionField | null {
  const raw = interrupt.requiredFields?.upload_sheet_selections;
  if (!raw || typeof raw !== 'object') return null;
  const field = raw as {
    required_upload_ids?: unknown;
    options_by_upload_id?: unknown;
    labels_by_upload_id?: unknown;
  };
  if (!Array.isArray(field.required_upload_ids) || !field.options_by_upload_id || typeof field.options_by_upload_id !== 'object') {
    return null;
  }
  const requiredUploadIds = field.required_upload_ids.filter((item): item is string => typeof item === 'string' && item.length > 0);
  const optionsByUploadId: Record<string, string[]> = {};
  for (const [uploadId, options] of Object.entries(field.options_by_upload_id as Record<string, unknown>)) {
    if (Array.isArray(options)) {
      optionsByUploadId[uploadId] = options.filter((item): item is string => typeof item === 'string' && item.length > 0);
    }
  }
  const labelsByUploadId: Record<string, string> = {};
  if (field.labels_by_upload_id && typeof field.labels_by_upload_id === 'object') {
    for (const [uploadId, label] of Object.entries(field.labels_by_upload_id as Record<string, unknown>)) {
      if (typeof label === 'string' && label.length > 0) labelsByUploadId[uploadId] = label;
    }
  }
  return {
    required_upload_ids: requiredUploadIds,
    options_by_upload_id: optionsByUploadId,
    labels_by_upload_id: labelsByUploadId,
  };
}

export function selectedSheetPayload(field: SheetSelectionField, answerText: string): Record<string, string> {
  const payload: Record<string, string> = {};
  const answer = answerText.trim();
  if (!answer) return {};
  if (field.required_upload_ids.length === 1) {
    const uploadId = field.required_upload_ids[0];
    const options = field.options_by_upload_id[uploadId] ?? [];
    const matched = options.find((option) => option === answer)
      ?? options.find((option) => option.toLowerCase() === answer.toLowerCase());
    return { [uploadId]: matched ?? answer };
  }

  const labelsByUploadId = field.labels_by_upload_id ?? {};
  const parts = answer.split(/[;\n；]+/).map((part) => part.trim()).filter(Boolean);
  for (const part of parts) {
    const match = part.match(/^(.+?)[=:：]\s*(.+)$/);
    if (!match) continue;
    const rawLabel = match[1].trim();
    const rawSheet = match[2].trim();
    const uploadId = field.required_upload_ids.find((candidate) => (
      candidate === rawLabel || labelsByUploadId[candidate] === rawLabel
    ));
    if (!uploadId || !rawSheet) continue;
    const options = field.options_by_upload_id[uploadId] ?? [];
    const matched = options.find((option) => option === rawSheet)
      ?? options.find((option) => option.toLowerCase() === rawSheet.toLowerCase());
    payload[uploadId] = matched ?? rawSheet;
  }
  return payload;
}

export function interruptVisibleFieldNames(interrupt: PendingInterrupt): string[] {
  return Object.keys(interrupt.requiredFields ?? {}).filter((field) => !isReservedInterruptField(field));
}

export function interruptVisibleFieldValues(interrupt: PendingInterrupt): unknown[] {
  return Object.entries(interrupt.requiredFields ?? {})
    .filter(([field]) => !isReservedInterruptField(field))
    .map(([, value]) => value);
}

export function interruptSlotCollectionRef(interrupt: PendingInterrupt): SlotCollectionRefField | null {
  const raw = interrupt.requiredFields?._slot_collection_ref;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const field = raw as { collection_id?: unknown; slots?: unknown };
  const collectionId = typeof field.collection_id === 'string' ? field.collection_id : '';
  if (!collectionId) return null;
  const slots: SlotCollectionRefSlot[] = [];
  if (Array.isArray(field.slots)) {
    for (const rawSlot of field.slots) {
      if (!rawSlot || typeof rawSlot !== 'object' || Array.isArray(rawSlot)) continue;
      const slot = rawSlot as {
        name?: unknown;
        label?: unknown;
        type?: unknown;
        status?: unknown;
        required_now?: unknown;
      };
      const name = typeof slot.name === 'string' && slot.name.length > 0 ? slot.name : '';
      if (!name) continue;
      const label = typeof slot.label === 'string' && slot.label.length > 0 ? slot.label : name;
      slots.push({
        name,
        label,
        type: typeof slot.type === 'string' ? slot.type : 'string',
        status: typeof slot.status === 'string' ? slot.status : '',
        requiredNow: slot.required_now === true,
      });
    }
  }
  return { collectionId, slots };
}

export function interruptSlotCollectionRefSlots(interrupt: PendingInterrupt): SlotCollectionRefSlot[] {
  const ref = interruptSlotCollectionRef(interrupt);
  if (!ref) return [];
  const activeSlots = ref.slots.filter((slot) => slot.requiredNow || ['missing', 'invalid'].includes(slot.status));
  return activeSlots.length > 0 ? activeSlots : ref.slots;
}

export function isReservedInterruptField(field: string): boolean {
  return field.startsWith('_');
}

export function interruptAnswerPlaceholder(interrupt: PendingInterrupt): string {
  return interrupt.question.trim() || '请补充当前任务所需信息';
}
