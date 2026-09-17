import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MarkdownText } from './MarkdownText';
import * as formulaParser from './mathFormulaParser';

vi.mock('./mathFormulaParser', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./mathFormulaParser')>();
  return {
    ...actual,
    parseBlockFormula: vi.fn(actual.parseBlockFormula),
    scanInlineFormulaSpans: vi.fn(actual.scanInlineFormulaSpans),
    scanInlineFormulas: vi.fn(actual.scanInlineFormulas),
  };
});

vi.mock('./MathFormula', () => ({
  MathFormula: ({ language, source, display, fallbackSource }: {
    language: string;
    source: string;
    display: boolean;
    fallbackSource: string;
  }) => (
    <span
      data-testid="formula"
      data-language={language}
      data-display={String(display)}
      data-source={source}
    >
      {fallbackSource}
    </span>
  ),
}));

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

  it('preserves literal formula-like source in code and ordinary currency text', () => {
    const { container } = render(
      <MarkdownText
        content={'行内 `$x^2$` 保持代码，价格 $100 与 $200 保持文本。\n\n```text\n$$not a formula$$\n```'}
      />,
    );

    expect(screen.getByText('$x^2$').tagName.toLowerCase()).toBe('code');
    expect(screen.getByText(/价格 \$100 与 \$200 保持文本/)).toBeInTheDocument();
    expect(screen.getByText('$$not a formula$$').tagName.toLowerCase()).toBe('code');
    expect(container.querySelectorAll('.markdown-code-block')).toHaveLength(1);
  });

  it('preserves multiline paragraph source and safe link labels', () => {
    render(<MarkdownText content={'第一行\\$literal\n第二行 [说明](mailto:test@example.test)'} />);

    const paragraph = screen.getByText(/第一行/).closest('p');
    expect(paragraph).toHaveTextContent('第一行$literal 第二行 说明');
    expect(screen.getByRole('link', { name: '说明' })).toHaveAttribute('href', 'mailto:test@example.test');
  });

  it('routes complete inline formulas through headings, lists, tables, strong text, and link labels', () => {
    render(
      <MarkdownText
        content={'## 标题 $h$\n\n- 列表 \\(l\\)\n\n| 指标 | 值 |\n| --- | --- |\n| **平方 $x^2$** | [公式 $y$](https://example.test/$literal$) |'}
      />,
    );

    const formulas = screen.getAllByTestId('formula');
    expect(formulas).toHaveLength(4);
    expect(formulas.map((formula) => formula.getAttribute('data-source'))).toEqual(['h', 'l', 'x^2', 'y']);
    expect(screen.getByRole('link', { name: /公式/ })).toHaveAttribute('href', 'https://example.test/$literal$');
  });

  it('keeps strong-like markers inside complete TeX while protecting code and link destinations', () => {
    const { container } = render(
      <MarkdownText
        content={'$a**b**c$ **外层 $x$** `$code$` [标签 $y$](https://example.test/$destination$)'}
      />,
    );

    const formulas = screen.getAllByTestId('formula');
    expect(formulas.map((formula) => formula.getAttribute('data-source'))).toEqual(['a**b**c', 'x', 'y']);
    expect(screen.getByText('$code$').tagName.toLowerCase()).toBe('code');
    expect(screen.getByRole('link', { name: /标签/ })).toHaveAttribute(
      'href',
      'https://example.test/$destination$',
    );
    expect(screen.getByText('外层').closest('strong')).toContainElement(formulas[1]);
    expect(container.textContent?.match(/\$a\*\*b\*\*c\$/g)).toHaveLength(1);
  });

  it('keeps a formula with strong-like markers inside an outer strong span', () => {
    const { container } = render(<MarkdownText content={'**outer $a**b**c$ end**'} />);

    const formula = screen.getByTestId('formula');
    const strong = container.querySelector('strong');
    expect(formula).toHaveAttribute('data-source', 'a**b**c');
    expect(strong).toContainElement(formula);
    expect(strong).toHaveTextContent('outer $a**b**c$ end');
  });

  it('counts a compositional strong formula only once toward the render budget', () => {
    const remainingFormulas = Array.from({ length: 100 }, (_, index) => `$x_${index}$`);
    render(<MarkdownText content={['**outer $a**b**c$ end**', ...remainingFormulas].join(' ')} />);

    const formulas = screen.getAllByTestId('formula');
    expect(formulas).toHaveLength(formulaParser.MAX_FORMULAS_PER_RENDER);
    expect(formulas[0]).toHaveAttribute('data-source', 'a**b**c');
    expect(formulas.at(-1)).toHaveAttribute('data-source', 'x_98');
  });

  it('parses multiline block candidates once and skips parsing after the source limit', () => {
    const parseBlockFormula = vi.mocked(formulaParser.parseBlockFormula);
    const formulaLines = Array.from({ length: 200 }, () => 'x');
    parseBlockFormula.mockClear();

    const { container, rerender } = render(<MarkdownText content={`$$\n${formulaLines.join('\n')}\n$$`} />);

    expect(screen.getByTestId('formula')).toHaveAttribute('data-source', formulaLines.join('\n'));
    expect(parseBlockFormula).toHaveBeenCalledTimes(1);

    const oversizedSource = 'x'.repeat(formulaParser.MAX_FORMULA_SOURCE_LENGTH + 1);
    for (const content of [
      `$$\n${oversizedSource}\n$$`,
      `\\[\n${oversizedSource}\n\\]`,
      `<math><mtext>${oversizedSource}</mtext></math>`,
    ]) {
      parseBlockFormula.mockClear();
      rerender(<MarkdownText content={content} />);

      expect(screen.queryByTestId('formula')).not.toBeInTheDocument();
      expect(container).toHaveTextContent(oversizedSource);
      expect(parseBlockFormula).not.toHaveBeenCalled();
    }
  });

  it('consumes consecutive malformed block candidates once without inline reinterpretation', () => {
    const malformedSources = [
      Array.from({ length: 200 }, () => '<math>').join('\n'),
      Array.from({ length: 200 }, () => '$$').join('\n'),
      Array.from({ length: 200 }, () => '\\[').join('\n'),
    ];
    const parseBlockFormula = vi.mocked(formulaParser.parseBlockFormula);
    const scanInlineFormulaSpans = vi.mocked(formulaParser.scanInlineFormulaSpans);
    const scanInlineFormulas = vi.mocked(formulaParser.scanInlineFormulas);
    parseBlockFormula.mockClear();
    scanInlineFormulaSpans.mockClear();
    scanInlineFormulas.mockClear();

    const { container } = render(<MarkdownText content={`${malformedSources.join('\n\n')}\n\n## after`} />);

    expect(screen.queryByTestId('formula')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'after' })).toBeInTheDocument();
    expect(Array.from(container.querySelectorAll('p'), (node) => node.textContent)).toEqual(malformedSources);
    expect(parseBlockFormula).toHaveBeenCalledTimes(1);
    expect(scanInlineFormulaSpans).toHaveBeenCalledTimes(1);
    expect(scanInlineFormulaSpans).toHaveBeenCalledWith('after');
    expect(scanInlineFormulas).toHaveBeenCalledTimes(1);
    expect(scanInlineFormulas.mock.calls[0]?.[0]).toBe('after');
  });

  it('renders complete display formulas and formula fences while leaving incomplete source readable', () => {
    const { rerender } = render(
      <MarkdownText content={'$$\nx^2 + y^2\n$$\n\n```latex\n\\frac{a}{b}\n```\n\n<math><mi>x</mi></math>'} />,
    );

    const formulas = screen.getAllByTestId('formula');
    expect(formulas).toHaveLength(3);
    expect(formulas.every((formula) => formula.dataset.display === 'true')).toBe(true);
    expect(formulas[0]).toHaveAttribute('data-language', 'tex');
    expect(formulas[2]).toHaveAttribute('data-language', 'mathml');

    rerender(<MarkdownText content={'未完成 $x 与 \\(y\n\n```math\nz^2'} />);
    expect(screen.queryByTestId('formula')).not.toBeInTheDocument();
    expect(screen.getByText(/未完成 \$x/)).toBeInTheDocument();
    expect(screen.getByText('z^2').tagName.toLowerCase()).toBe('code');
  });

});
