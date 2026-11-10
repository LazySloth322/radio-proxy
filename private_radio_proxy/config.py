"""Configuration loading and validation.

Files (all JSON, no database):
  config/app.json       - server, security, limits, paths, logging
  config/stations.json  - the owner's radio station list
  config/tokens.json    - hashed access tokens (managed by tokens.py)

The loader is defensive: it applies defaults, validates every field and
raises ConfigError with a human-readable message listing all problems.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import utils

CONFIG_DIR_NAME = "config"
APP_CONFIG_NAME = "app.json"
STATIONS_CONFIG_NAME = "stations.json"
TOKENS_CONFIG_NAME = "tokens.json"

SECRET_PATH_RE = r"^/[A-Za-z0-9._~-]{8,128}/$"
STATION_ID_RE = r"^[a-z0-9]{4,24}$"

SUPPORTED_METADATA_TYPES = ("icy", "json", "xml", "none")
SUPPORTED_CODECS = ("mp3", "aac", "aacp", "ogg", "oga", "opus", "flac", "wav")
COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# codec -> default Content-Type used when the upstream does not send a
# usable audio Content-Type.
CODEC_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "aacp": "audio/aacp",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "wav": "audio/wav",
}


class ConfigError(Exception):
    """Raised when configuration files are invalid."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MetadataConfig:
    type: str = "none"
    url: str | None = None
    title_path: str | None = None
    artist_path: str | None = None
    cover_path: str | None = None
    poll_interval_seconds: int = 15
    cache_ttl_seconds: int = 45
    background_poll: bool = False


@dataclass
class Station:
    id: str
    name: str
    enabled: bool
    stream_url: str
    fallback_stream_urls: list[str]
    codec: str
    bitrate_kbps: int | None
    cover: str | None
    metadata: MetadataConfig
    upstream_headers: dict[str, str]
    notes: str = ""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 54391
    secret_path: str = ""
    public_base_url: str = ""


@dataclass
class SecurityConfig:
    token_cookie_name: str = "rp_session"
    cookie_secure: bool = False
    cookie_httponly: bool = True
    cookie_samesite: str = "Lax"
    max_failed_token_attempts_per_ip_per_minute: int = 20
    allow_private_upstreams: bool = False  # for tests / local dev only
    trust_proxy_headers: bool = False


@dataclass
class LimitsConfig:
    max_concurrent_streams_per_ip: int = 3
    max_concurrent_streams_per_station: int = 5
    max_concurrent_streams_global: int = 10
    max_requests_per_ip_per_minute: int = 240
    stream_chunk_size_bytes: int = 16384
    upstream_connect_timeout_seconds: float = 10.0
    upstream_read_timeout_seconds: float = 30.0
    upstream_retry_attempts: int = 4
    upstream_backoff_base_seconds: float = 1.0
    upstream_backoff_max_seconds: float = 15.0
    initial_connect_deadline_seconds: float = 20.0
    max_stream_reconnects: int = 5
    client_write_timeout_seconds: float = 30.0
    metadata_poll_timeout_seconds: float = 10.0
    metadata_max_bytes: int = 262144


@dataclass
class PathsConfig:
    static_dir: Path = Path("static")
    covers_dir: Path = Path("covers")
    logs_dir: Path = Path("logs")


@dataclass
class LoggingConfig:
    level: str = "INFO"
    mask_tokens: bool = True


@dataclass
class PlayerConfig:
    # Bottom play bar's session track history (captured client-side from the
    # metadata poll; cleared when the listener switches station).
    history_limit: int = 30          # max tracks kept per session
    history_visible_items: int = 15  # rows visible before the list scrolls


@dataclass
class AppConfig:
    base_dir: Path
    config_dir: Path
    server: ServerConfig
    security: SecurityConfig
    limits: LimitsConfig
    paths: PathsConfig
    logging: LoggingConfig
    player: PlayerConfig
    stations: list[Station] = field(default_factory=list)
    stations_by_id: dict[str, Station] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def resolve(self, relative: Path | str) -> Path:
        """Resolve a configured relative path against the project base dir."""
        return resolve_path(self.base_dir, relative)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc


def _as_int(value: Any, name: str, minimum: int, maximum: int, errors: list[str]) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be an integer, got {value!r}")
        return minimum
    if not minimum <= iv <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}, got {iv}")
        return minimum
    return iv


