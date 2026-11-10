"""JSON/XML metadata parsing tests."""
import unittest

from private_radio_proxy.config import MetadataConfig, Station
from private_radio_proxy.metadata import parse_json_metadata, parse_xml_metadata


def make_station(mtype: str, **meta) -> Station:
    return Station(
        id="st0001", name="T", enabled=True,
        stream_url="https://upstream.example.com/live",
        fallback_stream_urls=[], codec="mp3", bitrate_kbps=None, cover=None,
        metadata=MetadataConfig(type=mtype, **meta),
        upstream_headers={},
    )


class TestJsonMetadata(unittest.TestCase):
    def test_dotted_paths(self):
        st = make_station("json", title_path="current.song.title",
                          artist_path="current.song.artist")
        data = b'{"current": {"song": {"title": "Song A", "artist": "Artist A"}}}'
        title, artist, artwork = parse_json_metadata(data, st)
        self.assertEqual((title, artist), ("Song A", "Artist A"))

    def test_array_index_path(self):
        st = make_station("json", title_path="playlist.0.title", artist_path=None)
        data = b'{"playlist": [{"title": "First"}]}'
        title, artist, _ = parse_json_metadata(data, st)
        self.assertEqual(title, "First")
        self.assertIsNone(artist)

    def test_missing_paths_return_none(self):
        st = make_station("json", title_path="a.b.c", artist_path="x.y")
        data = b'{"a": {"b": 1}}'
        title, artist, _ = parse_json_metadata(data, st)
        self.assertIsNone(title)
        self.assertIsNone(artist)

    def test_invalid_json_raises(self):
        st = make_station("json", title_path="t")
        with self.assertRaises(Exception):
            parse_json_metadata(b"not json", st)


class TestXmlMetadata(unittest.TestCase):
    def test_simple_tag_path(self):
        st = make_station("xml", title_path="now.title", artist_path="now.artist")
        data = (b'<?xml version="1.0"?><radio><now>'
                b"<artist>Artist X</artist><title>Title X</title>"
                b"</now></radio>")
        title, artist, _ = parse_xml_metadata(data, st)
        self.assertEqual((title, artist), ("Title X", "Artist X"))

    def test_namespaces_tolerated(self):
        st = make_station("xml", title_path="title")
        data = b'<root xmlns="urn:test"><title>NS Title</title></root>'
        title, _, _ = parse_xml_metadata(data, st)
        self.assertEqual(title, "NS Title")

    def test_invalid_xml_raises(self):
        st = make_station("xml", title_path="title")
        with self.assertRaises(Exception):
            parse_xml_metadata(b"<root><unclosed>", st)


if __name__ == "__main__":
    unittest.main()
