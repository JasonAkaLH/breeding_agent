import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const styles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8');

function cssRule(selector: string): string {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = styles.match(new RegExp(`${escapedSelector}\\s*\\{(?<body>[^}]*)\\}`, 'm'));
  return match?.groups?.body ?? '';
}

describe('sidebar history layout styles', () => {
  it('keeps history entries full-bleed and flush inside the sidebar', () => {
    expect(cssRule('.history-list')).toContain('width: calc(100% + 40px);');
    expect(cssRule('.history-list')).toContain('margin-inline: -20px;');
    expect(cssRule('.history-list')).toContain('gap: 0;');
    expect(cssRule('.history-row')).toContain('margin: 0;');
    expect(cssRule('.history-row')).toContain('min-height: 0;');
    expect(cssRule('.history-item')).toContain('display: flex;');
    expect(cssRule('.history-item')).toContain('min-height: 40px;');
    expect(cssRule('.history-item')).toContain('align-items: center;');
  });

  it('reveals history row actions only while the pointer hovers the row', () => {
    expect(cssRule('.history-row:hover .history-actions')).toContain('opacity: 1;');
    expect(styles).not.toContain('.history-row:focus-within .history-actions');
  });
});

describe('formula layout styles', () => {
  it('contains oversized formula output inside the message width', () => {
    expect(cssRule('.math-formula')).toContain('max-width: 100%;');
    expect(cssRule('.math-formula')).toContain('overflow-x: auto;');
    expect(cssRule('.math-formula--display')).toContain('width: 100%;');
    expect(cssRule('.math-formula--display')).toContain('text-align: center;');
  });

  it('inherits message color and keeps source fallback selectable', () => {
    expect(cssRule('.math-formula')).toContain('color: currentColor;');
    expect(cssRule('.math-formula svg')).toContain('color: currentColor;');
    expect(cssRule('.math-formula__fallback')).toContain('white-space: pre-wrap;');
    expect(cssRule('.math-formula__fallback')).toContain('user-select: text;');
    expect(styles).not.toMatch(/mjx-container|mjx-assistive-mml/);
  });
});
