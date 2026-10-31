# tv-collect — one live ProductInfo + native frame collection

Owner-invoked PC orchestrator for a **single** live collection over an
**injected, trusted** TV adapter. It ships one reviewed provider,
`owner-sdk-root`, which is constructed only from an explicit owner-private
configuration. With neither a provider config nor a registered parent adapter
it refuses and writes nothing. This directory itself contains no TV, signing,
root or network code; the provider drives the owner's pinned SDK and builds its
root payloads from the checked vendored source.

It produces two fresh same-session observations of one TV — a
`quiettv-device-facts/1` ProductInfo report and a `quiettv-inspection/1` native
frame — joins them through the existing `tvkit.factsjoin` library, and writes a
`quiettv-helper-preflight/1` config that the existing Helper
(`--preflight-config`) displays offline. Nothing here changes Helper HTTP
routes, enables an action, or touches Home/boot/startup.

## Reviewed public provider (`owner-sdk-root`)

`--provider-config <file> --adapter owner-sdk-root` loads the reviewed provider
in `public-toolkit/tvkit/provider/`. The config must be an explicit absolute
JSON file outside every repository, owner-only on disk, with exactly these
keys and no defaults:

```
schema                  "quiettv-live-provider/1"
target                  private-LAN IPv4 of the TV
callbackHost            private-LAN IPv4 of this PC
expectedSdkDuid         the owner's explicit SDK-DUID pin (never inferred)
sdk                     { executable: absolute sdb, sha256 }
rootPayloads            { archiveRootDll: {path, sha256}, rootAgentDll: {path, sha256} }
openssl                 { executable: absolute openssl, sha256 }
timeouts                { sdkSeconds, probeSeconds, commandSeconds,
                          nativeSeconds, reportAttempts }
```

The SDK, both payload DLLs and OpenSSL are re-hashed immediately before every
use. Any pin mismatch, missing file or malformed key refuses before device work.
An unknown `--adapter` name is refused even when a config is given, and omitting
`--provider-config` leaves the public registry empty and the tool fails closed.

The provider issues one fixed `sdb connect <target>:26101` for its own session
before the first capture, probe or write, and never calls `kill-server` or
`disconnect`. Its raw SDK guard reads `0 getduid` before every operation and
again before every write, including before the first staging capture. It must
equal `expectedSdkDuid`; a mismatch or unreadable DUID poisons the session, so
no later install, launch or cleanup can touch a changed target. Root work runs
the vendored upstream probe in a fresh child process and additionally requires
authenticated root identity (`uid/euid/gid/egid` zero), all commands exiting
zero, unchanged `/etc/passwd` and `/usr/share/sdbd/public.pem`, and a cleaned
staging namespace. The SDK is never found through `PATH`.

The SDK group used for the run directory is read by the vendored restricted-shell
`SdbClient.capture("id -g")` callback in a finite isolated worker, because the
reference SDK does not execute that Unix command through its raw shell. It is
bounded, requires the terminal `[exit:0]` marker, and returns one integer. This
is the SDK user's group, not the root probe's `gid` (which must still be zero).

It creates only
`/home/owner/share/tmp/sdk_tools/quiettv-collect-<run-id>` for this run, using a
fresh non-recursive `mkdir`, the owner UID read from the existing SDK-tools
directory and the SDK-captured group, mode `770` so the captured group keeps the
tested SDK upload path. The parent never chmods or chowns a pre-existing
directory or the global SDK-tools root. The install postcondition is the fixed
package-database `package:application` count, not an exit code alone. Cleanup
uninstalls only a package whose absence was proven before this run, and removes
only this run's exact upload paths after re-checking each SHA-256. A mismatched
replacement is left intact and the run reports `recovery-needed`.

Report and native reads refuse symlinks and non-regular files. Report reads use
a true `head -c` bound before base64 encoding, so a growing or oversized file
cannot outrun the read, and the decoded length is checked again. The target must
offer runtime `6.0`; a non-6 assessment is rejected by the worker before the
upstream payload is staged or executed. Only the two root DLLs are pinned as
external root payload files; the upstream launcher generates its runtimeconfig
files internally, so they are not four separately pinned inputs.

The isolated worker sets the measured restricted-shell timing locally before
any SDK capture or root preflight: injection delay `6.0` and minimum delta
`4.0` seconds (upstream `2.0`/`1.5` is flaky on the reference TV). The delta
check remains in force; no host environment or module global is changed.

