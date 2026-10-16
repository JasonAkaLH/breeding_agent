import { Alert, Button, Checkbox, Space, Typography } from 'antd';
import type { KnowledgeLibrary, KnowledgeSelection } from '../api/knowledgeTypes';

interface Props {
  libraries: KnowledgeLibrary[];
  selection: KnowledgeSelection;
  loading: boolean;
  saving: boolean;
  error: string | null;
  running: boolean;
  onChange(selection: KnowledgeSelection): void;
  onRetry(): void;
  onReload(): void;
}

export function KnowledgeLibrarySelector({ libraries, selection, loading, saving, error, running, onChange, onRetry, onReload }: Props) {
  const unavailable = selection.mode === 'selected'
    ? selection.library_ids.filter((id) => !libraries.some((library) => library.library_id === id && library.status === 'active')) : [];
  const allUnavailable = selection.mode === 'selected' && selection.library_ids.length > 0 && unavailable.length === selection.library_ids.length;
  const selectedNames = selection.library_ids.map((id) => libraries.find((library) => library.library_id === id)?.name ?? id).join('、');
  return (
    <details className="knowledge-selector">
      <summary>文献库：{loading ? '加载中' : selection.mode === 'auto' ? '全部可访问库' : selectedNames}{saving ? '（保存中）' : ''}</summary>
      <Space direction="vertical" size="small" className="knowledge-selector-body">
        <Typography.Text type="secondary">{running ? '当前问题范围已固定，修改将用于下一次提问。' : '默认使用全部可访问库；选择特定库后，仅使用这些库。'}</Typography.Text>
        <Space>
          <Button size="small" disabled={loading} onClick={() => onChange({ mode: 'auto', library_ids: [] })}>使用全部可访问库</Button>
          <Button size="small" disabled={saving} onClick={onReload}>刷新文献库</Button>
        </Space>
        {libraries.map((library) => (
          <Checkbox key={library.library_id} checked={selection.mode === 'selected' && selection.library_ids.includes(library.library_id)} disabled={loading || library.status !== 'active'} onChange={(event) => {
            const current = selection.mode === 'selected' ? selection.library_ids : [];
            const ids = event.target.checked ? [...current, library.library_id] : current.filter((id) => id !== library.library_id);
            if (ids.length > 0) onChange({ mode: 'selected', library_ids: ids });
          }}>{library.name}{library.role === 'reader' ? '（共享，只读）' : ''}</Checkbox>
        ))}
        {!loading && libraries.length === 0 ? <Typography.Text type="secondary">暂无可访问文献库，可在用户账户设置中创建。</Typography.Text> : null}
        {unavailable.length ? <Alert type="warning" showIcon message={allUnavailable ? '所选文献库全部失效，请重新选择文献库。' : '部分所选文献库已失效，仅使用仍可访问的库。'} description={`不可用：${unavailable.join('、')}`} /> : null}
        {error ? <Alert type="error" showIcon message={error} action={<Button size="small" onClick={onRetry}>重试保存</Button>} /> : null}
      </Space>
    </details>
  );
}
