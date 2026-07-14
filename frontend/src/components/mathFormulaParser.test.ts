import { describe, expect, it } from 'vitest';
import {
  createFormulaParseContext,
  MAX_FORMULAS_PER_RENDER,
  MAX_FORMULA_SOURCE_LENGTH,
  parseBlockFormula,
  parseFormulaFence,
  scanInlineFormulas,
} from './mathFormulaParser';

describe('mathFormulaParser', () => {
  it('scans complete inline dollar and backslash formulas deterministically', () => {
    expect(scanInlineFormulas('前 $x^2$ 中 \\(y+1\\) 后')).toEqual([
      { type: 'text', source: '前 ' },
      { type: 'formula', language: 'tex', source: 'x^2', display: false, fallbackSource: '$x^2$' },
      { type: 'text', source: ' 中 ' },
      { type: 'formula', language: 'tex', source: 'y+1', display: false, fallbackSource: '\\(y+1\\)' },
      { type: 'text', source: ' 后' },
    ]);
  });

  it('keeps currency, whitespace-bound dollars, line breaks, and incomplete streams as text', () => {
    for (const source of ['$5', '$5.00', '$100 与 $200', '$ x$', '$x $', '$x\n$', '流式 $x', '\\(x\n\\)']) {
      expect(scanInlineFormulas(source)).toEqual([{ type: 'text', source }]);
    }
    expect(scanInlineFormulas('流式 $x$')).toContainEqual(expect.objectContaining({ type: 'formula', source: 'x' }));
    expect(scanInlineFormulas('Cost $5 and $x$')).toEqual([
      { type: 'text', source: 'Cost $5 and ' },
      { type: 'formula', language: 'tex', source: 'x', display: false, fallbackSource: '$x$' },
    ]);
  });

  it('unescapes one backslash for escaped delimiters and honors even parity', () => {
    expect(scanInlineFormulas('literal \\$x$ and \\\\(y\\)')).toEqual([
      { type: 'text', source: 'literal $x$ and \\(y\\)' },
    ]);

    const evenParity = scanInlineFormulas('\\\\$x$');
    expect(evenParity[0]).toEqual({ type: 'text', source: '\\\\' });
    expect(evenParity[1]).toEqual(expect.objectContaining({ type: 'formula', source: 'x' }));
  });

  it('accepts only complete standalone display TeX blocks', () => {
    expect(parseBlockFormula('  $$\nx^2 + y^2\n$$  ')).toEqual({
      type: 'formula',
      language: 'tex',
      source: 'x^2 + y^2',
      display: true,
      fallbackSource: '  $$\nx^2 + y^2\n$$  ',
    });
    expect(parseBlockFormula('\\[\na+b\n\\]')).toEqual(expect.objectContaining({ source: 'a+b', display: true }));
    expect(parseBlockFormula('prose $$x$$')).toBeNull();
    expect(parseBlockFormula('$$x$$ trailing')).toBeNull();
    expect(parseBlockFormula('$$ incomplete')).toBeNull();
    expect(parseBlockFormula('$$x\\$$')).toBeNull();
    expect(parseBlockFormula('\\[x\\\\]')).toBeNull();
    expect(scanInlineFormulas('prose $$x$$')).toEqual([{ type: 'text', source: 'prose $$x$$' }]);
  });

  it('parses closed formula fences case-insensitively and rejects ordinary or incomplete fences', () => {
    for (const language of ['tex', 'LATEX', 'Math']) {
      expect(parseFormulaFence(language, '\\frac{a}{b}', true)).toEqual(expect.objectContaining({
        language: 'tex',
        source: '\\frac{a}{b}',
        display: true,
      }));
    }
    expect(parseFormulaFence('sql', 'select 1', true)).toBeNull();
    expect(parseFormulaFence('math', 'x', false)).toBeNull();
    expect(parseFormulaFence('math', '   ', true)).toBeNull();
  });

  it('validates unprefixed Presentation MathML and preserves display semantics', () => {
    const inline = '<math><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow></math>';
    const namespaced = '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mi>x</mi></math>';
    expect(scanInlineFormulas(`前 ${inline} 后`)).toContainEqual({
      type: 'formula', language: 'mathml', source: inline, display: false, fallbackSource: inline,
    });
    expect(scanInlineFormulas(namespaced)).toEqual([
      { type: 'formula', language: 'mathml', source: namespaced, display: true, fallbackSource: namespaced },
    ]);
    expect(parseBlockFormula(inline)).toEqual(expect.objectContaining({ language: 'mathml', display: true }));
  });

  it('rejects malformed, nested, prefixed, foreign-namespace, and Content MathML', () => {
    const invalidSources = [
      '<math><mi>x</math>',
      '<math><math><mi>x</mi></math></math>',
      '<m:math xmlns:m="http://www.w3.org/1998/Math/MathML"><m:mi>x</m:mi></m:math>',
      '<math xmlns="https://example.test/not-mathml"><mi>x</mi></math>',
      '<math><apply><plus/><ci>x</ci><cn>1</cn></apply></math>',
      '<math><mrow xmlns="https://example.test/foreign"><mi>x</mi></mrow></math>',
      '<math><mi>x</mi> $not-inline$',
    ];
    for (const source of invalidSources) {
      expect(parseBlockFormula(source)).toBeNull();
      expect(scanInlineFormulas(source)).toEqual([{ type: 'text', source }]);
    }
  });

  it('enforces the UTF-16 source length limit before producing a formula token', () => {
    const accepted = 'x'.repeat(MAX_FORMULA_SOURCE_LENGTH);
    const rejected = 'x'.repeat(MAX_FORMULA_SOURCE_LENGTH + 1);
    expect(scanInlineFormulas(`$${accepted}$`)).toEqual([
      expect.objectContaining({ type: 'formula', source: accepted }),
    ]);
    expect(scanInlineFormulas(`$${rejected}$`)).toEqual([{ type: 'text', source: `$${rejected}$` }]);
    expect(parseFormulaFence('tex', rejected, true)).toBeNull();
  });

  it('shares the 100-formula budget across block, fence, and inline parsing', () => {
    const context = createFormulaParseContext();
    expect(parseBlockFormula('$$first$$', { context })).not.toBeNull();
    expect(parseFormulaFence('math', 'second', true, { context })).not.toBeNull();
    const remaining = Array.from({ length: MAX_FORMULAS_PER_RENDER - 2 }, () => '$x$').join(' ');
    const tokens = scanInlineFormulas(`${remaining} $overflow$`, { context });
    expect(tokens.filter((token) => token.type === 'formula')).toHaveLength(MAX_FORMULAS_PER_RENDER - 2);
    expect(tokens.at(-1)).toEqual(expect.objectContaining({ type: 'text', source: expect.stringContaining('$overflow$') }));
    expect(context.formulaCount).toBe(MAX_FORMULAS_PER_RENDER);
  });
});
