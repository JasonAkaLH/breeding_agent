import { describe, expect, it, vi } from 'vitest';
import { createApiClient } from './client';

const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });

describe('knowledge API client', () => {
  it('downloads authenticated raw bytes without normalizing CRLF or putting credentials in a URL', async () => {
    const original = new TextEncoder().encode('\ufeff# 原始正文\r\n\r\n');
    const fetcher = vi.fn(async () => new Response(original));
    const createObjectURL = vi.fn(() => 'blob:knowledge');
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const previousCreate = URL.createObjectURL;
    const previousRevoke = URL.revokeObjectURL;
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
    try {
      const api = createApiClient({ fetcher, accessToken: 'secret' });
      await api.downloadKnowledgeDocument('lib', 'doc', 'original.md');
      expect(fetcher).toHaveBeenCalledWith('/api/v1/knowledge/libraries/lib/documents/doc/content?download=true', expect.objectContaining({ headers: { Authorization: 'Bearer secret' } }));
      const blob = createObjectURL.mock.calls[0][0] as Blob;
      expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual(Array.from(original));
      expect(click).toHaveBeenCalledOnce();
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:knowledge');
      await api.downloadKnowledgeSource('lib/a', 'documents', 'doc/a', 'original.pdf');
      await api.downloadKnowledgeSource('lib/a', 'imports', 'imp/a', 'draft.pdf');
      expect(fetcher).toHaveBeenNthCalledWith(2, '/api/v1/knowledge/libraries/lib%2Fa/documents/doc%2Fa/source', expect.objectContaining({ headers: { Authorization: 'Bearer secret' } }));
      expect(fetcher).toHaveBeenNthCalledWith(3, '/api/v1/knowledge/libraries/lib%2Fa/imports/imp%2Fa/source', expect.objectContaining({ headers: { Authorization: 'Bearer secret' } }));
    } finally {
      URL.createObjectURL = previousCreate;
      URL.revokeObjectURL = previousRevoke;
      click.mockRestore();
    }
  });
  it('authenticates raw previews and reads index text from the JSON contract', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ text: '# 索引', index_revision: 3, library_id: 'lib' })).mockResolvedValueOnce(new Response('# 正文'));
    const api = createApiClient({ fetcher, accessToken: 'secret', baseUrl: '/prefix' });
    expect(await api.readKnowledgeContent('lib', 'index')).toBe('# 索引');
    expect(await api.readKnowledgeContent('lib', 'documents', 'doc')).toBe('# 正文');
    expect(fetcher).toHaveBeenNthCalledWith(1, '/prefix/api/v1/knowledge/libraries/lib/index', expect.objectContaining({ credentials: 'same-origin', headers: { Authorization: 'Bearer secret' } }));
    expect(fetcher).toHaveBeenNthCalledWith(2, '/prefix/api/v1/knowledge/libraries/lib/documents/doc/content', expect.objectContaining({ headers: { Authorization: 'Bearer secret' } }));
  });

  it('uploads original files as multipart with an explicit replacement ID and no forced JSON header', async () => {
    const outcomes = [{ filename: 'new.md', status: 'pending' }, { filename: 'invalid.md', status: 'failed', error: '不是 UTF-8' }, { document_id: 'existing', status: 'active', duplicate_type: 'document' }];
    const fetcher = vi.fn(async () => json({ imports: outcomes }));
    const api = createApiClient({ fetcher, accessToken: 'secret' });
    const file = new File(['# original\r\n'], 'file.md');
    expect(await api.uploadKnowledgeImports('lib', [file], 'doc')).toEqual({ results: outcomes });
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect(init.headers).toEqual({ Authorization: 'Bearer secret' });
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).getAll('files')).toEqual([file]);
    expect((init.body as FormData).get('replacement_document_id')).toBe('doc');
  });

  it('saves conversation selection without permitting client authority fields', async () => {
    const fetcher = vi.fn(async () => json({ mode: 'selected', library_ids: ['lib'] }));
    const api = createApiClient({ fetcher, accessToken: 'secret' });
    await api.saveKnowledgeSelection('conv/a', { mode: 'selected', library_ids: ['lib'], revision: 5, unavailable_library_ids: ['old'] });
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/conversations/conv%2Fa/knowledge-selection');
    expect(JSON.parse((fetcher.mock.calls[0][1] as RequestInit).body as string)).toEqual({ mode: 'selected', library_ids: ['lib'] });
  });

  it('propagates authorization failure on raw reads to the existing sign-in boundary', async () => {
    const unauthorized = vi.fn();
    const actual = createApiClient({ fetcher: vi.fn(async () => new Response(JSON.stringify({ detail: 'expired' }), { status: 401 })), onUnauthorized: unauthorized });
    await expect(actual.readKnowledgeContent('lib', 'documents', 'doc')).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).toHaveBeenCalledOnce();
  });
});
