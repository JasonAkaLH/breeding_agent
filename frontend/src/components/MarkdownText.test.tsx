import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MarkdownText } from './MarkdownText';

describe('MarkdownText', () => {
  it('renders common markdown blocks and inline marks safely', () => {
    render(<MarkdownText content={'## 标题\n\n- **重点**\n\n```sql\nselect 1\n```\n\n[链接](https://example.test)'} />);

    expect(screen.getByRole('heading', { name: '标题' })).toBeInTheDocument();
    expect(screen.getByText('重点').tagName.toLowerCase()).toBe('strong');
    expect(screen.getByText('select 1')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '链接' })).toHaveAttribute('href', 'https://example.test');
  });

  it('does not render unsafe links as anchors', () => {
    render(<MarkdownText content={'[危险](javascript:alert(1))'} />);

    expect(screen.queryByRole('link', { name: '危险' })).not.toBeInTheDocument();
    expect(screen.getByText('危险')).toBeInTheDocument();
  });

  it('renders markdown tables as real tables with inline marks', () => {
    render(<MarkdownText content={'| 品种 | 说明 |\n| --- | --- |\n| 龙粳33 | **水稻** |\n| 龙粳18 | `审定` |'} />);

    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '品种' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '说明' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '龙粳33' })).toBeInTheDocument();
    expect(screen.getByText('水稻').tagName.toLowerCase()).toBe('strong');
    expect(screen.getByText('审定').tagName.toLowerCase()).toBe('code');
  });

  it('renders assistant tables with compact aligned separator cells', () => {
    render(
      <MarkdownText
        content={`| plots | r | trt |\n|:------|:--|:----|\n| 1 | 1 | A001 |`}
      />,
    );

    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'r' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'A001' })).toBeInTheDocument();
  });

});
