from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "mirror_gapless_quran_audio.py"
)
SPEC = importlib.util.spec_from_file_location("mirror_gapless_quran_audio", SCRIPT_PATH)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mirror
SPEC.loader.exec_module(mirror)


class FakeResponse:
    def __init__(self, url: str, payload: bytes) -> None:
        self.url = url
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}
        self.offset = 0

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload) - self.offset
        result = self.payload[self.offset : self.offset + size]
        self.offset += len(result)
        return result


class TruncatedResponse(FakeResponse):
    def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload) // 2:
            return b""
        return super().read(min(size, len(self.payload) // 2))


class GaplessQuranAudioMirrorTests(unittest.TestCase):
    def test_batch_credentials_are_prompted_once_and_exported_for_children(self) -> None:
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        with (
            mock.patch.dict(
                mirror.os.environ,
                {"B2_APPLICATION_KEY_ID": "", "B2_APPLICATION_KEY": ""},
            ),
            mock.patch.object(mirror.sys, "stdin", fake_stdin),
            mock.patch("builtins.input", return_value="key-id") as prompt_id,
            mock.patch.object(
                mirror.getpass,
                "getpass",
                return_value="application-key",
            ) as prompt_key,
        ):
            mirror.ensure_b2_credentials()
            mirror.ensure_b2_credentials()

            self.assertEqual("key-id", mirror.os.environ["B2_APPLICATION_KEY_ID"])
            self.assertEqual("application-key", mirror.os.environ["B2_APPLICATION_KEY"])

        prompt_id.assert_called_once()
        prompt_key.assert_called_once()

    def test_default_request_keeps_certificate_and_hostname_verification_enabled(
        self,
    ) -> None:
        request = mirror.urllib.request.Request("https://audio.example/001.mp3")
        response = object()

        with mock.patch.object(
            mirror.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            self.assertIs(response, mirror.default_request(request, 30))

        context = urlopen.call_args.kwargs["context"]
        self.assertEqual(mirror.ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)

    def test_repository_catalog_has_40_complete_timing_backed_packs(self) -> None:
        packs = mirror.load_catalog(
            Path(__file__).resolve().parents[1]
            / "config"
            / "quran-audio-gapless.json"
        )

        self.assertEqual(40, len(packs))
        self.assertTrue(all(pack.file_count == 114 for pack in packs))
        self.assertTrue(all(pack.timing_database_name for pack in packs))
        self.assertNotIn("67", {pack.pack_id for pack in packs})
        self.assertNotIn("68", {pack.pack_id for pack in packs})
        self.assertNotIn("69", {pack.pack_id for pack in packs})

    def test_catalog_rejects_duplicate_or_insecure_sources(self) -> None:
        payload = {
            "schemaVersion": 1,
            "fileCount": 1,
            "packs": [
                {
                    "packId": "1",
                    "pathSlug": "same",
                    "timingDatabaseName": "one",
                    "sourceBaseUrl": "https://example.com/one/",
                },
                {
                    "packId": "2",
                    "pathSlug": "same",
                    "timingDatabaseName": "two",
                    "sourceBaseUrl": "http://example.com/two/",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(mirror.MirrorError):
                mirror.load_catalog(path)

    def test_audit_stage_and_archives_verify_exact_files(self) -> None:
        pack = mirror.GaplessPack(
            pack_id="test",
            path_slug="test_pack",
            timing_database_name="test_timing",
            source_base_url="https://audio.example/",
            file_count=3,
        )
        payloads = {
            "001.mp3": b"ID3-one",
            "002.mp3": b"ID3-two-two",
            "003.mp3": b"ID3-three-three-three",
        }

        def request(request: object, timeout: int) -> FakeResponse:
            del timeout
            name = str(request.full_url).rsplit("/", 1)[-1]
            return FakeResponse(str(request.full_url), payloads[name])

        remote = mirror.audit_pack(
            pack,
            workers=2,
            retries=1,
            request_function=request,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = mirror.stage_pack(
                remote,
                root / "direct",
                workers=2,
                retries=1,
                request_function=request,
            )
            archives = mirror.build_archives(
                pack,
                local,
                root / "archives",
                max_archive_bytes=mirror.ARCHIVE_HEADROOM + 30,
            )

            self.assertEqual(3, len(local))
            self.assertEqual(2, len(archives))
            archived_names: list[str] = []
            for archive in archives:
                self.assertLess(
                    archive["bytes"],
                    mirror.ARCHIVE_HEADROOM + 30,
                )
                with zipfile.ZipFile(archive["path"]) as zip_file:
                    self.assertIsNone(zip_file.testzip())
                    archived_names.extend(zip_file.namelist())
            self.assertEqual(sorted(payloads), sorted(archived_names))

    def test_download_retries_an_incomplete_transfer(self) -> None:
        payload = b"complete-audio"
        remote = mirror.RemoteAudioFile(
            name="001.mp3",
            source_url="https://audio.example/001.mp3",
            final_url="https://audio.example/001.mp3",
            bytes=len(payload),
        )
        calls = 0

        def request(request: object, timeout: int) -> FakeResponse:
            del request, timeout
            nonlocal calls
            calls += 1
            if calls == 1:
                return TruncatedResponse(remote.source_url, payload)
            return FakeResponse(remote.source_url, payload)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / remote.name
            with mock.patch.object(mirror.time, "sleep"):
                mirror.download_file(remote, target, retries=2, request_function=request)

            self.assertEqual(payload, target.read_bytes())
            self.assertEqual(2, calls)
            self.assertFalse(target.with_suffix(".mp3.part").exists())

    def test_stage_redownloads_same_size_file_with_wrong_checkpoint_digest(self) -> None:
        payload = b"correct-audio"
        remote = mirror.RemoteAudioFile(
            name="001.mp3",
            source_url="https://audio.example/001.mp3",
            final_url="https://audio.example/001.mp3",
            bytes=len(payload),
        )
        calls = 0

        def request(request: object, timeout: int) -> FakeResponse:
            del request, timeout
            nonlocal calls
            calls += 1
            return FakeResponse(remote.source_url, payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = mirror.stage_pack(
                [remote],
                root / "direct",
                workers=1,
                retries=1,
                request_function=request,
            )
            staged[0].path.write_bytes(b"damaged-audio")
            restaged = mirror.stage_pack(
                [remote],
                root / "direct",
                workers=1,
                retries=1,
                request_function=request,
            )

            self.assertEqual(payload, restaged[0].path.read_bytes())
            self.assertEqual(2, calls)

    def test_b2_publish_delegates_to_verified_uploader_without_secrets(self) -> None:
        pack = mirror.GaplessPack(
            pack_id="1",
            path_slug="safe_slug",
            timing_database_name="timing",
            source_base_url="https://example.com/",
            file_count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "direct"
            source.mkdir()
            (source / "001.mp3").write_bytes(b"audio")
            with mock.patch.object(mirror.subprocess, "run") as run:
                mirror.publish_b2(
                    direct_directory=source,
                    pack=pack,
                    bucket="public-bucket",
                    prefix="release-assets/quran/audio/gapless",
                    state_directory=root / "state",
                )

        command = run.call_args.args[0]
        self.assertIn("upload_public_to_b2.py", " ".join(command))
        self.assertIn("public-bucket", command)
        self.assertIn(
            "release-assets/quran/audio/gapless/safe_slug",
            command,
        )
        self.assertFalse(any("APPLICATION_KEY" in part for part in command))

    def test_b2_state_verification_skips_only_an_exact_saved_file_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "source-manifest.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {"path": "001.mp3"},
                            {"path": "manifest.json"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            completed = mirror.subprocess.CompletedProcess([], 0)
            with mock.patch.object(
                mirror.subprocess,
                "run",
                return_value=completed,
            ) as run:
                exact = mirror.b2_state_is_complete(
                    direct_directory=root / "direct",
                    bucket="public-bucket",
                    prefix="release/audio/test",
                    state_directory=state,
                    expected_names=("001.mp3",),
                )
                wrong_pack = mirror.b2_state_is_complete(
                    direct_directory=root / "direct",
                    bucket="public-bucket",
                    prefix="release/audio/test",
                    state_directory=state,
                    expected_names=("002.mp3",),
                )

        self.assertTrue(exact)
        self.assertFalse(wrong_pack)
        run.assert_called_once()
        self.assertIn("--verify-state-only", run.call_args.args[0])

    def test_b2_only_manifest_does_not_require_github_archives(self) -> None:
        pack = mirror.GaplessPack(
            pack_id="1",
            path_slug="safe_slug",
            timing_database_name="timing",
            source_base_url="https://example.com/",
            file_count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            direct = Path(directory) / "direct"
            direct.mkdir()
            audio_path = direct / "001.mp3"
            audio_path.write_bytes(b"audio")
            local_file = mirror.LocalAudioFile(
                name=audio_path.name,
                path=audio_path,
                source_url="https://example.com/001.mp3",
                final_url="https://example.com/001.mp3",
                bytes=audio_path.stat().st_size,
                sha256=mirror.file_digest(audio_path)[1],
            )

            release_manifest = mirror.write_pack_manifest(
                pack,
                [local_file],
                archives=(),
                direct_directory=direct,
                archive_directory=None,
                b2_prefix="release-assets/quran/audio/gapless",
            )
            payload = json.loads((direct / "manifest.json").read_text())

        self.assertIsNone(release_manifest)
        self.assertEqual([], payload["archives"])
        self.assertEqual(1, payload["fileCount"])

    def test_b2_only_main_skips_github_archive_packaging(self) -> None:
        pack = mirror.GaplessPack(
            pack_id="1",
            path_slug="safe_slug",
            timing_database_name="timing",
            source_base_url="https://example.com/",
            file_count=1,
        )
        local_file = mirror.LocalAudioFile(
            name="001.mp3",
            path=Path("/temporary/direct/001.mp3"),
            source_url="https://example.com/001.mp3",
            final_url="https://example.com/001.mp3",
            bytes=5,
            sha256="0" * 64,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(mirror, "load_catalog", return_value=(pack,)),
            mock.patch.object(mirror, "audit_pack", return_value=()),
            mock.patch.object(mirror, "write_audit"),
            mock.patch.object(mirror, "stage_pack", return_value=(local_file,)),
            mock.patch.object(mirror, "ensure_b2_credentials"),
            mock.patch.object(
                mirror,
                "b2_state_is_complete",
                return_value=False,
            ),
            mock.patch.object(mirror, "build_archives") as build_archives,
            mock.patch.object(
                mirror,
                "write_pack_manifest",
                return_value=None,
            ) as write_pack_manifest,
            mock.patch.object(mirror, "publish_b2") as publish_b2,
            mock.patch.object(mirror, "publish_github") as publish_github,
        ):
            result = mirror.main(
                [
                    "--work-dir",
                    directory,
                    "--b2-bucket",
                    "public-bucket",
                    "--skip-github",
                    "--keep-stage",
                ]
            )

        self.assertEqual(0, result)
        build_archives.assert_not_called()
        self.assertIsNone(write_pack_manifest.call_args.args[4])
        publish_b2.assert_called_once()
        publish_github.assert_not_called()

    def test_b2_only_main_skips_completed_pack_before_audit(self) -> None:
        pack = mirror.GaplessPack(
            pack_id="1",
            path_slug="safe_slug",
            timing_database_name="timing",
            source_base_url="https://example.com/",
            file_count=1,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(mirror, "load_catalog", return_value=(pack,)),
            mock.patch.object(mirror, "ensure_b2_credentials") as credentials,
            mock.patch.object(
                mirror,
                "b2_state_is_complete",
                return_value=True,
            ) as state_complete,
            mock.patch.object(mirror, "audit_pack") as audit_pack,
            mock.patch.object(mirror, "stage_pack") as stage_pack,
            mock.patch.object(mirror, "publish_b2") as publish_b2,
        ):
            result = mirror.main(
                [
                    "--work-dir",
                    directory,
                    "--b2-bucket",
                    "public-bucket",
                    "--skip-github",
                ]
            )

        self.assertEqual(0, result)
        credentials.assert_called_once()
        state_complete.assert_called_once()
        audit_pack.assert_not_called()
        stage_pack.assert_not_called()
        publish_b2.assert_not_called()

    def test_github_publish_replaces_same_size_asset_with_wrong_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "audio.zip"
            asset.write_bytes(b"archive")
            expected = mirror.file_digest(asset)
            with (
                mock.patch.object(
                    mirror,
                    "resolve_github_release",
                    return_value="release-tag",
                ),
                mock.patch.object(
                    mirror,
                    "github_asset_metadata",
                    side_effect=[
                        {asset.name: (expected[0], "0" * 64)},
                        {asset.name: expected},
                    ],
                ),
                mock.patch.object(mirror.subprocess, "run") as run,
            ):
                mirror.publish_github(
                    [asset],
                    repository="owner/repository",
                    release="latest",
                )

        command = run.call_args.args[0]
        self.assertIn("--clobber", command)
        self.assertIn(str(asset), command)

    def test_github_publish_accepts_exact_asset_after_upload_command_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "audio.zip"
            asset.write_bytes(b"archive")
            expected = mirror.file_digest(asset)
            with (
                mock.patch.object(
                    mirror,
                    "resolve_github_release",
                    return_value="release-tag",
                ),
                mock.patch.object(
                    mirror,
                    "github_asset_metadata",
                    side_effect=[
                        {},
                        {asset.name: expected},
                        {asset.name: expected},
                    ],
                ),
                mock.patch.object(
                    mirror.subprocess,
                    "run",
                    side_effect=mirror.subprocess.CalledProcessError(
                        returncode=1,
                        cmd=["gh", "release", "upload"],
                    ),
                ),
            ):
                mirror.publish_github(
                    [asset],
                    repository="owner/repository",
                    release="latest",
                )

    def test_github_publish_rejects_mismatched_asset_after_command_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "audio.zip"
            asset.write_bytes(b"archive")
            expected = mirror.file_digest(asset)
            with (
                mock.patch.object(
                    mirror,
                    "resolve_github_release",
                    return_value="release-tag",
                ),
                mock.patch.object(
                    mirror,
                    "github_asset_metadata",
                    side_effect=[
                        {},
                        {asset.name: (expected[0], "0" * 64)},
                    ],
                ),
                mock.patch.object(
                    mirror.subprocess,
                    "run",
                    side_effect=mirror.subprocess.CalledProcessError(
                        returncode=1,
                        cmd=["gh", "release", "upload"],
                    ),
                ),
            ):
                with self.assertRaises(mirror.subprocess.CalledProcessError):
                    mirror.publish_github(
                        [asset],
                        repository="owner/repository",
                        release="latest",
                    )


if __name__ == "__main__":
    unittest.main()
