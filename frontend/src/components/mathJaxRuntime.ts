export type MathFormulaInputKind = 'tex' | 'mathml';

interface MathJaxConversionOptions {
  display: boolean;
}

interface MathJaxApi {
  startup: {
    promise: Promise<void>;
  };
  tex2svgPromise(source: string, options: MathJaxConversionOptions): Promise<HTMLElement>;
  mathml2svgPromise(source: string, options: MathJaxConversionOptions): Promise<HTMLElement>;
}

export interface MathJaxAssetUrls {
  baseUrl: string;
  fontRoot: string;
  mathJaxRoot: string;
  scriptUrl: string;
}

export interface MathJaxConfiguration {
  loader: {
    load: string[];
    paths: Record<string, string>;
  };
  startup: {
    typeset: false;
  };
  tex: {
    packages: {
      '[-]': string[];
    };
  };
  mml: {
    allowHtmlInTokenNodes: false;
    parseAs: 'xml';
  };
  svg: {
    fontCache: 'local';
  };
  options: {
    enableAssistiveMml: true;
    enableBraille: false;
    enableEnrichment: false;
    enableExplorer: false;
    enableMenu: false;
    enableSpeech: false;
    menuOptions: {
      settings: {
        assistiveMml: true;
        braille: false;
        collapsible: false;
        enrich: false;
        speech: false;
      };
    };
    safeOptions: {
      allow: {
        URLs: 'none';
        classes: 'none';
        cssIDs: 'none';
        styles: 'none';
      };
    };
  };
}

declare global {
  interface Window {
    MathJax?: MathJaxApi | MathJaxConfiguration;
  }
}

export class MathJaxRuntimeError extends Error {
  constructor(
    public readonly category: 'configuration' | 'initialization' | 'conversion' | 'unsafe-output',
    message: string,
    cause?: unknown,
  ) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = 'MathJaxRuntimeError';
  }
}

let initializationPromise: Promise<MathJaxApi> | null = null;
let initializationWarningLogged = false;

function directoryUrl(value: URL) {
  value.pathname = `${value.pathname.replace(/\/$/, '')}/`;
  value.search = '';
  value.hash = '';
  return value;
}

function withoutTrailingSlash(value: string) {
  return value.replace(/\/$/, '');
}

export function getMathJaxAssetUrls(
  baseUrl = import.meta.env.BASE_URL,
  origin = window.location.origin,
): MathJaxAssetUrls {
  const originUrl = new URL(origin);
  const resolvedBaseUrl = directoryUrl(new URL(baseUrl, `${originUrl.origin}/`));
  if (resolvedBaseUrl.origin !== originUrl.origin) {
    throw new MathJaxRuntimeError(
      'configuration',
      'MathJax assets must use the application origin',
    );
  }

  const mathJaxRoot = directoryUrl(new URL('vendor/mathjax/', resolvedBaseUrl));
  const fontRoot = directoryUrl(new URL('vendor/@mathjax/mathjax-newcm-font/', resolvedBaseUrl));
  return {
    baseUrl: resolvedBaseUrl.href,
    fontRoot: fontRoot.href,
    mathJaxRoot: mathJaxRoot.href,
    scriptUrl: new URL('tex-mml-svg.js', mathJaxRoot).href,
  };
}

export function createMathJaxConfiguration(urls: MathJaxAssetUrls): MathJaxConfiguration {
  return {
    loader: {
      load: ['ui/safe', 'a11y/assistive-mml'],
      paths: {
        mathjax: withoutTrailingSlash(urls.mathJaxRoot),
        fonts: withoutTrailingSlash(new URL('../', urls.fontRoot).href),
        'mathjax-newcm': withoutTrailingSlash(urls.fontRoot),
      },
    },
    startup: {
      typeset: false,
    },
    tex: {
      packages: {
        '[-]': ['require', 'autoload', 'html', 'texhtml'],
      },
    },
    mml: {
      allowHtmlInTokenNodes: false,
      parseAs: 'xml',
    },
    svg: {
      fontCache: 'local',
    },
    options: {
      enableAssistiveMml: true,
      enableBraille: false,
      enableEnrichment: false,
      enableExplorer: false,
      enableMenu: false,
      enableSpeech: false,
      menuOptions: {
        settings: {
          assistiveMml: true,
          braille: false,
          collapsible: false,
          enrich: false,
          speech: false,
        },
      },
      safeOptions: {
        allow: {
          URLs: 'none',
          classes: 'none',
          cssIDs: 'none',
          styles: 'none',
        },
      },
    },
  };
}