Native uploads require this session's own verified fresh run directory, and a
partial push leaves no trusted bundle. Immediately before dotnet, one fixed
compound shell guard re-hashes the three remote bundle files against the stored
pushed pins, so a replaced DLL/runtimeconfig/deps refuses execution. Cleanup
uses the session's own absence result, confirmed install postcondition and
actual uploaded paths. A failed or partial install that leaves a package
present is never uninstalled and its data is left intact with recovery needed.

On Windows the provider config ACL is checked by SID, not localised names: the
owner, LocalSystem (`S-1-5-18`) and Administrators (`S-1-5-32-544`) may be
allowed; other allow principals, unparseable ACLs and missing current-user
access refuse. Child processes use hidden, grouped Windows creation flags and
the absolute trusted `taskkill.exe`.

## Adapter contract (`tvkit/liveops.py`)

The public registry `LIVEOPS` is empty. A parent-owned reviewed adapter:

- binds **one** target per `ops.session(run_id)` and closes only its own
  resources on exit — never a shared SDK client, server process or host setting;
- exposes an opaque `session_token` and an opaque in-memory `binding`; every
  result echoes both plus the run id, and the collector refuses mixed values;
- implements only fixed operations: `assert_absent`, `install_widget`,
  `launch_facts`, `read_report`, `push_native`, `run_native`,
  `close_owned_run(id)` and `close()`;
- receives only tool-computed values (package id, fixed data paths, a random
  run staging directory, exact upload paths and SHA-256 pins) and the
  **verified bytes** of each artifact — it never re-reads a mutable local path;
- pushes and runs the fixed `dotnet <run dir>/QuietTVInspect.dll
  --inventory-framed`, strips **exactly one** transport `[exit:N]` trailer, and
  returns the bounded stdout bytes with the process exit code;
- reconciles cleanup: uninstall only when the exact package was proven absent
  before this run, and remove only the exact upload paths listed in the
  `OwnedRun`, re-checked by hash.

`session_token` is correlation, not cryptographic proof. The adapter is
explicitly trusted code and guarantees the target binding and fresh operation
execution; no attestation is claimed.

**Binding is frozen at open.** The collector reads the opaque token/binding once
immediately after the session opens, checks every typed result against that pair
right after each operation, and re-reads the live pair before every subsequent
operation (starting before the install that follows `assert_absent`). A changed
pair is refused before the next write. There is no async-provider race
guarantee: an adapter that mutates its target mid-session is out of contract.

## Inputs (all required, all validated before any TV operation)

| input | rule |
|---|---|
| `--adapter` | name in the **empty** reviewed registry, or exactly `owner-sdk-root` with `--provider-config`; no module path, no plugin import |
| `--provider-config` | optional explicit owner-private provider JSON; enables only the fixed `owner-sdk-root` name |
| `--out-dir` | absolute plain local directory, outside every git checkout, must not exist |
| `--signed-wgt` | owner-signed `.wgt`; its `config.xml` must declare `package == --package-id` and `application == <package-id>.Facts` |
| `--wgt-sha256` | trusted pin for the exact signed bytes |
| `--inspector-dll` | file named exactly `QuietTVInspect.dll` |
| `--dll-sha256` | trusted pin |
| `--inspector-runtimeconfig` | file named exactly `QuietTVInspect.runtimeconfig.json`, strict JSON, `net6.0` / `Microsoft.NETCore.App` |
| `--runtimeconfig-sha256` | trusted pin |
| `--inspector-deps` | file named exactly `QuietTVInspect.deps.json`, strict JSON deps document |
| `--deps-sha256` | trusted pin |
| `--package-id` | 10 alphanumeric Tizen package id |
| `--policy` | owner-trusted `tvkit-facts-join-policy/1` file |
| `--profile` | owner-trusted `tvkit-profile/2` file |
| `--operator-trust-policy-and-profile` | explicit assertion; not inferred |
| `--operator-paired-from-same-tv` | explicit assertion; not inferred |

**Artifact bundle.** A framework-dependent .NET 6 build needs the DLL **and** its
matching `QuietTVInspect.runtimeconfig.json`; the pinned build also emits
`QuietTVInspect.deps.json`. All three must come from the same build and all
three hashes are mandatory. The DLL alone is not enough.

