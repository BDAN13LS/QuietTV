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
2. Validates the **whole request** — package ids, install roots and count,
   registry field names, startup/Home key names, `ourPackages` — against the
   profile and capsule allowlists **before any package, registry or system-key
   read**, so an unknown field or key never reaches the backend.
3. For each declared package, **streams** the payload tar and the XattrBackup
   dump through the backend one blob at a time, staging each to disk under the
   owner-storage parent with per-chunk and aggregate size bounds; it never holds
   every payload in memory. It reads the current (`before` / `original`) values
   of the declared registry fields and startup/Home keys. The intended
   (`applied` / `expectedCurrent`) side is caller intent from the catalogue, not
   read from the device.
4. **Re-reads the device facts** and refuses if the identity or fingerprint
   changed during the capture (an adapter reconnect or device switch must not
   pass unnoticed).
5. Assembles the capsule in the staging area through the real `assemble`
   validator, gates the staged copy against the **TV-side digests**, then
   publishes it to owner storage by a **same-volume rename** that never
   overwrites a preexisting or raced destination.
6. Runs `readback_gate` once more against the TV-side digests on the **actual
   owner-storage copy** and returns its `GateResult` as the capture evidence, so
   a corruption introduced by publication cannot pass as proven.

## The injected adapter contract

`CaptureBackend` is an abstract, **read-only** reader. This toolkit ships no
implementation and registers none; `transport.TRANSPORTS` stays empty. A real
adapter is constructed and passed in by the owner's own reviewed code
(a per-firmware, read-only channel). A fake backend is a test double only — it is
never a stand-in for a real SDB or root transport.

| Method | Capability | Returns |
|---|---|---|
| `read_device_facts()` | `read-device-facts` | the `parse_device_facts` fact mapping |
| `capture_package(id, root, *, max_bytes)` | `capture-package` + `hash-on-device` | `CapturedStream` (payload tar) |
| `dump_xattrs(id, root, *, max_bytes)` | `xattr-dump` + `hash-on-device` | `CapturedStream` (XattrBackup v1 dump) |
| `read_registry_field(app, field)` | `registry-read` | the current value string |
| `read_system_key(key)` | `system-key-read` | the current value string |

There is deliberately no write, run, install or mutate method. The backend must
declare `read_only = True` and the capabilities the request needs, or the capture
refuses before any read.

### `CapturedStream` — bounded, lazy transfer

Each captured file is a `CapturedStream`: `device_sha256` (the `hash-on-device`
digest), `device_size` (the size the TV reported), and `chunks`, an iterable of
`bytes` yielded lazily. The payload and xattr reads are told the maximum number
of bytes they may transfer (`max_bytes`, the existing capsule schema maxima
`MAX_MEMBER_BYTES` / `MAX_XATTR_MANIFEST_BYTES`).

The engine stages each blob to disk **one at a time**: it bounds the declared
size against the limit before reading, then every chunk against `MAX_CHUNK_BYTES`
(one transfer unit, not a package-size cap) and the running total against both
the declared size and the limit as it writes. The iterator is **closed on every
path, including a refusal**, and nothing is held whole in memory. Real adapters
MUST enforce the transfer limit themselves; the `bytes_stream` fixture helper
materialises bytes for tests only.

### The digest that matters

`_device_digest` returns exactly the `CapturedStream.device_sha256` — the digest
the TV computed for itself, independently of the transfer — and that is what the
gate is given. **It is never a PC re-hash of the transferred bytes.** If it were,
the gate would compare the bytes against themselves and silently accept a
transfer the TV never attested. A regression guard
(`test_red_green_tv_digest_must_not_be_a_pc_rehash`) proves this both ways: with
the TV digest threaded through, a mutated transfer is refused; replace that one
line with a re-hash of the staged bytes and the mutated capture is accepted;
restore it and it refuses again.

## Refusals (all before anything persists)

Every failure raises a `CapsuleError` (this engine's `CaptureRefused`, or
`ReadbackRefused` / `CapsuleError` from the validator and gate). The capsule is
staged and gated before it is published, and only the scratch and output this
operation created are removed on a refusal, so **a refusal leaves owner storage
(and any preexisting or raced destination) untouched**.

- unsupported or incomplete identity; device/profile mismatch;
- a missing capability, or a backend that is not read-only;
- an unknown registry field name, startup/Home key name, bad install root, or a
  package count over the schema maximum — **refused before any backend read**, so
  an arbitrary field or key never reaches the device;
- a package id that is protected, invalid, or duplicated;
- a transfer chunk that is not `bytes` or exceeds the per-chunk limit;
- a blob whose size is not what the TV reported (truncated or overrun), or larger
  than the capsule limit (bounded on the TV's claim first, so an oversize file is
  never held in memory);
- bytes that do not match the TV's own digest (mutated transfer) — caught by the
  gate's byte comparison;
- the device identity or fingerprint changing between the first facts read and
  the re-read after capture;
- missing or disagreeing metadata (xattr entry set, registry value outside the
  allowlist, a no-op change);
- a payload with path traversal or a symlink escaping the package root — refused
  by the capsule validator, not re-implemented here;
- owner storage inside the repository, already existing, or without a real
  parent folder; a destination that appears before publication (never overwritten);
- corruption introduced during publication — caught by the readback of the
  **actual owner-storage copy**;
- an invalid baseline label, or a `stock` label the gate can contradict.

## Boundaries

- **Zero TV writes, zero repository writes.** The backend only reads. The engine
  stages under the owner-storage parent and publishes the proven capsule by a
  **same-volume rename** (no cross-volume copy that could corrupt silently);
  its staging folder is removed, and it removes only the output it created.
- Owner storage must be a path **outside this repository**. The capsule folder
  must not already exist, and a destination that races in before publication is
  never overwritten.
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

`tests/test_public_toolkit_capture.py`, with a fake streaming backend over the
synthetic fixtures: a passing capture into owner storage; the gate hashes proven
to be the TV digests; the facts read twice for the identity recheck; nothing
written outside owner storage; and every refusal above. Targeted behavioural
tests cover the four review defects:

- **destination-copy corruption** — a patched `_publish` flips a byte after the
  same-volume rename; the readback of the actual owner copy refuses, the corrupt
  copy is cleaned, and a sibling and a raced destination are preserved;
- **bounded lazy transfer** — tiny-chunk success, the transfer iterator closed on
  a refusal, the per-chunk cap, and an oversize declared size refused before any
  bytes are read;
- **validate before backend** — an unknown registry field, unknown system key,
  bad install root, or over-count causes **zero** package/registry/key reads;
- **identity change mid-capture** — a backend that reports a different fingerprint
  (or an invalid shape) on the post-capture re-read is refused.

Plus the red/green device-digest regression guard, and static checks that the
module imports no socket, subprocess or transport, has no TV-write surface, and
leaves `transport.TRANSPORTS` empty.
