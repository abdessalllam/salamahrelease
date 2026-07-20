#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
  createReadStream,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const DEFAULT_LANGUAGES = Object.freeze([
  "ara",
  "ben",
  "eng",
  "fas",
  "fra",
  "ind",
  "rus",
  "tam",
  "tur",
  "urd",
]);
const DEFAULT_SQLITE_PAGE_SIZE_BYTES = 16 * 1024;
const SQLITE_PAGE_SIZE_BYTES_BY_VARIANT = Object.freeze({
  combined: DEFAULT_SQLITE_PAGE_SIZE_BYTES,
  hadith: DEFAULT_SQLITE_PAGE_SIZE_BYTES,
  dhikr: DEFAULT_SQLITE_PAGE_SIZE_BYTES,
  names: 4 * 1024,
});
const CURRENT_APP_ARCHIVE_BYTES = 64 * 1024 * 1024;
const CURRENT_APP_ENTRY_BYTES = 384 * 1024 * 1024;
const CURRENT_APP_TOTAL_BYTES = 384 * 1024 * 1024;
const CURRENT_APP_MAX_ENTRIES = 256;
const CURRENT_APP_MAX_COMPRESSION_RATIO = 200;
const IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable";
const SCRIPT_DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = path.resolve(SCRIPT_DIRECTORY, "..", "..");
const TABLE_BY_DATASET = Object.freeze({
  dhikr: "dhikrs",
  hadith: "hadiths",
  hadith_editions: "hadith_editions",
  names_of_allah: "names_of_allah",
});
const VARIANTS = Object.freeze([
  Object.freeze({
    variant: "combined",
    cacheDirectory: "package",
    filePrefix: "hadithapi-offline-pack-",
    kind: "offline-pack",
    sqliteName: "offline.sqlite",
    datasets: Object.freeze([
      "dhikr",
      "hadith",
      "hadith_editions",
      "names_of_allah",
    ]),
  }),
  Object.freeze({
    variant: "hadith",
    cacheDirectory: "hadith-only",
    filePrefix: "hadithapi-hadith-offline-pack-",
    kind: "hadith",
    sqliteName: "hadith.sqlite",
    datasets: Object.freeze(["hadith", "hadith_editions"]),
  }),
  Object.freeze({
    variant: "dhikr",
    cacheDirectory: "dhikr",
    filePrefix: "hadithapi-dhikr-offline-pack-",
    kind: "dhikr",
    sqliteName: "dhikr.sqlite",
    datasets: Object.freeze(["dhikr"]),
  }),
  Object.freeze({
    variant: "names",
    cacheDirectory: "names-of-allah",
    filePrefix: "hadithapi-names-of-allah-offline-pack-",
    kind: "names-of-allah",
    sqliteName: "names_of_allah.sqlite",
    datasets: Object.freeze(["names_of_allah"]),
  }),
]);
const VALUE_ARGUMENT_KEYS = Object.freeze({
  "--source": "source",
  "--output": "output",
  "--base-url": "baseUrl",
  "--object-prefix": "objectPrefix",
  "--languages": "languages",
});

const options = parseArguments(process.argv.slice(2));

if (options.help) {
  printUsage();
  process.exit(0);
}
if (!options.source) {
  fail("--source is required.");
}

const sourceRoot = path.resolve(options.source);
const outputRoot = path.resolve(
  options.output || path.join(REPOSITORY_ROOT, "build", "public-b2-library"),
);
const languages = parseLanguages(options.languages);
const baseUrl = normalizeBaseUrl(options.baseUrl);
const objectPrefix = normalizeObjectPrefix(
  options.objectPrefix || `library/v3-${new Date().toISOString().slice(0, 10)}`,
);

if (!existsSync(sourceRoot)) {
  fail(`Source directory does not exist: ${sourceRoot}`);
}
if (
  isSameOrDescendant(outputRoot, sourceRoot) ||
  isSameOrDescendant(sourceRoot, outputRoot)
) {
  fail("--source and --output must not contain one another.");
}
if (existsSync(outputRoot) && !options.force) {
  fail(`Output already exists: ${outputRoot}. Pass --force to replace it.`);
}
if (existsSync(outputRoot)) {
  validateReplaceableOutput(outputRoot);
}

const stagedRoot = `${outputRoot}.staging-${process.pid}`;
const workRoot = path.join(stagedRoot, ".work");

rmSync(stagedRoot, { recursive: true, force: true });
mkdirSync(workRoot, { recursive: true });

