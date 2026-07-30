from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock


TOOLS_DIR = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import check_http_contract  # noqa: E402
import generate_manifest  # noqa: E402


class ManifestTests(unittest.TestCase):
    def test_generates_routes_and_decoded_golden_hashes(self) -> None:
        tile_data = b"representative hires tile"
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory)
            (webroot / "index.html").write_text("<html></html>", encoding="utf-8")
            tile = webroot / "maps" / "world" / "tiles" / "0" / "x-1" / "z-2.prbm.gz"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(gzip.compress(tile_data, mtime=0))
            settings = webroot / "maps" / "world" / "settings.json"
            settings.write_text("{}", encoding="utf-8")

            manifest = generate_manifest.generate(webroot)

        route = "/maps/world/tiles/0/x-1/z-2.prbm"
        self.assertEqual(manifest["mapIds"], ["world"])
        self.assertEqual(manifest["hotTile"], route)
        self.assertEqual(manifest["largeTile"], route)
        self.assertEqual(manifest["largeObject"], "/maps/world/settings.json")
        self.assertEqual(manifest["settings"], ["/maps/world/settings.json"])
        self.assertEqual(manifest["textures"], [])
        self.assertEqual(
            manifest["missingTile"],
            "/maps/world/tiles/0/x2/1/4/7/4/8/3/6/4/7/z2/1/4/7/4/8/3/6/4/7.prbm",
        )
        self.assertEqual(
            manifest["expected"][route]["decodedSha256"],
            hashlib.sha256(tile_data).hexdigest(),
        )
        self.assertEqual(manifest["expected"][route]["sourceEncoding"], "gzip")

    def test_selects_only_requested_map_and_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory)
            (webroot / "index.html").write_text("<html></html>", encoding="utf-8")
            for map_id in ("world", "other"):
                tile = (
                    webroot
                    / "maps"
                    / map_id
                    / "tiles"
                    / "0"
                    / "x0"
                    / "z0.prbm.gz"
                )
                tile.parent.mkdir(parents=True)
                tile.write_bytes(gzip.compress(map_id.encode(), mtime=0))
                (webroot / "maps" / map_id / "settings.json").write_text(
                    "{}",
                    encoding="utf-8",
                )

            manifest = generate_manifest.generate(webroot, ["world"])

        self.assertEqual(manifest["mapIds"], ["world"])
        self.assertTrue(
            all(
                not path.startswith("/maps/other/")
                for field in (
                    "tiles",
                    "settings",
                    "textures",
                    "assets",
                    "players",
                    "markers",
                )
                for path in manifest[field]
            )
        )

    def test_rejects_unknown_requested_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            webroot = Path(directory)
            (webroot / "index.html").write_text("<html></html>", encoding="utf-8")
            (webroot / "maps").mkdir()

            with self.assertRaisesRegex(ValueError, "do not exist"):
                generate_manifest.generate(webroot, ["world"])

    def test_fixture_expectations_do_not_modify_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            webroot = root / "web"
            fixture = root / "players-fixture.json"
            (webroot / "index.html").parent.mkdir(parents=True)
            (webroot / "index.html").write_text("<html></html>", encoding="utf-8")
            tile = webroot / "maps" / "world" / "tiles" / "0" / "x0" / "z0.prbm.gz"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(gzip.compress(b"tile", mtime=0))
            players = webroot / "maps" / "world" / "live" / "players.json"
            players.parent.mkdir(parents=True)
            players.write_text("{}", encoding="utf-8")
            fixture_payload = b'{"players":[{"name":"fixture"}]}\n'
            fixture.write_bytes(fixture_payload)

            manifest = generate_manifest.generate(
                webroot,
                ["world"],
                players_fixture=fixture,
            )
            snapshot_players = players.read_text(encoding="utf-8")

        route = "/maps/world/live/players.json"
        self.assertEqual(
            manifest["expected"][route]["decodedSha256"],
            hashlib.sha256(fixture_payload).hexdigest(),
        )
        self.assertEqual(snapshot_players, "{}")
        self.assertEqual(
            manifest["fixtures"]["players"]["sha256"],
            hashlib.sha256(fixture_payload).hexdigest(),
        )


