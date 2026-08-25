"""CineFind Bridge: a local Docker media-request relay.

The bridge only makes outbound HTTPS calls to CineFind. Its Seerr, Radarr and
Sonarr URLs and API keys are read locally and never leave this container.
CineFind sees only a revocable device token, a queued TMDB request and a
redacted catalog of selectable profile/root-folder IDs and friendly names.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_ENDPOINT = "https://ilztuuhkhzzdjsqliomk.supabase.co/functions/v1/seerr-bridge"
CONFIG_PATH = Path(os.environ.get("CINEFIND_BRIDGE_CONFIG_PATH", "/data/config.json"))
POLL_INTERVAL = max(6, min(int(os.environ.get("CINEFIND_BRIDGE_POLL_INTERVAL", "12")), 300))
HEALTH_PORT = int(os.environ.get("PORT", "8080"))
STOP = threading.Event()
LAST_SUCCESS: float | None = None
LAST_ERROR: str | None = None
LOCAL_REQUEST_TIMEOUT = 20.0
REMOTE_REQUEST_TIMEOUT = 20.0
SERVICE_TARGETS = ("seerr", "radarr", "sonarr")


class BridgeError(RuntimeError):
    """A stable error suitable for logs and a job completion response."""


def clean_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError as error:
        raise BridgeError("CineFind endpoint is not a valid URL.") from error
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BridgeError("CineFind endpoint must use HTTPS.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def clean_local_url(value: str, service_name: str, api_suffix: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError as error:
        raise BridgeError(f"{service_name} URL is not valid.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise BridgeError(f"{service_name} URL must start with http:// or https://.")
    if parsed.query or parsed.fragment:
        raise BridgeError(f"{service_name} URL must not include a query or fragment.")
    base_path = parsed.path.rstrip("/")
    if base_path.lower().endswith(api_suffix):
        base_path = base_path[:-len(api_suffix)]
    return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))


def clean_seerr_url(value: str) -> str:
    return clean_local_url(value, "Seerr", "/api/v1")


def clean_arr_url(value: str, service_name: str) -> str:
    return clean_local_url(value, service_name, "/api/v3")


def optional_service_url(environment_name: str, service_name: str, api_suffix: str) -> str:
    value = os.environ.get(environment_name, "").strip()
    if not value:
        return ""
    return clean_local_url(value, service_name, api_suffix)


def config_from_environment() -> dict[str, str]:
    configuration = {
        "endpoint": clean_endpoint(os.environ.get("CINEFIND_BRIDGE_API_URL", DEFAULT_ENDPOINT)),
        "seerr_url": optional_service_url("SEERR_URL", "Seerr", "/api/v1"),
        "seerr_api_key": os.environ.get("SEERR_API_KEY", "").strip(),
        "radarr_url": optional_service_url("RADARR_URL", "Radarr", "/api/v3"),
        "radarr_api_key": os.environ.get("RADARR_API_KEY", "").strip(),
        "sonarr_url": optional_service_url("SONARR_URL", "Sonarr", "/api/v3"),
        "sonarr_api_key": os.environ.get("SONARR_API_KEY", "").strip(),
        "pairing_code": os.environ.get("CINEFIND_PAIRING_CODE", "").strip(),
        "display_name": os.environ.get("CINEFIND_BRIDGE_NAME", f"Docker ({socket.gethostname()})").strip()[:80],
    }
    for target in SERVICE_TARGETS:
        if bool(configuration[f"{target}_url"]) != bool(configuration[f"{target}_api_key"]):
            raise BridgeError(f"Set both {target.upper()}_URL and {target.upper()}_API_KEY, or leave both empty.")
    return configuration


def configured_services(configuration: dict[str, str]) -> list[str]:
    """Return only services with a complete, local credential pair."""
    return [
        target for target in SERVICE_TARGETS
        if bool(configuration.get(f"{target}_url")) and bool(configuration.get(f"{target}_api_key"))
    ]


def load_paired_device() -> dict[str, str] | None:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError("Bridge configuration cannot be read.") from error
    if not isinstance(config, dict) or not all(isinstance(config.get(key), str) for key in ("device_id", "device_token", "endpoint")):
        raise BridgeError("Bridge configuration is invalid. Pair this device again.")
    return {"device_id": config["device_id"], "device_token": config["device_token"], "endpoint": clean_endpoint(config["endpoint"])}


def save_paired_device(endpoint: str, device_id: str, device_token: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "device_id": device_id,
        "device_token": device_token,
        "endpoint": endpoint,
    }, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)


def call_cinefind(
    client: httpx.Client,
    endpoint: str,
    payload: dict[str, Any],
    token: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "CineFind-Bridge/1.0"}
    if token:
        headers["X-CineFind-Bridge-Token"] = token
    try:
        response = client.post(endpoint, json=payload, headers=headers, timeout=REMOTE_REQUEST_TIMEOUT)
    except httpx.HTTPError as error:
        raise BridgeError("CineFind could not be reached.") from error
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.is_success:
        message = body.get("error") if isinstance(body, dict) else None
        raise BridgeError(str(message or f"CineFind returned {response.status_code}.")[:500])
    if not isinstance(body, dict):
        raise BridgeError("CineFind returned an invalid response.")
    return body


def ensure_paired(client: httpx.Client, configuration: dict[str, str]) -> dict[str, str]:
    paired = load_paired_device()
    if paired:
        return paired
    if not configuration["pairing_code"]:
        raise BridgeError("Set CINEFIND_PAIRING_CODE in the Bridge configuration, then restart it.")
    if not configured_services(configuration):
        raise BridgeError("Configure at least one local Seerr, Radarr or Sonarr service.")
    response = call_cinefind(client, configuration["endpoint"], {
        "action": "pair",
        "pairingCode": configuration["pairing_code"],
        "displayName": configuration["display_name"] or "Docker",
    })
    device_id = response.get("deviceId")
    device_token = response.get("deviceToken")
    if not isinstance(device_id, str) or not isinstance(device_token, str) or len(device_token) < 32:
        raise BridgeError("CineFind returned an invalid pairing response.")
    save_paired_device(configuration["endpoint"], device_id, device_token)
    logging.info("Paired as %s", response.get("displayName", "Docker"))
    return {"device_id": device_id, "device_token": device_token, "endpoint": configuration["endpoint"]}


def direct_arr_profiles(client: httpx.Client, server: dict[str, Any]) -> list[dict[str, Any]]:
    """Return profiles from a configured Radarr/Sonarr instance as a fallback.

    Some Seerr releases expose their service records normally but fail their
    per-service profile endpoint. The service API key is already held by Seerr
    and is used here only inside the user's local network; neither it nor the
    server address is ever sent to CineFind.
    """
    api_key = server.get("apiKey") or server.get("api_key")
    hostname = str(server.get("hostname") or server.get("host") or "").strip()
    port = server.get("port")
    if not isinstance(api_key, str) or not api_key.strip() or not hostname:
        return []
    if hostname.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(hostname)
            parsed.port
        except ValueError:
            return []
        if not parsed.hostname or parsed.username or parsed.password:
            return []
        scheme = parsed.scheme
        netloc = parsed.netloc
        base_path = parsed.path.rstrip("/")
    else:
        scheme = "https" if server.get("useSsl") or server.get("ssl") else "http"
        netloc = hostname if not isinstance(port, (int, str)) or not str(port).strip() else f"{hostname}:{str(port).strip()}"
        base_path = ""
    configured_base = str(server.get("baseUrl") or server.get("urlBase") or "").strip()
    if configured_base:
        base_path = f"{base_path}/{configured_base.lstrip('/')}".rstrip("/")
    service_url = urlunsplit((scheme, netloc, base_path, "", ""))
    try:
        response = client.get(f"{service_url}/api/v3/qualityprofile", headers={
            "X-Api-Key": api_key.strip(),
            "Accept": "application/json",
            "User-Agent": "CineFind-Bridge/1.2",
        }, timeout=LOCAL_REQUEST_TIMEOUT)
        profiles = response.json() if response.is_success else []
    except (httpx.HTTPError, ValueError):
        return []
    return profiles if isinstance(profiles, list) else []


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        result = int(value)
        return result if result > 0 else None
    return None


def service_list(value: Any, service_key: str) -> list[dict[str, Any]]:
    """Normalise the service-list variants returned by Seerr-compatible APIs."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []

    # The documented API returns an array, while some compatible deployments
    # wrap one configured service in either its name or `results`.
    nested = value.get(service_key, value.get("results", value.get("data")))
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return [nested]
    return [value] if "id" in value else []