function isMathJaxApi(value: MathJaxApi | MathJaxConfiguration | undefined): value is MathJaxApi {
  return Boolean(
    value
    && 'startup' in value
    && 'promise' in value.startup
    && 'tex2svgPromise' in value
    && 'mathml2svgPromise' in value,
  );
}

function waitForScript(script: HTMLScriptElement) {
  return new Promise<void>((resolve, reject) => {
    script.addEventListener('load', () => resolve(), { once: true });
    script.addEventListener(
      'error',
      () => reject(new Error('The local MathJax startup asset could not be loaded')),
      { once: true },
    );
  });
}

async function initializeMathJax(): Promise<MathJaxApi> {
  if (window.MathJax) {
    throw new MathJaxRuntimeError(
      'configuration',
      'A MathJax global already exists before strict runtime initialization',
    );
  }

  const urls = getMathJaxAssetUrls();
  window.MathJax = createMathJaxConfiguration(urls);

  const script = document.createElement('script');
  script.id = 'MathJax-script';
  script.async = true;
  script.dataset.mathjaxRuntime = 'local';
  script.src = urls.scriptUrl;
  const loaded = waitForScript(script);
  document.head.append(script);
  await loaded;

  if (!isMathJaxApi(window.MathJax)) {
    throw new Error('The local MathJax startup asset did not expose the conversion API');
  }
  await window.MathJax.startup.promise;
  return window.MathJax;
}

export function loadMathJaxRuntime(): Promise<MathJaxApi> {
  if (!initializationPromise) {
    initializationPromise = initializeMathJax().catch((error: unknown) => {
      if (!initializationWarningLogged) {
        initializationWarningLogged = true;
        console.warn('Local MathJax runtime initialization failed');
      }
      if (error instanceof MathJaxRuntimeError) throw error;
      throw new MathJaxRuntimeError(
        'initialization',
        'Local MathJax runtime initialization failed',
        error,
      );
    });
  }
  return initializationPromise;
}

function assertSafeOutput(node: HTMLElement) {
  for (const element of node.querySelectorAll('[href], [src], [xlink\\:href]')) {
    for (const attribute of ['href', 'src', 'xlink:href']) {
      const value = element.getAttribute(attribute);
      if (value && !value.startsWith('#')) {
        throw new MathJaxRuntimeError(
          'unsafe-output',
          'MathJax output contained a non-local resource reference',
        );
      }
    }
  }
}

export async function renderFormula(
  source: string,
  inputKind: MathFormulaInputKind,
  display: boolean,
): Promise<HTMLElement> {
  try {
    const mathJax = await loadMathJaxRuntime();
    const node = inputKind === 'tex'
      ? await mathJax.tex2svgPromise(source, { display })
      : await mathJax.mathml2svgPromise(source, { display });
    if (!(node instanceof HTMLElement)) {
      throw new Error('MathJax conversion did not return an HTML element');
    }
    assertSafeOutput(node);
    return node;
  } catch (error) {
    if (error instanceof MathJaxRuntimeError) throw error;
    throw new MathJaxRuntimeError('conversion', 'MathJax formula conversion failed', error);
  }
}

export function __resetMathJaxRuntimeForTests() {
  initializationPromise = null;
  initializationWarningLogged = false;
  document.querySelector('script[data-mathjax-runtime="local"]')?.remove();
  delete window.MathJax;
}
