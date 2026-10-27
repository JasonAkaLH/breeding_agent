import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ConfigProvider } from 'antd';
import type { KnowledgeApi } from '../api/client';
import { KnowledgeSettingsPanel } from './KnowledgeSettingsPanel';

const library = { library_id: 'lib', owner_username: 'alice', name: '稻种文献', description: '研究资料', role: 'owner' as const, revision: 1, index_revision: 1, status: 'active' };
const metadata = { title: '耐旱研究', authors: ['张三'], year: 2024, doi: '10.1/paper', keywords: ['耐旱'], summary: '完整研究的实验与发现' };
const document = { ...metadata, document_id: 'doc', library_id: 'lib', status: 'active', filename: 'paper.md', sha256: 'abc', revision: 2 };
const draft = { ...metadata, import_id: 'imp', library_id: 'lib', status: 'awaiting_review', filename: 'draft.md', sha256: 'def', revision: 3 };
function makeApi(overrides: Partial<KnowledgeApi> = {}) {
  return {
    listKnowledgeLibraries: vi.fn(async () => ({ libraries: [library] })),
    createKnowledgeLibrary: vi.fn(async () => library), patchKnowledgeLibrary: vi.fn(async () => library), deleteKnowledgeLibrary: vi.fn(async () => ({ ...library, status: 'cleanup_pending' })),
    listKnowledgeDocuments: vi.fn(async () => ({ documents: [document] })), patchKnowledgeDocument: vi.fn(async () => document), deleteKnowledgeDocument: vi.fn(async () => ({ ...document, status: 'cleanup_pending' })),
    listKnowledgeImports: vi.fn(async () => ({ imports: [draft] })), uploadKnowledgeImports: vi.fn(async () => ({ results: [{ filename: 'new.md', status: 'queued' }, { filename: 'bad.md', status: 'failed', error: '不是 UTF-8' }, { document_id: 'existing', status: 'active', duplicate_type: 'document' }] })),
    patchKnowledgeImport: vi.fn(async () => draft), confirmKnowledgeImport: vi.fn(async () => ({ ...draft, status: 'publishing' })),
    confirmKnowledgeImports: vi.fn(async () => ({ results: [{ import_id: 'imp', status: 'published' }, { import_id: 'failed', status: 'failed', error: '字段缺失' }] })),
    retryKnowledgeImport: vi.fn(async () => draft), discardKnowledgeImport: vi.fn(async () => undefined), readKnowledgeContent: vi.fn(async () => '# 完整原文'), downloadKnowledgeSource: vi.fn(async () => undefined), downloadKnowledgeDocument: vi.fn(async () => undefined),
    listKnowledgeGrants: vi.fn(async () => ({ grants: [{ grant_id: 'grant', library_id: 'lib', subject_type: 'user', subject_id: 'bob', role: 'reader' }] })),
    createKnowledgeGrant: vi.fn(async () => ({})), deleteKnowledgeGrant: vi.fn(async () => undefined),
    ...overrides,
  } as unknown as KnowledgeApi;
}
async function openLibrary(api: KnowledgeApi) {
  render(<ConfigProvider theme={{ token: { motion: false } }}><KnowledgeSettingsPanel api={api} /></ConfigProvider>);
  fireEvent.click(await screen.findByRole('button', { name: '稻种文献' }));
  await screen.findByRole('heading', { name: '已入库文献' });
}

