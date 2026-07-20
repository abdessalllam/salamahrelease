from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "upload_public_to_b2.py"
SPEC = importlib.util.spec_from_file_location("upload_public_to_b2", SCRIPT_PATH)
assert SPEC and SPEC.loader
uploader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = uploader
SPEC.loader.exec_module(uploader)


class MemoryLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)


class FakeB2Client:
    def __init__(self, bucket_type: str = "allPublic") -> None:
        self.bucket_type = bucket_type
        self.remote: dict[str, uploader.RemoteFile] = {}
        self.sync_calls = 0
        self.upload_calls: list[str] = []
        self.deleted_metadata_keys: list[str] = []
        self.unfinished_uploads: list[uploader.RemoteUnfinishedUpload] = []
        self.cancelled_upload_ids: list[str] = []
        self.version_counts: dict[str, int] | None = None

    def bucket_get(self, bucket: str) -> dict[str, str]:
        return {"bucketName": bucket, "bucketType": self.bucket_type}

    def list_files(
        self,
        bucket: str,
        prefix: str,
    ) -> dict[str, uploader.RemoteFile]:
        return {
            key: remote_file
            for key, remote_file in self.remote.items()
            if not prefix or key.startswith(f"{prefix}/")
        }

    def list_ignored_metadata_keys(
        self,
        bucket: str,
        prefix: str,
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key in self.remote
                if uploader.is_ignored_metadata_key(key)
                and (not prefix or key.startswith(f"{prefix}/"))
            )
        )

    def delete_all_versions(self, bucket: str, key: str) -> None:
        self.deleted_metadata_keys.append(key)
        self.remote.pop(key, None)

    def list_unfinished_uploads(
        self,
        bucket: str,
        prefix: str,
    ) -> tuple[uploader.RemoteUnfinishedUpload, ...]:
        return tuple(
            upload
            for upload in self.unfinished_uploads
            if not prefix or upload.key.startswith(f"{prefix}/")
        )

    def cancel_unfinished_upload(self, file_id: str) -> None:
        self.cancelled_upload_ids.append(file_id)
        self.unfinished_uploads = [
            upload
            for upload in self.unfinished_uploads
            if upload.file_id != file_id
        ]

    def list_version_counts(
        self,
        bucket: str,
        prefix: str,
    ) -> dict[str, int]:
        if self.version_counts is not None:
            return {
                key: count
                for key, count in self.version_counts.items()
                if not prefix or key.startswith(f"{prefix}/")
            }
        return {
            key: 1
            for key in self.remote
            if not prefix or key.startswith(f"{prefix}/")
        }

    def sync(
        self,
        source: Path,
        bucket: str,
        prefix: str,
        upload_threads: int,
        dry_run: bool,
    ) -> None:
        self.sync_calls += 1
        if dry_run:
            return
        snapshot = uploader.snapshot_source(source, workers=1)
        expected_keys = {
            uploader.object_key(prefix, record.path) for record in snapshot.files
        }
        for key in tuple(self.remote):
            in_destination = not prefix or key.startswith(f"{prefix}/")
            if in_destination and key not in expected_keys:
                del self.remote[key]
        for record in snapshot.files:
            key = uploader.object_key(prefix, record.path)
            current = self.remote.get(key)
            if current is not None and current.bytes == record.bytes:
                continue
            self.remote[key] = remote_from_record(key, record)

    def upload(
        self,
        source_file: Path,
        bucket: str,
        key: str,
        record: uploader.LocalFile,
        upload_threads: int,
    ) -> None:
        if source_file.stat().st_size != record.bytes:
            raise uploader.VerificationError("test source changed before upload")
        self.upload_calls.append(key)
        self.remote[key] = remote_from_record(key, record)


