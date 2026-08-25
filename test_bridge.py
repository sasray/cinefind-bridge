import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# The bridge container installs httpx. A minimal test stub keeps the pure
# configuration and request-payload tests runnable on stripped-down host
# Python installations as well; CI installs the real dependency first.
try:
    import httpx as _httpx  # noqa: F401
except ModuleNotFoundError:
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

    def test_normalises_direct_arr_api_urls(self) -> None:
        self.assertEqual(bridge.clean_arr_url("http://radarr:7878/api/v3/", "Radarr"), "http://radarr:7878")
        self.assertEqual(bridge.clean_arr_url("https://media.test/sonarr/api/v3", "Sonarr"), "https://media.test/sonarr")

    def test_rejects_malformed_service_urls(self) -> None:
        with self.assertRaises(bridge.BridgeError):
            bridge.clean_arr_url("http://[broken", "Radarr")
        with self.assertRaises(bridge.BridgeError):
            bridge.clean_arr_url("http://radarr:not-a-port", "Radarr")

    def test_allows_direct_arr_without_seerr(self) -> None:
        with patch.dict(os.environ, {
            "RADARR_URL": "http://radarr:7878/api/v3",
            "RADARR_API_KEY": "radarr-key",
        }, clear=True):
            configuration = bridge.config_from_environment()
        self.assertEqual(configuration["seerr_url"], "")
        self.assertEqual(configuration["radarr_url"], "http://radarr:7878")
        self.assertEqual(bridge.configured_services(configuration), ["radarr"])

    def test_rejects_half_configured_service(self) -> None:
        with patch.dict(os.environ, {"SONARR_URL": "http://sonarr:8989"}, clear=True):
            with self.assertRaisesRegex(bridge.BridgeError, "SONARR_URL and SONARR_API_KEY"):
                bridge.config_from_environment()

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
            "movie": [{"target": "seerr", "serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}],
            "tv": [{"target": "seerr", "serverId": 3, "profileId": 12, "name": "WEB-1080p", "serverName": "Series"}],
        })
        self.assertEqual(client.get.call_args_list[0].args[0], "http://seerr/api/v1/service/radarr")
        self.assertEqual(client.get.call_args_list[1].args[0], "http://seerr/api/v1/service/radarr/2")
        self.assertEqual(client.get.call_args_list[2].args[0], "http://seerr/api/v1/service/sonarr")
        self.assertEqual(client.get.call_args_list[3].args[0], "http://seerr/api/v1/service/sonarr/3")

    def test_falls_back_to_full_local_arr_service_when_seerr_profile_api_fails(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{"id": 2, "name": "Movies"}]),
            self.response(500, {}),
            self.response(500, {}),
            self.response(200, {"id": 2, "name": "Movies", "hostname": "radarr", "port": 7878, "apiKey": "local-key"}),
            self.response(200, [{"id": 11, "name": "HD-1080p"}]),
            self.response(200, []),
            self.response(200, []),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog["movie"], [{"target": "seerr", "serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}])
        self.assertEqual(catalog["tv"], [])
        self.assertEqual(client.get.call_args_list[1].args[0], "http://seerr/api/v1/service/radarr/2")
        self.assertEqual(client.get.call_args_list[3].args[0], "http://seerr/api/v1/settings/radarr/2")
        self.assertEqual(client.get.call_args_list[4].args[0], "http://radarr:7878/api/v3/qualityprofile")
        self.assertEqual(client.get.call_args_list[4].kwargs["headers"]["X-Api-Key"], "local-key")

    def test_accepts_single_service_object_and_string_ids(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, {"id": "2", "name": "Movies", "hostname": "radarr", "port": 7878, "api_key": "local-key"}),
            self.response(500, {}),
            self.response(500, {}),
            self.response(200, [{"id": "11", "name": "HD-1080p"}]),
            self.response(200, []),
            self.response(200, []),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog["movie"], [{"target": "seerr", "serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}])
        self.assertEqual(catalog["tv"], [])
        self.assertEqual(client.get.call_args_list[1].args[0], "http://seerr/api/v1/service/radarr/2")
        self.assertEqual(client.get.call_args_list[3].args[0], "http://radarr:7878/api/v3/qualityprofile")

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
            "movie": [{"target": "seerr", "serverId": 2, "profileId": 11, "name": "HD-1080p", "serverName": "Movies"}],
            "tv": [{"target": "seerr", "serverId": 3, "profileId": 12, "name": "WEB-1080p", "serverName": "Series"}],
        })


class DirectArrRequestTests(unittest.TestCase):
    def config(self) -> dict[str, str]:
        return {
            "seerr_url": "",
            "seerr_api_key": "",
            "radarr_url": "http://radarr:7878",
            "radarr_api_key": "radarr-key",
            "sonarr_url": "http://sonarr:8989",
            "sonarr_api_key": "sonarr-key",
        }

    def response(self, status: int, body):
        class Response:
            status_code = status
            is_success = 200 <= status < 300
            text = json.dumps(body)

            @staticmethod
            def json():
                return body

        return Response()

    def test_catalog_combines_quality_and_redacted_root_choices(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{"id": 7, "name": "HD-1080p"}]),
            self.response(200, [{"id": 3, "path": "/secret/media/Movies"}]),
            self.response(200, [{"id": 9, "name": "WEB-1080p"}]),
            self.response(200, [{"id": 4, "path": "/secret/media/Series"}]),
        ]

        catalog = bridge.profile_catalog(client, self.config())

        self.assertEqual(catalog, {
            "movie": [{
                "target": "radarr", "profileId": 7, "name": "HD-1080p", "serverName": "Radarr",
                "rootFolderId": 3, "rootFolderName": "Movies",
            }],
            "tv": [{
                "target": "sonarr", "profileId": 9, "name": "WEB-1080p", "serverName": "Sonarr",
                "rootFolderId": 4, "rootFolderName": "Series",
            }],
        })
        self.assertNotIn("/secret", json.dumps(catalog))
        self.assertNotIn("radarr-key", json.dumps(catalog))

    def test_publishes_services_without_local_credentials_or_paths(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{"id": 7, "name": "HD-1080p"}]),
            self.response(200, [{"id": 3, "path": "/private/Movies"}]),
            self.response(200, [{"id": 9, "name": "WEB-1080p"}]),
            self.response(200, [{"id": 4, "path": "/private/Series"}]),
        ]
        client.post.return_value = self.response(200, {"ok": True})

        bridge.publish_profile_catalog(client, {
            "endpoint": "https://cinefind.test/bridge",
            "device_id": "device-id",
            "device_token": "device-token",
        }, self.config())

        published = client.post.call_args.kwargs["json"]
        self.assertEqual(published["services"], ["radarr", "sonarr"])
        encoded = json.dumps(published)
        self.assertNotIn("/private/", encoded)
        self.assertNotIn("radarr-key", encoded)
        self.assertNotIn("sonarr-key", encoded)

    def test_radarr_duplicate_check_avoids_lookup_and_post(self) -> None:
        client = Mock()
        client.get.return_value = self.response(200, [{"id": 41, "tmdbId": 603}])

        result = bridge.create_radarr_request(client, self.config(), {
            "mediaType": "movie", "tmdbId": 603,
        })

        self.assertEqual(result, ("already_requested", 41, None))
        client.get.assert_called_once()
        client.post.assert_not_called()

    def test_adds_radarr_movie_with_local_profile_and_root_path(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, []),
            self.response(200, {"tmdbId": 603, "title": "The Matrix", "titleSlug": "the-matrix"}),
            self.response(200, [{"id": 7, "name": "HD-1080p"}]),
            self.response(200, [{"id": 3, "path": "/media/movies"}]),
        ]
        client.post.return_value = self.response(201, {"id": 42})

        result = bridge.dispatch_request(client, self.config(), {
            "requestTarget": "radarr",
            "mediaType": "movie",
            "tmdbId": 603,
            "requestOptions": {"target": "radarr", "profileId": 7, "rootFolderId": 3},
        })

        self.assertEqual(result, ("requested", 42, None))
        self.assertEqual(client.get.call_args_list[0].kwargs["params"], {"tmdbId": 603})
        self.assertEqual(client.get.call_args_list[1].args[0], "http://radarr:7878/api/v3/movie/lookup/tmdb")
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["qualityProfileId"], 7)
        self.assertEqual(payload["rootFolderPath"], "/media/movies")
        self.assertEqual(payload["addOptions"], {"monitor": "movieOnly", "searchForMovie": True})

    def test_adds_sonarr_series_using_tmdb_lookup_and_preserves_series_type(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{
                "tmdbId": 1399, "tvdbId": 121361, "title": "Game of Thrones",
                "titleSlug": "game-of-thrones", "seriesType": "standard",
            }]),
            self.response(200, []),
            self.response(200, [{"id": 9, "name": "WEB-1080p"}]),
            self.response(200, [{"id": 4, "path": "/media/series"}]),
            self.response(200, [{"id": 1, "name": "English"}]),
        ]
        client.post.return_value = self.response(201, {"id": 84})

        result = bridge.dispatch_request(client, self.config(), {
            "requestTarget": "sonarr",
            "mediaType": "tv",
            "tmdbId": 1399,
            "requestOptions": {"target": "sonarr", "profileId": 9, "rootFolderId": 4},
        })

        self.assertEqual(result, ("requested", 84, None))
        self.assertEqual(client.get.call_args_list[0].kwargs["params"], {"term": "tmdb:1399"})
        self.assertEqual(client.get.call_args_list[1].kwargs["params"], {"tvdbId": 121361})
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["tvdbId"], 121361)
        self.assertEqual(payload["seriesType"], "standard")
        self.assertEqual(payload["rootFolderPath"], "/media/series")
        self.assertEqual(payload["languageProfileId"], 1)
        self.assertEqual(payload["monitorNewItems"], "all")
        self.assertEqual(payload["addOptions"], {"monitor": "all", "searchForMissingEpisodes": True})

    def test_sonarr_duplicate_check_avoids_profile_and_post_calls(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{"tmdbId": 1399, "tvdbId": 121361, "title": "Game of Thrones"}]),
            self.response(200, [{"id": 81, "tvdbId": 121361}]),
        ]

        result = bridge.create_sonarr_request(client, self.config(), {"mediaType": "tv", "tmdbId": 1399})

        self.assertEqual(result, ("already_requested", 81, None))
        self.assertEqual(client.get.call_count, 2)
        client.post.assert_not_called()

    def test_sonarr_v4_omits_removed_language_profile(self) -> None:
        client = Mock()
        client.get.side_effect = [
            self.response(200, [{
                "tmdbId": 1399, "tvdbId": 121361, "title": "Game of Thrones",
                "titleSlug": "game-of-thrones", "seriesType": "standard",
            }]),
            self.response(200, []),
            self.response(200, [{"id": 9, "name": "WEB-1080p"}]),
            self.response(200, [{"id": 4, "path": "/media/series"}]),
            self.response(404, {"message": "Not found"}),
        ]
        client.post.return_value = self.response(201, {"id": 84})

        result = bridge.create_sonarr_request(client, self.config(), {
            "mediaType": "tv", "tmdbId": 1399,
            "requestOptions": {"target": "sonarr", "profileId": 9, "rootFolderId": 4},
        })

        self.assertEqual(result, ("requested", 84, None))
        self.assertNotIn("languageProfileId", client.post.call_args.kwargs["json"])

    def test_rejects_unknown_or_mismatched_targets_without_network_calls(self) -> None:
        client = Mock()
        unknown = bridge.dispatch_request(client, self.config(), {
            "requestTarget": "http://attacker.test", "mediaType": "movie", "tmdbId": 1,
        })
        mismatch = bridge.dispatch_request(client, self.config(), {
            "requestTarget": "radarr", "mediaType": "tv", "tmdbId": 1,
        })
        option_mismatch = bridge.dispatch_request(client, self.config(), {
            "requestTarget": "radarr", "mediaType": "movie", "tmdbId": 1,
            "requestOptions": {"target": "sonarr", "profileId": 2, "rootFolderId": 3},
        })

        self.assertEqual(unknown[0], "failed")
        self.assertEqual(mismatch[0], "failed")
        self.assertEqual(option_mismatch[0], "failed")
        client.get.assert_not_called()
        client.post.assert_not_called()

    def test_missing_target_defaults_to_legacy_seerr(self) -> None:
        configuration = self.config()
        configuration.update({"seerr_url": "http://seerr", "seerr_api_key": "seerr-key"})
        client = Mock()
        client.post.return_value = self.response(201, {"id": 6})

        result = bridge.dispatch_request(client, configuration, {"mediaType": "movie", "tmdbId": 12})

        self.assertEqual(result, ("requested", 6, None))
        self.assertEqual(client.post.call_args.args[0], "http://seerr/api/v1/request")

    def test_local_network_errors_are_bounded_and_do_not_leak_url_or_key(self) -> None:
        client = Mock()
        client.get.side_effect = bridge.httpx.HTTPError("http://radarr:7878?apikey=radarr-key")

        result = bridge.create_radarr_request(client, self.config(), {"mediaType": "movie", "tmdbId": 603})

        self.assertEqual(result, ("failed", None, "Local Radarr could not be reached."))
        self.assertNotIn("radarr-key", result[2])


if __name__ == "__main__":
    unittest.main()
