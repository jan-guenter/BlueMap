#!/usr/bin/env python3
"""Import a file-backed BlueMap web snapshot into a disposable SQL database."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import ssl
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import zstandard


GRID_PATH = re.compile(r"^x(-?\d+)z(-?\d+)$")
COMPRESSED_SUFFIXES = (".gz", ".zst", ".deflate", ".lz4")


@dataclass(frozen=True)
class DatabaseConfig:
    backend: str
    host: str
    port: int
    database: str
    username: str
    password: str
    tls_mode: str
    ca_file: str | None
    client_cert: str | None
    client_key: str | None


class Importer:
    def __init__(self, connection, backend: str, target_compression: str):
        self.connection = connection
        self.backend = backend
        self.target_compression = target_compression
        self.cursor = connection.cursor()
        self.rows_since_commit = 0
        self.bytes_since_commit = 0
        self.imported_grid_rows = 0
        self.imported_item_rows = 0

    def initialize_schema(self) -> None:
        statements = mariadb_schema() if self.backend == "mariadb" else postgres_schema()
        for statement in statements:
            self.cursor.execute(statement)
        self.connection.commit()

    def import_webroot(self, webroot: Path) -> None:
        maps_root = webroot / "maps"
        map_roots = sorted(path for path in maps_root.iterdir() if path.is_dir())
        if not map_roots:
            raise ValueError(f"No maps found below {maps_root}")

        for map_root in map_roots:
            print(f"Importing map {map_root.name}", flush=True)
            self.import_map(map_root.name, map_root)

        self.connection.commit()
        print(
            f"Imported {self.imported_grid_rows} grid rows and "
            f"{self.imported_item_rows} item rows",
            flush=True,
        )

    def import_map(self, map_id: str, map_root: Path) -> None:
        map_key = self.lookup_id("bluemap_map", "map_id", map_id)

        for tile_path in regular_files(map_root / "tiles"):
            parsed = parse_tile(tile_path, map_root / "tiles")
            if parsed is None:
                continue
            lod, x, z = parsed
            if lod == 0:
                storage_key = "bluemap:hires"
                compression = self.target_compression
                raw = read_uncompressed(tile_path)
                payload = compress(raw, compression)
            else:
                storage_key = f"bluemap:lowres/{lod}"
                compression = "none"
                payload = tile_path.read_bytes()

            self.write_grid(
                map_key,
                self.lookup_id("bluemap_grid_storage", "key", storage_key),
                x,
                z,
                self.lookup_id("bluemap_compression", "key", f"bluemap:{compression}"),
                payload,
                modification_time_ms(tile_path),
            )

        item_candidates: list[tuple[Path, str, str]] = [
            (map_root / "settings.json", "bluemap:settings", "none"),
            (find_compressed_file(map_root, "textures.json"), "bluemap:textures", self.target_compression),
            (map_root / "live" / "markers.json", "bluemap:markers", "none"),
            (map_root / "live" / "players.json", "bluemap:players", "none"),
        ]

        for path in regular_files(map_root / "assets"):
            asset_name = path.relative_to(map_root / "assets").as_posix()
            item_candidates.append((path, f"bluemap:asset/{asset_name}", "none"))

        for path, storage_key, compression in item_candidates:
            if not path.is_file():
                continue
            if compression == "none":
                payload = path.read_bytes()
            else:
                payload = compress(read_uncompressed(path), compression)
            self.write_item(
                map_key,
                self.lookup_id("bluemap_item_storage", "key", storage_key),
                self.lookup_id("bluemap_compression", "key", f"bluemap:{compression}"),
                payload,
                modification_time_ms(path),
            )

    def lookup_id(self, table: str, key_column: str, value: str) -> int:
        quoted_column = (
            f"`{key_column}`" if self.backend == "mariadb" else f'"{key_column}"'
        )
        if self.backend == "mariadb":
            self.cursor.execute(
                f"INSERT IGNORE INTO {table} ({quoted_column}) VALUES (%s)",
                (value,),
            )
        else:
            self.cursor.execute(
                f"INSERT INTO {table} ({quoted_column}) VALUES (%s) "
                f"ON CONFLICT ({quoted_column}) DO NOTHING",
                (value,),
            )
        self.cursor.execute(
            f"SELECT id FROM {table} WHERE {quoted_column} = %s",
            (value,),
        )
        row = self.cursor.fetchone()
        if row is None:
            raise RuntimeError(f"Failed to resolve {table}.{key_column}")
        return int(row[0])

    def write_grid(
        self,
        map_key: int,
        storage_key: int,
        x: int,
        z: int,
        compression_key: int,
        payload: bytes,
        updated_at: int,
    ) -> None:
        digest = hashlib.sha256(payload).digest()
        if self.backend == "mariadb":
            statement = """
                INSERT INTO bluemap_grid_storage_data
                  (map, storage, x, z, compression, data, content_hash, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  compression = VALUES(compression),
                  data = VALUES(data),
                  content_hash = VALUES(content_hash),
                  updated_at = VALUES(updated_at)
            """
        else:
            statement = """
                INSERT INTO bluemap_grid_storage_data
                  (map, storage, x, z, compression, data, content_hash, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (map, storage, x, z) DO UPDATE SET
                  compression = excluded.compression,
                  data = excluded.data,
                  content_hash = excluded.content_hash,
                  updated_at = excluded.updated_at
            """
        self.cursor.execute(
            statement,
            (map_key, storage_key, x, z, compression_key, payload, digest, updated_at),
        )
        self.imported_grid_rows += 1
        self.maybe_commit(len(payload))

    def write_item(
        self,
        map_key: int,
        storage_key: int,
        compression_key: int,
        payload: bytes,
        updated_at: int,
    ) -> None:
        digest = hashlib.sha256(payload).digest()
        if self.backend == "mariadb":
            statement = """
                INSERT INTO bluemap_item_storage_data
                  (map, storage, compression, data, content_hash, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  compression = VALUES(compression),
                  data = VALUES(data),
                  content_hash = VALUES(content_hash),
                  updated_at = VALUES(updated_at)
            """
        else:
            statement = """
                INSERT INTO bluemap_item_storage_data
                  (map, storage, compression, data, content_hash, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (map, storage) DO UPDATE SET
                  compression = excluded.compression,
                  data = excluded.data,
                  content_hash = excluded.content_hash,
                  updated_at = excluded.updated_at
            """
        self.cursor.execute(
            statement,
            (map_key, storage_key, compression_key, payload, digest, updated_at),
        )
        self.imported_item_rows += 1
        self.maybe_commit(len(payload))

    def maybe_commit(self, payload_size: int) -> None:
        self.rows_since_commit += 1
        self.bytes_since_commit += payload_size
        if self.rows_since_commit >= 100 or self.bytes_since_commit >= 64 * 1024 * 1024:
            self.connection.commit()
            self.rows_since_commit = 0
            self.bytes_since_commit = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("webroot", type=Path)
    parser.add_argument(
        "--target-compression",
        choices=("gzip", "zstd", "deflate", "none"),
        default="zstd",
    )
    return parser.parse_args()


def load_database_config() -> DatabaseConfig:
    backend = required_env("DB_BACKEND")
    if backend not in {"mariadb", "postgresql"}:
        raise ValueError("DB_BACKEND must be mariadb or postgresql")
    default_port = 3306 if backend == "mariadb" else 5432
    tls_mode = os.getenv("DB_TLS_MODE", "verify-full")
    if tls_mode not in {"disable", "required", "verify-ca", "verify-full"}:
        raise ValueError("Invalid DB_TLS_MODE")
    return DatabaseConfig(
        backend=backend,
        host=required_env("DB_HOST"),
        port=int(os.getenv("DB_PORT", str(default_port))),
        database=os.getenv("DB_DATABASE", "bluemap"),
        username=required_env("DB_USERNAME"),
        password=required_env("DB_PASSWORD"),
        tls_mode=tls_mode,
        ca_file=os.getenv("DB_TLS_CA"),
        client_cert=os.getenv("DB_TLS_CLIENT_CERT"),
        client_key=os.getenv("DB_TLS_CLIENT_KEY"),
    )


def connect(config: DatabaseConfig):
    if config.backend == "mariadb":
        import pymysql

        ssl_context = None
        if config.tls_mode != "disable":
            ssl_context = ssl.create_default_context()
            if config.tls_mode == "required":
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            else:
                if not config.ca_file:
                    raise ValueError(f"{config.tls_mode} requires DB_TLS_CA")
                ssl_context.load_verify_locations(cafile=config.ca_file)
                ssl_context.verify_mode = ssl.CERT_REQUIRED
                ssl_context.check_hostname = config.tls_mode == "verify-full"
            if config.client_cert:
                ssl_context.load_cert_chain(config.client_cert, config.client_key)

        return pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.username,
            password=config.password,
            database=config.database,
            ssl=ssl_context,
            ssl_disabled=config.tls_mode == "disable",
            autocommit=False,
        )

    import psycopg

    ssl_mode = {
        "disable": "disable",
        "required": "require",
        "verify-ca": "verify-ca",
        "verify-full": "verify-full",
    }[config.tls_mode]
    parameters: dict[str, object] = {
        "host": config.host,
        "port": config.port,
        "dbname": config.database,
        "user": config.username,
        "password": config.password,
        "sslmode": ssl_mode,
        "autocommit": False,
    }
    if config.ca_file:
        parameters["sslrootcert"] = config.ca_file
    if config.client_cert:
        parameters["sslcert"] = config.client_cert
    if config.client_key:
        parameters["sslkey"] = config.client_key
    return psycopg.connect(**parameters)


def regular_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".part"):
            yield path


def parse_tile(path: Path, tiles_root: Path) -> tuple[int, int, int] | None:
    relative = path.relative_to(tiles_root)
    if len(relative.parts) < 2:
        return None
    try:
        lod = int(relative.parts[0])
    except ValueError:
        return None

    encoded = "".join(relative.parts[1:])
    for suffix in COMPRESSED_SUFFIXES:
        if encoded.endswith(suffix):
            encoded = encoded[: -len(suffix)]
            break
    for suffix in (".prbm", ".png"):
        if encoded.endswith(suffix):
            encoded = encoded[: -len(suffix)]
            break

    match = GRID_PATH.fullmatch(encoded)
    if match is None:
        return None
    return lod, int(match.group(1)), int(match.group(2))


def find_compressed_file(root: Path, base_name: str) -> Path:
    for suffix in ("",) + COMPRESSED_SUFFIXES:
        candidate = root / f"{base_name}{suffix}"
        if candidate.is_file():
            return candidate
    return root / base_name


def read_uncompressed(path: Path) -> bytes:
    if path.name.endswith(".gz"):
        with gzip.open(path, "rb") as stream:
            return stream.read()
    if path.name.endswith(".zst"):
        with path.open("rb") as stream:
            return zstandard.ZstdDecompressor().stream_reader(stream).read()
    if path.name.endswith(".deflate"):
        return zlib.decompress(path.read_bytes())
    if path.name.endswith(".lz4"):
        raise ValueError(f"Java LZ4 block data is not supported by the importer: {path}")
    return path.read_bytes()


def compress(data: bytes, compression: str) -> bytes:
    if compression == "none":
        return data
    if compression == "gzip":
        return gzip.compress(data, compresslevel=6, mtime=0)
    if compression == "deflate":
        return zlib.compress(data)
    if compression == "zstd":
        return zstandard.ZstdCompressor(level=3).compress(data)
    raise ValueError(f"Unsupported compression {compression}")


def modification_time_ms(path: Path) -> int:
    return path.stat().st_mtime_ns // 1_000_000


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def mariadb_schema() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE IF NOT EXISTS bluemap_map (
          id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
          map_id VARCHAR(190) NOT NULL,
          PRIMARY KEY (id),
          UNIQUE INDEX map_id (map_id)
        ) COLLATE 'utf8mb4_bin'
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_compression (
          id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
          `key` VARCHAR(190) NOT NULL,
          PRIMARY KEY (id),
          UNIQUE INDEX compression_key (`key`)
        ) COLLATE 'utf8mb4_bin'
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_item_storage (
          id INT UNSIGNED NOT NULL AUTO_INCREMENT,
          `key` VARCHAR(190) NOT NULL,
          PRIMARY KEY (id),
          UNIQUE INDEX item_storage_key (`key`)
        ) COLLATE 'utf8mb4_bin'
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_grid_storage (
          id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
          `key` VARCHAR(190) NOT NULL,
          PRIMARY KEY (id),
          UNIQUE INDEX grid_storage_key (`key`)
        ) COLLATE 'utf8mb4_bin'
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_item_storage_data (
          map SMALLINT UNSIGNED NOT NULL,
          storage INT UNSIGNED NOT NULL,
          compression SMALLINT UNSIGNED NOT NULL,
          data LONGBLOB NOT NULL,
          content_hash BINARY(32) NULL,
          updated_at BIGINT NULL,
          PRIMARY KEY (map, storage),
          CONSTRAINT fk_bluemap_item_map FOREIGN KEY (map)
            REFERENCES bluemap_map (id) ON UPDATE RESTRICT ON DELETE CASCADE,
          CONSTRAINT fk_bluemap_item FOREIGN KEY (storage)
            REFERENCES bluemap_item_storage (id) ON UPDATE RESTRICT ON DELETE CASCADE,
          CONSTRAINT fk_bluemap_item_compression FOREIGN KEY (compression)
            REFERENCES bluemap_compression (id) ON UPDATE RESTRICT ON DELETE CASCADE
        ) COLLATE 'utf8mb4_bin'
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_grid_storage_data (
          map SMALLINT UNSIGNED NOT NULL,
          storage SMALLINT UNSIGNED NOT NULL,
          x INT NOT NULL,
          z INT NOT NULL,
          compression SMALLINT UNSIGNED NOT NULL,
          data LONGBLOB NOT NULL,
          content_hash BINARY(32) NULL,
          updated_at BIGINT NULL,
          PRIMARY KEY (map, storage, x, z),
          CONSTRAINT fk_bluemap_grid_map FOREIGN KEY (map)
            REFERENCES bluemap_map (id) ON UPDATE RESTRICT ON DELETE CASCADE,
          CONSTRAINT fk_bluemap_grid FOREIGN KEY (storage)
            REFERENCES bluemap_grid_storage (id) ON UPDATE RESTRICT ON DELETE CASCADE,
          CONSTRAINT fk_bluemap_grid_compression FOREIGN KEY (compression)
            REFERENCES bluemap_compression (id) ON UPDATE RESTRICT ON DELETE CASCADE
        ) COLLATE 'utf8mb4_bin'
        """,
    )


def postgres_schema() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE IF NOT EXISTS bluemap_map (
          id SMALLSERIAL PRIMARY KEY,
          map_id VARCHAR(190) UNIQUE NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_compression (
          id SMALLSERIAL PRIMARY KEY,
          key VARCHAR(190) UNIQUE NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_item_storage (
          id SERIAL PRIMARY KEY,
          key VARCHAR(190) UNIQUE NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_grid_storage (
          id SMALLSERIAL PRIMARY KEY,
          key VARCHAR(190) UNIQUE NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_item_storage_data (
          map SMALLINT NOT NULL REFERENCES bluemap_map (id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
          storage INT NOT NULL REFERENCES bluemap_item_storage (id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
          compression SMALLINT NOT NULL REFERENCES bluemap_compression (id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
          data BYTEA NOT NULL,
          content_hash BYTEA NULL,
          updated_at BIGINT NULL,
          PRIMARY KEY (map, storage)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bluemap_grid_storage_data (
          map SMALLINT NOT NULL REFERENCES bluemap_map (id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
          storage SMALLINT NOT NULL REFERENCES bluemap_grid_storage (id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
          x INT NOT NULL,
          z INT NOT NULL,
          compression SMALLINT NOT NULL REFERENCES bluemap_compression (id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
          data BYTEA NOT NULL,
          content_hash BYTEA NULL,
          updated_at BIGINT NULL,
          PRIMARY KEY (map, storage, x, z)
        )
        """,
    )


def main() -> int:
    args = parse_args()
    webroot = args.webroot.resolve()
    if not (webroot / "maps").is_dir():
        print(f"{webroot} does not contain a maps directory", file=sys.stderr)
        return 2

    start = time.monotonic()
    config = load_database_config()
    connection = connect(config)
    try:
        importer = Importer(connection, config.backend, args.target_compression)
        importer.initialize_schema()
        importer.import_webroot(webroot)
    finally:
        connection.close()
    print(f"Import completed in {time.monotonic() - start:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
