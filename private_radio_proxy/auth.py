"""Authentication and request gating.

Request flow (middlewares, outermost first):
  1. error/security-headers middleware  (app.py)
  2. rate-limit middleware              (app.py)
  3. auth middleware                    (this module)

Auth rules:
  * anything OUTSIDE the secret path -> plain 404 (stealth: the service does
    not even confirm it exists);
  * static assets under <secret>/assets/ are exempt from tokens - they sit
    behind the secret path and contain no secrets (no URLs, no config);
  * everything else needs a valid token, taken from the query string first
    (?token=...) and from the HttpOnly cookie as fallback;
  * on success via query token the server sets an HttpOnly cookie scoped to
    the secret path, so audio elements and later requests use clean URLs;
  * failures are rate-limited per IP and logged as 'AUTH_FAIL ip=...' lines
    for fail2ban; the response is a bare 404 so station existence and path
    validity are never confirmed to an attacker.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import utils

log = logging.getLogger("radio_proxy.auth")

_MAX_AGE_SECONDS = 180 * 24 * 3600  # cookie lifetime; token expiry still applies


def client_ip(request: web.Request) -> str:
    """Client IP for logging/limits. Direct connections only, unless
    security.trust_proxy_headers is enabled in app.json."""
    cfg = request.app["ctx"].cfg
    if cfg.security.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip() or request.remote
        real = request.headers.get("X-Real-IP")
        if real:
            return real.strip()
    return request.remote or "unknown"


def plain_response(status: int, text: str) -> web.Response:
    return web.Response(status=status, text=text, content_type="text/plain",
                        headers={"Cache-Control": "no-store"})


def attach_cookie_if_needed(request: web.Request, response: web.StreamResponse, cfg) -> None:
    """Set the session cookie when the request authenticated via query token
    and the cookie is not already present. Safe for prepared responses."""
    if request.get("auth_via") != "query":
        return
    if request.cookies.get(cfg.security.token_cookie_name):
        return
    if getattr(response, "prepared", False):
        return
    try:
        response.set_cookie(
            cfg.security.token_cookie_name,
            request["auth_token_raw"],
            max_age=_MAX_AGE_SECONDS,
            path=cfg.server.secret_path,
            httponly=cfg.security.cookie_httponly,
            secure=cfg.security.cookie_secure,
            samesite=cfg.security.cookie_samesite,
        )
    except Exception:  # pragma: no cover - never break a response over a cookie
        log.debug("could not set session cookie", exc_info=True)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    ctx = request.app["ctx"]
    cfg = ctx.cfg
    secret_path = cfg.server.secret_path

    # 1. Everything outside the secret path: stealth 404.
    if not request.path.startswith(secret_path):
        return plain_response(404, "Not Found")

    # 2. Normalize method: only GET (and HEAD for the router) exist at all.
    if request.method not in ("GET", "HEAD"):
        return plain_response(404, "Not Found")

    # 3. Static assets: behind the secret path, no token required.
    relative = request.path[len(secret_path):]
    if relative.startswith("assets/"):
        return await handler(request)

    # 4. Token check: query string first, then cookie.
    raw_q = request.query.get("token")
    raw_c = request.cookies.get(cfg.security.token_cookie_name)
    entry = None
    via = None
    raw = None
    if raw_q:
        entry = ctx.tokens.validate(raw_q)
        if entry is not None:
            via, raw = "query", raw_q
    if entry is None and raw_c:
        entry = ctx.tokens.validate(raw_c)
        if entry is not None:
            via, raw = "cookie", raw_c

    if entry is None:
        ip = client_ip(request)
        allowed = ctx.auth_fail.allow(
            "authfail:" + ip,
            cfg.security.max_failed_token_attempts_per_ip_per_minute,
            60.0,
        )
        if not allowed:
            log.warning("RATE_LIMITED ip=%s scope=auth path=%s",
                        ip, utils.mask_query_tokens(request.path_qs, cfg.security.token_cookie_name))
            return plain_response(429, "Too Many Requests")
        log.info("AUTH_FAIL ip=%s reason=%s path=%s",
                 ip,
                 "missing_token" if not raw else "invalid_or_expired_token",
                 utils.mask_query_tokens(request.path_qs, cfg.security.token_cookie_name))
        # 404 on purpose: do not reveal that the secret path exists.
        return plain_response(404, "Not Found")

    # 5. Success: stash auth info for handlers, run the route.
    request["auth_entry"] = entry
    request["auth_token_raw"] = raw
    request["auth_via"] = via

    response = await handler(request)
    attach_cookie_if_needed(request, response, cfg)
    return response