def profile_list(value: Any) -> list[dict[str, Any]]:
    """Normalise profile endpoint response variants without trusting extras."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    nested = value.get("profiles", value.get("qualityProfiles", value.get("results", value.get("data"))))
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return [nested]
    return []


def seerr_profile_catalog(client: httpx.Client, configuration: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Read the actual request profiles available in the user's Seerr.

    The Bridge sends only names and numeric IDs to CineFind. The Seerr URL and
    API key stay local, exactly like when a title is requested.
    """
    if "seerr" not in configured_services(configuration):
        return {"movie": [], "tv": []}
    headers = {
        "X-Api-Key": configuration["seerr_api_key"],
        "Accept": "application/json",
        "User-Agent": "CineFind-Bridge/1.1",
    }
    catalog: dict[str, list[dict[str, Any]]] = {"movie": [], "tv": []}

    # `/service` is deliberately used here instead of `/settings`: Seerr
    # permits ordinary request users to inspect the service and its profiles,
    # while `/settings` is restricted to Seerr administrators. The response
    # omits every private Arr setting and API key.
    for media_type, server_key in (("movie", "radarr"), ("tv", "sonarr")):
        try:
            response = client.get(
                f"{configuration['seerr_url']}/api/v1/service/{server_key}",
                headers=headers,
                timeout=LOCAL_REQUEST_TIMEOUT,
            )
            raw_servers = response.json() if response.is_success else []
        except (httpx.HTTPError, ValueError):
            raw_servers = []
        raw_servers = service_list(raw_servers, server_key)

        # Keep a narrow compatibility fallback for older Seerr builds. A
        # standard user key never needs this branch on current Seerr.
        if not raw_servers:
            try:
                response = client.get(
                    f"{configuration['seerr_url']}/api/v1/settings/{server_key}",
                    headers=headers,
                    timeout=LOCAL_REQUEST_TIMEOUT,
                )
                raw_servers = service_list(response.json() if response.is_success else [], server_key)
            except (httpx.HTTPError, ValueError):
                logging.warning("Could not load configured Seerr %s servers.", server_key)
                continue

        for server in raw_servers[:20]:
            if not isinstance(server, dict):
                continue
            server_id = positive_int(server.get("id"))
            if server_id is None:
                continue
            server_name = str(server.get("name") or ("Radarr" if media_type == "movie" else "Sonarr")).strip()[:120]
            try:
                response = client.get(
                    f"{configuration['seerr_url']}/api/v1/service/{server_key}/{server_id}",
                    headers=headers,
                    timeout=LOCAL_REQUEST_TIMEOUT,
                )
                raw_profiles = response.json() if response.is_success else []
            except (httpx.HTTPError, ValueError):
                logging.warning("Could not load Seerr profiles for %s server %s.", server_key, server_id)
                raw_profiles = []
            raw_profiles = profile_list(raw_profiles)

            # Older Seerr releases only exposed a Radarr profile endpoint in
            # the administrator-only settings API. Use it only as a fallback;
            # current Seerr returns both Radarr and Sonarr profiles above.
            if not raw_profiles:
                try:
                    response = client.get(
                        f"{configuration['seerr_url']}/api/v1/settings/{server_key}/{server_id}/profiles",
                        headers=headers,
                        timeout=LOCAL_REQUEST_TIMEOUT,
                    )
                    raw_profiles = profile_list(response.json() if response.is_success else [])
                except (httpx.HTTPError, ValueError):
                    raw_profiles = []
            # Seerr's profile endpoint has a known compatibility problem on
            # some self-hosted versions. In that case use the already trusted
            # local Radarr/Sonarr configuration as a strictly local fallback.
            if not raw_profiles:
                raw_profiles = direct_arr_profiles(client, server)
            # The service-list API can intentionally omit its API key. Fetch
            # the individual local service configuration only when needed;
            # this stays inside the home network and is never uploaded.
            if not raw_profiles:
                try:
                    response = client.get(
                        f"{configuration['seerr_url']}/api/v1/settings/{server_key}/{server_id}",
                        headers=headers,
                        timeout=LOCAL_REQUEST_TIMEOUT,
                    )
                    full_server = response.json() if response.is_success else None
                except (httpx.HTTPError, ValueError):
                    full_server = None
                for full_service in service_list(full_server, server_key)[:1]:
                    raw_profiles = direct_arr_profiles(client, full_service)
                    if raw_profiles:
                        break
            for profile in raw_profiles[:40]:
                if not isinstance(profile, dict):
                    continue
                profile_id = positive_int(profile.get("id"))
                profile_name = str(profile.get("name") or "").strip()[:120]
                if profile_id is not None and profile_name:
                    catalog[media_type].append({
                        "target": "seerr",
                        "serverId": server_id,
                        "profileId": profile_id,
                        "name": profile_name,
                        "serverName": server_name,
                    })
    return catalog


