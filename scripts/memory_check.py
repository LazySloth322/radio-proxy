#!/usr/bin/env python3
"""Memory footprint check: run the app with 3 concurrent streams and report RSS."""
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

from private_radio_proxy import config as config_mod
from private_radio_proxy.app import build_application
from private_radio_proxy.logging_utils import setup_logging

SECRET = "/x9k2m-p7qz4-v8n1r-t3w6/"
TOKEN = "mem_" + "K" * 40
ICY = 16384
AUDIO = bytes(range(256)) * 256  # 64KB pseudo-audio
TARGET_BYTES = 300_000           # per listener (~15 s at 128 kbps equivalent)


def mb(title):
    raw = f"StreamTitle='{title}';".encode()
    pad = (-len(raw)) % 16
    tot = raw + b"\0" * pad
    return bytes([len(tot) // 16]) + tot


async def upstream(request):
    r = web.StreamResponse()
    r.content_type = "audio/mpeg"
    r.headers["icy-metaint"] = str(ICY)
    await r.prepare(request)
    while True:
        await r.write(AUDIO[:ICY] + mb("Mem Artist - Mem Song"))
        await asyncio.sleep(0.03)
    return r


async def main():
    tmp = Path(tempfile.mkdtemp())
    up_app = web.Application()
    up_app.router.add_get("/live.mp3", upstream)
    ups = TestServer(up_app)
    await ups.start_server()

    (tmp / "config").mkdir()
    static = tmp / "static"
    static.mkdir()
    for n in ("index.html", "app.js", "style.css", "fallback-cover.svg"):
        shutil.copy(Path(__file__).resolve().parent.parent / "static" / n, static / n)

    app_cfg = {"server": {"host": "127.0.0.1", "port": 54391, "secret_path": SECRET},
               "security": {"allow_private_upstreams": True},
               "limits": {"stream_chunk_size_bytes": 16384},
               "logging": {"level": "WARNING"}}
    st = {"stations": [{"id": "st0001", "name": "Mem", "enabled": True,
                        "stream_url": f"http://127.0.0.1:{ups.port}/live.mp3",
                        "fallback_stream_urls": [], "codec": "mp3", "bitrate_kbps": 128,
                        "cover": None, "metadata": {"type": "icy"},
                        "upstream_headers": {}}]}
    tk = {"tokens": [{"key_id": "t1", "name": "o",
                      "token_hash": hashlib.sha256(TOKEN.encode()).hexdigest(),
                      "enabled": True, "created_at": "2026-01-01T00:00:00Z",
                      "expires_at": None, "last_used_at": None}]}
    (tmp / "config/app.json").write_text(json.dumps(app_cfg))
    (tmp / "config/stations.json").write_text(json.dumps(st))
    (tmp / "config/tokens.json").write_text(json.dumps(tk))

    cfg = config_mod.load_config(tmp)
    setup_logging(tmp / "logs", "WARNING", True, "rp_session")
    app = build_application(cfg, tmp)
    srv = TestServer(app)
    await srv.start_server()
    base = str(srv.make_url(""))

    def rss_kb():
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1])
        return -1

    rss_idle = rss_kb()
    results = {}
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as http:
        r = await http.get(f"{base}{SECRET}?token={TOKEN}")
        assert r.status == 200
        streams = [await http.get(f"{base}{SECRET}radio/st0001/stream") for _ in range(3)]
        for s in streams:
            assert s.status == 200, s.status

        async def drain(s):
            got = 0
            while got < TARGET_BYTES:
                c = await s.content.read(16384)
                if not c:
                    break
                got += len(c)
            return got

        got = await asyncio.gather(*[drain(s) for s in streams])
        results = got
        rss_loaded = rss_kb()
        for s in streams:
            s.release()
        np = await (await http.get(f"{base}{SECRET}api/st0001/now-playing")).json()
        await r.release()
    await srv.close()
    await ups.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"RSS idle  : {rss_idle/1024:.1f} MB")
    print(f"RSS loaded: {rss_loaded/1024:.1f} MB (3 concurrent listeners)")
    print(f"bytes delivered per listener: {[f'{g/1000:.0f} kB' for g in results]}")
    print(f"now playing: {np.get('display')} (source={np.get('source')})")
    ok = rss_loaded < 120 * 1024
    print("MEMORY CHECK:", "PASS (<120 MB)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
