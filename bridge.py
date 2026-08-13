"""CineFind Bridge: a local-only Seerr relay for TrueNAS.

The bridge only makes outbound HTTPS calls to CineFind. Its Seerr URL and API
key are read locally and never leave this container. CineFind sees only a
revocable device token and a queued TMDB request.
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


class BridgeError(RuntimeError):
    """A stable error suitable for logs and a job completion response."""


def clean_endpoint(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BridgeError("CineFind endpoint must use HTTPS.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def clean_seerr_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise BridgeError("Seerr URL must start with http:// or https://.")
    if parsed.query or parsed.fragment:
        raise BridgeError("Seerr URL must not include a query or fragment.")
    base_path = parsed.path.rstrip("/")
    if base_path.lower().endswith("/api/v1"):
        base_path = base_path[:-7]
    return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))


def config_from_environment() -> dict[str, str]:
    return {
        "endpoint": clean_endpoint(os.environ.get("CINEFIND_BRIDGE_API_URL", DEFAULT_ENDPOINT)),
        "seerr_url": clean_seerr_url(os.environ.get("SEERR_URL", "")),
        "seerr_api_key": os.environ.get("SEERR_API_KEY", "").strip(),
        "pairing_code": os.environ.get("CINEFIND_PAIRING_CODE", "").strip(),
        "display_name": os.environ.get("CINEFIND_BRIDGE_NAME", f"TrueNAS ({socket.gethostname()})").strip()[:80],
    }


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
        response = client.post(endpoint, json=payload, headers=headers)
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
        raise BridgeError("Set CINEFIND_PAIRING_CODE in the TrueNAS app, then restart it.")
    if not configuration["seerr_url"] or not configuration["seerr_api_key"]:
        raise BridgeError("Set the local Seerr URL and API key in the TrueNAS app.")
    response = call_cinefind(client, configuration["endpoint"], {
        "action": "pair",
        "pairingCode": configuration["pairing_code"],
        "displayName": configuration["display_name"] or "TrueNAS",
    })
    device_id = response.get("deviceId")
    device_token = response.get("deviceToken")
    if not isinstance(device_id, str) or not isinstance(device_token, str) or len(device_token) < 32:
        raise BridgeError("CineFind returned an invalid pairing response.")
    save_paired_device(configuration["endpoint"], device_id, device_token)
    logging.info("Paired as %s", response.get("displayName", "TrueNAS"))
    return {"device_id": device_id, "device_token": device_token, "endpoint": configuration["endpoint"]}


def direct_arr_profiles(client: httpx.Client, server: dict[str, Any]) -> list[dict[str, Any]]:
    """Return profiles from a configured Radarr/Sonarr instance as a fallback.

    Some Seerr releases expose their service records normally but fail their
    per-service profile endpoint. The service API key is used only inside the
    user's local network; neither it nor the server address leaves the bridge.
    """
    api_key = server.get("apiKey") or server.get("api_key")
    hostname = str(server.get("hostname") or server.get("host") or "").strip()
    port = server.get("port")
    if not isinstance(api_key, str) or not api_key.strip() or not hostname:
        return []
    if hostname.startswith(("http://", "https://")):
        parsed = urlsplit(hostname)
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
        })
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


def profile_catalog(client: httpx.Client, configuration: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Read the actual request profiles available in the user's Seerr.

    The Bridge sends only names and numeric IDs to CineFind. The Seerr URL and
    API key stay local, exactly like when a title is requested.
    """
    headers = {
        "X-Api-Key": configuration["seerr_api_key"],
        "Accept": "application/json",
        "User-Agent": "CineFind-Bridge/1.1",
    }
    catalog: dict[str, list[dict[str, Any]]] = {"movie": [], "tv": []}

    # Seerr exposes profiles separately for every configured Radarr/Sonarr
    # instance. They are not included in /settings/main.
    for media_type, server_key in (("movie", "radarr"), ("tv", "sonarr")):
        try:
            response = client.get(f"{configuration['seerr_url']}/api/v1/settings/{server_key}", headers=headers)
            raw_servers = response.json() if response.is_success else []
        except (httpx.HTTPError, ValueError):
            logging.warning("Could not load configured Seerr %s servers.", server_key)
            continue
        raw_servers = service_list(raw_servers, server_key)

        for server in raw_servers[:20]:
            if not isinstance(server, dict):
                continue
            server_id = positive_int(server.get("id"))
            if server_id is None:
                continue
            server_name = str(server.get("name") or ("Radarr" if media_type == "movie" else "Sonarr")).strip()[:120]
            try:
                response = client.get(
                    f"{configuration['seerr_url']}/api/v1/settings/{server_key}/{server_id}/profiles",
                    headers=headers,
                )
                raw_profiles = response.json() if response.is_success else []
            except (httpx.HTTPError, ValueError):
                logging.warning("Could not load Seerr profiles for %s server %s.", server_key, server_id)
                raw_profiles = []
            raw_profiles = profile_list(raw_profiles)
            if not raw_profiles:
                raw_profiles = direct_arr_profiles(client, server)
            # The service-list API can omit its API key. Request the complete
            # local service record only as a fallback; it never leaves the
            # bridge or the user's network.
            if not raw_profiles:
                try:
                    response = client.get(
                        f"{configuration['seerr_url']}/api/v1/settings/{server_key}/{server_id}",
                        headers=headers,
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
                        "serverId": server_id,
                        "profileId": profile_id,
                        "name": profile_name,
                        "serverName": server_name,
                    })
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
    }, paired["device_token"])


def create_seerr_request(client: httpx.Client, configuration: dict[str, str], job: dict[str, Any]) -> tuple[str, int | None, str | None]:
    media_type = job.get("mediaType")
    tmdb_id = job.get("tmdbId")
    if media_type not in {"movie", "tv"} or not isinstance(tmdb_id, int) or tmdb_id < 1:
        return "failed", None, "CineFind sent an invalid media job."
    payload: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
    if media_type == "tv":
        payload["seasons"] = "all"
    options = job.get("requestOptions")
    if isinstance(options, dict):
        server_id = options.get("serverId")
        profile_id = options.get("profileId")
        if isinstance(server_id, int) and server_id > 0 and isinstance(profile_id, int) and profile_id > 0:
            payload["serverId"] = server_id
            payload["profileId"] = profile_id
    headers = {
        "X-Api-Key": configuration["seerr_api_key"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "CineFind-Bridge/1.0",
    }
    try:
        response = client.post(f"{configuration['seerr_url']}/api/v1/request", json=payload, headers=headers)
        raw = response.text[:500]
        try:
            body = response.json()
        except ValueError:
            body = {}
    except httpx.HTTPError:
        return "failed", None, "Local Seerr could not be reached."
    request_id = body.get("id") if isinstance(body, dict) and isinstance(body.get("id"), int) else None
    if response.status_code == 409:
        return "already_requested", request_id, None
    if not response.is_success:
        return "failed", None, f"Seerr returned {response.status_code}: {raw.replace(chr(10), ' ').strip()}"[:500]
    return "requested", request_id, None


def run_worker() -> None:
    global LAST_SUCCESS, LAST_ERROR
    try:
        configuration = config_from_environment()
    except BridgeError as error:
        LAST_ERROR = str(error)
        logging.error("Configuration error: %s", error)
        return
    if not configuration["seerr_url"] or not configuration["seerr_api_key"]:
        LAST_ERROR = "Set the local Seerr URL and API key in the TrueNAS app."
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
                    state, request_id, failure = create_seerr_request(client, configuration, job)
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