try {
  const sourcePacks = discoverSourcePacks(sourceRoot, languages);
  const assets = [];

  for (const sourcePack of sourcePacks) {
    const asset = await optimizeAndStagePack({
      baseUrl,
      objectPrefix,
      sourcePack,
      stagedRoot,
      workRoot,
    });

    assets.push(asset);
    console.log(
      `  ${asset.lang} ${asset.variant}: ${formatBytes(asset.sourceArchiveBytes)} -> ` +
        `${formatBytes(asset.archiveBytes)}`,
    );
  }

  assets.sort(
    (left, right) =>
      left.lang.localeCompare(right.lang) ||
      left.variant.localeCompare(right.variant),
  );
  assertCompleteMatrix(assets, languages);

  const incompatibleObjects = assets
    .filter((asset) => !asset.currentAppCompatibility.compatible)
    .map((asset) => asset.objectKey);
  const fallbacks = assets.flatMap((asset) =>
    Object.entries(asset.datasets)
      .filter(([, dataset]) => dataset.sourceLang && dataset.sourceLang !== asset.lang)
      .map(([dataset, metadata]) => ({
        objectKey: asset.objectKey,
        requestedLang: asset.lang,
        dataset,
        sourceLang: metadata.sourceLang,
      })),
  );
  const totals = summarizeAssets(assets);
  const appMirrorBaseUrl = baseUrl
    ? publicObjectUrl(baseUrl, objectPrefix)
    : null;
  const manifest = {
    schemaVersion: 1,
    kind: "salamah-public-b2-library-release",
    generatedAt: new Date().toISOString(),
    objectPrefix,
    baseUrl,
    appMirrorBaseUrl,
    catalogObjectKey: `${objectPrefix}/library-manifest.json`,
    visibility: "public",
    objectLayout: "versioned-prefix",
    languages,
    variants: VARIANTS.map(({ variant, kind, sqliteName }) => ({
      variant,
      kind,
      sqliteName,
    })),
    b2ObjectMetadata: {
      zipContentType: "application/zip",
      cacheControl: IMMUTABLE_CACHE_CONTROL,
    },
    compression: {
      archive: "zip",
      method: "deflate",
      level: 9,
      lossless: true,
      sqliteCompaction: "VACUUM INTO",
      sqlitePageSizeBytesByVariant: SQLITE_PAGE_SIZE_BYTES_BY_VARIANT,
    },
    currentAppLimits: {
      archiveBytes: CURRENT_APP_ARCHIVE_BYTES,
      maxEntryCount: CURRENT_APP_MAX_ENTRIES,
      maxEntryUncompressedBytes: CURRENT_APP_ENTRY_BYTES,
      maxTotalUncompressedBytes: CURRENT_APP_TOTAL_BYTES,
      maxCompressionRatio: CURRENT_APP_MAX_COMPRESSION_RATIO,
    },
    incompatibleObjects,
    fallbacks,
    totals,
    assets,
  };
  const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
  const objectsText = `${toObjectsTsv(assets)}\n`;
  const checksumsText =
    `${assets.map((asset) => `${asset.archiveSha256}  ${asset.objectKey}`).join("\n")}\n`;
  const publicMetadataDirectory = path.join(
    stagedRoot,
    "release-assets",
    ...objectPrefix.split("/"),
  );

  mkdirSync(publicMetadataDirectory, { recursive: true });
  writeFileSync(path.join(stagedRoot, "manifest.json"), manifestText);
  writeFileSync(path.join(stagedRoot, "objects.tsv"), objectsText);
  writeFileSync(path.join(stagedRoot, "checksums.sha256"), checksumsText);
  writeFileSync(
    path.join(publicMetadataDirectory, "library-manifest.json"),
    manifestText,
  );
  writeFileSync(path.join(publicMetadataDirectory, "objects.tsv"), objectsText);
  writeFileSync(
    path.join(publicMetadataDirectory, "checksums.sha256"),
    checksumsText,
  );

  const uploadRows = [
    ...assets.map((asset) => ({
      sourcePath: asset.path,
      objectKey: asset.objectKey,
      contentType: "application/zip",
      cacheControl: IMMUTABLE_CACHE_CONTROL,
      bytes: asset.archiveBytes,
      sha256: asset.archiveSha256,
    })),
    ...(await Promise.all(
      [
        {
          name: "library-manifest.json",
          contentType: "application/json",
        },
        {
          name: "objects.tsv",
          contentType: "text/tab-separated-values; charset=utf-8",
        },
        {
          name: "checksums.sha256",
          contentType: "text/plain; charset=utf-8",
        },
      ].map(async ({ name, contentType }) => {
        const filePath = path.join(publicMetadataDirectory, name);

        return {
          sourcePath: toPosixPath(path.relative(stagedRoot, filePath)),
          objectKey: `${objectPrefix}/${name}`,
          contentType,
          cacheControl: IMMUTABLE_CACHE_CONTROL,
          bytes: statSync(filePath).size,
          sha256: await hashFile(filePath),
        };
      }),
    )),
  ];

  writeFileSync(
    path.join(stagedRoot, "upload.tsv"),
    `${toUploadTsv(uploadRows)}\n`,
  );
  rmSync(workRoot, { recursive: true, force: true });

  if (existsSync(outputRoot)) {
    rmSync(outputRoot, { recursive: true, force: true });
  }
  renameSync(stagedRoot, outputRoot);

  console.log("Public B2 Library staging complete.");
  console.log(
    JSON.stringify(
      {
        output: outputRoot,
        objectPrefix,
        appMirrorBaseUrl,
        incompatibleObjects,
        fallbacks,
        totals,
      },
      null,
      2,
    ),
  );
} catch (error) {
  rmSync(stagedRoot, { recursive: true, force: true });
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}

