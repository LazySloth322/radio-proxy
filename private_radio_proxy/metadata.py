"""Now-playing metadata manager.

Sources, in priority order (per station config):
  * metadata.type == "json" -> poll a JSON endpoint (dotted field mapping)
  * metadata.type == "xml"  -> poll an XML endpoint (simple tag path)
  * metadata.type == "icy"  -> titles are pushed in by the stream proxy while
    a listener is connected (no idle upstream connections are kept - a
    deliberate memory-saving trade-off, documented in the README)
  * otherwise / nothing available -> fallback "Unknown"

Caching:
  * one small in-memory entry per configured station (bounded by design);
  * TTL-based freshness from the station's cache_ttl_seconds;
  * on-demand refresh: when the API reads a stale entry, a single-flight
    background refresh is spawned (per-station asyncio.Lock);
  * optional low-frequency background polling per station
    (metadata.background_poll = true) for owners who want it.

The manager also tracks per-station online/offline/unknown status, updated
by stream connection results and metadata polls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import html
import re
import xml.etree.ElementTree as ET  # stdlib; small XML docs only (size-capped)
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

from . import utils
from .config import LimitsConfig, SecurityConfig, Station
from .icy import join_display, split_artist_title

log = logging.getLogger("radio_proxy.metadata")

MAX_JSON_DEPTH = 12


@dataclass
class NowPlaying:
    title: str | None
    artist: str | None
    display: str
    artwork: str | None
    source: str
    updated_at: str
    fetched_at: float  # monotonic
    ttl: float
    kind: str | None = None # new
    instance: str | None = None # new
    raw: str | None = None # new


def _walk_path(node, path: str | None):
    """Walk a dotted path like 'current.song.title' (list indices allowed)."""
    if not path:
        return None
    parts = [p for p in path.split(".") if p]
    if not parts or len(parts) > MAX_JSON_DEPTH:
        return None
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _clean_str(value) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text[:512] or None
    return None
    
## NEW CODE HERE ##

_IHEART_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"')
_IHEART_ARTIST_TEXT_RE = re.compile(r'^(?P<artist>.*?)\s+-\s+text="(?P<title>[^"]*)"')

_IHEART_HINTS = (
    'text="',
    'song_spot="',
    'spotinstance="',
    'spot_instance="',
    'artist="',
    'title="',
)


@dataclass
class IHeartParsed:
    artist: str | None
    title: str | None
    kind: str | None
    instance: str | None
    display: str
    raw: str
    attrs: dict[str, str]


def _looks_like_iheart_metadata(raw: str) -> bool:
    low = raw.lower()
    return any(hint in low for hint in _IHEART_HINTS)


def parse_iheart_metadata(raw: str | None) -> IHeartParsed | None:
    """
    Parse iHeart-style metadata.

    Examples:

        DEFTONES - text="Change" song_spot="M" spotInstance="abc123"

        artist="Deftones" title="Change" song_spot="M"

    Returns None if the string does not look like iHeart metadata.
    """

    raw = (raw or "").strip()
    if not raw or not _looks_like_iheart_metadata(raw):
        return None

    attrs = {
        key.lower(): html.unescape(value).strip()
        for key, value in _IHEART_ATTR_RE.findall(raw)
    }

    title = attrs.get("text") or attrs.get("title")
    artist = attrs.get("artist")

    # Common iHeart form:
    #
    #   DEFTONES - text="Change" song_spot="M" spotInstance="..."
    #
    # If there is no explicit artist="...", take everything before
    # ' - text="'.
    if not artist and title:
        match = _IHEART_ARTIST_TEXT_RE.match(raw)
        if match:
            artist = html.unescape(match.group("artist")).strip() or None

    # Fallback: remove all key="value" attributes and try normal
    # "Artist - Title" parsing.
    if not artist or not title:
        stripped = _IHEART_ATTR_RE.sub("", raw).strip(" -\t")

        if " - " in stripped:
            left, right = stripped.split(" - ", 1)
            artist = artist or (left.strip() or None)
            title = title or (right.strip() or None)
        elif not title:
            title = stripped or None

    kind = (
        attrs.get("song_spot")
        or attrs.get("spot_type")
        or attrs.get("type")
    )

    instance = (
        attrs.get("spotinstance")
        or attrs.get("spot_instance")
        or attrs.get("instance")
    )

    if not artist and not title:
        return None

    display = join_display(artist, title)

    return IHeartParsed(
        artist=artist,
        title=title,
        kind=kind,
        instance=instance,
        display=display,
        raw=raw,
        attrs=attrs,
    )

## NEW CODE HERE ##


def parse_json_metadata(data: bytes, station: Station) -> tuple[str | None, str | None, str | None]:
    obj = json.loads(data.decode("utf-8", errors="replace"))
    title = _clean_str(_walk_path(obj, station.metadata.title_path))
    artist = _clean_str(_walk_path(obj, station.metadata.artist_path))
    artwork = _clean_str(_walk_path(obj, station.metadata.cover_path))
    return title, artist, artwork


def parse_xml_metadata(data: bytes, station: Station) -> tuple[str | None, str | None, str | None]:
    """Simple tag-path extraction: 'tag.subtag' follows direct children from
    the root element. Full XPath is intentionally not supported (keep it
    lightweight); nested repeated tags return the first match."""
    root = ET.fromstring(data)
    # Strip namespace prefixes defensively: '{ns}tag' -> 'tag'
    def find_child(node, tag):
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == tag:
                return child
        return None

    def walk(path: str | None):
        if not path:
            return None
        node = root
        for tag in [t for t in path.split(".") if t][:MAX_JSON_DEPTH]:
            node = find_child(node, tag)
            if node is None:
                return None
        return getattr(node, "text", None)

    return _clean_str(walk(station.metadata.title_path)), \
        _clean_str(walk(station.metadata.artist_path)), \
        _clean_str(walk(station.metadata.cover_path))


class MetadataManager:
    def __init__(self, stations: dict[str, Station], limits: LimitsConfig, security: SecurityConfig):
        self._stations = stations
        self._limits = limits
        self._allow_private = security.allow_private_upstreams
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, NowPlaying] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._refresh_tasks: set[asyncio.Task] = set()
        self._bg_tasks: list[asyncio.Task] = []
        self._status: dict[str, str] = {sid: "unknown" for sid in stations}
        self._last_err_log: dict[str, float] = {}

    # ------------------------------------------------------------- plumbing

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def _lock(self, station_id: str) -> asyncio.Lock:
        if station_id not in self._locks:
            self._locks[station_id] = asyncio.Lock()
        return self._locks[station_id]

    # --------------------------------------------------------------- status

    def status(self, station_id: str) -> str:
        return self._status.get(station_id, "unknown")

    def set_online(self, station_id: str, online: bool) -> None:
        self._status[station_id] = "online" if online else "offline"

    # ------------------------------------------------------------ ICY input

    def set_from_icy(self, station_id: str, stream_title: str) -> None:
        station = self._stations.get(station_id)
        if station is None:
            return

        raw = (stream_title or "").strip()

        parsed = parse_iheart_metadata(raw)

        if parsed is not None:
            artist = parsed.artist
            title = parsed.title
            display = parsed.display
            artwork = None
            source = "icy-iheart"
            kind = parsed.kind
            instance = parsed.instance
        else:
            artist, title = split_artist_title(raw)
            display = join_display(artist, title)
            artwork = None
            source = "icy"
            kind = None
            instance = None

        old = self._cache.get(station_id)

        # If this is the same iHeart spotInstance and same display text,
        # do not treat it as a new track. Just refresh cache freshness.
        if (
            old is not None
            and instance
            and old.instance == instance
            and old.display == display
        ):
            self._cache[station_id] = replace(
                old,
                fetched_at=time.monotonic(),
            )
            return

        self._cache[station_id] = NowPlaying(
            title=title,
            artist=artist,
            display=display,
            artwork=artwork,
            source=source,
            updated_at=utils.utc_now_iso(),
            fetched_at=time.monotonic(),
            ttl=float(station.metadata.cache_ttl_seconds),
            kind=kind,
            instance=instance,
            raw=raw,
        )

    # ------------------------------------------------------------ API layer

    def snapshot_response(self, station: Station) -> dict:
        """Build the /api/<id>/now-playing response body."""
        entry = self._cache.get(station.id)
        fresh = entry is not None and (time.monotonic() - entry.fetched_at) < entry.ttl
        online = self.status(station.id) == "online"
        if entry is not None and entry.title and fresh:
            return {
                "station_id": station.id,
                "station_name": station.name,
                "title": entry.title,
                "artist": entry.artist,
                "display": entry.display,
                "artwork": entry.artwork,
                "source": entry.source,
                "updated_at": entry.updated_at,
                "online": online,
            }
        return {
            "station_id": station.id,
            "station_name": station.name,
            "title": None,
            "artist": None,
            "display": "Unknown",
            "artwork": None,
            "source": "fallback",
            "updated_at": utils.utc_now_iso(),
            "online": online,
        }

    def maybe_schedule_refresh(self, station: Station) -> None:
        """Spawn a single-flight background refresh if the cache is stale."""
        if station.metadata.type not in ("json", "xml") or self._session is None:
            return
        entry = self._cache.get(station.id)
        fresh = entry is not None and (time.monotonic() - entry.fetched_at) < entry.ttl
        lock = self._lock(station.id)
        if not fresh and not lock.locked():
            task = asyncio.create_task(self.refresh(station.id))
            self._refresh_tasks.add(task)
            task.add_done_callback(self._refresh_tasks.discard)

    # -------------------------------------------------------------- polling

    async def refresh(self, station_id: str) -> None:
        """Fetch JSON/XML metadata for one station; keeps last value on error."""
        station = self._stations.get(station_id)
        if station is None or self._session is None or station.metadata.type not in ("json", "xml"):
            return
        async with self._lock(station_id):
            try:
                await self._fetch_and_store(station)
            except Exception as exc:  # noqa: BLE001 - isolated per station
                self._log_error_throttled(station, exc)

    async def _fetch_and_store(self, station: Station) -> None:
        url = station.metadata.url or ""
        if not self._allow_private:
            await utils.assert_url_allowed(url)
        timeout = aiohttp.ClientTimeout(total=self._limits.metadata_poll_timeout_seconds)
        async with self._session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"metadata endpoint returned HTTP {resp.status}")
            data = await _read_bounded(resp.content, self._limits.metadata_max_bytes)
        if station.metadata.type == "json":
            title, artist, artwork = parse_json_metadata(data, station)
        else:
            title, artist, artwork = parse_xml_metadata(data, station)
        display = join_display(artist, title) if (title or artist) else None
        if display:
            self._cache[station.id] = NowPlaying(
                title=title,
                artist=artist,
                display=display,
                artwork=artwork,
                source=station.metadata.type,
                updated_at=utils.utc_now_iso(),
                fetched_at=time.monotonic(),
                ttl=float(station.metadata.cache_ttl_seconds),
            )
            self.set_online(station.id, True)

    def _log_error_throttled(self, station: Station, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_err_log.get(station.id, 0.0) >= 60.0:
            self._last_err_log[station.id] = now
            log.warning("metadata poll failed station=%s type=%s error=%s",
                        station.id, station.metadata.type, exc)

    # ----------------------------------------------------------- background

    def start_background(self) -> None:
        """Start optional low-frequency pollers for stations that ask for it."""
        for station in self._stations.values():
            if station.enabled and station.metadata.background_poll and \
                    station.metadata.type in ("json", "xml"):
                task = asyncio.create_task(
                    self._background_loop(station.id, float(station.metadata.poll_interval_seconds))
                )
                self._bg_tasks.append(task)

    async def _background_loop(self, station_id: str, interval: float) -> None:
        while True:
            await self.refresh(station_id)
            await asyncio.sleep(max(interval, 5.0))

    async def stop(self) -> None:
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        for task in list(self._refresh_tasks):
            task.cancel()
        if self._refresh_tasks:
            await asyncio.gather(*self._refresh_tasks, return_exceptions=True)
        self._refresh_tasks.clear()


async def _read_bounded(content: aiohttp.StreamReader, cap: int) -> bytes:
    """Read a response body up to `cap` bytes; reject larger bodies."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await content.read(16384)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise RuntimeError(f"metadata response exceeds {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def upstream_host(url: str) -> str:
    """Host:port of a URL, for logging without leaking full URLs."""
    parts = urlsplit(url)
    return parts.netloc or "unknown"
