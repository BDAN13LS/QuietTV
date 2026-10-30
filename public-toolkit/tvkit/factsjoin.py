"""Join a hardware-verified ProductInfo report to native runtime observations.

This is the reviewed bridge the device-facts probe (``tools/tv-device-facts``)
and the inspection tool (``tools/tv-inspect``) were built toward, and nothing
more. It turns two INDEPENDENT read-only observations of one TV --

* a ``quiettv-device-facts/1`` ProductInfo report (the Samsung Product API
  values, with the owner DUID hashed on-device), and
* a ``quiettv-inspection/1`` native inventory, read back through the unchanged
  ``tools/tv-inspect/frame.py`` decoder (its ``observedRuntime`` block) --

into explicit ``tvkit-profile/2`` device facts that the EXISTING matchers
(:func:`tvkit.profile.match_device` and :func:`tvkit.recovery2.bind_device`)
accept or refuse. It adds no parallel, more permissive matcher.

What it deliberately does NOT do, and why:

* **No universal transforms.** It never splits a firmware string, never maps
  ``armv7l`` to ``armv7`` or ``9.0`` to ``9.0.0``, and never fills a field from
  the profile it is being matched against. Every ``tvkit-profile/2`` field is
  either copied VERBATIM from one named observation or supplied by a
  caller-trusted :class:`JoinPolicy` as an exact interpretation of the exact
  observed tuple. A policy approves one exact observed combination; it is not a
  rule for other models or firmwares. The one built-in policy is SYNTHETIC.
* **No silent hash-as-raw-id conversion.** The ProductInfo identity scheme
  (``quiettv-device-facts-duid/sha256/1``) is a DIFFERENT domain from the
  profile/capsule device id (``tvkit-device-id/1`` in profile.py). This module
  never writes the facts digest into a ``deviceIdSha256`` slot. When, and only
  when, a policy explicitly selects the facts identity scheme, the joined facts
  carry a clearly scheme-tagged OPAQUE identity token
  (``<scheme>:<digest>``) as their ``deviceId``; the existing
  ``device_id_sha256`` then hashes that token in its own domain, exactly as it
  would any other id. Applying these facts to an OLDER capsule whose identity
  came from a raw id therefore cannot match, and :func:`bind_to_recovery2`
  refuses rather than rebinding.
* **No action.** A :class:`JoinReceipt` is immutable data. Nothing here reads,
  writes, installs, removes, restores or talks to a device or a network. The
  optional :func:`preflight_display` seam is display-only and enables nothing.

Refusals are explicit. Unknown/missing/duplicate/unexpected keys, a wrong
schema, a source/API mismatch, a malformed digest, control/oversize/placeholder
or non-concrete strings, an incomplete report, an absent native runtime, or any
cross-source conflict with the trusted policy all raise before facts exist. The
receipt binds to the policy, the profile, BOTH input digests and the identity
scheme, and is re-validated before any match.
"""

from __future__ import annotations

import hashlib
import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from . import recovery2 as _r2
from . import strictjson as sj
from .paths import is_sha256
from .profile import (MATCH_FIELDS_V2, PROFILE_SCHEMA_V2, Profile, ProfileError,
                      device_id_sha256, match_device)

# ---- contracts these two tools publish (must match exactly; a mismatch refuses) ----

PRODUCT_INFO_SCHEMA = "quiettv-device-facts/1"
PRODUCT_INFO_SOURCE = "samsung-productinfo-webapi"
PRODUCT_INFO_API_REFERENCE = (
    "https://developer.samsung.com/smarttv/develop/api-references/"
    "samsung-product-api-references/productinfo-api.html")
# The facts identity scheme (collector.js IDENTITY_SCHEME). Deliberately NOT the
# profile.DEVICE_ID_DOMAIN scheme; the two never equate.
FACTS_IDENTITY_SCHEME = "quiettv-device-facts-duid/sha256/1"
IDENTITY_SCHEME_NONE = "none"