**Signed widget.** The archive may contain only `config.xml`, `index.html`,
`collector.js`, `author-signature.xml`, `signature1.xml` (and optional
`signature2.xml`). `index.html`/`collector.js` must byte-equal the reviewed
sources in `tools/tv-device-facts/` (LF-normalised). ZIP entry count, declared
sizes, compression method/ratio, encryption, duplicate names, traversal,
absolute names and symlinks are all bounded and refused. This tool does not
sign, read a `.p12`, or handle a password.

## Freshness and ordering

Two runs of the same TV may legitimately return **identical** report and frame
bytes. Freshness is never proven by hashes differing from older runs or by
timestamps. Instead:

1. `assert_absent` must prove the exact package is not installed and neither
   fixed data path holds bytes, **immediately before** this run's install;
2. this run then installs and launches the widget, and reads the report in the
   same session;
3. the native inspector is pushed and run in the same session;
4. every result must echo the same run id, session token and binding.

A collision (leftover package or report) is refused **without touching it**.
There is no automatic pre-clean and no nonce.

## Outputs (owner-private, create-new, no overwrite)

- `device-facts.json` — the exact fresh report bytes
- `inventory.frame` — the exact fresh framed native bytes
- `join-receipt.json` — `factsjoin.JoinReceipt.body()`
- `helper-preflight-config.json` — `quiettv-helper-preflight/1` naming all four
  saved sources and **both** explicit operator assertions
- `collection.json` — `quiettv-collection/1`: collection id, artifact hashes,
  receipt/policy digests, cleanup status

All files are created with create-new semantics and owner-only permissions
through `helper/owner_init.py`. No raw DUID, target address, session token or
credential is written or printed. Stdout is a finite sanitized summary only.

The output directory is allocated after all local validation and **before the
first TV write**, so a failed mkdir/ACL cannot leave TV changes without a
recovery record.

## Cleanup and failure

Cleanup always runs after a session opens, including partial failures, but only
against the session that still holds the token/binding frozen at open. A
changed target is never "fixed": its cleanup is skipped and recovery is
recorded instead. With empty ownership (no install attempt, no uploads) no
package is ever uninstalled.

Ownership is tracked as intents (absence confirmed, install attempted, exact
uploads). The adapter reconciles before removal; an install that failed does
not mean no writes occurred, and a package is never uninstalled unless absence
was proven first. A partial/failed install may legitimately leave nothing:
`package_removed=false, package_absent_after=true` is a confirmed clean state,
not a failure.

If ownership or cleanup cannot be confirmed, the run writes `collection.json`
with `"cleanup": {"status": "recovery-needed"}` and a private `namespace` block
(package id, tool-computed remote run dir, and every expected upload path +
SHA-256) so the owner can find this run's own partial uploads. It keeps the
artifacts and refuses — it never reports success after a failed cleanup. When a
primary failure and a cleanup failure coexist, the recovery-required error is
raised first; the primary failure is recorded only as a finite category.

Every unexpected provider exception — at session open, each operation, close,
cleanup or cleanup-result validation, including a `RuntimeError` or a
provider-raised `LiveCollectionError` with an unsafe message — is converted to
a fixed, non-secret `LiveCollectionError` without echoing or chaining provider
text. The CLI never prints a traceback for those.

## How to build and run

No prebuilt binaries are distributed. Build the inspector and both root payload
projects from the checked sources into a **new** owner-private directory outside
every repository:

```
python tools/tv-collect/provider_build.py \
  --dotnet <absolute dotnet 6.0 executable> \
  --out-dir <new private directory>
python tools/tv-collect/provider_build.py --check <out-dir>/provider-artifacts.json
```

The helper writes `quiettv-provider-artifacts/1` with the absolute path and
SHA-256 of each of the five artifacts. The CLI is exact: `--dotnet <absolute
dotnet> --out-dir <new private directory>` builds; `--dry-run` prints the plan
and writes nothing; `--check <absolute manifest> [--provider-config <absolute
config>]` re-hashes the build. `--check` cannot be combined with the build flags,
and `--provider-config` is accepted only with `--check`. With a config, `--check`
also confirms that it pins those exact root payloads. Point the provider config's
`rootPayloads` at `SamsungTvArchiveRoot.dll` and `SamsungTvRootAgent.dll` from
that build, and pass its `QuietTVInspect*.dll/runtimeconfig.json/deps.json` as
the three inspector inputs. Then:

