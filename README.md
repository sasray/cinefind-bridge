# CineFind Media Server Bridge

CineFind Bridge is a local Docker companion for users who run Seerr, Radarr or
Sonarr on a private network such as `192.168.x.x`. It works with Docker Desktop,
Docker Compose, TrueNAS, Synology and Unraid. It retrieves a signed,
account-scoped queue from CineFind over outbound HTTPS and asks the selected
local service to request a movie or series. Seerr remains fully supported, but
is no longer required when Radarr and/or Sonarr are configured directly.

It does **not** download media, expose a port, open a router firewall rule, or
send local service credentials, URLs or filesystem paths to CineFind. The
local volume stores only a revocable device token with file permissions
`0600`. CineFind receives service names and numeric quality/root-folder IDs;
root folders are shown by a short friendly name such as `Movies`, never by
their full path.

## Install with Docker Compose

1. Install Docker Desktop on Windows/macOS, or Docker Engine with the Compose
   plugin on Linux.
2. In CineFind, open **Account → Media servers** and create a one-time pairing
   code. It expires after 15 minutes.
3. Create an empty folder and save the included `docker-compose.yml` in it.
4. In that folder, create a `.env` file:

   ```dotenv
   CINEFIND_PAIRING_CODE=YOUR-CODE
   CINEFIND_BRIDGE_NAME=My media server

   SEERR_URL=http://192.168.1.10:5055
   SEERR_API_KEY=
   RADARR_URL=http://192.168.1.10:7878
   RADARR_API_KEY=
   SONARR_URL=http://192.168.1.10:8989
   SONARR_API_KEY=
   ```

   Configure at least one complete URL/API-key pair and leave both fields empty
   for unused services. Radarr and Sonarr expose their API key under
   **Settings → General → Security**.
5. Start and verify the Bridge:

   ```sh
   docker compose up -d
   docker compose logs -f cinefind-bridge
   ```

The device will appear in **Account → Media servers**. No host port needs to
be published. When a local service runs on another device, use its LAN IP. If
it runs on the same Docker host, use its Docker service name, the host's LAN
IP, or `host.docker.internal` on Docker Desktop — not `localhost`.

To update later:

```sh
docker compose pull
docker compose up -d
```

## Install in TrueNAS

1. In CineFind, open **Account → Media servers** and create a
   pairing code.
2. Install the `cinefind-bridge` app from the CineFind catalog/release.
3. Paste the code and configure at least one service:
   - Seerr URL and API key (movies and series),
   - Radarr URL and API key (movies), and/or
   - Sonarr URL and API key (series).
   Local addresses such as `http://192.168.178.195:5055` are supported.
4. Start the app. It pairs once, then polls CineFind every 12 seconds.
5. In CineFind, enable **Automatically request** for the paired device if you
   want confirmed movie/series matches forwarded automatically.

The pairing code expires after 15 minutes and is single use. Use **Disconnect**
in your CineFind account to revoke an installed server immediately.

## TrueNAS Custom App

Create a TrueNAS Custom App with the image
`ghcr.io/sasray/cinefind-bridge:latest`, the persistent `/data` volume and
these environment variables:

- `CINEFIND_PAIRING_CODE` — one-time code from CineFind.
- `SEERR_URL` / `SEERR_API_KEY` — optional local Seerr connection; `/api/v1`
  is optional in the URL.
- `RADARR_URL` / `RADARR_API_KEY` — optional local Radarr connection;
  `/api/v3` is optional in the URL.
- `SONARR_URL` / `SONARR_API_KEY` — optional local Sonarr connection;
  `/api/v3` is optional in the URL.
- `CINEFIND_BRIDGE_NAME` — optional friendly device name.

Set both the URL and API key for every enabled service, and configure at least
one service. API keys are available under **Settings → General → Security** in
Radarr and Sonarr. Requests use the locally resolved quality profile and root
folder; the root-folder path never enters the CineFind queue.

Mount a persistent empty directory at `/data`; do not make it publicly
accessible. No host port needs to be published.

## Updates and recovery

The app needs internet access **outbound** to `cinefindpro.com`/CineFind Cloud
and local network access to each configured service. If you move the app or
lose its `/data` volume, create a new pairing code and pair it again. Never
share the `/data/config.json` file because it contains the device token.
