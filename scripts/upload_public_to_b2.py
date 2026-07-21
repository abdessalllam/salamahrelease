#!/usr/bin/env python3

"""Upload a complete public directory to Backblaze B2 with verified resume.

Credentials are accepted only through B2_APPLICATION_KEY_ID and
B2_APPLICATION_KEY, or hidden interactive prompts. They are never accepted as
command-line arguments or written to the state directory.

An object is complete when its exact B2 key and byte size match the local file.
The inventory checksum is derived from the relative filename and byte size; it
does not hash file contents. Remote .DS_Store objects are deleted with all
versions, and unfinished multipart uploads under the destination prefix are
cancelled, before a normal upload.
"""

from __future__ import annotations

import argparse
import codecs
import concurrent.futures
import dataclasses
import datetime as dt
import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import pty
import random
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Protocol, Sequence


SCRIPT_VERSION = "1.3.0"
MINIMUM_B2_VERSION = (4, 4, 0)
LOCK_INITIALIZATION_GRACE_SECONDS = 30
POST_EXIT_PIPE_GRACE_SECONDS = 0.5
PROCESS_TERMINATION_GRACE_SECONDS = 1.0
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    REPOSITORY_ROOT / "build" / "public-b2-library-v3-2026-07-19" / "public"
)
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
IGNORED_SOURCE_FILE_NAMES = frozenset({".DS_Store"})
IGNORED_SYNC_FILE_PATTERN = r"(^|.*/)\.DS_Store$"


class UploadError(RuntimeError):
    """Base error for a failed or unsafe upload."""


class VerificationError(UploadError):
    """Raised when the remote objects do not match the local snapshot."""


class B2CommandError(UploadError):
    """Raised after a B2 CLI command exhausts its retries."""


@dataclasses.dataclass(frozen=True)
class LocalFile:
    path: str
    bytes: int
    name_size_checksum: str

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "nameSizeChecksum": self.name_size_checksum,
        }


@dataclasses.dataclass(frozen=True)
class SourceSnapshot:
    source: Path
    files: tuple[LocalFile, ...]
    total_bytes: int
    fingerprint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": 2,
            "generatedAt": utc_now(),
            "source": str(self.source),
            "fileCount": len(self.files),
            "totalBytes": self.total_bytes,
            "fingerprint": self.fingerprint,
            "identity": "relative-filename-and-byte-size",
            "files": [file.to_json() for file in self.files],
        }


@dataclasses.dataclass(frozen=True)
class RemoteFile:
    key: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class RemoteUnfinishedUpload:
    key: str
    file_id: str


@dataclasses.dataclass(frozen=True)
class RemoteDifference:
    verified: tuple[LocalFile, ...]
    missing: tuple[LocalFile, ...]
    mismatched: tuple[LocalFile, ...]
    extras: tuple[str, ...]

    @property
    def unresolved(self) -> tuple[LocalFile, ...]:
        files = {
            file.path: file
            for file in (*self.missing, *self.mismatched)
        }
        return tuple(files[path] for path in sorted(files))

    @property
    def clean(self) -> bool:
        return not self.unresolved and not self.extras

    def summary(self) -> dict[str, int]:
        return {
            "verified": len(self.verified),
            "missing": len(self.missing),
            "mismatched": len(self.mismatched),
            "remoteExtras": len(self.extras),
        }


class UploadClient(Protocol):
    def bucket_get(self, bucket: str) -> Mapping[str, Any]: ...

    def list_files(self, bucket: str, prefix: str) -> Mapping[str, RemoteFile]: ...

    def list_ignored_metadata_keys(
        self,
        bucket: str,
        prefix: str,
    ) -> tuple[str, ...]: ...

    def delete_all_versions(self, bucket: str, key: str) -> None: ...

    def list_unfinished_uploads(
        self,
        bucket: str,
        prefix: str,
    ) -> tuple[RemoteUnfinishedUpload, ...]: ...

    def cancel_unfinished_upload(self, file_id: str) -> None: ...

    def list_version_counts(
        self,
        bucket: str,
        prefix: str,
    ) -> Mapping[str, int]: ...

    def sync(
        self,
        source: Path,
        bucket: str,
        prefix: str,
        upload_threads: int,
        dry_run: bool,
    ) -> None: ...

    def upload(
        self,
        source_file: Path,
        bucket: str,
        key: str,
        record: LocalFile,
        upload_threads: int,
    ) -> None: ...


class Logger:
    def __init__(self, log_path: Path, quiet: bool = False) -> None:
        self.log_path = log_path
        self.quiet = quiet

    def write(self, message: str) -> None:
        line = f"{utc_now()} {message}"
        with self.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{line}\n")
        if not self.quiet:
            print(line, file=sys.stderr, flush=True)


