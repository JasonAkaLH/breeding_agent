import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM, requestInterceptor, VirtualConsole } from 'jsdom';
import { describe, expect, it } from 'vitest';
import { createMathJaxConfiguration, getMathJaxAssetUrls } from './mathJaxRuntime';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const VENDOR_ROOT = path.join(FRONTEND_ROOT, 'public', 'vendor');

function localMathJaxInterceptor(basePath: string, requests: string[]) {
  return requestInterceptor(async (request) => {
    requests.push(request.url);
    const parsed = new URL(request.url);
    if (parsed.origin !== 'https://app.test') {
      throw new Error(`Cross-origin request blocked: ${request.url}`);
    }

    const vendorPrefix = `${basePath}vendor/`.replace(/\/+/g, '/');
    if (!parsed.pathname.startsWith(vendorPrefix)) {
      throw new Error(`Non-vendor request blocked: ${request.url}`);
    }
    const relativePath = decodeURIComponent(parsed.pathname.slice(vendorPrefix.length));
    const assetPath = path.resolve(VENDOR_ROOT, relativePath);
    if (!assetPath.startsWith(`${VENDOR_ROOT}${path.sep}`)) {
      throw new Error(`Vendor path escaped root: ${request.url}`);
    }
    return new Response(await readFile(assetPath), {
      headers: { 'Content-Type': 'application/javascript' },
    });
  });
}

interface BrowserMathJaxApi {
  startup: { promise: Promise<void> };
  asyncLoad?: (url: string) => Promise<void>;
  tex2svgPromise(source: string, options: { display: boolean }): Promise<HTMLElement>;
  mathml2svgPromise(source: string, options: { display: boolean }): Promise<HTMLElement>;
}

async function loadRealMathJax(basePath: '/' | '/seedpilot/') {
  const requests: string[] = [];
  const urls = getMathJaxAssetUrls(basePath, 'https://app.test');
  const virtualConsole = new VirtualConsole();
  const errors: string[] = [];
  virtualConsole.on('jsdomError', (error) => errors.push(error.message));
  virtualConsole.on('error', (message) => errors.push(String(message)));

  const dom = new JSDOM(
    `<!doctype html><html><head><script id="MathJax-script" src="${urls.scriptUrl}"></script></head><body></body></html>`,
    {
      beforeParse(window) {
        const config = createMathJaxConfiguration(urls);
        (window as unknown as { MathJax: unknown }).MathJax = window.JSON.parse(JSON.stringify(config));
      },
      pretendToBeVisual: true,
      resources: { interceptors: [localMathJaxInterceptor(basePath, requests)] },
      runScripts: 'dangerously',
      url: `https://app.test${basePath}`,
      virtualConsole,
    },
  );

  const script = dom.window.document.querySelector('script');
  await new Promise<void>((resolve, reject) => {
    script?.addEventListener('load', () => resolve(), { once: true });
    script?.addEventListener('error', () => reject(new Error('MathJax bundle failed to load')), { once: true });
  });
  const mathJax = (dom.window as unknown as { MathJax: BrowserMathJaxApi }).MathJax;
  await mathJax.startup.promise;
  // jsdom does not implement browser dynamic import for external scripts. Route MathJax's
  // dynamic-font hook through a normal script element so the local URL and asset are still exercised.
  mathJax.asyncLoad = (url) => new Promise<void>((resolve, reject) => {
    const dynamicScript = dom.window.document.createElement('script');
    dynamicScript.src = url;
    dynamicScript.addEventListener('load', () => resolve(), { once: true });
    dynamicScript.addEventListener('error', () => reject(new Error(`Dynamic MathJax asset failed: ${url}`)), { once: true });
    dom.window.document.head.append(dynamicScript);
  });
  return { dom, errors, requests, mathJax };
}

