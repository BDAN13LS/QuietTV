"""Test-only launcher: the real server over real stdio with Wake-on-LAN enabled and a recording UDP sender.

Nothing is sent on any network: each packet is appended (hex, address, port) to a record file instead, and the
same-LAN check is pinned because its real answer depends on the test machine's routes.
argv: <config.json> <record-file>
"""
import json
import sys
from pathlib import Path

from quiettv_mcp.config import load_config
from quiettv_mcp.device import QuietTV
from quiettv_mcp.server import build_server

record = Path(sys.argv[2])


def recording_sender(packet, address):
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps([packet.hex(), address[0], address[1]]) + "\n")


class PinnedLanTV(QuietTV):
    def check_lan(self):
        return {"same_lan": True, "lan_prefix": self.config.lan_prefix}


config = load_config(Path(sys.argv[1]))
build_server(config, device=PinnedLanTV(config, udp_sender=recording_sender)).run("stdio")