```
python tools/tv-device-facts/pack.py --app-id <package>.Facts --package <package> --out <new>.wgt
# sign the .wgt with the owner's reviewed tooling (not in this repository)
python tools/tv-collect/collect.py --adapter owner-sdk-root \
  --provider-config <owner-private provider.json> ...
```

A parent may instead register its own reviewed `LiveOps` in `liveops.LIVEOPS`
(or pass an explicit instance to `collect_reports(..., ops=...)`).

## Limitations

- The provider targets one owner-configured TV at a time; there is no default
  target, discovery, scan or maintainer credential.
- The payloads target the common .NET 6 runtime only. No claim is made for
  common31, QN90 or any other model/firmware.
- The provider does not attest the full model or firmware. The diagnostic widget
  collects the exact ProductInfo firmware/model in the same run; the trusted
  facts-join policy and profile decide compatibility afterwards. The native
  inspector intentionally does not infer them, and the SDK DUID is never treated
  as the ProductInfo identity.
- It makes no claim that boot, root or the launcher are unchanged. The root
  probe reports protected-file hashes and staging cleanup, and cleanup removes
  only this run's namespace, but this is not a boot-integrity attestation.
- The public provider was run end-to-end once on the reference TV and is covered
  by offline fake-SDK/fake-root tests. This is a one-time ADVANCED diagnostic with
  manual per-owner setup, not a consumer installation. Guided consumer integration,
  fresh-stock end-to-end verification and restore remain pending.

On the reference TV the Samsung SDK rejects pushes to `/tmp`. Run directories therefore
use `/home/owner/share/tmp/sdk_tools/quiettv-collect-<run-id>`. The trusted adapter must
create an accessible directory only inside that fresh namespace, verify uploaded hashes,
and remove the directory after cleanup. A transfer/readback/removal probe passed there;
this does not establish compatibility on another model or firmware.

## Reference-TV verification

The reviewed public `owner-sdk-root` provider was exercised once end-to-end on the
reference Samsung UN43U8000FBXZA / firmware T-X22UDFAKUC-0090-1300.8 / Tizen 9 / ARMv7
/ .NET 6.0.9 with an owner-private config, an official owner-signed Facts widget and an
inspector built from the checked sources. The SDK raw-identity pin matched; temporary root
was authenticated with uid/euid 0; root staging was cleaned; the original `/etc/passwd`
and SDB key hashes were unchanged.

The full CLI installed and launched its diagnostic widget, captured its fresh ProductInfo
report, staged and ran the pinned native inspector, joined all seven facts and wrote the
exact Helper config. The SDK uninstalled only its own diagnostic package and removed its
uploads and run directory. An independent physical root read confirmed the package, both
payload/data roots and the run directory absent, four protected runtime hashes unchanged,
and boot/control-service/dashboard PIDs unchanged; Home was requested. No reboot was
required. The complete collection took 570.579 s (~9 min 30 s) because of 17 isolated
root probes.

The running Helper then loaded the exact generated `helper-preflight-config.json` through
its normal CLI and authenticated HTTP interface. It displayed the seven matching facts with
sources, kept every change action unavailable and exposed no identity, token or source path
in HTTP. These are fresh observations supplied as saved files to the Helper; the Helper has
no new live-collection HTTP route and its live guided flow is not wired.

This is a one-time ADVANCED diagnostic with manual per-owner setup; it makes no cold-boot
improvement or consumer-install claim. Fresh-stock end-to-end verification, consumer
install/cleanup and hardware restore remain unavailable until independent hardware Rescue
and full restore are verified. No stock firmware, packages, keys or binaries are
distributed, and no TV reboot or startup/launcher change was made for this feature. After
the hardware run, small changes (1 MiB boundary, standard numeric RFC 1918 range form,
synthetic example addresses, source-guard passwd colon parsing) were verified offline; the
provider root core is the hardware-tested reference path.

The native inventory is a snapshot taken while the diagnostic widget is installed. Future
app-selection consumers must exclude that diagnostic package or obtain a post-cleanup
inventory; this slice uses the snapshot for runtime/profile facts only.