describe.each(['/' as const, '/seedpilot/' as const])('real MathJax engine at %s', (basePath) => {
  it('converts TeX and MathML with assistive MathML using only local assets', async () => {
    const { dom, errors, requests, mathJax } = await loadRealMathJax(basePath);

    const tex = await mathJax.tex2svgPromise('\\mathbb{A} + x^2', { display: false });
    const mathml = await mathJax.mathml2svgPromise(
      '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi><mo>+</mo><mn>1</mn></math>',
      { display: true },
    );

    expect(tex.querySelector('svg')).not.toBeNull();
    expect(mathml.querySelector('svg')).not.toBeNull();
    expect(tex.querySelector('mjx-assistive-mml')).not.toBeNull();
    expect(mathml.querySelector('mjx-assistive-mml')).not.toBeNull();
    for (const output of [tex, mathml]) {
      const svg = output.querySelector('svg');
      expect(svg).toHaveAttribute('aria-hidden', 'true');
      expect(svg).toHaveAttribute('focusable', 'false');
      expect(svg).toHaveAttribute('role', 'img');
      expect(output.querySelector('mjx-assistive-mml math')).not.toBeNull();
    }
    expect(requests.length).toBeGreaterThanOrEqual(3);
    expect(requests.some((request) => request.endsWith('/svg/dynamic/double-struck.js'))).toBe(true);
    expect(requests.every((request) => new URL(request).origin === 'https://app.test')).toBe(true);
    expect(requests.every((request) => new URL(request).pathname.startsWith(`${basePath}vendor/`.replace(/\/+/g, '/')))).toBe(true);
    expect(errors.filter((message) => message !== 'Could not parse CSS stylesheet')).toEqual([]);

    dom.window.close();
  });

  it('filters every denied URL scheme plus formula-supplied styles, classes, and IDs', async () => {
    const { dom, requests, mathJax } = await loadRealMathJax(basePath);
    for (const url of ['javascript:alert(1)', 'data:text/html,unsafe', 'file:///etc/passwd']) {
      const output = await mathJax.mathml2svgPromise(
        `<math xmlns="http://www.w3.org/1998/Math/MathML" href="${url}"><mi class="evil" id="evil" style="color:red">x</mi></math>`,
        { display: false },
      );

      expect(output.outerHTML).not.toContain(url);
      expect(output.outerHTML).not.toContain('class="evil"');
      expect(output.outerHTML).not.toContain('id="evil"');
      expect(output.outerHTML).not.toContain('color:red');
    }
    expect(requests.every((request) => new URL(request).origin === 'https://app.test')).toBe(true);

    dom.window.close();
  });

  it('does not render HTML embedded in MathML token nodes', async () => {
    const { dom, requests, mathJax } = await loadRealMathJax(basePath);
    const startupRequestCount = requests.length;

    await expect(mathJax.mathml2svgPromise(
      '<math xmlns="http://www.w3.org/1998/Math/MathML"><mtext><span xmlns="http://www.w3.org/1999/xhtml">unsafe</span>safe</mtext></math>',
      { display: false },
    )).rejects.toThrow('Unknown node type "span"');
    expect(requests).toHaveLength(startupRequestCount);

    dom.window.close();
  });

  it('does not load TeX components for require, autoload, or HTML commands', async () => {
    const { dom, requests, mathJax } = await loadRealMathJax(basePath);
    const startupRequestCount = requests.length;

    for (const source of [
      '\\require{color}x',
      '\\cancel{x}',
      '\\htmlClass{evil}{x}',
    ]) {
      const output = await mathJax.tex2svgPromise(source, { display: false });
      expect(output.querySelector('script, style, iframe')).toBeNull();
    }

    expect(requests).toHaveLength(startupRequestCount);
    expect(requests.some((request) => /extensions|autoload|require|texhtml|html/.test(request))).toBe(false);

    dom.window.close();
  });

  it('keeps failed component and font requests local without alternate-origin fallback', async () => {
    const { dom, requests, mathJax } = await loadRealMathJax(basePath);
    const urls = getMathJaxAssetUrls(basePath, 'https://app.test');
    const missingAssets = [
      new URL('input/tex/extensions/missing.js', urls.mathJaxRoot).href,
      new URL('svg/dynamic/missing.js', urls.fontRoot).href,
    ];

    for (const assetUrl of missingAssets) {
      await expect(mathJax.asyncLoad?.(assetUrl)).rejects.toThrow('Dynamic MathJax asset failed');
    }

    for (const assetUrl of missingAssets) expect(requests).toContain(assetUrl);
    expect(requests.every((request) => new URL(request).origin === 'https://app.test')).toBe(true);
    expect(requests.every((request) => new URL(request).pathname.startsWith(`${basePath}vendor/`.replace(/\/+/g, '/')))).toBe(true);

    dom.window.close();
  });
});
