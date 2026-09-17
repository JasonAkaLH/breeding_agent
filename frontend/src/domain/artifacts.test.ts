import { describe, expect, it } from 'vitest';
import { parseAssistantTextArtifact, parseCapabilityArtifactDisplays, parseDataQueryArtifacts, summarizeCapabilityArtifactDisplays } from './artifacts';
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

describe('parseDataQueryArtifacts', () => {
  it('prefers filtered result table while hiding SQL details', () => {
    const result = parseDataQueryArtifacts([
      artifact({
        artifact_id: 'task-1:query_result_preview:raw',
        producer_node_id: 'task-1:execute_query',
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
        producer_node_id: 'task-1:filter_results',
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

    expect(result.summary).toBe('数据查询已完成，共返回 1 行结果。');
    expect(result.table?.columns).toEqual(['variety_name', 'gene']);
    expect(result.table?.rows).toEqual([{ variety_name: '龙粳33', gene: 'G1' }]);
    expect(JSON.stringify(result)).not.toContain('select * from secret');
    expect(JSON.stringify(result)).not.toContain('token');
    expect(JSON.stringify(result)).not.toContain('龙粳331');
  });

  it('falls back to artifact summary when summary artifact storage_ref is invalid JSON', () => {
    const result = parseDataQueryArtifacts([
      artifact({
        artifact_id: 'task-1:data_query_result_summary:abc',
        producer_node_id: 'task-1:data_query_summarize',
        artifact_type: 'summary',
        storage_ref: '{bad',
        summary: '降级摘要',
      }),
    ]);
    expect(result.summary).toBe('降级摘要');
    expect(result.warnings[0]).toContain('解析');
  });

  it('returns a friendly unavailable state when artifacts are empty', () => {
    const result = parseDataQueryArtifacts([]);
    expect(result.summary).toContain('结果不可用');
    expect(result.table).toBeUndefined();
  });

  it('builds a neutral completion line from an empty filtered table', () => {
    const result = parseDataQueryArtifacts([
      artifact({
        artifact_id: 'task-1:filtered_query_result:def',
        producer_node_id: 'task-1:filter_results',
        storage_ref: JSON.stringify({
          columns: ['variety_name'],
          rows: [],
          row_count: 0,
          truncated: false,
        }),
      }),
    ]);

    expect(result.summary).toBe('数据查询已完成，共返回 0 行结果。');
    expect(result.table?.rows).toEqual([]);
  });

  it('builds a neutral completion line from the raw table when no filtered artifact exists', () => {
    const result = parseDataQueryArtifacts([
      artifact({
        artifact_id: 'task-1:query_result_preview:def',
        producer_node_id: 'task-1:execute_query',
        storage_ref: JSON.stringify({
          columns: ['variety_name'],
          rows: [{ variety_name: '龙粳33' }],
          row_count: 1,
          truncated: false,
        }),
      }),
    ]);

    expect(result.summary).toBe('数据查询已完成，共返回 1 行结果。');
    expect(result.table?.rows).toEqual([{ variety_name: '龙粳33' }]);
  });
});

describe('parseAssistantTextArtifact', () => {
  it('extracts the text artifact storage_ref', () => {
    expect(parseAssistantTextArtifact([artifact({ artifact_type: 'text', storage_ref: '最终回答', summary: '最终' })])).toBe('最终回答');
  });

  it('prefers the Agent final text artifact over capability-owned text artifacts', () => {
    expect(parseAssistantTextArtifact([
      artifact({
        artifact_id: 'weather_text:1',
        producer_node_id: 'task-1:weather.respond',
        artifact_type: 'text',
        storage_ref: '天气能力文本',
      }),
      artifact({
        artifact_id: 'agent-artifact:task-1:final',
        producer_node_id: 'agent-node:task-1:final',
        artifact_type: 'text',
        storage_ref: '主代理最终回答',
      }),
    ])).toBe('主代理最终回答');
  });

  it('uses artifact identity rather than opaque producer node id for Agent-final preference', () => {
    expect(parseAssistantTextArtifact([
      artifact({
        artifact_id: 'weather_text:1',
        producer_node_id: 'opaque:agent.final_output:misleading',
        artifact_type: 'text',
        storage_ref: '天气能力文本',
      }),
      artifact({
        artifact_id: 'agent-artifact:task-1:final',
        producer_node_id: 'opaque:capability:misleading',
        artifact_type: 'text',
        storage_ref: '主代理最终回答',
      }),
    ])).toBe('主代理最终回答');
  });
});

describe('parseCapabilityArtifactDisplays', () => {
  it('returns supplemental display models without consuming the assistant text artifact', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({ artifact_type: 'text', storage_ref: '最终回答', summary: '最终' }),
      artifact({
        artifact_id: 'task-1:filtered_query_result:def',
        producer_node_id: 'task-1:filter_results',
        storage_ref: JSON.stringify({
          columns: ['variety_name'],
          rows: [],
          row_count: 0,
        }),
      }),
    ]);

    expect(displays).toHaveLength(1);
    expect(displays[0]).toMatchObject({ kind: 'data_query' });
    expect(summarizeCapabilityArtifactDisplays(displays)).toBe('数据查询已完成，共返回 0 行结果。');
  });



  it('returns a file artifact display for downloadable Skill output files', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({ artifact_type: 'text', storage_ref: '最终回答', summary: '最终' }),
      artifact({
        artifact_id: 'art-file-1',
        producer_node_id: 'agent-node:task-1:final',
        artifact_type: 'file',
        storage_ref: '',
        summary: 'HTML 布局',
        filename: 'layout.html',
        mime_type: 'text/html',
        size_bytes: 12,
        download_url: '/api/v1/artifacts/art-file-1/download',
        source_file_count: 1,
        archive_format: null,
        retention_status: 'active',
      }),
    ]);

    expect(displays).toHaveLength(1);
    expect(displays[0]).toMatchObject({
      kind: 'file',
      result: {
        artifactId: 'art-file-1',
        filename: 'layout.html',
        mimeType: 'text/html',
        downloadUrl: '/api/v1/artifacts/art-file-1/download',
      },
    });
    expect(summarizeCapabilityArtifactDisplays(displays)).toBe('HTML 布局');
  });

  it('returns an OCR raw text display for OCR artifacts', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({
        artifact_id: 'task-1:skill_display:abc:ocr_raw_text',
        producer_node_id: 'task-1:ocr:skill_execute',
        artifact_type: 'json',
        summary: 'OCR 回传原文：scan.png',
        storage_ref: JSON.stringify({
          domain_kind: 'ocr',
          artifact_role: 'ocr_raw_text',
          raw_text: '品种：龙粳33\n处理：A1',
          filename: 'scan.png',
          job_id: 'job-1',
          status: 'succeeded',
        }),
      }),
    ]);

    expect(displays).toHaveLength(1);
    expect(displays[0]).toMatchObject({
      kind: 'ocr_raw_text',
      result: {
        title: 'OCR 回传原文：scan.png',
        rawText: '品种：龙粳33\n处理：A1',
        filename: 'scan.png',
        jobId: 'job-1',
      },
    });
    expect(summarizeCapabilityArtifactDisplays(displays)).toBe('OCR 回传原文：scan.png');
  });

  it('uses the typed MCP business view without reading raw storage or replacing the assistant answer', () => {
    const rawBody = '{"result":{"content":[{"type":"text","text":"不得展示的原始返回"}]}}';
    const artifacts = [
      artifact({
        artifact_id: 'agent-artifact:task-1:final',
        artifact_type: 'text',
        storage_ref: '主代理总结',
        summary: 'final',
      }),
      artifact({
        artifact_id: 'opaque-artifact-2',
        artifact_type: 'mcp_result',
        storage_ref: rawBody,
        summary: 'MCP 工具结果',
        mcp_business_result: {
          schema: 'maf.mcp.business_result_view.v1',
          availability: 'ready',
          outcome: 'succeeded',
          primary: { kind: 'structured', value: { answer: 42 }, truncated: false },
          projection_truncated: false,
        },
      }),
    ];

    expect(parseAssistantTextArtifact(artifacts)).toBe('主代理总结');
    expect(parseAssistantTextArtifact([artifacts[1]])).toBeNull();
    expect(parseCapabilityArtifactDisplays(artifacts)).toEqual([
      {
        kind: 'mcp_business_result',
        result: {
          artifactId: 'opaque-artifact-2',
          title: 'MCP 工具结果',
          result: {
            schema: 'maf.mcp.business_result_view.v1',
            availability: 'ready',
            outcome: 'succeeded',
            primary: { kind: 'structured', value: { answer: 42 }, truncated: false },
            projection_truncated: false,
          },
        },
      },
    ]);
    expect(JSON.stringify(parseCapabilityArtifactDisplays(artifacts))).not.toContain('不得展示的原始返回');
  });

  it('keeps every valid MCP business view regardless of character or UTF-8 byte length', () => {
    const structuredTail = 'STRUCTURED-END';
    const textTail = 'TEXT-END';
    const supplementalTail = 'SUPPLEMENTAL-END';
    const previewTail = 'PREVIEW-END';
    const largeStructuredValue = {
      rows: ['STRUCTURED-BEGIN', 'x'.repeat(20_100), structuredTail],
    };
    const largeText = `TEXT-BEGIN${'😀'.repeat(20_100)}${textTail}`;
    const largeSupplemental = `SUPPLEMENTAL-BEGIN${'😀'.repeat(20_100)}${supplementalTail}`;
    const largePreview = `PREVIEW-BEGIN${'p'.repeat(20_100)}${previewTail}`;

    const displays = parseCapabilityArtifactDisplays([
      artifact({
        artifact_id: 'large-structured',
        artifact_type: 'mcp_result',
        storage_ref: 'raw-structured-must-not-be-read',
        mcp_business_result: {
          schema: 'maf.mcp.business_result_view.v1',
          availability: 'ready',
          outcome: 'succeeded',
          primary: { kind: 'structured', value: largeStructuredValue, truncated: false },
          projection_truncated: false,
        },
      }),
      artifact({
        artifact_id: 'large-text',
        artifact_type: 'mcp_result',
        storage_ref: 'raw-text-must-not-be-read',
        mcp_business_result: {
          schema: 'maf.mcp.business_result_view.v1',
          availability: 'ready',
          outcome: 'succeeded',
          primary: { kind: 'text', text: largeText, truncated: false },
          projection_truncated: false,
        },
      }),
      artifact({
        artifact_id: 'large-supplemental',
        artifact_type: 'mcp_result',
        storage_ref: 'raw-supplemental-must-not-be-read',
        mcp_business_result: {
          schema: 'maf.mcp.business_result_view.v1',
          availability: 'ready',
          outcome: 'succeeded',
          primary: { kind: 'structured', value: { answer: 42 }, truncated: false },
          supplemental_texts: [largeSupplemental],
          projection_truncated: false,
        },
      }),
      artifact({
        artifact_id: 'large-backend-preview',
        artifact_type: 'mcp_result',
        storage_ref: 'raw-preview-must-not-be-read',
        mcp_business_result: {
          schema: 'maf.mcp.business_result_view.v1',
          availability: 'ready',
          outcome: 'succeeded',
          primary: { kind: 'structured_preview', preview: largePreview, truncated: true },
          projection_truncated: true,
        },
      }),
    ]);

    expect(displays).toHaveLength(4);
    expect(displays.every((display) => (
      display.kind === 'mcp_business_result' && display.result.result.availability === 'ready'
    ))).toBe(true);
    expect(displays[0]).toMatchObject({
      kind: 'mcp_business_result',
      result: { result: { primary: { kind: 'structured', value: largeStructuredValue } } },
    });
    expect(displays[1]).toMatchObject({
      kind: 'mcp_business_result',
      result: {
        result: {
          primary: { kind: 'text', text: expect.stringContaining(textTail) },
        },
      },
    });
    expect(displays[2]).toMatchObject({
      kind: 'mcp_business_result',
      result: {
        result: {
          supplemental_texts: [expect.stringContaining(supplementalTail)],
        },
      },
    });
    expect(displays[3]).toMatchObject({
      kind: 'mcp_business_result',
      result: {
        result: {
          primary: { kind: 'structured_preview', preview: expect.stringContaining(previewTail), truncated: true },
          projection_truncated: true,
        },
      },
    });
    expect(JSON.stringify(displays)).not.toContain('must-not-be-read');
  });

  it('still rejects an invalid large MCP business DTO without reading raw storage', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({
        artifact_type: 'mcp_result',
        storage_ref: 'raw-invalid-must-not-be-read',
        mcp_business_result: {
          schema: 'maf.mcp.business_result_view.v1',
          availability: 'ready',
          outcome: 'succeeded',
          primary: { kind: 'structured', value: { invalid: Number.NaN }, truncated: false },
          projection_truncated: false,
        },
      }),
    ]);

    expect(displays[0]).toMatchObject({
      kind: 'mcp_business_result',
      result: { result: { availability: 'unavailable', unavailable_reason: 'projection_invalid' } },
    });
    expect(JSON.stringify(displays)).not.toContain('must-not-be-read');
  });

  it('does not infer MCP semantics from an artifact id prefix and safely closes invalid DTOs', () => {
    const prefixedText = artifact({
      artifact_id: `mcp-result-artifact:v1:${'a'.repeat(64)}`,
      artifact_type: 'text',
      storage_ref: '普通文本',
    });
    expect(parseAssistantTextArtifact([prefixedText])).toBe('普通文本');
    expect(parseCapabilityArtifactDisplays([prefixedText])).toEqual([]);

    const invalid = artifact({
      artifact_type: 'mcp_result',
      storage_ref: 'raw-secret',
      mcp_business_result: {
        schema: 'maf.mcp.business_result_view.v1',
        availability: 'ready',
        outcome: 'succeeded',
        primary: { kind: 'text', text: 'business text', truncated: false },
        projection_truncated: false,
        unexpected: 'not allowed',
      } as never,
    });
    const displays = parseCapabilityArtifactDisplays([invalid]);
    expect(displays[0]).toMatchObject({
      kind: 'mcp_business_result',
      result: { result: { availability: 'unavailable', unavailable_reason: 'projection_invalid' } },
    });
    expect(JSON.stringify(displays)).not.toContain('raw-secret');
  });

  it('does not turn unrelated capability summaries into data-query cards', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({
        artifact_id: 'task-1:result_summary:abc',
        producer_node_id: 'task-1:weather.respond',
        artifact_type: 'summary',
        storage_ref: JSON.stringify({ summary: '天气结果' }),
        summary: '天气结果',
      }),
    ]);

    expect(displays).toEqual([]);
  });

  it('does not infer data-query semantics from an opaque producer node id', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({
        artifact_id: 'generic-summary:1',
        producer_node_id: 'task-1:plan:v1:p0:data_query:0123456789abcdef0123',
        artifact_type: 'summary',
        storage_ref: JSON.stringify({ summary: '普通能力结果' }),
        summary: '普通能力结果',
      }),
    ]);

    expect(displays).toEqual([]);
  });

  it('does not let unrelated summaries contaminate data-query display summaries', () => {
    const displays = parseCapabilityArtifactDisplays([
      artifact({
        artifact_id: 'weather-summary:1',
        producer_node_id: 'task-1:weather.respond',
        artifact_type: 'summary',
        storage_ref: JSON.stringify({ summary: '天气结果' }),
        summary: '天气结果',
      }),
      artifact({
        artifact_id: 'task-1:filtered_query_result:def',
        producer_node_id: 'task-1:filter_results',
        storage_ref: JSON.stringify({
          columns: ['variety_name'],
          rows: [],
          row_count: 0,
        }),
      }),
    ]);

    expect(summarizeCapabilityArtifactDisplays(displays)).toBe('数据查询已完成，共返回 0 行结果。');
    expect(JSON.stringify(displays)).not.toContain('天气结果');
  });
});
