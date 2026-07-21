#!/usr/bin/env python3

"""Audit, stage, package, and publish verse-by-verse Quran audio one pack at a time."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import mirror_gapless_quran_audio as common


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPOSITORY_ROOT / "config" / "quran-audio-verse.json"
DEFAULT_WORK_DIRECTORY = REPOSITORY_ROOT / "build" / "quran-audio-verse"
DEFAULT_B2_PREFIX = "release-assets/quran/audio/verse"
SOURCE_ROOT_URL = "https://everyayah.com/data/"
SOURCE_ARCHIVE_NAME = "000_versebyverse.zip"
ARCHIVE_HEADROOM = 10_000_000
MAX_DIRECTORY_LISTING_BYTES = 16 * 1024 * 1024
MAX_AUDIO_FILE_BYTES = 64 * 1024 * 1024
MAX_PACK_BYTES = 8_000_000_000
DISK_HEADROOM_BYTES = 1_000_000_000
USER_AGENT = "salamah-quran-verse-audio-mirror/1.0"

QURAN_VERSE_COUNTS = (
    7, 286, 200, 176, 120, 165, 206, 75, 129, 109, 123, 111, 43, 52, 99,
    128, 111, 110, 98, 135, 112, 78, 118, 64, 77, 227, 93, 88, 69, 60,
    34, 30, 73, 54, 45, 83, 182, 88, 75, 85, 54, 53, 89, 59, 37, 35,
    38, 29, 18, 45, 60, 49, 62, 55, 78, 96, 29, 22, 24, 13, 14, 11,
    11, 18, 12, 12, 30, 52, 52, 44, 28, 28, 20, 56, 40, 31, 50, 40,
    46, 42, 29, 19, 36, 25, 22, 17, 19, 26, 30, 20, 15, 21, 11, 8,
    8, 19, 5, 8, 8, 11, 11, 8, 3, 9, 5, 4, 7, 3, 6, 3, 5, 4, 5,
    6,
)


@dataclasses.dataclass(frozen=True)
class VersePack:
    pack_id: str
    path_slug: str
    source_path: str
    archive_bytes: int | None
    file_count: int

    @property
    def source_base_url(self) -> str:
        encoded_path = urllib.parse.quote(self.source_path, safe="/._-")
        return f"{SOURCE_ROOT_URL}{encoded_path}/"

    @property
    def archive_url(self) -> str | None:
        if self.archive_bytes is None:
            return None
        return f"{self.source_base_url}{SOURCE_ARCHIVE_NAME}"


@dataclasses.dataclass(frozen=True)
class SourceArchive:
    url: str
    bytes: int
    sha256: str


class DirectoryListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.audio_names: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if not href:
            return
        path = urllib.parse.urlsplit(urllib.parse.unquote(href)).path
        name = PurePosixPath(path).name
        if re.fullmatch(r"\d{6}\.mp3", name):
            self.audio_names.add(name)


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
    parser.add_argument("--download-workers", type=common.positive_integer, default=4)
    parser.add_argument("--audit-workers", type=common.positive_integer, default=8)
    parser.add_argument("--retries", type=common.positive_integer, default=4)
    parser.add_argument(
        "--max-archive-bytes",
        type=common.positive_integer,
        default=common.DEFAULT_ARCHIVE_LIMIT,
    )
    parser.add_argument(
        "--b2-bucket",
        default=os.environ.get("B2_BUCKET_NAME") or os.environ.get("B2_BUCKET"),
    )
    parser.add_argument("--b2-prefix", default=DEFAULT_B2_PREFIX)
    parser.add_argument("--skip-b2", action="store_true")
    parser.add_argument(
        "--github-repository",
        default=common.DEFAULT_GITHUB_REPOSITORY,
    )
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


def expected_verse_names() -> tuple[str, ...]:
    return tuple(
        f"{chapter_number:03d}{verse_number:03d}.mp3"
        for chapter_number, verse_count in enumerate(QURAN_VERSE_COUNTS, start=1)
        for verse_number in range(1, verse_count + 1)
    )


def load_catalog(path: Path) -> tuple[VersePack, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise common.MirrorError("unsupported verse audio catalog schema")
    file_count = payload.get("fileCount")
    if file_count != len(expected_verse_names()):
        raise common.MirrorError("catalog fileCount does not match the Quran verse count")
    raw_packs = payload.get("packs")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise common.MirrorError("catalog packs must be a non-empty list")
    packs = tuple(
        VersePack(
            pack_id=common.required_text(raw, "packId"),
            path_slug=common.required_slug(raw, "pathSlug"),
            source_path=required_source_path(raw),
            archive_bytes=optional_positive_integer(raw, "archiveBytes"),
            file_count=file_count,
        )
        for raw in raw_packs
    )
    common.assert_unique((pack.pack_id for pack in packs), "pack id")
    common.assert_unique((pack.path_slug for pack in packs), "path slug")
    common.assert_unique((pack.source_path for pack in packs), "source path")
    archive_total = sum(pack.archive_bytes or 0 for pack in packs)
    if payload.get("archiveTotalBytes") != archive_total:
        raise common.MirrorError("catalog archiveTotalBytes does not match its packs")
    return packs


def required_source_path(payload: Mapping[str, Any]) -> str:
    value = common.required_text(payload, "sourcePath")
    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or not all(character.isalnum() or character in "._-" for character in segment)
        for segment in segments
    ):
        raise common.MirrorError(f"catalog sourcePath is unsafe: {value!r}")
    return value


def optional_positive_integer(
    payload: Mapping[str, Any],
    key: str,
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        raise common.MirrorError(f"catalog {key} must be null or a positive integer")
    return value


def select_packs(
    packs: Sequence[VersePack],
    selectors: Sequence[str],
) -> tuple[VersePack, ...]:
    if not selectors:
        return tuple(packs)
    selected: list[VersePack] = []
    for selector in selectors:
        matches = [
            pack
            for pack in packs
            if selector in {pack.pack_id, pack.path_slug}
        ]
        if len(matches) != 1:
            raise common.MirrorError(f"unknown verse audio pack: {selector}")
        if matches[0] not in selected:
            selected.append(matches[0])
    return tuple(selected)


def fetch_directory_audio_names(
    pack: VersePack,
    retries: int,
    request_function: common.RequestFunction = common.default_request,
) -> set[str]:
    response = common.request_with_retries(
        pack.source_base_url,
        method="GET",
        retries=retries,
        request_function=request_function,
    )
    with response:
        body = response.read(MAX_DIRECTORY_LISTING_BYTES + 1)
    if len(body) > MAX_DIRECTORY_LISTING_BYTES:
        raise common.MirrorError(f"directory listing is unexpectedly large: {pack.source_base_url}")
    parser = DirectoryListingParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.audio_names


def audit_pack(
    pack: VersePack,
    retries: int,
    request_function: common.RequestFunction = common.default_request,
) -> dict[str, Any]:
    expected = set(expected_verse_names())
    listed = fetch_directory_audio_names(pack, retries, request_function)
    missing = sorted(expected - listed)
    if missing:
        preview = ", ".join(missing[:5])
        raise common.MirrorError(
            f"{pack.path_slug} directory is missing {len(missing)} expected files: {preview}"
        )
    archive: dict[str, Any] | None = None
    if pack.archive_url is not None and pack.archive_bytes is not None:
        response = common.request_with_retries(
            pack.archive_url,
            method="HEAD",
            retries=retries,
            request_function=request_function,
        )
        with response:
            content_length = response.headers.get("Content-Length")
            if content_length is None or not content_length.isdigit():
                raise common.MirrorError(f"archive size is missing: {pack.archive_url}")
            actual_bytes = int(content_length)
            if actual_bytes != pack.archive_bytes:
                raise common.MirrorError(
                    f"{pack.path_slug} archive size changed: "
                    f"{actual_bytes} != {pack.archive_bytes}"
                )
            final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise common.MirrorError(f"archive redirected outside HTTPS: {final_url}")
        archive = {
            "url": pack.archive_url,
            "finalUrl": final_url,
            "bytes": actual_bytes,
        }
    return {
        "schemaVersion": 1,
        "auditedAt": common.utc_now(),
        "packId": pack.pack_id,
        "pathSlug": pack.path_slug,
        "sourcePath": pack.source_path,
        "sourceBaseUrl": pack.source_base_url,
        "fileCount": len(expected),
        "listedFileCount": len(listed),
        "ignoredExtraAudioFiles": len(listed - expected),
        "archive": archive,
    }


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n",
        encoding="utf-8",
    )


def audit_direct_files(
    pack: VersePack,
    workers: int,
    retries: int,
    request_function: common.RequestFunction = common.default_request,
) -> tuple[common.RemoteAudioFile, ...]:
    def inspect(name: str) -> common.RemoteAudioFile:
        source_url = f"{pack.source_base_url}{name}"
        response = common.request_with_retries(
            source_url,
            method="HEAD",
            retries=retries,
            request_function=request_function,
        )
        with response:
            content_length = response.headers.get("Content-Length")
            if (
                content_length is None
                or not content_length.isdigit()
                or int(content_length) <= 0
            ):
                raise common.MirrorError(f"missing Content-Length for {source_url}")
            return common.RemoteAudioFile(
                name=name,
                source_url=source_url,
                final_url=response.geturl(),
                bytes=int(content_length),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        files = tuple(executor.map(inspect, expected_verse_names()))
    return files


def load_complete_direct_pack(
    pack: VersePack,
    direct_directory: Path,
    checkpoint_directory: Path,
) -> tuple[common.LocalAudioFile, ...] | None:
    names = expected_verse_names()
    if any(
        not (direct_directory / name).is_file()
        or not (checkpoint_directory / f"{name}.json").is_file()
        for name in names
    ):
        return None
    files: list[common.LocalAudioFile] = []
    for name in names:
        target = direct_directory / name
        try:
            payload = json.loads(
                (checkpoint_directory / f"{name}.json").read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            return None
        byte_count = payload.get("bytes")
        source_url = f"{pack.source_base_url}{name}"
        if not isinstance(byte_count, int) or byte_count <= 0:
            return None
        remote = common.RemoteAudioFile(
            name=name,
            source_url=source_url,
            final_url=source_url,
            bytes=byte_count,
        )
        digest = common.reusable_file_digest(
            target,
            remote,
            checkpoint_directory / f"{name}.json",
        )
        if digest is None:
            return None
        files.append(
            common.LocalAudioFile(
                name=name,
                path=target,
                source_url=source_url,
                final_url=source_url,
                bytes=digest[0],
                sha256=digest[1],
            )
        )
    return tuple(files)


def download_resumable_archive(
    url: str,
    expected_bytes: int,
    target: Path,
    retries: int,
    request_function: common.RequestFunction = common.default_request,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == expected_bytes:
        return
    if target.exists():
        target.unlink()
    partial = target.with_suffix(f"{target.suffix}.part")
    if partial.exists() and partial.stat().st_size > expected_bytes:
        partial.unlink()
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, method="GET", headers=headers)
        try:
            response = request_function(request, 120)
            with response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if offset:
                    expected_prefix = f"bytes {offset}-"
                    content_range = response.headers.get("Content-Range", "")
                    if status != 206 or not content_range.startswith(expected_prefix):
                        partial.unlink(missing_ok=True)
                        raise common.MirrorError(
                            f"source did not honor archive resume at byte {offset}"
                        )
                elif status not in {200, 206}:
                    raise common.MirrorError(f"archive download returned HTTP {status}")
                mode = "ab" if offset else "wb"
                with partial.open(mode) as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            current_bytes = partial.stat().st_size
            if current_bytes == expected_bytes:
                os.replace(partial, target)
                return
            if current_bytes > expected_bytes:
                partial.unlink(missing_ok=True)
                raise common.MirrorError(
                    f"archive exceeded its audited size: {current_bytes} > {expected_bytes}"
                )
            last_error = common.MirrorError(
                f"incomplete archive download: {current_bytes} < {expected_bytes}"
            )
        except (
            common.MirrorError,
            OSError,
            http.client.HTTPException,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = error
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    raise common.MirrorError(f"archive download failed for {url}: {last_error}")


def archive_audio_members(
    archive: zipfile.ZipFile,
    expected_names: Iterable[str],
) -> dict[str, zipfile.ZipInfo]:
    expected = set(expected_names)
    members: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = PurePosixPath(info.filename).name
        if name not in expected:
            continue
        if name in members:
            raise common.MirrorError(f"duplicate archive audio member: {name}")
        if info.flag_bits & 0x1:
            raise common.MirrorError(f"encrypted archive audio member: {name}")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise common.MirrorError(f"unsupported archive compression for {name}")
        if info.file_size <= 0 or info.file_size > MAX_AUDIO_FILE_BYTES:
            raise common.MirrorError(f"unsafe archive audio size for {name}")
        members[name] = info
    missing = sorted(expected - members.keys())
    if missing:
        preview = ", ".join(missing[:5])
        raise common.MirrorError(
            f"source archive is missing {len(missing)} expected files: {preview}"
        )
    if sum(info.file_size for info in members.values()) > MAX_PACK_BYTES:
        raise common.MirrorError("source archive expands beyond the pack safety limit")
    return members


def extract_source_archive(
    pack: VersePack,
    archive_path: Path,
    direct_directory: Path,
    checkpoint_directory: Path,
) -> tuple[common.LocalAudioFile, ...]:
    direct_directory.mkdir(parents=True, exist_ok=True)
    checkpoint_directory.mkdir(parents=True, exist_ok=True)
    files: list[common.LocalAudioFile] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive_audio_members(archive, expected_verse_names())
            for name in expected_verse_names():
                info = members[name]
                source_url = f"{pack.source_base_url}{name}"
                remote = common.RemoteAudioFile(
                    name=name,
                    source_url=source_url,
                    final_url=source_url,
                    bytes=info.file_size,
                )
                target = direct_directory / name
                checkpoint = checkpoint_directory / f"{name}.json"
                digest = common.reusable_file_digest(target, remote, checkpoint)
                if digest is None:
                    temporary = target.with_suffix(f"{target.suffix}.part")
                    temporary.unlink(missing_ok=True)
                    try:
                        with archive.open(info) as source, temporary.open("wb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        digest = common.file_digest(temporary)
                        if digest[0] != info.file_size:
                            raise common.MirrorError(
                                f"archive extraction size mismatch for {name}"
                            )
                        os.replace(temporary, target)
                        common.write_download_checkpoint(
                            checkpoint,
                            remote,
                            digest[1],
                        )
                    finally:
                        temporary.unlink(missing_ok=True)
                files.append(
                    common.LocalAudioFile(
                        name=name,
                        path=target,
                        source_url=source_url,
                        final_url=source_url,
                        bytes=digest[0],
                        sha256=digest[1],
                    )
                )
    except zipfile.BadZipFile as error:
        raise common.MirrorError(f"invalid source archive: {error}") from error
    return tuple(files)


def source_archive_metadata_path(pack_root: Path) -> Path:
    return pack_root / "source-archive.json"


def load_source_archive_metadata(pack_root: Path) -> SourceArchive | None:
    try:
        payload = json.loads(
            source_archive_metadata_path(pack_root).read_text(encoding="utf-8")
        )
        metadata = SourceArchive(
            url=common.required_text(payload, "url"),
            bytes=int(payload["bytes"]),
            sha256=common.required_text(payload, "sha256"),
        )
        if (
            not metadata.url.startswith("https://")
            or metadata.bytes <= 0
            or re.fullmatch(r"[0-9a-f]{64}", metadata.sha256) is None
        ):
            return None
        return metadata
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def require_free_disk_space(
    path: Path,
    required_bytes: int,
    operation: str,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(path).free
    if free_bytes < required_bytes:
        raise common.MirrorError(
            f"{operation} requires {required_bytes} free bytes; only {free_bytes} are available"
        )


def stage_pack(
    pack: VersePack,
    pack_root: Path,
    audit_workers: int,
    download_workers: int,
    retries: int,
) -> tuple[tuple[common.LocalAudioFile, ...], SourceArchive | None]:
    direct_directory = pack_root / "direct"
    checkpoint_directory = pack_root / "download-state"
    complete = load_complete_direct_pack(
        pack,
        direct_directory,
        checkpoint_directory,
    )
    if complete is not None:
        return complete, load_source_archive_metadata(pack_root)
    if pack.archive_url is None or pack.archive_bytes is None:
        remote_files = audit_direct_files(
            pack,
            workers=audit_workers,
            retries=retries,
        )
        files = common.stage_pack(
            remote_files,
            direct_directory,
            workers=download_workers,
            retries=retries,
            checkpoint_directory=checkpoint_directory,
        )
        return files, None

    require_free_disk_space(
        pack_root,
        pack.archive_bytes + MAX_PACK_BYTES + DISK_HEADROOM_BYTES,
        f"staging {pack.path_slug}",
    )
    archive_path = pack_root / "source" / SOURCE_ARCHIVE_NAME
    download_resumable_archive(
        pack.archive_url,
        pack.archive_bytes,
        archive_path,
        retries,
    )
    archive_size, archive_sha256 = common.file_digest(archive_path)
    if archive_size != pack.archive_bytes:
        raise common.MirrorError(
            f"source archive size mismatch: {archive_size} != {pack.archive_bytes}"
        )
    source_archive = SourceArchive(
        url=pack.archive_url,
        bytes=archive_size,
        sha256=archive_sha256,
    )
    write_json(
        source_archive_metadata_path(pack_root),
        dataclasses.asdict(source_archive),
    )
    files = extract_source_archive(
        pack,
        archive_path,
        direct_directory,
        checkpoint_directory,
    )
    # The validated direct files are sufficient for upload and repackaging.
    # Removing the source ZIP keeps the largest reciter below the local disk budget.
    archive_path.unlink()
    archive_path.parent.rmdir()
    return files, source_archive


def partition_files(
    files: Sequence[common.LocalAudioFile],
    max_archive_bytes: int,
) -> tuple[tuple[common.LocalAudioFile, ...], ...]:
    payload_limit = max_archive_bytes - ARCHIVE_HEADROOM
    if payload_limit <= 0:
        raise common.MirrorError("archive limit is too small")
    partitions: list[list[common.LocalAudioFile]] = []
    current: list[common.LocalAudioFile] = []
    current_bytes = 0
    for file in files:
        if file.bytes > payload_limit:
            raise common.MirrorError(f"{file.name} exceeds the GitHub archive limit")
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
    pack: VersePack,
    files: Sequence[common.LocalAudioFile],
    destination: Path,
    max_archive_bytes: int,
) -> tuple[dict[str, Any], ...]:
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob(
        f"quran__audio__verse__{pack.path_slug}__part-*.zip"
    ):
        stale.unlink()
    partitions = partition_files(files, max_archive_bytes)
    archives: list[dict[str, Any]] = []
    for index, partition in enumerate(partitions, start=1):
        name = (
            f"quran__audio__verse__{pack.path_slug}__"
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
        archive_bytes, archive_sha256 = common.file_digest(archive_path)
        if archive_bytes >= max_archive_bytes:
            raise common.MirrorError(f"archive exceeds configured limit: {archive_path}")
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise common.MirrorError(f"archive CRC verification failed: {archive_path}")
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
    pack: VersePack,
    files: Sequence[common.LocalAudioFile],
    source_archive: SourceArchive | None,
    archives: Sequence[Mapping[str, Any]],
    direct_directory: Path,
    archive_directory: Path | None,
    b2_prefix: str,
) -> Path | None:
    object_prefix = f"{b2_prefix.strip('/')}/{pack.source_path}"
    payload = {
        "schemaVersion": 1,
        "generatedAt": common.utc_now(),
        "kind": "quran-audio-verse",
        "packId": pack.pack_id,
        "pathSlug": pack.path_slug,
        "sourcePath": pack.source_path,
        "sourceBaseUrl": pack.source_base_url,
        "sourceArchive": (
            dataclasses.asdict(source_archive)
            if source_archive is not None
            else None
        ),
        "objectPrefix": object_prefix,
        "fileCount": len(files),
        "totalBytes": sum(file.bytes for file in files),
        "files": [
            {
                "name": file.name,
                "sourceUrl": file.source_url,
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
        / f"quran__audio__verse__{pack.path_slug}__manifest.json"
    )
    release_manifest.write_text(text, encoding="utf-8")
    return release_manifest


def publish_b2(
    direct_directory: Path,
    pack: VersePack,
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
        f"{prefix.strip('/')}/{pack.source_path}",
        "--state-dir",
        str(state_directory),
        "--quiet",
    ]
    subprocess.run(command, check=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv or sys.argv[1:])
        packs = select_packs(
            load_catalog(arguments.catalog.expanduser().resolve()),
            arguments.pack,
        )
        work_directory = arguments.work_dir.expanduser().resolve()
        publishing_b2 = (
            not arguments.audit_only
            and not arguments.stage_only
            and not arguments.skip_b2
        )
        if publishing_b2:
            common.ensure_b2_credentials()
        canonical_names = expected_verse_names()
        for index, pack in enumerate(packs, start=1):
            pack_root = work_directory / pack.path_slug
            direct_directory = pack_root / "direct"
            state_directory = pack_root / "b2-state"
            b2_prefix = f"{arguments.b2_prefix.strip('/')}/{pack.source_path}"
            if (
                publishing_b2
                and arguments.skip_github
                and not arguments.keep_stage
                and common.b2_state_is_complete(
                    direct_directory=direct_directory,
                    bucket=arguments.b2_bucket,
                    prefix=b2_prefix,
                    state_directory=state_directory,
                    expected_names=canonical_names,
                )
            ):
                print(
                    f"[{index}/{len(packs)}] verified {pack.path_slug}; skipping",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print(
                f"[{index}/{len(packs)}] auditing {pack.path_slug}",
                file=sys.stderr,
                flush=True,
            )
            audit = audit_pack(pack, retries=arguments.retries)
            write_json(pack_root / "audit.json", audit)
            if arguments.audit_only:
                continue
            print(
                f"[{index}/{len(packs)}] staging {pack.path_slug}",
                file=sys.stderr,
                flush=True,
            )
            files, source_archive = stage_pack(
                pack,
                pack_root,
                audit_workers=arguments.audit_workers,
                download_workers=arguments.download_workers,
                retries=arguments.retries,
            )
            archive_directory = pack_root / "archives"
            should_package_github = arguments.stage_only or not arguments.skip_github
            if should_package_github:
                require_free_disk_space(
                    pack_root,
                    sum(file.bytes for file in files) + DISK_HEADROOM_BYTES,
                    f"packaging {pack.path_slug}",
                )
            archives = (
                build_archives(
                    pack,
                    files,
                    archive_directory,
                    arguments.max_archive_bytes,
                )
                if should_package_github
                else ()
            )
            release_manifest = write_pack_manifest(
                pack,
                files,
                source_archive,
                archives,
                direct_directory,
                archive_directory if should_package_github else None,
                arguments.b2_prefix,
            )
            if arguments.stage_only:
                continue
            if not arguments.skip_b2:
                publish_b2(
                    direct_directory,
                    pack,
                    arguments.b2_bucket,
                    arguments.b2_prefix,
                    state_directory,
                )
            if not arguments.skip_github:
                if release_manifest is None:
                    raise common.MirrorError("GitHub release manifest was not generated")
                common.publish_github(
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
        common.MirrorError,
        OSError,
        subprocess.CalledProcessError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        print(f"verse audio mirror failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
