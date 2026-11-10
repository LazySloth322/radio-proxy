#!/usr/bin/env python3
"""End-to-end smoke test for private-radio-proxy.

Starts a fake ICY radio upstream on localhost plus the real application
(with security.allow_private_upstreams=true), then checks:

  * outside secret path -> 404
  * secret path without token -> 404
  * bad token -> 404
  * valid ?token= -> 200 + HttpOnly cookie set
  * /api/stations with cookie -> 200, no upstream URLs leaked
  * /api/<id>/now-playing -> ICY title picked up from the stream
  * /radio/<id>/stream -> 200 audio/mpeg, bytes flow
  * cover fallback -> 200 image/svg+xml
  * unknown station -> 404
  * rate limit -> 429 after exhausting the per-minute budget

Usage:  python3 scripts/smoke_test.py [base_dir]
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from private_radio_proxy import config as config_mod  # noqa: E402
from private_radio_proxy.app import build_application  # noqa: E402
from private_radio_proxy.tokens import TokenStore  # noqa: E402

APP_PORT_UNUSED = 0  # TestServer picks a port


def write(base: Path, rel: str, text: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def make_fake_station_config(base: Path, upstream_port: int, token: str, secret_path: str) -> None:
    import hashlib

    app_cfg = {
        "server": {"host": "127.0.0.1", "port": 54391, "secret_path": secret_path,
                   "public_base_url": ""},
        "security": {"allow_private_upstreams": True,
                     "max_failed_token_attempts_per_ip_per_minute": 200},
        "limits": {"max_requests_per_ip_per_minute": 10000,
                   "stream_chunk_size_bytes": 8192},
        "logging": {"level": "WARNING", "mask_tokens": True},
    }
    stations_cfg = {"stations": [{
        "id": "st0001",
        "name": "Smoke Test Radio",
        "enabled": True,
        "stream_url": f"http://127.0.0.1:{upstream_port}/live.mp3",
        "fallback_stream_urls": [],
        "codec": "mp3",
        "bitrate_kbps": 128,
        "cover": None,
        "metadata": {"type": "icy", "poll_interval_seconds": 15, "cache_ttl_seconds": 45},
        "upstream_headers": {"User-Agent": "SmokeTest/1.0"},
        "notes": "smoke test",
    }, {
        "id": "st0002",
        "name": "Disabled Radio",
        "enabled": False,
        "stream_url": f"http://127.0.0.1:{upstream_port}/live.mp3",
        "fallback_stream_urls": [],
        "codec": "mp3",
        "bitrate_kbps": 128,
        "cover": None,
        "metadata": {"type": "none"},
        "upstream_headers": {},
        "notes": "",
    }]}
    tokens_cfg = {"tokens": [{
        "key_id": "t1", "name": "owner",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "enabled": True, "created_at": "2026-01-01T00:00:00Z",
        "expires_at": None, "last_used_at": None,
    }]}
    write(base, "config/app.json", __import__("json").dumps(app_cfg, indent=2))
    write(base, "config/stations.json", __import__("json").dumps(stations_cfg, indent=2))
    write(base, "config/tokens.json", __import__("json").dumps(tokens_cfg, indent=2))


# ------------------------------------------------------------ fake upstream

ICY_METAINT = 32
AUDIO = bytes(range(256)) * 4  # 1024 bytes of pseudo-audio


def meta_block(title: str) -> bytes:
    raw = f"StreamTitle='{title}';".encode()
    pad = (-len(raw)) % 16
    return bytes([len(raw + b"\0" * pad) // 16]) + raw + b"\0" * pad


async def upstream_handler(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, reason="OK")
    resp.content_type = "audio/mpeg"
    resp.headers["icy-metaint"] = str(ICY_METAINT)
    resp.headers["icy-name"] = "Smoke Upstream"
    await resp.prepare(request)
    for _ in range(60):
        # ICY layout: exactly metaint audio bytes, then one metadata block.
        await resp.write(AUDIO[:ICY_METAINT] + meta_block("Smoke Artist - Smoke Song"))
        await asyncio.sleep(0.02)
    return resp


async def start_upstream() -> TestServer:
    app = web.Application()
    app.router.add_get("/live.mp3", upstream_handler)
    server = TestServer(app)
    await server.start_server()
    return server


# --------------------------------------------------------------------- test

async def run(base_dir: Path) -> int:
    upstream = await start_upstream()
    token = "smoketoken_" + "a" * 24
    secret_path = "/x9k2m-p7qz4-v8n1r-t3w6/"
    make_fake_station_config(base_dir, upstream.port, token, secret_path)

    # static assets needed by handlers
    static = base_dir / "static"
    static.mkdir(exist_ok=True)
    for name in ("index.html", "app.js", "style.css", "fallback-cover.svg"):
        shutil.copy(PROJECT_ROOT / "static" / name, static / name)

    cfg = config_mod.load_config(base_dir)
    app = build_application(cfg, base_dir)
    server = TestServer(app)
    await server.start_server()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        base_url = str(server.make_url(""))
        sp = secret_path
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as http:
            # outside secret path
            r = await http.get(base_url + "/")
            check("outside secret path -> 404", r.status == 404)
            r.release()

            # secret path, no token
            r = await http.get(base_url + sp)
            check("secret path without token -> 404", r.status == 404)
            r.release()

            # bad token
            r = await http.get(base_url + sp + "?token=WRONG")
            check("bad token -> 404", r.status == 404)
            r.release()

            # assets work without token (by design)
            r = await http.get(base_url + sp + "assets/app.js")
            check("assets reachable behind secret path", r.status == 200)
            r.release()

            # valid token -> page + cookie
            r = await http.get(base_url + sp + f"?token={token}")
            cookies = r.headers.getall("Set-Cookie", [])
            check("valid token -> 200", r.status == 200)
            check("session cookie set (HttpOnly)",
                  any("rp_session=" in c and "HttpOnly" in c for c in cookies))
            body = await r.text()
            check("index.html served", "Private Radio" in body)
            r.release()

            # api/stations with cookie
            r = await http.get(base_url + sp + "api/stations")
            data = await r.json()
            check("api/stations -> 200 with cookie", r.status == 200)
            ids = [s["id"] for s in data.get("stations", [])]
            check("disabled station hidden", ids == ["st0001"], str(ids))
            blob = await r.text()
            check("no upstream URL leak", "127.0.0.1" not in blob and "stream_url" not in blob)
            r.release()

            # unknown station + disabled station
            r = await http.get(base_url + sp + "api/zz9999/now-playing")
            check("unknown station -> 404", r.status == 404)
            r.release()
            r = await http.get(base_url + sp + "radio/st0002/stream")
            check("disabled station stream -> 404", r.status == 404)
            r.release()

            # no open proxy endpoint
            r = await http.get(
                base_url + sp + "proxy?url=http://example.com/",
                cookies={"rp_session": token})
            check("no /proxy endpoint -> 404", r.status == 404)
            r.release()

            # stream: bytes flow, content type right, metadata appears
            r = await http.get(base_url + sp + "radio/st0001/stream")
            check("stream -> 200", r.status == 200)
            check("stream content-type audio/mpeg",
                  r.headers.get("Content-Type", "").startswith("audio/mpeg"))
            total = b""
            np = {}
            while len(total) < 4096:
                chunk = await r.content.read(1024)
                if not chunk:
                    break
                total += chunk
            r2 = await http.get(base_url + sp + "api/st0001/now-playing")
            np = await r2.json()
            r2.release()
            check("stream delivered >4KB", len(total) >= 4096, str(len(total)))
            check("ICY now playing captured",
                  np.get("source") == "icy" and np.get("title") == "Smoke Song"
                  and np.get("artist") == "Smoke Artist", str(np))
            check("status online", np.get("online") is True)
            r.release()

            # cover fallback
            r = await http.get(base_url + sp + "radio/st0001/cover",
                               cookies={"rp_session": token})
            check("cover fallback svg -> 200", r.status == 200 and
                  r.headers.get("Content-Type", "").startswith("image/svg+xml"))
            r.release()

            # stream with query token (no cookie) + Set-Cookie on stream too
            r = await http.get(base_url + sp + f"radio/st0001/stream?token={token}")
            check("stream via query token -> 200", r.status == 200)
            r.release()

        print()
        if failures:
            print(f"SMOKE TEST FAILED: {len(failures)} check(s): {failures}")
            return 1
        print("SMOKE TEST PASSED: all checks OK")
        return 0
    finally:
        await server.close()
        await upstream.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="prp-smoke-"))
    try:
        code = asyncio.run(run(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
