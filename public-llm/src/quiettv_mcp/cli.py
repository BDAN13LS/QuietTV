"""Console entry: quiettv-mcp {serve,setup,doctor,client-config}."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config, resolve_config_path
from .profiles import APP_IDS, KEY_CODES
from .setup_cli import CLIENTS, run_doctor, run_setup, write_client_fragments


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quiettv-mcp", description="Local MCP server for one owner-configured Quiet TV.")
    sub = parser.add_subparsers(dest="command", required=True)
    cfg_help = "path to the private owner config (default: QUIETTV_MCP_CONFIG, then the per-user config folder)"

    serve = sub.add_parser("serve", help="run the MCP server on stdio (what MCP clients launch)")
    serve.add_argument("--config", help=cfg_help)
    serve.add_argument("--enable-maintenance", action="store_true",
                       help="owner opt-in: expose backup/cleanup/restore tools IF a validated toolkit adapter is installed")

    setup = sub.add_parser("setup", help="interactive guided setup that writes the private owner config")
    setup.add_argument("--config", help=cfg_help)

    doctor = sub.add_parser("doctor", help="check the config, key file and LAN; --online adds read-only device checks")
    doctor.add_argument("--config", help=cfg_help)
    doctor.add_argument("--online", action="store_true", help="also check identity (8001) and service status (4190), read-only")

    run = sub.add_parser("run", help="human CLI: the same checked tools without any AI model")
    run.add_argument("--config", help=cfg_help)
    actions = run.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    actions.add_parser("home")
    actions.add_parser("diagnostics")
    actions.add_parser("key").add_argument("name", choices=list(KEY_CODES))
    actions.add_parser("launch").add_argument("app_id", choices=list(APP_IDS))
    capture = actions.add_parser("capture")
    capture.add_argument("--out", required=True, help="new PNG file to write (private output; never overwritten)")

    client = sub.add_parser("client-config", help="print or write MCP client config fragments (never edits global settings)")
    client.add_argument("--config", help=cfg_help)
    client.add_argument("--client", choices=[*CLIENTS, "all"], default="all")
    client.add_argument("--output-dir", help="write fragment files here instead of printing (refuses to overwrite)")
    return parser


def _serve(config_path: Path, enable_maintenance: bool) -> int:
    # stdout carries the MCP protocol; every log line goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="quiettv-mcp %(levelname)s %(message)s")
    from .server import build_server
    from .toolkit import MaintenanceGate, discover_adapter

    config = load_config(config_path)
    adapter, reason = discover_adapter() if enable_maintenance else (None, "maintenance is off")
    gate = MaintenanceGate(enabled=enable_maintenance, adapter=adapter, reason=reason)
    build_server(config, maintenance=gate).run("stdio")
    return 0


def _run(config_path: Path, args: argparse.Namespace, device_factory=None) -> int:
    from .device import DeviceError, QuietTV

    config = load_config(config_path)
    tv = (device_factory or QuietTV)(config)
    try:
        if args.action == "capture":
            out = Path(args.out).expanduser()
            shot, meta = tv.capture()
            with open(out, "xb") as handle:  # exclusive create: never overwrite
                handle.write(shot.png)
            meta["written_to"] = str(out)
            result = meta
        else:
            calls = {"status": tv.status, "home": tv.home, "diagnostics": tv.diagnostics,
                     "key": lambda: tv.key(args.name), "launch": lambda: tv.launch(args.app_id)}
            result = calls[args.action]()
    except DeviceError as exc:
        print(f"quiettv-mcp: refused: {exc}", file=sys.stderr)
        return 1
    except FileExistsError:
        print("quiettv-mcp: --out already exists; nothing overwritten", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_path = resolve_config_path(getattr(args, "config", None))
    try:
        if args.command == "serve":
            return _serve(config_path, args.enable_maintenance)
        if args.command == "setup":
            run_setup(config_path)
            return 0
        if args.command == "doctor":
            return run_doctor(config_path, online=args.online)
        if args.command == "run":
            return _run(config_path, args)
        clients = list(CLIENTS) if args.client == "all" else [args.client]
        write_client_fragments(config_path, clients, Path(args.output_dir) if args.output_dir else None, sys.stdout)
        return 0
    except ConfigError as exc:
        print(f"quiettv-mcp: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("quiettv-mcp: cancelled; nothing further was written", file=sys.stderr)
        return 130
