"""Access token storage and validation.

Design:
  * Raw tokens exist ONLY in the moment the CLI prints them to the owner.
  * config/tokens.json stores SHA-256 hashes plus metadata (name, dates,
    enabled flag) - never the raw token.
  * The file is re-read whenever its mtime changes, so token changes made by
    the CLI take effect on the running server within a second.
  * Writes are atomic (temp file + os.replace) and the file is chmod 0600.
  * Validation uses constant-time comparison on the hashes.
  * 'last_used_at' is updated in memory and flushed to disk at most once per
    minute (merge-on-write so CLI edits are never clobbered).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import utils

log = logging.getLogger("radio_proxy.tokens")


@dataclass
class TokenEntry:
    key_id: str
    name: str
    token_hash: str
    enabled: bool
    created_at: str
    expires_at: str | None
    last_used_at: str | None


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._entries: dict[str, TokenEntry] = {}
        self._mtime: float = -1.0
        self._dirty = False
        # last_used_at marks that must survive a concurrent CLI rewrite:
        # key_id -> ISO timestamp, applied on reload and cleared on flush.
        self._pending_used: dict[str, str] = {}
        self._load(create_if_missing=True)

    # ------------------------------------------------------------------ I/O

    def _load(self, create_if_missing: bool = False) -> None:
        if not self.path.exists():
            if create_if_missing:
                self._write_file(list(self._entries.values()))
            return
        stat = self.path.stat()
        if stat.st_mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            log.error("cannot read token file %s: %s", self.path, exc)
            return
        tokens = data.get("tokens") if isinstance(data, dict) else None
        entries: dict[str, TokenEntry] = {}
        if isinstance(tokens, list):
            for raw in tokens:
                if not isinstance(raw, dict):
                    continue
                key_id = str(raw.get("key_id") or "")
                token_hash = str(raw.get("token_hash") or "")
                if not key_id or len(token_hash) != 64:
                    continue  # skip malformed entries defensively
                entries[key_id] = TokenEntry(
                    key_id=key_id,
                    name=str(raw.get("name") or key_id)[:100],
                    token_hash=token_hash,
                    enabled=bool(raw.get("enabled", True)),
                    created_at=str(raw.get("created_at") or utils.utc_now_iso()),
                    expires_at=raw.get("expires_at") if isinstance(raw.get("expires_at"), str) else None,
                    last_used_at=raw.get("last_used_at") if isinstance(raw.get("last_used_at"), str) else None,
                )
        self._entries = entries
        self._mtime = stat.st_mtime
        # Re-apply last_used_at marks so a CLI rewrite (add/revoke) between
        # our validation and our flush cannot lose them.
        if self._pending_used:
            for key_id, used in self._pending_used.items():
                entry = entries.get(key_id)
                if entry is not None and (not entry.last_used_at or entry.last_used_at < used):
                    entry.last_used_at = used
                    self._dirty = True

    def _write_file(self, entries: list[TokenEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tokens": [asdict(e) for e in entries]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ------------------------------------------------------------ validation

    def validate(self, raw_token: str | None) -> TokenEntry | None:
        """Return the entry for a valid, enabled, non-expired token; else None."""
        if not raw_token or len(raw_token) > 512:
            return None
        self._load()  # pick up CLI-made changes via mtime check
        candidate = utils.hash_token(raw_token)
        now = utils.parse_iso_utc(utils.utc_now_iso())
        for entry in self._entries.values():
            if not entry.enabled:
                continue
            if not hmac.compare_digest(entry.token_hash, candidate):
                continue
            if entry.expires_at:
                try:
                    if utils.parse_iso_utc(entry.expires_at) <= now:
                        return None  # expired
                except ValueError:
                    return None
            entry.last_used_at = utils.utc_now_iso()
            self._pending_used[entry.key_id] = entry.last_used_at
            self._dirty = True
            return entry
        return None

    def entry(self, key_id: str) -> TokenEntry | None:
        return self._entries.get(key_id)

    # ----------------------------------------------------------- management

    def add(self, name: str, expires_at: str | None = None) -> tuple[str, TokenEntry]:
        """Create a token. Returns (raw_token, entry); the raw token is shown
        exactly once by the caller."""
        self._load()
        raw = utils.generate_token()
        key_id = utils.short_id("t")
        while key_id in self._entries:
            key_id = utils.short_id("t")
        entry = TokenEntry(
            key_id=key_id,
            name=(name or "unnamed")[:100],
            token_hash=utils.hash_token(raw),
            enabled=True,
            created_at=utils.utc_now_iso(),
            expires_at=expires_at,
            last_used_at=None,
        )
        self._entries[key_id] = entry
        self._write_file(list(self._entries.values()))
        log.info("token added key_id=%s name=%s", entry.key_id, entry.name)
        return raw, entry

    def set_enabled(self, key_id: str, enabled: bool) -> bool:
        self._load()
        entry = self._entries.get(key_id)
        if entry is None:
            return False
        entry.enabled = enabled
        self._write_file(list(self._entries.values()))
        log.info("token %s key_id=%s", "enabled" if enabled else "revoked", key_id)
        return True

    def list(self) -> list[TokenEntry]:
        self._load()
        return sorted(self._entries.values(), key=lambda e: (e.created_at, e.key_id))

    # ------------------------------------------------------- periodic flush

    async def flush_if_dirty(self) -> None:
        """Persist throttled last_used_at updates, merging with on-disk state
        so concurrent CLI edits (add/revoke) are preserved."""
        if not self._dirty:
            return
        async with self._lock:
            if not self._dirty:
                return
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                disk = {}
            disk_by_id = {
                str(t.get("key_id")): t
                for t in (disk.get("tokens") or [])
                if isinstance(t, dict) and t.get("key_id")
            }
            merged: list[TokenEntry] = []
            for key_id, raw in disk_by_id.items():
                pending = self._pending_used.get(key_id)
                if pending:
                    prev = raw.get("last_used_at")
                    if not isinstance(prev, str) or prev < pending:
                        raw["last_used_at"] = pending
                merged.append(
                    TokenEntry(
                        key_id=key_id,
                        name=str(raw.get("name") or key_id)[:100],
                        token_hash=str(raw.get("token_hash") or ""),
                        enabled=bool(raw.get("enabled", True)),
                        created_at=str(raw.get("created_at") or ""),
                        expires_at=raw.get("expires_at") if isinstance(raw.get("expires_at"), str) else None,
                        last_used_at=raw.get("last_used_at") if isinstance(raw.get("last_used_at"), str) else None,
                    )
                )
            self._write_file(merged)
            self._entries = {e.key_id: e for e in merged}
            self._pending_used.clear()
            self._dirty = False
            self._mtime = time.time()
