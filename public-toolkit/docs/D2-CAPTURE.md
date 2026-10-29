# D2 typed read-only capture engine

The read-only producer in `tools/tv-capture` captures both package tar archives
and version 1 full-xattr manifests. The typed `decode_package_stream` and
`decode_xattr_stream` functions in `tvkit/package_stream.py` verify and spool
those distinct record kinds outside Git on owner storage. See the reader README
for protocol, limits and required caller transport controls. This is not a
complete backend: observed firmware facts and a reviewed privileged channel
remain missing. It registers no transport and enables no installer or cleanup.

On the supported TV, a live read-only test captured an 81,920-byte archive and a
3,971-byte metadata manifest: all 14 entries, file hashes and link targets
matched, and 36 security attributes were observed. Both received files matched
their TV-computed digests. Temporary SDK staging was removed, and the existing
boot, launcher and root process stayed unchanged. No restoration was performed;
this evidence proves capture of that package, not a complete recovery system.
External loader links still need explicitly reviewed restore policy; do not
weaken capsule validation to admit them.

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

`CaptureBackend` is an abstract, **read-only** reader. This toolkit ships no complete
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
- owner storage inside this repository or any other git checkout/worktree,
  already existing, or without a real parent folder; a destination that appears
  before publication (never overwritten, empty or populated);
- corruption introduced during publication — caught by the readback of the
  **actual owner-storage copy**; on that refusal a directory that replaced the
  output is preserved, not deleted;
- an invalid baseline label, or a `stock` label the gate can contradict.

## Boundaries

- **Zero TV writes, zero repository writes.** The backend only reads. The engine
  stages under the owner-storage parent and publishes the proven capsule into an
  **exclusively reserved** output: `os.mkdir` is an atomic create-or-fail on both
  POSIX and Windows, so a destination that races in before publication — empty or
  populated — is refused, never overwritten. (Plain `os.rename` is *not* a
  no-overwrite operation: on POSIX it silently replaces an empty target directory,
  so it is not relied on for this.) Each member then moves into the reserved
  directory by a same-volume rename. The staging folder is removed; the engine
  removes only the output it created, and only while that is still the exact
  directory it made — a replacement with a different inode, or a symlink, is left
  untouched.
- Owner storage must be a path **outside this repository and outside any other git
  checkout or worktree** (the proven `owner_init.check_outside_repos` policy:
  resolved-path containment plus an ancestor `.git` directory *or* worktree file
  marker), checked before any backend read, so a capture in a worktree can never
  land in the parent repo and risk the owner's stock data being committed. The
  capsule folder must not already exist.
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
  rename; the readback of the actual owner copy refuses, the corrupt copy is
  cleaned, and a sibling is preserved. A raced populated destination and a raced
  **empty** destination are both preserved (the reservation refuses them), and a
  directory that replaces the output before the final readback is preserved on the
  refusal (cleanup proves inode identity first). Owner storage inside a synthetic
  git repo and inside a worktree `.git` file marker are both refused before any
  read;
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
