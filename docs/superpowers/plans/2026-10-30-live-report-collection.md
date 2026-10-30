# Live report collection: implemented plan and remaining work

The initial design was written by Opus 4.8 against merged PR19. Implementation
and hardware checks refined it as recorded below. This page describes the final
boundary; the original draft remains in Git history.

## Objective and scope

- [x] Collect a fresh ProductInfo report and native inspection frame from one TV.
- [x] Validate them through the existing facts join and produce Helper inputs.
- [x] Reconcile temporary diagnostic artifacts without a reboot or startup change.
- [ ] Publish a reviewed privileged provider for stock-TV setup.
- [ ] Verify independent hardware Rescue and full off-TV restoration.
- [ ] Enable consumer installation/removal only after recovery gates pass.

The public code is an owner-invoked orchestration library and CLI over an explicitly
trusted provider. Its provider registry is empty. It does not ship a privileged
connection, accept a plug-in module path, add Helper HTTP routes, or enable actions.
No maintainer credentials are published; a future provider needs per-owner setup.

## Files and trust boundaries

- `public-toolkit/tvkit/liveops.py`: finite typed provider/session/result contract,
  an empty registry, immutable upload bytes and reconciliation records.
- `public-toolkit/tvkit/livecollect.py`: input validation, one-run orchestration,
  facts joining, owner-private output files and recovery handling.
- `tools/tv-collect/collect.py`: explicit local CLI inputs; no arbitrary shell or
  remote-path argument. Without a registered provider it refuses.
- `tests/test_helper_live_collect.py`: synthetic positive/refusal tests and controls.
- `tools/tv-collect/README.md`: interface, reference-TV evidence and limitations.

The provider holds target selection, SDK/signing/root credentials and callback
authentication. It receives only computed operation inputs. A maintainer-owned
provider was used for live validation; it is not a consumer prerequisite advertised
as complete. Publishing its reviewed replacement remains separate work.

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
- [x] A warm reference-TV run executed the new collector, captured both reports,
  joined all seven facts and removed its temporary package/files. Boot/root/launcher
  processes and four protected runtime/recovery hashes stayed unchanged.
- [x] Actual Helper CLI startup and authenticated HTTP loaded the exact generated
  config and displayed the seven facts without enabling any change capability.

The inventory snapshot is captured while the diagnostic widget is installed.
App-selection work must exclude that transient package or refresh after cleanup.
This hardware check establishes the maintainer's tested provider path; it does
not establish a public provider, a clean-stock installation or real restoration.
