"""Stream proxy: browser <- this server <- upstream radio station.

Core properties:
  * the browser only ever talks to this server (the upstream URL never
    leaves the owner's config);
  * audio is relayed in small chunks (default 16 KB), never buffered whole;
  * ICY metadata blocks are stripped (or recorded when metadata.type=icy)
    so the browser receives pure audio bytes;
  * upstream drops are retried with exponential backoff, cycling through
    fallback URLs, while keeping the client response open (a short audio
    gap instead of a hard failure);
  * client disconnects cancel the upstream download immediately;
  * per-IP / per-station / global concurrency limits and write timeouts
    prevent hung connections from consuming memory forever.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import aiohttp
from aiohttp import web

from . import utils
from .config import CODEC_CONTENT_TYPES, Station
from .icy import ICYParser, parse_icy_metaint
from .metadata import upstream_host

log = logging.getLogger("radio_proxy.stream")


class UpstreamUnavailable(Exception):
    """All upstream URLs failed (before or during a client connection)."""


def acquire_stream_slots(ctx, station: Station, client_ip: str) -> tuple[bool, list[str]]:
    """Try to take global + per-IP + per-station slots.

    Returns (ok, acquired_keys) - release() every key in acquired_keys when
    done, whatever the outcome.
    """
    limits = ctx.cfg.limits
    acquired: list[str] = []
    for key, limit in (
        ("global", limits.max_concurrent_streams_global),
        ("ip:" + client_ip, limits.max_concurrent_streams_per_ip),
        ("station:" + station.id, limits.max_concurrent_streams_per_station),
    ):
        if ctx.stream_conc.try_acquire(key, limit):
            acquired.append(key)
        else:
            for k in acquired:
                ctx.stream_conc.release(k)
            return False, []
    return True, acquired


def release_stream_slots(ctx, keys: list[str]) -> None:
    for key in keys:
        ctx.stream_conc.release(key)


def pick_content_type(station: Station, upstream_ctype: str) -> str:
    """Choose the Content-Type sent to the browser."""
    base = (upstream_ctype or "").split(";")[0].strip().lower()
    if base.startswith("audio/") or base in ("application/ogg", "video/ogg"):
        return base
    return CODEC_CONTENT_TYPES.get(station.codec, "audio/mpeg")


def _backoff_delay(attempt: int, limits) -> float:
    delay = limits.upstream_backoff_base_seconds * (2 ** (attempt - 1))
    delay = min(delay, limits.upstream_backoff_max_seconds)
    return delay + random.uniform(0, 0.25)  # jitter avoids thundering herds


async def connect_upstream(ctx, station: Station, *, initial: bool) -> tuple[aiohttp.ClientResponse, str]:
    """Connect to the station's primary URL, then fallbacks, with retries,
    exponential backoff and an overall deadline. Returns (response, ctype)."""
    limits = ctx.cfg.limits
    urls = [station.stream_url] + list(station.fallback_stream_urls)
    deadline = time.monotonic() + float(limits.initial_connect_deadline_seconds)
    timeout = aiohttp.ClientTimeout(
        connect=limits.upstream_connect_timeout_seconds,
        sock_read=limits.upstream_read_timeout_seconds,
        total=None,  # live stream: no total limit, sock_read handles stalls
    )
    last_error: Exception | None = None

    for attempt in range(1, limits.upstream_retry_attempts + 1):
        if time.monotonic() > deadline:
            break
        url = urls[(attempt - 1) % len(urls)]
        try:
            if not ctx.cfg.security.allow_private_upstreams:
                await utils.assert_url_allowed(url)
            headers = dict(station.upstream_headers)
            headers.setdefault("User-Agent", utils.DEFAULT_USER_AGENT)
            headers["Accept"] = "*/*"
            if station.metadata.type == "icy":
                headers["Icy-MetaData"] = "1"
            resp = await ctx.session.get(url, headers=headers, timeout=timeout)
            if resp.status != 200:
                resp.close()
                raise UpstreamUnavailable(f"HTTP {resp.status}")
            log.info("stream_upstream_connected station=%s host=%s attempt=%d",
                     station.id, upstream_host(url), attempt)
            return resp, resp.headers.get("Content-Type", "")
        except (UpstreamUnavailable, aiohttp.ClientError, asyncio.TimeoutError, OSError,
                utils.UrlBlockedError) as exc:
            last_error = exc
            log.info("stream_upstream_attempt_failed station=%s host=%s attempt=%d error=%s",
                     station.id, upstream_host(url), attempt, exc)
            if attempt < limits.upstream_retry_attempts and time.monotonic() < deadline:
                await asyncio.sleep(_backoff_delay(attempt, limits))

    raise UpstreamUnavailable(str(last_error) or "all upstream URLs failed")


def client_gone(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


async def proxy_stream(request: web.Request, ctx, station: Station) -> web.StreamResponse:
    """Handle one client stream request end-to-end."""
    limits = ctx.cfg.limits
    chunk_size = limits.stream_chunk_size_bytes
    response = web.StreamResponse()
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    delivered = 0
    reconnects = 0
    started = time.monotonic()

    try:
        upstream, upstream_ctype = await connect_upstream(ctx, station, initial=True)
    except UpstreamUnavailable as exc:
        log.info("stream_upstream_dead station=%s error=%s", station.id, exc)
        ctx.metadata.set_online(station.id, False)
        return web.Response(status=502, text="Station unavailable",
                            content_type="text/plain", headers={"Cache-Control": "no-store"})

    response.content_type = pick_content_type(station, upstream_ctype)

    # If the user arrived with ?token=..., persist the session cookie so the
    # audio element and later requests can use the cookie instead.
    from .auth import attach_cookie_if_needed  # local import avoids a cycle
    attach_cookie_if_needed(request, response, ctx.cfg)

    try:
        await response.prepare(request)
    except (ConnectionError, OSError):
        upstream.close()
        return response

    parser: ICYParser | None = None
    metaint = parse_icy_metaint(upstream.headers)
    if metaint is not None:
        parser = ICYParser(metaint)

    log.info("stream_start station=%s ip=%s ctype=%s metaint=%s",
             station.id, request.remote, response.content_type, metaint)

    try:
        while True:
            try:
                async for chunk in upstream.content.iter_chunked(chunk_size):
                    if client_gone(request):
                        return response
                    audio, titles = parser.feed(chunk) if parser else (chunk, None)
                    if titles and station.metadata.type == "icy":
                        for title in titles:
                            ctx.metadata.set_from_icy(station.id, title)
                    if not audio:
                        continue
                    delivered += len(audio)
                    if delivered == len(audio):  # first byte reached the client
                        ctx.metadata.set_online(station.id, True)
                    try:
                        await asyncio.wait_for(
                            response.write(audio), timeout=limits.client_write_timeout_seconds
                        )
                    except (asyncio.TimeoutError, ConnectionError, OSError):
                        log.info("stream_client_write_failed station=%s ip=%s delivered=%d",
                                 station.id, request.remote, delivered)
                        return response
                # Clean upstream EOF: live streams should not end; treat as drop.
                log.info("stream_upstream_eof station=%s delivered=%d", station.id, delivered)
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
                log.info("stream_upstream_error station=%s error=%s delivered=%d",
                         station.id, exc, delivered)

            upstream.close()
            upstream = None

            if client_gone(request) or reconnects >= limits.max_stream_reconnects:
                break
            reconnects += 1
            log.info("stream_reconnect station=%s attempt=%d/%d",
                     station.id, reconnects, limits.max_stream_reconnects)
            try:
                await asyncio.sleep(_backoff_delay(1, limits) * 0.5)
                upstream, upstream_ctype = await connect_upstream(ctx, station, initial=False)
            except UpstreamUnavailable as exc:
                log.info("stream_reconnect_failed station=%s error=%s", station.id, exc)
                break
            metaint = parse_icy_metaint(upstream.headers)
            parser = ICYParser(metaint) if metaint is not None else None
    finally:
        if upstream is not None:
            upstream.close()
        if delivered > 0:
            ctx.metadata.set_online(station.id, True)
        log.info("stream_stop station=%s ip=%s delivered=%d seconds=%.1f reconnects=%d",
                 station.id, request.remote, delivered, time.monotonic() - started, reconnects)

    return response