def _as_num(value: Any, name: str, minimum: float, maximum: float, errors: list[str]) -> float:
    try:
        fv = float(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number, got {value!r}")
        return minimum
    if not minimum <= fv <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}, got {fv}")
        return minimum
    return fv


def _as_bool(value: Any, name: str, default: bool, errors: list[str]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    errors.append(f"{name} must be true or false, got {value!r}")
    return default


def _as_str(value: Any, name: str, default: str, errors: list[str], max_len: int = 200) -> str:
    if value is None:
        return default
    if isinstance(value, str) and 0 < len(value) <= max_len:
        return value
    errors.append(f"{name} must be a non-empty string of at most {max_len} chars")
    return default


# ---------------------------------------------------------------------------
# Station parsing
# ---------------------------------------------------------------------------


def _parse_station(raw: Any, index: int, errors: list[str], warnings: list[str],
                   allow_private: bool = False) -> Station | None:
    label = f"stations[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{label} must be an object")
        return None

    sid = raw.get("id")
    if not isinstance(sid, str) or not re.fullmatch(STATION_ID_RE, sid):
        errors.append(
            f"{label}.id must match {STATION_ID_RE} (opaque, lowercase, 4-24 chars), got {sid!r}"
        )
        sid = None

    name = raw.get("name")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= 100):
        errors.append(f"{label}.name must be a 1-100 char string")
        name = "Unnamed"

    stream_url = raw.get("stream_url")
    if not isinstance(stream_url, str):
        errors.append(f"{label}.stream_url is required")
        stream_url = ""
    else:
        try:
            if not allow_private:
                utils.check_url_allowed_sync(stream_url)
            host = urlsplit(stream_url).hostname or ""
            if not any(c.isalpha() for c in host) and ":" not in host:
                warnings.append(f"{label}.stream_url uses a literal IP address ({host})")
        except utils.UrlBlockedError as exc:
            errors.append(f"{label}.stream_url rejected: {exc}")
            stream_url = ""

    fallback: list[str] = []
    raw_fallback = raw.get("fallback_stream_urls", [])
    if isinstance(raw_fallback, list):
        for i, fb in enumerate(raw_fallback):
            try:
                if not allow_private:
                    utils.check_url_allowed_sync(fb)
                fallback.append(fb)
            except utils.UrlBlockedError as exc:
                errors.append(f"{label}.fallback_stream_urls[{i}] rejected: {exc}")
    elif raw_fallback:
        errors.append(f"{label}.fallback_stream_urls must be a list of URLs")

    codec = _as_str(raw.get("codec"), f"{label}.codec", "mp3", errors, max_len=16).lower()
    if codec not in SUPPORTED_CODECS:
        errors.append(f"{label}.codec must be one of {SUPPORTED_CODECS}, got {codec!r}")
        codec = "mp3"

    bitrate = raw.get("bitrate_kbps")
    if bitrate is not None:
        bitrate = _as_int(bitrate, f"{label}.bitrate_kbps", 16, 2048, errors)

    cover = raw.get("cover")
    if cover is not None:
        if not isinstance(cover, str) or not cover:
            errors.append(f"{label}.cover must be a relative path string or null")
            cover = None
        else:
            try:
                utils.safe_join(Path("covers"), cover)
            except ValueError as exc:
                errors.append(f"{label}.cover rejected: {exc}")
                cover = None
            else:
                if not cover.lower().endswith(COVER_EXTENSIONS):
                    errors.append(
                        f"{label}.cover must end with one of {COVER_EXTENSIONS}, got {cover!r}"
                    )
                    cover = None

    md_raw = raw.get("metadata") or {}
    md_errors: list[str] = []
    md = _parse_metadata(md_raw, f"{label}.metadata", md_errors, allow_private)
    errors.extend(md_errors)

    headers: dict[str, str] = {}
    raw_headers = raw.get("upstream_headers") or {}
    if isinstance(raw_headers, dict):
        for hname, hvalue in raw_headers.items():
            try:
                utils.validate_header_pair(str(hname), str(hvalue))
            except ValueError as exc:
                errors.append(f"{label}.upstream_headers: {exc}")
                continue
            headers[str(hname)] = str(hvalue)
    else:
        errors.append(f"{label}.upstream_headers must be an object")

    if sid is None:
        return None

    if raw.get("notes") is not None and not isinstance(raw.get("notes"), str):
        errors.append(f"{label}.notes must be a string")

    return Station(
        id=sid,
        name=name.strip(),
        enabled=_as_bool(raw.get("enabled"), f"{label}.enabled", True, errors),
        stream_url=stream_url,
        fallback_stream_urls=fallback,
        codec=codec,
        bitrate_kbps=bitrate,
        cover=cover,
        metadata=md,
        upstream_headers=headers,
        notes=str(raw.get("notes") or ""),
    )


