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
