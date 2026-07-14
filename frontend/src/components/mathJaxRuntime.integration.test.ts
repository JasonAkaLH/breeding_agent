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
    expect(requests.length).toBeGreaterThanOrEqual(3);
    expect(requests.some((request) => request.endsWith('/svg/dynamic/double-struck.js'))).toBe(true);
    expect(requests.every((request) => new URL(request).origin === 'https://app.test')).toBe(true);
    expect(requests.every((request) => new URL(request).pathname.startsWith(`${basePath}vendor/`.replace(/\/+/g, '/')))).toBe(true);
    expect(errors.filter((message) => message !== 'Could not parse CSS stylesheet')).toEqual([]);

    dom.window.close();
  });

  it('filters formula-supplied URLs, styles, classes, and IDs', async () => {
    const { dom, requests, mathJax } = await loadRealMathJax(basePath);
    const output = await mathJax.mathml2svgPromise(
      '<math xmlns="http://www.w3.org/1998/Math/MathML" href="https://outside.example/"><mi class="evil" id="evil" style="color:red">x</mi></math>',
      { display: false },
    );

    expect(output.outerHTML).not.toContain('outside.example');
    expect(output.outerHTML).not.toContain('class="evil"');
    expect(output.outerHTML).not.toContain('id="evil"');
    expect(output.outerHTML).not.toContain('color:red');
    expect(requests.every((request) => new URL(request).origin === 'https://app.test')).toBe(true);

    dom.window.close();
  });
});
