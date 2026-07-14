export const MAX_FORMULA_SOURCE_LENGTH = 10_000;
export const MAX_FORMULAS_PER_RENDER = 100;

export interface FormulaToken {
  type: 'formula';
  language: 'tex' | 'mathml';
  source: string;
  display: boolean;
  fallbackSource: string;
}

export interface TextToken {
  type: 'text';
  source: string;
}

export interface FormulaParseContext {
  formulaCount: number;
}

export interface FormulaParseOptions {
  context?: FormulaParseContext;
}

type InlineFormulaToken = FormulaToken | TextToken;

interface MathMlCandidate {
  end: number;
  source: string;
  valid: boolean;
  display: boolean;
}

const MATHML_NAMESPACE = 'http://www.w3.org/1998/Math/MathML';
const PRESENTATION_MATHML_ELEMENTS = new Set([
  'annotation',
  'annotation-xml',
  'maction',
  'maligngroup',
  'malignmark',
  'menclose',
  'merror',
  'mfenced',
  'mfrac',
  'mglyph',
  'mi',
  'mlabeledtr',
  'mlongdiv',
  'mmultiscripts',
  'mn',
  'mo',
  'mover',
  'mpadded',
  'mphantom',
  'mprescripts',
  'mroot',
  'mrow',
  'ms',
  'mscarries',
  'mscarry',
  'msgroup',
  'msline',
  'mspace',
  'msqrt',
  'msrow',
  'mstack',
  'mstyle',
  'msub',
  'msubsup',
  'msup',
  'mtable',
  'mtd',
  'mtext',
  'mtr',
  'munder',
  'munderover',
  'none',
  'semantics',
]);

export function createFormulaParseContext(): FormulaParseContext {
  return { formulaCount: 0 };
}

export function parseFormulaFence(
  language: string,
  content: string,
  closed: boolean,
  options: FormulaParseOptions = {},
): FormulaToken | null {
  if (!closed || !/^(?:tex|latex|math)$/i.test(language) || !content.trim()) return null;
  return acceptFormula({
    type: 'formula',
    language: 'tex',
    source: content,
    display: true,
    fallbackSource: `\`\`\`${language}\n${content}\n\`\`\``,
  }, options);
}

export function parseBlockFormula(source: string, options: FormulaParseOptions = {}): FormulaToken | null {
  const trimmed = source.trim();
  if (!trimmed) return null;

  for (const [opening, closing] of [['$$', '$$'], ['\\[', '\\]']] as const) {
    if (!trimmed.startsWith(opening)) continue;
    const closingIndex = findUnescapedDelimiter(trimmed, opening.length, closing);
    if (closingIndex < 0 || trimmed.slice(closingIndex + closing.length).trim()) return null;
    const formulaSource = trimmed.slice(opening.length, closingIndex).trim();
    if (!formulaSource) return null;
    return acceptFormula({
      type: 'formula',
      language: 'tex',
      source: formulaSource,
      display: true,
      fallbackSource: source,
    }, options);
  }

  const mathMl = extractMathMlCandidate(trimmed, 0);
  if (!mathMl || !mathMl.valid || mathMl.end !== trimmed.length) return null;
  return acceptFormula({
    type: 'formula',
    language: 'mathml',
    source: mathMl.source,
    display: true,
    fallbackSource: source,
  }, options);
}

export function scanInlineFormulas(
  source: string,
  options: FormulaParseOptions = {},
): InlineFormulaToken[] {
  if (!source) return [];
  const tokens: InlineFormulaToken[] = [];
  let text = '';
  let index = 0;

  const flushText = () => {
    if (!text) return;
    pushTextToken(tokens, text);
    text = '';
  };

  while (index < source.length) {
    const mathMl = extractMathMlCandidate(source, index);
    if (!mathMl && startsWithMathRoot(source, index)) {
      text += source.slice(index);
      index = source.length;
      continue;
    }
    if (mathMl) {
      if (!mathMl.valid) {
        text += mathMl.source;
        index = mathMl.end;
        continue;
      }
      const token = acceptFormula({
        type: 'formula',
        language: 'mathml',
        source: mathMl.source,
        display: mathMl.display,
        fallbackSource: mathMl.source,
      }, options);
      if (token) {
        flushText();
        tokens.push(token);
      } else {
        text += mathMl.source;
      }
      index = mathMl.end;
      continue;
    }

    if (source[index] === '$') {
      if (isEscapedAt(source, index)) {
        text = removeOneTrailingBackslash(text) + '$';
        index += 1;
        continue;
      }
      if (source[index + 1] === '$') {
        text += '$$';
        index += 2;
        continue;
      }
      if (!isSingleDollarOpening(source, index)) {
        text += source[index];
        index += 1;
        continue;
      }
      const closingIndex = findSingleDollarClose(source, index + 1);
      if (closingIndex >= 0) {
        const fallbackSource = source.slice(index, closingIndex + 1);
        const token = acceptFormula({
          type: 'formula',
          language: 'tex',
          source: source.slice(index + 1, closingIndex),
          display: false,
          fallbackSource,
        }, options);
        if (token) {
          flushText();
          tokens.push(token);
        } else {
          text += fallbackSource;
        }
        index = closingIndex + 1;
        continue;
      }
    }

    if (source.startsWith('\\(', index)) {
      if (isEscapedAt(source, index)) {
        text = removeOneTrailingBackslash(text) + '\\(';
        index += 2;
        continue;
      }
      const closingIndex = findBackslashClose(source, index + 2, '\\)');
      if (closingIndex >= 0) {
        const fallbackSource = source.slice(index, closingIndex + 2);
        const formulaSource = source.slice(index + 2, closingIndex);
        const token = formulaSource && !formulaSource.includes('\n')
          ? acceptFormula({
            type: 'formula',
            language: 'tex',
            source: formulaSource,
            display: false,
            fallbackSource,
          }, options)
          : null;
        if (token) {
          flushText();
          tokens.push(token);
        } else {
          text += fallbackSource;
        }
        index = closingIndex + 2;
        continue;
      }
    }

    const backslashDelimiter = backslashDelimiterAt(source, index);
    if (backslashDelimiter && isEscapedAt(source, index)) {
      text = removeOneTrailingBackslash(text) + backslashDelimiter;
      index += 2;
      continue;
    }

    text += source[index];
    index += 1;
  }

  flushText();
  return tokens;
}

