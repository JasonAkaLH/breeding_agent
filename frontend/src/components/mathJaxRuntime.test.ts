import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetMathJaxRuntimeForTests,
  createMathJaxConfiguration,
  getMathJaxAssetUrls,
  loadMathJaxRuntime,
  renderFormula,
} from './mathJaxRuntime';

function fakeMathJaxApi(overrides: Record<string, unknown> = {}) {
  return {
    startup: { promise: Promise.resolve() },
    tex2svgPromise: vi.fn(async () => document.createElement('mjx-container')),
    mathml2svgPromise: vi.fn(async () => document.createElement('mjx-container')),
    ...overrides,
  };
}

beforeEach(() => {
  __resetMathJaxRuntimeForTests();
  document.head.innerHTML = '';
  vi.restoreAllMocks();
});

describe('MathJax asset configuration', () => {
  it('derives root and subpath assets from the same application origin', () => {
    expect(getMathJaxAssetUrls('/', 'https://example.test')).toEqual({
      baseUrl: 'https://example.test/',
      fontRoot: 'https://example.test/vendor/@mathjax/mathjax-newcm-font/',
      mathJaxRoot: 'https://example.test/vendor/mathjax/',
      scriptUrl: 'https://example.test/vendor/mathjax/tex-mml-svg.js',
    });
    expect(getMathJaxAssetUrls('/seedpilot/', 'https://example.test').scriptUrl).toBe(
      'https://example.test/seedpilot/vendor/mathjax/tex-mml-svg.js',
    );
  });

  it('rejects a cross-origin base URL', () => {
    expect(() => getMathJaxAssetUrls('https://cdn.example/math/', 'https://example.test')).toThrow(
      'must use the application origin',
    );
  });

  it('builds the strict static runtime configuration', () => {
    const config = createMathJaxConfiguration(getMathJaxAssetUrls('/seedpilot/', 'https://example.test'));

    expect(config.loader.load).toEqual(['ui/safe', 'a11y/assistive-mml']);
    expect(config.loader.paths).toMatchObject({
      mathjax: 'https://example.test/seedpilot/vendor/mathjax',
      'mathjax-newcm': 'https://example.test/seedpilot/vendor/@mathjax/mathjax-newcm-font',
    });
    expect(config.startup.typeset).toBe(false);
    expect(config.tex.packages['[-]']).toEqual(expect.arrayContaining(['require', 'autoload', 'html', 'texhtml']));
    expect(config.mml).toEqual({ allowHtmlInTokenNodes: false, parseAs: 'xml' });
    expect(config.svg.fontCache).toBe('local');
    expect(config.options.safeOptions.allow).toEqual({
      URLs: 'none',
      classes: 'none',
      cssIDs: 'none',
      styles: 'none',
    });
    expect(config.options.menuOptions.settings).toMatchObject({
      assistiveMml: true,
      braille: false,
      enrich: false,
      speech: false,
    });
    expect(config.options.enableMenu).toBe(false);
  });
});

describe('MathJax runtime loading and conversion', () => {
  it('does not insert a script until the runtime is requested', () => {
    expect(document.querySelector('script[data-mathjax-runtime="local"]')).toBeNull();
  });

  it('rejects a pre-existing MathJax global rather than reusing unknown configuration', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    window.MathJax = fakeMathJaxApi();

    await expect(loadMathJaxRuntime()).rejects.toMatchObject({ category: 'configuration' });
    expect(document.querySelector('script[data-mathjax-runtime="local"]')).toBeNull();
  });

  it('shares one initialization promise across concurrent callers', async () => {
    const first = loadMathJaxRuntime();
    const second = loadMathJaxRuntime();
    const script = document.querySelector<HTMLScriptElement>('script[data-mathjax-runtime="local"]');
    expect(script).not.toBeNull();
    expect(document.querySelectorAll('script[data-mathjax-runtime="local"]')).toHaveLength(1);

    const api = fakeMathJaxApi();
    window.MathJax = api;
    script?.dispatchEvent(new Event('load'));

    await expect(first).resolves.toBe(api);
    await expect(second).resolves.toBe(api);
  });

  it('uses the promise conversion API for TeX and MathML', async () => {
    const loading = loadMathJaxRuntime();
    const api = fakeMathJaxApi();
    window.MathJax = api;
    document.querySelector<HTMLScriptElement>('script[data-mathjax-runtime="local"]')?.dispatchEvent(new Event('load'));
    await loading;

    await renderFormula('x^2', 'tex', false);
    await renderFormula('<math><mi>x</mi></math>', 'mathml', true);

    expect(api.tex2svgPromise).toHaveBeenCalledWith('x^2', { display: false });
    expect(api.mathml2svgPromise).toHaveBeenCalledWith('<math><mi>x</mi></math>', { display: true });
  });

  it('keeps a failed initialization rejected without retrying or logging source', async () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const first = loadMathJaxRuntime();
    const script = document.querySelector<HTMLScriptElement>('script[data-mathjax-runtime="local"]');
    script?.dispatchEvent(new Event('error'));

    await expect(first).rejects.toMatchObject({ category: 'initialization' });
    await expect(loadMathJaxRuntime()).rejects.toMatchObject({ category: 'initialization' });
    expect(document.querySelectorAll('script[data-mathjax-runtime="local"]')).toHaveLength(1);
    expect(warning).toHaveBeenCalledTimes(1);
    expect(warning).toHaveBeenCalledWith('Local MathJax runtime initialization failed');
  });

  it('rejects conversion output containing a non-local resource reference', async () => {
    const loading = loadMathJaxRuntime();
    const output = document.createElement('mjx-container');
    const link = document.createElementNS('http://www.w3.org/2000/svg', 'a');
    link.setAttribute('href', 'https://outside.example/');
    output.append(link);
    const api = fakeMathJaxApi({ tex2svgPromise: vi.fn(async () => output) });
    window.MathJax = api;
    document.querySelector<HTMLScriptElement>('script[data-mathjax-runtime="local"]')?.dispatchEvent(new Event('load'));
    await loading;

    await expect(renderFormula('x', 'tex', false)).rejects.toMatchObject({ category: 'unsafe-output' });
  });
});
