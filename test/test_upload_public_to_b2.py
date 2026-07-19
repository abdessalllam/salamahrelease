from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import textwrap
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

    def bucket_get(self, bucket: str) -> dict[str, str]:
        return {"bucketName": bucket, "bucketType": self.bucket_type}

    def list_files(
        self,
        bucket: str,
        prefix: str,
    ) -> dict[str, uploader.RemoteFile]:
        return dict(self.remote)

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
        actual_sha1 = hashlib.sha1(source_file.read_bytes()).hexdigest()
        if actual_sha1 != record.sha1:
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
        content_sha1=record.sha1,
        file_info={},
    )


class UploadPublicToB2Tests(unittest.TestCase):
    def test_snapshot_includes_hidden_files_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / ".hidden").write_text("hidden", encoding="utf-8")
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

    def test_upload_repairs_same_size_mismatch_then_resumes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "same-size.txt").write_text("right", encoding="utf-8")
            (source / "missing.txt").write_text("missing", encoding="utf-8")
            snapshot = uploader.snapshot_source(source, workers=1)
            same_size = next(
                record for record in snapshot.files if record.path == "same-size.txt"
            )

            client = FakeB2Client()
            wrong_sha1 = hashlib.sha1(b"wrong").hexdigest()
            client.remote["release/same-size.txt"] = uploader.RemoteFile(
                key="release/same-size.txt",
                bytes=same_size.bytes,
                content_sha1=wrong_sha1,
                file_info={},
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
            self.assertEqual(client.upload_calls, ["release/same-size.txt"])

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
            self.assertEqual(client.upload_calls, ["release/same-size.txt"])

    def test_large_file_sha1_metadata_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "large.zip").write_bytes(b"large-content")
            snapshot = uploader.snapshot_source(source, workers=1)
            record = snapshot.files[0]
            remote = {
                "large.zip": uploader.RemoteFile(
                    key="large.zip",
                    bytes=record.bytes,
                    content_sha1="none",
                    file_info={"large_file_sha1": record.sha1},
                )
            }

            difference = uploader.compare_remote(snapshot, remote, prefix="")

            self.assertTrue(difference.clean)
            self.assertEqual(len(difference.verified), 1)

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
                    count = int(counter_path.read_text() or "0") if counter_path.exists() else 0
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
