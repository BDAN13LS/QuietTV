# Facts join: ProductInfo + native runtime -> profile/2 device facts

`tvkit/factsjoin.py` is the reviewed adapter the device-facts probe
(`tools/tv-device-facts`) and the inspection tool (`tools/tv-inspect`) were
built toward. It joins two independent read-only observations of one TV into
explicit `tvkit-profile/2` device facts that the **existing** matchers accept or
refuse. It adds no parallel, more permissive matcher, no transport, no install,
and no new identity semantics.

## Inputs

| input | contract | how it is validated |
|---|---|---|
| ProductInfo report | `quiettv-device-facts/1` (collector.js) | `parse_product_info` (strict; source, apiReference, schema, exact field set, concrete observed values, a 64-hex identity digest) |
| native inventory | `quiettv-inspection/1` framed stdout | `extract_native_runtime` -> the unchanged `tools/tv-inspect/frame.py` `decode_frame` (schema, hash, noise, whole document); the `observedRuntime` block only |

The native side is decoded through the inspection tool's own v1 boundary; it is
never re-implemented or weakened here. `model`/`firmware`/`region` are always
`null` on the native side (the v1 validator forbids native guessing), so the
join never reads them from there.

## Field sources (fixed, documented, no transforms)

| profile/2 field | source |
|---|---|
| `model` | observed: ProductInfo `getRealModel()`, verbatim |
| `arch` | observed: native `uname` machine, verbatim (never `armv7l`->`armv7`) |
| `firmwareBuild` | observed: ProductInfo `getFirmware()` full string, verbatim |
| `firmware` | interpreted by the trusted policy (the short Samsung revision) |
| `osName` | interpreted by the trusted policy |
| `osVersion` | interpreted by the trusted policy (never `9.0`->`9.0.0`, never truncation) |
| `region` | interpreted by the trusted policy (never inferred from a model suffix) |

There is **no universal firmware split and no suffix rule**. A `JoinPolicy`
approves exactly one observed tuple -> one interpretation; it is not a rule for
other models or firmwares. The only built-in policy is **synthetic** (matches
the synthetic `tests/fixtures/consumer/profile-v2.json`); a real TV needs the
owner's own reviewed policy, supplied through `join_policy_from_json`.

`firmwareBuild` is the Samsung full firmware string. The native
`/etc/tizen-release` `BUILD_ID` is a **different** observation; it is preserved
in `receipt.other_observations` and is never substituted for `firmwareBuild`.
The ProductInfo `platformVersion` is likewise preserved and kept distinct from
the interpreted `osVersion`. The network API's literal `Unknown` firmware never
reaches a match: the source must be the Product API and `Unknown` is rejected as
a placeholder.

## Identity (no silent hash-as-raw-id conversion)

The ProductInfo identity scheme `quiettv-device-facts-duid/sha256/1` is a
different domain from the profile/capsule id `tvkit-device-id/1`. This module
**never** writes the facts digest into a `deviceIdSha256` slot. When, and only
when, a policy explicitly selects the facts scheme, the joined facts carry a
scheme-tagged **opaque** token as their `deviceId`:

    deviceId = "quiettv-device-facts-duid/sha256/1:<on-device DUID digest>"

`device_id_sha256` then hashes that token in its own `tvkit-device-id/1` domain,
exactly as for any id. A capsule whose identity came from a raw id therefore can
never equal `device_id_sha256(<facts token>)`, so `bind_to_recovery2` **refuses
rather than rebinds** a legacy capsule. No raw DUID is ever an input, output or
log line.

## Receipt

`join(...)` returns an immutable, data-only `JoinReceipt` bound to the policy
digest, the profile digest, **both** input SHA-256 digests and the identity
scheme. `verify_receipt` re-derives its digest before any match, so a tampered
receipt refuses before it can reach a matcher or a screen.

## Public API and parent invocation

```python
from tvkit import factsjoin as fj
from tvkit.profile import parse_profile

profile = parse_profile(load_your_profile2_json())     # a tvkit-profile/2
policy  = fj.join_policy_from_json(your_trusted_policy) # your reviewed exact interpretation

receipt = fj.join(product_info_bytes, inspection_frame_bytes, policy=policy, profile=profile)
facts   = receipt.device_facts()        # 8 profile/2 fields; deviceId is the opaque token

# Exact match is the EXISTING matcher; factsjoin adds no permissive matcher:
from tvkit.profile import match_device
match_device(profile, facts)

# Prove a recovery/2 capsule belongs to this exact TV, through the existing path:
result = fj.bind_to_recovery2(receipt, capsule_path, profile,
                              external_policy=ext_policy, boot_policy=boot_policy)
assert result.device_bound

# Optional offline preflight DISPLAY (enables nothing):
screen = fj.preflight_display(receipt)   # fields + sources only; no capability, no action, no token
```

The parent exercises this against their saved private TV observations with the
**same public API** and their own reviewed `JoinPolicy`; no owner values or
secrets live in this repository.

## Boundaries (what this slice does not do)

- No device read/write, no network, no transport, no install/remove/restore.
  A `JoinReceipt` is evidence, not a command; a bound `recovery2` result still
  refuses to expose a capsule/1 apply surface (`payload_verified` raises).
- Existing profile identity semantics, the native source/decoder-v1 contract,
  capsule/recovery exact-matching and the export allowlist are **unchanged**.
- No production profile is added and no universal "read any TV" support is
  claimed; the built-in policy is a synthetic worked example only.

## Remaining setup wiring (stated honestly)

The library boundary is sufficient and is all this slice ships. `factsjoin` is
**not** wired into the live Helper (`helper/session.py`, which reads only the
TV's network info page and cannot see these facts) or the CLI; `preflight_display`
is a pure offline seam a future setup screen may consume when clean. Wiring a
setup/CLI preflight path, and supplying real per-TV policies, are separate
reviewed steps and are out of scope here.