def _parse_metadata(raw: Any, label: str, errors: list[str], allow_private: bool = False) -> MetadataConfig:
    if not isinstance(raw, dict):
        errors.append(f"{label} must be an object")
        return MetadataConfig()
    mtype = str(raw.get("type", "none")).lower()
    if mtype not in SUPPORTED_METADATA_TYPES:
        errors.append(f"{label}.type must be one of {SUPPORTED_METADATA_TYPES}, got {mtype!r}")
        mtype = "none"
    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str):
            errors.append(f"{label}.url must be a string or null")
            url = None
        else:
            try:
                if not allow_private:
                    utils.check_url_allowed_sync(url)
            except utils.UrlBlockedError as exc:
                errors.append(f"{label}.url rejected: {exc}")
                url = None
    if mtype in ("json", "xml") and not url:
        errors.append(f"{label}.url is required when metadata.type is {mtype!r}")

    for key in ("title_path", "artist_path", "cover_path"):
        v = raw.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > 128):
            errors.append(f"{label}.{key} must be a short dotted path string or null")

    return MetadataConfig(
        type=mtype,
        url=url,
        title_path=raw.get("title_path"),
        artist_path=raw.get("artist_path"),
        cover_path=raw.get("cover_path"),
        poll_interval_seconds=_as_int(
            raw.get("poll_interval_seconds", 15), f"{label}.poll_interval_seconds", 1, 3600, errors
        ),
        cache_ttl_seconds=_as_int(
            raw.get("cache_ttl_seconds", 45), f"{label}.cache_ttl_seconds", 5, 86400, errors
        ),
        background_poll=_as_bool(raw.get("background_poll"), f"{label}.background_poll", False, errors),
    )


# ---------------------------------------------------------------------------
# app.json parsing
# ---------------------------------------------------------------------------


