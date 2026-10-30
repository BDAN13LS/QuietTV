# tv-collect — one live ProductInfo + native frame collection

Owner-invoked PC orchestrator for a **single** live collection over an
**injected, trusted** TV adapter. This directory ships no adapter and no TV,
signing, root or network code. With no registered adapter it refuses and writes
nothing.

It produces two fresh same-session observations of one TV — a
`quiettv-device-facts/1` ProductInfo report and a `quiettv-inspection/1` native
frame — joins them through the existing `tvkit.factsjoin` library, and writes a
`quiettv-helper-preflight/1` config that the existing Helper
(`--preflight-config`) displays offline. Nothing here changes Helper HTTP
routes, enables an action, or touches Home/boot/startup.

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
| `--adapter` | name in the **empty** reviewed registry; no module path, no plugin import |
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

## How to build and run (parent-owned)

```
dotnet build tools/tv-inspect/QuietTVInspect.csproj -c Release   # pinned SDK 6.0.428
python tools/tv-device-facts/pack.py --app-id <package>.Facts --package <package> --out <new>.wgt
# sign the .wgt with the owner's reviewed tooling (not in this repository)
python tools/tv-collect/collect.py --adapter <parent adapter name> ...
```

The parent registers its reviewed `LiveOps` in `liveops.LIVEOPS` (or passes an
explicit instance to `tvkit.livecollect.collect_reports(..., ops=...)`).

## What cannot ship yet

- The privileged `LiveOps` adapter (SDB/root/signing, one-shot callback token)
  is **not** in this public repository. Without one the tool fails closed.
- This slice makes no claim that boot, root or the launcher are unchanged; only
  the parent's own live channel can verify that.
- A stock-TV consumer setup still needs a published, reviewed, typed adapter
  and an authenticated-root read path. Both are future work, not assumed here.

On the reference TV the Samsung SDK rejects pushes to `/tmp`. Run directories therefore
use `/home/owner/share/tmp/sdk_tools/quiettv-collect-<run-id>`. The trusted adapter must
create an accessible directory only inside that fresh namespace, verify uploaded hashes,
and remove the directory after cleanup. A transfer/readback/removal probe passed there;
this does not establish compatibility on another model or firmware.
