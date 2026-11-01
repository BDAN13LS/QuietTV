# QuietTV Rescue (restore-only, offline) — engineering slice D3

**This build cannot recover a real TV by itself.** No transport adapter ships
(`tvkit.transport.TRANSPORTS` is empty) and no cached bootstrap/root method is
bundled; that is deferred to D5b publishing. **There is no hard-brick
guarantee.** If the TV will not start, use Samsung's own recovery (Settings >
Support > Self Diagnosis / Reset, or Samsung support).

Nothing here has run against hardware. Every restore test uses one synthetic
package and a fake in-memory adapter.

> **Release note:** the recovery/2 restore plan, the signed run journal
> (`tvkit-rescue-journal/1`) and the `RecoveryTransport` contract described in
> this guide are implemented and offline-tested in the private SamsungTV
> repository. They are not part of this public release and are not
> hardware-verified; no adapter ships.

## Commands

```
python rescue/main.py validate --capsule PATH --profile ID [--device-facts FACTS.json]
python rescue/main.py plan     --capsule PATH --profile ID
python rescue/main.py restore  --capsule PATH --profile ID [--apply --transport NAME]
python rescue/main.py rehearse --capsule PATH --profile ID [--apply --transport NAME]
```

`restore` and `rehearse` are dry runs without `--apply`. With `--apply` the
adapter name is resolved only from the empty registry, so they refuse (exit 3).
Rescue never loads adapter code from a path or a config file. The library
functions `restore()` and `rehearse()` take an injected, implemented
`RescueTransport` (tests now, the owner's own code later).

Exit codes: 0 ok (validate, dry run, rehearsal), 1 refused (nothing changed),
2 usage, 3 no implemented adapter (nothing changed), 4 adapter-verified, or
already matching, and the owner's own checks are still pending (Rescue keeps
no record that they were ever done), 5 stopped partway or failed read-back
(the TV may be partly changed).

## What the gate does before any write

1. Re-reads the capsule with `capsule.validate` (a partial backup stops here;
   so does any `tvkit-profile/2` profile, before the TV is asked anything).
2. Reads fresh device facts from the adapter and matches the profile exactly.
3. Refuses `tvkit-profile/2` again (defence in depth): capsule/1 cannot bind
   firmware build or region.
4. Re-validates the capsule against those facts (bytes and device identity).
5. Refuses capsules with `ourPackages` (launcher/root-helper removal needs an
   independently accepted final control check) or startup/Home key changes.
6. Checks every capability the run needs.
7. Inspects every target with typed evidence. Rescue decides "identical"
   itself: absent → restore, identical → skip, anything else (for example a
   newer version) → refuse the whole run. Registry fields: three-way
   (before → skip, applied → restore, else refuse).
8. Re-reads device facts, requires them unchanged, then runs the plan through
   `tvkit.transport.run_plan`, stopping at the first unverified step.

After the run every restored target is read back and compared again, so an
adapter that reports success without changing anything is caught (exit 5).
Identity is still evidence the adapter supplies: Rescue does the comparison,
but it cannot detect an adapter that lies.

The plan is cut before its first manual-acceptance step. Rescue never marks
the owner's checks (Home, remote, Settings, sources, Store) as passed.

## Development status (private repository, not in this public release)

The sections below describe development in the private SamsungTV repository.
The recovery/2 restore plan, the signed run journal, the owner-acceptance
(`accept`/`--resume`) flow and the `RecoveryTransport` contract are implemented
and offline-tested there, but are not in this public release, are not
hardware-verified, and no adapter ships.

### Not done yet

- **Rescue validates `tvkit-recovery/2` offline, but does not restore it.**
  The `validate` command dispatches through `recovery2.validate_any` and
  accepts a recovery/2 capsule with a profile/2 plus both trusted policies;
  `recovery2.restore_plan` now orders the full one-run restore, but Rescue's
  `plan`/`restore`/`rehearse` still refuse recovery/2, and capsule/1 with a
  profile/2 is still refused outright.
