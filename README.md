# CineFind Bridge for TrueNAS

CineFind Bridge is a local companion for users who run Seerr on a private
network such as `192.168.x.x`. It retrieves a signed, account-scoped queue
from CineFind over outbound HTTPS and asks the local Seerr server to request a
movie in Radarr or a series in Sonarr.

It does **not** download media, expose a port, open a router firewall rule, or
send Seerr credentials to CineFind. The local volume stores only a revocable
device token with file permissions `0600`.

## Install in TrueNAS

1. In CineFind, open **Account → CineFind Bridge · TrueNAS** and create a
   pairing code.
2. Install the `cinefind-bridge` app from the CineFind catalog/release.
3. Paste the code, your local Seerr URL (for example
   `http://192.168.178.195:5055`) and your Seerr API key into the app form.
4. Start the app. It pairs once, then polls CineFind every 12 seconds.
5. In CineFind, enable **Automatically request** for the paired device if you
   want confirmed movie/series matches forwarded automatically.

The pairing code expires after 15 minutes and is single use. Use **Disconnect**
in your CineFind account to revoke an installed server immediately.

## Custom App / Docker Compose beta

Use `docker-compose.yml` or create a TrueNAS Custom App with these environment
variables:

- `CINEFIND_PAIRING_CODE` — one-time code from CineFind.
- `SEERR_URL` — local HTTP/HTTPS Seerr address; `/api/v1` is optional.
- `SEERR_API_KEY` — created in Seerr.
- `CINEFIND_BRIDGE_NAME` — optional friendly device name.

Mount a persistent empty directory at `/data`; do not make it publicly
accessible. No host port needs to be published.

## Updates and recovery

The app needs internet access **outbound** to `cinefindpro.com`/CineFind Cloud
and local network access to your Seerr server. If you move the app or lose its
`/data` volume, create a new pairing code and pair it again. Never share the
`/data/config.json` file because it contains the device token.