class ContractHelperTests(unittest.TestCase):
    def test_fetch_records_headers_and_body_completion_without_query_data(
        self,
    ) -> None:
        class FakeResponse:
            status = 200
            headers = Message()

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b"response body"

        FakeResponse.headers["Content-Length"] = "13"
        FakeResponse.headers["Content-Encoding"] = "zstd"
        stderr = io.StringIO()
        with (
            mock.patch.object(
                check_http_contract.HTTP_OPENER,
                "open",
                return_value=FakeResponse(),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            response = check_http_contract.fetch(
                "http://127.0.0.1:8100",
                "/maps/world/settings.json?credential=secret",
                {"User-Agent": "test"},
            )

        events = [
            json.loads(line)
            for line in stderr.getvalue().splitlines()
        ]
        self.assertEqual(response.body, b"response body")
        self.assertEqual(
            [event["event"] for event in events],
            ["request-start", "response-headers", "response-complete"],
        )
        self.assertTrue(
            all(
                event["path"] == "/maps/world/settings.json"
                for event in events
            )
        )
        self.assertNotIn("secret", stderr.getvalue())
        self.assertEqual(events[1]["contentLength"], "13")
        self.assertEqual(events[2]["bodyBytes"], 13)

    def test_fetch_records_the_phase_of_a_body_failure(self) -> None:
        class FailingResponse:
            status = 200
            headers = Message()

            def __enter__(self) -> FailingResponse:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                raise TimeoutError("must not be included in diagnostics")

        stderr = io.StringIO()
        with (
            mock.patch.object(
                check_http_contract.HTTP_OPENER,
                "open",
                return_value=FailingResponse(),
            ),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(TimeoutError),
        ):
            check_http_contract.fetch(
                "http://127.0.0.1:8100",
                "/maps/world/asset",
                {"User-Agent": "test"},
            )

        events = [
            json.loads(line)
            for line in stderr.getvalue().splitlines()
        ]
        self.assertEqual(events[-1]["event"], "request-error")
        self.assertEqual(events[-1]["phase"], "body")
        self.assertEqual(events[-1]["errorType"], "TimeoutError")
        self.assertNotIn("must not be included", stderr.getvalue())

    def test_decodes_and_hashes_gzip_response(self) -> None:
        body = b"stored body"
        headers = Message()
        headers["Content-Encoding"] = "gzip"
        response = check_http_contract.Response(200, headers, gzip.compress(body, mtime=0))

        check_http_contract.check_body(
            response,
            {
                "decodedSha256": hashlib.sha256(body).hexdigest(),
                "decodedSize": len(body),
            },
            "/maps/world/example",
        )

    def test_header_tokens_are_case_insensitive_for_values(self) -> None:
        headers = Message()
        headers["Cache-Control"] = "private, No-Store"
        response = check_http_contract.Response(200, headers, b"")

        self.assertEqual(
            check_http_contract.header_tokens(response, "cache-control"),
            {"private", "no-store"},
        )

    def test_strong_sha256_etag_must_match_transferred_bytes(self) -> None:
        body = b"stored compressed representation"
        response = check_http_contract.Response(200, Message(), body)
        etag = f'"{hashlib.sha256(body).hexdigest()}"'

        check_http_contract.check_strong_sha256_etag(response, etag, "/tile")
        with self.assertRaisesRegex(
            check_http_contract.ContractFailure,
            "transferred representation",
        ):
            check_http_contract.check_strong_sha256_etag(
                response,
                f'"{"0" * 64}"',
                "/tile",
            )

    def test_manifest_validation_rejects_routes_outside_selected_maps(self) -> None:
        manifest = {
            "mapIds": ["world"],
            "tiles": ["/maps/other/tiles/0/x0z0.prbm"],
            "settings": [],
            "textures": [],
            "assets": [],
            "players": [],
            "markers": [],
            "hotTile": "/maps/world/tiles/0/x0z0.prbm",
            "largeTile": "/maps/world/tiles/0/x0z0.prbm",
            "largeObject": "/maps/world/settings.json",
            "missingTile": "/maps/world/tiles/0/x1z1.prbm",
            "expected": {
                "/maps/other/tiles/0/x0z0.prbm": {},
            },
        }

        with self.assertRaisesRegex(
            check_http_contract.ContractFailure,
            "outside mapIds",
        ):
            check_http_contract.validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
