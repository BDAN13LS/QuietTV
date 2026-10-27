"""Test-only launcher: the real server over real stdio, pointed at a loopback FakeTV.

Loopback is allowed only through the in-process allow_loopback_fixture flag
used here; the shipped console entry has no way to enable it.
argv: <config.json> <identity_port> <control_port>
"""
import sys
from pathlib import Path

from quiettv_mcp.config import load_config
from quiettv_mcp.device import QuietTV
from quiettv_mcp.server import build_server

config = load_config(Path(sys.argv[1]), allow_loopback_fixture=True)
device = QuietTV(config, identity_port=int(sys.argv[2]), control_port=int(sys.argv[3]))
build_server(config, device=device).run("stdio")
