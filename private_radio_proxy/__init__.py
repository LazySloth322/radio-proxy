"""private-radio-proxy - a minimal private internet radio stream proxy.

Design goals:
  * owner-configured stations (JSON files only, no database);
  * token-based private access, everything under one secret URL path;
  * the server proxies audio from its own network location;
  * no transcoding, no fingerprinting, no public features;
  * conservative memory usage (target: well below 120 MB RSS for 2-3 listeners).

Runs on Debian 12/13 with Python 3.11+ and aiohttp as the only runtime
dependency. Deployed as a plain systemd service.
"""

__version__ = "1.0.0"
