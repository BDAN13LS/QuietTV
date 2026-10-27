# D2 offline readback gate

`tvkit/backup.py`. This is plan phase D2 of
`docs/superpowers/plans/2026-10-27-consumer-install.md`, offline slice only.
It proves that a backup stored off the TV is byte-for-byte the backup the TV
hashed, bound to that exact device. It does not capture anything, talk to a
TV, rehearse or restore.

## API

```python
from tvkit.backup import readback_gate, GateResult, ReadbackRefused

result = readback_gate(stored_path, device_hashes, facts, *, baseline="current-state")
```

- `stored_path`: a capsule folder or an uncompressed `.tar` bundle, in the
  unchanged `tvkit-capsule/1` layout.
- `device_hashes`: `{capsule file name: sha256}` for **exactly** the files the
  TV produced: every package's `payload/<id>.tar` and `xattrs/<id>.json`.
  `capsule.json` is assembled on the PC, so it is not part of this set.
  Keys are compared exactly, with no case-folding or normalisation. Values
  must be 64 lowercase hex characters.
- `facts`: live device facts in the `profile.parse_device_facts` shape (the
  match fields plus `deviceId`). Required: `None` is refused.
- `baseline`: keyword-only. It must be exactly `"stock"` or `"current-state"`
  (the default).

It returns a frozen `GateResult` only on a full pass. Every refusal raises
`ReadbackRefused`, which is a subclass of `capsule.CapsuleError`, so existing
handlers catch it. There is no pass/fail field and no flag that skips a check.

### What the gate does, in order

1. Checks the baseline label and copies the device-hash mapping.
2. Picks the profile from the live facts. It must be exactly one built-in
   profile matched by `match_device`; the capsule never chooses its own.
3. Hashes every stored file through `capsule.open_source`, so the existing
   folder and bundle rules apply (no symlinks, special files, links in
   bundles, or case collisions).
4. Runs the unchanged `capsule.validate(path, profile, facts)`, then requires
   both `payload_verified` and `device_verified`.
5. Hashes the stored files a second time and refuses if anything changed
   between the two reads.
6. Requires the stored files to be exactly the manifest's files plus
   `capsule.json`.
7. `compare_device_hashes`: exact name set; then for each file, the re-read
   bytes must equal the manifest `FileRef` (size and SHA-256), and the TV's
   hash must equal the re-read bytes. A capsule rebuilt with different bytes
   and recomputed manifest hashes passes `validate`; this step is what
   refuses it.
8. `check_baseline`: the contradiction rule below.

Refusal messages name the file, never a hash value or the raw device id.

### `GateResult`

| field | meaning |
|---|---|
| `profile_id` | the matched profile |
| `device` | match fields plus `deviceIdSha256`, as tuples; never the raw id |
| `storage_kind` | `directory` or `bundle` |
| `bundle_sha256` | SHA-256 of the bundle file, or `None` for a folder |
| `manifest_sha256` | SHA-256 of the stored `capsule.json` bytes |
| `files` | `StoredFile(name, sha256, size)` for each TV-produced file, sorted |
| `counts` | `capsule.validate` counts, as sorted tuples |
| `baseline`, `baseline_basis` | the label, and always `declared` |
| `identity_sha256` | the canonical backup identity (below) |
| `limitations` | the limits listed on this page, carried with the evidence |

`to_json()` gives a plain dict for a journal record.

The result cannot be built outside the gate: construction without the
module's private seal raises. A `dataclasses.replace` copy keeps the seal,
so the constructor also recomputes the identity and raises if any covered
field was edited. This is protection against accidents, not against
deliberate Python-level forgery.

### Canonical backup identity

`identity_sha256` is the SHA-256 of compact, key-sorted, ASCII JSON:

```json
{"baseline": "...", "baselineBasis": "declared", "capsuleSchema": "tvkit-capsule/1",
 "device": {"arch": "...", "deviceIdSha256": "...", "firmware": "...", "model": "...",
            "osName": "...", "osVersion": "..."},
 "domain": "tvkit-backup-identity/1", "files": [["payload/<id>.tar", "<sha256>", <size>], ...],
 "manifestSha256": "...", "profileId": "..."}
```

