import { Alert, Card, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { SqlQueryDisplayModel } from '../domain/artifacts';

interface Props {
  result: SqlQueryDisplayModel;
}

export function SqlQueryResultCard({ result }: Props) {
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
      {typeof result.table?.rowCount === 'number' ? <Tag color="blue">共 {result.table.rowCount} 行</Tag> : null}
      {result.table?.truncated ? <Tag color="orange">仅展示预览</Tag> : null}
      {columns.length > 0 ? (
        <Table
          className="result-table"
          size="small"
          pagination={false}
          columns={columns}
          dataSource={rows}
          rowKey={(row) => String(row.__row_key)}
        />
      ) : (
        <Typography.Text type="secondary">当前结果没有可展示的表格预览。</Typography.Text>
      )}
      {result.warnings.map((warning) => (
        <Alert key={warning} className="result-warning" type="warning" showIcon message={warning} />
      ))}
    </Card>
  );
}