# Mirrors of the collector's own acceptance rules, applied again to the RETRIEVED
# report so a tampered or placeholder value can never pass here either.
MAX_FIELD_CHARS = 4096
_PLACEHOLDERS = frozenset({"unknown"})

# The exact field set a ProductInfo report carries (collector STRING_FIELDS plus
# the platform-version capability). localSet is opaque and optional for a join.
_PRODUCT_INFO_FIELD_KEYS = (
    "firmware", "realModel", "model", "modelCode", "serviceCountry", "localSet", "platformVersion")
_REQUIRED_OBSERVED = ("firmware", "realModel", "model", "modelCode", "serviceCountry", "platformVersion")

JOIN_POLICY_SCHEMA = "tvkit-facts-join-policy/1"
RECEIPT_SCHEMA = "tvkit-facts-join-receipt/1"
PREFLIGHT_SCHEMA = "tvkit-facts-join-preflight/1"

# Where each tvkit-profile/2 field comes from. "observed" fields are copied
# byte-for-byte from one named observation; "interpreted" fields are supplied by
# the trusted policy. This table is fixed and documented on purpose: the policy
# cannot move a field to a different source, and no code path infers a value from
# the profile. firmwareBuild is the FULL Samsung firmware string from the Product
# API (getFirmware); the native /etc/tizen-release BUILD_ID is a DIFFERENT
# observation and is preserved untouched in the receipt, never substituted here.
FIELD_SOURCES = {
    "model": "observed:productInfo.realModel",
    "arch": "observed:native.machine",
    "firmwareBuild": "observed:productInfo.firmware",
    "firmware": "interpreted",
    "osName": "interpreted",
    "osVersion": "interpreted",
    "region": "interpreted",
}
_OBSERVED_FIELDS = tuple(f for f, s in FIELD_SOURCES.items() if s.startswith("observed:"))
_INTERPRETED_FIELDS = tuple(f for f, s in FIELD_SOURCES.items() if s == "interpreted")
assert set(FIELD_SOURCES) == set(MATCH_FIELDS_V2), "FIELD_SOURCES must cover exactly the profile/2 fields"


class JoinError(ValueError):
    """A reason to refuse a join. No facts, token or receipt was produced.

    It names the field or rule, never an observed value, so a refusal cannot leak
    a device value into a log.
    """


# --------------------------------------------------------------------------
# Reuse the unchanged tv-inspect v1 decoder for the native side

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRAME_PATH = _REPO_ROOT / "tools" / "tv-inspect" / "frame.py"
_frame_lock = threading.Lock()
_frame_cache = None


def _frame_module():
    """Load ``tools/tv-inspect/frame.py`` once, by path, without polluting sys.path.

    The native side is decoded through this module's EXISTING strict boundary
    (``decode_frame`` -> ``validate_document``); it is never re-implemented or
    weakened here.
    """
    global _frame_cache
    with _frame_lock:
        if _frame_cache is None:
            spec = importlib.util.spec_from_file_location("quiettv_tv_inspect_frame", _FRAME_PATH)
            if spec is None or spec.loader is None:
                raise JoinError("the tv-inspect frame decoder could not be located")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _frame_cache = module
        return _frame_cache


# --------------------------------------------------------------------------
# Strict ProductInfo report parsing


def _classify(value: Any) -> str:
    """The collector's own accept/reject classification, re-applied to a value."""
    if type(value) is not str:
        return "not-a-string"
    if len(value) > MAX_FIELD_CHARS:
        return "too-long"
    if value.strip() == "":
        return "empty"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "unsafe"
    if value.strip().lower() in _PLACEHOLDERS:
        return "placeholder"
    if value != value.strip():
        return "non-concrete"  # surrounding whitespace: not a concrete match value
    return "observed"


