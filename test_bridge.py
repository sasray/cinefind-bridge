import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# The bridge container installs httpx. A minimal test stub keeps the pure
# configuration and Seerr-payload tests runnable on stripped-down host Python
# installations as well; CI installs the real dependency before this suite.
if "httpx" not in sys.modules:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.HTTPError = OSError
    sys.modules["httpx"] = httpx_stub

import bridge


class BridgeConfigTests(unittest.TestCase):
    def test_normalises_local_seerr_url(self) -> None:
        self.assertEqual(bridge.clean_seerr_url("http://192.168.1.20:5055/"), "http://192.168.1.20:5055")
        self.assertEqual(bridge.clean_seerr_url("https://seerr.local/api/v1"), "https://seerr.local")

    def test_rejects_credentials_in_seerr_url(self) -> None:
        with self.assertRaises(bridge.BridgeError):
            bridge.clean_seerr_url("http://secret@example.test")

    def test_writes_device_token_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original = bridge.CONFIG_PATH
            try:
                bridge.CONFIG_PATH = Path(temp) / "config.json"
                bridge.save_paired_device("https://example.test/bridge", "device-id", "x" * 40)
                loaded = bridge.load_paired_device()
                self.assertEqual(loaded["device_id"], "device-id")
                self.assertEqual(bridge.CONFIG_PATH.stat().st_mode & 0o777, 0o600)
            finally:
                bridge.CONFIG_PATH = original


class SeerrRequestTests(unittest.TestCase):
    def config(self) -> dict[str, str]:
        return {"seerr_url": "http://seerr", "seerr_api_key": "key"}

    def response(self, status: int, body: dict):
        class Response:
            status_code = status
            is_success = 200 <= status < 300
            text = json.dumps(body)

            @staticmethod
            def json():
                return body

        return Response()

    def test_uses_tmdb_and_all_seasons_for_tv(self) -> None:
        client = Mock()
        client.post.return_value = self.response(201, {"id": 42})
        result = bridge.create_seerr_request(client, self.config(), {"mediaType": "tv", "tmdbId": 12})
        self.assertEqual(result, ("requested", 42, None))
        self.assertEqual(client.post.call_args.kwargs["json"], {"mediaType": "tv", "mediaId": 12, "seasons": "all"})

    def test_marks_duplicate_request(self) -> None:
        client = Mock()
        client.post.return_value = self.response(409, {})
        result = bridge.create_seerr_request(client, self.config(), {"mediaType": "movie", "tmdbId": 12})
        self.assertEqual(result, ("already_requested", None, None))


if __name__ == "__main__":
    unittest.main()
