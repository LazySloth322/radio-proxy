"""Path traversal and URL validation tests (SSRF guard)."""
import asyncio
import unittest

from private_radio_proxy import utils


class TestSafeJoin(unittest.TestCase):
    def setUp(self):
        self.base = utils.Path  # noqa: kept for readability below

    def test_ok_relative(self):
        import tempfile, pathlib
        base = pathlib.Path(tempfile.mkdtemp())
        out = utils.safe_join(base, "covers/station.jpg")
        self.assertTrue(str(out).startswith(str(base.resolve())))
        out2 = utils.safe_join(base, "a/b/c.png")
        self.assertTrue(str(out2).startswith(str(base.resolve())))

    def test_traversals_rejected(self):
        import tempfile, pathlib
        base = pathlib.Path(tempfile.mkdtemp())
        bad = [
            "../etc/passwd",
            "covers/../../etc/passwd",
            "..",
            "./..",
            "a/../../x",
            "/etc/passwd",
            "~/secret",
            "a\\..\\..\\windows",
            "with\x00null",
            "",
        ]
        for rel in bad:
            with self.assertRaises(ValueError, msg=f"should reject {rel!r}"):
                utils.safe_join(base, rel)


class TestUrlValidation(unittest.TestCase):
    def test_sync_checks(self):
        ok = [
            "https://stream.example.com/live",
            "http://radio.example.org:8000/mp3",
            "https://198.51.100.10:8000/stream",  # TEST-NET-2 is not routable but allowed by list? -> it IS blocked
        ]
        # NOTE: 198.51.100.0/24 is in our blocked list (documentation range).
        allowed, blocked = ok[:2], ok[2:]
        for url in allowed:
            utils.check_url_allowed_sync(url)  # must not raise
        for url in blocked + [
            "http://127.0.0.1/stream",
            "http://127.255.0.1/x",
            "http://10.0.0.5/x",
            "http://172.16.1.1/x",
            "http://172.31.255.255/x",
            "http://192.168.1.50/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/x",
            "http://100.64.0.1/x",
            "http://224.0.0.1/x",
            "http://[::1]/x",
            "http://[fe80::1]/x",
            "http://[fc00::1]/x",
            "http://[::ffff:127.0.0.1]/x",
            "http://localhost/x",
            "http://something.localhost/x",
            "ftp://example.com/x",
            "file:///etc/passwd",
            "gopher://example.com/x",
            "javascript:alert(1)",
            "data:text/html,x",
        ]:
            with self.assertRaises(utils.UrlBlockedError, msg=f"should reject {url!r}"):
                utils.check_url_allowed_sync(url)

    def test_async_dns_localhost_blocked(self):
        # localhost resolves to loopback -> async check must reject
        with self.assertRaises(utils.UrlBlockedError):
            asyncio.run(utils.assert_url_allowed("http://localhost:8000/x"))

    def test_async_dns_fail_closed(self):
        # a non-resolvable name must fail closed
        with self.assertRaises(utils.UrlBlockedError):
            asyncio.run(utils.assert_url_allowed(
                "http://this-domain-does-not-exist-xxxxxxxx.invalid/x"))

    def test_header_validation(self):
        utils.validate_header_pair("User-Agent", "PrivateRadioProxy/1.0")
        for name, value in [
            ("X-Bad\r\nInjected", "v"),
            ("bad name with space", "v"),
            ("X-Ok", "line1\r\nline2"),
            ("X-Ok", "non ascii \u00e9"),
            ("", "v"),
        ]:
            with self.assertRaises(ValueError):
                utils.validate_header_pair(name, value)


if __name__ == "__main__":
    unittest.main()
