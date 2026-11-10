"""Station/app config validation tests."""
import json
import tempfile
import unittest
from pathlib import Path

from private_radio_proxy import config

SECRET = "/x9k2m-p7qz4-v8n1r-t3w6/"

VALID_APP = {
    "server": {"host": "0.0.0.0", "port": 54391, "secret_path": SECRET},
    "security": {"allow_private_upstreams": False},
    "limits": {},
    "logging": {"level": "INFO"},
}

VALID_STATION = {
    "id": "s7k2m9",
    "name": "Example Radio",
    "enabled": True,
    "stream_url": "https://upstream.example.com/live.mp3",
    "fallback_stream_urls": ["https://mirror.example.com/live.mp3"],
    "codec": "mp3",
    "bitrate_kbps": 128,
    "cover": "covers/s7k2m9.jpg",
    "metadata": {"type": "icy", "url": None, "poll_interval_seconds": 15,
                 "cache_ttl_seconds": 45},
    "upstream_headers": {"User-Agent": "PrivateRadioProxy/1.0"},
    "notes": "",
}


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data) -> None:
        (self.base / "config").mkdir(exist_ok=True)
        (self.base / "config" / name).write_text(json.dumps(data), encoding="utf-8")

    def load(self):
        return config.load_config(self.base)

    def test_valid_example_loads(self):
        self.write("app.json", VALID_APP)
        self.write("stations.json", {"stations": [VALID_STATION]})
        cfg = self.load()
        self.assertEqual(len(cfg.stations), 1)
        self.assertEqual(cfg.stations[0].id, "s7k2m9")
        self.assertEqual(cfg.server.port, 54391)

    def test_duplicate_station_ids_rejected(self):
        self.write("app.json", VALID_APP)
        self.write("stations.json", {"stations": [VALID_STATION, VALID_STATION]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("duplicate", str(ctx.exception))

    def test_bad_metadata_type_rejected(self):
        self.write("app.json", VALID_APP)
        st = dict(VALID_STATION, metadata={"type": "shazam"})
        self.write("stations.json", {"stations": [st]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("metadata.type", str(ctx.exception))

    def test_json_metadata_requires_url(self):
        self.write("app.json", VALID_APP)
        st = dict(VALID_STATION, metadata={"type": "json", "url": None})
        self.write("stations.json", {"stations": [st]})
        with self.assertRaises(config.ConfigError):
            self.load()

    def test_unsafe_cover_path_rejected(self):
        self.write("app.json", VALID_APP)
        st = dict(VALID_STATION, cover="../../etc/passwd.jpg")
        self.write("stations.json", {"stations": [st]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("cover", str(ctx.exception))

    def test_private_stream_url_rejected(self):
        self.write("app.json", VALID_APP)
        st = dict(VALID_STATION, stream_url="http://192.168.1.10:8000/live")
        self.write("stations.json", {"stations": [st]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("stream_url", str(ctx.exception))

    def test_header_injection_rejected(self):
        self.write("app.json", VALID_APP)
        st = dict(VALID_STATION, upstream_headers={"X-Test": "v1\r\nX-Evil: 1"})
        self.write("stations.json", {"stations": [st]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("upstream_headers", str(ctx.exception))

    def test_bad_station_id_rejected(self):
        self.write("app.json", VALID_APP)
        st = dict(VALID_STATION, id="My Station!!")
        self.write("stations.json", {"stations": [st]})
        with self.assertRaises(config.ConfigError):
            self.load()

    def test_weak_secret_path_rejected(self):
        bad_app = json.loads(json.dumps(VALID_APP))
        bad_app["server"]["secret_path"] = "/a/"
        self.write("app.json", bad_app)
        self.write("stations.json", {"stations": [VALID_STATION]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("secret_path", str(ctx.exception))

    def test_example_secret_path_warns(self):
        self.write("app.json", VALID_APP)  # VALID_APP uses the shipped example path
        self.write("stations.json", {"stations": [VALID_STATION]})
        cfg = self.load()
        self.assertTrue(any("secret_path" in w for w in cfg.warnings))

    def test_defaults_applied(self):
        self.write("app.json", {"server": {"secret_path": SECRET}})
        self.write("stations.json", {"stations": [
            {"id": "abcd01", "name": "Minimal", "stream_url": "https://x.example.com/s.mp3"}
        ]})
        cfg = self.load()
        self.assertEqual(cfg.limits.max_concurrent_streams_per_ip, 3)
        self.assertEqual(cfg.limits.stream_chunk_size_bytes, 16384)
        self.assertEqual(cfg.player.history_limit, 30)
        self.assertEqual(cfg.player.history_visible_items, 15)
        self.assertEqual(cfg.stations[0].codec, "mp3")
        self.assertTrue(cfg.stations[0].enabled)
        self.assertEqual(cfg.stations[0].metadata.type, "none")

    def test_player_history_config(self):
        app = json.loads(json.dumps(VALID_APP))
        app["player"] = {"history_limit": 50, "history_visible_items": 8}
        self.write("app.json", app)
        self.write("stations.json", {"stations": [VALID_STATION]})
        cfg = self.load()
        self.assertEqual(cfg.player.history_limit, 50)
        self.assertEqual(cfg.player.history_visible_items, 8)

    def test_player_history_out_of_range_rejected(self):
        app = json.loads(json.dumps(VALID_APP))
        app["player"] = {"history_limit": 99999}
        self.write("app.json", app)
        self.write("stations.json", {"stations": [VALID_STATION]})
        with self.assertRaises(config.ConfigError) as ctx:
            self.load()
        self.assertIn("player.history_limit", str(ctx.exception))

    def test_tokens_file_check(self):
        self.write("app.json", VALID_APP)
        self.write("stations.json", {"stations": []})
        (self.base / "config" / "tokens.json").write_text(json.dumps({"tokens": [
            {"key_id": "t1", "name": "x", "token_hash": "ab" * 32, "enabled": True,
             "created_at": "2026-01-01T00:00:00Z", "expires_at": None, "last_used_at": None}
        ]}))
        self.assertEqual(config.check_tokens_file(self.base / "config" / "tokens.json"), [])
        (self.base / "config" / "tokens.json").write_text(json.dumps({"tokens": [
            {"key_id": "t1", "name": "x", "token_hash": "nothex", "enabled": True,
             "created_at": None, "expires_at": None, "last_used_at": None}
        ]}))
        errors = config.check_tokens_file(self.base / "config" / "tokens.json")
        self.assertTrue(any("token_hash" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
