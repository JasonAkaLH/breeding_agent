import { mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  MATHJAX_VERSION,
  STARTUP_SIZE_LIMIT,
  VENDOR_SIZE_LIMIT,
  prepareMathJaxAssets,
} from './prepare_mathjax_assets.mjs';

const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, {
    force: true,
    recursive: true,
  })));
});

async function makeFixture() {
  const { mkdtemp } = await import('node:fs/promises');
  const root = await mkdtemp(path.join(os.tmpdir(), 'mathjax-assets-'));
  temporaryDirectories.push(root);
  const nodeModules = path.join(root, 'node_modules');
  const outputRoot = path.join(root, 'public', 'vendor');

  for (const packageName of ['mathjax', '@mathjax/mathjax-newcm-font']) {
    const packageRoot = path.join(nodeModules, ...packageName.split('/'));
    await mkdir(packageRoot, { recursive: true });
    await writeFile(path.join(packageRoot, 'package.json'), JSON.stringify({ version: MATHJAX_VERSION }));
  }

  const assets = [
    ['mathjax', 'tex-mml-svg.js'],
    ['mathjax', 'ui', 'safe.js'],
    ['mathjax', 'a11y', 'assistive-mml.js'],
    ['@mathjax', 'mathjax-newcm-font', 'svg', 'dynamic', 'latin.js'],
  ];
  for (const segments of assets) {
    const assetPath = path.join(nodeModules, ...segments);
    await mkdir(path.dirname(assetPath), { recursive: true });
    await writeFile(assetPath, segments.join('/'));
  }

  return { nodeModules, outputRoot };
}

async function listFiles(root: string, relative = ''): Promise<string[]> {
  const files: string[] = [];
  for (const entry of await readdir(path.join(root, relative), { withFileTypes: true })) {
    const entryPath = path.join(relative, entry.name);
    if (entry.isDirectory()) files.push(...await listFiles(root, entryPath));
    else files.push(entryPath);
  }
  return files.sort();
}

describe('prepareMathJaxAssets', () => {
  it('recreates exactly the allowlisted offline asset tree', async () => {
    const fixture = await makeFixture();
    await mkdir(fixture.outputRoot, { recursive: true });
    await writeFile(path.join(fixture.outputRoot, 'stale.js'), 'stale');

    const result = await prepareMathJaxAssets(fixture);

    expect(await listFiles(fixture.outputRoot)).toEqual([
      '@mathjax/mathjax-newcm-font/svg/dynamic/latin.js',
      'mathjax/a11y/assistive-mml.js',
      'mathjax/tex-mml-svg.js',
      'mathjax/ui/safe.js',
    ]);
    expect(result.startupBytes).toBeLessThanOrEqual(STARTUP_SIZE_LIMIT);
    expect(result.vendorBytes).toBeLessThanOrEqual(VENDOR_SIZE_LIMIT);
  });

  it('is deterministic across repeated runs', async () => {
    const fixture = await makeFixture();
    await prepareMathJaxAssets(fixture);
    const first = await readFile(path.join(fixture.outputRoot, 'mathjax', 'tex-mml-svg.js'), 'utf8');

    await prepareMathJaxAssets(fixture);

    expect(await readFile(path.join(fixture.outputRoot, 'mathjax', 'tex-mml-svg.js'), 'utf8')).toBe(first);
  });

  it('fails before replacing output when a required source is missing', async () => {
    const fixture = await makeFixture();
    await mkdir(fixture.outputRoot, { recursive: true });
    await writeFile(path.join(fixture.outputRoot, 'keep.txt'), 'keep');
    await rm(path.join(fixture.nodeModules, 'mathjax', 'ui', 'safe.js'));

    await expect(prepareMathJaxAssets(fixture)).rejects.toThrow();
    expect(await readFile(path.join(fixture.outputRoot, 'keep.txt'), 'utf8')).toBe('keep');
  });

  it('rejects symlinks in the dynamic font source tree', async () => {
    const fixture = await makeFixture();
    const outside = path.join(path.dirname(fixture.nodeModules), 'outside.js');
    await writeFile(outside, 'outside');
    await symlink(
      outside,
      path.join(fixture.nodeModules, '@mathjax', 'mathjax-newcm-font', 'svg', 'dynamic', 'escape.js'),
    );

    await expect(prepareMathJaxAssets(fixture)).rejects.toThrow('contains a symlink');
  });

  it('requires exact matching package versions', async () => {
    const fixture = await makeFixture();
    await writeFile(
      path.join(fixture.nodeModules, 'mathjax', 'package.json'),
      JSON.stringify({ version: '4.1.2' }),
    );

    await expect(prepareMathJaxAssets(fixture)).rejects.toThrow(`Expected MathJax and NewCM font ${MATHJAX_VERSION}`);
  });
});
