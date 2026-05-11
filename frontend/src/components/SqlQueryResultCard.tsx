import { useState } from 'react';
import { Alert, Button, Card, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { SqlQueryDisplayModel } from '../domain/artifacts';

interface Props {
  result: SqlQueryDisplayModel;
}

export function SqlQueryResultCard({ result }: Props) {
  const [tableExpanded, setTableExpanded] = useState(false);
  const columns: ColumnsType<Record<string, unknown>> = (result.table?.columns ?? []).map((column) => ({
    title: column,
    dataIndex: column,
    key: column,
    render: (value: unknown) => String(value ?? ''),
  }));
  const rows = (result.table?.rows ?? []).map((row, index) => ({ ...row, __row_key: index }));

  return (
    <Card className="result-card" title="SQLQuery 查询结果" size="small">
      <Typography.Paragraph>{result.summary}</Typography.Paragraph>
      {typeof result.table?.rowCount === 'number' ? <Tag color="green">共 {result.table.rowCount} 行</Tag> : null}
      {result.table?.truncated ? <Tag color="orange">仅展示预览</Tag> : null}
      {columns.length > 0 ? (
        <div className="result-table-section">
          <Space size="small" className="result-table-toolbar">
            <Typography.Text type="secondary">原始表格预览默认隐藏</Typography.Text>
            <Button
              size="small"
              aria-label={tableExpanded ? '收起原始表格' : '展开原始表格'}
              onClick={() => setTableExpanded((expanded) => !expanded)}
            >
              {tableExpanded ? '收起表格' : '展开表格'}
            </Button>
          </Space>
          {tableExpanded ? (
            <Table
              className="result-table"
              size="small"
              pagination={false}
              columns={columns}
              dataSource={rows}
              scroll={{ x: 'max-content' }}
              rowKey={(row) => String(row.__row_key)}
            />
          ) : null}
        </div>
      ) : (
        <Typography.Text type="secondary">当前结果没有可展示的表格预览。</Typography.Text>
      )}
      {result.warnings.map((warning) => (
        <Alert key={warning} className="result-warning" type="warning" showIcon message={warning} />
      ))}
    </Card>
  );
}
