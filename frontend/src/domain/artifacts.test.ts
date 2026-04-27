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
  it('extracts summary and preview table while hiding SQL details', () => {
    const result = parseSqlQueryArtifacts([
      artifact({
        artifact_id: 'task-1:result_summary:abc',
        artifact_type: 'summary',
        storage_ref: JSON.stringify({ summary: '共返回 1 行结果。', row_count: 1, truncated: false }),
        summary: 'fallback summary',
      }),
      artifact({
        artifact_id: 'task-1:query_result_preview:def',
        storage_ref: JSON.stringify({
          sql: 'select * from secret',
          guard_pass_token: 'token',
          columns: ['variety_name', 'gene'],
          rows: [{ variety_name: '龙粳33', gene: 'G1' }],
          row_count: 1,
        }),
      }),
    ]);

    expect(result.summary).toBe('共返回 1 行结果。');
    expect(result.table?.columns).toEqual(['variety_name', 'gene']);
    expect(result.table?.rows).toEqual([{ variety_name: '龙粳33', gene: 'G1' }]);
    expect(JSON.stringify(result)).not.toContain('select * from secret');
    expect(JSON.stringify(result)).not.toContain('token');
  });

  it('falls back to artifact summary when storage_ref is invalid JSON', () => {
    const result = parseSqlQueryArtifacts([
      artifact({ artifact_id: 'task-1:result_summary:abc', artifact_type: 'summary', storage_ref: '{bad', summary: '降级摘要' }),
    ]);
    expect(result.summary).toBe('降级摘要');
    expect(result.warnings[0]).toContain('解析');
  });

  it('returns a friendly unavailable state when artifacts are empty', () => {
    const result = parseSqlQueryArtifacts([]);
    expect(result.summary).toContain('摘要不可用');
    expect(result.table).toBeUndefined();
  });
});

describe('parseAssistantTextArtifact', () => {
  it('extracts the text artifact storage_ref', () => {
    expect(parseAssistantTextArtifact([artifact({ artifact_type: 'text', storage_ref: '最终回答', summary: '最终' })])).toBe('最终回答');
  });
});