- A real adapter and the cached bootstrap/root method (D5b). The
  `RecoveryTransport` contract is built and contract-tested (offline, synthetic
  adapters only); no real adapter ships and `TRANSPORTS` stays empty.
- Removing our launcher or root helper (`ourPackages`) is now reachable: the
  remove phase is gated behind the signed journal's owner acceptance
  (verify-control before verify-control-final) and is tested with a synthetic
  transport. Boot/startup keys (`system_key_changes`) remain refused. A real
  device resume still needs the RecoveryTransport adapter (D5b) and the
  bundle-pinned public key (D6).
- Registry and startup-key rehearsal (the setup plan's rehearsal assumes the
  pre-setup state).
- A recovery/2 rehearsal path: `rehearsal_plan` goes through `setup_plan`,
  which reads `payload_verified` and deliberately raises for recovery/2, so
  H1 needs a new rehearsal route for the profile/2 hardware path.
- The first real `tvkit-profile/2` (read from the TV in a D5 session, not
  typed by hand).
- Bundling Rescue with the Helper and copying it to owner storage (D6).

### Design (adapter contract D5b is built; the rest is planned, nothing proven on hardware)

**Adapter (D5b) — built.** The contract `RecoveryTransport` now lives in
`tvkit/transport.py` (replacing `RescueTransport`), along with the frozen
`TargetEvidence`/`RegistryEvidence` types and the capability-name constants;
`rescue/main.py` enforces the exact-profile gate. It keeps today's methods
(`read_device_facts`, `run`, `inspect_target`, `read_registry`) and adds:

- `profile_digests`: the profiles (`recovery2.profile_digest`) it was proven
  on; Rescue refuses it for any other. The digest binds schema, profileId,
  match, install roots, registry and system keys, but not
  `manualPrerequisites`, so "exact profile" means those bound fields.
- `read_recovery_evidence(record) -> bytes`: raw `tvkit-recovery-evidence/1`
  JSON, so the strict parser keeps its duplicate-key and digest checks. It
  returns both `bootRecords` and `externalTargets`; callers must hold
  `boot-field-read` and `external-target-read` before asking.
- `close()`: ends the session, removes its own staging and reads that back.
  It does not undo the bootstrap's writes; those come from the bootstrap's
  `sideEffects[]`/`cleanup[]` in the run itself.
- Capabilities `boot-field-read`, `boot-field-cas` (the plan op
  `compare-and-set-field`), `external-target-read` (the plan op
  `verify-external-target-read-only`; read-only) and `remove-root-helper`
  (only after the final owner check).

The adapter supplies evidence; Rescue still decides identity, the three-way
outcome and the read-back. `TRANSPORTS` stays empty in this repository: only
a signed Rescue bundle registers an adapter, from a module named in its
manifest whose hash Rescue checked first. Never from a path, an environment
variable or a config file.

