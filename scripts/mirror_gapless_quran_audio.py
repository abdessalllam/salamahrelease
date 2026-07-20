#!/usr/bin/env python3

"""Audit, stage, package, and publish gapless Quran audio one reciter at a time."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.request
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPOSITORY_ROOT / "config" / "quran-audio-gapless.json"
DEFAULT_WORK_DIRECTORY = REPOSITORY_ROOT / "build" / "quran-audio-gapless"
DEFAULT_GITHUB_REPOSITORY = "abdessalllam/salamahrelease"
DEFAULT_B2_PREFIX = "release-assets/quran/audio/gapless"
DEFAULT_ARCHIVE_LIMIT = 1_900_000_000
ARCHIVE_HEADROOM = 1_000_000
USER_AGENT = "salamah-quran-audio-mirror/1.0"


class MirrorError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class GaplessPack:
    pack_id: str
    path_slug: str
    timing_database_name: str
    source_base_url: str
    file_count: int


@dataclasses.dataclass(frozen=True)
class RemoteAudioFile:
    name: str
    source_url: str
    final_url: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class LocalAudioFile:
    name: str
    path: Path
    source_url: str
    final_url: str
    bytes: int
    sha256: str


RequestFunction = Callable[[urllib.request.Request, int], Any]


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIRECTORY)
    parser.add_argument(
        "--pack",
        action="append",
        default=[],
        help="pack id or path slug; repeat to select more than one (default: all)",
    )
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument("--download-workers", type=positive_integer, default=4)
    parser.add_argument("--audit-workers", type=positive_integer, default=8)
    parser.add_argument("--retries", type=positive_integer, default=4)
    parser.add_argument(
        "--max-archive-bytes",
        type=positive_integer,
        default=DEFAULT_ARCHIVE_LIMIT,
    )
    parser.add_argument(
        "--b2-bucket",
        default=os.environ.get("B2_BUCKET_NAME") or os.environ.get("B2_BUCKET"),
    )
    parser.add_argument("--b2-prefix", default=DEFAULT_B2_PREFIX)
    parser.add_argument("--skip-b2", action="store_true")
    parser.add_argument("--github-repository", default=DEFAULT_GITHUB_REPOSITORY)
    parser.add_argument("--github-release", default="latest")
    parser.add_argument("--skip-github", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.audit_only and arguments.stage_only:
        parser.error("--audit-only and --stage-only are mutually exclusive")
    if not arguments.audit_only and not arguments.stage_only:
        if not arguments.skip_b2 and not arguments.b2_bucket:
            parser.error("--b2-bucket or B2_BUCKET_NAME is required unless --skip-b2 is used")
        if arguments.skip_b2 and arguments.skip_github:
            parser.error("both upload destinations are disabled; use --stage-only instead")
    return arguments


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def load_catalog(path: Path) -> tuple[GaplessPack, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise MirrorError("unsupported gapless audio catalog schema")
    file_count = payload.get("fileCount")
    if not isinstance(file_count, int) or file_count <= 0:
        raise MirrorError("catalog fileCount must be a positive integer")
    raw_packs = payload.get("packs")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise MirrorError("catalog packs must be a non-empty list")
    packs = tuple(
        GaplessPack(
            pack_id=required_text(raw, "packId"),
            path_slug=required_slug(raw, "pathSlug"),
            timing_database_name=required_slug(raw, "timingDatabaseName"),
            source_base_url=required_https_directory_url(raw, "sourceBaseUrl"),
            file_count=file_count,
        )
        for raw in raw_packs
    )
    assert_unique((pack.pack_id for pack in packs), "pack id")
    assert_unique((pack.path_slug for pack in packs), "path slug")
    assert_unique((pack.timing_database_name for pack in packs), "timing database")
    return packs


def required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MirrorError(f"catalog {key} must be a non-blank string")
    return value.strip()


def required_slug(payload: Mapping[str, Any], key: str) -> str:
    value = required_text(payload, key)
    if not all(character.isalnum() or character in "._-" for character in value):
        raise MirrorError(f"catalog {key} contains unsafe characters: {value!r}")
    return value


def required_https_directory_url(payload: Mapping[str, Any], key: str) -> str:
    value = required_text(payload, key)
    if not value.startswith("https://") or not value.endswith("/"):
        raise MirrorError(f"catalog {key} must be an HTTPS directory URL: {value!r}")
    return value


def assert_unique(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise MirrorError(f"duplicate {label}: {', '.join(sorted(duplicates))}")


def select_packs(
    packs: Sequence[GaplessPack],
    selectors: Sequence[str],
) -> tuple[GaplessPack, ...]:
    if not selectors:
        return tuple(packs)
    selected: list[GaplessPack] = []
    for selector in selectors:
        matches = [
            pack
            for pack in packs
            if selector in {pack.pack_id, pack.path_slug}
        ]
        if len(matches) != 1:
            raise MirrorError(f"unknown gapless audio pack: {selector}")
        if matches[0] not in selected:
            selected.append(matches[0])
    return tuple(selected)


def audio_file_name(number: int) -> str:
    return f"{number:03d}.mp3"


def verified_ssl_context() -> ssl.SSLContext:
    default_paths = ssl.get_default_verify_paths()
    if default_paths.cafile and Path(default_paths.cafile).is_file():
        return ssl.create_default_context()
    for ca_file in (
        Path("/etc/ssl/cert.pem"),
        Path("/etc/ssl/certs/ca-certificates.crt"),
    ):
        if ca_file.is_file():
            return ssl.create_default_context(cafile=str(ca_file))
    return ssl.create_default_context()


VERIFIED_SSL_CONTEXT = verified_ssl_context()


def default_request(request: urllib.request.Request, timeout: int) -> Any:
    return urllib.request.urlopen(
        request,
        timeout=timeout,
        context=VERIFIED_SSL_CONTEXT,
    )


def audit_pack(
    pack: GaplessPack,
    workers: int,
    retries: int,
    request_function: RequestFunction = default_request,
) -> tuple[RemoteAudioFile, ...]:
    def inspect(number: int) -> RemoteAudioFile:
        name = audio_file_name(number)
        source_url = f"{pack.source_base_url}{name}"
        response = request_with_retries(
            source_url,
            method="HEAD",
            retries=retries,
            request_function=request_function,
        )
        with response:
            byte_count = response.headers.get("Content-Length")
            if byte_count is None or not byte_count.isdigit() or int(byte_count) <= 0:
                raise MirrorError(f"missing Content-Length for {source_url}")
            return RemoteAudioFile(
                name=name,
                source_url=source_url,
                final_url=response.geturl(),
                bytes=int(byte_count),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        files = tuple(executor.map(inspect, range(1, pack.file_count + 1)))
    if len(files) != pack.file_count:
        raise MirrorError(f"{pack.path_slug} audit returned an incomplete file set")
    return files


def request_with_retries(
    url: str,
    method: str,
    retries: int,
    request_function: RequestFunction = default_request,
) -> Any:
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            return request_function(request, 30)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            last_error = error
            status = getattr(error, "code", None)
            if status is not None and status < 500 and status != 429:
                break
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise MirrorError(f"{method} failed for {url}: {last_error}")


def stage_pack(
    remote_files: Sequence[RemoteAudioFile],
    destination: Path,
    workers: int,
    retries: int,
    request_function: RequestFunction = default_request,
    checkpoint_directory: Path | None = None,
) -> tuple[LocalAudioFile, ...]:
    destination.mkdir(parents=True, exist_ok=True)
    checkpoint_directory = checkpoint_directory or destination.parent / "download-state"
    checkpoint_directory.mkdir(parents=True, exist_ok=True)

    def stage(remote: RemoteAudioFile) -> LocalAudioFile:
        target = destination / remote.name
        checkpoint = checkpoint_directory / f"{remote.name}.json"
        digest = reusable_file_digest(target, remote, checkpoint)
        if digest is None:
            download_file(remote, target, retries, request_function)
            digest = file_digest(target)
            write_download_checkpoint(checkpoint, remote, digest[1])
        byte_count, sha256 = digest
        if byte_count != remote.bytes:
            raise MirrorError(
                f"download size mismatch for {remote.name}: {byte_count} != {remote.bytes}"
            )
        return LocalAudioFile(
            name=remote.name,
            path=target,
            source_url=remote.source_url,
            final_url=remote.final_url,
            bytes=byte_count,
            sha256=sha256,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        files = tuple(executor.map(stage, remote_files))
    return tuple(sorted(files, key=lambda file: file.name))


def reusable_file_digest(
    target: Path,
    remote: RemoteAudioFile,
    checkpoint: Path,
) -> tuple[int, str] | None:
    if not target.is_file() or target.stat().st_size != remote.bytes:
        return None
    try:
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if (
        payload.get("schemaVersion") != 1
        or payload.get("name") != remote.name
        or payload.get("sourceUrl") != remote.source_url
        or payload.get("bytes") != remote.bytes
        or not isinstance(payload.get("sha256"), str)
    ):
        return None
    digest = file_digest(target)
    if digest[1] != payload["sha256"]:
        return None
    return digest


def write_download_checkpoint(
    path: Path,
    remote: RemoteAudioFile,
    sha256: str,
) -> None:
    payload = {
        "schemaVersion": 1,
        "name": remote.name,
        "sourceUrl": remote.source_url,
        "bytes": remote.bytes,
        "sha256": sha256,
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(f"{json.dumps(payload, sort_keys=True)}\n", encoding="utf-8")
    os.replace(temporary, path)


def download_file(
    remote: RemoteAudioFile,
    target: Path,
    retries: int,
    request_function: RequestFunction = default_request,
) -> None:
    temporary = target.with_suffix(f"{target.suffix}.part")
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        temporary.unlink(missing_ok=True)
        try:
            response = request_with_retries(
                remote.source_url,
                method="GET",
                retries=1,
                request_function=request_function,
            )
            with response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            if temporary.stat().st_size != remote.bytes:
                raise MirrorError(f"incomplete download for {remote.source_url}")
            os.replace(temporary, target)
            return
        except (
            MirrorError,
            OSError,
            http.client.HTTPException,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
        finally:
            temporary.unlink(missing_ok=True)
    raise MirrorError(f"download failed for {remote.source_url}: {last_error}")


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            byte_count += len(chunk)
            digest.update(chunk)
    return byte_count, digest.hexdigest()


def partition_files(
    files: Sequence[LocalAudioFile],
    max_archive_bytes: int,
) -> tuple[tuple[LocalAudioFile, ...], ...]:
    payload_limit = max_archive_bytes - ARCHIVE_HEADROOM
    if payload_limit <= 0:
        raise MirrorError("archive limit is too small")
    partitions: list[list[LocalAudioFile]] = []
    current: list[LocalAudioFile] = []
    current_bytes = 0
    for file in files:
        if file.bytes > payload_limit:
            raise MirrorError(f"{file.name} exceeds the GitHub archive limit")
        if current and current_bytes + file.bytes > payload_limit:
            partitions.append(current)
            current = []
            current_bytes = 0
        current.append(file)
        current_bytes += file.bytes
    if current:
        partitions.append(current)
    return tuple(tuple(partition) for partition in partitions)


def build_archives(
    pack: GaplessPack,
    files: Sequence[LocalAudioFile],
    destination: Path,
    max_archive_bytes: int,
) -> tuple[dict[str, Any], ...]:
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob(f"quran__audio__gapless__{pack.path_slug}__part-*.zip"):
        stale.unlink()
    partitions = partition_files(files, max_archive_bytes)
    archives: list[dict[str, Any]] = []
    for index, partition in enumerate(partitions, start=1):
        name = (
            f"quran__audio__gapless__{pack.path_slug}__"
            f"part-{index:02d}-of-{len(partitions):02d}.zip"
        )
        archive_path = destination / name
        with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
            for file in partition:
                info = zipfile.ZipInfo(file.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                with archive.open(info, "w", force_zip64=True) as output:
                    with file.path.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        archive_bytes, archive_sha256 = file_digest(archive_path)
        if archive_bytes >= max_archive_bytes:
            raise MirrorError(f"archive exceeds configured limit: {archive_path}")
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise MirrorError(f"archive CRC verification failed: {archive_path}")
        archives.append(
            {
                "name": name,
                "path": str(archive_path),
                "bytes": archive_bytes,
                "sha256": archive_sha256,
                "files": [file.name for file in partition],
            }
        )
    return tuple(archives)


def write_pack_manifest(
    pack: GaplessPack,
    files: Sequence[LocalAudioFile],
    archives: Sequence[Mapping[str, Any]],
    direct_directory: Path,
    archive_directory: Path | None,
    b2_prefix: str,
) -> Path | None:
    object_prefix = f"{b2_prefix.strip('/')}/{pack.path_slug}"
    payload = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "kind": "quran-audio-gapless",
        "packId": pack.pack_id,
        "pathSlug": pack.path_slug,
        "timingDatabaseName": pack.timing_database_name,
        "sourceBaseUrl": pack.source_base_url,
        "objectPrefix": object_prefix,
        "fileCount": len(files),
        "totalBytes": sum(file.bytes for file in files),
        "files": [
            {
                "name": file.name,
                "sourceUrl": file.source_url,
                "finalUrl": file.final_url,
                "objectPath": f"{object_prefix}/{file.name}",
                "bytes": file.bytes,
                "sha256": file.sha256,
            }
            for file in files
        ],
        "archives": [
            {
                key: value
                for key, value in archive.items()
                if key != "path"
            }
            for archive in archives
        ],
    }
    text = f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"
    direct_manifest = direct_directory / "manifest.json"
    direct_manifest.write_text(text, encoding="utf-8")
    if archive_directory is None:
        return None
    release_manifest = (
        archive_directory
        / f"quran__audio__gapless__{pack.path_slug}__manifest.json"
    )
    release_manifest.write_text(text, encoding="utf-8")
    return release_manifest


def publish_b2(
    direct_directory: Path,
    pack: GaplessPack,
    bucket: str,
    prefix: str,
    state_directory: Path,
) -> None:
    uploader = REPOSITORY_ROOT / "scripts" / "upload_public_to_b2.py"
    command = [
        sys.executable,
        str(uploader),
        "--source",
        str(direct_directory),
        "--bucket",
        bucket,
        "--prefix",
        f"{prefix.strip('/')}/{pack.path_slug}",
        "--state-dir",
        str(state_directory),
        "--quiet",
    ]
    subprocess.run(command, check=True)


def publish_github(
    files: Sequence[Path],
    repository: str,
    release: str,
) -> None:
    tag = resolve_github_release(repository, release)
    local = {
        file.name: file_digest(file)
        for file in files
    }
    existing = github_asset_metadata(repository, tag)
    for file in files:
        if existing.get(file.name) == local[file.name]:
            continue
        try:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "upload",
                    tag,
                    str(file),
                    "--repo",
                    repository,
                    "--clobber",
                ],
                check=True,
            )
        except subprocess.CalledProcessError:
            if github_asset_metadata(repository, tag).get(file.name) != local[file.name]:
                raise
    verified = github_asset_metadata(repository, tag)
    mismatches = [
        file.name
        for file in files
        if verified.get(file.name) != local[file.name]
    ]
    if mismatches:
        raise MirrorError(
            f"GitHub release verification failed: {', '.join(mismatches)}"
        )


def resolve_github_release(repository: str, release: str) -> str:
    if release != "latest":
        return release
    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            "--repo",
            repository,
            "--json",
            "tagName",
            "--jq",
            ".tagName",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    tag = result.stdout.strip()
    if not tag:
        raise MirrorError("could not resolve the latest GitHub release tag")
    return tag


def github_asset_metadata(
    repository: str,
    tag: str,
) -> dict[str, tuple[int, str]]:
    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "assets",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assets = json.loads(result.stdout).get("assets", [])
    metadata: dict[str, tuple[int, str]] = {}
    for asset in assets:
        name = asset.get("name")
        size = asset.get("size")
        digest = asset.get("digest")
        if (
            isinstance(name, str)
            and size is not None
            and isinstance(digest, str)
            and digest.startswith("sha256:")
        ):
            metadata[name] = (int(size), digest.removeprefix("sha256:"))
    return metadata


def write_audit(
    pack: GaplessPack,
    files: Sequence[RemoteAudioFile],
    path: Path,
) -> None:
    payload = {
        "schemaVersion": 1,
        "auditedAt": utc_now(),
        "packId": pack.pack_id,
        "pathSlug": pack.path_slug,
        "timingDatabaseName": pack.timing_database_name,
        "sourceBaseUrl": pack.source_base_url,
        "fileCount": len(files),
        "totalBytes": sum(file.bytes for file in files),
        "files": [dataclasses.asdict(file) for file in files],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv or sys.argv[1:])
        packs = select_packs(
            load_catalog(arguments.catalog.expanduser().resolve()),
            arguments.pack,
        )
        work_directory = arguments.work_dir.expanduser().resolve()
        for index, pack in enumerate(packs, start=1):
            print(
                f"[{index}/{len(packs)}] auditing {pack.path_slug}",
                file=sys.stderr,
                flush=True,
            )
            pack_root = work_directory / pack.path_slug
            remote_files = audit_pack(
                pack,
                workers=arguments.audit_workers,
                retries=arguments.retries,
            )
            write_audit(pack, remote_files, pack_root / "audit.json")
            if arguments.audit_only:
                continue
            direct_directory = pack_root / "direct"
            archive_directory = pack_root / "archives"
            local_files = stage_pack(
                remote_files,
                direct_directory,
                workers=arguments.download_workers,
                retries=arguments.retries,
            )
            should_package_github = arguments.stage_only or not arguments.skip_github
            archives = (
                build_archives(
                    pack,
                    local_files,
                    archive_directory,
                    arguments.max_archive_bytes,
                )
                if should_package_github
                else ()
            )
            release_manifest = write_pack_manifest(
                pack,
                local_files,
                archives,
                direct_directory,
                archive_directory if should_package_github else None,
                arguments.b2_prefix,
            )
            if arguments.stage_only:
                continue
            if not arguments.skip_b2:
                publish_b2(
                    direct_directory=direct_directory,
                    pack=pack,
                    bucket=arguments.b2_bucket,
                    prefix=arguments.b2_prefix,
                    state_directory=pack_root / "b2-state",
                )
            if not arguments.skip_github:
                if release_manifest is None:
                    raise MirrorError("GitHub release manifest was not generated")
                publish_github(
                    files=[
                        *(Path(archive["path"]) for archive in archives),
                        release_manifest,
                    ],
                    repository=arguments.github_repository,
                    release=arguments.github_release,
                )
            if not arguments.keep_stage:
                shutil.rmtree(direct_directory)
                if archive_directory.exists():
                    shutil.rmtree(archive_directory)
        return 0
    except (
        json.JSONDecodeError,
        MirrorError,
        OSError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        print(f"gapless audio mirror failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
