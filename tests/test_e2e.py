"""End-to-end test: full HTTP flow through the real application with a fake
ICY upstream. Covers auth, cookie, API shape, streaming, metadata capture,
404-stealth behaviour and rate limiting."""
import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

from private_radio_proxy import config as config_mod
from private_radio_proxy.app import build_application

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRET = "/x9k2m-p7qz4-v8n1r-t3w6/"
TOKEN = "e2e_" + "K" * 40

ICY_METAINT = 32
AUDIO = bytes(range(256)) * 4


def meta_block(title: str) -> bytes:
    raw = f"StreamTitle='{title}';".encode()
    pad = (-len(raw)) % 16
    total = raw + b"\x00" * pad
    return bytes([len(total) // 16]) + total  # length byte counts 16-byte units


async def upstream_handler(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(status=200)
    resp.content_type = "audio/mpeg"
    resp.headers["icy-metaint"] = str(ICY_METAINT)
    await resp.prepare(request)
    for _ in range(100):
        # ICY layout: exactly metaint audio bytes, then one metadata block.
        await resp.write(AUDIO[:ICY_METAINT] + meta_block("E2E Artist - E2E Song"))
        await asyncio.sleep(0.01)
    return resp


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.upstream = TestServer(web.Application())  # placeholder, replaced below
        await self.upstream.close()
        self.upstream = TestServer(self._upstream_app())
        await self.upstream.start_server()
        self._write_config()
        static = self.base / "static"
        static.mkdir(exist_ok=True)
        for name in ("index.html", "app.js", "style.css", "fallback-cover.svg"):
            shutil.copy(PROJECT_ROOT / "static" / name, static / name)
        cfg = config_mod.load_config(self.base)
        self.app = build_application(cfg, self.base)
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.base_url = str(self.server.make_url(""))
        self.sp = SECRET

    async def asyncTearDown(self):
        await self.server.close()
        await self.upstream.close()
        self.tmp.cleanup()

    @staticmethod
    def _upstream_app() -> web.Application:
        app = web.Application()
        app.router.add_get("/live.mp3", upstream_handler)
        return app

    def _write_config(self):
        (self.base / "config").mkdir(exist_ok=True)
        app_cfg = {
            "server": {"host": "127.0.0.1", "port": 54391, "secret_path": SECRET},
            "security": {"allow_private_upstreams": True,
                         "max_failed_token_attempts_per_ip_per_minute": 100},
            "limits": {"max_requests_per_ip_per_minute": 10000,
                       "stream_chunk_size_bytes": 8192},
            "logging": {"level": "WARNING", "mask_tokens": True},
        }
        stations_cfg = {"stations": [
            {"id": "st0001", "name": "E2E Radio", "enabled": True,
             "stream_url": f"http://127.0.0.1:{self.upstream.port}/live.mp3",
             "fallback_stream_urls": [], "codec": "mp3", "bitrate_kbps": 128,
             "cover": None,
             "metadata": {"type": "icy", "cache_ttl_seconds": 45},
             "upstream_headers": {"User-Agent": "E2E/1.0"}, "notes": "internal"},
            {"id": "st0002", "name": "Disabled", "enabled": False,
             "stream_url": f"http://127.0.0.1:{self.upstream.port}/live.mp3",
             "fallback_stream_urls": [], "codec": "mp3", "bitrate_kbps": 128,
             "cover": None, "metadata": {"type": "none"},
             "upstream_headers": {}, "notes": ""},
        ]}
        tokens_cfg = {"tokens": [{
            "key_id": "t1", "name": "owner",
            "token_hash": hashlib.sha256(TOKEN.encode()).hexdigest(),
            "enabled": True, "created_at": "2026-01-01T00:00:00Z",
            "expires_at": None, "last_used_at": None}]}
        (self.base / "config" / "app.json").write_text(json.dumps(app_cfg))
        (self.base / "config" / "stations.json").write_text(json.dumps(stations_cfg))
        (self.base / "config" / "tokens.json").write_text(json.dumps(tokens_cfg))

    # ---------------------------------------------------------------- tests

    async def test_full_flow(self):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as http:
            base, sp = self.base_url, self.sp

            # 1. outside secret path -> stealth 404
            r = await http.get(base + "/")
            self.assertEqual(r.status, 404)
            r.release()

            # 2. secret path without / with bad token -> 404
            for url in (base + sp, base + sp + "?token=" + quote("badtoken" * 5)):
                r = await http.get(url)
                self.assertEqual(r.status, 404)
                r.release()

            # 3. valid token -> page + cookie
            r = await http.get(base + sp + "?token=" + TOKEN)
            self.assertEqual(r.status, 200)
            set_cookies = r.headers.getall("Set-Cookie", [])
            self.assertTrue(any("rp_session=" in c for c in set_cookies))
            self.assertTrue(any("HttpOnly" in c for c in set_cookies))
            self.assertIn("Private Radio", await r.text())
            r.release()

            # 4. api/stations with cookie: shape + no leaks
            r = await http.get(base + sp + "api/stations")
            self.assertEqual(r.status, 200)
            payload = await r.json()
            self.assertEqual([s["id"] for s in payload["stations"]], ["st0001"])
            # play bar settings for the frontend (session history limits)
            self.assertEqual(payload["player"]["history_limit"], 30)
            self.assertEqual(payload["player"]["history_visible_items"], 15)
            blob = json.dumps(payload)
            self.assertNotIn("stream_url", blob)
            self.assertNotIn("upstream.example", blob)
            self.assertNotIn("notes", blob)
            self.assertNotIn(str(self.upstream.port), blob)
            r.release()

            # 5. station stream -> audio + ICY metadata captured
            r = await http.get(base + sp + "radio/st0001/stream")
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers.get("Content-Type", "").startswith("audio/mpeg"))
            total = b""
            while len(total) < 4096:
                chunk = await r.content.read(1024)
                if not chunk:
                    break
                total += chunk
            self.assertGreaterEqual(len(total), 4096)
            r.release()

            r = await http.get(base + sp + "api/st0001/now-playing")
            np = await r.json()
            r.release()
            self.assertEqual(np["station_id"], "st0001")
            self.assertEqual(np["source"], "icy")
            self.assertEqual(np["title"], "E2E Song")
            self.assertEqual(np["artist"], "E2E Artist")
            self.assertEqual(np["display"], "E2E Artist - E2E Song")
            self.assertTrue(np["online"])

            # 6. unknown + disabled stations -> identical 404
            for sid in ("zz9999", "st0002"):
                r = await http.get(base + sp + f"api/{sid}/now-playing")
                self.assertEqual(r.status, 404)
                r.release()

            # 7. no open proxy route even with valid auth
            r = await http.get(base + sp + "proxy?url=http://example.com/")
            self.assertEqual(r.status, 404)
            r.release()

            # 8. cover falls back to built-in svg
            r = await http.get(base + sp + "radio/st0001/cover")
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers.get("Content-Type", "").startswith("image/svg+xml"))
            r.release()

            # 9. assets reachable (behind secret path, token-free by design)
            r = await http.get(base + sp + "assets/style.css")
            self.assertEqual(r.status, 200)
            r.release()

    async def test_security_headers_and_csp(self):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as http:
            r = await http.get(self.base_url + self.sp + "?token=" + TOKEN)
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer")
            self.assertIn("default-src 'self'", r.headers.get("Content-Security-Policy", ""))
            r.release()

    async def test_failed_auth_rate_limit(self):
        # dedicated app instance with a tiny auth-fail budget
        app_cfg_path = self.base / "config" / "app.json"
        cfg_data = json.loads(app_cfg_path.read_text())
        cfg_data["security"]["max_failed_token_attempts_per_ip_per_minute"] = 3
        cfg_data["limits"]["max_requests_per_ip_per_minute"] = 10000
        app_cfg_path.write_text(json.dumps(cfg_data))
        cfg = config_mod.load_config(self.base)
        app = build_application(cfg, self.base)
        server = TestServer(app)
        await server.start_server()
        try:
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as http:
                seen = []
                for _ in range(8):
                    r = await http.get(str(server.make_url("")) + self.sp + "?token=wrong")
                    seen.append(r.status)
                    r.release()
                self.assertEqual(seen[0], 404)
                self.assertIn(429, seen)
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