function parseArguments(argv) {
  const parsed = {};

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];

    if (argument === "--help" || argument === "-h") {
      parsed.help = true;
      continue;
    }
    if (argument === "--force") {
      parsed.force = true;
      continue;
    }
    const key = VALUE_ARGUMENT_KEYS[argument];

    if (!key) {
      fail(`Unknown argument: ${argument}`);
    }
    parsed[key] = requiredValue(argv, ++index, argument);
  }

  return parsed;
}

function requiredValue(argv, index, flag) {
  const value = argv[index];

  if (!value || value.startsWith("--")) {
    fail(`${flag} requires a value.`);
  }
  return value;
}

function printUsage() {
  console.log(`Usage:
  node .github/scripts/stage-library-release.mjs \\
    --source <offline-pack-cache> \\
    [--output <directory>] \\
    [--object-prefix <versioned/object/prefix>] \\
    [--base-url <public-bucket-or-CDN-root>] \\
    [--languages <comma-separated-codes>] \\
    [--force]

The source cache must contain schema-v2 SQLite packs for combined, Hadith,
dhikr, and Names of Allah variants. The default language set is:
${DEFAULT_LANGUAGES.join(",")}

The command compacts each SQLite database with benchmark-selected page sizes,
rebuilds a standard DEFLATE ZIP at level 9, validates every row count/hash, and
writes a complete versioned Public B2 object tree plus manifest, checksums, and
upload metadata.`);
}

function parseLanguages(value) {
  const languages = value
    ? value
        .split(",")
        .map((language) => language.trim().toLowerCase())
        .filter(Boolean)
    : [...DEFAULT_LANGUAGES];

  if (languages.length === 0 || new Set(languages).size !== languages.length) {
    fail("--languages must contain unique language codes.");
  }
  for (const language of languages) {
    if (!/^[a-z]{3}$/.test(language)) {
      fail(`Invalid language code: ${language}`);
    }
  }
  return languages.sort();
}

function normalizeBaseUrl(value) {
  if (!value) {
    return null;
  }

  let parsed;

  try {
    parsed = new URL(value);
  } catch {
    fail(`Invalid --base-url: ${value}`);
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    fail("--base-url must be HTTP(S) without credentials, a query, or a fragment.");
  }

  return parsed.toString().replace(/\/+$/, "");
}

function normalizeObjectPrefix(value) {
  const normalized = value.trim().replace(/^\/+|\/+$/g, "");
  const segments = normalized.split("/");

  if (
    !normalized ||
    segments.some(
      (segment) =>
        !segment ||
        segment === "." ||
        segment === ".." ||
        !/^[a-zA-Z0-9._-]+$/.test(segment),
    )
  ) {
    fail("--object-prefix must contain safe non-empty path segments.");
  }

  return segments.join("/");
}

function isSameOrDescendant(candidate, parent) {
  return candidate === parent || candidate.startsWith(`${parent}${path.sep}`);
}

