import type { ArtifactResponse } from '../api/types';

export interface DataQueryTablePreview {
  columns: string[];
  rows: Record<string, unknown>[];
  rowCount?: number;
  truncated?: boolean;
}

export interface DataQueryDisplayModel {
  summary: string;
  table?: DataQueryTablePreview;
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

export interface OcrRawTextDisplayModel {
  artifactId: string;
  title: string;
  rawText: string;
  filename?: string;
  status?: string;
  jobId?: string;
}

export interface MCPResultTextDisplayModel {
  artifactId: string;
  title: string;
  text: string;
}

export type CapabilityArtifactDisplay =
  | {
      kind: 'data_query';
      result: DataQueryDisplayModel;
    }
  | {
      kind: 'file';
      result: FileArtifactDisplayModel;
    }
  | {
      kind: 'ocr_raw_text';
      result: OcrRawTextDisplayModel;
    }
  | {
      kind: 'mcp_result_text';
      result: MCPResultTextDisplayModel;
    };

export function parseCapabilityArtifactDisplays(artifacts: ArtifactResponse[]): CapabilityArtifactDisplay[] {
  const displays: CapabilityArtifactDisplay[] = [];
  const dataQueryResult = parseDataQueryArtifacts(artifacts);
  const hasDataQueryResult = Boolean(dataQueryResult.table) || dataQueryResult.sourceArtifactIds.length > 0;
  if (hasDataQueryResult) {
    displays.push({ kind: 'data_query', result: dataQueryResult });
  }
  displays.push(...parseOcrRawTextDisplays(artifacts).map((result) => ({ kind: 'ocr_raw_text' as const, result })));
  displays.push(...parseMCPResultTextDisplays(artifacts).map((result) => ({ kind: 'mcp_result_text' as const, result })));
  displays.push(...parseFileArtifactDisplays(artifacts).map((result) => ({ kind: 'file' as const, result })));
  return displays;
}

export function summarizeCapabilityArtifactDisplays(displays: CapabilityArtifactDisplay[]): string {
  const first = displays[0];
  if (!first) return '';
  if (first.kind === 'data_query') return first.result.summary;
  if (first.kind === 'file') return first.result.summary;
  if (first.kind === 'ocr_raw_text') return first.result.title;
  if (first.kind === 'mcp_result_text') return first.result.title;
  return '';
}

export function parseMCPResultTextDisplays(artifacts: ArtifactResponse[]): MCPResultTextDisplayModel[] {
  return artifacts
    .filter(isMCPResultTextArtifact)
    .map((artifact) => ({
      artifactId: artifact.artifact_id,
      title: stringOrFallback(artifact.summary, 'MCP Tool原始返回'),
      text: artifact.storage_ref,
    }));
}

export function parseOcrRawTextDisplays(artifacts: ArtifactResponse[]): OcrRawTextDisplayModel[] {
  const displays: OcrRawTextDisplayModel[] = [];
  for (const artifact of artifacts) {
    const parsed = artifactMetadata(artifact);
    if (!isOcrRawTextArtifact(artifact, parsed)) continue;
    const rawText = stringField(parsed, 'raw_text') || stringField(parsed, 'text') || stringField(parsed, 'markdown');
    if (!rawText) continue;
    const filename = stringField(parsed, 'filename') ?? undefined;
    const title = stringOrFallback(artifact.summary, filename ? `OCR 回传原文：${filename}` : 'OCR 回传原文');
    displays.push(
      {
        artifactId: artifact.artifact_id,
        title,
        rawText,
        filename,
        status: stringField(parsed, 'status') ?? undefined,
        jobId: stringField(parsed, 'job_id') ?? undefined,
      },
    );
  }
  return displays;
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

export function parseDataQueryArtifacts(artifacts: ArtifactResponse[]): DataQueryDisplayModel {
  const warnings: string[] = [];
  const sourceArtifactIds: string[] = [];
  const dataQueryArtifacts = artifacts.filter(isDataQueryDisplayArtifact);
  const summaryArtifact = dataQueryArtifacts.find(isSummaryArtifact);
  const previewArtifact = dataQueryArtifacts.find(isFilteredPreviewArtifact) ?? dataQueryArtifacts.find(isPreviewArtifact);

  let summary = '数据查询已完成，但结果不可用。';
  if (summaryArtifact) {
    sourceArtifactIds.push(summaryArtifact.artifact_id);
    const parsed = parseStorageRef(summaryArtifact, warnings);
    summary = stringField(parsed, 'summary') || summaryArtifact.summary || summary;
  }

  let table: DataQueryTablePreview | undefined;
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
        summary = typeof rowCount === 'number' ? `数据查询已完成，共返回 ${rowCount} 行结果。` : '数据查询已完成，已返回表格结果。';
      }
    }
  }

  return { summary, table, warnings, sourceArtifactIds };
}

export function parseAssistantTextArtifact(artifacts: ArtifactResponse[]): string | null {
  const textArtifacts = artifacts.filter(
    (artifact) => artifact.artifact_type === 'text' && !isMCPResultTextArtifact(artifact),
  );
  const textArtifact = textArtifacts.find(isMainAgentTextArtifact) ?? textArtifacts[0];
  if (!textArtifact) return null;
  return textArtifact.storage_ref || textArtifact.summary || null;
}

function isMCPResultTextArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_type === 'text'
    && artifact.artifact_id.startsWith('mcp-result-artifact:v1:');
}

function isSummaryArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_type === 'summary' || artifactMetadata(artifact).artifact_role === 'summary';
}

function isFilteredPreviewArtifact(artifact: ArtifactResponse): boolean {
  const metadata = artifactMetadata(artifact);
  return metadata.artifact_role === 'filtered_query_result'
    || artifact.artifact_id.includes('filtered_query_result');
}

function isPreviewArtifact(artifact: ArtifactResponse): boolean {
  const metadata = artifactMetadata(artifact);
  return metadata.artifact_role === 'query_result_preview'
    || isFilteredPreviewArtifact(artifact)
    || artifact.artifact_id.includes('query_result_preview');
}

function isDataQueryDisplayArtifact(artifact: ArtifactResponse): boolean {
  const metadata = artifactMetadata(artifact);
  return (
    metadata.domain_kind === 'data_query'
    || metadata.artifact_family === 'data_query'
    || artifact.artifact_id.includes('data_query')
    || isPreviewArtifact(artifact)
  );
}

function isOcrRawTextArtifact(artifact: ArtifactResponse, metadata: Record<string, unknown>): boolean {
  return artifact.artifact_type === 'json'
    && (
      metadata.domain_kind === 'ocr'
      || metadata.artifact_family === 'ocr'
      || metadata.artifact_role === 'ocr_raw_text'
      || artifact.artifact_id.includes('ocr_raw_text')
    )
    && metadata.artifact_role === 'ocr_raw_text';
}

function artifactMetadata(artifact: ArtifactResponse): Record<string, unknown> {
  try {
    const parsed = JSON.parse(artifact.storage_ref) as unknown;
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function isMainAgentTextArtifact(artifact: ArtifactResponse): boolean {
  return artifact.artifact_id.includes('main_agent_response')
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
