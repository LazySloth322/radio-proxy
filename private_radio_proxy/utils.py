"""Shared helpers: URL validation (SSRF guard), safe filesystem paths,
time formatting, random token/secret-path generation, log masking.

Everything here is dependency-free standard library code.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import re
import secrets
import string
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# URL / SSRF protection
# ---------------------------------------------------------------------------


class UrlBlockedError(ValueError):
    """Raised when a URL points to a forbidden (private/loopback/...) target."""


# Networks that must never be contacted as upstreams, even by the owner
# config, to keep the server from being turned into an internal-network probe.
_BLOCKED_V4 = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",        # "this" network
        "10.0.0.0/8",       # RFC1918 private
        "100.64.0.0/10",    # carrier-grade NAT
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local, incl. cloud metadata 169.254.169.254
        "172.16.0.0/12",    # RFC1918 private
        "192.0.0.0/24",     # IETF protocol assignments
        "192.0.2.0/24",     # TEST-NET-1
        "192.168.0.0/16",   # RFC1918 private
        "198.18.0.0/15",    # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",   # TEST-NET-3
        "224.0.0.0/4",      # multicast
        "240.0.0.0/4",      # reserved
    )
]
_BLOCKED_V6 = [
    ipaddress.ip_network(n)
    for n in (
        "::/128",       # unspecified
        "::1/128",      # loopback
        "64:ff9b:1::/48",  # NAT64 local
        "100::/64",     # discard-only
        "2001:db8::/32",  # documentation
        "fc00::/7",     # unique local
        "fe80::/10",    # link-local
        "ff00::/8",     # multicast
    )
]

ALLOWED_SCHEMES = ("http", "https")


def is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """True if the address is private/loopback/link-local/otherwise forbidden."""
    # Handle IPv4-mapped IPv6 addresses (::ffff:127.0.0.1) explicitly.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    networks = _BLOCKED_V6 if ip.version == 6 else _BLOCKED_V4
    return any(ip in net for net in networks)


def check_url_allowed_sync(url: str) -> None:
    """Cheap synchronous validation of an upstream URL.

    Verifies the scheme and rejects literal-IP hosts in blocked ranges and
    "localhost" names. DNS resolution is checked asynchronously at connect
    time (see assert_url_allowed).
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UrlBlockedError(f"scheme not allowed: {parts.scheme!r}")
    host = parts.hostname  # lower-cased, without brackets for IPv6
    if not host:
        raise UrlBlockedError("URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        raise UrlBlockedError("localhost is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname: resolved and re-checked at connect time
    if is_blocked_ip(ip):
        raise UrlBlockedError(f"blocked address: {host}")


async def assert_url_allowed(url: str) -> None:
    """Full async validation: scheme, then DNS resolution with IP checks.

    Fails closed: if DNS cannot be resolved, the URL is rejected.
    """
    check_url_allowed_sync(url)
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return  # literal IP already checked above
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as exc:
        raise UrlBlockedError(f"DNS resolution failed for {host!r}") from exc
    if not infos:
        raise UrlBlockedError(f"DNS returned no addresses for {host!r}")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover - getaddrinfo returns IPs
            continue
        if is_blocked_ip(ip):
            raise UrlBlockedError(f"{host!r} resolves to blocked address {addr}")


# ---------------------------------------------------------------------------
# Safe filesystem paths
# ---------------------------------------------------------------------------


def safe_join(base: Path, rel: str) -> Path:
    """Join `rel` onto `base`, rejecting any form of path traversal.

    Rejects absolute paths, ".." segments, backslashes and NUL bytes, then
    verifies the resolved candidate stays inside the resolved base directory.
    """
    if not rel or not isinstance(rel, str):
        raise ValueError("empty path")
    if "\x00" in rel or "\\" in rel:
        raise ValueError("illegal characters in path")
    if rel.startswith("/") or rel.startswith("~"):
        raise ValueError("absolute paths are not allowed")
    parts = rel.split("/")
    if any(p in ("..", ".") for p in parts):
        raise ValueError("path traversal detected")
    base_res = base.resolve()
    candidate = (base_res / "/".join(parts)).resolve()
    if candidate != base_res and base_res not in candidate.parents:
        raise ValueError("resolved path escapes base directory")
    return candidate


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Current UTC time as '2026-11-09T12:34:56Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> datetime:
    """Parse ISO-8601 UTC dates/datetimes ('2027-01-01', '...T00:00:00Z')."""
    value = (value or "").strip()
    if not value:
        raise ValueError("empty timestamp")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if "T" not in value and len(value) == 10:
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Random generation
# ---------------------------------------------------------------------------

# Unambiguous lowercase alphabet (no 0/o/1/l/i) for the secret path.
SECRET_PATH_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def generate_secret_path(groups: int = 4, group_len: int = 5) -> str:
    """Random secret path like '/x9k2m-p7qz4-v8n1r-t3w6/' (~99 bits entropy)."""
    rng = secrets.SystemRandom()
    groups_out = [
        "".join(rng.choice(SECRET_PATH_ALPHABET) for _ in range(group_len))
        for _ in range(groups)
    ]
    return "/" + "-".join(groups_out) + "/"


def generate_token() -> str:
    """Long random URL-safe token (~192 bits entropy)."""
    return secrets.token_urlsafe(24)


def short_id(prefix: str = "t") -> str:
    """Short random identifier for token key_ids, e.g. 't4f9c2a'."""
    return prefix + secrets.token_hex(3)


# ---------------------------------------------------------------------------
# Token hashing / comparison
# ---------------------------------------------------------------------------


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a raw token. Only hashes are stored on disk."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Log masking
# ---------------------------------------------------------------------------

_TOKEN_QS_RE = re.compile(r"([?&]token=)[^&\s'\"]+", re.IGNORECASE)


def mask_query_tokens(text: str, cookie_name: str | None = None) -> str:
    """Replace token query-string values with '***' (never log raw tokens)."""
    out = _TOKEN_QS_RE.sub(r"\1***", text)
    if cookie_name:
        out = re.sub(
            r"(\b" + re.escape(cookie_name) + r"=)[^;\s'\"]+",
            r"\1***",
            out,
            flags=re.IGNORECASE,
        )
    return out


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]{0,512}$")

DEFAULT_USER_AGENT = "PrivateRadioProxy/1.0"


def validate_header_pair(name: str, value: str) -> None:
    """Reject header names/values that could enable CRLF injection."""
    if not HEADER_NAME_RE.fullmatch(name or ""):
        raise ValueError(f"invalid header name: {name!r}")
    if not HEADER_VALUE_RE.fullmatch(value or ""):
        raise ValueError(f"invalid header value for {name!r}")
