# Recovery capsule `tvkit-recovery/2` (D5, offline slice)

`tvkit/recovery2.py` holds a strict validator and a pure undo planner for the
versioned recovery schema that capsule/1 cannot represent. This is the
**offline** slice. There is no transport, no extraction, no writer and no
hardware proof. "Restore Samsung" is **not** claimed by this document. The
hardware exit of D5 is still open.

## What it adds over capsule/1

| | capsule/1 | recovery/2 |
|---|---|---|
| Profile | `tvkit-profile/1` only | `tvkit-profile/2` only |
| Device block | 5 v1 match fields + `deviceIdSha256` | **every** `profile.match_fields` (adds `firmwareBuild`, `region`) + `deviceIdSha256` |
| External symlinks | always refused | `externalRefs[]`, exact match against a trusted policy |
| Boot changes | none | `bootChanges[]`, per target/field, names approved by a trusted policy |
| Plans | `tvkit.plan` setup/restore | `recovery2.plan_undo` (boot fields only); `tvkit.plan` **refuses** |

The layout (`capsule.json`, `payload/<id>.tar`, `xattrs/<id>.json`, as a
folder or one uncompressed tar) and the payload verifier are shared. The tar
stream, the xattr cross-check, the entry table rules, protected package ids,
hard-link/device refusal and the "nothing below a symlink" rule all come
from `tvkit/capsule.py` unchanged.

## Dispatch

1. `capsule.open_source(path)` decides **only** folder versus bundle.
2. `capsule.manifest_schema(path)` reads and strictly parses `capsule.json`
   (duplicate keys refused) and returns its exact `schema` string.
3. `recovery2.validate_any(...)` picks the validator from that string:
   - `tvkit-capsule/1` → `capsule.validate` (unchanged). Passing policies is refused.
   - `tvkit-recovery/2` → `recovery2.validate`. Both policies are required.
   - anything else is refused.

Each validator re-checks the schema itself, so a file swapped between the two
reads is refused. `capsule.validate` stays capsule/1 only. Given a recovery/2
capsule, it refuses with a profile/2 ("cannot bind firmware build or region")
and with a profile/1 ("unknown properties bootChanges, externalRefs").

## Manifest

Every object has an exact key set. Unknown keys, duplicate JSON keys and
wrong types are refused.

```json
{
  "schema": "tvkit-recovery/2",
  "profileId": "...",
  "provenance": "owner-device-backup",
  "device": {"model": "", "firmware": "", "arch": "", "osName": "", "osVersion": "",
             "firmwareBuild": "", "region": "", "deviceIdSha256": ""},
  "packages": ["same as capsule/1"],
  "externalRefs": [{"packageId": "org.example.delta", "linkPath": "bin/runtime",
                    "target": "/usr/bin/example-runtime", "access": "read-only",
                    "targetEvidence": {"kind": "file", "sha256": "", "size": 0,
                                       "uid": 0, "gid": 0, "mode": 493}}],
  "registryChanges": ["same as capsule/1"],
  "systemKeyChanges": ["same as capsule/1"],
  "bootChanges": [{"target": "bootcache-record:org.example.delta", "field": "autostart",
                   "before": "true", "ours": "false", "beforeSha256": "", "oursSha256": ""}],
  "ourPackages": ["same as capsule/1"]
}
```

All examples are synthetic. `/usr/bin/example-runtime` is not a real firmware object.

## Trusted policies (caller supplied, never from the capsule)

Both policies are frozen objects built by bundled or owner code, or loaded
from raw JSON bytes (`external_policy_from_json`, `boot_policy_from_json`;
a `dict` is refused because it cannot show duplicate keys). Each is bound to
one `profileId`. Any other type, including a dict with `"allow": true`, is
refused. Their canonical digests go into every undo plan.

**`ExternalRefPolicy`**: one `ExternalApproval` per exact
`(packageId, linkPath)`. Each approval carries one exact target and its file
evidence (`kind`, `sha256`, `size`, `uid`, `gid`, `mode`).

- There are no root, prefix or wildcard exemptions.
- A manifest ref with no approval is **unknown** and is refused (the component is held).
- A ref whose target or evidence differs from its approval is **stale** and is refused.
- The capsule/1 parser is given only these exact `(linkPath, target)` pairs
  (`capsule._is_approved_external`). Any other link is checked exactly as
  before. That includes a link that walks *through* the approved one, such as
  `alias -> runtime` or `alias -> runtime/..`.
- The link must be a real symlink entry in its package, never the package
  root `.`. Nothing may sit below it, in either the entry table or the tar.
- The target must be an absolute exact path, outside every install root and
  never an ancestor of one. `/dev`, `/proc` and `/sys` are refused.
- Only `kind: "file"` is accepted. Directory targets are deferred.

**`BootFieldPolicy`**: one `BootField(target, field, values)` per approved
field, with an exact `frozenset` of 1 to 64 values of at most 256 characters
each.

- `target` is `<bootcache-record|service>:<recordId>`. It is never a path.
- Whole-thing names (`all`, `blob`, `raw`, `cache`, `data`, `*`, …) are
  refused as a record or a field, at policy construction and again at
  manifest parse (`refuse_opaque_whole`).