def arr_headers(configuration: dict[str, str], target: str) -> dict[str, str]:
    return {
        "X-Api-Key": configuration[f"{target}_api_key"],
        "Accept": "application/json",
        "User-Agent": "CineFind-Bridge/2.0",
    }


def dictionary_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def load_arr_choices(
    client: httpx.Client,
    configuration: dict[str, str],
    target: str,
    *,
    log_failures: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch quality profiles and root folders without exposing either URL."""
    service_name = target.title()
    base_url = configuration[f"{target}_url"]
    values: list[list[dict[str, Any]]] = []
    for resource in ("qualityprofile", "rootfolder"):
        try:
            response = client.get(
                f"{base_url}/api/v3/{resource}",
                headers=arr_headers(configuration, target),
                timeout=LOCAL_REQUEST_TIMEOUT,
            )
            if not response.is_success:
                if log_failures:
                    logging.warning("%s %s catalog returned %s.", service_name, resource, response.status_code)
                values.append([])
                continue
            values.append(dictionary_list(response.json()))
        except (httpx.HTTPError, ValueError):
            if log_failures:
                logging.warning("Could not load local %s %s catalog.", service_name, resource)
            values.append([])
    return values[0], values[1]


def friendly_root_name(root: dict[str, Any], root_id: int) -> str:
    """Return a non-sensitive root label; the full filesystem path stays local."""
    configured_name = root.get("name")
    if isinstance(configured_name, str) and configured_name.strip():
        name = configured_name.strip().rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if name:
            return name[:120]
    path = root.get("path")
    if isinstance(path, str):
        basename = path.strip().rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if basename:
            return basename[:120]
    return f"Root folder {root_id}"


def direct_arr_catalog(
    client: httpx.Client,
    configuration: dict[str, str],
    target: str,
) -> list[dict[str, Any]]:
    """Build a cloud-safe catalog for one directly configured Arr service."""
    profiles, roots = load_arr_choices(client, configuration, target)
    clean_profiles: list[tuple[int, str]] = []
    clean_roots: list[tuple[int, str]] = []
    for profile in profiles[:40]:
        profile_id = positive_int(profile.get("id"))
        name = str(profile.get("name") or "").strip()[:120]
        if profile_id is not None and name:
            clean_profiles.append((profile_id, name))
    for root in roots[:20]:
        root_id = positive_int(root.get("id"))
        if root_id is not None and root.get("accessible") is not False:
            clean_roots.append((root_id, friendly_root_name(root, root_id)))

    entries: list[dict[str, Any]] = []
    for profile_id, profile_name in clean_profiles:
        choices: list[tuple[int | None, str | None]] = clean_roots or [(None, None)]
        for root_id, root_name in choices:
            entry: dict[str, Any] = {
                "target": target,
                "profileId": profile_id,
                "name": profile_name,
                "serverName": target.title(),
            }
            if root_id is not None and root_name is not None:
                entry["rootFolderId"] = root_id
                entry["rootFolderName"] = root_name
            entries.append(entry)
    return entries


def merge_catalog_entries(
    seerr_entries: list[dict[str, Any]],
    direct_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep both targets visible within the cloud's 48-entry media limit."""
    if seerr_entries and direct_entries:
        return seerr_entries[:24] + direct_entries[:24]
    return (seerr_entries or direct_entries)[:48]


def profile_catalog(client: httpx.Client, configuration: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Combine Seerr and direct Arr choices in the legacy movie/TV shape."""
    catalog = seerr_profile_catalog(client, configuration)
    if "radarr" in configured_services(configuration):
        direct = direct_arr_catalog(client, configuration, "radarr")
        catalog["movie"] = merge_catalog_entries(catalog["movie"], direct)
    if "sonarr" in configured_services(configuration):
        direct = direct_arr_catalog(client, configuration, "sonarr")
        catalog["tv"] = merge_catalog_entries(catalog["tv"], direct)
    return catalog


def publish_profile_catalog(
    client: httpx.Client,
    paired: dict[str, str],
    configuration: dict[str, str],
) -> None:
    call_cinefind(client, paired["endpoint"], {
        "action": "profile_catalog",
        "deviceId": paired["device_id"],
        "profiles": profile_catalog(client, configuration),
        "services": configured_services(configuration),
    }, paired["device_token"])


def create_seerr_request(client: httpx.Client, configuration: dict[str, str], job: dict[str, Any]) -> tuple[str, int | None, str | None]:
    media_type = job.get("mediaType")
    tmdb_id = job.get("tmdbId")
    if media_type not in {"movie", "tv"} or not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id < 1:
        return "failed", None, "CineFind sent an invalid media job."
    payload: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
    if media_type == "tv":
        payload["seasons"] = "all"
    options = job.get("requestOptions")
    if isinstance(options, dict):
        option_target = options.get("target")
        if option_target is not None and option_target != "seerr":
            return "failed", None, "CineFind sent request options for the wrong service."
        raw_server_id = options.get("serverId")
        raw_profile_id = options.get("profileId")
        server_id = positive_int(raw_server_id)
        profile_id = positive_int(raw_profile_id)
        if (raw_server_id is not None or raw_profile_id is not None) and (server_id is None or profile_id is None):
            return "failed", None, "CineFind sent invalid Seerr request options."
        if server_id is not None and profile_id is not None:
            payload["serverId"] = server_id
            payload["profileId"] = profile_id
    headers = {
        "X-Api-Key": configuration["seerr_api_key"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "CineFind-Bridge/1.0",
    }
    try:
        response = client.post(
            f"{configuration['seerr_url']}/api/v1/request",
            json=payload,
            headers=headers,
            timeout=LOCAL_REQUEST_TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
    except httpx.HTTPError:
        return "failed", None, "Local Seerr could not be reached."
    request_id = positive_int(body.get("id")) if isinstance(body, dict) else None
    if response.status_code == 409:
        return "already_requested", request_id, None
    if not response.is_success:
        return "failed", None, f"Seerr returned {response.status_code}."
    return "requested", request_id, None


def local_json_get(
    client: httpx.Client,
    configuration: dict[str, str],
    target: str,
    resource: str,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[Any | None, Any | None, str | None]:
    """Issue a bounded local GET and return a response/body/error triple."""
    service_name = target.title()
    base_url = configuration[f"{target}_url"]
    try:
        response = client.get(
            f"{base_url}/api/v3/{resource}",
            params=params,
            headers=arr_headers(configuration, target),
            timeout=LOCAL_REQUEST_TIMEOUT,
        )
    except httpx.HTTPError:
        return None, None, f"Local {service_name} could not be reached."
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.is_success:
        return response, body, f"{service_name} returned {response.status_code}."
    if body is None:
        return response, None, f"{service_name} returned an invalid response."
    return response, body, None


def local_json_post(
    client: httpx.Client,
    configuration: dict[str, str],
    target: str,
    resource: str,
    payload: dict[str, Any],
) -> tuple[Any | None, Any | None, str | None]:
    """Issue a bounded local POST without putting credentials in an error."""
    service_name = target.title()
    base_url = configuration[f"{target}_url"]
    headers = {**arr_headers(configuration, target), "Content-Type": "application/json"}
    try:
        response = client.post(
            f"{base_url}/api/v3/{resource}",
            json=payload,
            headers=headers,
            timeout=LOCAL_REQUEST_TIMEOUT,
        )
    except httpx.HTTPError:
        return None, None, f"Local {service_name} could not be reached."
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.is_success:
        return response, body, f"{service_name} returned {response.status_code}."
    if body is None:
        return response, None, f"{service_name} returned an invalid response."
    return response, body, None


def matching_record(value: Any, key: str, expected: int) -> dict[str, Any] | None:
    for record in dictionary_list(value):
        if positive_int(record.get(key)) == expected:
            return record
    return None


def response_reports_duplicate(response: Any, body: Any) -> bool:
    if response is not None and response.status_code == 409:
        return True
    if response is None or response.status_code != 400:
        return False
    try:
        message = json.dumps(body, ensure_ascii=True).lower()
    except (TypeError, ValueError):
        return False
    return "already" in message and ("exist" in message or "added" in message)


def resolve_arr_options(
    client: httpx.Client,
    configuration: dict[str, str],
    target: str,
    options: Any,
) -> tuple[int | None, str | None, str | None]:
    """Resolve cloud-safe IDs to the path held only by the local Arr server."""
    service_name = target.title()
    if options is not None and not isinstance(options, dict):
        return None, None, f"CineFind sent invalid {service_name} request options."
    raw_options = options if isinstance(options, dict) else {}
    option_target = raw_options.get("target")
    if option_target is not None and option_target != target:
        return None, None, "CineFind sent request options for the wrong service."

    _response, raw_profiles, failure = local_json_get(client, configuration, target, "qualityprofile")
    if failure:
        return None, None, failure
    _response, raw_roots, failure = local_json_get(client, configuration, target, "rootfolder")
    if failure:
        return None, None, failure
    profiles = dictionary_list(raw_profiles)
    roots = dictionary_list(raw_roots)
    available_profiles = {
        profile_id: profile
        for profile in profiles
        if (profile_id := positive_int(profile.get("id"))) is not None
    }
    available_roots = {
        root_id: root
        for root in roots
        if (root_id := positive_int(root.get("id"))) is not None
        and root.get("accessible") is not False
        and isinstance(root.get("path"), str)
        and bool(root["path"].strip())
    }
    if not available_profiles or not available_roots:
        return None, None, f"{service_name} has no available quality profile or root folder."

    raw_profile_id = raw_options.get("profileId")
    profile_id = positive_int(raw_profile_id) if raw_profile_id is not None else next(iter(available_profiles))
    if profile_id is None or profile_id not in available_profiles:
        return None, None, f"The selected {service_name} quality profile is no longer available."
    raw_root_id = raw_options.get("rootFolderId")
    root_id = positive_int(raw_root_id) if raw_root_id is not None else next(iter(available_roots))
    if root_id is None or root_id not in available_roots:
        return None, None, f"The selected {service_name} root folder is no longer available."
    return profile_id, str(available_roots[root_id]["path"]), None


def create_radarr_request(
    client: httpx.Client,
    configuration: dict[str, str],
    job: dict[str, Any],
) -> tuple[str, int | None, str | None]:
    tmdb_id = job.get("tmdbId")
    if job.get("mediaType") != "movie" or not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id < 1:
        return "failed", None, "CineFind sent an invalid Radarr media job."

    _response, existing, failure = local_json_get(
        client, configuration, "radarr", "movie", params={"tmdbId": tmdb_id},
    )
    if failure:
        return "failed", None, failure
    duplicate = matching_record(existing, "tmdbId", tmdb_id)
    if duplicate is not None:
        return "already_requested", positive_int(duplicate.get("id")), None

    _response, lookup, failure = local_json_get(
        client, configuration, "radarr", "movie/lookup/tmdb", params={"tmdbId": tmdb_id},
    )
    if failure:
        return "failed", None, failure
    candidate = matching_record(lookup, "tmdbId", tmdb_id)
    if candidate is None and isinstance(lookup, dict) and positive_int(lookup.get("tmdbId")) == tmdb_id:
        candidate = lookup
    if candidate is None:
        return "failed", None, "Radarr could not find that TMDB movie."

    profile_id, root_path, failure = resolve_arr_options(
        client, configuration, "radarr", job.get("requestOptions"),
    )
    if failure or profile_id is None or root_path is None:
        return "failed", None, failure or "Radarr request options are unavailable."
    payload = dict(candidate)
    payload.pop("id", None)
    payload.update({
        "tmdbId": tmdb_id,
        "qualityProfileId": profile_id,
        "rootFolderPath": root_path,
        "monitored": True,
        "addOptions": {"monitor": "movieOnly", "searchForMovie": True},
    })
    response, body, failure = local_json_post(client, configuration, "radarr", "movie", payload)
    if failure:
        if response_reports_duplicate(response, body):
            return "already_requested", positive_int(body.get("id")) if isinstance(body, dict) else None, None
        return "failed", None, failure
    request_id = positive_int(body.get("id")) if isinstance(body, dict) else None
    return "requested", request_id, None


def create_sonarr_request(
    client: httpx.Client,
    configuration: dict[str, str],
    job: dict[str, Any],
) -> tuple[str, int | None, str | None]:
    tmdb_id = job.get("tmdbId")
    if job.get("mediaType") != "tv" or not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id < 1:
        return "failed", None, "CineFind sent an invalid Sonarr media job."

    _response, lookup, failure = local_json_get(
        client, configuration, "sonarr", "series/lookup", params={"term": f"tmdb:{tmdb_id}"},
    )
    if failure:
        return "failed", None, failure
    candidates = dictionary_list(lookup)
    candidate = matching_record(candidates, "tmdbId", tmdb_id)
    if candidate is None and len(candidates) == 1:
        candidate = candidates[0]
    tvdb_id = positive_int(candidate.get("tvdbId")) if candidate is not None else None
    if candidate is None or tvdb_id is None:
        return "failed", None, "Sonarr could not find that TMDB series."

    _response, existing, failure = local_json_get(
        client, configuration, "sonarr", "series", params={"tvdbId": tvdb_id},
    )
    if failure:
        return "failed", None, failure
    duplicate = matching_record(existing, "tvdbId", tvdb_id)
    if duplicate is not None:
        return "already_requested", positive_int(duplicate.get("id")), None

    profile_id, root_path, failure = resolve_arr_options(
        client, configuration, "sonarr", job.get("requestOptions"),
    )
    if failure or profile_id is None or root_path is None:
        return "failed", None, failure or "Sonarr request options are unavailable."

    # Sonarr v3 requires a configured language profile, while v4 removed that
    # setting. Reuse a valid lookup value when present; otherwise query the v3
    # endpoint and omit the field when v4 answers with 404.
    language_profile_id = positive_int(candidate.get("languageProfileId"))
    if language_profile_id is None:
        language_response, raw_languages, language_failure = local_json_get(
            client, configuration, "sonarr", "languageprofile",
        )
        if language_failure and not (
            language_response is not None and language_response.status_code == 404
        ):
            return "failed", None, language_failure
        if language_failure is None:
            language_profile_id = next((
                profile_id
                for language in dictionary_list(raw_languages)
                if (profile_id := positive_int(language.get("id"))) is not None
            ), None)

    payload = dict(candidate)
    payload.pop("id", None)
    payload.pop("languageProfileId", None)
    payload.update({
        "tvdbId": tvdb_id,
        "qualityProfileId": profile_id,
        "rootFolderPath": root_path,
        "monitored": True,
        "seasonFolder": True,
        "monitorNewItems": "all",
        "addOptions": {"monitor": "all", "searchForMissingEpisodes": True},
    })
    if language_profile_id is not None:
        payload["languageProfileId"] = language_profile_id
    response, body, failure = local_json_post(client, configuration, "sonarr", "series", payload)
    if failure:
        if response_reports_duplicate(response, body):
            return "already_requested", positive_int(body.get("id")) if isinstance(body, dict) else None, None
        return "failed", None, failure
    request_id = positive_int(body.get("id")) if isinstance(body, dict) else None
    return "requested", request_id, None


def dispatch_request(
    client: httpx.Client,
    configuration: dict[str, str],
    job: dict[str, Any],
) -> tuple[str, int | None, str | None]:
    """Route a job through an explicit allowlist; never accept a URL in a job."""
    target = job.get("requestTarget") if "requestTarget" in job else "seerr"
    if target not in SERVICE_TARGETS:
        return "failed", None, "CineFind sent an invalid request target."
    options = job.get("requestOptions")
    if isinstance(options, dict) and options.get("target") is not None and options.get("target") != target:
        return "failed", None, "CineFind sent request options for the wrong service."
    if target not in configured_services(configuration):
        return "failed", None, f"Local {target.title()} is not configured on this bridge."
    media_type = job.get("mediaType")
    if (target == "radarr" and media_type != "movie") or (target == "sonarr" and media_type != "tv"):
        return "failed", None, "CineFind sent a media type that does not match the request target."
    if target == "seerr":
        return create_seerr_request(client, configuration, job)
    if target == "radarr":
        return create_radarr_request(client, configuration, job)
    return create_sonarr_request(client, configuration, job)


def run_worker() -> None:
    global LAST_SUCCESS, LAST_ERROR
    try:
        configuration = config_from_environment()
    except BridgeError as error:
        LAST_ERROR = str(error)
        logging.error("Configuration error: %s", error)
        return
    if not configured_services(configuration):
        LAST_ERROR = "Configure at least one local Seerr, Radarr or Sonarr service."
        logging.error(LAST_ERROR)
        return
    with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False) as client:
        paired: dict[str, str] | None = None
        profiles_published_at = 0.0
        while not STOP.is_set():
            try:
                paired = paired or ensure_paired(client, configuration)
                if time.time() - profiles_published_at > 6 * 60 * 60:
                    publish_profile_catalog(client, paired, configuration)
                    profiles_published_at = time.time()
                payload = {"action": "poll", "deviceId": paired["device_id"]}
                response = call_cinefind(client, paired["endpoint"], payload, paired["device_token"])
                LAST_SUCCESS = time.time()
                LAST_ERROR = None
                job = response.get("job")
                if isinstance(job, dict):
                    state, request_id, failure = dispatch_request(client, configuration, job)
                    call_cinefind(client, paired["endpoint"], {
                        "action": "complete",
                        "deviceId": paired["device_id"],
                        "jobId": job.get("id"),
                        "status": state,
                        "remoteRequestId": request_id,
                        "errorMessage": failure,
                    }, paired["device_token"])
                    logging.info("Completed CineFind job %s as %s", job.get("id"), state)
            except BridgeError as error:
                LAST_ERROR = str(error)
                logging.warning("Bridge cycle failed: %s", error)
                if "authentication failed" in str(error).lower():
                    paired = None
            except Exception:
                LAST_ERROR = "Unexpected bridge error."
                logging.exception("Unexpected bridge cycle error")
            STOP.wait(POLL_INTERVAL)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        healthy = LAST_ERROR is None
        body = json.dumps({"ok": healthy, "last_success_at": LAST_SUCCESS, "error": LAST_ERROR}).encode("utf-8")
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signal_number, lambda *_args: STOP.set())
    run_worker()
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