describe('KnowledgeSettingsPanel', () => {
  it.each(['save', 'cancel'])('removes the library dialog before uploads can resume after %s', async (action) => {
    const api = makeApi();
    const view = render(<ConfigProvider theme={{ token: { motion: true } }}><KnowledgeSettingsPanel api={api} /></ConfigProvider>);
    fireEvent.click(await screen.findByRole('button', { name: '稻种文献' }));
    await screen.findByRole('heading', { name: '已入库文献' });
    const trigger = screen.getByRole('button', { name: '创建文献库' });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole('dialog', { name: '创建文献库' });
    const input = within(dialog).getByLabelText('文献库名称');
    input.focus();
    fireEvent.change(input, { target: { value: '新文献库' } });
    vi.useFakeTimers();
    try {
      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: action === 'save' ? 'OK' : 'Cancel' }));
      });
      expect(screen.getByRole('button', { name: '上传文献' })).toBeEnabled();
      expect(dialog).not.toBeInTheDocument();
      expect(globalThis.document.querySelector('.ant-modal-wrap')).toBeNull();
      expect(trigger).toHaveFocus();
      expect(api.uploadKnowledgeImports).not.toHaveBeenCalled();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it('separates PDF source downloads, Markdown export and text preview for a reader', async () => {
    const api = makeApi({
      listKnowledgeLibraries: vi.fn(async () => ({ libraries: [{ ...library, role: 'reader' }] })),
      listKnowledgeDocuments: vi.fn(async () => ({ documents: [{ ...document, filename: 'paper.pdf', source_format: 'pdf' }] })),
    });
    await openLibrary(api);
    fireEvent.click(screen.getByRole('button', { name: '下载原 PDF' }));
    await waitFor(() => expect(api.downloadKnowledgeSource).toHaveBeenCalledWith('lib', 'documents', 'doc', 'paper.pdf'));
    await waitFor(() => expect(screen.getByRole('button', { name: '导出 Markdown' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '导出 Markdown' }));
    await waitFor(() => expect(api.downloadKnowledgeDocument).toHaveBeenCalledWith('lib', 'doc', 'paper.md'));
    expect(screen.queryByRole('button', { name: '下载 Markdown' })).not.toBeInTheDocument();
  });

  it('blocks confirmation and metadata editing until PDF text is ready, while retaining source and retry', async () => {
    const pdf = { ...draft, filename: 'scan.pdf', source_format: 'pdf' as const, status: 'failed', text_ready: false, error_code: 'pdf_page_text_unavailable' };
    const api = makeApi({ listKnowledgeDocuments: vi.fn(async () => ({ documents: [] })), listKnowledgeImports: vi.fn(async () => ({ imports: [pdf] })) });
    await openLibrary(api);
    expect(screen.getByRole('button', { name: '确认入库' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '编辑审核字段' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '预览提取正文' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '确认所选文献' })).toBeDisabled();
    expect(screen.getByText(/部分页面无法提取文字/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下载原 PDF' }));
    await waitFor(() => expect(api.downloadKnowledgeSource).toHaveBeenCalledWith('lib', 'imports', 'imp', 'scan.pdf'));
    await waitFor(() => expect(screen.getByRole('button', { name: '重试提取' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '重试提取' }));
    await waitFor(() => expect(api.retryKnowledgeImport).toHaveBeenCalledWith('lib', 'imp'));
  });

  it('allows manual review when PDF text is ready after metadata extraction failed', async () => {
    const api = makeApi({ listKnowledgeImports: vi.fn(async () => ({ imports: [{ ...draft, source_format: 'pdf', status: 'failed', text_ready: true, error_code: 'extraction_failed' }] })) });
    await openLibrary(api);
    expect(screen.getByRole('button', { name: '确认入库' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '编辑审核字段' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '预览提取正文' })).toBeEnabled();
  });

  it('shows publication and cleanup work without claiming the document is readable', async () => {
    const api = makeApi({ listKnowledgeDocuments: vi.fn(async () => ({ documents: [{ ...document, publication_state: 'pending', cleanup_pending: true, error_code: 'cleanup_failed' }] })) });
    await openLibrary(api);
    expect(screen.getByRole('button', { name: '阅读全文' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下载 Markdown' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '替换正文' })).toBeDisabled();
    expect(screen.getByText('发布未完成')).toBeInTheDocument();
    expect(screen.getByText('清理未完成')).toBeInTheDocument();
    expect(screen.getByText('cleanup_failed')).toBeInTheDocument();
  });
  it('creates and edits library metadata, then preserves visible failure details', async () => {
    const api = makeApi({ patchKnowledgeLibrary: vi.fn(async () => { throw new Error('修订冲突，请刷新'); }) });
    await openLibrary(api);
    fireEvent.click(screen.getByRole('button', { name: '创建文献库' }));
    const create = screen.getByRole('dialog', { name: '创建文献库' });
    fireEvent.change(within(create).getByLabelText('文献库名称'), { target: { value: '新文献库' } });
    fireEvent.click(within(create).getByRole('button', { name: 'OK' }));
    await waitFor(() => expect(api.createKnowledgeLibrary).toHaveBeenCalledWith({ name: '新文献库', description: '' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '编辑文献库' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '编辑文献库' }));
    const edit = screen.getByRole('dialog', { name: '编辑文献库' });
    fireEvent.change(within(edit).getByLabelText('描述'), { target: { value: '新研究范围' } });
    fireEvent.click(within(edit).getByRole('button', { name: 'OK' }));
    await waitFor(() => expect(api.patchKnowledgeLibrary).toHaveBeenCalledWith('lib', { name: '稻种文献', description: '新研究范围', revision: 1 }));
    expect(await within(edit).findByText('修订冲突，请刷新')).toBeInTheDocument();
  });

  it('supports failed extraction retry and manual draft editing without bypassing confirmation', async () => {
    const api = makeApi({ listKnowledgeImports: vi.fn(async () => ({ imports: [{ ...draft, status: 'failed', title: '', summary: '', error_code: 'model_unavailable' }] })) });
    await openLibrary(api);
    expect(await screen.findByText('model_unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认入库' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '重试提取' }));
    await waitFor(() => expect(api.retryKnowledgeImport).toHaveBeenCalledWith('lib', 'imp'));
    await waitFor(() => expect(screen.getByRole('button', { name: '编辑审核字段' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '编辑审核字段' }));
    const edit = screen.getByRole('dialog', { name: '审核文献' });
    fireEvent.change(within(edit).getByLabelText('标题'), { target: { value: '手工标题' } });
    fireEvent.click(within(edit).getByRole('button', { name: /保\s*存/ }));
    await waitFor(() => expect(api.patchKnowledgeImport).toHaveBeenCalledWith('lib', 'imp', { metadata: { ...metadata, title: '手工标题', summary: '' }, revision: 3 }));
    expect(api.confirmKnowledgeImport).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '审核文献' })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('button', { name: '丢弃草稿' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '丢弃草稿' }));
    const discard = await screen.findByRole('dialog', { name: '丢弃导入草稿' });
    fireEvent.click(within(discard).getByRole('button', { name: /确\s*认/ }));
    await waitFor(() => expect(api.discardKnowledgeImport).toHaveBeenCalledWith('lib', 'imp', 3));
  });
  it('keeps shared readers away from drafts, grants, and all maintenance controls', async () => {
    const api = makeApi({ listKnowledgeLibraries: vi.fn(async () => ({ libraries: [{ ...library, role: 'reader' }] })) });
    await openLibrary(api);
    expect(screen.getByRole('region', { name: '共享给我' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑文献库' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '上传文献' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑元数据' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '导入审核' })).not.toBeInTheDocument();
    expect(api.listKnowledgeImports).not.toHaveBeenCalled();
    expect(api.listKnowledgeGrants).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '阅读全文' }));
    const preview = await screen.findByRole('dialog', { name: '耐旱研究' });
    expect(within(preview).getByRole('heading', { name: '完整原文' })).toBeInTheDocument();
    expect(api.readKnowledgeContent).toHaveBeenCalledWith('lib', 'documents', 'doc');
    fireEvent.click(within(preview).getByRole('button', { name: 'Close' }));
    fireEvent.click(screen.getByRole('button', { name: '下载 Markdown' }));
    await waitFor(() => expect(api.downloadKnowledgeDocument).toHaveBeenCalledWith('lib', 'doc', 'paper.md'));
  });

  it('supports batch uploads and shows each accepted or failed item accurately', async () => {
    const api = makeApi(); await openLibrary(api);
    const files = [new File(['# body'], 'new.md'), new File(['bad'], 'bad.md')];
    fireEvent.change(screen.getByLabelText('批量上传文献'), { target: { files } });
    await waitFor(() => expect(api.uploadKnowledgeImports).toHaveBeenCalledWith('lib', files));
    expect(await screen.findByText('new.md：待处理')).toBeInTheDocument();
    expect(screen.getByText('不是 UTF-8')).toBeInTheDocument();
    expect(screen.getByText('existing：重复，已复用')).toBeInTheDocument();
    expect(screen.queryByText('new.md：已入库')).not.toBeInTheDocument();
  });

  it('edits all review fields and confirms selected imports with their current revisions', async () => {
    const api = makeApi(); await openLibrary(api);
    fireEvent.click(await screen.findByRole('button', { name: '编辑审核字段' }));
    const modal = screen.getByRole('dialog', { name: '审核文献' });
    fireEvent.change(within(modal).getByLabelText('标题'), { target: { value: '修订标题' } });
    fireEvent.change(within(modal).getByLabelText('年份'), { target: { value: '' } });
    fireEvent.click(within(modal).getByRole('button', { name: /保\s*存/ }));
    await waitFor(() => expect(api.patchKnowledgeImport).toHaveBeenCalledWith('lib', 'imp', { metadata: { ...metadata, title: '修订标题', year: null }, revision: 3 }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '审核文献' })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 draft.md' }));
    fireEvent.click(screen.getByRole('button', { name: '确认所选文献' }));
    await waitFor(() => expect(api.confirmKnowledgeImports).toHaveBeenCalledWith('lib', [{ import_id: 'imp', revision: 3 }]));
    expect(await screen.findByText('字段缺失')).toBeInTheDocument();
  });

  it('uses an explicit replacement target and preserves the human review step', async () => {
    const api = makeApi(); await openLibrary(api);
    fireEvent.click(screen.getByRole('button', { name: '替换正文' }));
    expect(screen.getByText(/确认前原正文仍可用/)).toBeInTheDocument();
    const file = new File(['# new body'], 'replacement.md');
    fireEvent.change(screen.getByLabelText('上传替换文献'), { target: { files: [file] } });
    await waitFor(() => expect(api.uploadKnowledgeImports).toHaveBeenCalledWith('lib', [file], 'doc'));
    expect(api.confirmKnowledgeImport).not.toHaveBeenCalled();
  });

  it('shares to existing users, revokes access, and confirms destructive document removal', async () => {
    const api = makeApi(); await openLibrary(api);
    fireEvent.change(screen.getByLabelText('共享用户名'), { target: { value: 'carol' } });
    fireEvent.click(screen.getByRole('button', { name: '授予只读共享' }));
    await waitFor(() => expect(api.createKnowledgeGrant).toHaveBeenCalledWith('lib', 'carol'));
    await waitFor(() => expect(screen.getByRole('button', { name: '撤销 bob' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '撤销 bob' }));
    await waitFor(() => expect(api.deleteKnowledgeGrant).toHaveBeenCalledWith('lib', 'grant'));
    await waitFor(() => expect(screen.getByRole('button', { name: '删除文献' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '删除文献' }));
    expect(api.deleteKnowledgeDocument).not.toHaveBeenCalled();
    const modal = screen.getByRole('dialog', { name: '删除文献' });
    expect(within(modal).getByText(/历史回答保持原样/)).toBeInTheDocument();
    fireEvent.click(within(modal).getByRole('button', { name: /确\s*认/ }));
    await waitFor(() => expect(api.deleteKnowledgeDocument).toHaveBeenCalledWith('lib', 'doc', 2));
  });

  it('rejects a late preview after changing libraries', async () => {
    let resolve!: (text: string) => void;
    const pending = new Promise<string>((done) => { resolve = done; });
    const api = makeApi({ listKnowledgeLibraries: vi.fn(async () => ({ libraries: [library, { ...library, library_id: 'other', name: '其他文献' }] })), readKnowledgeContent: vi.fn(() => pending) });
    await openLibrary(api);
    fireEvent.click(screen.getByRole('button', { name: '阅读全文' }));
    fireEvent.click(screen.getByRole('button', { name: '其他文献' }));
    await act(async () => resolve('# 不应展示旧文献'));
    expect(screen.queryByText('不应展示旧文献')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
