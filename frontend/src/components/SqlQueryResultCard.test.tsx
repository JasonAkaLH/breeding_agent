import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SqlQueryResultCard } from './SqlQueryResultCard';
import type { SqlQueryDisplayModel } from '../domain/artifacts';

describe('SqlQueryResultCard', () => {
  it('keeps wide SQL preview tables horizontally scrollable inside the card', () => {
    const result: SqlQueryDisplayModel = {
      summary: '共 1 行。',
      warnings: [],
      table: {
        columns: ['very_long_column_a', 'very_long_column_b', 'very_long_column_c', 'very_long_column_d'],
        rows: [{
          very_long_column_a: 'A'.repeat(40),
          very_long_column_b: 'B'.repeat(40),
          very_long_column_c: 'C'.repeat(40),
          very_long_column_d: 'D'.repeat(40),
        }],
        rowCount: 1,
        truncated: false,
      },
    };

    const { container } = render(<SqlQueryResultCard result={result} />);

    expect(screen.getByText('SQLQuery 查询结果')).toBeInTheDocument();
    expect(container.querySelector('.ant-table-content')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开原始表格' }));
    const tableContent = container.querySelector<HTMLElement>('.ant-table-content');
    expect(tableContent).toBeTruthy();
    expect(tableContent?.style.overflowX).toBe('auto');
  });
});
