import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Input, Modal, Space, Tag, Typography } from 'antd';
import type { KnowledgeApi } from '../api/client';
import type { KnowledgeBatchResult, KnowledgeDocument, KnowledgeGrant, KnowledgeImport, KnowledgeLibrary, KnowledgeMetadata } from '../api/knowledgeTypes';
import { MarkdownText } from './MarkdownText';

interface Props { api: KnowledgeApi; onLibrariesChanged?(): void }
type Editable = { kind: 'import'; record: KnowledgeImport } | { kind: 'document'; record: KnowledgeDocument };
interface MetadataForm { title: string; authors: string; year: string; doi: string; keywords: string; summary: string }
const blankMetadata: MetadataForm = { title: '', authors: '', year: '', doi: '', keywords: '', summary: '' };
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试。';
const statusLabels: Record<string, string> = {
  uploading: '上传中', pending: '待处理', queued: '待处理', extracting: '处理中', processing: '处理中', awaiting_review: '待确认', ready: '待确认', draft: '待确认',
  failed: '失败', published: '已入库', active: '可用', publishing: '发布未完成', cleanup_pending: '清理未完成', deleting: '清理未完成',
  deleted: '已删除', discarded: '已丢弃', duplicate: '重复，已复用', completed: '已完成', index_pending: '索引待修复',
};
const statusLabel = (status: string) => statusLabels[status] ?? status;
const errorLabels: Record<string, string> = {
  pdf_unreadable: 'PDF 损坏或无法读取，请上传可读文件。', pdf_encrypted: '暂不支持加密 PDF，请上传未加密原件。',
  pdf_no_text: 'PDF 没有可提取文字，扫描版暂不处理。', pdf_page_text_unavailable: '部分页面无法提取文字，扫描页和纯图形页暂不处理，请对照原件。',
  pdf_limit_exceeded: 'PDF 超过解析资源限制，请改传较小的文献。', knowledge_text_not_ready: '提取正文尚未就绪，请等待处理或重试。',
  knowledge_format_unsupported: '请选择 Markdown 或 PDF 文件。', knowledge_file_too_large: '单文件不能超过 20 MiB。',
  extraction_failed: '元数据提取失败；正文就绪时可手工完善审核字段或重试。', knowledge_file_unavailable: '文件存储暂不可用，请稍后重试。',
};
const errorLabel = (code?: string | null) => code ? errorLabels[code] ?? code : '';
const reviewable = (item: KnowledgeImport) => ['awaiting_review', 'failed', 'ready', 'draft'].includes(item.status) && item.text_ready !== false;
const importEditable = (item: KnowledgeImport) => ['uploading', 'pending', 'queued', 'extracting', 'processing', 'awaiting_review', 'ready', 'draft', 'failed'].includes(item.status);

