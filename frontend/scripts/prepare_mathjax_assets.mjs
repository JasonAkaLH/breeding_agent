import { constants } from 'node:fs';
import {
  access,
  cp,
  lstat,
  mkdir,
  readdir,
  readFile,
  rm,
  stat,
} from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const MATHJAX_VERSION = '4.1.3';
export const STARTUP_SIZE_LIMIT = 2_000_000;
export const VENDOR_SIZE_LIMIT = 15 * 1024 * 1024;

const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = path.resolve(SCRIPT_DIRECTORY, '..');

const FILE_ASSETS = [
  ['mathjax', 'tex-mml-svg.js'],
  ['mathjax', 'ui', 'safe.js'],
  ['mathjax', 'a11y', 'assistive-mml.js'],
];

const DYNAMIC_FONT_ASSET = [
  '@mathjax',
  'mathjax-newcm-font',
  'svg',
  'dynamic',
];

function packageRoot(nodeModules, packageName) {
  return path.join(nodeModules, ...packageName.split('/'));
}

async function readPackageVersion(nodeModules, packageName) {
  const packageJsonPath = path.join(packageRoot(nodeModules, packageName), 'package.json');
  const packageJson = JSON.parse(await readFile(packageJsonPath, 'utf8'));
  return packageJson.version;
}

async function assertRegularFile(filePath) {
  const details = await lstat(filePath);
  if (!details.isFile() || details.isSymbolicLink()) {
    throw new Error(`MathJax asset must be a regular file: ${filePath}`);
  }
}

async function assertSafeDirectory(directoryPath) {
  const details = await lstat(directoryPath);
  if (!details.isDirectory() || details.isSymbolicLink()) {
    throw new Error(`MathJax asset directory must not be a symlink: ${directoryPath}`);
  }

  for (const entry of await readdir(directoryPath, { withFileTypes: true })) {
    const entryPath = path.join(directoryPath, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`MathJax asset tree contains a symlink: ${entryPath}`);
    }
    if (entry.isDirectory()) {
      await assertSafeDirectory(entryPath);
    } else if (!entry.isFile()) {
      throw new Error(`MathJax asset tree contains an unsupported entry: ${entryPath}`);
    }
  }
}

async function directorySize(directoryPath) {
  let bytes = 0;
  for (const entry of await readdir(directoryPath, { withFileTypes: true })) {
    const entryPath = path.join(directoryPath, entry.name);
    if (entry.isDirectory()) {
      bytes += await directorySize(entryPath);
    } else if (entry.isFile()) {
      bytes += (await stat(entryPath)).size;
    }
  }
  return bytes;
}

export async function prepareMathJaxAssets({
  nodeModules = path.join(FRONTEND_ROOT, 'node_modules'),
  outputRoot = path.join(FRONTEND_ROOT, 'public', 'vendor'),
} = {}) {
  const mathJaxVersion = await readPackageVersion(nodeModules, 'mathjax');
  const fontVersion = await readPackageVersion(nodeModules, '@mathjax/mathjax-newcm-font');
  if (mathJaxVersion !== MATHJAX_VERSION || fontVersion !== MATHJAX_VERSION) {
    throw new Error(
      `Expected MathJax and NewCM font ${MATHJAX_VERSION}; found ${mathJaxVersion} and ${fontVersion}`,
    );
  }

  const sourceFiles = FILE_ASSETS.map((segments) => ({
    source: path.join(nodeModules, ...segments),
    destination: path.join(outputRoot, ...segments),
  }));
  const dynamicSource = path.join(nodeModules, ...DYNAMIC_FONT_ASSET);
  const dynamicDestination = path.join(outputRoot, ...DYNAMIC_FONT_ASSET);

  for (const { source } of sourceFiles) {
    await assertRegularFile(source);
  }
  await assertSafeDirectory(dynamicSource);

  await rm(outputRoot, { force: true, recursive: true });
  await mkdir(outputRoot, { recursive: true });

  for (const { source, destination } of sourceFiles) {
    await mkdir(path.dirname(destination), { recursive: true });
    await cp(source, destination, { errorOnExist: true, force: false });
    await access(destination, constants.R_OK);
  }
  await mkdir(path.dirname(dynamicDestination), { recursive: true });
  await cp(dynamicSource, dynamicDestination, {
    errorOnExist: true,
    force: false,
    recursive: true,
  });

  const startupBytes = (await stat(path.join(outputRoot, 'mathjax', 'tex-mml-svg.js'))).size;
  const vendorBytes = await directorySize(outputRoot);
  if (startupBytes > STARTUP_SIZE_LIMIT) {
    throw new Error(`MathJax startup asset exceeds ${STARTUP_SIZE_LIMIT} bytes: ${startupBytes}`);
  }
  if (vendorBytes > VENDOR_SIZE_LIMIT) {
    throw new Error(`MathJax vendor assets exceed ${VENDOR_SIZE_LIMIT} bytes: ${vendorBytes}`);
  }

  return { outputRoot, startupBytes, vendorBytes };
}

async function main() {
  const result = await prepareMathJaxAssets();
  process.stdout.write(
    `Prepared MathJax ${MATHJAX_VERSION} assets (${result.vendorBytes} bytes) in ${result.outputRoot}\n`,
  );
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