def _parse_app_config(data: Any, errors: list[str], warnings: list[str]) -> tuple[
    ServerConfig, SecurityConfig, LimitsConfig, PathsConfig, LoggingConfig, PlayerConfig
]:
    if not isinstance(data, dict):
        errors.append("app.json must contain a JSON object")
        data = {}

    srv = data.get("server") or {}
    if not isinstance(srv, dict):
        errors.append("app.json 'server' must be an object")
        srv = {}
    secret_path = srv.get("secret_path", "")
    if not isinstance(secret_path, str) or not re.fullmatch(SECRET_PATH_RE, secret_path):
        errors.append(
            "server.secret_path must look like '/x9k2m-p7qz4-v8n1r-t3w6/' "
            "(starts and ends with '/', 8-128 safe characters total)"
        )
    elif secret_path in ("/x9k2m-p7qz4-v8n1r-t3w6/",):
        warnings.append(
            "server.secret_path is still the shipped example value - run "
            "'python -m private_radio_proxy.cli generate-secret-path' and replace it"
        )
    host = _as_str(srv.get("host"), "server.host", "0.0.0.0", errors, max_len=64)
    server = ServerConfig(
        host=host,
        port=_as_int(srv.get("port", 54391), "server.port", 1, 65535, errors),
        secret_path=secret_path,
        public_base_url=_as_str(srv.get("public_base_url") or None, "server.public_base_url", "", errors, max_len=200),
    )

    sec = data.get("security") or {}
    if not isinstance(sec, dict):
        errors.append("app.json 'security' must be an object")
        sec = {}
    samesite = _as_str(sec.get("cookie_samesite"), "security.cookie_samesite", "Lax", errors, max_len=10)
    if samesite not in ("Lax", "Strict", "None"):
        errors.append("security.cookie_samesite must be Lax, Strict or None")
        samesite = "Lax"
    cookie_name = _as_str(sec.get("token_cookie_name"), "security.token_cookie_name", "rp_session", errors, max_len=64)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cookie_name):
        errors.append("security.token_cookie_name must be a simple token ([A-Za-z0-9_-])")
        cookie_name = "rp_session"
    security = SecurityConfig(
        token_cookie_name=cookie_name,
        cookie_secure=_as_bool(sec.get("cookie_secure"), "security.cookie_secure", False, errors),
        cookie_httponly=_as_bool(sec.get("cookie_httponly"), "security.cookie_httponly", True, errors),
        cookie_samesite=samesite,
        max_failed_token_attempts_per_ip_per_minute=_as_int(
            sec.get("max_failed_token_attempts_per_ip_per_minute", 20),
            "security.max_failed_token_attempts_per_ip_per_minute", 1, 10000, errors,
        ),
        allow_private_upstreams=_as_bool(
            sec.get("allow_private_upstreams"), "security.allow_private_upstreams", False, errors
        ),
        trust_proxy_headers=_as_bool(
            sec.get("trust_proxy_headers"), "security.trust_proxy_headers", False, errors
        ),
    )

    lim = data.get("limits") or {}
    if not isinstance(lim, dict):
        errors.append("app.json 'limits' must be an object")
        lim = {}
    limits = LimitsConfig(
        max_concurrent_streams_per_ip=_as_int(lim.get("max_concurrent_streams_per_ip", 3), "limits.max_concurrent_streams_per_ip", 1, 100, errors),
        max_concurrent_streams_per_station=_as_int(lim.get("max_concurrent_streams_per_station", 5), "limits.max_concurrent_streams_per_station", 1, 100, errors),
        max_concurrent_streams_global=_as_int(lim.get("max_concurrent_streams_global", 10), "limits.max_concurrent_streams_global", 1, 200, errors),
        max_requests_per_ip_per_minute=_as_int(lim.get("max_requests_per_ip_per_minute", 240), "limits.max_requests_per_ip_per_minute", 5, 10000, errors),
        stream_chunk_size_bytes=_as_int(lim.get("stream_chunk_size_bytes", 16384), "limits.stream_chunk_size_bytes", 4096, 65536, errors),
        upstream_connect_timeout_seconds=_as_num(lim.get("upstream_connect_timeout_seconds", 10), "limits.upstream_connect_timeout_seconds", 1, 120, errors),
        upstream_read_timeout_seconds=_as_num(lim.get("upstream_read_timeout_seconds", 30), "limits.upstream_read_timeout_seconds", 1, 600, errors),
        upstream_retry_attempts=_as_int(lim.get("upstream_retry_attempts", 4), "limits.upstream_retry_attempts", 1, 10, errors),
        upstream_backoff_base_seconds=_as_num(lim.get("upstream_backoff_base_seconds", 1.0), "limits.upstream_backoff_base_seconds", 0.1, 60, errors),
        upstream_backoff_max_seconds=_as_num(lim.get("upstream_backoff_max_seconds", 15.0), "limits.upstream_backoff_max_seconds", 1, 300, errors),
        initial_connect_deadline_seconds=_as_num(lim.get("initial_connect_deadline_seconds", 20), "limits.initial_connect_deadline_seconds", 2, 300, errors),
        max_stream_reconnects=_as_int(lim.get("max_stream_reconnects", 5), "limits.max_stream_reconnects", 0, 50, errors),
        client_write_timeout_seconds=_as_num(lim.get("client_write_timeout_seconds", 30), "limits.client_write_timeout_seconds", 1, 600, errors),
        metadata_poll_timeout_seconds=_as_num(lim.get("metadata_poll_timeout_seconds", 10), "limits.metadata_poll_timeout_seconds", 1, 120, errors),
        metadata_max_bytes=_as_int(lim.get("metadata_max_bytes", 262144), "limits.metadata_max_bytes", 1024, 10 * 1024 * 1024, errors),
    )
    if limits.max_concurrent_streams_global < limits.max_concurrent_streams_per_station:
        warnings.append("limits.max_concurrent_streams_global is lower than per-station limit")

    pth = data.get("paths") or {}
    if not isinstance(pth, dict):
        errors.append("app.json 'paths' must be an object")
        pth = {}
    for key in ("static_dir", "covers_dir", "logs_dir"):
        v = pth.get(key)
        if v is not None and (not isinstance(v, str) or v.startswith("/") or ".." in v.split("/")):
            errors.append(f"paths.{key} must be a relative directory name")

    log = data.get("logging") or {}
    if not isinstance(log, dict):
        errors.append("app.json 'logging' must be an object")
        log = {}
    level = str(log.get("level", "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append("logging.level must be DEBUG, INFO, WARNING or ERROR")
        level = "INFO"

    plr = data.get("player") or {}
    if not isinstance(plr, dict):
        errors.append("app.json 'player' must be an object")
        plr = {}

    return (
        server,
        security,
        limits,
        PathsConfig(
            static_dir=Path(str(pth.get("static_dir", "static"))),
            covers_dir=Path(str(pth.get("covers_dir", "covers"))),
            logs_dir=Path(str(pth.get("logs_dir", "logs"))),
        ),
        LoggingConfig(level=level, mask_tokens=_as_bool(log.get("mask_tokens"), "logging.mask_tokens", True, errors)),
        PlayerConfig(
            history_limit=_as_int(
                plr.get("history_limit", 30), "player.history_limit", 1, 500, errors
            ),
            history_visible_items=_as_int(
                plr.get("history_visible_items", 15),
                "player.history_visible_items", 1, 100, errors,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(base_dir: Path, config_dir: Path | None = None) -> AppConfig:
    """Load and validate app.json + stations.json. Raises ConfigError."""
    base_dir = Path(base_dir).resolve()
    config_dir = Path(config_dir) if config_dir else base_dir / CONFIG_DIR_NAME

    errors: list[str] = []
    warnings: list[str] = []

    server, security, limits, paths, logging_cfg, player = _parse_app_config(
        _read_json(config_dir / APP_CONFIG_NAME), errors, warnings
    )
    allow_private = security.allow_private_upstreams
    if allow_private:
        warnings.append(
            "security.allow_private_upstreams is TRUE - upstream SSRF protection is "
            "disabled. Use only for local development/testing, never on a public VPS."
        )

    stations: list[Station] = []
    stations_by_id: dict[str, Station] = {}
    st_data = _read_json(config_dir / STATIONS_CONFIG_NAME)
    if not isinstance(st_data, dict) or not isinstance(st_data.get("stations"), list):
        errors.append("stations.json must contain an object with a 'stations' array")
    else:
        for i, raw in enumerate(st_data["stations"]):
            st = _parse_station(raw, i, errors, warnings, allow_private)
            if st is None:
                continue
            if st.id in stations_by_id:
                errors.append(f"duplicate station id: {st.id!r}")
                continue
            stations.append(st)
            stations_by_id[st.id] = st

    if not any(c.isalpha() for c in server.host) and server.host != "localhost":
        try:
            ip = __import__("ipaddress").ip_address(server.host)
            if utils.is_blocked_ip(ip) and server.host != "0.0.0.0":
                warnings.append(f"server.host {server.host} is a private address")
        except ValueError:
            pass

    if errors:
        raise ConfigError("configuration invalid:\n  - " + "\n  - ".join(errors))

    return AppConfig(
        base_dir=base_dir,
        config_dir=config_dir,
        server=server,
        security=security,
        limits=limits,
        paths=paths,
        logging=logging_cfg,
        player=player,
        stations=stations,
        stations_by_id=stations_by_id,
        warnings=warnings,
    )


def check_tokens_file(path: Path) -> list[str]:
    """Light validation of tokens.json (used by the CLI validate command)."""
    errors: list[str] = []
    if not path.exists():
        return errors
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"tokens.json: {exc}"]
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        return ["tokens.json: missing 'tokens' array"]
    for i, t in enumerate(tokens):
        if not isinstance(t, dict):
            errors.append(f"tokens.json: tokens[{i}] must be an object")
            continue
        th = t.get("token_hash")
        if not isinstance(th, str) or not re.fullmatch(r"[0-9a-f]{64}", th):
            errors.append(f"tokens.json: tokens[{i}].token_hash must be a sha256 hex digest")
        for key in ("created_at", "expires_at", "last_used_at"):
            v = t.get(key)
            if v is not None and not isinstance(v, str):
                errors.append(f"tokens.json: tokens[{i}].{key} must be a string or null")
    return errors


def resolve_path(base_dir: Path, relative: Path | str) -> Path:
    """Resolve a config-relative path against the project base directory."""
    p = Path(relative)
    return p if p.is_absolute() else (base_dir / p)