function validateReplaceableOutput(directory) {
  if (!statSync(directory).isDirectory()) {
    fail(`Output is not a directory: ${directory}`);
  }
  if (readdirSync(directory).length === 0) {
    return;
  }

  let manifest;

  try {
    manifest = JSON.parse(readFileSync(path.join(directory, "manifest.json"), "utf8"));
  } catch {
    fail(`Refusing to replace unrecognized output: ${directory}`);
  }
  if (manifest.kind !== "salamah-public-b2-library-release") {
    fail(`Refusing to replace unrecognized output: ${directory}`);
  }
}

function discoverSourcePacks(root, languages) {
  const packs = [];

  for (const language of languages) {
    for (const variant of VARIANTS) {
      const languageRoot = path.join(root, variant.cacheDirectory, language);

      if (!existsSync(languageRoot)) {
        fail(
          `Missing ${variant.variant} cache for ${language}: ${languageRoot}`,
        );
      }

      const buildDirectories = readdirSync(languageRoot, {
        withFileTypes: true,
      })
        .filter((entry) => entry.isDirectory() && entry.name.startsWith("build-"))
        .map((entry) => path.join(languageRoot, entry.name))
        .sort()
        .reverse();
      let archivePath = null;

      for (const buildDirectory of buildDirectories) {
        const fileName = readdirSync(buildDirectory)
          .filter(
            (entry) =>
              entry.startsWith(variant.filePrefix) &&
              entry.endsWith("-sqlite.zip"),
          )
          .sort()
          .reverse()[0];

        if (fileName) {
          archivePath = path.join(buildDirectory, fileName);
          break;
        }
      }
      if (!archivePath) {
        fail(`No SQLite ${variant.variant} archive found for ${language}.`);
      }

      packs.push({
        archivePath,
        expectedLanguage: language,
        ...variant,
      });
    }
  }

  return packs;
}

