# Live report collection: implemented plan and remaining work

The initial design was written by Opus 4.8 against merged PR19. Implementation
and hardware checks refined it as recorded below. This page describes the final
boundary; the original draft remains in Git history.

## Objective and scope

- [x] Collect a fresh ProductInfo report and native inspection frame from one TV.
- [x] Validate them through the existing facts join and produce Helper inputs.
- [x] Reconcile temporary diagnostic artifacts without a reboot or startup change.
- [x] Publish the reviewed owner-configured `owner-sdk-root` provider for manual ADVANCED collection.
- [ ] Wire the published provider into the guided consumer/Helper flow.
- [ ] Verify independent hardware Rescue and full off-TV restoration.
- [ ] Enable consumer installation/removal only after recovery gates pass.

The public code is an owner-invoked orchestration library and CLI over an explicitly
trusted provider. Without `--provider-config` the public registry is empty and the tool
refuses; with it, only the fixed reviewed `owner-sdk-root` provider is available. It
accepts no plug-in module path, adds no Helper HTTP route and enables no action. No
maintainer credentials or default target are published: the provider needs an explicit
per-owner config (SDK and OpenSSL paths and hashes, SDK DUID pin, private IPv4
target/callback, owner-trusted profile/policy, owner-signed Facts WGT and build hashes).

## Files and trust boundaries

- `public-toolkit/tvkit/liveops.py`: finite typed provider/session/result contract,
  an empty registry, immutable upload bytes and reconciliation records.
- `public-toolkit/tvkit/livecollect.py`: input validation, one-run orchestration,
  facts joining, owner-private output files and recovery handling.
- `public-toolkit/tvkit/provider/`: the reviewed fixed `owner-sdk-root` provider,
  loaded only from an explicit owner-private config.
- `tools/tv-collect/collect.py`: explicit local CLI inputs; no arbitrary shell or
  remote-path argument. Without `--provider-config` (or a registered parent adapter)
  it refuses; with it, only `owner-sdk-root` is accepted.
- `tools/tv-collect/provider_build.py`: owner-run source build and manifest re-check
  for the five artifacts; no prebuilt binary is shipped.
- `tests/test_helper_live_collect.py` and `tests/test_public_provider_liveops.py`:
  synthetic positive/refusal tests, command-boundary checks and controls.
- `tools/tv-collect/README.md`: interface, reference-TV evidence and limitations.

The provider holds target selection, SDK/signing/root credentials and callback
authentication. It receives only computed operation inputs. The reviewed public
provider uses the owner's explicit config; it is not a default or credential-bearing
consumer installer. The guided consumer/Helper flow remains unwired.

## Enforced sequence

1. Validate plain absolute local input/output paths outside Git checkouts. Read
   bounded files and verify explicit hashes. Freeze artifact bytes before upload.
2. Validate the signed WGT structure, requested package/application IDs, local
   `index.html` entry point and reviewed probe sources. Pin DLL, runtimeconfig and
   deps files individually. No certificate creation or signing happens here.
3. Open one trusted provider session and freeze its opaque correlation and target
   binding. Validate every result immediately; recheck binding before the next
   operation. Correlation is not cryptographic attestation. A concurrently mutating
   provider is outside the contract; detected drift stops subsequent operations.
4. Assert both the package and prior result paths absent. Collisions refuse;
   there is no pre-clean. Allocate private output storage before the first write.
5. Install and launch the diagnostic widget, then read and validate its report.
6. Stage and run the fixed native inspector, require a successful exit, and pass
   the bounded frame to the existing strict decoder and facts join.
7. Write reports, join receipt and the exact Helper preflight config in owner-only
   storage. Policy/profile trust and same-TV assertions must be explicit inputs.
8. Reconcile partial operations against the original bound target. Remove only
   proven-owned packages/uploads; already-absent files are confirmed clean without
   unnecessary removal. Unknown ownership or cleanup failure requires recovery.
   A recovery failure takes priority over the original error; the private record
   contains computed namespace/hash information, never credentials or session tokens.
9. Release only provider-owned resources. Never restart a shared SDK server,
   disconnect another session or change Developer Mode's host address.

Reports may legitimately be byte-identical across fresh runs. Freshness depends
on trusted execution plus absence/install/launch ordering, not timestamps or
comparing hashes to old reports. The native frame limit is 4 MiB to match Helper.

## SDK staging correction verified on hardware

The reference Samsung SDK rejected a push to `/tmp` even though its exit code was
zero; its output contained an error. A harmless transfer/readback/removal probe
passed under `/home/owner/share/tmp/sdk_tools`. The collector now computes
`quiettv-collect-<32-hex-run-id>` directories below that root. Provider permissions
and ownership are profile-specific and must be handled only in that fresh namespace.

## Verification

- [x] A successful synthetic run writes both reports, receipt and compatible config;
  the real Helper preflight displays seven facts while all actions remain unavailable.
- [x] Identical fresh report bytes are accepted across separate runs.
- [x] Collision/stale namespace, wrong hashes, malformed WGT/entry point, incomplete
  or raw-identity report, nonzero native exit and cross-session results refuse.
- [x] Frozen-binding checks prevent a wrong absence result from reaching install.
- [x] Provider failures become finite errors without leaking provider messages.
- [x] Partial cleanup and simultaneous primary/cleanup failures require recovery.
- [x] Historical/guard-off controls fail without the relevant guards and pass restored.
- [x] The reviewed public `owner-sdk-root` provider ran end-to-end once on the reference
  TV with a per-owner private config, an official owner-signed Facts widget and a
  source-built inspector. The SDK raw-identity pin matched; temporary root was
  authenticated with uid/euid 0; root staging was cleaned; the original `/etc/passwd`
  and SDB key hashes were unchanged.
- [x] The full CLI collected a fresh ProductInfo report and native frame, joined all
  seven facts, wrote the exact Helper config, and the SDK uninstalled only its own
  diagnostic package and removed its uploads and run directory. An independent physical
  root read confirmed the package, both payload/data roots and the run directory absent;
  four protected runtime hashes and boot/control-service/dashboard PIDs were unchanged,
  and Home was requested.
- [x] Actual Helper CLI startup and authenticated HTTP loaded the exact new config and
  displayed the seven facts with sources, without identity, token or source paths. All
  change actions remained OFF; the Helper live guided flow was not wired.
- [x] The complete collection took 570.579 s (~9 min 30 s) because of 17 isolated root
  probes. This is a one-time ADVANCED diagnostic, not a cold-boot improvement or a
  consumer-install claim.
- [x] On Windows, the focused provider and collector suites ran 129 tests with
  4 POSIX-only skips; the large-frame bounds fix fails on the old code and passes on
  the new code.
- [x] Root C# DLLs reproduced byte-identically from the two checked source roots, and the
  public builder built and re-checked all five artifacts with SDK 6.0.428.
- [x] After the hardware run, small changes (1 MB boundary, standard numeric RFC 1918
  range form, synthetic example addresses, source-guard passwd colon parsing) were
  verified offline. The provider root core is the hardware-tested reference path.

The inventory snapshot is captured while the diagnostic widget is installed.
App-selection work must exclude that transient package or refresh after cleanup.
This hardware check establishes the reviewed public provider path with manual owner
configuration; it does not establish a guided consumer flow, a clean-stock installation
or real restoration. Consumer install, cleanup and restore remain unavailable until
independent hardware Rescue and full restore are verified; no stock firmware, packages,
keys or binaries are distributed. No TV reboot or startup/launcher change was made for
this feature.
