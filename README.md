# Salamah Release Assets

This repository hosts Salamah binary asset packs through GitHub Releases.

Generated asset packs must stay out of git. Publish new mirrors through the `Publish static mirror release` workflow instead. The workflow copies the previous release payloads, adds the bundled Quran topics database from `abdessalllam/Salamah`, rebuilds `manifest.json` and `urls.tsv`, then publishes the requested tag as the latest release.

Manual dispatch inputs:

- `tag`: release tag to create, such as `salamah-assets-20260510`. If left empty, the workflow uses the current UTC date.
- `source_release_tag`: existing release to copy current mirror payloads from.
- `salamah_ref`: Salamah branch, tag, or commit containing the bundled topic database.

Required repository secret:

- `SALAMAH_SOURCE_TOKEN`: token with read access to the private Salamah source repository.
