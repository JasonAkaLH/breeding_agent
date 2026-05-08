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

export interface FileArtifactDisplayModel {
  artifactId: string;
  filename: string;
  mimeType: string;
  summary: string;
  downloadUrl: string;
  sizeBytes?: number;
  sourceFileCount?: number;
  archiveFormat?: string;
}

export type CapabilityArtifactDisplay =
  | {
      kind: 'sql_query';
      result: SqlQueryDisplayModel;
    }
  | {
      kind: 'file';
      result: FileArtifactDisplayModel;
    };

export function parseCapabilityArtifactDisplays(artifacts: ArtifactResponse[]): CapabilityArtifactDisplay[] {
  const displays: CapabilityArtifactDisplay[] = [];
  const sqlResult = parseSqlQueryArtifacts(artifacts);
  const hasSqlResult = Boolean(sqlResult.table) || sqlResult.sourceArtifactIds.length > 0;
  if (hasSqlResult) {
    displays.push({ kind: 'sql_query', result: sqlResult });
  }
  displays.push(...parseFileArtifactDisplays(artifacts).map((result) => ({ kind: 'file' as const, result })));
  return displays;
}

export function summarizeCapabilityArtifactDisplays(displays: CapabilityArtifactDisplay[]): string {
  const first = displays[0];
  if (!first) return '';
  if (first.kind === 'sql_query') return first.result.summary;
  if (first.kind === 'file') return first.result.summary;
  return '';
}

export function parseFileArtifactDisplays(artifacts: ArtifactResponse[]): FileArtifactDisplayModel[] {
  return artifacts
    .filter((artifact) => artifact.artifact_type === 'file' && Boolean(artifact.download_url))
    .map((artifact) => {
      const filename = stringOrFallback(artifact.filename, '生成文件');
      return {
        artifactId: artifact.artifact_id,
        filename,
        mimeType: stringOrFallback(artifact.mime_type, 'application/octet-stream'),
        summary: stringOrFallback(artifact.summary, filename),
        downloadUrl: artifact.download_url || '',
        sizeBytes: typeof artifact.size_bytes === 'number' ? artifact.size_bytes : undefined,
        sourceFileCount: typeof artifact.source_file_count === 'number' ? artifact.source_file_count : undefined,
        archiveFormat: typeof artifact.archive_format === 'string' && artifact.archive_format ? artifact.archive_format : undefined,
      };
    });
}

export function parseSqlQueryArtifacts(artifacts: ArtifactResponse[]): SqlQueryDisplayModel {
  const warnings: string[] = [];
  const sourceArtifactIds: string[] = [];
  const sqlArtifacts = artifacts.filter(isSqlQueryDisplayArtifact);
  const summaryArtifact = sqlArtifacts.find(isSummaryArtifact);
  const previewArtifact = sqlArtifacts.find(isFilteredPreviewArtifact) ?? sqlArtifacts.find(isPreviewArtifact);

  let summary = '查询已完成，但结果不可用。';
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
    if (columns.length > 0) {
      const rowCount = numberField(parsed, 'row_count');
      table = {
        columns,
        rows: rows.map((row) => pickColumns(row, columns)),
        rowCount,
        truncated: booleanField(parsed, 'truncated'),
      };
      if (!summaryArtifact) {
        summary = typeof rowCount === 'number' ? `查询已完成，共返回 ${rowCount} 行结果。` : '查询已完成，已返回表格结果。';
      }
    }
  }

  return { summary, table, warnings, sourceArtifactIds };
}

export function parseAssistantTextArtifact(artifacts: ArtifactResponse[]): string | null {
  const textArtifacts = artifacts.filter((artifact) => artifact.artifact_type === 'text');
  const textArtifact = textArtifacts.find(isMainAgentTextArtifact) ?? textArtifacts[0];
  if (!textArtifact) return null;
  return textArtifact.storage_ref || textArtifact.summary || null;
}

function isSummaryArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_type === 'summary';
}

function isFilteredPreviewArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_id.includes('filtered_query_result') || artifact.producer_node_id.includes('result_filtering');
}

function isPreviewArtifact(artifact: ArtifactResponse): boolean {
  return isFilteredPreviewArtifact(artifact) || artifact.artifact_id.includes('query_result_preview') || artifact.producer_node_id.includes('sql_execute_readonly');
}

function isSqlQueryDisplayArtifact(artifact: ArtifactResponse): boolean {
  return (
    isPreviewArtifact(artifact)
    || artifact.artifact_id.includes('sql_query')
    || artifact.producer_node_id.includes('sql_query')
  );
}

function isMainAgentTextArtifact(artifact: ArtifactResponse): boolean {
  return artifact.producer_node_id.includes('main_agent.respond')
    || artifact.artifact_id.includes('main_agent_response')
    || artifact.artifact_id.includes('main_agent_text');
}

function stringOrFallback(value: string | null | undefined, fallback: string): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
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