export function KnowledgeSettingsPanel({ api, onLibrariesChanged }: Props) {
  const [libraries, setLibraries] = useState<KnowledgeLibrary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [imports, setImports] = useState<KnowledgeImport[]>([]);
  const [grants, setGrants] = useState<KnowledgeGrant[]>([]);
  const [checked, setChecked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<KnowledgeBatchResult | null>(null);
  const [libraryForm, setLibraryForm] = useState<{ id: string | null; revision: number; name: string; description: string } | null>(null);
  const [editing, setEditing] = useState<Editable | null>(null);
  const [metadata, setMetadata] = useState<MetadataForm>(blankMetadata);
  const [preview, setPreview] = useState<{ title: string; text: string } | null>(null);
  const [sharing, setSharing] = useState('');
  const [replacement, setReplacement] = useState<KnowledgeDocument | null>(null);
  const [confirmation, setConfirmation] = useState<{ title: string; description: string; run: () => Promise<unknown> } | null>(null);
  const mounted = useRef(true);
  const selectedRef = useRef(selectedId);
  const requestGeneration = useRef(0);
  const filesRef = useRef<HTMLInputElement>(null);
  const replacementRef = useRef<HTMLInputElement>(null);
  selectedRef.current = selectedId;
  const library = libraries.find((item) => item.library_id === selectedId) ?? null;
  const owner = library?.role === 'owner';

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; requestGeneration.current += 1; }; }, []);
  const refresh = useCallback(async (target = selectedRef.current) => {
    const generation = ++requestGeneration.current;
    try {
      const listed = await api.listKnowledgeLibraries();
      if (!mounted.current || generation !== requestGeneration.current || target !== selectedRef.current) return;
      setLibraries(listed.libraries);
      const current = listed.libraries.find((item) => item.library_id === target);
      if (!current) { setDocuments([]); setImports([]); setGrants([]); return; }
      if (current.status !== 'active') { setDocuments([]); setImports([]); setGrants([]); return; }
      const [docs, drafts, shares] = await Promise.all([
        api.listKnowledgeDocuments(current.library_id),
        current.role === 'owner' ? api.listKnowledgeImports(current.library_id) : Promise.resolve({ imports: [] }),
        current.role === 'owner' ? api.listKnowledgeGrants(current.library_id) : Promise.resolve({ grants: [] }),
      ]);
      if (!mounted.current || generation !== requestGeneration.current || target !== selectedRef.current) return;
      setDocuments(docs.documents); setImports(drafts.imports); setGrants(shares.grants);
      setChecked((old) => old.filter((id) => drafts.imports.some((item) => item.import_id === id && importEditable(item))));
    } catch (failure) {
      if (mounted.current && generation === requestGeneration.current && target === selectedRef.current) setError(message(failure));
    }
  }, [api]);

  useEffect(() => {
    setError(null); setResults(null); setChecked([]); setDocuments([]); setImports([]); setGrants([]);
    setEditing(null); setPreview(null); setReplacement(null); setConfirmation(null); setSharing('');
    void refresh(selectedId);
  }, [selectedId, refresh]);

  // Poll persisted processing state; no separate event protocol or background request after closing the panel.
  useEffect(() => {
    if (!selectedId || !owner) return;
    const timer = window.setInterval(() => { if (!busy) void refresh(); }, 3000);
    return () => window.clearInterval(timer);
  }, [selectedId, owner, busy, refresh]);

  async function run(operation: () => Promise<unknown>, after?: () => void) {
    if (busy) return;
    const target = selectedRef.current;
    setBusy(true); setError(null); setResults(null);
    try {
      const result = await operation();
      if (!mounted.current || target !== selectedRef.current) return;
      if (result && typeof result === 'object' && 'results' in result) setResults(result as KnowledgeBatchResult);
      after?.();
      await refresh(target);
      onLibrariesChanged?.();
    } catch (failure) {
      if (mounted.current && target === selectedRef.current) setError(message(failure));
    } finally { if (mounted.current) setBusy(false); }
  }

  function chooseLibrary(id: string) {
    selectedRef.current = id;
    requestGeneration.current += 1;
    setSelectedId(id);
  }
  function editMetadata(item: Editable) {
    setEditing(item); setError(null);
    setMetadata({ title: item.record.title ?? '', authors: (item.record.authors ?? []).join(', '), year: item.record.year == null ? '' : String(item.record.year), doi: item.record.doi ?? '', keywords: (item.record.keywords ?? []).join(', '), summary: item.record.summary ?? '' });
  }
  async function saveMetadata() {
    if (!editing || !selectedId) return;
    if (editing.kind === 'document' && (!metadata.title.trim() || !metadata.summary.trim())) { setError('标题和检索简述不能为空。'); return; }
    if (metadata.year && !/^\d{1,4}$/.test(metadata.year)) { setError('年份应为 1 至 4 位整数，或留空。'); return; }
    const value: KnowledgeMetadata = {
      title: metadata.title.trim(), summary: metadata.summary.trim(), year: metadata.year ? Number(metadata.year) : null,
      doi: metadata.doi.trim() || null, authors: metadata.authors.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
      keywords: metadata.keywords.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    };
    await run(() => editing.kind === 'import'
      ? api.patchKnowledgeImport(selectedId, editing.record.import_id, { metadata: value, revision: editing.record.revision })
      : api.patchKnowledgeDocument(selectedId, editing.record.document_id, { metadata: value, revision: editing.record.revision }), () => setEditing(null));
  }
  async function showContent(resource: 'index' | 'documents' | 'imports', id: string | undefined, title: string) {
    if (!selectedId) return;
    const target = selectedId;
    await run(async () => {
      const text = await api.readKnowledgeContent(target, resource, id);
      if (mounted.current && target === selectedRef.current) setPreview({ title, text });
    });
  }
  function requireDelete(title: string, description: string, operation: () => Promise<unknown>) {
    setConfirmation({ title, description, run: operation });
  }

  return (
    <Space direction="vertical" size="middle" className="knowledge-settings">
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Space wrap>
        <Button type="primary" onClick={() => setLibraryForm({ id: null, revision: 0, name: '', description: '' })} disabled={busy}>创建文献库</Button>
        <Button onClick={() => void refresh()} disabled={busy}>刷新</Button>
      </Space>
      {(['owner', 'reader'] as const).map((role) => (
        <section key={role} aria-label={role === 'owner' ? '我的文献库' : '共享给我'}>
          <Typography.Title level={5}>{role === 'owner' ? '我的文献库' : '共享给我'}</Typography.Title>
          <Space wrap>{libraries.filter((item) => item.role === role).map((item) => <Button key={item.library_id} type={selectedId === item.library_id ? 'primary' : 'default'} onClick={() => chooseLibrary(item.library_id)}>{item.name}</Button>)}</Space>
          {!libraries.some((item) => item.role === role) ? <Typography.Text type="secondary">暂无文献库</Typography.Text> : null}
        </section>
      ))}
      {library ? <Card title={library.name}>
        <Space direction="vertical" size="middle" className="knowledge-settings">
          <Typography.Paragraph>{library.description || '尚未填写描述，补充描述有助于模型选库。'}</Typography.Paragraph>
          <Space wrap><Tag>{owner ? '所有者' : '只读'}</Tag><Tag>{statusLabel(library.status)}</Tag>{library.index_status ? <Tag>索引：{statusLabel(library.index_status)}</Tag> : null}{library.cleanup_status ? <Tag>清理：{statusLabel(library.cleanup_status)}</Tag> : null}</Space>
          {library.published_index_revision !== undefined && library.published_index_revision < library.index_revision ? <Tag>索引待修复</Tag> : null}
          {library.cleanup_pending ? <Tag>清理未完成</Tag> : null}
          {library.publication_state && library.publication_state !== 'complete' ? <Tag>发布：{statusLabel(library.publication_state)}</Tag> : null}
          {library.error || library.error_code ? <Alert type="warning" message={library.error ?? library.error_code} /> : null}
          <Space wrap>
            <Button disabled={busy || library.status !== 'active'} onClick={() => void showContent('index', undefined, `${library.name} 索引`)}>查看 Markdown 索引</Button>
            {owner ? <>
              <Button disabled={busy || library.status !== 'active'} onClick={() => setLibraryForm({ id: library.library_id, revision: library.revision, name: library.name, description: library.description })}>编辑文献库</Button>
              <Button disabled={busy || library.status !== 'active'} onClick={() => filesRef.current?.click()}>上传文献</Button>
              <Button danger disabled={busy} onClick={() => requireDelete('删除文献库', '删除后立即停止读取并撤销共享，随后清理所有正文和草稿。清理未完成时会保留可恢复状态；历史回答保持原样。', () => api.deleteKnowledgeLibrary(library.library_id, library.revision))}>删除文献库</Button>
            </> : null}
          </Space>
          <Typography.Title level={5}>已入库文献</Typography.Title>
          {documents.length === 0 ? <Typography.Text type="secondary">暂无已入库文献</Typography.Text> : null}
          {documents.map((document) => {
            const readable = document.status === 'active' && (!document.publication_state || document.publication_state === 'complete');
            return <Card key={document.document_id} size="small" title={document.title}>
            <Space direction="vertical" className="knowledge-settings">
              <Typography.Text>{document.authors.join('、')}{document.year ? ` · ${document.year}` : ''}</Typography.Text>
              {document.doi ? <Typography.Text>DOI：{document.doi}</Typography.Text> : null}
              {document.keywords.length ? <Typography.Text>关键词：{document.keywords.join('、')}</Typography.Text> : null}
              <Typography.Paragraph>{document.summary}</Typography.Paragraph>
              <Space wrap><Tag>{document.source_format === 'pdf' ? 'PDF' : 'Markdown'}</Tag><Tag>{statusLabel(document.status === 'active' && !readable ? 'publishing' : document.status)}</Tag>{document.cleanup_status ? <Tag>{statusLabel(document.cleanup_status)}</Tag> : null}</Space>
              {document.cleanup_pending ? <Tag>清理未完成</Tag> : null}
              {document.publication_state && document.publication_state !== 'complete' ? <Tag>发布：{statusLabel(document.publication_state)}</Tag> : null}
              {document.error || document.error_code ? <Alert type="warning" message={document.error ?? document.error_code} /> : null}
              <Space wrap>
                <Button disabled={busy || !readable} onClick={() => void showContent('documents', document.document_id, document.title)}>阅读全文</Button>
                <Button disabled={busy || !readable} onClick={() => void run(() => api.downloadKnowledgeDocument(library.library_id, document.document_id, document.source_format === 'pdf' ? document.filename.replace(/\.pdf$/i, '.md') : document.filename || `${document.title}.md`))}>{document.source_format === 'pdf' ? '导出 Markdown' : '下载 Markdown'}</Button>
                {document.source_format === 'pdf' ? <Button disabled={busy || !readable} onClick={() => void run(() => api.downloadKnowledgeSource(library.library_id, 'documents', document.document_id, document.filename))}>下载原 PDF</Button> : null}
                {owner ? <>
                  <Button disabled={busy || !readable} onClick={() => editMetadata({ kind: 'document', record: document })}>编辑元数据</Button>
                  <Button disabled={busy || !readable} onClick={() => setReplacement(document)}>替换正文</Button>
                  <Button disabled={busy} danger onClick={() => requireDelete('删除文献', '该文献将立即退出读取范围，并清理当前正文及相关草稿。历史回答保持原样。', () => api.deleteKnowledgeDocument(library.library_id, document.document_id, document.revision))}>删除文献</Button>
                </> : null}
              </Space>
            </Space>
          </Card>;
          })}
          {owner && library.status === 'active' ? <section aria-label="导入审核">
            <Typography.Title level={5}>导入审核</Typography.Title>
            <Typography.Paragraph type="secondary">支持 Markdown 和 PDF。PDF 仅提取文字层，不识别扫描页、图片或页面视觉语义，请对照原件审核。正文就绪后仍需人工确认；标题和检索简述必填，其余字段可留空。</Typography.Paragraph>
            <Button disabled={busy || !imports.some((item) => checked.includes(item.import_id) && reviewable(item))} onClick={() => void run(() => api.confirmKnowledgeImports(library.library_id, imports.filter((item) => checked.includes(item.import_id) && reviewable(item)).map((item) => ({ import_id: item.import_id, revision: item.revision }))))}>确认所选文献</Button>
            {imports.map((item) => <Card key={item.import_id} size="small" title={item.filename}>
              <Space direction="vertical" className="knowledge-settings">
                <Space><Tag>{item.source_format === 'pdf' ? 'PDF' : 'Markdown'}</Tag><Tag>{statusLabel(item.status)}</Tag>{item.replacement_document_id ? <Tag>替换草稿</Tag> : null}</Space>
                <Typography.Text>{item.title || '标题待补全'}</Typography.Text>
                <Typography.Paragraph>{item.summary || '检索简述待补全'}</Typography.Paragraph>
                {item.cleanup_pending ? <Tag>清理未完成</Tag> : null}
                {item.publication_state && item.publication_state !== 'complete' ? <Tag>发布：{statusLabel(item.publication_state)}</Tag> : null}
                {item.error || item.error_code ? <Alert type="error" message={errorLabel(item.error ?? item.error_code)} /> : null}
                <Space wrap>
                  {reviewable(item) ? <Checkbox checked={checked.includes(item.import_id)} onChange={(event) => setChecked((old) => event.target.checked ? [...old, item.import_id] : old.filter((id) => id !== item.import_id))}>选择 {item.filename}</Checkbox> : null}
                  <Button disabled={busy || item.text_ready === false || !importEditable(item)} onClick={() => void showContent('imports', item.import_id, item.filename)}>{item.source_format === 'pdf' ? '预览提取正文' : '查看原始 Markdown'}</Button>
                  {item.source_format === 'pdf' && importEditable(item) ? <Button disabled={busy} onClick={() => void run(() => api.downloadKnowledgeSource(library.library_id, 'imports', item.import_id, item.filename))}>下载原 PDF</Button> : null}
                  {importEditable(item) ? <>
                    <Button disabled={busy || item.text_ready === false} onClick={() => editMetadata({ kind: 'import', record: item })}>编辑审核字段</Button>
                    <Button disabled={busy || !reviewable(item) || !item.title?.trim() || !item.summary?.trim()} onClick={() => void run(() => api.confirmKnowledgeImport(library.library_id, item.import_id, item.revision))}>确认入库</Button>
                    {item.status === 'failed' ? <Button disabled={busy} onClick={() => void run(() => api.retryKnowledgeImport(library.library_id, item.import_id))}>重试提取</Button> : null}
                    <Button danger disabled={busy} onClick={() => requireDelete('丢弃导入草稿', '丢弃后清理上传文件和处理临时文件，不改变已入库的当前正文。', () => api.discardKnowledgeImport(library.library_id, item.import_id, item.revision))}>丢弃草稿</Button>
                  </> : null}
                </Space>
              </Space>
            </Card>)}
          </section> : null}
          {owner && library.status === 'active' ? <section aria-label="共享管理">
            <Typography.Title level={5}>共享管理</Typography.Title>
            <Typography.Paragraph type="secondary">向系统中已有用户立即授予只读权限。</Typography.Paragraph>
            <Space><Input aria-label="共享用户名" value={sharing} onChange={(event) => setSharing(event.target.value)} placeholder="已有用户名" /><Button disabled={busy || !sharing.trim()} onClick={() => void run(() => api.createKnowledgeGrant(library.library_id, sharing.trim()), () => setSharing(''))}>授予只读共享</Button></Space>
            {grants.map((grant) => <div key={grant.grant_id}><Space><Typography.Text>{grant.subject_id}（只读）</Typography.Text><Button disabled={busy} onClick={() => void run(() => api.deleteKnowledgeGrant(library.library_id, grant.grant_id))}>撤销 {grant.subject_id}</Button></Space></div>)}
          </section> : null}
        </Space>
      </Card> : null}
      {results ? <section aria-label="逐项处理结果"><Typography.Title level={5}>逐项处理结果</Typography.Title>{results.results.map((item, index) => <Alert key={`${item.import_id ?? item.filename}-${index}`} type={item.error ? 'error' : 'info'} message={`${item.filename ?? item.import_id ?? item.document_id ?? index + 1}：${item.duplicate_type ? '重复，已复用' : statusLabel(item.status)}${item.unchanged ? '（正文未变）' : ''}`} description={errorLabel(item.error)} />)}</section> : null}
      <input ref={filesRef} className="file-input-hidden" aria-label="批量上传文献" type="file" accept=".md,.markdown,.pdf,text/markdown,application/pdf" multiple onChange={(event) => {
        const files = Array.from(event.target.files ?? []); event.target.value = '';
        if (selectedId && files.length) void run(() => api.uploadKnowledgeImports(selectedId, files));
      }} />
      <Modal destroyOnHidden title={libraryForm?.id ? '编辑文献库' : '创建文献库'} open={libraryForm !== null} confirmLoading={busy} onCancel={() => setLibraryForm(null)} onOk={() => {
        if (!libraryForm?.name.trim()) { setError('文献库名称不能为空。'); return; }
        const form = libraryForm;
        void run(() => form.id ? api.patchKnowledgeLibrary(form.id, { name: form.name.trim(), description: form.description, revision: form.revision }) : api.createKnowledgeLibrary({ name: form.name.trim(), description: form.description }), () => setLibraryForm(null));
      }}>
        {error ? <Alert type="error" message={error} /> : null}
        <label>文献库名称<Input value={libraryForm?.name ?? ''} onChange={(event) => setLibraryForm((old) => old && ({ ...old, name: event.target.value }))} /></label>
        <label>描述<Input.TextArea value={libraryForm?.description ?? ''} onChange={(event) => setLibraryForm((old) => old && ({ ...old, description: event.target.value }))} /></label>
        <Typography.Text type="secondary">描述可留空，补充研究范围有助于模型选择文献库。</Typography.Text>
      </Modal>
      <Modal destroyOnHidden title={editing?.kind === 'import' ? '审核文献' : '编辑文献元数据'} open={editing !== null} okText="保存" onCancel={() => setEditing(null)} onOk={() => void saveMetadata()} confirmLoading={busy}>
        {error ? <Alert type="error" message={error} /> : null}
        {(['title', 'authors', 'year', 'doi', 'keywords', 'summary'] as const).map((field) => <label key={field}>{({ title: '标题', authors: '作者（逗号分隔）', year: '年份', doi: 'DOI', keywords: '关键词（逗号分隔）', summary: '检索简述' })[field]}{field === 'summary' ? <Input.TextArea rows={5} value={metadata[field]} onChange={(event) => setMetadata((old) => ({ ...old, [field]: event.target.value }))} /> : <Input value={metadata[field]} onChange={(event) => setMetadata((old) => ({ ...old, [field]: event.target.value }))} />}</label>)}
      </Modal>
      <Modal destroyOnHidden title={preview?.title} open={preview !== null} width={900} footer={null} onCancel={() => setPreview(null)}><MarkdownText content={preview?.text ?? ''} /></Modal>
      <Modal destroyOnHidden title="替换正文" open={replacement !== null} footer={null} onCancel={() => setReplacement(null)}>
        <Typography.Paragraph>为“{replacement?.title}”上传新的 Markdown 或 PDF。新草稿须经人工审核；确认前原正文仍可用，确认后将清理旧原件、旧解析正文及受管副本，不保留历史正文。</Typography.Paragraph>
        <Button disabled={busy} onClick={() => replacementRef.current?.click()}>选择替换文献</Button>
        <input ref={replacementRef} className="file-input-hidden" aria-label="上传替换文献" type="file" accept=".md,.markdown,.pdf,text/markdown,application/pdf" onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = '';
          if (selectedId && replacement && file) void run(() => api.uploadKnowledgeImports(selectedId, [file], replacement.document_id), () => setReplacement(null));
        }} />
      </Modal>
      <Modal destroyOnHidden title={confirmation?.title} open={confirmation !== null} okText="确认" okButtonProps={{ danger: true }} confirmLoading={busy} onCancel={() => setConfirmation(null)} onOk={() => {
        if (confirmation) void run(confirmation.run, () => setConfirmation(null));
      }}><Typography.Paragraph>{confirmation?.description}</Typography.Paragraph>{error ? <Alert type="error" message={error} /> : null}</Modal>
    </Space>
  );
}