`files` is sorted by name. The storage form is left out on purpose, so a
folder and its bundle share an identity. The test suite reproduces this
digest independently.

## Baseline: what "stock" can and cannot mean

The design asks for each backup to be labelled `stock` or `current-state`.
capsule/1 has no field for it: its manifest parser accepts no unknown keys,
so adding a label to `capsule.json` would break every existing capsule. A
sidecar file would also break it, because `validate` refuses unexpected
files. So the label is a gate input that is recorded in the evidence and in
the identity digest. Recording it on disk waits for the versioned recovery
schema (D5).

- **The label is declared, not proven.** `baseline_basis` is always
  `declared`. capsule/1 cannot show packages that were removed before
  capture, so the gate cannot prove that a TV was stock.
- **Only one contradiction is checked.** If a startup or Home key's captured
  `original` value is one of the capsule's `ourPackages` (compared
  case-insensitively), our launcher was already Home when the backup was
  taken. In that case `stock` is refused and `current-state` passes.
- **Planned changes are not taken as proof of customisation.**
  `registryChanges`, `systemKeyChanges` and `ourPackages` describe changes
  that setup is about to make. `setup_plan` rehearses them expecting the
  original values. A genuine pre-setup stock capsule therefore carries all
  three.

## Limits (deliberately not in this slice)

- Scope is capsule/1 only: app directories, the listed registry fields and
  two startup/Home keys. There are no boot changes, and nothing here supports
  "Restore Samsung".
- Device hashes are taken as given. The gate does not prove how or where
  they were computed. That is the job of the `hash-on-device` capability.
- **The manifest is not device-attested.** `capsule.json` is assembled on the
  PC, so no TV hash covers it. That includes the registry before/applied
  values, the startup/Home `original` values and `ourPackages`. The identity
  binds the manifest only through `manifestSha256`, and the stock
  contradiction check reads values that came from the PC side.
- **A bundle cut inside its trailing zero padding still passes.** It has
  lost no content and keeps the same identity; only `bundle_sha256` differs
  from what `bundle()` returned. A cut anywhere else breaks a member or drops
  a file, and is refused.
- The identity covers today's `MATCH_FIELDS`. If D1 adds `tvkit-profile/2`
  match fields such as `firmwareBuild` or `region`, the domain should move
  to `tvkit-backup-identity/2` rather than silently changing digests.
- Not built yet: `plan.backup_plan` and the capture capabilities
  (`capture-package`, `xattr-dump`, `registry-read`, `system-key-read`,
  `hash-on-device`), and transport registration.
- `tvkit/vault.py` (encryption in the `age` format) is deferred. No crypto
  is invented here.
- Nothing here has been captured from a physical TV and no rehearsal has
  run. The D2 exit ("one candidate package passes the gate, zero writes
  outside scratch") needs the TV session. Rescue (D3) is not started.
- The gate writes nothing and imports nothing from `transport`, sockets or
  subprocesses. A test checks this by reading the module's syntax tree.

## Tests

`tests/test_public_toolkit_backup.py` covers:

- passing folders and bundles, with an independently reproduced identity;
- zero writes, both on a pass and on a refusal;
- missing, extra, changed, case-variant and malformed device hashes;
- a self-consistent capsule with swapped bytes;
- truncated payloads and bundles, missing or extra stored files, the wrong
  file given, and bytes changing between reads;
- wrong, missing or extra facts, and facts that match no profile;
- exact labels and the stock contradiction;
- the signature, immutability and imports.

The negative controls patch `compare_device_hashes` and `check_baseline` to
no-ops. With either disabled, the matching attack is accepted; with them
restored, it is refused again. Each guard in `backup.py` was also switched
off in a scratch copy (the `dev/guard_off_check.py` pattern) and its test
failed. Those mutations are not yet in `guard_off_check.py`.