- Binary boot-cache parsing is **deferred**. A complete cache can never be
  an approved opaque field. A reader that cannot produce per-field values
  cannot produce evidence, so undo refuses.

## Boot changes and the undo plan

Each `bootChanges[]` item is refused if any of these hold:

- its target/field is not approved;
- a value is not in the approved set;
- `before == ours`;
- either digest does not bind its value;
- the `(target, field)` pair is duplicated;
- the record id is protected by the profile.

`plan_undo(path, profile, device_facts, evidence_bytes, *, external_policy, boot_policy)`:

1. Re-validates the capsule from its **path** against **mandatory** live facts.
   It never trusts a validated object a caller could construct.
2. Parses the reader evidence (`tvkit-recovery-evidence/1`, raw bytes):
   - `bootRecords[] {target, field, value, valueSha256}`;
   - `externalTargets[] {target, targetEvidence}`.

   The evidence must name the same profile, device id hash and capsule
   `manifestSha256`, and cover **exactly** the capsule's boot fields and
   external targets. A digest mismatch, a duplicate or an unknown key refuses.
   An external target whose evidence differs is stale and refuses.
3. Three-way compare per field (`classify_current`):
   - current equals `ours` → one compare-and-set action that writes `before`;
   - current equals `before` → no-op;
   - otherwise → **refuse the whole plan**. Every field is classified before
     any action is built.
4. Returns a frozen `UndoPlan`. It binds `profileDigest`, `deviceIdSha256`,
   `manifestSha256`, both policy digests and `evidenceDigest`, and has a
   `planDigest` over the canonical body.

Unlisted records and fields never appear in a plan. Running the plan and
re-reading gives a plan with zero actions (idempotent).
`verify_undo_plan(plan, …same inputs…)` re-derives the plan and refuses any
difference, so a consumer never acts on a bool or an unchecked object.

## Guards and their both-ways tests

`tests/test_public_toolkit_recovery2.py` switches each guard off in memory
and shows that the protected outcome changes:

- `require_profile2`
- `bind_device`
- `check_external_ref`
- `capsule._is_approved_external` (exact pair only)
- `refuse_opaque_whole`
- `classify_current` (three-way)
- `_bind_evidence`
- `Recovery2.payload_verified`. With this guard off, `tvkit.plan.restore_plan`
  silently drops the boot changes. That drop is the reason the guard exists.

The existing `dev/guard_off_check.py` mutations for capsule/1 still apply
unchanged.

## Known gaps (named, not built)

- **No production profile/2, no real build or region values,** and no exact
  security-id override (`profiles/components/reviewed/`).
- **Assemble path (offline, PC only).** `assemble.assemble_recovery2(spec, profile,
  device_facts, out, *, external_policy, boot_policy, target_evidence)` builds a
  recovery/2 capsule from a capture, under its own spec schema
  `tvkit-assemble-spec/2` (packages, registryChanges, systemKeys, bootChanges,
  ourPackages; no `externalRefs`, `device` or policy key — the spec cannot
  self-authorize). It binds every profile/2 fact from the given facts (build,
  region and the hashed device id); a missing or unknown fact refuses and no
  value is taken from the expected profile. An external loader link is kept only
  when a payload symlink exactly matches an `ExternalRefPolicy` approval, and its
  file evidence comes from the caller's `target_evidence`, which must cover
  exactly the referenced targets — never copied from the policy. The in-memory
  manifest goes through the real validator (`recovery2.parse_manifest`) before
  any folder is created, and `recovery2.validate` runs again on the written
  capsule. Every profile, policy, fact, spec and manifest refusal happens before
  the output folder exists; a source that changes mid-copy or a raced destination
  is refused without overwriting the raced-in file (each copy opens its target
  exclusively) and before `capsule.json` is written. There is **no global
  atomic-failure promise**, though: once the copy loop begins, a source-copy race
  may leave a partially written folder (as for capsule/1), and the final on-disk
  `recovery2.validate` runs only after `capsule.json` is written, so an xattr or
  payload-byte failure there leaves a folder that contains `capsule.json` and is
  invalid. A refusal means only that no validated capsule was produced and nothing
  pre-existing was overwritten; the caller discards the output folder. capsule/1
  `assemble` is unchanged and still refuses a loader-link payload and a profile/2.
  **No CLI command is wired for it; there is still no consumer, writer or TV restore.**
- **No consumer of `UndoPlan`.** There is no typed writer, no transport
  capability, no CLI command, and no wiring into launcher or root-helper
  removal ordering. The design order still holds: our changes first, then
  control verified, then the launcher and root helper removed last. It is not
  encoded here.
- **Not covered:** restore of `registryChanges`/`systemKeyChanges`/packages for
  recovery/2. `tvkit.plan` refuses recovery/2, and no recovery/2 restore-plan
  builder exists yet.
- **Not yet supported:**
  - external directory targets;
  - relative external links;
  - empty boot values;
  - regex-typed (non-enumerated) boot values.
- **Export:** the two fixtures in `tests/fixtures/recovery2/` are listed by
  exact path in `export-allowlist.json`. `test_public_toolkit_v2_integration`
  exports to a fresh folder and runs this suite from the exported copy.
- **Not proven on hardware.** The D5 exit, "Restore Samsung over our boot
  changes", is still open.
