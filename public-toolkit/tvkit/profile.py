"""Device profiles: the exact model/firmware/arch/OS this toolkit supports.

Matching is exact string equality on every field. There is no prefix,
substring, case-folding or "close enough" match: an unknown TV is rejected,
never guessed at.

``tvkit-profile/2`` adds two match fields, ``firmwareBuild`` and ``region``.
Version 1 profiles stay readable and match on the version 1 fields only.
``MATCH_FIELDS`` is unchanged on purpose: the capsule/1 device block is
built from it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from . import strictjson as sj
from .paths import check_absolute_posix

PROFILE_SCHEMA = "tvkit-profile/1"
PROFILE_SCHEMA_V2 = "tvkit-profile/2"
MATCH_FIELDS = ("model", "firmware", "arch", "osName", "osVersion")
V2_FIELDS = ("firmwareBuild", "region")
MATCH_FIELDS_V2 = MATCH_FIELDS + V2_FIELDS
SCHEMA_MATCH_FIELDS = {PROFILE_SCHEMA: MATCH_FIELDS, PROFILE_SCHEMA_V2: MATCH_FIELDS_V2}
DEVICE_ID_DOMAIN = "tvkit-device-id/1\n"
PLACEHOLDER_PREFIX = "<CONFIGURE"
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    profile_id: str
    match: Mapping[str, str]
    install_roots: tuple[str, ...]
    registry_table: str
    registry_key: str
    registry_fields: tuple[str, ...]
    registry_values: frozenset[str]
    system_keys: tuple[str, ...]
    protected_substrings: tuple[str, ...]
    manual_prerequisites: tuple[str, ...]
    schema: str = PROFILE_SCHEMA

    def __post_init__(self) -> None:
        # A read-only view, so a caller cannot weaken a cached profile by editing its match.
        object.__setattr__(self, "match", MappingProxyType(dict(self.match)))

    @property
    def match_fields(self) -> tuple[str, ...]:
        """Fixed per schema, never taken from the match mapping itself."""
        if self.schema not in SCHEMA_MATCH_FIELDS:
            raise ProfileError("profile schema is not supported")
        return SCHEMA_MATCH_FIELDS[self.schema]

    def is_protected(self, identifier: str) -> bool:
        lowered = identifier.lower()
        return any(token in lowered for token in self.protected_substrings)


def is_placeholder(value: Any) -> bool:
    return type(value) is str and value.strip().startswith(PLACEHOLDER_PREFIX)


def device_id_sha256(device_id: str) -> str:
    """Domain-separated hash, so a capsule stored on a NAS never holds the raw id."""
    if type(device_id) is not str or device_id == "" or is_placeholder(device_id):
        raise ProfileError("device identifier is missing or still a placeholder")
    return hashlib.sha256((DEVICE_ID_DOMAIN + device_id).encode("utf-8")).hexdigest()


def parse_profile(data: Any) -> Profile:
    try:
        top = sj.require_object(
            data,
            "profile",
            ("schema", "profileId", "match", "installRoots", "registry",
             "systemKeys", "protectedIdSubstrings", "manualPrerequisites"),
        )
        schema = top["schema"]
        if type(schema) is not str or schema not in SCHEMA_MATCH_FIELDS:
            raise ProfileError(f"profile schema must be {PROFILE_SCHEMA} or {PROFILE_SCHEMA_V2}")
        fields = SCHEMA_MATCH_FIELDS[schema]
        profile_id = sj.require_str(top["profileId"], "profile.profileId")
        match = sj.require_object(top["match"], "profile.match", fields)
        for field in fields:
            value = sj.require_str(match[field], f"profile.match.{field}")
            if is_placeholder(value) or value != value.strip():
                raise ProfileError(f"profile.match.{field} is not a concrete value")
        roots = tuple(
            check_absolute_posix(root, "profile.installRoots[]")
            for root in sj.require_list(top["installRoots"], "profile.installRoots", max_items=8)
        )
        registry = sj.require_object(
            top["registry"], "profile.registry", ("table", "key", "fields", "values")
        )
        system_keys = tuple(
            sj.require_str(key, "profile.systemKeys[]")
            for key in sj.require_list(top["systemKeys"], "profile.systemKeys", max_items=16)
        )
        protected = tuple(
            sj.require_str(token, "profile.protectedIdSubstrings[]").lower()
            for token in sj.require_list(
                top["protectedIdSubstrings"], "profile.protectedIdSubstrings", max_items=128
            )
        )
        prereqs = tuple(
            sj.require_str(item, "profile.manualPrerequisites[]")
            for item in sj.require_list(
                top["manualPrerequisites"], "profile.manualPrerequisites", max_items=32
            )
        )
        return Profile(
            profile_id=profile_id,
            match={field: match[field] for field in fields},
            install_roots=roots,
            registry_table=sj.require_str(registry["table"], "profile.registry.table"),
            registry_key=sj.require_str(registry["key"], "profile.registry.key"),
            registry_fields=tuple(
                sj.require_str(f, "profile.registry.fields[]")
                for f in sj.require_list(registry["fields"], "profile.registry.fields", max_items=16)
            ),
            registry_values=frozenset(
                sj.require_str(v, "profile.registry.values[]")
                for v in sj.require_list(registry["values"], "profile.registry.values", max_items=16)
            ),
            system_keys=system_keys,
            protected_substrings=protected,
            manual_prerequisites=prereqs,
            schema=schema,
        )
    except sj.StrictJsonError as exc:
        raise ProfileError(str(exc)) from exc


def load_profile(path: str | Path) -> Profile:
    try:
        return parse_profile(sj.load_file(path, max_bytes=256 * 1024))
    except sj.StrictJsonError as exc:
        raise ProfileError(str(exc)) from exc


def builtin_profiles() -> dict[str, Profile]:
    result: dict[str, Profile] = {}
    for path in sorted(PROFILES_DIR.glob("*.json")):
        profile = load_profile(path)
        if profile.profile_id in result:
            raise ProfileError(f"duplicate built-in profile {profile.profile_id}")
        result[profile.profile_id] = profile
    return result


def get_profile(profile_id: str) -> Profile:
    profiles = builtin_profiles()
    if profile_id not in profiles:
        raise ProfileError(
            "unsupported profile; this toolkit supports only: " + ", ".join(sorted(profiles))
        )
    return profiles[profile_id]


def parse_device_facts(data: Any) -> dict[str, str]:
    """Facts the owner's transport read from the TV: the match fields plus deviceId.

    ``firmwareBuild`` and ``region`` are optional here (version 1 facts stay
    valid); a version 2 profile refuses facts that lack them in ``match_device``.
    """
    try:
        facts = sj.require_object(data, "device facts", MATCH_FIELDS + ("deviceId",), V2_FIELDS)
        result = {key: sj.require_str(facts[key], f"device facts.{key}") for key in facts}
    except sj.StrictJsonError as exc:
        raise ProfileError(str(exc)) from exc
    for key in V2_FIELDS:
        if key in result and (is_placeholder(result[key]) or result[key] != result[key].strip()):
            raise ProfileError(f"device facts.{key} is not a concrete value")
    return result


def match_device(profile: Profile, facts: Mapping[str, str]) -> None:
    """Exact equality on every field. Raises naming the field, never the value."""
    for field in profile.match_fields:
        if field not in profile.match:
            raise ProfileError(f"profile {profile.profile_id} has no {field} to match; refusing")
        if facts.get(field) != profile.match[field]:
            raise ProfileError(
                f"device {field} does not exactly match profile {profile.profile_id}; refusing"
            )
