"""Unit tests for the stateful ICY metadata parser."""
import unittest

from private_radio_proxy.icy import ICYParser, parse_icy_metaint, split_artist_title


def meta_block(title: str) -> bytes:
    """Build one ICY metadata block: length byte + StreamTitle padded to 16k."""
    raw = f"StreamTitle='{title}';".encode("utf-8")
    pad = (-len(raw)) % 16
    total = raw + b"\x00" * pad
    return bytes([len(total) // 16]) + total


class TestICYParser(unittest.TestCase):
    def test_rejects_bad_metaint(self):
        for bad in (0, -1, 2**20 * 2):
            with self.assertRaises(ValueError):
                ICYParser(bad)

    def test_audio_only_roundtrip(self):
        p = ICYParser(8)
        # length byte 0 -> no metadata, audio flows continuously
        out = b""
        titles = []
        for chunk in (b"12345678\x00", b"abcdefg", b"h\x00", b"23456789\x00"):
            audio, t = p.feed(chunk)
            out += audio
            titles += t
        self.assertEqual(out, b"12345678abcdefgh23456789")
        self.assertEqual(titles, [])

    def test_metadata_across_chunk_boundaries(self):
        p = ICYParser(5)
        block = meta_block("Artist - Song")  # len byte + padded metadata
        stream = b"12345" + block + b"67890" + block
        audio, titles = b"", []
        # feed in awkward 3-byte pieces
        for i in range(0, len(stream), 3):
            a, t = p.feed(stream[i:i + 3])
            audio += a
            titles += t
        self.assertEqual(audio, b"1234567890")
        self.assertEqual(titles, ["Artist - Song", "Artist - Song"])

    def test_title_extraction_and_unescape(self):
        p = ICYParser(4)
        _, titles = p.feed(b"abcd" + meta_block("Foo\\'s Bar - Baz"))
        self.assertEqual(titles, ["Foo's Bar - Baz"])

    def test_latin1_fallback(self):
        p = ICYParser(4)
        raw = "StreamTitle='Ärger - Song';".encode("latin-1")
        pad = (-len(raw)) % 16
        block = bytes([len(raw + b"\x00" * pad) // 16]) + raw + b"\x00" * pad
        _, titles = p.feed(b"abcd" + block)
        self.assertEqual(titles, ["Ärger - Song"])

    def test_malformed_metadata_no_crash_audio_continues(self):
        p = ICYParser(4)
        # metadata without StreamTitle at all
        junk = b"garbage-no-title-here\x00" * 2
        block = bytes([len(junk) // 16]) + junk[: (len(junk) // 16) * 16]
        audio, titles = p.feed(b"wxyz" + block + b"next")
        self.assertEqual(audio, b"wxyznext")
        self.assertEqual(titles, [])

    def test_broken_mode_passthrough(self):
        p = ICYParser(4)
        p.broken = True
        audio, titles = p.feed(b"\x03garbage")
        self.assertEqual(audio, b"\x03garbage")
        self.assertEqual(titles, [])

    def test_split_artist_title(self):
        self.assertEqual(split_artist_title("ABBA - Dancing Queen"), ("ABBA", "Dancing Queen"))
        self.assertEqual(split_artist_title("Just a title"), (None, "Just a title"))
        self.assertEqual(split_artist_title(" - leading dash"), (None, "leading dash"))

    def test_parse_icy_metaint_header(self):
        class H(dict):
            def get(self, k, default=None):
                return super().get(k.lower(), default)

        self.assertEqual(parse_icy_metaint(H({"icy-metaint": "8192"})), 8192)
        self.assertIsNone(parse_icy_metaint(H({})))
        self.assertIsNone(parse_icy_metaint(H({"icy-metaint": "abc"})))
        self.assertIsNone(parse_icy_metaint(H({"icy-metaint": "-5"})))


if __name__ == "__main__":
    unittest.main()
