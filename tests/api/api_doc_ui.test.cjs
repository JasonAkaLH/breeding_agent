const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { execFileSync } = require('node:child_process');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');

const root = resolve(__dirname, '../..');
const originalHtml = readFileSync(resolve(root, 'docs/api/api-doc.html'), 'utf8');
const nginx = readFileSync(resolve(root, 'docker/nginx.conf'), 'utf8');

async function openPage(t, url, { secure = true, copySucceeded = true } = {}) {
  let html = originalHtml;
  if (new URL(url).pathname.startsWith('/seedpilot/')) {
    const filters = new Map([...nginx.matchAll(/sub_filter '([^']+)' '([^']+)';/g)].map(([, source, replacement]) => [source, replacement]));
    for (const [source, replacement] of filters) {
      // Each location applies these filters once; the two locations repeat them.
      html = html.replaceAll(source, replacement);
    }
  }
  const copied = [];
  const fetched = [];
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push(error));
  const dom = new JSDOM(html, {
    url, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole,
    beforeParse(window) {
      window.scrollTo = () => {};
      Object.defineProperty(window, 'isSecureContext', { value: secure });
      Object.defineProperty(window.navigator, 'clipboard', {
        value: { writeText: async (text) => copied.push(text) },
      });
      window.document.execCommand = () => {
        if (copySucceeded) copied.push(window.document.querySelector('textarea').value);
        return copySucceeded;
      };
      window.fetch = async (source) => {
        fetched.push(source);
        return {
          ok: true,
          text: async () => '# API 更新日志\n\n本地测试。',
          json: async () => ({ paths: {}, components: { schemas: {
            Example: { type: 'object', properties: { id: { type: 'string' } } },
          } } }),
        };
      };
    },
  });
  t.after(() => dom.window.close());
  await new Promise(setImmediate);
  assert.deepEqual(errors, []);
  const document = dom.window.document;
  return {
    document, copied, fetched,
    input(selector, value) {
      const element = document.querySelector(selector);
      element.value = value;
      element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    },
  };
}

for (const [url, expectedBase, resourcePrefix] of [
  ['http://server.example:41888/api-doc', 'http://server.example:41888', ''],
  ['http://server.example:41999/seedpilot/api-doc', 'http://server.example:41999/seedpilot', '/seedpilot'],
  ['https://api.example/seedpilot/api-doc/', 'https://api.example/seedpilot', '/seedpilot'],
  ['file:///tmp/api-doc.html', 'http://127.0.0.1:41888', ''],
]) {
  test(`detects the example base URL at ${url}`, async (t) => {
    const { document, fetched } = await openPage(t, url);
    assert.equal(document.querySelector('#base-url').value, expectedBase);
    const samples = [...document.querySelectorAll('code[id^="curl-"]')];
    assert.equal(samples.length, 22);
    for (const sample of samples) {
      assert.ok(sample.textContent.includes(`${expectedBase}/api/v1/`));
      assert.ok(!sample.textContent.includes('${BASE_URL}'));
      execFileSync('/bin/bash', ['-n'], { input: sample.textContent });
    }
    assert.deepEqual(fetched.sort(), [
      `${resourcePrefix}/api-doc/API更新日志.md`, `${resourcePrefix}/openapi.json`,
    ].sort());
  });
}

test('editing the base updates every example and its copied content', async (t) => {
  const page = await openPage(t, 'http://server.example:41999/seedpilot/api-doc');
  page.input('#base-url', 'https://gateway.example/team/seedpilot///');
  for (const sample of page.document.querySelectorAll('code[id^="curl-"]')) {
    assert.ok(sample.textContent.includes('https://gateway.example/team/seedpilot/api/v1/'));
    page.document.querySelector(`[data-copy-from="${sample.id}"]`).click();
    assert.equal(page.copied.at(-1), sample.textContent.trim());
  }
  page.document.querySelector('[data-copy-from="base-url"]').click();
  assert.equal(page.copied.at(-1), 'https://gateway.example/team/seedpilot');
  assert.equal(page.fetched.length, 2, 'editing examples never contacts the chosen API');
  assert.ok(page.document.querySelector('#curl-endpoint-get-api-v1-auth-me').textContent.includes('${ACCESS_TOKEN}'));
});

test('invalid base URLs cannot copy stale invocation examples and can be corrected', async (t) => {
  const page = await openPage(t, 'http://server.example:41888/api-doc');
  const button = page.document.querySelector('[data-copy-from="curl-endpoint-post-api-v1-auth-login"]');
  for (const value of ['', 'not-a-url', 'javascript:alert(1)', 'https://user:pass@example.test', 'https://example.test?query=1', 'https://example.test#fragment']) {
    page.input('#base-url', value);
    assert.equal(page.document.querySelector('#base-url').getAttribute('aria-invalid'), 'true');
    assert.equal(button.disabled, true);
    button.click();
    assert.equal(page.copied.length, 0);
  }
  page.input('#base-url', 'https://valid.example/seedpilot');
  assert.equal(page.document.querySelector('#base-url').getAttribute('aria-invalid'), 'false');
  assert.equal(button.disabled, false);
});

