from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))
SCRIPT_PATH = SCRIPTS_DIRECTORY / "mirror_verse_quran_audio.py"
SPEC = importlib.util.spec_from_file_location("mirror_verse_quran_audio", SCRIPT_PATH)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mirror
SPEC.loader.exec_module(mirror)


class FakeResponse:
    def __init__(
        self,
        url: str,
        payload: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.payload = payload
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}
        self.offset = 0

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload) - self.offset
        result = self.payload[self.offset : self.offset + size]
        self.offset += len(result)
        return result


class VerseQuranAudioMirrorTests(unittest.TestCase):
    def test_repository_catalog_matches_all_27_app_verse_packs(self) -> None:
        packs = mirror.load_catalog(
            REPOSITORY_ROOT / "config" / "quran-audio-verse.json"
        )

        self.assertEqual(27, len(packs))
        self.assertEqual(6236, len(mirror.expected_verse_names()))
        self.assertEqual("001001.mp3", mirror.expected_verse_names()[0])
        self.assertEqual("114006.mp3", mirror.expected_verse_names()[-1])
        self.assertEqual(
            {
                *map(str, range(4, 22)),
                "34",
                "35",
                "36",
                "37",
                "42",
                "43",
                "44",
                "45",
                "46",
            },
            {pack.pack_id for pack in packs},
        )
        self.assertEqual(
            "English/Sahih_Intnl_Ibrahim_Walk_192kbps",
            next(pack for pack in packs if pack.pack_id == "11").source_path,
        )
        self.assertEqual(
            "MultiLanguage/Basfar_Walk_192kbps",
            next(pack for pack in packs if pack.pack_id == "35").source_path,
        )
        self.assertEqual(
            "translations/urdu_farhat_hashmi",
            next(pack for pack in packs if pack.pack_id == "36").source_path,
        )
        self.assertEqual(
            "translations/urdu_shamshad_ali_khan_46kbps",
            next(pack for pack in packs if pack.pack_id == "37").source_path,
        )
        no_archive = [pack.pack_id for pack in packs if pack.archive_bytes is None]
        self.assertEqual(["42"], no_archive)

    def test_catalog_rejects_unsafe_source_path(self) -> None:
        payload = {
            "schemaVersion": 1,
            "fileCount": 6236,
            "archiveTotalBytes": 1,
            "packs": [
                {
                    "packId": "1",
                    "pathSlug": "unsafe",
                    "sourcePath": "../private",
                    "archiveBytes": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(mirror.common.MirrorError):
                mirror.load_catalog(path)

    def test_audit_accepts_expected_listing_and_exact_archive_size(self) -> None:
        pack = mirror.VersePack(
            pack_id="1",
            path_slug="test",
            source_path="Test_Reciter",
            archive_bytes=123,
            file_count=3,
        )
        names = ("001001.mp3", "001002.mp3", "114006.mp3")
        listing = "".join(
            f'<a href="{name}">{name}</a>'
            for name in (*names, "002000.mp3")
        ).encode()

        def request(request: object, timeout: int) -> FakeResponse:
            del timeout
            if request.method == "HEAD":
                return FakeResponse(
                    request.full_url,
                    status=200,
                    headers={"Content-Length": "123"},
                )
            return FakeResponse(request.full_url, listing)

        with mock.patch.object(mirror, "expected_verse_names", return_value=names):
            result = mirror.audit_pack(
                pack,
                retries=1,
                request_function=request,
            )

        self.assertEqual(3, result["fileCount"])
        self.assertEqual(4, result["listedFileCount"])
        self.assertEqual(1, result["ignoredExtraAudioFiles"])
        self.assertEqual(123, result["archive"]["bytes"])

    def test_archive_extraction_is_safe_resumable_and_repairs_corruption(self) -> None:
        pack = mirror.VersePack(
            pack_id="1",
            path_slug="test",
            source_path="Test_Reciter",
            archive_bytes=1,
            file_count=3,
        )
        payloads = {
            "001001.mp3": b"one-audio",
            "001002.mp3": b"two-audio",
            "114006.mp3": b"end-audio",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "source.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in payloads.items():
                    archive.writestr(f"nested/{name}", payload)
                archive.writestr("../../ignored.txt", b"ignored")
            names = tuple(payloads)
            with mock.patch.object(
                mirror,
                "expected_verse_names",
                return_value=names,
            ):
                first = mirror.extract_source_archive(
                    pack,
                    archive_path,
                    root / "direct",
                    root / "state",
                )
                first[0].path.write_bytes(b"bad-audio")
                second = mirror.extract_source_archive(
                    pack,
                    archive_path,
                    root / "direct",
                    root / "state",
                )

            self.assertEqual(3, len(first))
            self.assertEqual(payloads["001001.mp3"], second[0].path.read_bytes())
            self.assertFalse((root / "ignored.txt").exists())
            self.assertEqual(3, len(list((root / "state").glob("*.json"))))

    def test_archive_rejects_duplicate_expected_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "source.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one/001001.mp3", b"one")
                archive.writestr("two/001001.mp3", b"two")
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(mirror.common.MirrorError):
                    mirror.archive_audio_members(archive, ["001001.mp3"])

    def test_archive_download_resumes_from_existing_partial_file(self) -> None:
        payload = b"complete-archive"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.zip"
            partial = target.with_suffix(".zip.part")
            partial.write_bytes(payload[:4])

            def request(request: object, timeout: int) -> FakeResponse:
                del timeout
                self.assertEqual("bytes=4-", request.headers["Range"])
                return FakeResponse(
                    request.full_url,
                    payload[4:],
                    status=206,
                    headers={
                        "Content-Length": str(len(payload) - 4),
                        "Content-Range": f"bytes 4-{len(payload) - 1}/{len(payload)}",
                    },
                )

            mirror.download_resumable_archive(
                "https://audio.example/source.zip",
                len(payload),
                target,
                retries=1,
                request_function=request,
            )

            self.assertEqual(payload, target.read_bytes())
            self.assertFalse(partial.exists())

    def test_disk_guard_rejects_pack_that_cannot_fit(self) -> None:
        usage = mock.Mock(free=99)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(mirror.shutil, "disk_usage", return_value=usage),
            self.assertRaises(mirror.common.MirrorError),
        ):
            mirror.require_free_disk_space(
                Path(directory),
                required_bytes=100,
                operation="test staging",
            )

    def test_archive_builder_partitions_and_verifies_every_file(self) -> None:
        pack = mirror.VersePack(
            pack_id="1",
            path_slug="test",
            source_path="Test_Reciter",
            archive_bytes=1,
            file_count=3,
        )
        payloads = {
            "001001.mp3": b"1234567",
            "001002.mp3": b"12345678901",
            "114006.mp3": b"1234567890123456789",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct"
            direct.mkdir()
            files = []
            for name, payload in payloads.items():
                path = direct / name
                path.write_bytes(payload)
                size, sha256 = mirror.common.file_digest(path)
                files.append(
                    mirror.common.LocalAudioFile(
                        name=name,
                        path=path,
                        source_url=f"https://audio.example/{name}",
                        final_url=f"https://audio.example/{name}",
                        bytes=size,
                        sha256=sha256,
                    )
                )
            archives = mirror.build_archives(
                pack,
                files,
                root / "archives",
                max_archive_bytes=mirror.ARCHIVE_HEADROOM + 30,
            )

            self.assertEqual(2, len(archives))
            names: list[str] = []
            for archive in archives:
                with zipfile.ZipFile(archive["path"]) as zip_file:
                    self.assertIsNone(zip_file.testzip())
                    names.extend(zip_file.namelist())
            self.assertEqual(sorted(payloads), sorted(names))

    def test_b2_publish_uses_nested_source_path_without_credentials(self) -> None:
        pack = mirror.VersePack(
            pack_id="1",
            path_slug="test",
            source_path="translations/Test_Reciter",
            archive_bytes=1,
            file_count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "direct"
            source.mkdir()
            (source / "001001.mp3").write_bytes(b"audio")
            with mock.patch.object(mirror.subprocess, "run") as run:
                mirror.publish_b2(
                    source,
                    pack,
                    bucket="public-bucket",
                    prefix="release-assets/quran/audio/verse",
                    state_directory=root / "state",
                )

        command = run.call_args.args[0]
        self.assertIn(
            "release-assets/quran/audio/verse/translations/Test_Reciter",
            command,
        )
        self.assertFalse(any("APPLICATION_KEY" in part for part in command))


if __name__ == "__main__":
    unittest.main()
