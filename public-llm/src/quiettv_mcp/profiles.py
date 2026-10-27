"""Supported device profiles. Anything not listed here is rejected, not guessed at."""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

# Linux input key codes accepted by the TV-side RemoteProbe allowlist.
KEY_CODES: Mapping[str, int] = MappingProxyType({
    "back": 1,
    "ok": 28,
    "home": 63,
    "volume_down": 67,
    "volume_up": 68,
    "up": 103,
    "left": 105,
    "right": 106,
    "down": 108,
})

KeyName = Literal["back", "ok", "home", "volume_down", "volume_up", "up", "left", "right", "down"]

# Application IDs accepted by the TV-side launch allowlist for this profile.
APP_IDS: tuple[str, ...] = (
    "5b8c3eb16b.BeamCTVDev",
    "9Ur5IzDKqV.TizenYouTube",
    "EYm8vc1St4.Philo",
    "LBUAQX1exg.Hulu",
    "MCmYXNxgcu.DisneyPlus",
    "Ob3XhAVMu6.amcplus",
    "PvWgqxV3Xa.YouTubeTV",
    "XtU8fQk2gN.ESPN",
    "com.samsung.tv.aria-video",
    "com.samsung.tv.quicksetting",
    "com.samsung.tv.store",
    "gzcc4LRFBF.Peacock",
    "org.tizen.browser",
    "org.tizen.netflix-app",
    "org.tizen.primevideo",
    "quiet.iptv",
    "rJyOSqC6Up.CBSAllAccess",
)

AppId = Literal[
    "5b8c3eb16b.BeamCTVDev",
    "9Ur5IzDKqV.TizenYouTube",
    "EYm8vc1St4.Philo",
    "LBUAQX1exg.Hulu",
    "MCmYXNxgcu.DisneyPlus",
    "Ob3XhAVMu6.amcplus",
    "PvWgqxV3Xa.YouTubeTV",
    "XtU8fQk2gN.ESPN",
    "com.samsung.tv.aria-video",
    "com.samsung.tv.quicksetting",
    "com.samsung.tv.store",
    "gzcc4LRFBF.Peacock",
    "org.tizen.browser",
    "org.tizen.netflix-app",
    "org.tizen.primevideo",
    "quiet.iptv",
    "rJyOSqC6Up.CBSAllAccess",
]


@dataclass(frozen=True)
class DeviceProfile:
    profile_id: str
    model: str
    firmware: str
    firmware_build: str
    os: str
    os_version: str
    arch: str
    keys: Mapping[str, int]
    apps: tuple[str, ...]
    full_builds: tuple[str, ...] = ()

    def firmware_status(self, reported: object) -> Literal["match", "mismatch", "unreported"]:
        """Compare a device-reported firmware string with this profile.

        Samsung's 8001 identity endpoint often reports "Unknown"; that is
        "unreported", never "match". A recorded full build matches only exactly.
        """
        if not isinstance(reported, str) or reported.strip().lower() in ("", "unknown", "none", "null"):
            return "unreported"
        if reported.strip().upper() in self.full_builds:
            return "match"
        normal = re.sub(r"[^A-Z0-9.]", "", reported.upper())
        accepted = {self.firmware, re.sub(r"[^A-Z0-9.]", "", (self.firmware_build + self.firmware).upper())}
        return "match" if normal in accepted else "mismatch"


UN43U8000FBXZA_1300_8 = DeviceProfile(
    profile_id="UN43U8000FBXZA-1300.8",
    model="UN43U8000FBXZA",
    firmware="1300.8",
    firmware_build="T-X22UDFAKUC",
    os="Tizen",
    os_version="9",
    arch="armv7",
    keys=KEY_CODES,
    apps=APP_IDS,
    full_builds=("T-X22UDFAKUC-0090-1300.8",),
)

PROFILES: Mapping[str, DeviceProfile] = MappingProxyType({UN43U8000FBXZA_1300_8.profile_id: UN43U8000FBXZA_1300_8})