test('generated shell examples treat a custom URL as literal text', async (t) => {
  const page = await openPage(t, 'http://server.example:41888/api-doc');
  const base = 'https://example.test/$(printf INJECTED)/`printf INJECTED`/$ACCESS_TOKEN/$&';
  page.input('#base-url', base);
  const example = page.document.querySelector('#curl-endpoint-post-api-v1-auth-login').textContent;
  const actual = execFileSync('/bin/bash', ['-c', 'curl() { printf "%s\\n" "$3"; }\n' + example], { encoding: 'utf8' }).trim();
  assert.equal(actual, new URL(base).href + '/api/v1/auth/login');
});

test('search covers SSE, parameter names and multiple case-insensitive keywords with category filters', async (t) => {
  const page = await openPage(t, 'http://server.example:41888/api-doc');
  const visible = () => [...page.document.querySelectorAll('.endpoint-card')].filter((card) => !card.hidden);
  for (const [term, id] of [
    ['SSE', 'endpoint-get-api-v1-tasks-task-id-events'],
    ['deep_thinking', 'endpoint-post-api-v1-conversations-chat-messages'],
    ['reasoning_effort', 'endpoint-post-api-v1-conversations-chat-messages'],
    ['POST DEEP_THINKING', 'endpoint-post-api-v1-conversations-chat-messages'],
  ]) {
    page.input('#endpoint-search', term);
    assert.ok(visible().some((card) => card.id === id), term);
  }
  page.document.querySelector('[data-filter="auth"]').click();
  assert.equal(visible().length, 0);
  assert.equal(page.document.querySelector('#endpoint-empty').classList.contains('show'), true);
  page.input('#endpoint-search', 'LOGIN');
  assert.deepEqual(visible().map((card) => card.id), ['endpoint-post-api-v1-auth-login']);
  page.input('#endpoint-search', '');
  page.document.querySelector('[data-filter="all"]').click();
  assert.equal(visible().length, 22);
});

test('collapse controls all detailed sections and navigation opens the selected endpoint', async (t) => {
  const { document } = await openPage(t, 'http://server.example:41888/api-doc');
  const cards = [...document.querySelectorAll('.endpoint-card')];
  for (const card of cards) {
    const detail = card.querySelector('.endpoint-detail');
    assert.ok(detail);
    for (const section of card.querySelectorAll('.external-explanation, .param-section, .response-section, .formal-example-grid')) {
      assert.equal(section.closest('.endpoint-detail'), detail);
    }
    assert.equal(card.querySelector('.endpoint-summary').closest('details'), null);
  }
  document.querySelector('#collapse-all').click();
  assert.ok(cards.every((card) => !card.querySelector('.endpoint-detail').open));
  document.querySelector('[data-endpoint-nav="endpoint-get-api-v1-tasks-task-id-events"]').click();
  assert.equal(document.querySelector('#endpoint-get-api-v1-tasks-task-id-events .endpoint-detail').open, true);
  document.querySelector('#expand-all').click();
  assert.ok(cards.every((card) => card.querySelector('.endpoint-detail').open));
});

test('HTTP clipboard fallback reports success or failure accurately', async (t) => {
  for (const succeeded of [true, false]) {
    const page = await openPage(t, 'http://server.example:41888/api-doc', { secure: false, copySucceeded: succeeded });
    page.document.querySelector('[data-copy-from="curl-endpoint-post-api-v1-auth-login"]').click();
    await new Promise(setImmediate);
    assert.equal(page.document.querySelector('#toast').textContent, succeeded ? '已复制' : '复制失败');
    assert.equal(page.document.querySelector('textarea'), null);
  }
});

test('compat deployment references and internal anchors remain consistent', () => {
  assert.ok(!/51888|51999|breeding-agent-runtime:/.test(originalHtml));
  assert.ok(originalHtml.includes('41999/seedpilot/'));
  assert.ok(originalHtml.includes('41888'));
  assert.ok(originalHtml.includes('breeding-agent-runtime-compat'));
  const dom = new JSDOM(originalHtml);
  const ids = [...dom.window.document.querySelectorAll('[id]')].map((element) => element.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const link of dom.window.document.querySelectorAll('a[href^="#"]')) {
    assert.ok(dom.window.document.getElementById(link.getAttribute('href').slice(1)));
  }
  dom.window.close();
});
