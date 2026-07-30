from __future__ import annotations

import gzip
import hashlib
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path


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
        self.assertEqual(manifest["hotTile"], route)
        self.assertEqual(manifest["largeTile"], route)
        self.assertEqual(manifest["missingTile"], "/maps/world/tiles/0/x2147483647/z2147483647.prbm")
        self.assertEqual(
            manifest["expected"][route]["decodedSha256"],
            hashlib.sha256(tile_data).hexdigest(),
        )
        self.assertEqual(manifest["expected"][route]["sourceEncoding"], "gzip")


class ContractHelperTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
