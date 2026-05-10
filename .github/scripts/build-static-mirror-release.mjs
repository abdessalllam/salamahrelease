#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { mkdir, readdir, readFile, copyFile, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

const [, , previousManifestPath, releaseAssetsDirectory, outputDirectory] = process.argv;

if (!previousManifestPath || !releaseAssetsDirectory || !outputDirectory) {
  throw new Error('Usage: build-static-mirror-release.mjs <previous-manifest> <release-assets-dir> <output-dir>');
}

const releaseTag = requiredEnv('RELEASE_TAG');
const githubRepository = process.env.GITHUB_REPOSITORY || 'abdessalllam/salamahrelease';
const salamahSourcePath = process.env.SALAMAH_SOURCE_PATH || 'salamah-source';
const salamahRef = process.env.SALAMAH_REF || '';
const salamahSha = process.env.SALAMAH_SHA || '';
const sourceReleaseTag = process.env.SOURCE_RELEASE_TAG || '';
const topicSourceRelativePath = 'composeApp/src/androidMain/assets/quran/qurantopics.db';
const topicSourcePath = path.join(salamahSourcePath, topicSourceRelativePath);
const topicReleaseAssetName = 'quran__topics__qurantopics.db';
const topicOutputPath = 'quran/topics/qurantopics.db';
const releaseBaseUrl = `https://github.com/${githubRepository}/releases/download/${encodeURIComponent(releaseTag)}`;

await mkdir(releaseAssetsDirectory, { recursive: true });
await mkdir(outputDirectory, { recursive: true });
await copyFile(topicSourcePath, path.join(releaseAssetsDirectory, topicReleaseAssetName));

const previousManifest = JSON.parse(await readFile(previousManifestPath, 'utf8'));
const assetFileNames = new Set(await readdir(releaseAssetsDirectory));
const previousAssets = Array.isArray(previousManifest.assets) ? previousManifest.assets : [];
const retainedAssets = previousAssets.filter((asset) => (
  asset.id !== 'quran-topics-qurantopics' &&
  asset.outputPath !== topicOutputPath &&
  asset.releaseAssetName !== topicReleaseAssetName
));

const assets = [];
for (const asset of retainedAssets) {
  const missing = isMissingAsset(asset);
  if (missing) {
    assets.push({
      ...asset,
      mirroredUrl: undefined,
      releaseAssetName: undefined,
      releaseDownloadUrl: undefined,
    });
    continue;
  }

  const releaseAssetName = asset.releaseAssetName || releaseAssetNameForOutputPath(asset.outputPath);
  if (!assetFileNames.has(releaseAssetName)) {
    throw new Error(`Missing copied release asset: ${releaseAssetName}`);
  }
  const fileMetadata = await fileDigest(path.join(releaseAssetsDirectory, releaseAssetName));
  const downloadUrl = `${releaseBaseUrl}/${encodeURIComponent(releaseAssetName)}`;
  assets.push({
    ...asset,
    bytes: fileMetadata.bytes,
    sha256: fileMetadata.sha256,
    mirroredUrl: downloadUrl,
    releaseAssetName,
    releaseDownloadUrl: downloadUrl,
  });
}

const topicMetadata = await fileDigest(path.join(releaseAssetsDirectory, topicReleaseAssetName));
const topicDownloadUrl = `${releaseBaseUrl}/${encodeURIComponent(topicReleaseAssetName)}`;
assets.push({
  id: 'quran-topics-qurantopics',
  kind: 'quran-topics-database',
  group: 'quran-topics',
  sourceUrl: `local://${topicSourceRelativePath}`,
  outputPath: topicOutputPath,
  mirroredUrl: topicDownloadUrl,
  bytes: topicMetadata.bytes,
  sha256: topicMetadata.sha256,
  releaseAssetName: topicReleaseAssetName,
  releaseDownloadUrl: topicDownloadUrl,
  languageCodes: ['en', 'ar'],
});

const releaseAssets = assets
  .filter((asset) => !isMissingAsset(asset))
  .map((asset) => ({
    id: asset.id,
    name: asset.releaseAssetName,
    path: `release-assets/${asset.releaseAssetName}`,
    sourcePath: asset.outputPath,
    bytes: asset.bytes,
    sha256: asset.sha256,
    downloadUrl: asset.releaseDownloadUrl,
  }));

const missingAssets = assets.filter(isMissingAsset).map((asset) => ({
  id: asset.id,
  kind: asset.kind,
  group: asset.group,
  sourceUrl: asset.sourceUrl,
  outputPath: asset.outputPath,
  statusCode: asset.statusCode,
  error: asset.error,
}));

const manifest = {
  schemaVersion: previousManifest.schemaVersion || 1,
  generatedAt: new Date().toISOString(),
  generatedBy: 'github-actions',
  sourceCommit: salamahSha || salamahRef,
  salamahRef,
  salamahSha,
  sourceReleaseTag,
  options: previousManifest.options || {},
  countsByGroup: countBy(assets, 'group'),
  countsByKind: countBy(assets, 'kind'),
  missingAssetCount: missingAssets.length,
  missingCountsByGroup: countBy(missingAssets, 'group'),
  releaseAssets,
  assets,
  missingAssets,
};

await writeFile(
  path.join(outputDirectory, 'manifest.json'),
  `${JSON.stringify(manifest, null, 2)}\n`,
);
await writeFile(
  path.join(outputDirectory, 'urls.tsv'),
  `${urlsTsv(assets)}\n`,
);

function requiredEnv(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function isMissingAsset(asset) {
  return !asset.releaseAssetName || Boolean(asset.statusCode || asset.error);
}

function releaseAssetNameForOutputPath(outputPath) {
  if (!outputPath) {
    throw new Error('Cannot derive a release asset name without outputPath');
  }
  return outputPath.split(/[\\/]+/).filter(Boolean).join('__');
}

async function fileDigest(filePath) {
  const fileStat = await stat(filePath);
  const buffer = await readFile(filePath);
  return {
    bytes: fileStat.size,
    sha256: createHash('sha256').update(buffer).digest('hex'),
  };
}

function countBy(items, key) {
  return items.reduce((counts, item) => {
    const value = item[key] || 'unknown';
    counts[value] = (counts[value] || 0) + 1;
    return counts;
  }, {});
}

function urlsTsv(items) {
  const columns = [
    'id',
    'kind',
    'group',
    'sourceUrl',
    'outputPath',
    'bytes',
    'sha256',
    'mirroredUrl',
    'releaseAssetName',
    'releaseDownloadUrl',
    'missing',
    'statusCode',
    'error',
  ];
  return [
    columns.join('\t'),
    ...items.map((asset) => columns.map((column) => tsvValue(column, asset)).join('\t')),
  ].join('\n');
}

function tsvValue(column, asset) {
  if (column === 'missing') {
    return isMissingAsset(asset) ? 'true' : 'false';
  }
  const value = asset[column];
  return value == null ? '' : String(value).replaceAll('\t', ' ').replaceAll('\n', ' ');
}
