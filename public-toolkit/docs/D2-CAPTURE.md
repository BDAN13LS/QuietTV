# D2 typed read-only capture engine

`tvkit/capture.py`. Plan phase D2 of
`docs/superpowers/plans/2026-10-27-consumer-install.md`, offline capture slice.

This is the seam between a device-reading adapter and the capsule the rest of the
engine already understands. It adds **no new schema**: it drives the existing
`assemble.assemble` (so the output is the unchanged `tvkit-capsule/1`) and the
existing `backup.readback_gate` (so the proof is the unchanged `GateResult`). It
is not a device driver, a transport, an installer or a cleanup. It reads, builds
an owner-storage capsule, and proves it.

## What it does

`capture_backup(backend, request, out_dir, *, baseline="current-state")`:

1. Reads device facts through the injected backend and binds them to **exactly
   one** built-in profile and the hashed owner-TV fingerprint
   (`deviceIdSha256`). An unsupported or incomplete identity refuses here.
2. For each declared package, captures the payload tar and the XattrBackup dump
   through the backend; reads the current (`before` / `original`) values of the
   declared registry fields and startup/Home keys. The intended
   (`applied` / `expectedCurrent`) side is caller intent from the catalogue, not
   read from the device.
3. Assembles the capsule in a temporary folder through the real `assemble`
   validator, then runs `readback_gate` against the **TV-side digests**.
4. Only a capsule that passes the gate is moved into owner storage. The
   `GateResult` is returned as the capture evidence.

## The injected adapter contract

`CaptureBackend` is an abstract, **read-only** reader. This toolkit ships no
implementation and registers none; `transport.TRANSPORTS` stays empty. A real
adapter is constructed and passed in by the owner's own reviewed code
(a per-firmware, read-only channel). A fake backend is a test double only — it is
never a stand-in for a real SDB or root transport.

| Method | Capability | Returns |
|---|---|---|
| `read_device_facts()` | `read-device-facts` | the `parse_device_facts` fact mapping |
| `capture_package(id, root)` | `capture-package` + `hash-on-device` | `CapturedFile` (payload tar) |
| `dump_xattrs(id, root)` | `xattr-dump` + `hash-on-device` | `CapturedFile` (XattrBackup v1 dump) |
| `read_registry_field(app, field)` | `registry-read` | the current value string |
| `read_system_key(key)` | `system-key-read` | the current value string |

There is deliberately no write, run, install or mutate method. The backend must
declare `read_only = True` and the capabilities the request needs, or the capture
refuses before any read.

### `CapturedFile` and the digest that matters

Each captured file carries `device_sha256` — the digest the TV computed for
itself (`hash-on-device`), independently of the transfer — and `device_size`.
`_device_digest` returns exactly that TV digest, and that is what the gate is
given. **It is never a PC re-hash of the transferred bytes.** If it were, the gate
would compare the bytes against themselves and silently accept a transfer the TV
never attested. A regression guard (`test_red_green_tv_digest_must_not_be_a_pc_rehash`)
proves this both ways: with the TV digest threaded through, a mutated transfer is
refused; replace that one line with `sha256(data)` and the mutated capture is
accepted; restore it and it refuses again.

## Refusals (all before anything persists)

Every failure raises a `CapsuleError` (this engine's `CaptureRefused`, or
`ReadbackRefused` / `CapsuleError` from the validator and gate). Because the
capsule is built and gated in a temporary folder and only moved into owner
storage once proven, **a refusal leaves owner storage untouched**.

- unsupported or incomplete identity; device/profile mismatch;
- a missing capability, or a backend that is not read-only;
- a package id that is protected, invalid, duplicated, or whose install root is
  not an allowlisted root for that id;
- a blob whose size is not what the TV reported, or larger than the capsule
  limit (bounded on the TV's claim first, so an oversize file is never held in
  memory);
- bytes that do not match the TV's own digest (mutated or truncated transfer) —
  caught by the gate's byte comparison;
- missing or disagreeing metadata (xattr entry set, registry value outside the
  allowlist, a no-op change);
- a payload with path traversal or a symlink escaping the package root — refused
  by the capsule validator, not re-implemented here;
- owner storage inside the repository, already existing, or without a real
  parent folder;
- an invalid baseline label, or a `stock` label the gate can contradict.

## Boundaries

- **Zero TV writes, zero repository writes.** The backend only reads. The engine
  writes the proven capsule to owner storage and nothing else; its working files
  live in a temporary directory that is removed.
- Owner storage must be a path **outside this repository**. The capsule folder
  must not already exist.
- The baseline is **declared**, not device-proven (same limit as the readback
  gate): capsule/1 cannot show packages removed before capture, so `stock` is a
  claim the gate can only contradict, never confirm.
- The registry and startup/Home values in `capsule.json` are assembled on the PC
  and are **not device-attested**; only the payload and xattr files carry a TV
  digest.

## Remaining live gates (not in this slice)

- No real read-only adapter exists yet. `CaptureBackend` is the contract a D5b
  per-firmware, read-only channel must implement; until one is constructed and
  reviewed, nothing is registered and the engine cannot reach a TV.
- Nothing here has captured from a physical TV. The D2 exit ("one candidate
  package passes the gate, zero writes outside scratch") still needs the TV
  session with a real backend.
- No encryption (`tvkit/vault.py`, the `age` format) and no bundling of the
  owner capsule are done here.
- No CLI or UI surface is wired. A UI may later report readiness and gate status,
  but must not enable cleanup, install or restore from this engine.

## Tests

`tests/test_public_toolkit_capture.py`, with a fake in-memory backend over the
synthetic fixtures: a passing capture into owner storage; the gate hashes proven
to be the TV digests; nothing written outside owner storage; every refusal above;
the red/green device-digest regression guard; and static checks that the module
imports no socket, subprocess or transport, has no TV-write surface, and leaves
`transport.TRANSPORTS` empty.