class StateLock:
    def __init__(self, state_directory: Path) -> None:
        self.lock_directory = state_directory / "lock"

    def __enter__(self) -> "StateLock":
        try:
            self.lock_directory.mkdir()
        except FileExistsError:
            pid_path = self.lock_directory / "pid"
            stale_pid = read_pid(pid_path)
            if stale_pid and process_is_running(stale_pid):
                raise UploadError(
                    f"another uploader is using this state directory (pid={stale_pid})"
                )
            if stale_pid is None and lock_age(self.lock_directory) < (
                LOCK_INITIALIZATION_GRACE_SECONDS
            ):
                raise UploadError(
                    "another uploader is initializing this state directory; "
                    "retry shortly"
                )
            shutil.rmtree(self.lock_directory)
            try:
                self.lock_directory.mkdir()
            except FileExistsError as error:
                raise UploadError(
                    "another uploader acquired the state directory lock"
                ) from error
        (self.lock_directory / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
        return self

    def __exit__(self, *_: object) -> None:
        shutil.rmtree(self.lock_directory, ignore_errors=True)


class B2Cli:
    def __init__(
        self,
        executable: str,
        application_key_id: str,
        application_key: str,
        account_info_path: Path,
        retries: int,
        logger: Logger,
        verbose: bool = False,
        stall_timeout_seconds: int = 0,
    ) -> None:
        resolved_executable = shutil.which(executable)
        if not resolved_executable:
            raise UploadError(
                "Backblaze B2 CLI was not found. Install it with "
                "`python3 -m pip install --upgrade b2`."
            )
        self.executable = resolved_executable
        self.retries = retries
        self.logger = logger
        self.verbose = verbose
        self.stall_timeout_seconds = stall_timeout_seconds
        self.secrets = tuple(
            secret for secret in (application_key_id, application_key) if secret
        )
        self.environment = os.environ.copy()
        self.environment["B2_APPLICATION_KEY_ID"] = application_key_id
        self.environment["B2_APPLICATION_KEY"] = application_key
        self.environment["B2_ACCOUNT_INFO"] = str(account_info_path)
        existing_user_agent = self.environment.get("B2_USER_AGENT_APPEND", "").strip()
        user_agent = f"salamah-public-uploader/{SCRIPT_VERSION}"
        self.environment["B2_USER_AGENT_APPEND"] = (
            f"{existing_user_agent} {user_agent}".strip()
        )

    def check_version(self) -> None:
        output = self._run(("version",), retry=False)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if not match:
            raise UploadError(f"could not parse B2 CLI version from: {output.strip()}")
        version = tuple(int(part) for part in match.groups())
        if version < MINIMUM_B2_VERSION:
            minimum = ".".join(str(part) for part in MINIMUM_B2_VERSION)
            actual = ".".join(str(part) for part in version)
            raise UploadError(
                f"B2 CLI {actual} is unsupported; install version {minimum} or newer"
            )
        self.logger.write(f"B2 CLI version verified: {'.'.join(match.groups())}")

    def authorize(self) -> None:
        self._run(("account", "authorize"))
        self.logger.write("B2 credentials authorized in an isolated temporary cache")

    def bucket_get(self, bucket: str) -> Mapping[str, Any]:
        payload = self._run_json(("bucket", "get", bucket))
        if not isinstance(payload, Mapping):
            raise B2CommandError("B2 bucket response was not a JSON object")
        return payload

    def list_files(self, bucket: str, prefix: str) -> Mapping[str, RemoteFile]:
        if self.verbose:
            self.logger.write("listing current remote objects")
        payload = self._run_json(
            ("ls", "--recursive", "--json", b2_uri(bucket, prefix, directory=True))
        )
        remote_files: dict[str, RemoteFile] = {}
        for item in json_items(payload):
            if not isinstance(item, Mapping):
                continue
            action = str(item.get("action") or "upload")
            if action not in {"upload", "copy"}:
                continue
            key = item.get("fileName") or item.get("file_name") or item.get("name")
            byte_count = (
                item.get("contentLength")
                if item.get("contentLength") is not None
                else item.get("size")
            )
            if not isinstance(key, str) or byte_count is None:
                continue
            remote_files[key] = RemoteFile(
                key=key,
                bytes=int(byte_count),
            )
        if self.verbose:
            self.logger.write(f"listed {len(remote_files)} current remote objects")
        return remote_files

    def list_ignored_metadata_keys(
        self,
        bucket: str,
        prefix: str,
    ) -> tuple[str, ...]:
        if self.verbose:
            self.logger.write("checking all remote versions for ignored metadata")
        payload = self._run_json(
            (
                "ls",
                "--versions",
                "--recursive",
                "--json",
                b2_uri(bucket, prefix, directory=True),
            )
        )
        keys = {
            key
            for item in json_items(payload)
            if isinstance(item, Mapping)
            for key in (
                item.get("fileName") or item.get("file_name") or item.get("name"),
            )
            if isinstance(key, str) and is_ignored_metadata_key(key)
        }
        sorted_keys = tuple(sorted(keys))
        if self.verbose:
            self.logger.write(
                f"found {len(sorted_keys)} ignored remote metadata keys"
            )
        return sorted_keys

    def delete_all_versions(self, bucket: str, key: str) -> None:
        arguments = ["rm"]
        if not self.verbose:
            arguments.append("--no-progress")
        arguments.extend(
            (
                "--fail-fast",
                "--versions",
                b2_uri(bucket, key, directory=False),
            )
        )
        self._run(tuple(arguments), live=self.verbose, managed=True)

    def list_unfinished_uploads(
        self,
        bucket: str,
        prefix: str,
    ) -> tuple[RemoteUnfinishedUpload, ...]:
        if self.verbose:
            self.logger.write("checking for unfinished remote multipart uploads")
        payload = self._run_json(
            (
                "ls",
                "--versions",
                "--recursive",
                "--json",
                b2_uri(bucket, prefix, directory=True),
            )
        )
        uploads: dict[str, RemoteUnfinishedUpload] = {}
        for item in json_items(payload):
            if not isinstance(item, Mapping) or item.get("action") != "start":
                continue
            key = item.get("fileName") or item.get("file_name") or item.get("name")
            file_id = item.get("fileId") or item.get("file_id")
            if not isinstance(key, str) or not isinstance(file_id, str):
                continue
            uploads[file_id] = RemoteUnfinishedUpload(key=key, file_id=file_id)
        result = tuple(
            sorted(uploads.values(), key=lambda upload: (upload.key, upload.file_id))
        )
        if self.verbose:
            self.logger.write(f"found {len(result)} unfinished multipart uploads")
        return result

    def cancel_unfinished_upload(self, file_id: str) -> None:
        self._run(
            (
                "file",
                "large",
                "unfinished",
                "cancel",
                f"b2id://{file_id}",
            ),
            live=self.verbose,
            managed=True,
        )

    def list_version_counts(
        self,
        bucket: str,
        prefix: str,
    ) -> Mapping[str, int]:
        if self.verbose:
            self.logger.write("counting completed remote file versions")
        payload = self._run_json(
            (
                "ls",
                "--versions",
                "--recursive",
                "--json",
                b2_uri(bucket, prefix, directory=True),
            )
        )
        counts: dict[str, int] = {}
        for item in json_items(payload):
            if not isinstance(item, Mapping) or item.get("action") == "start":
                continue
            key = item.get("fileName") or item.get("file_name") or item.get("name")
            if not isinstance(key, str):
                continue
            counts[key] = counts.get(key, 0) + 1
        if self.verbose:
            self.logger.write(
                "counted "
                f"{sum(counts.values())} completed versions across {len(counts)} keys"
            )
        return counts

    def sync(
        self,
        source: Path,
        bucket: str,
        prefix: str,
        upload_threads: int,
        dry_run: bool,
    ) -> None:
        arguments = ["sync"]
        if not self.verbose:
            arguments.append("--no-progress")
        arguments.extend(
            (
                "--exclude-all-symlinks",
                "--exclude-regex",
                IGNORED_SYNC_FILE_PATTERN,
                "--upload-threads",
                str(upload_threads),
                "--compare-versions",
                "size",
                "--delete",
                "--replace-newer",
            )
        )
        if dry_run:
            arguments.append("--dry-run")
        arguments.extend((str(source), b2_uri(bucket, prefix, directory=False)))
        # Re-list immediately after a failed parallel pass, then repair only
        # unresolved objects instead of restarting a potentially multi-hour sync.
        self._run(
            tuple(arguments),
            retry=False,
            live=self.verbose,
            managed=True,
        )

    def upload(
        self,
        source_file: Path,
        bucket: str,
        key: str,
        record: LocalFile,
        upload_threads: int,
    ) -> None:
        arguments = ["file", "upload"]
        if not self.verbose:
            arguments.append("--no-progress")
        arguments.extend(
            (
                "--info",
                f"name_size_checksum={record.name_size_checksum}",
                "--threads",
                str(upload_threads),
                bucket,
                str(source_file),
                key,
            )
        )
        self._run(tuple(arguments), live=self.verbose, managed=True)

    def _run_json(self, arguments: Sequence[str]) -> Any:
        return parse_json_output(self._run(arguments))

    def _run(
        self,
        arguments: Sequence[str],
        retry: bool = True,
        live: bool = False,
        managed: bool = False,
    ) -> str:
        attempts = self.retries if retry else 1
        command = (self.executable, *arguments)
        last_output = ""
        for attempt in range(1, attempts + 1):
            if live or managed:
                if live:
                    self.logger.write(
                        f"running live B2 command: {' '.join(arguments[:3])}"
                    )
                completed = self._run_managed(
                    command,
                    display=live,
                    stall_timeout_seconds=(
                        self.stall_timeout_seconds
                        if live and self.stall_timeout_seconds > 0
                        else None
                    ),
                )
            else:
                completed = subprocess.run(
                    command,
                    env=self.environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            last_output = "\n".join(
                output.strip()
                for output in (completed.stdout, completed.stderr)
                if output.strip()
            )
            if completed.returncode == 0:
                return completed.stdout
            redacted_output = redact(last_output, self.secrets)
            if attempt >= attempts or is_permanent_b2_error(redacted_output):
                command_name = " ".join(arguments[:3])
                raise B2CommandError(
                    f"`b2 {command_name}` failed (exit={completed.returncode}): "
                    f"{tail(redacted_output)}"
                )
            delay = retry_delay(attempt)
            command_name = " ".join(arguments[:3])
            self.logger.write(
                f"B2 command failed; retrying {command_name!r} "
                f"in {delay:.1f}s (attempt {attempt + 1}/{attempts})"
            )
            time.sleep(delay)
        raise B2CommandError(tail(redact(last_output, self.secrets)))

    def _run_managed(
        self,
        command: Sequence[str],
        *,
        display: bool,
        stall_timeout_seconds: int | None,
    ) -> subprocess.CompletedProcess[str]:
        master_fd: int | None = None
        slave_fd: int | None = None
        if display:
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                command,
                env=self.environment,
                stdout=slave_fd,
                stderr=slave_fd,
                bufsize=0,
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = None
            stream_fd = master_fd
        else:
            process = subprocess.Popen(
                command,
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
            )
            if process.stdout is None:
                process.kill()
                raise B2CommandError("failed to capture managed B2 command output")
            stream_fd = process.stdout.fileno()

        selector = selectors.DefaultSelector()
        selector.register(stream_fd, selectors.EVENT_READ)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output: list[str] = []
        pending = ""
        last_output_at = time.monotonic()
        parent_exited_at: float | None = None
        timed_out = False

        def display_complete_updates(text: str) -> None:
            nonlocal pending
            if not display:
                return
            pending += text
            while True:
                delimiters = [
                    position
                    for position in (pending.find("\n"), pending.find("\r"))
                    if position >= 0
                ]
                if not delimiters:
                    return
                end = min(delimiters) + 1
                sys.stderr.write(redact(pending[:end], self.secrets))
                sys.stderr.flush()
                pending = pending[end:]

        try:
            while True:
                now = time.monotonic()
                events = selector.select(timeout=0.2)
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    last_output_at = now
                    text = decoder.decode(chunk)
                    output.append(text)
                    display_complete_updates(text)

                returncode = process.poll()
                if returncode is not None:
                    if not selector.get_map():
                        break
                    if parent_exited_at is None:
                        parent_exited_at = now
                    elif now - parent_exited_at >= POST_EXIT_PIPE_GRACE_SECONDS:
                        self._terminate_process_group(process)
                        break
                elif (
                    stall_timeout_seconds is not None
                    and now - last_output_at >= stall_timeout_seconds
                ):
                    timed_out = True
                    self._terminate_process_group(process)
                    break

            final_text = decoder.decode(b"", final=True)
            if final_text:
                output.append(final_text)
                display_complete_updates(final_text)
            if pending:
                sys.stderr.write(redact(pending, self.secrets))
                sys.stderr.flush()
            if timed_out:
                timeout_message = (
                    "B2 command produced no progress for "
                    f"{stall_timeout_seconds} seconds and was terminated"
                )
                output.append(f"\n{timeout_message}\n")
                if display:
                    sys.stderr.write(f"\n{timeout_message}\n")
                    sys.stderr.flush()
            return subprocess.CompletedProcess(
                args=tuple(command),
                returncode=124 if timed_out else (process.poll() or 0),
                stdout="".join(output),
                stderr="",
            )
        except KeyboardInterrupt:
            self._terminate_process_group(process)
            raise
        finally:
            selector.close()
            if master_fd is not None:
                os.close(master_fd)
            elif process.stdout is not None:
                process.stdout.close()
            if slave_fd is not None:
                os.close(slave_fd)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            if process.poll() is None:
                process.terminate()

        deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        if process.poll() is None:
            try:
                process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload every eligible file below Public to a public Backblaze B2 bucket. "
            "Objects with the same exact key and byte size are skipped on reruns."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Credentials:
  Export B2_APPLICATION_KEY_ID and B2_APPLICATION_KEY. If either is missing in
  an interactive terminal, the script prompts for it. Credentials are never
  accepted as command-line arguments.

Example:
  export B2_APPLICATION_KEY_ID='...'
  export B2_APPLICATION_KEY='...'
  python3 scripts/upload_public_to_b2.py --bucket my-public-bucket --verbose

The application key needs listBuckets, listFiles, writeFiles, and deleteFiles.
Normal uploads delete every version of exact .DS_Store keys under the selected
prefix, cancel unfinished multipart uploads, and mirror the local source exactly.
Remote keys and older versions that are absent from the source are deleted.
""",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.environ.get("PUBLIC_DIR", DEFAULT_SOURCE)),
        help=f"Public directory to upload (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("B2_BUCKET_NAME") or os.environ.get("B2_BUCKET"),
        help="B2 bucket name (or B2_BUCKET_NAME environment variable)",
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get("B2_PREFIX", ""),
        help="optional object-key prefix inside the bucket",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="local state directory; defaults beside the source directory",
    )
    parser.add_argument(
        "--b2-command",
        default=os.environ.get("B2_COMMAND", "b2"),
        help="B2 CLI executable (default: b2)",
    )
    parser.add_argument(
        "--upload-threads",
        type=positive_integer,
        default=environment_integer("B2_UPLOAD_THREADS", 10),
        help="B2 upload threads (default: 10)",
    )
    parser.add_argument(
        "--hash-workers",
        type=positive_integer,
        default=min(8, os.cpu_count() or 4),
        help="parallel source inventory workers (default: min(8, CPU count))",
    )
    parser.add_argument(
        "--retries",
        type=positive_integer,
        default=environment_integer("B2_UPLOAD_RETRIES", 6),
        help="maximum attempts for each B2 command (default: 6)",
    )
    parser.add_argument(
        "--stall-timeout",
        type=nonnegative_integer,
        default=environment_nonnegative_integer("B2_STALL_TIMEOUT_SECONDS", 0),
        help=(
            "verbose progress inactivity timeout in seconds; "
            "0 disables it (default: 0)"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="inventory names and sizes without credentials or network access",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report remote cleanup and preview B2 sync without changing remote files",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help=(
            "verify without changes; require an exact current/version match "
            "with no unfinished uploads"
        ),
    )
    mode.add_argument(
        "--verify-state-only",
        action="store_true",
        help=(
            "verify remote objects against the saved source manifest without "
            "requiring the source files"
        ),
    )
    parser.add_argument(
        "--allow-private-bucket",
        action="store_true",
        help="permit a private destination bucket instead of requiring allPublic",
    )
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--verbose",
        action="store_true",
        help="stream B2 progress and print detailed remote-operation updates",
    )
    output_mode.add_argument(
        "--quiet",
        action="store_true",
        help="write only to the log",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    started_at = utc_now()
    try:
        arguments = parse_arguments(argv or sys.argv[1:])
        source = arguments.source.expanduser().resolve()
        prefix = normalize_prefix(arguments.prefix)
        bucket = arguments.bucket or ""
        if not arguments.plan:
            validate_bucket_name(bucket)
        if arguments.verify_state_only and arguments.state_dir is None:
            raise ValueError("--verify-state-only requires --state-dir")

        state_directory = resolve_state_directory(
            arguments.state_dir,
            source,
            bucket or "local-plan",
            prefix,
        )
        ensure_state_outside_source(state_directory, source)
        state_directory.mkdir(parents=True, exist_ok=True)
    except (OSError, UploadError, ValueError) as error:
        print(f"upload failed: {error}", file=sys.stderr)
        return 2

    logger = Logger(state_directory / "upload.log", quiet=arguments.quiet)
    report_path = state_directory / "last-run.json"

    try:
        with StateLock(state_directory):
            if arguments.verify_state_only:
                manifest_path = state_directory / "source-manifest.json"
                logger.write(f"loading saved source snapshot: {manifest_path}")
                snapshot = load_source_snapshot(manifest_path)
                source = snapshot.source
                ensure_state_outside_source(state_directory, source)
            else:
                logger.write(f"snapshotting source: {source}")
                snapshot = snapshot_source(source, arguments.hash_workers)
                write_json_atomic(
                    state_directory / "source-manifest.json", snapshot.to_json()
                )
            logger.write(
                f"source snapshot: {len(snapshot.files)} files, "
                f"{snapshot.total_bytes} bytes, fingerprint={snapshot.fingerprint}"
            )

            if arguments.plan:
                report = base_report(started_at, source, bucket, prefix, snapshot)
                report.update({"status": "planned", "completedAt": utc_now()})
                write_json_atomic(report_path, report)
                print_plan(snapshot, state_directory, arguments.quiet)
                return 0

            application_key_id, application_key = get_credentials()
            with tempfile.TemporaryDirectory(
                prefix="salamah-b2-auth-"
            ) as auth_directory:
                client = B2Cli(
                    executable=arguments.b2_command,
                    application_key_id=application_key_id,
                    application_key=application_key,
                    account_info_path=Path(auth_directory) / "account.sqlite",
                    retries=arguments.retries,
                    logger=logger,
                    verbose=arguments.verbose,
                    stall_timeout_seconds=arguments.stall_timeout,
                )
                client.check_version()
                client.authorize()
                result = perform_upload(
                    source=source,
                    snapshot=snapshot,
                    client=client,
                    bucket=bucket,
                    prefix=prefix,
                    upload_threads=arguments.upload_threads,
                    dry_run=arguments.dry_run,
                    verify_only=(
                        arguments.verify_only or arguments.verify_state_only
                    ),
                    allow_private_bucket=arguments.allow_private_bucket,
                    logger=logger,
                )

            if (
                not arguments.dry_run
                and not arguments.verify_only
                and not arguments.verify_state_only
            ):
                logger.write(
                    "re-inventorying source to confirm names and sizes did not change"
                )
                final_snapshot = snapshot_source(source, arguments.hash_workers)
                if final_snapshot.fingerprint != snapshot.fingerprint:
                    raise VerificationError(
                        "the source changed during upload; rerun to reconcile "
                        "the new snapshot"
                    )

            report = base_report(started_at, source, bucket, prefix, snapshot)
            report.update(result)
            report.update({"status": result["mode"], "completedAt": utc_now()})
            write_json_atomic(report_path, report)
            print_remote_result(result, state_directory, arguments.quiet)
            return 0
    except KeyboardInterrupt:
        logger.write(
            "upload interrupted; completed remote objects will be reused on rerun"
        )
        write_failure_report(
            report_path,
            started_at,
            source,
            bucket,
            prefix,
            "interrupted",
        )
        return 130
    except VerificationError as error:
        logger.write(f"verification failed: {error}")
        write_failure_report(
            report_path,
            started_at,
            source,
            bucket,
            prefix,
            str(error),
        )
        return 3
    except (OSError, UploadError, ValueError) as error:
        logger.write(f"upload failed: {error}")
        write_failure_report(
            report_path,
            started_at,
            source,
            bucket,
            prefix,
            str(error),
        )
        return 2


def perform_upload(
    *,
    source: Path,
    snapshot: SourceSnapshot,
    client: UploadClient,
    bucket: str,
    prefix: str,
    upload_threads: int,
    dry_run: bool,
    verify_only: bool,
    allow_private_bucket: bool,
    logger: Logger,
) -> dict[str, Any]:
    bucket_info = client.bucket_get(bucket)
    bucket_type = str(bucket_info.get("bucketType") or bucket_info.get("bucket_type"))
    if bucket_type != "allPublic" and not allow_private_bucket:
        raise UploadError(
            f"bucket {bucket!r} is {bucket_type or 'unknown'}, not allPublic; "
            "make the bucket public or pass --allow-private-bucket"
        )
    logger.write(f"destination bucket verified: {bucket!r} ({bucket_type})")

    expected_keys = {
        object_key(prefix, local_file.path) for local_file in snapshot.files
    }
    unfinished_uploads = client.list_unfinished_uploads(bucket, prefix)
    cancelled_unfinished_uploads = 0
    if unfinished_uploads:
        if verify_only:
            raise VerificationError(
                unfinished_upload_message(
                    unfinished_uploads,
                    "remote verification found unfinished multipart uploads",
                )
            )
        if dry_run:
            logger.write(
                unfinished_upload_message(
                    unfinished_uploads,
                    "dry-run would cancel unfinished multipart uploads",
                )
            )
        else:
            for index, upload in enumerate(unfinished_uploads, start=1):
                logger.write(
                    "cancelling unfinished multipart upload "
                    f"{index}/{len(unfinished_uploads)}: {upload.key}"
                )
                client.cancel_unfinished_upload(upload.file_id)
                cancelled_unfinished_uploads += 1
            remaining_unfinished_uploads = client.list_unfinished_uploads(
                bucket,
                prefix,
            )
            if remaining_unfinished_uploads:
                raise VerificationError(
                    unfinished_upload_message(
                        remaining_unfinished_uploads,
                        "unfinished multipart cancellation verification failed",
                    )
                )
            logger.write(
                "cancelled and verified "
                f"{cancelled_unfinished_uploads} unfinished multipart uploads"
            )

    ignored_metadata_keys = client.list_ignored_metadata_keys(bucket, prefix)
    deleted_metadata_keys = 0
    if ignored_metadata_keys:
        if verify_only:
            raise VerificationError(
                ignored_metadata_message(
                    ignored_metadata_keys,
                    "remote verification found forbidden metadata",
                )
            )
        if dry_run:
            logger.write(
                ignored_metadata_message(
                    ignored_metadata_keys,
                    "dry-run would delete all versions of remote metadata",
                )
            )
        else:
            for index, key in enumerate(ignored_metadata_keys, start=1):
                logger.write(
                    "deleting all versions of ignored remote metadata "
                    f"{index}/{len(ignored_metadata_keys)}: {key}"
                )
                client.delete_all_versions(bucket, key)
                deleted_metadata_keys += 1
            remaining_metadata_keys = client.list_ignored_metadata_keys(
                bucket,
                prefix,
            )
            if remaining_metadata_keys:
                raise VerificationError(
                    ignored_metadata_message(
                        remaining_metadata_keys,
                        "remote metadata deletion verification failed",
                    )
                )
            logger.write(
                f"deleted and verified {deleted_metadata_keys} remote metadata keys"
            )

    metadata_result = {
        "remoteIgnoredMetadataFound": len(ignored_metadata_keys),
        "remoteIgnoredMetadataDeleted": deleted_metadata_keys,
        "remoteUnfinishedUploadsFound": len(unfinished_uploads),
        "remoteUnfinishedUploadsCancelled": cancelled_unfinished_uploads,
        "remoteUnfinishedUploadsRemaining": (
            len(unfinished_uploads) if dry_run else 0
        ),
    }
    version_counts_before = client.list_version_counts(bucket, prefix)
    obsolete_versions_before = obsolete_version_count(
        version_counts_before,
        expected_keys,
    )
    metadata_result["remoteObsoleteVersionsFound"] = obsolete_versions_before
    metadata_result["remoteObsoleteVersionsRemaining"] = obsolete_versions_before
    remote_before = client.list_files(bucket, prefix)
    before = compare_remote(snapshot, remote_before, prefix)
    logger.write(
        f"initial remote comparison: {json.dumps(before.summary(), sort_keys=True)}"
    )

    if verify_only:
        if not before.clean or obsolete_versions_before:
            details = difference_message(before)
            if obsolete_versions_before:
                details = (
                    f"{details}; obsolete remote versions: "
                    f"{obsolete_versions_before}"
                )
            raise VerificationError(details)
        return {
            "mode": "verified",
            "bucketType": bucket_type,
            "before": before.summary(),
            "after": before.summary(),
            "syncAttempted": False,
            "repairedFiles": 0,
            **metadata_result,
        }

    if dry_run:
        sync_attempted = not before.clean or obsolete_versions_before > 0
        if sync_attempted:
            client.sync(source, bucket, prefix, upload_threads, dry_run=True)
        return {
            "mode": "dry-run",
            "bucketType": bucket_type,
            "before": before.summary(),
            "after": before.summary(),
            "syncAttempted": sync_attempted,
            "repairedFiles": 0,
            **metadata_result,
        }

    if before.clean and obsolete_versions_before == 0:
        logger.write(
            "all local files already exist with matching object names and byte sizes"
        )
        return {
            "mode": "complete",
            "bucketType": bucket_type,
            "before": before.summary(),
            "after": before.summary(),
            "syncAttempted": False,
            "repairedFiles": 0,
            **metadata_result,
        }

    sync_error: B2CommandError | None = None
    try:
        logger.write("running parallel B2 sync for exact-mirror reconciliation")
        client.sync(source, bucket, prefix, upload_threads, dry_run=False)
    except B2CommandError as error:
        sync_error = error
        logger.write(
            "B2 sync did not finish cleanly; checking completed objects before "
            "falling back to verified single-file repairs"
        )

    after_sync = compare_remote(snapshot, client.list_files(bucket, prefix), prefix)
    logger.write(
        "post-sync remote comparison: "
        f"{json.dumps(after_sync.summary(), sort_keys=True)}"
    )

    repaired_files = 0
    for index, record in enumerate(after_sync.unresolved, start=1):
        key = object_key(prefix, record.path)
        logger.write(f"repairing object {index}/{len(after_sync.unresolved)}: {key}")
        assert_local_file_unchanged(source / record.path, record)
        client.upload(
            source / record.path,
            bucket,
            key,
            record,
            upload_threads,
        )
        repaired_files += 1

    after = compare_remote(snapshot, client.list_files(bucket, prefix), prefix)
    logger.write(
        f"final remote comparison: {json.dumps(after.summary(), sort_keys=True)}"
    )
    final_unfinished_uploads = client.list_unfinished_uploads(bucket, prefix)
    if final_unfinished_uploads:
        metadata_result["remoteUnfinishedUploadsFound"] += len(
            final_unfinished_uploads
        )
        for index, upload in enumerate(final_unfinished_uploads, start=1):
            logger.write(
                "cancelling post-sync unfinished multipart upload "
                f"{index}/{len(final_unfinished_uploads)}: {upload.key}"
            )
            client.cancel_unfinished_upload(upload.file_id)
            cancelled_unfinished_uploads += 1
        remaining_unfinished_uploads = client.list_unfinished_uploads(bucket, prefix)
        if remaining_unfinished_uploads:
            raise VerificationError(
                unfinished_upload_message(
                    remaining_unfinished_uploads,
                    "post-sync unfinished multipart cancellation failed",
                )
            )
        logger.write(
            "cancelled and verified "
            f"{len(final_unfinished_uploads)} post-sync unfinished multipart uploads"
        )
    metadata_result["remoteUnfinishedUploadsCancelled"] = (
        cancelled_unfinished_uploads
    )
    metadata_result["remoteUnfinishedUploadsRemaining"] = 0

    if not after.clean:
        detail = difference_message(after)
        if sync_error:
            detail = f"{detail}; earlier sync error: {sync_error}"
        raise VerificationError(detail)

    version_counts_after = client.list_version_counts(bucket, prefix)
    obsolete_versions_after = obsolete_version_count(
        version_counts_after,
        expected_keys,
    )
    if obsolete_versions_after:
        raise VerificationError(
            "remote current files match, but exact-mirror version cleanup is "
            f"incomplete: {obsolete_versions_after} obsolete version(s) remain"
        )
    metadata_result["remoteObsoleteVersionsRemaining"] = obsolete_versions_after

    return {
        "mode": "complete",
        "bucketType": bucket_type,
        "before": before.summary(),
        "after": after.summary(),
        "syncAttempted": True,
        "syncRecoveredFromError": sync_error is not None,
        "repairedFiles": repaired_files,
        **metadata_result,
    }


def snapshot_source(source: Path, workers: int) -> SourceSnapshot:
    if not source.is_dir():
        raise UploadError(f"source directory does not exist: {source}")
    if source.is_symlink():
        raise UploadError(f"source directory must not be a symlink: {source}")

    paths = source_file_paths(source)
    if not paths:
        raise UploadError(f"source directory is empty: {source}")

    records: list[LocalFile] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(inventory_local_file, source, path): path for path in paths
        }
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda record: record.path)

    fingerprint_hash = hashlib.sha256()
    for record in records:
        fingerprint_hash.update(record.name_size_checksum.encode("ascii"))
        fingerprint_hash.update(b"\n")

    return SourceSnapshot(
        source=source,
        files=tuple(records),
        total_bytes=sum(record.bytes for record in records),
        fingerprint=fingerprint_hash.hexdigest(),
    )


# Persisted snapshots are loaded here so batch publishers can verify a finished
# remote prefix without retaining or rebuilding its multi-gigabyte source tree.
def load_source_snapshot(path: Path) -> SourceSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UploadError(f"could not read saved source manifest {path}: {error}") from error
    if not isinstance(payload, Mapping) or payload.get("schemaVersion") != 2:
        raise UploadError(f"unsupported saved source manifest: {path}")
    if payload.get("identity") != "relative-filename-and-byte-size":
        raise UploadError(f"saved source manifest has an unsupported identity: {path}")
    source_value = payload.get("source")
    raw_files = payload.get("files")
    if not isinstance(source_value, str) or not source_value.strip():
        raise UploadError(f"saved source manifest has no source path: {path}")
    if not isinstance(raw_files, list) or not raw_files:
        raise UploadError(f"saved source manifest has no files: {path}")

    records: list[LocalFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, Mapping):
            raise UploadError(f"saved source manifest contains an invalid file: {path}")
        relative_path = raw_file.get("path")
        byte_count = raw_file.get("bytes")
        checksum = raw_file.get("nameSizeChecksum")
        if not isinstance(relative_path, str):
            raise UploadError(f"saved source manifest contains an invalid path: {path}")
        validate_relative_path(relative_path)
        if relative_path in seen_paths:
            raise UploadError(
                f"saved source manifest contains duplicate path {relative_path!r}"
            )
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise UploadError(
                f"saved source manifest contains an invalid size for {relative_path!r}"
            )
        expected_checksum = file_identity_checksum(relative_path, byte_count)
        if checksum != expected_checksum:
            raise UploadError(
                f"saved source manifest checksum mismatch for {relative_path!r}"
            )
        seen_paths.add(relative_path)
        records.append(
            LocalFile(
                path=relative_path,
                bytes=byte_count,
                name_size_checksum=expected_checksum,
            )
        )

    records.sort(key=lambda record: record.path)
    fingerprint_hash = hashlib.sha256()
    for record in records:
        fingerprint_hash.update(record.name_size_checksum.encode("ascii"))
        fingerprint_hash.update(b"\n")
    fingerprint = fingerprint_hash.hexdigest()
    total_bytes = sum(record.bytes for record in records)
    if payload.get("totalBytes") != total_bytes:
        raise UploadError(f"saved source manifest total byte count is invalid: {path}")
    if payload.get("fingerprint") != fingerprint:
        raise UploadError(f"saved source manifest fingerprint is invalid: {path}")

    return SourceSnapshot(
        source=Path(source_value).expanduser().resolve(),
        files=tuple(records),
        total_bytes=total_bytes,
        fingerprint=fingerprint,
    )


def source_file_paths(source: Path) -> list[Path]:
    paths: list[Path] = []
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise UploadError(f"directory symlinks are not supported: {candidate}")
            validate_relative_path(candidate.relative_to(source).as_posix())
        for name in file_names:
            if name in IGNORED_SOURCE_FILE_NAMES:
                continue
            candidate = directory_path / name
            relative_path = candidate.relative_to(source).as_posix()
            validate_relative_path(relative_path)
            if candidate.is_symlink():
                raise UploadError(f"file symlinks are not supported: {candidate}")
            if not candidate.is_file():
                raise UploadError(
                    f"non-regular source entry is not supported: {candidate}"
                )
            paths.append(candidate)
    return sorted(paths, key=lambda path: path.relative_to(source).as_posix())


def inventory_local_file(source: Path, file_path: Path) -> LocalFile:
    metadata = file_path.stat()
    relative_path = file_path.relative_to(source).as_posix()
    return LocalFile(
        path=relative_path,
        bytes=metadata.st_size,
        name_size_checksum=file_identity_checksum(relative_path, metadata.st_size),
    )


def assert_local_file_unchanged(file_path: Path, record: LocalFile) -> None:
    metadata = file_path.stat()
    if metadata.st_size != record.bytes:
        raise VerificationError(f"source file changed before upload: {record.path}")


def compare_remote(
    snapshot: SourceSnapshot,
    remote_files: Mapping[str, RemoteFile],
    prefix: str,
) -> RemoteDifference:
    verified: list[LocalFile] = []
    missing: list[LocalFile] = []
    mismatched: list[LocalFile] = []
    expected_keys: set[str] = set()

    for local_file in snapshot.files:
        key = object_key(prefix, local_file.path)
        expected_keys.add(key)
        remote_file = remote_files.get(key)
        if remote_file is None:
            missing.append(local_file)
            continue
        if remote_file.bytes != local_file.bytes:
            mismatched.append(local_file)
            continue
        verified.append(local_file)

    extras = tuple(sorted(set(remote_files) - expected_keys))
    return RemoteDifference(
        verified=tuple(verified),
        missing=tuple(missing),
        mismatched=tuple(mismatched),
        extras=extras,
    )


def resolve_state_directory(
    requested: Path | None,
    source: Path,
    bucket: str,
    prefix: str,
) -> Path:
    if requested:
        return requested.expanduser().resolve()
    identity = hashlib.sha256(
        f"{source}\0{bucket}\0{prefix}".encode("utf-8")
    ).hexdigest()[:16]
    return source.parent / ".b2-upload-state" / identity


def ensure_state_outside_source(state_directory: Path, source: Path) -> None:
    try:
        state_directory.relative_to(source)
    except ValueError:
        return
    raise UploadError("state directory must be outside the uploaded source tree")


def get_credentials() -> tuple[str, str]:
    application_key_id = os.environ.get("B2_APPLICATION_KEY_ID", "").strip()
    application_key = os.environ.get("B2_APPLICATION_KEY", "")
    if not application_key_id and sys.stdin.isatty():
        application_key_id = input("Backblaze application key ID: ").strip()
    if not application_key and sys.stdin.isatty():
        application_key = getpass.getpass("Backblaze application key: ")
    if not application_key_id or not application_key:
        raise UploadError(
            "set B2_APPLICATION_KEY_ID and B2_APPLICATION_KEY, or run interactively"
        )
    return application_key_id, application_key


def normalize_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    if not prefix:
        return ""
    validate_relative_path(prefix)
    if "\\" in prefix:
        raise ValueError("B2 prefix must use forward slashes")
    if any(part in {"", ".", ".."} for part in prefix.split("/")):
        raise ValueError("B2 prefix contains an unsafe path segment")
    return prefix


def validate_bucket_name(bucket: str) -> None:
    if not bucket:
        raise ValueError("--bucket or B2_BUCKET_NAME is required")
    if "/" in bucket or CONTROL_CHARACTER_PATTERN.search(bucket):
        raise ValueError(f"invalid B2 bucket name: {bucket!r}")


def validate_relative_path(relative_path: str) -> None:
    if not relative_path or relative_path.startswith("/"):
        raise UploadError(f"unsafe source path: {relative_path!r}")
    if CONTROL_CHARACTER_PATTERN.search(relative_path):
        raise UploadError(
            f"source path contains a control character: {relative_path!r}"
        )
    if any(part in {"", ".", ".."} for part in relative_path.split("/")):
        raise UploadError(f"unsafe source path segment: {relative_path!r}")


def object_key(prefix: str, relative_path: str) -> str:
    return f"{prefix}/{relative_path}" if prefix else relative_path


def is_ignored_metadata_key(key: str) -> bool:
    return key.rsplit("/", 1)[-1] in IGNORED_SOURCE_FILE_NAMES


def file_identity_checksum(relative_path: str, byte_count: int) -> str:
    identity = f"{relative_path}\0{byte_count}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def obsolete_version_count(
    version_counts: Mapping[str, int],
    expected_keys: set[str],
) -> int:
    obsolete = 0
    for key, count in version_counts.items():
        if key in expected_keys:
            obsolete += max(0, count - 1)
        else:
            obsolete += count
    return obsolete


def b2_uri(bucket: str, prefix: str, directory: bool) -> str:
    suffix = f"/{prefix}" if prefix else ""
    if directory and prefix:
        suffix = f"{suffix}/"
    return f"b2://{bucket}{suffix}"


def parse_json_output(output: str) -> Any:
    text = output.strip()
    if not text:
        raise B2CommandError("B2 CLI returned empty output where JSON was expected")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        values: list[Any] = []
        cursor = 0
        while cursor < len(text):
            match = re.search(r"[\[{]", text[cursor:])
            if not match:
                break
            start = cursor + match.start()
            try:
                value, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            values.append(value)
            cursor = end
        if len(values) == 1:
            return values[0]
        if values:
            return values
        raise B2CommandError(f"could not parse B2 JSON output: {tail(text)}")


def json_items(payload: Any) -> Iterable[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("files", "items", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return (payload,)
    return ()


def difference_message(difference: RemoteDifference) -> str:
    details = difference.summary()
    examples = [
        record.path
        for record in (
            *difference.missing,
            *difference.mismatched,
        )[:5]
    ]
    examples.extend(difference.extras[: max(0, 5 - len(examples))])
    suffix = f"; examples: {', '.join(examples)}" if examples else ""
    rendered_details = json.dumps(details, sort_keys=True)
    return f"remote verification is incomplete: {rendered_details}{suffix}"


def ignored_metadata_message(keys: Sequence[str], prefix: str) -> str:
    examples = ", ".join(keys[:5])
    suffix = f"; examples: {examples}" if examples else ""
    return f"{prefix}: {len(keys)} key(s){suffix}"


def unfinished_upload_message(
    uploads: Sequence[RemoteUnfinishedUpload],
    prefix: str,
) -> str:
    examples = ", ".join(upload.key for upload in uploads[:5])
    suffix = f"; examples: {examples}" if examples else ""
    return f"{prefix}: {len(uploads)} upload(s){suffix}"


def base_report(
    started_at: str,
    source: Path,
    bucket: str,
    prefix: str,
    snapshot: SourceSnapshot,
) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "uploaderVersion": SCRIPT_VERSION,
        "startedAt": started_at,
        "source": str(source),
        "bucket": bucket or None,
        "prefix": prefix,
        "sourceFingerprint": snapshot.fingerprint,
        "localFiles": len(snapshot.files),
        "localBytes": snapshot.total_bytes,
    }


def write_failure_report(
    report_path: Path,
    started_at: str,
    source: Path,
    bucket: str,
    prefix: str,
    error: str,
) -> None:
    write_json_atomic(
        report_path,
        {
            "schemaVersion": 2,
            "uploaderVersion": SCRIPT_VERSION,
            "status": "failed",
            "startedAt": started_at,
            "completedAt": utc_now(),
            "source": str(source),
            "bucket": bucket or None,
            "prefix": prefix,
            "error": error,
        },
    )


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def print_plan(snapshot: SourceSnapshot, state_directory: Path, quiet: bool) -> None:
    if quiet:
        return
    print(
        json.dumps(
            {
                "status": "planned",
                "source": str(snapshot.source),
                "files": len(snapshot.files),
                "bytes": snapshot.total_bytes,
                "fingerprint": snapshot.fingerprint,
                "stateDirectory": str(state_directory),
            },
            indent=2,
        )
    )


def print_remote_result(
    result: Mapping[str, Any],
    state_directory: Path,
    quiet: bool,
) -> None:
    if quiet:
        return
    print(json.dumps({**result, "stateDirectory": str(state_directory)}, indent=2))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def environment_integer(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return positive_integer(value)
    except (ValueError, argparse.ArgumentTypeError) as error:
        raise ValueError(f"{name} must be a positive integer") from error


def nonnegative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return parsed


def environment_nonnegative_integer(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return nonnegative_integer(value)
    except (ValueError, argparse.ArgumentTypeError) as error:
        raise ValueError(f"{name} must be zero or a positive integer") from error


def retry_delay(attempt: int) -> float:
    if os.environ.get("B2_UPLOAD_TEST_NO_SLEEP") == "1":
        return 0.0
    return min(60.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 1.0)


def is_permanent_b2_error(output: str) -> bool:
    normalized = output.lower()
    permanent_markers = (
        "application key is bad",
        "bad application key",
        "missing capability",
        "no such bucket",
        "bucket not found",
        "invalid bucket",
    )
    return any(marker in normalized for marker in permanent_markers)


def redact(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        return None


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_age(lock_directory: Path) -> float:
    try:
        return max(0.0, time.time() - lock_directory.stat().st_mtime)
    except FileNotFoundError:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
