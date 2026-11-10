"""Logging setup with token masking and rotation.

* File handler: logs/app.log rotated at ~1 MB, 3 backups.
* Console handler on stderr (captured by the systemd journal too).
* A filter rewrites any 'token=...' query values and the session cookie value
  to '***' in every record, so raw tokens can never end up in logs.
* Structured keywords (AUTH_FAIL, RATE_LIMITED, ip=...) are written for
  fail2ban matching - see fail2ban/filter.d/private-radio-proxy.conf.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from . import utils

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SensitiveDataFilter(logging.Filter):
    """Mask tokens in every emitted log record."""

    def __init__(self, cookie_name: str | None = None):
        super().__init__()
        self.cookie_name = cookie_name

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # never break logging on odd records
            return True
        masked = utils.mask_query_tokens(message, self.cookie_name)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(logs_dir: Path, level: str = "INFO", mask_tokens: bool = True,
                  cookie_name: str | None = None) -> None:
    """Configure the 'radio_proxy' logger tree. Safe to call once at start."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("radio_proxy")
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()
    root.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console)

    if mask_tokens:
        sensitive = SensitiveDataFilter(cookie_name)
        file_handler.addFilter(sensitive)
        console.addFilter(sensitive)

    # Quiet down aiohttp internals unless debugging.
    logging.getLogger("aiohttp").setLevel(
        logging.WARNING if getattr(logging, level, logging.INFO) > logging.DEBUG else logging.DEBUG
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("radio_proxy." + name)
