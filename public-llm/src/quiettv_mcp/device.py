"""The only code that talks to the TV.

Every control call runs, in order: read the key file (local, fail closed) ->
same-LAN check (local, no packets) -> identity check against 8001/api/v2
(no key sent) -> one allowlisted request to the 4190 service. A failure at
any step stops the call before the next one starts.

Wake-on-LAN is the one exception: a TV in standby does not answer 8001, so
wake() runs only the local same-LAN check and then sends a standard magic
packet to the owner-configured MAC on the derived LAN broadcast. It uses no
key, contacts neither port, and never claims the TV woke.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets as _secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import capture as cap
from .config import OwnerConfig
from .redact import redact_text
from .summaries import summarize_command_result, summarize_diagnostics, summarize_status
from .transport import Response, TransportError, request

IDENTITY_PORT = 8001
CONTROL_PORT = 4190
KEY_HEADER = "X-QuietTV-Key"
MAX_JSON_BYTES = 256 * 1024
MAX_KEY_FILE_BYTES = 4096
_KEY = re.compile(r"^[\x21-\x7e]{16,256}$")
WAKE_COOLDOWN_S = 5.0
WAKE_NOTE = ("Wake packet sent; this does not confirm the TV woke. Wait about 15 seconds, then call tv_status, "
             "which checks the TV's identity. Wake works only if the TV allows network power-on (for example "
             "Network Standby / Power On with Mobile) and is plugged in; support over Wi-Fi versus Ethernet varies.")

UdpSender = Callable[[bytes, tuple[str, int]], None]


def magic_packet(mac: str) -> bytes:
    """Standard Wake-on-LAN payload: six 0xFF bytes, then the MAC sixteen times (102 bytes, no password)."""
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def send_udp_broadcast(packet: bytes, address: tuple[str, int]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, address)


class DeviceError(Exception):
    """A refused or failed device operation. Messages are safe to show and never contain the key."""


class IdentityMismatch(DeviceError):
    pass


@dataclass(frozen=True)
class Identity:
    model: str
    mac_verified: bool
    firmware: str  # "match" or "unverified"
    os: str | None
    reported_ip_checked: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "mac_verified": self.mac_verified,
            "firmware": self.firmware,
            "os": self.os,
            "reported_ip_checked": self.reported_ip_checked,
        }


class QuietTV:
    def __init__(self, config: OwnerConfig, *, identity_port: int = IDENTITY_PORT, control_port: int = CONTROL_PORT,
                 timeout: float = 10.0, clock: Callable[[], float] = time.time, udp_sender: UdpSender | None = None):
        self.config = config
        self.identity_port = identity_port
        self.control_port = control_port
        self.timeout = timeout
        self.clock = clock
        self.udp_sender = udp_sender
        self._seen_capture_ids: set[str] = set()
        self._last_wake: float | None = None

    # -- local checks -------------------------------------------------------
    def read_key(self) -> str:
        path = self.config.control_key_file
        try:
            if path.is_symlink() or not path.is_file():
                raise DeviceError("control key file is missing or not a regular file; no request sent")
            with open(path, "rb") as handle:
                raw = handle.read(MAX_KEY_FILE_BYTES + 1)
        except DeviceError:
            raise
        except OSError:
            raise DeviceError("control key file could not be read; no request sent") from None
        try:
            key = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise DeviceError("control key file is not ASCII; no request sent") from None
        if len(raw) > MAX_KEY_FILE_BYTES or not _KEY.match(key):
            raise DeviceError("control key file does not hold one printable 16-256 character key; no request sent")
        return key

    def check_lan(self) -> dict[str, Any]:
        """Confirm this machine routes to the TV from inside the configured LAN prefix (UDP connect sends nothing)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((self.config.host, self.identity_port))
                local = ipaddress.IPv4Address(probe.getsockname()[0])
        except OSError:
            raise DeviceError("no local route to the configured TV address; no request sent") from None
        network = ipaddress.IPv4Network(f"{local}/{self.config.lan_prefix}", strict=False)
        if self.config.tv_ip not in network:
            raise DeviceError("the TV is not on this machine's LAN segment (lan_prefix); no request sent")
        return {"same_lan": True, "lan_prefix": self.config.lan_prefix}

    # -- identity -----------------------------------------------------------
    def verify_identity(self) -> Identity:
        try:
            resp = request(self.config.host, self.identity_port, "GET", "/api/v2/", timeout=min(self.timeout, 5.0),
                           max_bytes=64 * 1024)
        except TransportError as exc:
            raise DeviceError(f"identity check failed ({exc}); no control request sent") from None
        if resp.status != 200:
            raise IdentityMismatch(f"identity endpoint returned HTTP {resp.status}; no control request sent")
        try:
            device = json.loads(resp.body.decode("utf-8")).get("device")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            device = None
        if not isinstance(device, dict):
            raise IdentityMismatch("identity endpoint did not return a device record; no control request sent")
        profile = self.config.profile
        if device.get("modelName") != profile.model:
            raise IdentityMismatch("TV model does not match the configured profile; no control request sent")
        mac = device.get("wifiMac")
        if not isinstance(mac, str) or mac.strip().upper().replace("-", ":") != self.config.tv_mac:
            raise IdentityMismatch("TV MAC address does not match the owner configuration; no control request sent")
        firmware = profile.firmware_status(device.get("firmwareVersion"))
        if firmware == "mismatch":
            raise IdentityMismatch("TV reports firmware outside the supported profile; no control request sent")
        os_name = device.get("OS")
        if os_name is not None and os_name != profile.os:
            raise IdentityMismatch("TV reports an unsupported operating system; no control request sent")
        reported_ip = device.get("ip")
        if reported_ip is not None and reported_ip != self.config.host:
            raise IdentityMismatch("TV reports a different IP address than configured; no control request sent")
        return Identity(
            model=profile.model,
            mac_verified=True,
            firmware="match" if firmware == "match" else "unverified",
            os=os_name if isinstance(os_name, str) else None,
            reported_ip_checked=reported_ip is not None,
        )

    # -- control ------------------------------------------------------------
    def _control(self, method: str, route: str, *, max_bytes: int = MAX_JSON_BYTES,
                 extra_headers: dict[str, str] | None = None) -> tuple[Response, Identity, str]:
        key = self.read_key()
        self.check_lan()
        identity = self.verify_identity()
        headers = {KEY_HEADER: key, **(extra_headers or {})}
        try:
            resp = request(self.config.host, self.control_port, method, "/" + route, headers=headers,
                           timeout=self.timeout, max_bytes=max_bytes)
        except TransportError as exc:
            raise DeviceError(redact_text(str(exc), (key,))) from None
        if resp.status == 403:
            raise DeviceError("TV service refused the request (HTTP 403): wrong control key, or this PC's address "
                              "is not on the TV service's controller allowlist")
        return resp, identity, key

    def _json(self, resp: Response, key: str) -> Any:
        try:
            return json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeviceError(f"TV service returned non-JSON content (HTTP {resp.status})") from None

    def _check_ok(self, resp: Response, key: str, what: str) -> Any:
        doc = self._json(resp, key)
        if resp.status != 200:
            error = doc.get("error") if isinstance(doc, dict) else None
            detail = redact_text(str(error), (key,))[:80] if isinstance(error, str) else "no detail"
            raise DeviceError(f"{what} failed with HTTP {resp.status} ({detail})")
        return doc

    def _envelope(self, identity: Identity, **body: Any) -> dict[str, Any]:
        out = {"identity": identity.as_dict(), **body}
        if self.config.fixture_mode:
            out["mode"] = "FIXTURE MODE (loopback test target, not a TV)"
        return out

    def status(self) -> dict[str, Any]:
        resp, identity, key = self._control("GET", "status")
        doc = self._check_ok(resp, key, "status")
        return self._envelope(identity, status=summarize_status(doc, (key,)))

    def home(self) -> dict[str, Any]:
        # The remote Home key, not POST /home: on the real TV POST /home was accepted but left another app
        # (HBO Max) in the foreground, while key 63 brought the registered Home forward.
        result = self.key("home")
        if not result["injected"]:
            raise DeviceError(f"Home key injection failed (exit code {result['exit_code']}); nothing was accepted")
        result.update(accepted=True, foreground_verified=False,
                      note="Home key injected; the foreground is not verified. Confirm with a fresh tv_capture.")
        return result

    def key(self, name: str) -> dict[str, Any]:
        code = self.config.profile.keys.get(name)
        if code is None:
            raise DeviceError("unknown remote key; allowed: " + ", ".join(self.config.profile.keys))
        resp, identity, key = self._control("POST", f"key/{code}")
        result = summarize_command_result(self._check_ok(resp, key, "remote key"))
        return self._envelope(identity, key=name, code=code, injected=bool(result.get("succeeded")),
                              exit_code=result.get("exit_code"),
                              note="Injection accepted is not proof of effect; confirm with tv_capture.")

    def launch(self, app_id: str) -> dict[str, Any]:
        if app_id not in self.config.allowed_apps:
            raise DeviceError("app ID is not on the owner's allowlist")
        resp, identity, key = self._control("POST", f"launch/{app_id}")
        doc = self._check_ok(resp, key, "launch")
        accepted = isinstance(doc, dict) and doc.get("accepted") is True
        return self._envelope(identity, app_id=app_id, accepted=accepted,
                              note="Launch was queued on the TV; confirm with tv_capture.")

    def diagnostics(self) -> dict[str, Any]:
        resp, identity, key = self._control("POST", "diagnostics")
        doc = self._check_ok(resp, key, "diagnostics")
        return self._envelope(identity, diagnostics=summarize_diagnostics(doc, (key,)))

    def capture(self) -> tuple[cap.Capture, dict[str, Any]]:
        nonce = _secrets.token_hex(16)
        resp, identity, key = self._control("POST", "capture", max_bytes=cap.MAX_CAPTURE_BYTES,
                                            extra_headers={cap.NONCE_HEADER: nonce})
        try:
            shot = cap.validate_capture(resp, nonce=nonce, now_ms=int(self.clock() * 1000),
                                        max_age_s=self.config.capture_max_age_s, seen_ids=self._seen_capture_ids)
        except cap.CaptureError as exc:
            raise DeviceError(redact_text(str(exc), (key,))) from None
        meta = self._envelope(identity, capture=shot.metadata())
        saved = self._save_capture(shot)
        meta["saved_to_private_dir"] = saved
        return shot, meta

    def wake(self) -> dict[str, Any]:
        wake = self.config.wake
        if wake is None:
            raise DeviceError("Wake-on-LAN is off: the owner config has no enabled \"wake\" section; nothing sent")
        now = self.clock()
        if self._last_wake is not None and 0 <= now - self._last_wake < WAKE_COOLDOWN_S:
            raise DeviceError(f"a wake was requested less than {WAKE_COOLDOWN_S:g} s ago; nothing sent")
        self.check_lan()
        self._last_wake = now
        packet = magic_packet(self.config.tv_mac)
        sender = self.udp_sender or send_udp_broadcast
        sent = 0
        for _ in range(wake.packets):
            try:
                sender(packet, (str(wake.broadcast), wake.port))
            except OSError as exc:
                raise DeviceError(f"sending the wake packet failed ({type(exc).__name__}) after {sent} of "
                                  f"{wake.packets} sent") from None
            sent += 1
        # Neither the MAC nor the broadcast address is echoed: results go to a model, and nothing else returns them.
        return {"requested": True, "packets_sent": sent, "port": wake.port,
                "target": "owner-configured TV MAC on the tv.ip/lan_prefix broadcast",
                "awake_verified": False, "identity_checked": False, "note": WAKE_NOTE}

    def _save_capture(self, shot: cap.Capture) -> bool:
        directory = self.config.capture_dir
        if directory is None:
            return False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(shot.captured_at_ms / 1000))
            target = Path(directory) / f"capture-{stamp}-{shot.capture_id}.png"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(shot.png)
            return True
        except OSError:
            return False
