from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).parents[1]
TOOLS_DIR = BENCHMARK_ROOT / "tools"
IMPORTER_DIR = BENCHMARK_ROOT / "importer"
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(IMPORTER_DIR))

import copy_snapshot  # noqa: E402
import generate_live_fixtures  # noqa: E402
import import_snapshot  # noqa: E402


class SnapshotCopyTests(unittest.TestCase):
    def test_copies_and_verifies_normalized_content_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            (source / "maps" / "world").mkdir(parents=True)
            (source / "index.html").write_text("index", encoding="utf-8")
            (source / "maps" / "world" / "settings.json").write_text(
                '{"name":"world"}',
                encoding="utf-8",
            )
            destination.mkdir()
            (destination / "lost+found").mkdir()

            receipt = copy_snapshot.create_snapshot(source, destination)

            copied = destination / "bluemap" / "web"
            self.assertEqual(
                (copied / "maps" / "world" / "settings.json").read_text(
                    encoding="utf-8"
                ),
                '{"name":"world"}',
            )
            self.assertEqual(receipt["fileCount"], 2)
            self.assertEqual(
                receipt["treeSha256"],
                copy_snapshot.tree_digest(copy_snapshot.inventory(copied)),
            )
            stored_receipt = json.loads(
                (destination / "SNAPSHOT.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored_receipt["treeSha256"], receipt["treeSha256"])
            self.assertEqual(os.stat(copied / "index.html").st_mode & 0o777, 0o444)

    def test_rejects_non_blank_destination_and_source_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "index.html").write_text("index", encoding="utf-8")
            destination = root / "destination"
            destination.mkdir()
            (destination / "unexpected").write_text("data", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not blank"):
                copy_snapshot.create_snapshot(source, destination)

            (destination / "unexpected").unlink()
            (source / "link").symlink_to(source / "index.html")
            with self.assertRaisesRegex(ValueError, "symlink"):
                copy_snapshot.create_snapshot(source, destination)


class LiveFixtureTests(unittest.TestCase):
    def test_fixtures_are_deterministic_representative_and_importable(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first = Path(first_directory)
            second = Path(second_directory)
            first_summary = generate_live_fixtures.generate(first, 32, 64)
            second_summary = generate_live_fixtures.generate(second, 32, 64)

            self.assertEqual(first_summary, second_summary)
            self.assertEqual(
                (first / "SHA256SUMS").read_text(encoding="ascii"),
                (second / "SHA256SUMS").read_text(encoding="ascii"),
            )
            players = import_snapshot.load_live_fixture(
                first / "players.json",
                "players",
            )
            markers = import_snapshot.load_live_fixture(
                first / "markers.json",
                "markers",
            )
            self.assertEqual(players.sha256, first_summary["players"]["sha256"])
            self.assertEqual(markers.sha256, first_summary["markers"]["sha256"])
            self.assertGreater(len(players.payload), 2)
            self.assertGreater(len(markers.payload), 2)


if __name__ == "__main__":
    unittest.main()
