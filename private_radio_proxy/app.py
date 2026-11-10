"""Application factory, background tasks and server entry point.

Memory notes:
  * aiohttp server + client, three streams and periodic metadata polling
    typically stay around 40-70 MB RSS; systemd MemoryMax=180M is the guard
    rail (see README "Memory tuning").
  * One shared ClientSession for all upstream work (connection pool capped).
  * Access logging is done minimally in middleware with token masking
    (aiohttp's default access logger is disabled).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__, config as config_mod, routes as routes_mod
from .auth import auth_middleware
from .config import AppConfig
from .limits import ConcurrencyLimiter, SlidingWindowRateLimiter
from .logging_utils import get_logger, setup_logging
from .metadata import MetadataManager
from .tokens import TokenStore
from . import utils

log = get_logger("main")

TOKEN_FLUSH_INTERVAL = 60.0
LIMITER_PRUNE_INTERVAL = 300.0


@dataclass
class AppContext:
    cfg: AppConfig
    base_dir: Path
    tokens: TokenStore
    metadata: MetadataManager
    rate: SlidingWindowRateLimiter = field(default_factory=SlidingWindowRateLimiter)
    auth_fail: SlidingWindowRateLimiter = field(default_factory=SlidingWindowRateLimiter)
    stream_conc: ConcurrencyLimiter = field(default_factory=ConcurrencyLimiter)
    session: aiohttp.ClientSession | None = None
    bg_tasks: list[asyncio.Task] = field(default_factory=list)

    def resolve(self, relative) -> Path:
        return config_mod.resolve_path(self.base_dir, relative)


# --------------------------------------------------------------- middlewares


@web.middleware
async def error_and_headers_middleware(request: web.Request, handler):
    """Uniform security headers + generic 500s (no stack traces to clients)."""
    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except (asyncio.CancelledError, ConnectionResetError):
        raise
    except Exception:  # noqa: BLE001
        log.exception("unhandled error path=%s", request.path)
        response = web.Response(status=500, text="Internal Server Error",
                                content_type="text/plain")
    headers = getattr(response, "headers", None)
    if headers is not None:
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
    return response


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """Per-IP request rate limit for everything under the secret path."""
    ctx = request.app["ctx"]
    cfg = ctx.cfg
    if not request.path.startswith(cfg.server.secret_path):
        return await handler(request)  # outside path: auth middleware 404s it
    from .auth import client_ip
    ip = client_ip(request)
    ctx.rate.maybe_prune(60.0)
    if not ctx.rate.allow("req:" + ip, cfg.limits.max_requests_per_ip_per_minute, 60.0):
        log.warning("RATE_LIMITED ip=%s scope=requests path=%s", ip, request.path)
        return web.Response(status=429, text="Too Many Requests", content_type="text/plain",
                            headers={"Cache-Control": "no-store"})
    return await handler(request)


# ---------------------------------------------------------- app construction


def build_application(cfg: AppConfig, base_dir: Path | None = None) -> web.Application:
    base_dir = Path(base_dir).resolve() if base_dir else cfg.base_dir

    # Resolve configured paths against the base directory.
    cfg.paths.static_dir = cfg.resolve(cfg.paths.static_dir)
    cfg.paths.covers_dir = cfg.resolve(cfg.paths.covers_dir)
    cfg.paths.logs_dir = cfg.resolve(cfg.paths.logs_dir)

    tokens = TokenStore(cfg.config_dir / config_mod.TOKENS_CONFIG_NAME)
    metadata = MetadataManager(cfg.stations_by_id, cfg.limits, cfg.security)
    ctx = AppContext(cfg=cfg, base_dir=base_dir, tokens=tokens, metadata=metadata)

    app = web.Application(middlewares=[
        error_and_headers_middleware,
        rate_limit_middleware,
        auth_middleware,
    ], client_max_size=64 * 1024)
    app["ctx"] = ctx

    async def on_startup(app: web.Application) -> None:
        connector = aiohttp.TCPConnector(
            limit=20,               # total upstream connections
            limit_per_host=4,       # per upstream host
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
        ctx.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=None),
            headers={"User-Agent": utils.DEFAULT_USER_AGENT},
        )
        metadata.attach_session(ctx.session)
        metadata.start_background()

        async def token_flush_loop() -> None:
            while True:
                await asyncio.sleep(TOKEN_FLUSH_INTERVAL)
                try:
                    await ctx.tokens.flush_if_dirty()
                except Exception:  # noqa: BLE001
                    log.exception("token flush failed")

        async def limiter_prune_loop() -> None:
            while True:
                await asyncio.sleep(LIMITER_PRUNE_INTERVAL)
                ctx.rate.prune_idle(60.0)
                ctx.auth_fail.prune_idle(60.0)
                ctx.stream_conc.prune()

        ctx.bg_tasks.append(asyncio.create_task(token_flush_loop()))
        ctx.bg_tasks.append(asyncio.create_task(limiter_prune_loop()))
        log.info("started stations=%d listen=%s:%d",
                 len(cfg.stations), cfg.server.host, cfg.server.port)

    async def on_cleanup(app: web.Application) -> None:
        for task in ctx.bg_tasks:
            task.cancel()
        if ctx.bg_tasks:
            await asyncio.gather(*ctx.bg_tasks, return_exceptions=True)
        await metadata.stop()
        try:
            await ctx.tokens.flush_if_dirty()
        except Exception:  # noqa: BLE001
            log.exception("final token flush failed")
        if ctx.session is not None:
            await ctx.session.close()
        log.info("shutdown complete")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    routes_mod.build_routes(app)
    return app


# --------------------------------------------------------------------- main


def run_server(base_dir: Path, host: str | None = None, port: int | None = None,
               config_dir: Path | None = None) -> None:
    cfg = config_mod.load_config(Path(base_dir), config_dir=config_dir)
    setup_logging(cfg.resolve(cfg.paths.logs_dir), cfg.logging.level,
                  cfg.logging.mask_tokens, cfg.security.token_cookie_name)
    # Re-resolve paths now that logs dir exists (idempotent).
    host = host or cfg.server.host
    port = port or cfg.server.port
    app = build_application(cfg, base_dir)
    log.info("private-radio-proxy v%s starting on %s:%d", __version__, host, port)
    if cfg.warnings:
        for warning in cfg.warnings:
            log.warning("config warning: %s", warning)
    web.run_app(app, host=host, port=port, access_log=None,
                print=lambda *_a, **_k: None, shutdown_timeout=10.0)


def default_base_dir() -> Path:
    env = os.environ.get("PRP_BASE_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m private_radio_proxy",
        description="private-radio-proxy server (see 'python -m private_radio_proxy.cli' for management tools)",
    )
    parser.add_argument("--base-dir", default=None,
                        help="project directory containing config/ (default: package parent or $PRP_BASE_DIR)")
    parser.add_argument("--host", default=None, help="override server.host from app.json")
    parser.add_argument("--port", type=int, default=None, help="override server.port from app.json")
    parser.add_argument("--config-dir", default=None, help="override config directory")
    args = parser.parse_args(argv)
    base = Path(args.base_dir).resolve() if args.base_dir else default_base_dir()
    try:
        run_server(base, host=args.host, port=args.port,
                   config_dir=Path(args.config_dir) if args.config_dir else None)
    except config_mod.ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
