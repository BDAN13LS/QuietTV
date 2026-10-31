"""The reviewed public provider: ``owner-sdk-root``.

This package is the reusable implementation behind that one fixed name. It is
constructed only from an explicit owner-private JSON configuration outside every
repository; nothing is registered by importing this package, so
``tvkit.liveops.LIVEOPS`` stays empty unless a parent explicitly asks for it.
"""

from __future__ import annotations

from .channel import ChannelError, LiveChannel, ProbeEvidence, SdkOutcome
from .config import ProviderConfig, ProviderConfigError, load_config, revalidate
from .session import PROVIDER_NAME, ProviderOps, ProviderSession, load_provider

DEFAULT_CHANNEL_FACTORY = LiveChannel

__all__ = [
    "ChannelError",
    "DEFAULT_CHANNEL_FACTORY",
    "LiveChannel",
    "ProbeEvidence",
    "ProviderConfig",
    "ProviderConfigError",
    "ProviderOps",
    "ProviderSession",
    "PROVIDER_NAME",
    "SdkOutcome",
    "load_config",
    "load_provider",
    "revalidate",
]
