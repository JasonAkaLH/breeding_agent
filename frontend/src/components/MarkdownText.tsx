import type { ReactNode } from 'react';
import { MathFormula } from './MathFormula';
import {
  createFormulaParseContext,
  MAX_FORMULA_SOURCE_LENGTH,
  parseBlockFormula,
  parseFormulaFence,
  scanInlineFormulaSpans,
  scanInlineFormulas,
  type FormulaToken,
  type InlineFormulaSpan,
} from './mathFormulaParser';

interface MarkdownTextProps {
  content: string;
}

type ListKind = 'ul' | 'ol';

export function MarkdownText({ content }: MarkdownTextProps) {
  const context = createFormulaParseContext();
  const blocks = parseBlocks(content, context);
  return <div className="markdown-content">{blocks}</div>;
}

type FormulaParseContext = ReturnType<typeof createFormulaParseContext>;

function parseBlocks(content: string, context: FormulaParseContext): ReactNode[] {
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
      const closed = index < lines.length;
      if (closed) index += 1;
      const formula = parseFormulaFence(language, codeLines.join('\n'), closed, { context });
      if (formula) {
        blocks.push(renderFormulaToken(formula, `fence-${blocks.length}`));
        continue;
      }
      blocks.push(
        <pre key={`code-${blocks.length}`} className="markdown-code-block">
          <code className={language ? `language-${language}` : undefined}>{codeLines.join('\n')}</code>
        </pre>,
      );
      continue;
    }

    if (isBlockFormulaCandidate(line)) {
      const formulaBlock = parseBlockFormulaAt(lines, index, context);
      if (formulaBlock) {
        blocks.push(renderFormulaToken(formulaBlock.token, `block-${blocks.length}`));
        index = formulaBlock.end;
        continue;
      }
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const Tag = (`h${Math.min(level + 2, 5)}`) as 'h3' | 'h4' | 'h5';
      blocks.push(<Tag key={`heading-${blocks.length}`}>{renderInline(heading[2], context)}</Tag>);
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
              <tr>{headers.map((header, columnIndex) => <th key={`header-${columnIndex}`}>{renderInline(header, context)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={`row-${rowIndex}`}>
                  {row.map((cell, columnIndex) => <td key={`cell-${rowIndex}-${columnIndex}`}>{renderInline(cell, context)}</td>)}
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
        items.push(<li key={`item-${index}`}>{renderInline(itemMatch[1], context)}</li>);
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
    blocks.push(<p key={`paragraph-${blocks.length}`}>{renderInline(paragraphLines.join('\n'), context)}</p>);
  }

  return blocks;
}

function isSpecialBlockStart(lines: string[], index: number): boolean {
  const line = lines[index];
  return /^```/.test(line)
    || /^(#{1,3})\s+/.test(line)
    || /^\s*[-*]\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
    || isBlockFormulaCandidate(line)
    || isTableStart(lines, index);
}

function isBlockFormulaCandidate(line: string): boolean {
  return /^\s*(?:\$\$|\\\[|<math(?:\s|>|\/))/.test(line);
}

function parseBlockFormulaAt(
  lines: string[],
  index: number,
  context: FormulaParseContext,
): { token: FormulaToken; end: number } | null {
  const candidateLines: string[] = [];
  const length = createTrimmedLengthTracker();
  const opening = lines[index].match(/^\s*(\$\$|\\\[)/);
  const closing = opening?.[1] === '$$' ? '$$' : opening ? '\\]' : null;
  let end = index;

  while (end < lines.length && (end === index || lines[end].trim())) {
    const line = lines[end];
    const searchStart = end === index && opening ? opening[0].length : 0;
    const closingIndex = closing
      ? findUnescapedDelimiterInLine(line, searchStart, closing)
      : line.indexOf('</math>', searchStart);

    if (end > index) appendTrimmedLength(length, '\n');
    const sourceEnd = closingIndex < 0
      ? line.length
      : closing
        ? closingIndex
        : closingIndex + '</math>'.length;
    appendTrimmedLength(length, line.slice(searchStart, sourceEnd));
    if (trimmedLength(length) > MAX_FORMULA_SOURCE_LENGTH) return null;

    candidateLines.push(line);
    end += 1;
    if (closingIndex >= 0) {
      const token = parseBlockFormula(candidateLines.join('\n'), { context });
      return token ? { token, end } : null;
    }
  }
  return null;
}

interface TrimmedLengthTracker {
  total: number;
  leadingWhitespace: number;
  trailingWhitespace: number;
  sawNonWhitespace: boolean;
}

function createTrimmedLengthTracker(): TrimmedLengthTracker {
  return { total: 0, leadingWhitespace: 0, trailingWhitespace: 0, sawNonWhitespace: false };
}

function appendTrimmedLength(tracker: TrimmedLengthTracker, source: string) {
  for (const character of source) {
    tracker.total += character.length;
    if (/\s/.test(character)) {
      if (!tracker.sawNonWhitespace) tracker.leadingWhitespace += character.length;
      tracker.trailingWhitespace += character.length;
    } else {
      tracker.sawNonWhitespace = true;
      tracker.trailingWhitespace = 0;
    }
  }
}

function trimmedLength(tracker: TrimmedLengthTracker): number {
  return tracker.total - tracker.leadingWhitespace - tracker.trailingWhitespace;
}

function findUnescapedDelimiterInLine(line: string, start: number, delimiter: '$$' | '\\]'): number {
  let index = line.indexOf(delimiter, start);
  while (index >= 0) {
    let backslashCount = 0;
    for (let cursor = index - 1; cursor >= 0 && line[cursor] === '\\'; cursor -= 1) {
      backslashCount += 1;
    }
    if (backslashCount % 2 === 0) return index;
    index = line.indexOf(delimiter, index + delimiter.length);
  }
  return -1;
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

function renderInline(text: string, context: FormulaParseContext): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  const formulaSpans = scanInlineFormulaSpans(text);
  const structuralText = maskFormulaStrongMarkers(text, formulaSpans);
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(structuralText)) !== null) {
    const matchIndex = match.index;
    const token = text.slice(matchIndex, pattern.lastIndex);
    const key = `inline-${matchIndex}-${nodes.length}`;

    if (matchIndex > cursor) {
      nodes.push(...renderFormulaAwareText(text.slice(cursor, matchIndex), context, cursor));
    }

    if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(<strong key={key}>{renderInline(token.slice(2, -2), context)}</strong>);
    } else if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link && isSafeUrl(link[2])) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {renderInline(link[1], context)}
          </a>,
        );
      } else if (link) {
        nodes.push(<span key={key}>{renderInline(link[1], context)}</span>);
      } else {
        nodes.push(token);
      }
    }
    cursor = match.index + token.length;
  }

  if (cursor < text.length) {
    nodes.push(...renderFormulaAwareText(text.slice(cursor), context, cursor));
  }
  return nodes;
}

interface InlineRange {
  start: number;
  end: number;
}

function maskFormulaStrongMarkers(text: string, formulaSpans: InlineFormulaSpan[]): string {
  const protectedRanges = collectProtectedInlineRanges(text);
  let protectedIndex = 0;
  let characters: string[] | null = null;

  for (const formulaSpan of formulaSpans) {
    while (protectedRanges[protectedIndex]?.end <= formulaSpan.start) protectedIndex += 1;
    const protectedRange = protectedRanges[protectedIndex];
    if (protectedRange && protectedRange.start < formulaSpan.end) continue;

    for (let index = formulaSpan.start; index < formulaSpan.end; index += 1) {
      if (text[index] !== '*') continue;
      characters ??= text.split('');
      characters[index] = ' ';
    }
  }

  return characters?.join('') ?? text;
}

function collectProtectedInlineRanges(text: string): InlineRange[] {
  const ranges: InlineRange[] = [];
  const pattern = /`[^`]+`|\[[^\]]+\]\([^)]+\)/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    ranges.push({ start: match.index, end: pattern.lastIndex });
  }
  return ranges;
}

function renderFormulaAwareText(text: string, context: FormulaParseContext, offset: number): ReactNode[] {
  return scanInlineFormulas(text, { context }).map((token, tokenIndex) => {
    if (token.type === 'text') return token.source;
    return renderFormulaToken(token, `inline-${offset}-${tokenIndex}-${formulaTokenHash(token)}`);
  });
}

function renderFormulaToken(token: FormulaToken, key: string): ReactNode {
  return (
    <MathFormula
      key={key}
      language={token.language}
      source={token.source}
      display={token.display}
      fallbackSource={token.fallbackSource}
    />
  );
}

function formulaTokenHash(token: FormulaToken): string {
  const value = `${token.language}:${token.display ? 'display' : 'inline'}:${token.fallbackSource}`;
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function isSafeUrl(value: string): boolean {
  try {
    const url = new URL(value, window.location.origin);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol);
  } catch {
    return false;
  }
}