function acceptFormula(token: FormulaToken, options: FormulaParseOptions): FormulaToken | null {
  const context = options.context;
  if (token.source.length > MAX_FORMULA_SOURCE_LENGTH) return null;
  if (context && context.formulaCount >= MAX_FORMULAS_PER_RENDER) return null;
  if (context) context.formulaCount += 1;
  return token;
}

function pushTextToken(tokens: InlineFormulaToken[], source: string) {
  const previous = tokens.at(-1);
  if (previous?.type === 'text') {
    previous.source += source;
  } else {
    tokens.push({ type: 'text', source });
  }
}

function isSingleDollarOpening(source: string, index: number): boolean {
  const next = source[index + 1];
  return Boolean(next && !/\s/.test(next));
}

function findSingleDollarClose(source: string, start: number): number {
  for (let index = start; index < source.length; index += 1) {
    const character = source[index];
    if (character === '\n') return -1;
    if (character !== '$' || isEscapedAt(source, index)) continue;
    const previous = source[index - 1];
    const next = source[index + 1] ?? '';
    if (!/\s/.test(previous) && !/\d/.test(next)) return index;
    return -1;
  }
  return -1;
}

function findBackslashClose(source: string, start: number, delimiter: '\\)'): number {
  for (let index = start; index < source.length - 1; index += 1) {
    if (source[index] === '\n') return -1;
    if (source.startsWith(delimiter, index) && !isEscapedAt(source, index)) return index;
  }
  return -1;
}

function findUnescapedDelimiter(source: string, start: number, delimiter: '$$' | '\\]'): number {
  let index = source.indexOf(delimiter, start);
  while (index >= 0) {
    if (!isEscapedAt(source, index)) return index;
    index = source.indexOf(delimiter, index + delimiter.length);
  }
  return -1;
}

function backslashDelimiterAt(source: string, index: number): '\\(' | '\\)' | '\\[' | '\\]' | null {
  for (const delimiter of ['\\(', '\\)', '\\[', '\\]'] as const) {
    if (source.startsWith(delimiter, index)) return delimiter;
  }
  return null;
}

function isEscapedAt(source: string, index: number): boolean {
  let backslashCount = 0;
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) {
    backslashCount += 1;
  }
  return backslashCount % 2 === 1;
}

function removeOneTrailingBackslash(source: string): string {
  return source.endsWith('\\') ? source.slice(0, -1) : source;
}

function extractMathMlCandidate(source: string, start: number): MathMlCandidate | null {
  if (!startsWithMathRoot(source, start)) return null;
  let cursor = start + '<math'.length;
  let depth = 1;
  let nested = false;
  let end = -1;
  while (depth > 0) {
    const nextOpening = findNextMathOpening(source, cursor);
    const nextClosing = source.indexOf('</math>', cursor);
    if (nextClosing < 0) return null;
    if (nextOpening >= 0 && nextOpening < nextClosing) {
      nested = true;
      depth += 1;
      cursor = nextOpening + '<math'.length;
      continue;
    }
    depth -= 1;
    end = nextClosing + '</math>'.length;
    cursor = end;
  }

  const candidate = source.slice(start, end);
  if (nested) return { end, source: candidate, valid: false, display: false };
  const validation = validateMathMl(candidate);
  return {
    end,
    source: candidate,
    valid: validation.valid,
    display: validation.display,
  };
}

function startsWithMathRoot(source: string, index: number): boolean {
  if (!source.startsWith('<math', index)) return false;
  const boundary = source[index + '<math'.length];
  return boundary === '>' || boundary === '/' || Boolean(boundary && /\s/.test(boundary));
}

function findNextMathOpening(source: string, start: number): number {
  let index = source.indexOf('<math', start);
  while (index >= 0) {
    if (startsWithMathRoot(source, index)) return index;
    index = source.indexOf('<math', index + 5);
  }
  return -1;
}

function validateMathMl(source: string): { valid: boolean; display: boolean } {
  const documentNode = new DOMParser().parseFromString(source, 'application/xml');
  if (documentNode.getElementsByTagName('parsererror').length > 0) return { valid: false, display: false };
  const root = documentNode.documentElement;
  if (root.tagName !== 'math' || root.localName !== 'math' || root.prefix) return { valid: false, display: false };
  if (root.namespaceURI && root.namespaceURI !== MATHML_NAMESPACE) return { valid: false, display: false };
  if (root.getElementsByTagNameNS('*', 'math').length > 0) return { valid: false, display: false };

  for (const element of Array.from(root.getElementsByTagName('*'))) {
    if (element.prefix) return { valid: false, display: false };
    if (element.namespaceURI && element.namespaceURI !== MATHML_NAMESPACE) return { valid: false, display: false };
    if (!PRESENTATION_MATHML_ELEMENTS.has(element.localName)) return { valid: false, display: false };
  }
  return { valid: true, display: root.getAttribute('display') === 'block' };
}
