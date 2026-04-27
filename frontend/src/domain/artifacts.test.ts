import { describe, expect, it } from 'vitest';
import { parseAssistantTextArtifact, parseSqlQueryArtifacts } from './artifacts';
import type { ArtifactResponse } from '../api/types';

function artifact(overrides: Partial<ArtifactResponse>): ArtifactResponse {
  return {
    artifact_id: 'art-1',
    producer_node_id: 'node-1',
    artifact_type: 'json',
    storage_ref: '{}',
    summary: null,
    is_complete: true,
    created_at: null,
    ...overrides,
  };
}

describe('parseSqlQueryArtifacts', () => {
  it('prefers filtered result table while hiding SQL details', () => {
    const result = parseSqlQueryArtifacts([
      artifact({
        artifact_id: 'task-1:query_result_preview:raw',
        producer_node_id: 'task-1:sql_execute_readonly',
        storage_ref: JSON.stringify({
          sql: 'select * from secret',
          guard_pass_token: 'token',
          columns: ['variety_name', 'gene'],
          rows: [
            { variety_name: '龙粳33', gene: 'G1' },
            { variety_name: '龙粳331', gene: 'G2' },
          ],
          row_count: 2,
        }),
      }),
      artifact({
        artifact_id: 'task-1:filtered_query_result:def',
        producer_node_id: 'task-1:result_filtering',
        storage_ref: JSON.stringify({
          columns: ['variety_name', 'gene'],
          rows: [{ variety_name: '龙粳33', gene: 'G1' }],
          row_count: 1,
          source_row_count: 2,
          removed_row_count: 1,
          filter_source: 'llm',
        }),
      }),
    ]);

    expect(result.summary).toBe('查询已完成，共返回 1 行结果。');
    expect(result.table?.columns).toEqual(['variety_name', 'gene']);
    expect(result.table?.rows).toEqual([{ variety_name: '龙粳33', gene: 'G1' }]);
    expect(JSON.stringify(result)).not.toContain('select * from secret');
    expect(JSON.stringify(result)).not.toContain('token');
    expect(JSON.stringify(result)).not.toContain('龙粳331');
  });

  it('falls back to artifact summary when summary artifact storage_ref is invalid JSON', () => {
    const result = parseSqlQueryArtifacts([
      artifact({ artifact_id: 'task-1:summary:abc', artifact_type: 'summary', storage_ref: '{bad', summary: '降级摘要' }),
    ]);
    expect(result.summary).toBe('降级摘要');
    expect(result.warnings[0]).toContain('解析');
  });

  it('returns a friendly unavailable state when artifacts are empty', () => {
    const result = parseSqlQueryArtifacts([]);
    expect(result.summary).toContain('结果不可用');
    expect(result.table).toBeUndefined();
  });

  it('builds a neutral completion line from an empty filtered table', () => {
    const result = parseSqlQueryArtifacts([
      artifact({
        artifact_id: 'task-1:filtered_query_result:def',
        producer_node_id: 'task-1:result_filtering',
        storage_ref: JSON.stringify({
          columns: ['variety_name'],
          rows: [],
          row_count: 0,
          truncated: false,
        }),
      }),
    ]);

    expect(result.summary).toBe('查询已完成，共返回 0 行结果。');
    expect(result.table?.rows).toEqual([]);
  });

  it('builds a neutral completion line from the raw table when no filtered artifact exists', () => {
    const result = parseSqlQueryArtifacts([
      artifact({
        artifact_id: 'task-1:query_result_preview:def',
        producer_node_id: 'task-1:sql_execute_readonly',
        storage_ref: JSON.stringify({
          columns: ['variety_name'],
          rows: [{ variety_name: '龙粳33' }],
          row_count: 1,
          truncated: false,
        }),
      }),
    ]);

    expect(result.summary).toBe('查询已完成，共返回 1 行结果。');
    expect(result.table?.rows).toEqual([{ variety_name: '龙粳33' }]);
  });
});

describe('parseAssistantTextArtifact', () => {
  it('extracts the text artifact storage_ref', () => {
    expect(parseAssistantTextArtifact([artifact({ artifact_type: 'text', storage_ref: '最终回答', summary: '最终' })])).toBe('最终回答');
  });
});