async function optimizeAndStagePack({
  baseUrl,
  objectPrefix,
  sourcePack,
  stagedRoot,
  workRoot,
}) {
  run("unzip", ["-tqq", sourcePack.archivePath]);
  const entries = zipEntries(sourcePack.archivePath);

  assertExactValues(
    entries,
    ["manifest.json", sourcePack.sqliteName],
    `archive entries in ${sourcePack.archivePath}`,
  );

  const workDirectory = path.join(
    workRoot,
    sourcePack.expectedLanguage,
    sourcePack.variant,
  );
  const sourceDirectory = path.join(workDirectory, "source");
  const optimizedDirectory = path.join(workDirectory, "optimized");

  mkdirSync(sourceDirectory, { recursive: true });
  mkdirSync(optimizedDirectory, { recursive: true });
  run("unzip", [
    "-qq",
    sourcePack.archivePath,
    "manifest.json",
    sourcePack.sqliteName,
    "-d",
    sourceDirectory,
  ]);

  const sourceManifestPath = path.join(sourceDirectory, "manifest.json");
  const sourceSqlitePath = path.join(sourceDirectory, sourcePack.sqliteName);
  const manifest = JSON.parse(readFileSync(sourceManifestPath, "utf8"));
  const sourceSqliteHash = await hashFile(sourceSqlitePath);

  validatePackManifest(manifest, sourcePack, sourceSqliteHash);
  validateSqlite(sourceSqlitePath, manifest);

  const optimizedSqlitePath = path.join(
    optimizedDirectory,
    sourcePack.sqliteName,
  );
  const sqlitePageSizeBytes =
    SQLITE_PAGE_SIZE_BYTES_BY_VARIANT[sourcePack.variant];
  run("sqlite3", [
    "-batch",
    sourceSqlitePath,
    `PRAGMA page_size=${sqlitePageSizeBytes}; VACUUM INTO ${sqlString(
      optimizedSqlitePath,
    )};`,
  ]);

  const optimizedPageSize = Number(
    sqliteValue(optimizedSqlitePath, "PRAGMA page_size;"),
  );
  const freePages = Number(
    sqliteValue(optimizedSqlitePath, "PRAGMA freelist_count;"),
  );

  if (optimizedPageSize !== sqlitePageSizeBytes || freePages !== 0) {
    fail(`SQLite compaction failed for ${sourcePack.archivePath}.`);
  }
  validateSqlite(optimizedSqlitePath, manifest);

  const sqliteSha256 = await hashFile(optimizedSqlitePath);
  const optimizedManifest = structuredClone(manifest);

  optimizedManifest.files.sqlite.sha256 = sqliteSha256;
  const optimizedManifestPath = path.join(
    optimizedDirectory,
    "manifest.json",
  );
  writeFileSync(
    optimizedManifestPath,
    `${JSON.stringify(optimizedManifest, null, 2)}\n`,
  );

  const packTimestamp = new Date(manifest.generated_at);

  if (Number.isNaN(packTimestamp.getTime())) {
    fail(`Invalid generated_at in ${sourcePack.archivePath}.`);
  }
  utimesSync(optimizedManifestPath, packTimestamp, packTimestamp);
  utimesSync(optimizedSqlitePath, packTimestamp, packTimestamp);

  const objectName =
    `library__${sourcePack.expectedLanguage}__${sourcePack.kind}.zip`;
  const objectKey = `${objectPrefix}/${objectName}`;
  const relativePath = toPosixPath(path.join("release-assets", objectKey));
  const targetPath = path.join(stagedRoot, ...relativePath.split("/"));

  mkdirSync(path.dirname(targetPath), { recursive: true });
  run(
    "zip",
    [
      "-q",
      "-9",
      "-X",
      "-j",
      targetPath,
      optimizedManifestPath,
      optimizedSqlitePath,
    ],
    {
      COPYFILE_DISABLE: "1",
      TZ: "UTC",
    },
  );
  run("unzip", ["-tqq", targetPath]);
  assertExactValues(
    zipEntries(targetPath),
    ["manifest.json", sourcePack.sqliteName],
    `staged archive entries for ${objectKey}`,
  );

  const zipInfo = readZipEntryInfo(targetPath);
  const sqliteInfo = zipInfo.find(
    (entry) => entry.name === sourcePack.sqliteName,
  );

  if (
    !sqliteInfo ||
    zipInfo.some((entry) => !entry.method.startsWith("Defl"))
  ) {
    fail(`Staged archive is not standard DEFLATE: ${targetPath}`);
  }
  const archivedSqliteHash = await hashZipEntry(
    targetPath,
    sourcePack.sqliteName,
  );

  if (archivedSqliteHash !== sqliteSha256) {
    fail(`Staged SQLite hash mismatch: ${targetPath}`);
  }

  const datasets = Object.fromEntries(
    Object.entries(manifest.files.sqlite.datasets).map(([dataset, metadata]) => [
      dataset,
      {
        count: metadata.count,
        sourceLang: metadata.source_lang || sourcePack.expectedLanguage,
      },
    ]),
  );
  const archiveBytes = statSync(targetPath).size;
  const sourceArchiveBytes = statSync(sourcePack.archivePath).size;
  const sqliteBytes = statSync(optimizedSqlitePath).size;
  const sourceSqliteBytes = statSync(sourceSqlitePath).size;
  const totalUncompressedBytes = zipInfo.reduce(
    (total, entry) => total + entry.uncompressedBytes,
    0,
  );
  const maximumCompressionRatio = Math.max(
    ...zipInfo.map(
      (entry) =>
        entry.uncompressedBytes / Math.max(entry.compressedBytes, 1),
    ),
  );
  const currentAppCompatibility = {
    archiveWithinLimit: archiveBytes <= CURRENT_APP_ARCHIVE_BYTES,
    entryCountWithinLimit: zipInfo.length <= CURRENT_APP_MAX_ENTRIES,
    entriesWithinLimit: zipInfo.every(
      (entry) => entry.uncompressedBytes <= CURRENT_APP_ENTRY_BYTES,
    ),
    totalWithinLimit: totalUncompressedBytes <= CURRENT_APP_TOTAL_BYTES,
    compressionRatioWithinLimit:
      maximumCompressionRatio <= CURRENT_APP_MAX_COMPRESSION_RATIO,
  };

  currentAppCompatibility.compatible = Object.values(
    currentAppCompatibility,
  ).every(Boolean);

  rmSync(workDirectory, { recursive: true, force: true });

  return {
    lang: sourcePack.expectedLanguage,
    variant: sourcePack.variant,
    kind: sourcePack.kind,
    objectName,
    objectKey,
    path: relativePath,
    downloadUrl: baseUrl ? publicObjectUrl(baseUrl, objectKey) : null,
    schemaVersion: manifest.schema_version,
    contentVersion: manifest.content_version,
    packGeneratedAt: manifest.generated_at,
    datasets,
    sourceArchiveFileName: path.basename(sourcePack.archivePath),
    sourceArchiveBytes,
    sourceArchiveSha256: await hashFile(sourcePack.archivePath),
    archiveBytes,
    archiveSha256: await hashFile(targetPath),
    sourceSqliteBytes,
    sqliteBytes,
    sqliteSha256,
    sqliteCompressedBytes: sqliteInfo.compressedBytes,
    totalUncompressedBytes,
    maximumCompressionRatio,
    compression: {
      method: "deflate",
      level: 9,
      sqlitePageSizeBytes: optimizedPageSize,
      sqliteFreePages: freePages,
    },
    currentAppCompatibility,
  };
}

