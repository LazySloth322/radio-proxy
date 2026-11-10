"""HTTP routes.

All routes live under the secret path (auth middleware handles the gate):

  GET <secret>/                        -> single-page frontend (index.html)
  GET <secret>/assets/{name}           -> whitelisted static assets
  GET <secret>/api/stations            -> safe public station list
                                       (+ player settings: history limits)
  GET <secret>/api/{sid}/now-playing   -> now-playing metadata
  GET <secret>/radio/{sid}/cover       -> local cover image (or fallback icon)
  GET <secret>/radio/{sid}/stream      -> proxied audio stream

The API never exposes upstream URLs, fallback URLs, headers, notes or any
internal configuration. Unknown or disabled station IDs return the same
generic 404 as anything else, so nothing can be probed.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import stream_proxy, utils
from .auth import plain_response
from .config import COVER_EXTENSIONS

log = logging.getLogger("radio_proxy.routes")

# Explicit whitelist: no user-controlled filesystem mapping at all.
ASSETS = {
    "app.js": "application/javascript",
    "theme.js": "application/javascript",
    "style.css": "text/css",
    "fallback-cover.svg": "image/svg+xml",
}


def _station_or_404(request: web.Request):
    ctx = request.app["ctx"]
    sid = request.match_info.get("sid", "")
    station = ctx.cfg.stations_by_id.get(sid)
    if station is None or not station.enabled:
        return None
    return station


# ---------------------------------------------------------------- frontend


async def handle_index(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    path = ctx.cfg.paths.static_dir / "index.html"
    if not path.is_file():
        log.error("static/index.html missing (base_dir=%s)", ctx.base_dir)
        return plain_response(500, "Server misconfigured")
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def handle_asset(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    name = request.match_info["name"]
    ctype = ASSETS.get(name)
    if ctype is None:
        return plain_response(404, "Not Found")
    try:
        path = utils.safe_join(ctx.cfg.paths.static_dir, name)
    except ValueError:
        return plain_response(404, "Not Found")
    if not path.is_file():
        return plain_response(404, "Not Found")
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# -------------------------------------------------------------------- API


async def handle_api_stations(request: web.Request) -> web.Response:
    ctx = request.app["ctx"]
    stations_out = []
    for station in ctx.cfg.stations:
        if not station.enabled:
            continue
        ctx.metadata.maybe_schedule_refresh(station)
        np = ctx.metadata.snapshot_response(station)
        stations_out.append({
            "id": station.id,
            "name": station.name,
            "codec": station.codec,
            "bitrate_kbps": station.bitrate_kbps,
            "status": ctx.metadata.status(station.id),  # online/offline/unknown
            "cover_url": f"radio/{station.id}/cover",
            "stream_path": f"radio/{station.id}/stream",
            "now_playing": {
                "display": np["display"],
                "source": np["source"],
                "updated_at": np["updated_at"],
            },
        })
    resp = web.json_response({
        "stations": stations_out,
        "server_time": utils.utc_now_iso(),
        # Play bar settings for the frontend (session track history etc.).
        # Small + public-safe: just the two numeric limits, nothing internal.
        "player": {
            "history_limit": ctx.cfg.player.history_limit,
            "history_visible_items": ctx.cfg.player.history_visible_items,
        },
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def handle_now_playing(request: web.Request) -> web.Response:
    station = _station_or_404(request)
    if station is None:
        return plain_response(404, "Not Found")
    ctx = request.app["ctx"]
    ctx.metadata.maybe_schedule_refresh(station)
    resp = web.json_response(ctx.metadata.snapshot_response(station))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ covers


async def handle_cover(request: web.Request) -> web.StreamResponse:
    station = _station_or_404(request)
    if station is None:
        return plain_response(404, "Not Found")
    ctx = request.app["ctx"]

    if station.cover:
        try:
            path = utils.safe_join(ctx.base_dir, station.cover)
            covers_root = ctx.cfg.paths.covers_dir.resolve()
            if covers_root not in path.parents or not path.is_file():
                raise ValueError("cover outside covers dir or missing")
            if path.suffix.lower() not in COVER_EXTENSIONS:
                raise ValueError("unsupported cover extension")
        except ValueError:
            path = None
    else:
        path = None

    if path is not None:
        resp = web.FileResponse(path)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    # Built-in fallback icon when no cover is configured/present.
    fallback = ctx.cfg.paths.static_dir / "fallback-cover.svg"
    if fallback.is_file():
        resp = web.FileResponse(fallback)
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp
    return plain_response(404, "Not Found")


# ------------------------------------------------------------------ stream


async def handle_stream(request: web.Request) -> web.StreamResponse:
    station = _station_or_404(request)
    if station is None:
        return plain_response(404, "Not Found")
    ctx = request.app["ctx"]

    from .auth import client_ip  # local import avoids a cycle
    ip = client_ip(request)
    ok, slots = stream_proxy.acquire_stream_slots(ctx, station, ip)
    if not ok:
        log.info("RATE_LIMITED ip=%s scope=stream station=%s", ip, station.id)
        return plain_response(429, "Too Many Requests")
    try:
        return await stream_proxy.proxy_stream(request, ctx, station)
    finally:
        stream_proxy.release_stream_slots(ctx, slots)


# ----------------------------------------------------------------- wiring


def build_routes(app: web.Application) -> None:
    ctx = app["ctx"]
    sp = ctx.cfg.server.secret_path  # starts and ends with '/'

    # NOTE: /api/stations is registered before /api/{sid}/now-playing so the
    # literal route wins over the variable one.
    app.router.add_get(sp, handle_index, name="index")
    app.router.add_get(sp + "assets/{name}", handle_asset, name="asset")
    app.router.add_get(sp + "api/stations", handle_api_stations, name="api_stations")
    app.router.add_get(sp + "api/{sid}/now-playing", handle_now_playing, name="api_now_playing")
    app.router.add_get(sp + "radio/{sid}/cover", handle_cover, name="cover")
    app.router.add_get(sp + "radio/{sid}/stream", handle_stream, name="stream", allow_head=False)