def _observed_value(fields: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    """Return a concrete observed field value, or refuse. ``None`` iff optional and absent."""
    record = sj.require_object(fields[key], f"productInfo.fields.{key}", ("api", "status"), ("value", "opaque"))
    sj.require_str(record["api"], f"productInfo.fields.{key}.api")
    status = sj.require_str(record["status"], f"productInfo.fields.{key}.status")
    if "value" not in record:
        if required:
            raise JoinError(f"productInfo field {key} was not observed (status {status!r}); refusing an incomplete join")
        return None
    code = _classify(record["value"])
    if code != "observed":
        raise JoinError(f"productInfo field {key} value is {code}; refusing")
    if status != "observed":
        raise JoinError(f"productInfo field {key} carries a value but a non-observed status; refusing")
    return record["value"]


@dataclass(frozen=True)
class ProductInfoReport:
    """A validated ``quiettv-device-facts/1`` report. Every observed string is concrete."""

    firmware: str          # getFirmware(): the full Samsung firmware string, byte-exact
    real_model: str        # getRealModel()
    model: str             # getModel(): opaque, preserved only
    model_code: str        # getModelCode(): opaque, preserved only
    service_country: str   # getSystemConfig(CONFIG_KEY_SERVICE_COUNTRY)
    local_set: str | None  # getLocalSet(): opaque, never interpreted
    platform_version: str  # tizen.systeminfo platform.version capability
    identity_scheme: str
    identity_digest: str | None
    identity_status: str
    report_sha256: str


def parse_product_info(raw: bytes | str) -> ProductInfoReport:
    """Validate a retrieved ProductInfo report. Raw bytes/str, so duplicate keys refuse."""
    if not isinstance(raw, (bytes, str)):
        raise JoinError("ProductInfo report must be raw JSON bytes or text")
    report_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(report_bytes) > 256 * 1024:
        raise JoinError("ProductInfo report is too large")
    try:
        top = sj.require_object(
            sj.loads(report_bytes), "productInfo",
            ("schema", "status", "source", "apiReference", "fields", "identity"))
        if top["schema"] != PRODUCT_INFO_SCHEMA:
            raise JoinError(f"ProductInfo report schema must be {PRODUCT_INFO_SCHEMA}")
        if top["source"] != PRODUCT_INFO_SOURCE:
            raise JoinError("ProductInfo report is not from the Samsung Product API; refusing (source mismatch)")
        if top["apiReference"] != PRODUCT_INFO_API_REFERENCE:
            raise JoinError("ProductInfo report apiReference does not match the Product API; refusing")
        if top["status"] not in ("complete", "incomplete"):
            raise JoinError("ProductInfo report status is not complete/incomplete")
        if top["status"] != "complete":
            raise JoinError("ProductInfo report is incomplete; refusing to join an incomplete observation")
        fields = sj.require_object(top["fields"], "productInfo.fields", _PRODUCT_INFO_FIELD_KEYS)
        values = {key: _observed_value(fields, key, required=key in _REQUIRED_OBSERVED)
                  for key in _PRODUCT_INFO_FIELD_KEYS}
        identity = sj.require_object(top["identity"], "productInfo.identity", ("scheme", "api", "status"), ("digest",))
        scheme = sj.require_str(identity["scheme"], "productInfo.identity.scheme")
        sj.require_str(identity["api"], "productInfo.identity.api")
        status = sj.require_str(identity["status"], "productInfo.identity.status")
        digest = identity.get("digest")
        if digest is not None and not is_sha256(digest):
            raise JoinError("productInfo.identity.digest is malformed; refusing")
    except sj.StrictJsonError as exc:
        raise JoinError(str(exc)) from exc
    return ProductInfoReport(
        firmware=values["firmware"], real_model=values["realModel"], model=values["model"],
        model_code=values["modelCode"], service_country=values["serviceCountry"],
        local_set=values["localSet"], platform_version=values["platformVersion"],
        identity_scheme=scheme, identity_digest=digest, identity_status=status,
        report_sha256=hashlib.sha256(report_bytes).hexdigest())


# --------------------------------------------------------------------------
# Native runtime, through the unchanged tv-inspect decoder


@dataclass(frozen=True)
class NativeRuntime:
    """The ``observedRuntime`` block of a validated ``quiettv-inspection/1`` document.

    ``model``/``firmware``/``region`` are not carried: the v1 validator already
    guarantees the native side never guesses them (``schema-runtime-guessed``).
    """

    tizen_release_path: str
    tizen_release_lines: tuple[str, ...]
    tizen_version: str
    machine: str
    frame_sha256: str


def extract_native_runtime(inspection_frame: bytes) -> NativeRuntime:
    """Decode a framed inspection capture and return its runtime, or refuse.

    The frame is validated by ``tools/tv-inspect/frame.py`` (schema, hash, noise,
    whole document) exactly as the inspection tool's own reader does.
    """
    if type(inspection_frame) is not bytes:
        raise JoinError("native inspection frame must be raw bytes")
    frame = _frame_module()
    try:
        document = frame.decode_frame(inspection_frame)
    except frame.FrameError as exc:
        raise JoinError(f"native inspection frame rejected by the v1 decoder: {exc.code}") from exc
    runtime = document["observedRuntime"]
    if runtime is None:
        raise JoinError("native inspection carries no observedRuntime; refusing an incomplete join")
    release = runtime["tizenRelease"]
    if release is None:
        raise JoinError("native /etc/tizen-release was not observed; refusing")
    if runtime["tizenVersion"] is None or runtime["machine"] is None:
        raise JoinError("native tizenVersion or machine was not observed; refusing")
    return NativeRuntime(
        tizen_release_path=release["path"], tizen_release_lines=tuple(release["lines"]),
        tizen_version=runtime["tizenVersion"], machine=runtime["machine"],
        frame_sha256=hashlib.sha256(inspection_frame).hexdigest())


# --------------------------------------------------------------------------
# The trusted joining policy (caller supplied; the one built-in is synthetic)


@dataclass(frozen=True)
class JoinPolicy:
    """One caller-trusted interpretation of one EXACT observed tuple.

    It records the exact ProductInfo and native values it expects to see and the
    exact profile/2 interpretation for the four interpreted fields. It approves
    this one combination only; it is never a transform or a rule for other TVs.
    """

    profile_id: str
    identity_scheme: str
    product_info: Mapping[str, str]
    native: Mapping[str, Any]
    interpreted: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise JoinError("join policy needs a profile id")
        if self.identity_scheme not in (FACTS_IDENTITY_SCHEME, IDENTITY_SCHEME_NONE):
            raise JoinError("join policy identityScheme must be the facts scheme or 'none'")
        object.__setattr__(self, "product_info", MappingProxyType(dict(self.product_info)))
        object.__setattr__(self, "native", MappingProxyType(dict(self.native)))
        object.__setattr__(self, "interpreted", MappingProxyType(dict(self.interpreted)))

    @property
    def digest(self) -> str:
        return _r2.canonical_digest({
            "schema": JOIN_POLICY_SCHEMA, "profileId": self.profile_id,
            "identityScheme": self.identity_scheme, "productInfo": dict(self.product_info),
            "native": {"tizenReleaseLines": list(self.native["tizenReleaseLines"]),
                       "tizenVersion": self.native["tizenVersion"], "machine": self.native["machine"]},
            "interpreted": dict(self.interpreted)})


def join_policy_from_json(raw: bytes | str) -> JoinPolicy:
    """Parse a caller's trusted policy. Raw bytes/str so duplicate keys refuse."""
    if not isinstance(raw, (bytes, str)):
        raise JoinError("join policy must be raw JSON bytes or text")
    if len(raw) > 256 * 1024:
        raise JoinError("join policy is too large")
    try:
        top = sj.require_object(sj.loads(raw), "join policy",
                                ("schema", "profileId", "identityScheme", "productInfo", "native", "interpreted"))
        if top["schema"] != JOIN_POLICY_SCHEMA:
            raise JoinError(f"join policy schema must be {JOIN_POLICY_SCHEMA}")
        product_info = sj.require_object(top["productInfo"], "join policy.productInfo", _PRODUCT_INFO_FIELD_KEYS)
        product_info = {k: sj.require_str(product_info[k], f"join policy.productInfo.{k}") for k in _PRODUCT_INFO_FIELD_KEYS}
        native = sj.require_object(top["native"], "join policy.native", ("tizenReleaseLines", "tizenVersion", "machine"))
        lines = tuple(sj.require_str(x, "join policy.native.tizenReleaseLines[]")
                      for x in sj.require_list(native["tizenReleaseLines"], "join policy.native.tizenReleaseLines", max_items=32))
        native = {"tizenReleaseLines": lines,
                  "tizenVersion": sj.require_str(native["tizenVersion"], "join policy.native.tizenVersion"),
                  "machine": sj.require_str(native["machine"], "join policy.native.machine")}
        interpreted = sj.require_object(top["interpreted"], "join policy.interpreted", _INTERPRETED_FIELDS)
        interpreted = {k: sj.require_str(interpreted[k], f"join policy.interpreted.{k}") for k in _INTERPRETED_FIELDS}
    except sj.StrictJsonError as exc:
        raise JoinError(str(exc)) from exc
    return JoinPolicy(sj.require_str(top["profileId"], "join policy.profileId"),
                      sj.require_str(top["identityScheme"], "join policy.identityScheme"),
                      product_info, native, interpreted)


# The ONE built-in policy is entirely synthetic and matches only the synthetic
# tvkit-profile/2 fixture (tests/fixtures/consumer/profile-v2.json). It is a
# worked example, not production support: a real TV needs the owner's own
# reviewed policy, supplied through join_policy_from_json. No owner values live
# here.
_SYNTHETIC_POLICY_JSON = """{
  "schema": "tvkit-facts-join-policy/1",
  "profileId": "SYNTHETIC-QTV0001-0000.1-armv7l-tizen9",
  "identityScheme": "quiettv-device-facts-duid/sha256/1",
  "productInfo": {
    "firmware": "SYNTHETIC-BUILD-0000.1",
    "realModel": "SYNTHETIC-QTV0001",
    "model": "SYN_X22U_MODEL",
    "modelCode": "SYN_X22U_CODE",
    "serviceCountry": "SR",
    "localSet": "SR",
    "platformVersion": "9.0"
  },
  "native": {
    "tizenReleaseLines": ["Tizen9/TV 9.0.0 (arm)", "VERSION=9.0.0", "BUILD_ID=T-SYNTHETIC-0000-REL-200001010000"],
    "tizenVersion": "9.0.0",
    "machine": "armv7l"
  },
  "interpreted": {
    "firmware": "0000.1",
    "osName": "Tizen",
    "osVersion": "9",
    "region": "SYNTHETIC-REGION"
  }
}"""


def builtin_join_policies() -> dict[str, JoinPolicy]:
    """The synthetic worked-example policies, keyed by profile id."""
    policy = join_policy_from_json(_SYNTHETIC_POLICY_JSON)
    return {policy.profile_id: policy}


# --------------------------------------------------------------------------
# The join and its receipt


def identity_token(scheme: str, digest: str) -> str:
    """The scheme-tagged opaque identity token used as the facts ``deviceId``.

    It embeds the ProductInfo identity SCHEME and its on-device DUID DIGEST (a
    hash, never a raw DUID). ``device_id_sha256`` later hashes this token in the
    distinct ``tvkit-device-id/1`` domain, so the facts digest is never treated
    as a raw id and the two identity domains never equate.
    """
    return f"{scheme}:{digest}"


@dataclass(frozen=True)
class JoinReceipt:
    """Immutable, data-only proof of one join, bound to everything it depends on.

    It is re-validated (:func:`verify_receipt`) before any match, and it enables
    no action: it is evidence, not a command.
    """

    profile_id: str
    profile_digest: str
    policy_digest: str
    product_info_sha256: str
    inspection_sha256: str
    identity_scheme: str
    facts: Mapping[str, str]
    other_observations: Mapping[str, Any]
    receipt_digest: str

    def device_facts(self) -> dict[str, str]:
        """The tvkit-profile/2 facts for match_device / bind_device. A fresh copy."""
        return dict(self.facts)

    def body(self) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "profileId": self.profile_id,
            "profileDigest": self.profile_digest,
            "policyDigest": self.policy_digest,
            "productInfoSha256": self.product_info_sha256,
            "inspectionSha256": self.inspection_sha256,
            "identityScheme": self.identity_scheme,
            "fieldSources": dict(FIELD_SOURCES),
            "facts": dict(self.facts),
            "otherObservations": dict(self.other_observations),
        }


