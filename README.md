# Salamah Release Assets

This repository hosts Salamah binary asset packs through GitHub Releases.

Generated asset packs must stay out of git. Publish new mirrors through the `Publish static mirror release` workflow instead. The workflow copies the previous release payloads, adds the bundled Quran topic databases from `abdessalllam/Salamah`, rebuilds `manifest.json` and `urls.tsv`, then publishes the requested tag as the latest release.

Manual dispatch inputs:

- `tag`: release tag to create, such as `salamah-assets-20260510`. If left empty, the workflow uses the current UTC date.
- `source_release_tag`: existing release to copy current mirror payloads from.
- `salamah_ref`: Salamah branch, tag, or commit containing the bundled topic databases.

Required repository secret:

- `SALAMAH_SOURCE_TOKEN`: token with read access to the private Salamah source repository.

## Gapless Quran Audio

The gapless audio mirror is processed one reciter at a time so the complete
audio catalog never needs to fit on the build machine at once. The catalog in
`config/quran-audio-gapless.json` contains only reciters with all 114 Surah
files and a matching timing database.

Audit every source without downloading audio:

```bash
python3 scripts/mirror_gapless_quran_audio.py --audit-only
```

Stage and package one reciter without uploading:

```bash
python3 scripts/mirror_gapless_quran_audio.py \
  --pack mishari_alafasy \
  --stage-only
```

For publication, expose `B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`, and
`B2_BUCKET_NAME` through private environment storage, then run the same command
without `--stage-only`. Each reciter is uploaded as direct B2 MP3 objects under
`release-assets/quran/audio/gapless/<path-slug>/`, while deterministic stored
ZIP parts below 1.9 GB and a checksum manifest are uploaded to the latest
GitHub release. Completed staging files are removed unless `--keep-stage` is
set. Existing matching B2 objects and GitHub assets are skipped on reruns.

## Verse-by-Verse Quran Audio

The verse audio catalog in `config/quran-audio-verse.json` tracks the 27
verse-by-verse packs exposed by the app. It is based on the official
[EveryAyah recitation catalog](https://everyayah.com/recitations_ayat.html) and
contains the measured full-archive size for each available pack.

Audit all 168,372 required direct MP3 paths and the 26 available source
archives without downloading an archive:

```bash
python3 scripts/mirror_verse_quran_audio.py --audit-only
```

Stage and package one reciter:

```bash
python3 scripts/mirror_verse_quran_audio.py \
  --pack ghamadi_40kbps \
  --stage-only
```

Publication uses the same private B2 environment variables as the gapless
pipeline. Each pack is processed independently. The source ZIP is resumable,
validated, and removed after extraction to keep peak disk use bounded. Exactly
6,236 canonical ayah files are uploaded under
`release-assets/quran/audio/verse/<EveryAyah-source-path>/`; extra chapter-level
audio files are ignored. GitHub receives deterministic stored ZIP parts below
1.9 GB plus a SHA-256 manifest. Ayman Sowaid is downloaded from its individual
ayah files because EveryAyah does not publish its full archive.

## Public B2 Library

The Salamah Library release contains four schema-v2 SQLite objects for every
supported content language:

```text
library__eng__offline-pack.zip
library__eng__hadith.zip
library__eng__dhikr.zip
library__eng__names-of-allah.zip
```

Backblaze B2 serves immutable objects. It does not evaluate `lang`, `limit`,
`offset`, `book`, or `section` query parameters. The app selects the language
object first, installs its SQLite database, and applies filtering and paging
locally. The online content API remains responsible for filtered JSON requests.

Generate all four variants in a fresh Hadith API cache, then stage the complete
40-object release:

```bash
node .github/scripts/stage-library-release.mjs \
  --source /path/to/offline-pack-cache \
  --output build/public-b2-library-v3 \
  --object-prefix library/v3-2026-07-19
```

The default language matrix is
`ara,ben,eng,fas,fra,ind,rus,tam,tur,urd`. Add `--base-url` with the public
bucket or CDN root to populate download URLs and the exact app mirror base URL.

The staging command:

- requires every language and variant;
- verifies source ZIPs, manifests, hashes, SQLite integrity, JSON, and row counts;
- compacts SQLite with `VACUUM INTO` and benchmark-selected 4/16 KiB pages;
- rebuilds standard app-compatible DEFLATE ZIPs at level 9;
- records source-language fallbacks instead of claiming missing translations;
- emits `manifest.json`, `objects.tsv`, `checksums.sha256`, and `upload.tsv`;
- mirrors public catalog files beneath the same versioned object prefix; and
- reports objects that exceed the app's current archive safety limits.

Upload the contents of `release-assets/` with their relative paths preserved.
Use the content types and immutable cache header in `upload.tsv`. Never replace
objects in an existing version prefix; publish a new prefix and update the
app's static mirror base URL.
