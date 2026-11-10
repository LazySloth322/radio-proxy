# private-radio-proxy

A minimal, private internet radio **stream proxy** for a personal VPS.

You (the owner) configure a handful of foreign radio stations in JSON files.
The server fetches each stream from its own network location and re-serves it
to a few authorized people (you + 2-3 friends). The browser plays audio from
**your server**, never directly from the radio station, and the page shows
cover art and "now playing" text when the station provides it.

Strictly personal / non-commercial: no public registration, no admin UI, no
open proxy, no accounts, no monetization, no fingerprinting, no transcoding.

```
client browser ──HTTP──>  VPS (this app)  ──HTTPS──> radio station upstream
                 token   :54391/<secret>/            owner-configured URLs
```

---

## 1. How it works (architecture)

* **Runtime**: Python 3.11+, [aiohttp](https://docs.aiohttp.org/) as the only
  runtime dependency (async HTTP server + client). No database, no Redis, no
  queues - just JSON files and local images.
* **Single secret path**: every useful route lives under one long random path,
  e.g. `http://<server-ip>:54391/x9k2m-p7qz4-v8n1r-t3w6/`. Anything outside it
  gets a bare `404`, so the server does not even confirm it exists.
* **Tokens**: the owner mints long random tokens with the CLI. Only a
  SHA-256 hash is stored (`config/tokens.json`); the raw token is printed
  once. Tokens can expire and can be revoked individually. The first visit
  with `?token=...` sets an `HttpOnly` cookie scoped to the secret path;
  the query parameter keeps working for audio elements and odd browsers.
* **Stream proxy** (`stream_proxy.py`): connects to the upstream URL from the
  owner config (primary + fallbacks), strips interleaved ICY metadata, and
  relays pure audio to the browser in small chunks (default 16 KB). Upstream
  drops are retried with exponential backoff within the same client
  connection; a client disconnect cancels the upstream download immediately.
* **Metadata** (`metadata.py` + `icy.py`): per-station "now playing" from
  ICY in-stream titles (while someone listens), or from JSON/XML endpoints
  (polled on demand with TTL caching, optional low-frequency background
  polling). Unknown/fallback text otherwise.
* **Limits** (`limits.py`): per-IP request rate limit, per-IP / per-station /
  global stream concurrency caps, upstream connect/read timeouts, client
  write timeout. All bounded in-memory structures.
* **Frontend** (`static/`): single page, vanilla JS. Station cards with cover
  art and a fixed bottom player bar containing the selected station's name
  and "now playing" track, a volume slider (remembered in `localStorage`) and
  a lightweight Web Audio spectrum visualizer (bars, same-origin stream, so
  no CORS tainting; playback keeps working if Web Audio is unavailable).
  The chevron button on the left edge of the bar opens the **session track
  history**: the last `player.history_limit` tracks of the current station
  ("Artist - Title", newest first, scrollable after
  `player.history_visible_items` rows). It is captured client-side from the
  metadata poll, so it holds only tracks heard on this page session, is
  private to the logged-in token's browser, is wiped on station change and
  costs no server memory.
  Light/dark theme switcher in the top bar - defaults to the OS preference,
  choice persisted in `localStorage`, applied before first paint by
  `theme.js` (no flash).
* **Hardening**: SSRF protection on all upstream URLs (only `http(s)`, no
  loopback/private/link-local/CGNAT IPs, DNS re-checked at connect time,
  fail-closed), path-traversal-safe file serving, CRLF-safe upstream headers,
  constant-time token comparison, token masking in all logs.

```
radio-proxy/
├── README.md
├── requirements.txt              # aiohttp only
├── config/
│   ├── app.json                  # server, security, limits, paths, logging
│   ├── stations.json             # owner-configured station list
│   └── tokens.json               # hashed tokens (chmod 600, CLI-managed)
├── covers/                       # local cover images (jpg/jpeg/png/webp)
│   └── .gitkeep
├── static/                       # vanilla frontend (no CDNs)
│   ├── index.html
│   ├── style.css
│   ├── theme.js                  # pre-paint light/dark bootstrap
│   ├── app.js
│   └── fallback-cover.svg
├── private_radio_proxy/
│   ├── __init__.py
│   ├── __main__.py               # python -m private_radio_proxy
│   ├── app.py                    # factory, middlewares, lifecycle
│   ├── config.py                 # JSON loading + strict validation
│   ├── tokens.py                 # hash-only token store (atomic writes)
│   ├── auth.py                   # secret-path gate, cookie, fail2ban events
│   ├── routes.py                 # page / assets / API / cover / stream
│   ├── stream_proxy.py           # chunked relay, retries, fallbacks
│   ├── metadata.py               # now-playing cache, JSON/XML polling
│   ├── icy.py                    # bounded stateful ICY parser
│   ├── limits.py                 # rate + concurrency limiters
│   ├── logging_utils.py          # rotation + token masking
│   ├── cli.py                    # owner management CLI
│   └── utils.py                  # SSRF guard, safe paths, helpers
├── systemd/private-radio-proxy.service
├── fail2ban/
│   ├── filter.d/private-radio-proxy.conf
│   └── jail.d/private-radio-proxy.conf
├── scripts/
│   ├── firewall_example.sh       # nftables ruleset (uff alternatives inside)
│   ├── smoke_test.py             # end-to-end self test
│   └── memory_check.py           # RSS footprint check
└── tests/                        # unittest suite (stdlib + aiohttp test utils)
    ├── test_icy.py
    ├── test_tokens.py
    ├── test_utils.py             # path traversal + SSRF rejection
    ├── test_config.py
    ├── test_metadata.py
    ├── test_limits.py
    └── test_e2e.py               # full HTTP flow with fake upstream
```

Measured footprint (this machine, `scripts/memory_check.py`): ~47 MB RSS
with 3 concurrent listeners, flat. Budget target: comfortably below 120 MB.

---

## 2. Requirements

* Debian 12 (bookworm) or 13 (trixie), any arch.
* Python 3.11+ (`python3 --version`).
* ~256 MB free RAM is plenty. No root needed at runtime (dedicated user).

## 3. Quick start

```bash
# 1. Install (as root or with sudo)
apt update && apt install -y python3 python3-venv
adduser --system --group --home /opt/radio-proxy radioproxy
mkdir -p /opt/radio-proxy
cp -r . /opt/radio-proxy/            # from wherever you unpacked it
cd /opt/radio-proxy
python3 -m venv venv
venv/bin/pip install -r requirements.txt
chown -R radioproxy:radioproxy /opt/radio-proxy

# 2. Generate a secret path and put it into config/app.json
python3 -m private_radio_proxy.cli generate-secret-path
#   -> /x9k2m-p7qz4-v8n1r-t3w6/   (example; use YOUR generated value)
$EDITOR config/app.json                     # set server.secret_path (+ port)

# 3. Create tokens (raw token printed ONCE - copy immediately)
python3 -m private_radio_proxy.cli token add --name owner
python3 -m private_radio_proxy.cli token add --name friend-1 --expires 2027-01-01
python3 -m private_radio_proxy.cli token add --name friend-2

# 4. Add your stations: edit config/stations.json (see section 6)

# 5. Validate everything
python3 -m private_radio_proxy.cli validate-config

# 6. Optional: probe the upstreams without starting the server
python3 -m private_radio_proxy.cli healthcheck

# 7. Try it in the foreground
sudo -u radioproxy venv/bin/python -m private_radio_proxy
# Open: http://<server-ip>:54391/<secret-path>/?token=<owner-token>

# 8. Install as a systemd service
cp systemd/private-radio-proxy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now private-radio-proxy
systemctl status private-radio-proxy
```

Files and permissions:

```bash
chmod 600 config/*.json          # config is private
chmod 755 covers static          # static assets readable
chmod 644 covers/*               # cover images
```

## 4. Configuration reference

### `config/app.json`

| Key | Meaning |
| --- | --- |
| `server.host` / `server.port` | Listen address; use a high custom port, e.g. 54391. |
| `server.secret_path` | The secret prefix for ALL routes. Generate it, 8-128 safe chars, starts/ends with `/`. |
| `server.public_base_url` | Optional; used by the CLI to print full login URLs. |
| `security.token_cookie_name` | Cookie name (default `rp_session`). |
| `security.cookie_*` | Cookie flags. `cookie_secure` stays `false` because the app is plain HTTP. |
| `security.max_failed_token_attempts_per_ip_per_minute` | Auth-failure rate limit (also fed to fail2ban). |
| `security.allow_private_upstreams` | **Keep `false`.** `true` disables SSRF protection - only for tests on localhost. |
| `security.trust_proxy_headers` | Set `true` ONLY behind your own nginx/Caddy so rate limits see real IPs. |
| `limits.*` | Stream concurrency (per IP / station / global), chunk size, all timeouts, retry counts. `limits.max_requests_per_ip_per_minute` is the per-IP request rate limit - it counts **every** request under the secret path (page, assets, covers, API polls). Default `240`, valid range 5-10000. Raise it if several listeners share one IP/NAT; the auth-failure limit under `security.*` is separate and should stay strict. |
| `paths.*` | Relative dirs for static files, covers, logs. |
| `player.history_limit` | Max tracks kept in the play bar's recent-tracks list (newest first). Captured per browser session from the metadata poll, cleared on station change. Default `30`, valid range 1-500. |
| `player.history_visible_items` | Rows visible in the recent-tracks list before it scrolls. Default `15`, valid range 1-100. |
| `logging.level` / `logging.mask_tokens` | Log verbosity; masking must stay on. |

### `config/stations.json`

```json
{
  "stations": [
    {
      "id": "s7k2m9",
      "name": "Example Radio",
      "enabled": true,
      "stream_url": "https://upstream.example.com/live.mp3",
      "fallback_stream_urls": ["https://mirror.example.com/live.mp3"],
      "codec": "mp3",
      "bitrate_kbps": 128,
      "cover": "covers/s7k2m9.jpg",
      "metadata": {
        "type": "icy",
        "url": null,
        "poll_interval_seconds": 15,
        "cache_ttl_seconds": 45
      },
      "upstream_headers": { "User-Agent": "PrivateRadioProxy/1.0" },
      "notes": "Owner only - never shown to users"
    }
  ]
}
```

* `id` - opaque public identifier used in URLs (`radio/s7k2m9/stream`).
  Lowercase letters/digits, 4-24 chars. Use something unpredictable; never
  the station name. Change it to invalidate old URLs.
* `metadata.type`:
  * `icy` - parse `StreamTitle` from the audio stream while someone listens
    (no idle upstream connection is kept; metadata stops updating when nobody
    listens, and the last value is kept until `cache_ttl_seconds` expires).
  * `json` - poll `metadata.url` and extract fields with dotted paths, e.g.
    `"title_path": "current.song.title"` (array indices like `playlist.0.title`
    also work). Optional `artist_path`, `cover_path`.
  * `xml` - poll `metadata.url` and extract with a simple tag path
    (`"title_path": "now.title"` = direct children chain from the root).
    Full XPath is intentionally not supported - keep it lightweight.
  * `none` - no metadata; the UI shows "Unknown".
* `metadata.background_poll` - `true` = poll JSON/XML metadata at
  `poll_interval_seconds` even with no listeners (default `false`).
* `upstream_headers` - extra headers for upstream requests (CRLF-safe).

### `config/tokens.json`

Managed by the CLI; format:

```json
{
  "tokens": [
    {
      "key_id": "t4f9c2a",
      "name": "friend-1",
      "token_hash": "<64 hex chars - sha256 of the raw token>",
      "enabled": true,
      "created_at": "2026-11-09T10:05:00Z",
      "expires_at": "2027-01-01T00:00:00Z",
      "last_used_at": null
    }
  ]
}
```

Raw tokens are never written anywhere.

## 5. CLI reference

```
python -m private_radio_proxy.cli generate-secret-path
python -m private_radio_proxy.cli token add --name friend-1 [--expires 2027-01-01]
python -m private_radio_proxy.cli token list
python -m private_radio_proxy.cli token revoke --key-id t4f9c2a
python -m private_radio_proxy.cli token revoke --key-id t4f9c2a --enable   # undo
python -m private_radio_proxy.cli validate-config
python -m private_radio_proxy.cli healthcheck [--station s7k2m9]
python -m private_radio_proxy.cli serve [--host H] [--port P]
python -m private_radio_proxy                                              # = serve
```

`token add` prints the raw token **once** plus a ready-to-send login URL.

## 6. Add / remove a station

Add:

1. Edit `config/stations.json` - append a station object with a fresh opaque `id`.
2. Put a small cover image (256x256 or 320x320, jpg/png/webp) into `covers/`
   with the name from the `cover` field. No cover? Leave `cover: null` - the
   built-in fallback icon is served.
3. `python3 -m private_radio_proxy.cli validate-config`
4. `systemctl restart private-radio-proxy` (config is read at startup).

Remove (e.g. on a rights-holder request):

1. Delete the station object from `config/stations.json` (or set
   `"enabled": false` to keep it hidden temporarily).
2. `systemctl restart private-radio-proxy`. The station disappears from the
   UI and its stream/cover endpoints start returning 404 immediately.

## 7. Revoke / rotate access

* One person: `token revoke --key-id tXXXX` - that friend loses access on
  their next request; others are unaffected.
* Everyone: revoke all tokens and issue new ones.
* Suspected link leak: `generate-secret-path`, update `server.secret_path`
  in `app.json`, restart, and send everyone the new URL (tokens stay valid).

## 8. Security model - read this

**Plain HTTP means everything is visible on the wire**: the secret path, the
`?token=` value on the first visit, and the audio itself. Any network hop
between a friend and the VPS can observe them.

* The secret path + tokens are **obfuscation and access control, not
  encryption**. They stop casual scanning, port scanners and hotlinking -
  they do not stop a determined eavesdropper on the path.
* Do not share links publicly or in chat channels you do not control.
  Revoke and rotate immediately if a link leaks.
* **If you want real privacy, put the app behind a VPN instead of exposing
  the port**: WireGuard, Tailscale, Netbird or ZeroTier all work well on a
  256 MB VPS. Then bind `server.host` to the VPN interface and drop the
  firewall rule for the app port entirely. The app works unchanged.
* Keep Debian updated (`unattended-upgrades` is preinstalled on Debian;
  make sure it is enabled).
* **SSH**: prefer SSH keys and disable password authentication
  (`PasswordAuthentication no` in `/etc/ssh/sshd_config`) even if you
  currently use a strong password on a non-standard port with fail2ban.
  Keys are cheap to set up and remove the single most common break-in path.
  The web app does not depend on SSH security, but your server does.

Other built-in protections:

* No open proxy: the server fetches only URLs from `stations.json`. There is
  no `/proxy?url=...` and no user-supplied URL anywhere. Upstream URLs are
  additionally SSRF-checked (scheme, literal and resolved IPs, fail-closed
  DNS).
* Unknown and disabled stations return the same generic 404 as everything
  else - station IDs cannot be probed.
* Failed-token attempts are rate-limited per IP (default 20/min) and logged
  as `AUTH_FAIL ip=...` for fail2ban.
* Constant-time token comparison; tokens stored only as SHA-256 hashes;
  `tokens.json` is `chmod 600`.
* Cover/asset serving is whitelist- and containment-based; traversal attempts
  (`..`, absolute paths, backslashes, NUL) are rejected.
* Logs mask every `token=...` value and the session cookie; rotation at
  ~1 MB x 3 backups.

## 9. Firewall

Run `scripts/firewall_example.sh` (review it first; it applies nftables
rules allowing only SSH + the app port). Persistent install:

```bash
sudoedit /etc/nftables.conf    # paste the ruleset from the script
systemctl enable --now nftables
```

ufw alternative:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow <your-ssh-port>/tcp
ufw allow 54391/tcp
ufw enable
```

Behind a VPN? Allow only the VPN interface/traffic and remove the public
app-port rule.

## 10. fail2ban

The app writes `AUTH_FAIL ip=...` and `RATE_LIMITED ip=...` lines to
`logs/app.log`. The provided jail bans IPs after 20 bad attempts/minute for
1 hour (with increments):

```bash
cp fail2ban/filter.d/private-radio-proxy.conf /etc/fail2ban/filter.d/
cp fail2ban/jail.d/private-radio-proxy.conf   /etc/fail2ban/jail.d/
systemctl restart fail2ban
fail2ban-client status private-radio-proxy
```

Keep your existing SSH jail enabled as well.

## 11. Memory tuning

Defaults are already conservative (16 KB chunks, bounded caches, pooled
upstream connections). Measured ~47 MB RSS with 3 listeners; the systemd
unit enforces `MemoryHigh=140M` / `MemoryMax=180M` as a guard rail.

* If the OOM killer ever triggers (check `journalctl -u private-radio-proxy
  | grep -i oom`), first lower `max_concurrent_streams_global`.
* Raising `stream_chunk_size_bytes` slightly improves CPU efficiency at the
  cost of a few MB; 8192-65536 is the valid range.
* `CPUQuota=60%` limits runaway upstream loops; raise it if you add stations
  with heavy JSON polling.

## 12. Running / operating

```bash
systemctl start|stop|restart private-radio-proxy
journalctl -u private-radio-proxy -f        # console log
tail -f /opt/radio-proxy/logs/app.log
```

Self-tests (offline, no real radio needed):

```bash
python3 -m unittest discover -s tests -t .   # unit + integration suite
python3 scripts/smoke_test.py                # end-to-end HTTP flow
python3 scripts/memory_check.py              # RSS footprint
```

`smoke_test.py` spins up a fake ICY radio upstream, runs the real app and
verifies auth, cookies, API, streaming, metadata capture, 404-stealth and
the absence of any open-proxy endpoint.

## 13. Troubleshooting

| Symptom | Check |
| --- | --- |
| Page 404 with valid-looking link | Secret path typo; token revoked/expired (`token list`); wrong port. |
| Play button fails instantly | `healthcheck --station <id>`; station disabled; upstream blocking your VPS IP (check `logs/app.log` for `stream_upstream_*`). |
| No "now playing" | `metadata.type=icy` only updates while someone is listening; JSON/XML: run `healthcheck` to test the endpoint. |
| 429 responses | Per-IP request rate limit hit - by design. Raise `limits.max_requests_per_ip_per_minute` in `config/app.json` (default `240`, valid 5-10000) and restart the service. Note it counts every request: page load, covers and the 15 s metadata poll. |
| Logs full of AUTH_FAIL | Scanner found nothing (it gets 404s); confirm fail2ban is banning (`fail2ban-client status private-radio-proxy`). |

## 14. Legal / ethical note

Private personal project. All radio streams, trademarks, artwork, and
content belong to their respective owners. No monetization. Access is
intended for personal use only. The owner is responsible for ensuring that
the streams configured here are used in accordance with their terms; the
app makes it easy to remove any station instantly if a rights holder asks.

## 15. License

For private, non-commercial use by the deploying owner. Provided as-is,
without warranty of any kind.
