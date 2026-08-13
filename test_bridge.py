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

    def test_passes_selected_quality_profile_to_seerr(self) -> None:
        client = Mock()
        client.post.return_value = self.response(201, {"id": 42})
        result = bridge.create_seerr_request(client, self.config(), {
            "mediaType": "tv",
            "tmdbId": 12,
            "requestOptions": {"serverId": 4, "profileId": 7},
        })
        self.assertEqual(result, ("requested", 42, None))
        self.assertEqual(client.post.call_args.kwargs["json"], {
            "mediaType": "tv", "mediaId": 12, "seasons": "all", "serverId": 4, "profileId": 7,
        })

    def test_reads_profiles_from_each_configured_seerr_server(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{"id": 2, "name": "Movies"}]),
            self.response(200, [{"id": 11, "name": "HD-1080p"}]),
            self.response(200, [{"id": 3, "name": "Series"}]),
            self.response(200, [{"id": 12, "name": "WEB-1080p"}]),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog, {
            "movie": [{"serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}],
            "tv": [{"serverId": 3, "profileId": 12, "name": "WEB-1080p", "serverName": "Series"}],
        })
        self.assertEqual(client.get.call_args_list[1].args[0], "http://seerr/api/v1/settings/radarr/2/profiles")
        self.assertEqual(client.get.call_args_list[3].args[0], "http://seerr/api/v1/settings/sonarr/3/profiles")

    def test_falls_back_to_full_local_arr_service_when_seerr_profile_api_fails(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{"id": 2, "name": "Movies"}]),
            self.response(500, {}),
            self.response(200, {"id": 2, "name": "Movies", "hostname": "radarr", "port": 7878, "apiKey": "local-key"}),
            self.response(200, [{"id": 11, "name": "HD-1080p"}]),
            self.response(200, []),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog["movie"], [{"serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}])
        self.assertEqual(catalog["tv"], [])
        self.assertEqual(client.get.call_args_list[2].args[0], "http://seerr/api/v1/settings/radarr/2")
        self.assertEqual(client.get.call_args_list[3].args[0], "http://radarr:7878/api/v3/qualityprofile")
        self.assertEqual(client.get.call_args_list[3].kwargs["headers"]["X-Api-Key"], "local-key")

    def test_accepts_single_service_object_and_string_ids(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, {"id": "2", "name": "Movies", "hostname": "radarr", "port": 7878, "api_key": "local-key"}),
            self.response(500, {}),
            self.response(200, [{"id": "11", "name": "HD-1080p"}]),
            self.response(200, []),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog["movie"], [{"serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}])
        self.assertEqual(catalog["tv"], [])
        self.assertEqual(client.get.call_args_list[1].args[0], "http://seerr/api/v1/settings/radarr/2/profiles")
        self.assertEqual(client.get.call_args_list[2].args[0], "http://radarr:7878/api/v3/qualityprofile")

    def test_accepts_keyed_service_and_profile_objects(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, {"radarr": {"id": "2", "name": "Movies"}}),
            self.response(200, {"qualityProfiles": {"id": "11", "name": "HD-1080p"}}),
            self.response(200, {"sonarr": {"id": "3", "name": "Series"}}),
            self.response(200, {"profiles": [{"id": "12", "name": "WEB-1080p"}]}),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog, {
            "movie": [{"serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}],
            "tv": [{"serverId": 3, "profileId": 12, "name": "WEB-1080p", "serverName": "Series"}],
        })


if __name__ == "__main__":
    unittest.main()