def _require_observations_match_policy(report: ProductInfoReport, native: NativeRuntime, policy: JoinPolicy) -> None:
    """Exact equality of every observed value against the trusted policy.

    This single gate is the cross-source conflict check: a drifted ProductInfo
    value, a native value that disagrees, or an observation the policy does not
    cover all refuse here, naming the field only.
    """
    observed_product = {
        "firmware": report.firmware, "realModel": report.real_model, "model": report.model,
        "modelCode": report.model_code, "serviceCountry": report.service_country,
        "localSet": report.local_set, "platformVersion": report.platform_version}
    for key in _PRODUCT_INFO_FIELD_KEYS:
        if observed_product[key] != policy.product_info[key]:
            raise JoinError(f"observed ProductInfo field {key} does not match the trusted policy; refusing")
    if native.tizen_release_lines != tuple(policy.native["tizenReleaseLines"]):
        raise JoinError("observed native /etc/tizen-release lines do not match the trusted policy; refusing")
    if native.tizen_version != policy.native["tizenVersion"]:
        raise JoinError("observed native tizenVersion does not match the trusted policy; refusing")
    if native.machine != policy.native["machine"]:
        raise JoinError("observed native machine does not match the trusted policy; refusing")


def join(product_info: bytes | str, inspection_frame: bytes, *, policy: JoinPolicy, profile: Profile) -> JoinReceipt:
    """Validate both observations, interpret them under the trusted policy against
    ``profile``, and return a bound receipt. The authoritative match is
    ``profile.match_device``; this adds no permissive matcher of its own.
    """
    if not isinstance(policy, JoinPolicy):
        raise JoinError("join needs an explicit trusted JoinPolicy object; refusing")
    if not isinstance(profile, Profile) or profile.schema != PROFILE_SCHEMA_V2:
        raise JoinError(f"facts join yields profile/2 facts and needs a {PROFILE_SCHEMA_V2} profile; refusing")
    if policy.profile_id != profile.profile_id:
        raise JoinError("the trusted policy was written for a different profile; refusing")

    report = parse_product_info(product_info)
    native = extract_native_runtime(inspection_frame)
    _require_observations_match_policy(report, native, policy)

    # Build the profile/2 facts: observed fields copied verbatim from the ACTUAL
    # observations, interpreted fields taken from the trusted policy. Nothing is
    # taken from the profile.
    facts: dict[str, str] = {
        "model": report.real_model,
        "arch": native.machine,
        "firmwareBuild": report.firmware,
    }
    for field in _INTERPRETED_FIELDS:
        facts[field] = policy.interpreted[field]

    # The scheme-tagged opaque identity token, only when the policy selects it.
    if policy.identity_scheme == FACTS_IDENTITY_SCHEME:
        if report.identity_scheme != FACTS_IDENTITY_SCHEME:
            raise JoinError("ProductInfo identity scheme does not match the policy scheme; refusing")
        if report.identity_status != "observed" or not is_sha256(report.identity_digest):
            raise JoinError("ProductInfo identity was not observed with a valid digest; refusing")
        facts["deviceId"] = identity_token(report.identity_scheme, report.identity_digest)

    # Authoritative exact match: the existing matcher, never a parallel one.
    match_device(profile, facts)

    other_observations = {
        "productInfoModel": report.model,
        "productInfoModelCode": report.model_code,
        "productInfoLocalSet": report.local_set,
        "productInfoPlatformVersion": report.platform_version,
        "nativeTizenReleasePath": native.tizen_release_path,
        "nativeTizenReleaseLines": list(native.tizen_release_lines),
        "nativeTizenVersion": native.tizen_version,
        "firmwareBuildNote": ("firmwareBuild is the Samsung full firmware string (getFirmware); "
                              "the native /etc/tizen-release BUILD_ID is a distinct observation preserved above "
                              "and is never substituted for it"),
    }
    draft = JoinReceipt(
        profile_id=profile.profile_id, profile_digest=_r2.profile_digest(profile), policy_digest=policy.digest,
        product_info_sha256=report.report_sha256, inspection_sha256=native.frame_sha256,
        identity_scheme=policy.identity_scheme, facts=MappingProxyType(dict(facts)),
        other_observations=MappingProxyType(dict(other_observations)), receipt_digest="")
    return JoinReceipt(**{**draft.__dict__, "receipt_digest": _r2.canonical_digest(draft.body())})


