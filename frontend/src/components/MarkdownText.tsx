import type { ReactNode } from 'react';

interface MarkdownTextProps {
  content: string;
}

type ListKind = 'ul' | 'ol';

export function MarkdownText({ content }: MarkdownTextProps) {
  const blocks = parseBlocks(content);
  return <div className="markdown-content">{blocks}</div>;
}

function parseBlocks(content: string): ReactNode[] {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const codeFence = line.match(/^```(\w+)?\s*$/);
    if (codeFence) {
      const language = codeFence[1] ?? '';
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <pre key={`code-${blocks.length}`} className="markdown-code-block">
          <code className={language ? `language-${language}` : undefined}>{codeLines.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const Tag = (`h${Math.min(level + 2, 5)}`) as 'h3' | 'h4' | 'h5';
      blocks.push(<Tag key={`heading-${blocks.length}`}>{renderInline(heading[2])}</Tag>);
      index += 1;
      continue;
    }

    if (isTableStart(lines, index)) {
      const headers = splitTableRow(lines[index]);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && isTableRow(lines[index])) {
        rows.push(normalizeTableRow(splitTableRow(lines[index]), headers.length));
        index += 1;
      }
      blocks.push(
        <div key={`table-${blocks.length}`} className="markdown-table-wrapper">
          <table className="markdown-table">
            <thead>
              <tr>{headers.map((header, columnIndex) => <th key={`header-${columnIndex}`}>{renderInline(header)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={`row-${rowIndex}`}>
                  {row.map((cell, columnIndex) => <td key={`cell-${rowIndex}-${columnIndex}`}>{renderInline(cell)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (unordered || ordered) {
      const listKind: ListKind = unordered ? 'ul' : 'ol';
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const itemMatch = listKind === 'ul' ? lines[index].match(/^\s*[-*]\s+(.+)$/) : lines[index].match(/^\s*\d+\.\s+(.+)$/);
        if (!itemMatch) break;
        items.push(<li key={`item-${index}`}>{renderInline(itemMatch[1])}</li>);
        index += 1;
      }
      const ListTag = listKind;
      blocks.push(<ListTag key={`list-${blocks.length}`}>{items}</ListTag>);
      continue;
    }

    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isSpecialBlockStart(lines, index)) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    blocks.push(<p key={`paragraph-${blocks.length}`}>{renderInline(paragraphLines.join('\n'))}</p>);
  }

  return blocks;
}

function isSpecialBlockStart(lines: string[], index: number): boolean {
  const line = lines[index];
  return /^```/.test(line)
    || /^(#{1,3})\s+/.test(line)
    || /^\s*[-*]\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
    || isTableStart(lines, index);
}

function isTableStart(lines: string[], index: number): boolean {
  if (index + 1 >= lines.length) return false;
  const header = splitTableRow(lines[index]);
  const separator = splitTableRow(lines[index + 1]);
  return header.length >= 2
    && separator.length === header.length
    && separator.every(isTableSeparatorCell);
}

function isTableSeparatorCell(cell: string): boolean {
  // Assistant output often keeps separator width close to short headers, for example `:--` under `r`.
  // Accept that compact alignment form so valid-looking chat tables render as tables instead of paragraphs.
  return /^:?-{2,}:?$/.test(cell.trim());
}

function isTableRow(line: string): boolean {
  return splitTableRow(line).length >= 2;
}

function splitTableRow(line: string): string[] {
  const trimmed = line.trim();
  if (!trimmed.includes('|')) return [];
  const withoutOuterPipes = trimmed.replace(/^\|/, '').replace(/\|$/, '');
  return withoutOuterPipes.split('|').map((cell) => cell.trim());
}

function normalizeTableRow(cells: string[], expectedLength: number): string[] {
  const normalized = cells.slice(0, expectedLength);
  while (normalized.length < expectedLength) {
    normalized.push('');
  }
  return normalized;
}

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    const token = match[0];
    const key = `inline-${match.index}-${nodes.length}`;

    if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(<strong key={key}>{renderInline(token.slice(2, -2))}</strong>);
    } else if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link && isSafeUrl(link[2])) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {renderInline(link[1])}
          </a>,
        );
      } else if (link) {
        nodes.push(<span key={key}>{renderInline(link[1])}</span>);
      } else {
        nodes.push(token);
      }
    }
    cursor = match.index + token.length;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function isSafeUrl(value: string): boolean {
  try {
    const url = new URL(value, window.location.origin);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol);
  } catch {
    return false;
  }
}
