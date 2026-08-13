# CineFind Bridge

CineFind Bridge sends movies and series found by CineFind to a private Seerr
instance. Seerr can then pass them to Radarr or Sonarr.

The app makes an outbound connection to CineFind; no public IP, incoming port,
Cloudflare Tunnel or router configuration is needed. Your Seerr API key stays
in the TrueNAS app and is never stored by CineFind.

Create a one-time pairing code in **CineFind Account → CineFind Bridge**, then
paste the code, your local Seerr URL and API key into the installation form.
