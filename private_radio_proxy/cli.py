"""Owner-only management CLI.

Commands:
  generate-secret-path            random secret URL path
  token add/list/revoke           token management (raw token printed once)
  validate-config                 validate JSON config files
  healthcheck [--station ID]      probe upstreams without starting the server
  serve                           start the server (same as -m private_radio_proxy)

Run from the project directory, e.g.:
  python -m private_radio_proxy.cli token add --name friend-1 --expires 2027-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

from . import __version__, config as config_mod, utils
from .tokens import TokenStore

# ---------------------------------------------------------------------------
# helpers


def get_base_dir(args) -> Path:
    import os

    if args.base_dir:
        return Path(args.base_dir).resolve()
    env = os.environ.get("PRP_BASE_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def _store(args) -> TokenStore:
    base = get_base_dir(args)
    return TokenStore(base / config_mod.CONFIG_DIR_NAME / config_mod.TOKENS_CONFIG_NAME)


def _print_full_login_url(args, raw_token: str) -> None:
    base = get_base_dir(args)
    try:
        cfg = config_mod.load_config(base)
        origin = cfg.server.public_base_url.rstrip("/") or f"http://<server-ip>:{cfg.server.port}"
        print(f"  Login URL: {origin}{cfg.server.secret_path}?token={raw_token}")
    except config_mod.ConfigError:
        print("  (app.json not configured yet - see README for the full URL format)")


# ---------------------------------------------------------------------------
# commands


def cmd_generate_secret_path(_args) -> None:
    print(utils.generate_secret_path())
    print(
        "Put this value into config/app.json -> server.secret_path, then restart the server.",
        file=sys.stderr,
    )


def cmd_token_add(args) -> None:
    expires_at = None
    if args.expires:
        try:
            expires_at = utils.parse_iso_utc(args.expires).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            print(f"Invalid --expires value: {exc}", file=sys.stderr)
            raise SystemExit(2)
    store = _store(args)
    raw, entry = store.add(args.name, expires_at)
    print(f"Token created: name={entry.name} key_id={entry.key_id}"
          + (f" expires_at={entry.expires_at}" if entry.expires_at else ""))
    print()
    print("RAW TOKEN (copy it now - it will NOT be shown again):")
    print()
    print(f"  {raw}")
    print()
    print("It is stored only as a SHA-256 hash in config/tokens.json.")
    _print_full_login_url(args, raw)


def cmd_token_list(args) -> None:
    entries = _store(args).list()
    if not entries:
        print("No tokens. Create one: token add --name owner")
        return
    width = max(len(e.key_id) for e in entries) + 2
    header = f"{'KEY_ID':<{width}} {'NAME':<20} {'ENABLED':<8} {'CREATED':<21} {'EXPIRES':<21} LAST_USED"
    print(header)
    print("-" * len(header))
    for e in entries:
        print(
            f"{e.key_id:<{width}} {e.name[:19]:<20} {'yes' if e.enabled else 'NO':<8} "
            f"{e.created_at:<21} {e.expires_at or '-':<21} {e.last_used_at or '-'}"
        )


def cmd_token_revoke(args) -> None:
    store = _store(args)
    if store.set_enabled(args.key_id, enabled=not args.enable):
        state = "re-enabled" if args.enable else "REVOKED (disabled)"
        print(f"Token {args.key_id} {state}.")
        if not args.enable:
            print("Existing users lose access on their next request.")
    else:
        print(f"No token with key_id={args.key_id!r}. Use 'token list'.", file=sys.stderr)
        raise SystemExit(1)


def cmd_validate_config(args) -> None:
    base = get_base_dir(args)
    errors: list[str] = []
    warnings: list[str] = []
    cfg = None
    try:
        cfg = config_mod.load_config(base)
        warnings.extend(cfg.warnings)
        print(f"OK  {base / config_mod.CONFIG_DIR_NAME / config_mod.APP_CONFIG_NAME}")
        print(f"OK  {base / config_mod.CONFIG_DIR_NAME / config_mod.STATIONS_CONFIG_NAME} "
              f"({len(cfg.stations)} station(s), {sum(1 for s in cfg.stations if s.enabled)} enabled)")
    except config_mod.ConfigError as exc:
        errors.extend(str(exc).splitlines()[1:])  # strip the header line

    tokens_path = base / config_mod.CONFIG_DIR_NAME / config_mod.TOKENS_CONFIG_NAME
    errors.extend(config_mod.check_tokens_file(tokens_path))
    if tokens_path.exists():
        print(f"OK  {tokens_path}")

    for warning in warnings:
        print(f"WARN {warning}")
    if errors:
        for error in errors:
            print(f"ERR  {error}", file=sys.stderr)
        print(f"\nValidation FAILED ({len(errors)} error(s)).", file=sys.stderr)
        raise SystemExit(1)
    print("\nValidation passed.")


def cmd_healthcheck(args) -> None:
    base = get_base_dir(args)
    try:
        cfg = config_mod.load_config(base)
    except config_mod.ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        raise SystemExit(2)

    stations = cfg.stations
    if args.station:
        station = cfg.stations_by_id.get(args.station)
        if station is None:
            print(f"Unknown station id: {args.station}", file=sys.stderr)
            raise SystemExit(1)
        stations = [station]

    failures = asyncio.run(_healthcheck_all(cfg, stations))
    raise SystemExit(1 if failures else 0)


async def _healthcheck_all(cfg, stations) -> int:
    failures = 0
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        for station in stations:
            result = await _healthcheck_station(cfg, station, session)
            if result["stream_ok"]:
                print(f"[{station.id}] stream OK  ctype={result['content_type']} "
                      f"bytes={result['bytes_read']} icy-metaint={result['metaint']}")
            else:
                failures += 1
                print(f"[{station.id}] stream FAIL {result['error']}")
            if station.metadata.type in ("json", "xml") and station.metadata.url:
                if result["metadata_title"] is not None or result["metadata_display"]:
                    print(f"[{station.id}] metadata OK  {result['metadata_display']}")
                else:
                    failures += 1
                    print(f"[{station.id}] metadata FAIL {result['metadata_error']}")
            elif station.metadata.type == "icy":
                hint = "available while a listener is connected" if result["metaint"] \
                    else "upstream did not offer ICY metadata"
                print(f"[{station.id}] metadata ICY ({hint})")
    return failures


async def _healthcheck_station(cfg, station, session) -> dict:
    result = {
        "stream_ok": False, "content_type": "-", "bytes_read": 0,
        "metaint": None, "error": "-",
        "metadata_title": None, "metadata_display": None, "metadata_error": None,
    }
    timeout = aiohttp.ClientTimeout(connect=cfg.limits.upstream_connect_timeout_seconds,
                                    total=15, sock_read=5)
    headers = dict(station.upstream_headers)
    headers.setdefault("User-Agent", utils.DEFAULT_USER_AGENT)
    headers["Accept"] = "*/*"
    if station.metadata.type == "icy":
        headers["Icy-MetaData"] = "1"
    try:
        if not cfg.security.allow_private_upstreams:
            await utils.assert_url_allowed(station.stream_url)
        async with session.get(station.stream_url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                result["error"] = f"HTTP {resp.status}"
                return result
            result["content_type"] = resp.headers.get("Content-Type", "-")
            raw_mi = resp.headers.get("icy-metaint")
            if raw_mi and raw_mi.strip().isdigit():
                result["metaint"] = int(raw_mi.strip())
            chunk = await asyncio.wait_for(resp.content.read(16384), timeout=5)
            result["bytes_read"] = len(chunk)
            result["stream_ok"] = len(chunk) > 0
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result

    if station.metadata.type in ("json", "xml") and station.metadata.url:
        try:
            async with session.get(station.metadata.url, timeout=aiohttp.ClientTimeout(total=10)) as mresp:
                if mresp.status != 200:
                    raise RuntimeError(f"HTTP {mresp.status}")
                data = await mresp.read()
            from .metadata import parse_json_metadata, parse_xml_metadata
            if station.metadata.type == "json":
                title, artist, _ = parse_json_metadata(data, station)
            else:
                title, artist, _ = parse_xml_metadata(data, station)
            from .icy import join_display
            result["metadata_title"] = title
            result["metadata_display"] = join_display(artist, title) if (title or artist) else None
        except Exception as exc:  # noqa: BLE001
            result["metadata_error"] = str(exc)
    return result


def cmd_serve(args) -> None:
    from .app import run_server
    base = get_base_dir(args)
    run_server(base, host=args.host, port=args.port,
               config_dir=Path(args.config_dir) if args.config_dir else None)


# ---------------------------------------------------------------------------
# parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m private_radio_proxy.cli",
        description="Management CLI for private-radio-proxy (owner only).",
    )
    parser.add_argument("--base-dir", default=None,
                        help="project directory containing config/ (default: package parent or $PRP_BASE_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate-secret-path", help="print a random secret URL path")
    p.set_defaults(func=cmd_generate_secret_path)

    p = sub.add_parser("token", help="token management")
    tsub = p.add_subparsers(dest="token_command", required=True)

    pt = tsub.add_parser("add", help="create a token; prints the raw token once")
    pt.add_argument("--name", required=True, help="human label, e.g. owner / friend-1")
    pt.add_argument("--expires", default=None, help="expiry date, e.g. 2027-01-01 (optional)")
    pt.set_defaults(func=cmd_token_add)

    pt = tsub.add_parser("list", help="list tokens (never shows raw tokens)")
    pt.set_defaults(func=cmd_token_list)

    pt = tsub.add_parser("revoke", help="disable a token by key_id")
    pt.add_argument("--key-id", required=True)
    pt.add_argument("--enable", action="store_true", help="re-enable instead of revoke")
    pt.set_defaults(func=cmd_token_revoke)

    p = sub.add_parser("validate-config", help="validate app.json / stations.json / tokens.json")
    p.set_defaults(func=cmd_validate_config)

    p = sub.add_parser("healthcheck", help="probe upstream stream/metadata without the web server")
    p.add_argument("--station", default=None, help="station id (default: all enabled stations)")
    p.set_defaults(func=cmd_healthcheck)

    p = sub.add_parser("serve", help="start the server")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--config-dir", default=None)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    print(f"private-radio-proxy v{__version__}", file=sys.stderr) if args.command == "serve" else None
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