**Cached bootstrap method (D5b).** Per profile,
`bootstrap/<profileId>/bootstrap.json`, schema `tvkit-bootstrap/1`, exact keys:
`schema`, `profileId`, `profileDigest`; `match` (the profile/2 match block,
including `firmwareBuild` and `region`); `routeId` (the reviewed per-firmware
route, named only: no exploit steps in docs); `foothold` (what the owner
provides first: Developer Mode with this PC's address, then a restart);
`artifacts[]` (`file`, `sha256`, `size`, `licence`, `upstream`);
`capabilitiesGranted[]`; `stockCapsule` (the "stock" baseline, by
`manifestSha256`) and `rootHelperPackageId` (which of `ourPackages` is the
root helper, removed last); `externalRefPolicy` and `bootFieldPolicy` (the two
trusted policies recovery/2 requires, keyed to this profile and pinned by
digest in the signed `release/manifest.json`); `sideEffects[]` (every write
the bootstrap makes on the TV) with `cleanup[]`; and `testedOn[]` (the
hardware sessions that proved it). Everything is on owner storage before a
rescue; Rescue downloads nothing. Before the bootstrap opens a session, Rescue
re-reads fresh facts from the adapter (never from the capsule) and requires
`bootstrap.match == profile.match`, `bootstrap.profileDigest ==
recovery2.profile_digest(profile)` and fresh facts equal to `match` exactly.
If any field differs, even only `firmwareBuild`, the method is not tried:
Rescue refuses before any write with exit 1 (EXIT_REFUSED); the specific
"firmware changed, cached method unusable" message is planned, not yet built.
No hard-brick guarantee. It opens its own session from the PC and never uses
the launcher, its host or the TV-side root helper as its channel.

**Recovery v2 (D5).** Rescue dispatches through `recovery2.validate_any`;
capsule/1 with profile/1 keeps today's path. The recovery/2 manifest schema is
frozen (exact keys), so the "stock" baseline and the root-helper identity are
NOT new manifest fields: both live in the per-profile `bootstrap/1` record as
`stockCapsule` and `rootHelperPackageId`. recovery/2 already binds
`firmwareBuild`, `region` and `deviceIdSha256`, and the payload and entry
hashes are stricter than a package version string. "Restore Samsung" also
needs the capsule's D2 readback receipt labelled `stock`, matched by
`manifestSha256` (today `readback_gate` only supports capsule/1 and
`check_baseline` ignores `bootChanges`; extending it to recovery/2 is still to
build). `recovery2.restore_plan` now orders one run: preflight and read-only
external-target checks, packages, registry fields, boot fields
(compare-and-set), startup keys, owner check, removal of our packages, final
owner check, root helper last (using `rootHelperPackageId`, supplied as an
argument for now), then the bootstrap's own `sideEffects[]` undone via its
`cleanup[]` and read back (still to build: the bootstrap/1 source of
`rootHelperPackageId` and the cleanup steps).

The run stops at the first owner check by design (`_cut_before_manual`), so a
complete restore spans more than one session. Each owner-acceptance milestone
is recorded in the signed run journal (keyed to the capsule/profile/evidence
digests), and the next invocation resumes from the last recorded milestone;
Rescue never synthesizes an owner check. The signed `tvkit-rescue-journal/1`
format, its Ed25519 sign/verify, and the `resume()`/`accept()` wiring (with the
`--resume`/`accept` CLI) are implemented in the private repository.

**Bundle (D6).** `QuietTV-Rescue/`: the embeddable Python runtime, `rescue/`,
`tvkit/`, `bootstrap/`, the adapter, the per-profile `externalRefPolicy` and
`bootFieldPolicy`, and a minisign-signed `release/manifest.json` that pins
every file and both policies by digest. No capsule inside it. The Helper
copies it to owner storage and re-reads every file. Install stays refused
until a verified copy with a bootstrap for this TV's exact profile exists.
Rescue checks its own manifest offline at start.

**Hardware proof (pre-registered).**

| # | Proof | Passes only if |
|---|---|---|
| H1 | Rehearse from the copied bundle, WAN blocked (capsule/1 today; recovery/2 rehearsal still to build) | scratch tree equals the capsule; scratch removed and read back; no write outside scratch |
| H2 | One optional package removed, restored by Rescue | exact bytes, UID/GID, modes, xattrs, links, fields; a second run makes zero writes |
| H3 | Our boot changes undone | every field back to `before`; an unrelated field untouched; a decoy third value refuses the whole run with zero writes |
| H4 | Complete stock restore from a `stock` capsule after full install and cleanup, launcher host stopped and TV-side helper disabled first | every captured hash back; our packages and root helper gone; the bootstrap's `sideEffects[]` undone and `cleanup[]` read back; a second run makes zero writes; owner checks pass |
| N1 | A changed `firmwareBuild` or `region` | refused before any write (exit 1, EXIT_REFUSED) because fresh facts no longer equal `bootstrap.match`; the method is never tried |

"Restore Samsung" is claimed only after H4 passes.
