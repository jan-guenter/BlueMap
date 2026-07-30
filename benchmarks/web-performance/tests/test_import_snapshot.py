from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


IMPORTER_DIR = Path(__file__).parents[1] / "importer"
sys.path.insert(0, str(IMPORTER_DIR))

import import_snapshot  # noqa: E402


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        self.statements.append((statement, parameters))

    @staticmethod
    def fetchone() -> tuple[int]:
        return (7,)


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


class TileParsingTests(unittest.TestCase):
    def test_parses_flat_tile(self) -> None:
        root = Path("/snapshot/maps/world/tiles")
        tile = root / "0" / "x-7" / "z-9.prbm.gz"

        self.assertEqual(import_snapshot.parse_tile(tile, root), (0, -7, -9))

    def test_flattens_sharded_coordinates(self) -> None:
        root = Path("/snapshot/maps/world/tiles")
        tile = root / "0" / "x-1" / "z-2" / "3.prbm.zst"

        self.assertEqual(import_snapshot.parse_tile(tile, root), (0, -1, -23))

    def test_ignores_unrecognized_paths(self) -> None:
        root = Path("/snapshot/maps/world/tiles")

        self.assertIsNone(import_snapshot.parse_tile(root / "0" / "README", root))
        self.assertIsNone(import_snapshot.parse_tile(root / "not-a-lod" / "x0" / "z0.png", root))


class CompressionTests(unittest.TestCase):
    def test_round_trips_supported_encodings(self) -> None:
        payload = (b"BlueMap snapshot data" * 1000) + bytes(range(256))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for encoding, suffix in (
                ("gzip", ".gz"),
                ("zstd", ".zst"),
                ("deflate", ".deflate"),
            ):
                path = root / f"tile.prbm{suffix}"
                path.write_bytes(import_snapshot.compress(payload, encoding))
                self.assertEqual(import_snapshot.read_uncompressed(path), payload)

    def test_rejects_lz4_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tile.prbm.lz4"
            path.write_bytes(b"not parsed")

            with self.assertRaisesRegex(ValueError, "LZ4"):
                import_snapshot.read_uncompressed(path)


class SqlQuotingTests(unittest.TestCase):
    def test_quotes_mariadb_key_identifier(self) -> None:
        connection = FakeConnection()
        importer = import_snapshot.Importer(connection, "mariadb", "zstd")

        self.assertEqual(
            importer.lookup_id("bluemap_compression", "key", "bluemap:zstd"),
            7,
        )
        statements = [statement for statement, _ in connection.cursor_instance.statements]
        self.assertIn("(`key`)", statements[0])
        self.assertIn("WHERE `key`", statements[1])

    def test_quotes_postgresql_key_identifier(self) -> None:
        connection = FakeConnection()
        importer = import_snapshot.Importer(connection, "postgresql", "zstd")

        self.assertEqual(
            importer.lookup_id("bluemap_compression", "key", "bluemap:zstd"),
            7,
        )
        statements = [statement for statement, _ in connection.cursor_instance.statements]
        self.assertIn('("key")', statements[0])
        self.assertIn('ON CONFLICT ("key")', statements[0])
        self.assertIn('WHERE "key"', statements[1])


if __name__ == "__main__":
    unittest.main()