class InterruptedSyncClient(FakeB2Client):
    def sync(
        self,
        source: Path,
        bucket: str,
        prefix: str,
        upload_threads: int,
        dry_run: bool,
    ) -> None:
        self.sync_calls += 1
        snapshot = uploader.snapshot_source(source, workers=1)
        first = snapshot.files[0]
        key = uploader.object_key(prefix, first.path)
        self.remote[key] = remote_from_record(key, first)
        raise uploader.B2CommandError("simulated interrupted sync")


def remote_from_record(
    key: str,
    record: uploader.LocalFile,
) -> uploader.RemoteFile:
    return uploader.RemoteFile(
        key=key,
        bytes=record.bytes,
    )


class UploadPublicToB2Tests(unittest.TestCase):
    def test_snapshot_includes_hidden_files_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / ".hidden").write_text("hidden", encoding="utf-8")
            (source / ".DS_Store").write_bytes(b"metadata")
            (source / "nested").mkdir()
            (source / "nested" / "asset.bin").write_bytes(b"asset")

            first = uploader.snapshot_source(source, workers=2)
            second = uploader.snapshot_source(source, workers=1)

            self.assertEqual(
                [record.path for record in first.files],
                [".hidden", "nested/asset.bin"],
            )
            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(first.total_bytes, 11)
            manifest = first.to_json()
            self.assertEqual(manifest["schemaVersion"], 2)
            self.assertEqual(
                manifest["identity"],
                "relative-filename-and-byte-size",
            )
            self.assertIn("nameSizeChecksum", manifest["files"][0])
            self.assertNotIn("sha1", manifest["files"][0])

    def test_upload_replaces_different_size_then_resumes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "different-size.txt").write_text("right", encoding="utf-8")
            (source / "missing.txt").write_text("missing", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            different_size = next(
                record
                for record in snapshot.files
                if record.path == "different-size.txt"
            )

            client = FakeB2Client()
            client.remote["release/different-size.txt"] = uploader.RemoteFile(
                key="release/different-size.txt",
                bytes=different_size.bytes + 1,
            )
            logger = MemoryLogger()

            first_result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=logger,
            )
            self.assertEqual(first_result["after"]["verified"], 2)
            self.assertEqual(client.sync_calls, 1)
            self.assertEqual(client.upload_calls, [])

            second_result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=logger,
            )
            self.assertEqual(second_result["before"]["verified"], 2)
            self.assertFalse(second_result["syncAttempted"])
            self.assertEqual(client.sync_calls, 1)
            self.assertEqual(client.upload_calls, [])

    def test_same_name_and_size_is_skipped_without_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("right", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            record = snapshot.files[0]
            client = FakeB2Client()
            client.remote["release/asset.txt"] = uploader.RemoteFile(
                key="release/asset.txt",
                bytes=record.bytes,
            )

            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(result["before"]["verified"], 1)
            self.assertFalse(result["syncAttempted"])
            self.assertEqual(client.sync_calls, 0)
            self.assertEqual(client.upload_calls, [])

    def test_name_size_checksum_ignores_same_size_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            asset = source / "asset.txt"
            asset.write_bytes(b"first")
            first = uploader.snapshot_source(source, workers=1)
            asset.write_bytes(b"other")
            second = uploader.snapshot_source(source, workers=1)

            self.assertEqual(first.fingerprint, second.fingerprint)
            self.assertEqual(
                first.files[0].name_size_checksum,
                second.files[0].name_size_checksum,
            )

    def test_name_size_checksum_changes_with_name_or_size(self) -> None:
        checksum = uploader.file_identity_checksum("nested/asset.bin", 42)

        self.assertNotEqual(
            checksum,
            uploader.file_identity_checksum("nested/renamed.bin", 42),
        )
        self.assertNotEqual(
            checksum,
            uploader.file_identity_checksum("nested/asset.bin", 43),
        )

    def test_single_file_upload_does_not_require_content_sha1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            source_file = temporary_directory / "asset.bin"
            source_file.write_bytes(b"asset")
            record = uploader.inventory_local_file(temporary_directory, source_file)

            with mock.patch.object(
                uploader.shutil,
                "which",
                return_value="/usr/bin/true",
            ):
                client = uploader.B2Cli(
                    executable="b2",
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=1,
                    logger=MemoryLogger(),
                )
            with mock.patch.object(client, "_run", return_value="") as run:
                client.upload(
                    source_file,
                    "public-bucket",
                    "release/asset.bin",
                    record,
                    upload_threads=2,
                )

            arguments = run.call_args.args[0]
            self.assertNotIn("--sha1", arguments)
            self.assertIn(
                f"name_size_checksum={record.name_size_checksum}",
                arguments,
            )

    def test_parallel_sync_is_single_pass_and_excludes_finder_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            source = temporary_directory / "public"
            source.mkdir()

            with mock.patch.object(
                uploader.shutil,
                "which",
                return_value="/usr/bin/true",
            ):
                client = uploader.B2Cli(
                    executable="b2",
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=6,
                    logger=MemoryLogger(),
                )
            with mock.patch.object(client, "_run", return_value="") as run:
                client.sync(
                    source,
                    "public-bucket",
                    "",
                    upload_threads=2,
                    dry_run=False,
                )

            arguments = run.call_args.args[0]
            self.assertEqual(
                run.call_args.kwargs,
                {"retry": False, "live": False, "managed": True},
            )
            self.assertIn("--exclude-regex", arguments)
            self.assertIn("--no-progress", arguments)
            self.assertIn("--delete", arguments)
            self.assertIn(uploader.IGNORED_SYNC_FILE_PATTERN, arguments)
            self.assertEqual(arguments[-2:], (str(source), "b2://public-bucket"))

    def test_verbose_sync_enables_live_progress_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            source = temporary_directory / "public"
            source.mkdir()

            with mock.patch.object(
                uploader.shutil,
                "which",
                return_value="/usr/bin/true",
            ):
                client = uploader.B2Cli(
                    executable="b2",
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=2,
                    logger=MemoryLogger(),
                    verbose=True,
                )
            with mock.patch.object(client, "_run", return_value="") as run:
                client.sync(
                    source,
                    "public-bucket",
                    "",
                    upload_threads=2,
                    dry_run=False,
                )

            arguments = run.call_args.args[0]
            self.assertNotIn("--no-progress", arguments)
            self.assertEqual(
                run.call_args.kwargs,
                {"retry": False, "live": True, "managed": True},
            )

    def test_live_progress_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            fake_b2_path = temporary_directory / "fake-b2"
            fake_b2_path.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys

                    key_id = os.environ["B2_APPLICATION_KEY_ID"]
                    key = os.environ["B2_APPLICATION_KEY"]
                    print(f"uploading {key_id} {key}", end="\\r", flush=True)
                    print("complete", flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake_b2_path.chmod(0o755)
            client = uploader.B2Cli(
                executable=str(fake_b2_path),
                application_key_id="private-key-id",
                application_key="private-application-key",
                account_info_path=temporary_directory / "account.sqlite",
                retries=1,
                logger=MemoryLogger(),
                verbose=True,
            )

            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                client._run(("progress",), live=True)

            rendered = output.getvalue()
            self.assertIn("uploading [REDACTED] [REDACTED]", rendered)
            self.assertIn("complete", rendered)
            self.assertNotIn("private-key-id", rendered)
            self.assertNotIn("private-application-key", rendered)

    def test_managed_command_stops_after_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            child_pid_path = temporary_directory / "child.pid"
            fake_b2_path = temporary_directory / "fake-b2"
            fake_b2_path.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    from pathlib import Path
                    import subprocess
                    import sys

                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"]
                    )
                    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid))
                    print("complete", flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake_b2_path.chmod(0o755)
            with mock.patch.dict(
                os.environ,
                {"FAKE_CHILD_PID": str(child_pid_path)},
            ):
                client = uploader.B2Cli(
                    executable=str(fake_b2_path),
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=1,
                    logger=MemoryLogger(),
                    verbose=True,
                    stall_timeout_seconds=5,
                )
                started_at = time.monotonic()
                output = client._run(("work",), live=True, managed=True)
                elapsed = time.monotonic() - started_at

            self.assertIn("complete", output)
            self.assertLess(elapsed, 5)
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            while (
                uploader.process_is_running(child_pid)
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertFalse(uploader.process_is_running(child_pid))

    def test_verbose_stall_timeout_terminates_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            fake_b2_path = temporary_directory / "fake-b2"
            fake_b2_path.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import time

                    time.sleep(60)
                    """
                ),
                encoding="utf-8",
            )
            fake_b2_path.chmod(0o755)
            client = uploader.B2Cli(
                executable=str(fake_b2_path),
                application_key_id="key-id",
                application_key="application-key",
                account_info_path=temporary_directory / "account.sqlite",
                retries=1,
                logger=MemoryLogger(),
                verbose=True,
                stall_timeout_seconds=1,
            )

            started_at = time.monotonic()
            with self.assertRaisesRegex(
                uploader.B2CommandError,
                "produced no progress",
            ):
                client._run(("work",), live=True, managed=True)
            self.assertLess(time.monotonic() - started_at, 5)

    def test_remote_finder_metadata_is_deleted_before_clean_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            record = snapshot.files[0]
            client = FakeB2Client()
            client.remote["release/asset.txt"] = remote_from_record(
                "release/asset.txt",
                record,
            )
            client.remote["release/.DS_Store"] = uploader.RemoteFile(
                key="release/.DS_Store",
                bytes=4096,
            )
            client.remote["release/nested/.DS_Store"] = uploader.RemoteFile(
                key="release/nested/.DS_Store",
                bytes=8192,
            )
            client.remote["other/.DS_Store"] = uploader.RemoteFile(
                key="other/.DS_Store",
                bytes=1024,
            )

            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(
                client.deleted_metadata_keys,
                ["release/.DS_Store", "release/nested/.DS_Store"],
            )
            self.assertNotIn("release/.DS_Store", client.remote)
            self.assertNotIn("release/nested/.DS_Store", client.remote)
            self.assertIn("other/.DS_Store", client.remote)
            self.assertEqual(result["remoteIgnoredMetadataFound"], 2)
            self.assertEqual(result["remoteIgnoredMetadataDeleted"], 2)
            self.assertFalse(result["syncAttempted"])

    def test_verify_only_refuses_remote_finder_metadata_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            client = FakeB2Client()
            client.remote[".DS_Store"] = uploader.RemoteFile(
                key=".DS_Store",
                bytes=4096,
            )

            with self.assertRaisesRegex(
                uploader.VerificationError,
                "forbidden metadata",
            ):
                uploader.perform_upload(
                    source=source,
                    snapshot=snapshot,
                    client=client,
                    bucket="public-bucket",
                    prefix="",
                    upload_threads=2,
                    dry_run=False,
                    verify_only=True,
                    allow_private_bucket=False,
                    logger=MemoryLogger(),
                )

            self.assertEqual(client.deleted_metadata_keys, [])
            self.assertIn(".DS_Store", client.remote)

    def test_dry_run_reports_remote_finder_metadata_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            client = FakeB2Client()
            client.remote[".DS_Store"] = uploader.RemoteFile(
                key=".DS_Store",
                bytes=4096,
            )

            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="",
                upload_threads=2,
                dry_run=True,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(result["remoteIgnoredMetadataFound"], 1)
            self.assertEqual(result["remoteIgnoredMetadataDeleted"], 0)
            self.assertEqual(client.deleted_metadata_keys, [])
            self.assertIn(".DS_Store", client.remote)

    def test_b2_cli_lists_and_deletes_all_remote_metadata_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            with mock.patch.object(
                uploader.shutil,
                "which",
                return_value="/usr/bin/true",
            ):
                client = uploader.B2Cli(
                    executable="b2",
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=2,
                    logger=MemoryLogger(),
                )

            versions = [
                {"fileName": "release/.DS_Store", "action": "upload"},
                {"fileName": "release/.DS_Store", "action": "hide"},
                {"fileName": "release/nested/.DS_Store", "action": "upload"},
                {"fileName": "release/not.DS_Store", "action": "upload"},
            ]
            with mock.patch.object(
                client,
                "_run_json",
                return_value=versions,
            ) as run_json:
                keys = client.list_ignored_metadata_keys(
                    "public-bucket",
                    "release",
                )

            self.assertEqual(
                keys,
                ("release/.DS_Store", "release/nested/.DS_Store"),
            )
            self.assertIn("--versions", run_json.call_args.args[0])

            with mock.patch.object(client, "_run", return_value="") as run:
                client.delete_all_versions(
                    "public-bucket",
                    "release/.DS_Store",
                )

            self.assertEqual(
                run.call_args.args[0],
                (
                    "rm",
                    "--no-progress",
                    "--fail-fast",
                    "--versions",
                    "b2://public-bucket/release/.DS_Store",
                ),
            )

    def test_unfinished_uploads_in_destination_prefix_are_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            record = snapshot.files[0]
            client = FakeB2Client()
            client.remote["release/asset.txt"] = remote_from_record(
                "release/asset.txt",
                record,
            )
            client.unfinished_uploads = [
                uploader.RemoteUnfinishedUpload(
                    key="release/asset.txt",
                    file_id="source-upload-id",
                ),
                uploader.RemoteUnfinishedUpload(
                    key="release/unrelated.bin",
                    file_id="unrelated-upload-id",
                ),
            ]

            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(
                client.cancelled_upload_ids,
                ["source-upload-id", "unrelated-upload-id"],
            )
            self.assertEqual(client.unfinished_uploads, [])
            self.assertEqual(result["remoteUnfinishedUploadsFound"], 2)
            self.assertEqual(result["remoteUnfinishedUploadsCancelled"], 2)
            self.assertFalse(result["syncAttempted"])

    def test_b2_cli_lists_and_cancels_unfinished_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            with mock.patch.object(
                uploader.shutil,
                "which",
                return_value="/usr/bin/true",
            ):
                client = uploader.B2Cli(
                    executable="b2",
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=2,
                    logger=MemoryLogger(),
                )

            versions = [
                {
                    "fileName": "release/asset.zip",
                    "fileId": "unfinished-id",
                    "action": "start",
                },
                {
                    "fileName": "release/asset.zip",
                    "fileId": "finished-id",
                    "action": "upload",
                },
            ]
            with mock.patch.object(
                client,
                "_run_json",
                return_value=versions,
            ):
                uploads = client.list_unfinished_uploads(
                    "public-bucket",
                    "release",
                )

            self.assertEqual(
                uploads,
                (
                    uploader.RemoteUnfinishedUpload(
                        key="release/asset.zip",
                        file_id="unfinished-id",
                    ),
                ),
            )

            with mock.patch.object(client, "_run", return_value="") as run:
                client.cancel_unfinished_upload("unfinished-id")

            self.assertEqual(
                run.call_args.args[0],
                (
                    "file",
                    "large",
                    "unfinished",
                    "cancel",
                    "b2id://unfinished-id",
                ),
            )
            self.assertEqual(
                run.call_args.kwargs,
                {"live": False, "managed": True},
            )

    def test_obsolete_versions_force_exact_mirror_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            record = snapshot.files[0]
            client = FakeB2Client()
            client.remote["release/asset.txt"] = remote_from_record(
                "release/asset.txt",
                record,
            )
            client.version_counts = {"release/asset.txt": 2}

            original_sync = client.sync

            def mirror_sync(*args: object, **kwargs: object) -> None:
                original_sync(*args, **kwargs)
                client.version_counts = {"release/asset.txt": 1}

            client.sync = mirror_sync  # type: ignore[method-assign]
            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(client.sync_calls, 1)
            self.assertEqual(result["remoteObsoleteVersionsFound"], 1)
            self.assertEqual(result["remoteObsoleteVersionsRemaining"], 0)

    def test_remote_extra_is_deleted_by_exact_mirror_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            record = snapshot.files[0]
            client = FakeB2Client()
            client.remote["release/asset.txt"] = remote_from_record(
                "release/asset.txt",
                record,
            )
            client.remote["release/stale.txt"] = uploader.RemoteFile(
                key="release/stale.txt",
                bytes=5,
            )

            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(client.sync_calls, 1)
            self.assertEqual(result["before"]["remoteExtras"], 1)
            self.assertEqual(result["after"]["remoteExtras"], 0)
            self.assertNotIn("release/stale.txt", client.remote)

    def test_post_sync_unfinished_uploads_are_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            client = FakeB2Client()
            original_sync = client.sync

            def sync_with_residual_upload(*args: object, **kwargs: object) -> None:
                original_sync(*args, **kwargs)
                client.unfinished_uploads = [
                    uploader.RemoteUnfinishedUpload(
                        key="release/asset.txt",
                        file_id="post-sync-upload-id",
                    )
                ]

            client.sync = sync_with_residual_upload  # type: ignore[method-assign]
            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertEqual(client.cancelled_upload_ids, ["post-sync-upload-id"])
            self.assertEqual(result["remoteUnfinishedUploadsFound"], 1)
            self.assertEqual(result["remoteUnfinishedUploadsCancelled"], 1)
            self.assertEqual(result["remoteUnfinishedUploadsRemaining"], 0)

    def test_b2_cli_counts_completed_versions_but_not_unfinished_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            with mock.patch.object(
                uploader.shutil,
                "which",
                return_value="/usr/bin/true",
            ):
                client = uploader.B2Cli(
                    executable="b2",
                    application_key_id="key-id",
                    application_key="application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=2,
                    logger=MemoryLogger(),
                )

            versions = [
                {"fileName": "release/asset.zip", "action": "upload"},
                {"fileName": "release/asset.zip", "action": "upload"},
                {"fileName": "release/asset.zip", "action": "start"},
                {"fileName": "release/hidden.txt", "action": "hide"},
                {"fileName": "release/copied.txt", "action": "copy"},
            ]
            with mock.patch.object(
                client,
                "_run_json",
                return_value=versions,
            ) as run_json:
                counts = client.list_version_counts("public-bucket", "release")

            self.assertEqual(
                counts,
                {
                    "release/asset.zip": 2,
                    "release/copied.txt": 1,
                    "release/hidden.txt": 1,
                },
            )
            arguments = run_json.call_args.args[0]
            self.assertIn("--versions", arguments)
            self.assertEqual(arguments[-1], "b2://public-bucket/release/")

    def test_interrupted_sync_repairs_only_unresolved_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "first.txt").write_text("first", encoding="utf-8")
            (source / "second.txt").write_text("second", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            client = InterruptedSyncClient()

            result = uploader.perform_upload(
                source=source,
                snapshot=snapshot,
                client=client,
                bucket="public-bucket",
                prefix="release",
                upload_threads=2,
                dry_run=False,
                verify_only=False,
                allow_private_bucket=False,
                logger=MemoryLogger(),
            )

            self.assertTrue(result["syncRecoveredFromError"])
            self.assertEqual(result["after"]["verified"], 2)
            self.assertEqual(client.sync_calls, 1)
            self.assertEqual(client.upload_calls, ["release/second.txt"])

    def test_verify_only_fails_for_missing_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "missing.txt").write_text("missing", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)

            with self.assertRaises(uploader.VerificationError):
                uploader.perform_upload(
                    source=source,
                    snapshot=snapshot,
                    client=FakeB2Client(),
                    bucket="public-bucket",
                    prefix="",
                    upload_threads=1,
                    dry_run=False,
                    verify_only=True,
                    allow_private_bucket=False,
                    logger=MemoryLogger(),
                )

    def test_private_bucket_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "asset.txt").write_text("asset", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)

            with self.assertRaisesRegex(uploader.UploadError, "not allPublic"):
                uploader.perform_upload(
                    source=source,
                    snapshot=snapshot,
                    client=FakeB2Client(bucket_type="allPrivate"),
                    bucket="private-bucket",
                    prefix="",
                    upload_threads=1,
                    dry_run=False,
                    verify_only=False,
                    allow_private_bucket=False,
                    logger=MemoryLogger(),
                )

    def test_symlinks_are_rejected_instead_of_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            target = source / "target.txt"
            target.write_text("target", encoding="utf-8")
            (source / "link.txt").symlink_to(target)

            with self.assertRaisesRegex(uploader.UploadError, "symlinks"):
                uploader.snapshot_source(source, workers=1)

    def test_prefix_rejects_parent_segments_and_backslashes(self) -> None:
        with self.assertRaises(uploader.UploadError):
            uploader.normalize_prefix("../release")
        with self.assertRaises(ValueError):
            uploader.normalize_prefix(r"release\windows")

    def test_json_parser_tolerates_cli_leading_text(self) -> None:
        payload = uploader.parse_json_output(
            'Using endpoint\n{"bucketType":"allPublic"}'
        )
        self.assertEqual(payload["bucketType"], "allPublic")

    def test_recent_lock_without_pid_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_directory = Path(directory)
            lock_directory = state_directory / "lock"
            lock_directory.mkdir()

            with self.assertRaisesRegex(uploader.UploadError, "initializing"):
                with uploader.StateLock(state_directory):
                    self.fail("a recent incomplete lock must not be acquired")

            self.assertTrue(lock_directory.is_dir())

    def test_cli_retries_transient_failure_and_redacts_permanent_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_directory = Path(directory)
            counter_path = temporary_directory / "counter"
            fake_b2_path = temporary_directory / "fake-b2"
            fake_b2_path.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    from pathlib import Path
                    import sys

                    counter_path = Path(os.environ["FAKE_B2_COUNTER"])
                    if counter_path.exists():
                        count = int(counter_path.read_text() or "0")
                    else:
                        count = 0
                    counter_path.write_text(str(count + 1))
                    key_id = os.environ["B2_APPLICATION_KEY_ID"]
                    key = os.environ["B2_APPLICATION_KEY"]

                    if sys.argv[1] == "transient" and count == 0:
                        print(f"temporary failure {key_id} {key}", file=sys.stderr)
                        raise SystemExit(1)
                    if sys.argv[1] == "permanent":
                        print(f"bad application key {key_id} {key}", file=sys.stderr)
                        raise SystemExit(2)
                    print("ok")
                    """
                ),
                encoding="utf-8",
            )
            fake_b2_path.chmod(0o755)
            logger = MemoryLogger()

            with mock.patch.dict(
                os.environ,
                {
                    "FAKE_B2_COUNTER": str(counter_path),
                    "B2_UPLOAD_TEST_NO_SLEEP": "1",
                },
            ):
                client = uploader.B2Cli(
                    executable=str(fake_b2_path),
                    application_key_id="private-key-id",
                    application_key="private-application-key",
                    account_info_path=temporary_directory / "account.sqlite",
                    retries=3,
                    logger=logger,
                )
                self.assertEqual(client._run(("transient",)).strip(), "ok")
                self.assertEqual(counter_path.read_text(encoding="utf-8"), "2")
                self.assertTrue(
                    any("retrying" in message for message in logger.messages)
                )

                with self.assertRaises(uploader.B2CommandError) as context:
                    client._run(("permanent",))

            error_message = str(context.exception)
            self.assertNotIn("private-key-id", error_message)
            self.assertNotIn("private-application-key", error_message)
            self.assertIn("[REDACTED]", error_message)


if __name__ == "__main__":
    unittest.main()