def verify_receipt(receipt: JoinReceipt) -> JoinReceipt:
    """Re-derive the receipt digest; a tampered receipt refuses before any match."""
    if not isinstance(receipt, JoinReceipt):
        raise JoinError("not a join receipt")
    if receipt.receipt_digest != _r2.canonical_digest(receipt.body()):
        raise JoinError("join receipt digest does not bind its contents; refusing")
    return receipt


# --------------------------------------------------------------------------
# Binding to an existing recovery/2 capsule (never rebinding a legacy identity)


def bind_to_recovery2(receipt: JoinReceipt, capsule_path, profile: Profile, *,
                      external_policy, boot_policy) -> "_r2.Recovery2":
    """Prove the joined facts match a recovery/2 capsule, through the EXISTING
    ``recovery2.validate`` / ``bind_device`` path. No parallel matcher, no rebind.

    Refuses unless the receipt carries a scheme-tagged facts identity: without
    one there is no ``deviceId`` to hash, and a legacy capsule whose identity came
    from a raw id will never equal ``device_id_sha256(<facts token>)``, so it is
    refused by ``bind_device`` rather than rebound.
    """
    verify_receipt(receipt)
    if receipt.identity_scheme != FACTS_IDENTITY_SCHEME:
        raise JoinError("joined facts carry no scheme-tagged device identity; refusing to bind (never rebinding a "
                        "legacy capsule identity)")
    if not isinstance(profile, Profile) or profile.schema != PROFILE_SCHEMA_V2:
        raise JoinError(f"binding joined facts needs a {PROFILE_SCHEMA_V2} profile; refusing")
    if receipt.profile_id != profile.profile_id or receipt.profile_digest != _r2.profile_digest(profile):
        raise JoinError("join receipt was produced for a different profile; refusing")
    # recovery2.validate re-runs match_device AND bind_device (field equality plus
    # device_id_sha256 of the token against the capsule). It is the authority.
    return _r2.validate(capsule_path, profile, receipt.device_facts(),
                        external_policy=external_policy, boot_policy=boot_policy)


# --------------------------------------------------------------------------
# Offline preflight-display seam (display only; enables nothing)


def preflight_display(receipt: JoinReceipt) -> dict[str, Any]:
    """A display-only projection of a clean receipt for a setup PREFLIGHT screen.

    It shows the matched profile/2 fields and the identity scheme. It carries no
    capability, no enabled action, no transport and no device id token: consuming
    it can never turn on backup, install, remove or restore. It raises if the
    receipt does not re-validate, so a tampered receipt never reaches a screen.
    """
    verify_receipt(receipt)
    return {
        "schema": PREFLIGHT_SCHEMA,
        "profileId": receipt.profile_id,
        "identityScheme": receipt.identity_scheme,
        "fields": {field: receipt.facts[field] for field in MATCH_FIELDS_V2},
        "fieldSources": dict(FIELD_SOURCES),
        "actionsEnabled": {},
        "capabilities": {},
        "note": ("Preflight display only. This result performs and enables no device action "
                 "(no backup, install, remove or restore) and uses no network or transport."),
    }
