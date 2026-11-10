"""Stateful ICY (Icecast/Shoutcast in-stream metadata) parser.

Icecast streams interleave audio bytes with periodic small metadata blocks:

    [metaint bytes of audio][1 length byte N][N*16 bytes of metadata]...

The metadata text usually looks like:  StreamTitle='Artist - Track';

This parser is a small state machine:
  * accepts arbitrary chunk boundaries (metadata split across chunks is fine);
  * emits (audio_bytes, [titles...]) per feed() call;
  * never accumulates more than one metadata block (max 16*255 bytes);
  * decodes defensively (UTF-8 first, Latin-1 fallback);
  * unescapes \\' inside titles;
  * if anything looks wrong it degrades to raw passthrough (broken=True)
    so audio never stops because of metadata problems.

One parser instance exists per proxied connection; it is discarded with the
connection, so per-connection state cannot leak.
"""
from __future__ import annotations

import re

MAX_METADATA_BLOCK = 16 * 255  # protocol maximum

_STREAM_TITLE_RE = re.compile(rb"StreamTitle\s*=\s*'((?:\\'|[^'])*)'")


class ICYParser:
    def __init__(self, metaint: int):
        if not isinstance(metaint, int) or metaint <= 0 or metaint > 1_048_576:
            raise ValueError("metaint must be a positive integer <= 1 MiB")
        self.metaint = metaint
        self.broken = False  # set on unrecoverable parse problems
        self._audio_needed = metaint   # audio bytes until next metadata block
        self._meta_needed = 0          # metadata bytes still expected
        self._meta_buf = bytearray()

    # ------------------------------------------------------------------

    def feed(self, chunk: bytes) -> tuple[bytes, list[str]]:
        """Consume upstream bytes. Returns (audio_bytes, titles found)."""
        if self.broken:
            return chunk, []

        audio = bytearray()
        titles: list[str] = []
        pos = 0
        end = len(chunk)

        while pos < end:
            if self._audio_needed > 0:
                take = min(end - pos, self._audio_needed)
                audio += chunk[pos:pos + take]
                pos += take
                self._audio_needed -= take
                continue

            if self._meta_needed == 0:
                # Next byte is the length byte (N * 16 bytes of metadata).
                n = chunk[pos]
                pos += 1
                self._meta_needed = n * 16
                if self._meta_needed == 0:
                    self._audio_needed = self.metaint
                    continue
                if self._meta_needed > MAX_METADATA_BLOCK:  # cannot happen, defensive
                    self.broken = True
                    self._reset()
                    return bytes(audio), titles
                self._meta_buf.clear()
                continue

            take = min(end - pos, self._meta_needed)
            self._meta_buf += chunk[pos:pos + take]
            pos += take
            self._meta_needed -= take
            if self._meta_needed == 0:
                title = _extract_title(bytes(self._meta_buf))
                self._meta_buf.clear()
                self._audio_needed = self.metaint
                if title is not None:
                    titles.append(title)

        return bytes(audio), titles

    def _reset(self) -> None:
        self._audio_needed = self.metaint
        self._meta_needed = 0
        self._meta_buf.clear()


def _extract_title(meta: bytes) -> str | None:
    """Extract StreamTitle from one metadata block. Returns None if absent."""
    text = meta.replace(b"\x00", b"")  # strip padding, keep interior bytes
    if not text:
        return None
    match = _STREAM_TITLE_RE.search(text)
    if not match:
        return None
    raw = match.group(1)
    # Defensive decoding: Icecast may send UTF-8 or ISO-8859-1.
    try:
        title = raw.decode("utf-8")
    except UnicodeDecodeError:
        title = raw.decode("latin-1", errors="replace")
    # Unescape common ICY escapes: \' -> ' and \\ -> backslash.
    title = title.replace("\\'", "'").replace("\\\\", "\\")
    return title.strip() or None


def parse_icy_metaint(headers) -> int | None:
    """Read 'icy-metaint' from response headers; None if absent/invalid."""
    value = headers.get("icy-metaint")
    if value is None:
        return None
    try:
        metaint = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if metaint <= 0 or metaint > 1_048_576:
        return None
    return metaint


def split_artist_title(stream_title: str) -> tuple[str | None, str]:
    """Split "Artist - Track" into (artist, title); (None, title) otherwise."""
    if " - " in stream_title:
        artist, track = stream_title.split(" - ", 1)
        artist = artist.strip()
        return (artist or None), track.strip()
    return None, stream_title.strip()


def join_display(artist: str | None, title: str | None) -> str:
    if artist and title:
        return f"{artist} - {title}"
    return title or artist or "Unknown"
