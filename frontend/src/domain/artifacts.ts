import type { ArtifactResponse } from '../api/types';

export interface SqlQueryTablePreview {
  columns: string[];
  rows: Record<string, unknown>[];
  rowCount?: number;
  truncated?: boolean;
}

export interface SqlQueryDisplayModel {
  summary: string;
  table?: SqlQueryTablePreview;
  warnings: string[];
  sourceArtifactIds: string[];
}

export function parseSqlQueryArtifacts(artifacts: ArtifactResponse[]): SqlQueryDisplayModel {
  const warnings: string[] = [];
  const sourceArtifactIds: string[] = [];
  const summaryArtifact = artifacts.find(isSummaryArtifact);
  const previewArtifact = artifacts.find(isPreviewArtifact);

  let summary = '查询已完成，但摘要不可用。';
  if (summaryArtifact) {
    sourceArtifactIds.push(summaryArtifact.artifact_id);
    const parsed = parseStorageRef(summaryArtifact, warnings);
    summary = stringField(parsed, 'summary') || summaryArtifact.summary || summary;
  }

  let table: SqlQueryTablePreview | undefined;
  if (previewArtifact) {
    sourceArtifactIds.push(previewArtifact.artifact_id);
    const parsed = parseStorageRef(previewArtifact, warnings);
    const columns = arrayOfStrings(parsed?.columns);
    const rows = arrayOfRecords(parsed?.rows);
    if (columns.length > 0 && rows.length > 0) {
      table = {
        columns,
        rows: rows.map((row) => pickColumns(row, columns)),
        rowCount: numberField(parsed, 'row_count'),
        truncated: booleanField(parsed, 'truncated'),
      };
    }
  }

  return { summary, table, warnings, sourceArtifactIds };
}

export function parseAssistantTextArtifact(artifacts: ArtifactResponse[]): string | null {
  const textArtifact = artifacts.find((artifact) => artifact.artifact_type === 'text');
  if (!textArtifact) return null;
  return textArtifact.storage_ref || textArtifact.summary || null;
}

function isSummaryArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_type === 'summary' || artifact.artifact_id.includes('result_summary') || artifact.producer_node_id.includes('result_summarize');
}

function isPreviewArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_id.includes('query_result_preview') || artifact.producer_node_id.includes('sql_execute_readonly');
}

function parseStorageRef(artifact: ArtifactResponse, warnings: string[]): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(artifact.storage_ref) as unknown;
    return isRecord(parsed) ? parsed : null;
  } catch {
    warnings.push(`产物 ${artifact.artifact_id} 的结构化内容解析失败，已使用降级展示。`);
    return null;
  }
}

function stringField(value: Record<string, unknown> | null, field: string): string | null {
  const candidate = value?.[field];
  return typeof candidate === 'string' && candidate.trim() ? candidate : null;
}

function numberField(value: Record<string, unknown> | null, field: string): number | undefined {
  const candidate = value?.[field];
  return typeof candidate === 'number' ? candidate : undefined;
}

function booleanField(value: Record<string, unknown> | null, field: string): boolean | undefined {
  const candidate = value?.[field];
  return typeof candidate === 'boolean' ? candidate : undefined;
}

function arrayOfStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pickColumns(row: Record<string, unknown>, columns: string[]): Record<string, unknown> {
  const picked: Record<string, unknown> = {};
  for (const column of columns) {
    picked[column] = row[column];
  }
  return picked;
}