function validatePackManifest(manifest, sourcePack, sqliteSha256) {
  validatePackMetadata(manifest, sourcePack);
  const sqlite = validateSqliteManifest(manifest, sourcePack, sqliteSha256);

  assertExactValues(
    Object.keys(sqlite.datasets),
    sourcePack.datasets,
    `datasets in ${sourcePack.archivePath}`,
  );
  validateDatasetMetadata(sqlite.datasets, sourcePack.archivePath);
}

function validatePackMetadata(manifest, sourcePack) {
  const valid = [
    manifest.schema_version === 2,
    manifest.lang === sourcePack.expectedLanguage,
    isNonEmptyString(manifest.content_version),
    isNonEmptyString(manifest.generated_at),
  ].every(Boolean);

  if (!valid) {
    fail(`Invalid pack metadata: ${sourcePack.archivePath}`);
  }
}

function validateSqliteManifest(manifest, sourcePack, sqliteSha256) {
  const sqlite = manifest.files?.sqlite;
  const valid = [
    sqlite?.name === sourcePack.sqliteName,
    sqlite?.sha256 === sqliteSha256,
    sqlite?.datasets &&
      typeof sqlite.datasets === "object" &&
      !Array.isArray(sqlite.datasets),
  ].every(Boolean);

  if (!valid) {
    fail(`Invalid SQLite manifest: ${sourcePack.archivePath}`);
  }
  return sqlite;
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function validateDatasetMetadata(datasets, archivePath) {
  for (const [dataset, metadata] of Object.entries(datasets)) {
    if (!Number.isSafeInteger(metadata.count) || metadata.count <= 0) {
      fail(`Dataset ${dataset} is empty or invalid: ${archivePath}`);
    }
    if (
      metadata.source_lang !== undefined &&
      !/^[a-z]{3}$/.test(metadata.source_lang)
    ) {
      fail(`Dataset ${dataset} has an invalid source_lang.`);
    }
  }
}

function validateSqlite(sqlitePath, manifest) {
  validateSqliteMetadata(sqlitePath, manifest);
  validateSqliteDatasetCounts(sqlitePath, manifest.files.sqlite.datasets);
  validateHadithSqlite(sqlitePath, manifest);
  validateDhikrSqlite(sqlitePath, manifest);
  validateNamesOfAllahSqlite(sqlitePath, manifest);
}

function validateSqliteMetadata(sqlitePath, manifest) {
  if (sqliteValue(sqlitePath, "PRAGMA integrity_check;") !== "ok") {
    fail(`SQLite integrity check failed: ${sqlitePath}`);
  }
  if (
    Number(
      sqliteValue(
        sqlitePath,
        "SELECT value FROM metadata WHERE key='schema_version';",
      ),
    ) !== manifest.schema_version ||
    sqliteValue(
      sqlitePath,
      "SELECT value FROM metadata WHERE key='lang';",
    ) !== manifest.lang
  ) {
    fail(`SQLite metadata mismatch: ${sqlitePath}`);
  }
}

function validateSqliteDatasetCounts(sqlitePath, datasets) {
  for (const [dataset, metadata] of Object.entries(datasets)) {
    const table = TABLE_BY_DATASET[dataset];

    if (!table) {
      fail(`Unknown SQLite dataset: ${dataset}`);
    }
    const count = Number(
      sqliteValue(sqlitePath, `SELECT COUNT(*) FROM ${table};`),
    );

    if (count !== metadata.count) {
      fail(`SQLite row count mismatch for ${dataset}: ${sqlitePath}`);
    }
  }
}

function validateHadithSqlite(sqlitePath, manifest) {
  const hadith = manifest.files.sqlite.datasets.hadith;

  if (!hadith) {
    return;
  }
  const mismatchedLanguages = Number(
    sqliteValue(
      sqlitePath,
      `SELECT
        (SELECT COUNT(*) FROM hadiths WHERE lang != ${sqlString(manifest.lang)}) +
        (SELECT COUNT(*) FROM hadith_editions WHERE lang != ${sqlString(manifest.lang)});`,
    ),
  );
  const invalidJson = Number(
    sqliteValue(
      sqlitePath,
      "SELECT COUNT(*) FROM hadiths " +
        "WHERE NOT json_valid(grades_json) OR NOT json_valid(references_json);",
    ),
  );
  const appVisibleRows = Number(
    sqliteValue(
      sqlitePath,
      "SELECT COUNT(*) FROM hadiths " +
        "WHERE trim(COALESCE(text, '')) != '';",
    ),
  );

  if (invalidJson !== 0) {
    fail(`SQLite contains invalid Hadith JSON: ${sqlitePath}`);
  }
  if (mismatchedLanguages !== 0) {
    fail(`SQLite contains Hadith rows for the wrong language: ${sqlitePath}`);
  }
  if (appVisibleRows !== hadith.count) {
    fail(`SQLite Hadith rows do not match the app-visible count: ${sqlitePath}`);
  }
}

function validateDhikrSqlite(sqlitePath, manifest) {
  const dhikr = manifest.files.sqlite.datasets.dhikr;

  if (
    dhikr &&
    Number(
      sqliteValue(
        sqlitePath,
        `SELECT COUNT(*) FROM dhikrs WHERE lang = ${sqlString(manifest.lang)};`,
      ),
    ) !== dhikr.count
  ) {
    fail(`SQLite dhikr rows do not match the app lookup language: ${sqlitePath}`);
  }
}

function validateNamesOfAllahSqlite(sqlitePath, manifest) {
  const names = manifest.files.sqlite.datasets.names_of_allah;

  if (!names) {
    return;
  }
  const lookupLang =
    manifest.lang === "ara"
      ? names.source_lang || manifest.lang
      : manifest.lang;
  const lookupStats = sqliteValue(
    sqlitePath,
    `SELECT COUNT(*), COUNT(DISTINCT name_number),
            SUM(CASE WHEN trim(COALESCE(arabic_name, '')) = ''
                       OR trim(COALESCE(meaning, '')) = '' THEN 1 ELSE 0 END)
     FROM names_of_allah
     WHERE lang = ${sqlString(lookupLang)};`,
  )
    .split("|")
    .map(Number);

  const valid = [
    lookupStats[0] === names.count,
    lookupStats[1] === names.count,
    lookupStats[2] === 0,
  ].every(Boolean);

  if (!valid) {
    fail(
      `SQLite Names rows do not match the app lookup language ${lookupLang}: ` +
        sqlitePath,
    );
  }
}

function assertCompleteMatrix(assets, languages) {
  const expected = languages.flatMap((language) =>
    VARIANTS.map((variant) => `${language}:${variant.variant}`),
  );
  const actual = assets.map((asset) => `${asset.lang}:${asset.variant}`);

  assertExactValues(actual, expected, "release language/variant matrix");
}

function assertExactValues(actual, expected, label) {
  const normalizedActual = [...actual].sort();
  const normalizedExpected = [...expected].sort();

  if (
    normalizedActual.length !== normalizedExpected.length ||
    normalizedActual.some(
      (value, index) => value !== normalizedExpected[index],
    )
  ) {
    fail(
      `Invalid ${label}: expected ${normalizedExpected.join(", ")}, got ` +
        normalizedActual.join(", "),
    );
  }
}

function summarizeAssets(assets) {
  const combined = assets.filter((asset) => asset.variant === "combined");
  const datasetRows = {
    dhikr: 0,
    hadith: 0,
    hadith_editions: 0,
    names_of_allah: 0,
  };

  for (const asset of combined) {
    for (const [dataset, metadata] of Object.entries(asset.datasets)) {
      datasetRows[dataset] += metadata.count;
    }
  }

  return {
    objects: assets.length,
    languages: new Set(assets.map((asset) => asset.lang)).size,
    variants: new Set(assets.map((asset) => asset.variant)).size,
    datasetRows,
    sourceArchiveBytes: sumBy(assets, "sourceArchiveBytes"),
    archiveBytes: sumBy(assets, "archiveBytes"),
    archiveBytesSaved:
      sumBy(assets, "sourceArchiveBytes") - sumBy(assets, "archiveBytes"),
    sourceSqliteBytes: sumBy(assets, "sourceSqliteBytes"),
    sqliteBytes: sumBy(assets, "sqliteBytes"),
    sqliteBytesSaved:
      sumBy(assets, "sourceSqliteBytes") - sumBy(assets, "sqliteBytes"),
  };
}

function sumBy(items, field) {
  return items.reduce((total, item) => total + item[field], 0);
}

function zipEntries(archivePath) {
  return run("unzip", ["-Z", "-1", archivePath])
    .split("\n")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function readZipEntryInfo(archivePath) {
  const expectedEntries = new Set(zipEntries(archivePath));
  const result = [];

  for (const line of run("unzip", ["-lv", archivePath]).split("\n")) {
    const fields = line.trim().split(/\s+/);
    const name = fields.at(-1);

    if (!expectedEntries.has(name)) {
      continue;
    }
    const uncompressedBytes = Number(fields[0]);
    const method = fields[1];
    const compressedBytes = Number(fields[2]);

    if (
      !Number.isSafeInteger(uncompressedBytes) ||
      !Number.isSafeInteger(compressedBytes) ||
      !method
    ) {
      fail(`Could not parse ZIP entry metadata: ${archivePath}`);
    }
    result.push({
      name,
      method,
      uncompressedBytes,
      compressedBytes,
    });
  }

  assertExactValues(
    result.map((entry) => entry.name),
    [...expectedEntries],
    `ZIP listing for ${archivePath}`,
  );
  return result;
}

function sqliteValue(sqlitePath, sql) {
  return run("sqlite3", ["-batch", "-noheader", sqlitePath, sql]);
}

function sqlString(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function run(command, arguments_, extraEnvironment = {}) {
  const result = spawnSync(command, arguments_, {
    encoding: "utf8",
    env: {
      ...process.env,
      ...extraEnvironment,
    },
    maxBuffer: 32 * 1024 * 1024,
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    fail(
      `${command} ${arguments_.join(" ")} failed: ${(
        result.stderr || result.stdout
      ).trim()}`,
    );
  }
  return result.stdout.trim();
}

async function hashFile(filePath) {
  return new Promise((resolvePromise, rejectPromise) => {
    const hash = createHash("sha256");
    const input = createReadStream(filePath);

    input.on("data", (chunk) => hash.update(chunk));
    input.on("error", rejectPromise);
    input.on("end", () => resolvePromise(hash.digest("hex")));
  });
}

async function hashZipEntry(archivePath, entryName) {
  return new Promise((resolvePromise, rejectPromise) => {
    const hash = createHash("sha256");
    const child = spawn("unzip", ["-p", archivePath, entryName], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let errorText = "";

    child.stdout.on("data", (chunk) => hash.update(chunk));
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      errorText += chunk;
    });
    child.on("error", rejectPromise);
    child.on("close", (code) => {
      if (code !== 0) {
        rejectPromise(
          new Error(
            `Could not hash ${entryName} in ${archivePath}: ${errorText.trim()}`,
          ),
        );
        return;
      }
      resolvePromise(hash.digest("hex"));
    });
  });
}

function publicObjectUrl(baseUrl, objectKey) {
  return `${baseUrl}/${objectKey
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/")}`;
}

function toObjectsTsv(assets) {
  const columns = [
    "lang",
    "variant",
    "kind",
    "objectKey",
    "downloadUrl",
    "contentVersion",
    "datasets",
    "archiveBytes",
    "archiveSha256",
    "sqliteBytes",
    "sqliteSha256",
    "currentAppCompatible",
  ];
  const rows = assets.map((asset) => ({
    ...asset,
    datasets: JSON.stringify(asset.datasets),
    currentAppCompatible: asset.currentAppCompatibility.compatible,
  }));

  return toTsv(columns, rows);
}

function toUploadTsv(rows) {
  return toTsv(
    [
      "sourcePath",
      "objectKey",
      "contentType",
      "cacheControl",
      "bytes",
      "sha256",
    ],
    rows,
  );
}

function toTsv(columns, rows) {
  return [
    columns.join("\t"),
    ...rows.map((row) =>
      columns
        .map((column) =>
          String(row[column] ?? "")
            .replaceAll("\t", " ")
            .replaceAll(/\r?\n/g, " "),
        )
        .join("\t"),
    ),
  ].join("\n");
}

function toPosixPath(value) {
  return value.split(path.sep).join("/");
}

function formatBytes(value) {
  return `${(value / 1024 / 1024).toFixed(2)} MiB`;
}

function fail(message) {
  throw new Error(message);
}
