# QuietTV Live Report Collection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or
> superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. This is a SMALL slice,
> not a release.

**Goal:** produce a FRESH `quiettv-device-facts/1` ProductInfo report and a FRESH
`quiettv-inspection/1` native frame from one live TV, then feed them to the EXISTING
`tvkit.factsjoin` join and the EXISTING Helper `--preflight-config` display. Nothing new
is installed, cleaned, or restored on the TV beyond the owner's own diagnostic widget and
the read-only native inspector.

**Base:** merged PR19 (`24e59742df3ff28935a577195b1dd08651e7df1b`).

**Why now:** the offline pipeline already works end to end given four saved files
(`factsjoin.join` → `preflight_display` → `session.run_preflight`). The only gap is
*acquiring* the two artifacts live. `helper/bootstrap.py:acquisition_state()` documents
the gap exactly: the pinned Apps2Samsung CLI has no capture verb, and every TV verb
triggers the upstream SDB-server kill hook. This slice adds collection as an
owner-invoked CLI over an INJECTED privileged adapter — the parent plugs in its own
credential-held `SdbClient.capture` foothold; the public repo ships no implementation,
exactly like `transport.TRANSPORTS` and `capture.CaptureBackend`.

## Constraints (hold all of these)

- No Helper HTTP change: no route takes a path, SERIAL, command, or widget. This slice
  adds no capability to `helper/session.py` and does not enable install/cleanup/restore.
- `collector.js` / `quiettv-device-facts/1` and `tools/tv-inspect` + `frame.py` are
  UNCHANGED. `helper/bootstrap.py` and the certificate path are UNCHANGED.
- The public tool spawns NO device subprocess. All `sdb`/signing/token/root work is the
  parent adapter's. The adapter MUST reuse `bootstrap._run_cli` / `_kill_tree` discipline
  (both pipes capped, hard timeout, whole-tree kill, reaped children) — referenced, not
  mutated. No global `sdb kill-server`, no disconnect of other sessions, no host change.
- Fail closed with no adapter, like `transport.get_transport`. Parent-supplied temporary
  trusted callbacks enable LIVE validation now; a consumer stock-TV release still cannot
  ship from this slice (see "What cannot ship yet").

## New files

- `public-toolkit/tvkit/liveops.py` — the typed, finite privileged-operation contract.
  - Abstract `LiveOps` provider, `read_only = False` but WRITE-LIMITED to its own
    namespace. Empty registry `LIVEOPS: dict[str, Callable[[], LiveOps]] = {}`; the
    toolkit registers none. `get_liveops(name)` refuses when unnamed/unregistered/not
    implemented (mirror `transport.get_transport`).
  - A `LiveSession` returned by `LiveOps.session()` (context manager): binds ONE
    owner-provided target, disconnects only itself on exit. Carries a `session_token`
    (adapter-chosen opaque id) that every read below echoes, so same-session pairing is
    proven, not guessed.
  - Fixed operations, each parameterised ONLY by tool-computed, tool-validated values
    (package id, local artifact path, owned remote tmp path, expected sha256) — never a
    SERIAL, key, callback token, or free command:
    - `assert_absent(package_id, data_paths) -> bool` — package not installed AND no
      bytes at either data path.
    - `install_widget(package_id, local_wgt, local_sha256, remote_tmp_wgt)` — push the
      hash-verified signed `.wgt`, then fixed vendor install
      `0 vd_appinstall <package_id> <remote_tmp_wgt>`.
    - `launch_facts(package_id)` — fixed `/usr/bin/app_launcher -s <package_id>.Facts`.
    - `read_report(package_id, data_paths) -> (bytes, session_token)` — ≤ 262145 bytes
      from `/opt/usr/home/owner/apps_rw/<PKG>/data/device-facts.json` or
      `/opt/usr/apps/<PKG>/data/device-facts.json`. May require parent root.
    - `push_native(local_dll, local_sha256, remote_tmp_dir)` /
      `run_native(remote_dll) -> (bytes, session_token)` — fixed
      `/usr/bin/dotnet <remote_dll> --inventory-framed`; strip exactly one `[exit:N]`
      trailer before returning (`frame.py` decodes).
    - `uninstall_widget(package_id)` — fixed `0 vd_appuninstall <package_id>`, only the
      proven-owned temp package id.
    - `remove_upload(remote_path, expected_sha256)` — remove ONLY an upload this run
      pushed, re-checked by hash and by the exact computed path.
  - `LiveOpsError(ValueError)`: fixed messages; never echoes a SERIAL, path, report byte,
    or DUID (same contract as `JoinError`/`BootstrapError`).

- `tools/tv-collect/collect.py` — owner-invoked CLI orchestrator (`sys.dont_write_bytecode`).
  Inputs (all explicit, validated): `--out-dir` (owner storage, absolute, plain, outside
  every repo via `owner_init.check_outside_repos`; must not pre-exist); `--signed-wgt` +
  `--wgt-sha256`; `--inspector-dll` + `--dll-sha256`; `--package-id` (pack.py
  `PACKAGE_RE`); `--policy` + `--profile` (owner's trusted files); `--adapter <name>`.
  Any IPv4 it is handed is validated private-only (reuse `session.validate_target`
  `PRIVATE_NETS`). Flow, with ordering ENFORCED by the orchestrator:
  1. Validate every path/hash/id; pin both artifacts by sha256 before any push.
  2. `with liveops.session() as s:` (one target).
  3. `assert_absent` — refuse if a leftover temp package or a prior data file exists
     (freshness precondition; no silent overwrite). A guarded pre-clean of EXACTLY the
     temp package id is allowed, then re-assert.
  4. `install_widget` → `launch_facts` → `read_report`. The report is read only after a
     passing `assert_absent` + this run's launch, so the bytes are proven fresh.
  5. `push_native` → `run_native`; `frame.py.decode_frame` the result in-process.
  6. Require `read_report` and `run_native` to carry the SAME `session_token` → pairing.
  7. `parse_product_info` + `extract_native_runtime` + `join(...)` against the owner's
     trusted policy+profile (exact-tuple + generation check; drift/stale/raw-DUID all
     refuse inside factsjoin). Write `device-facts.json`, `inventory.frame`, and the
     `JoinReceipt.body()` to `--out-dir` by create-or-fail; emit the collection metadata
     (`collectionId`, both fresh sha256, `session_token`). No boot/root/launcher claim —
     that is parent-verified and named as out of scope.
  8. Always `uninstall_widget` + `remove_upload` for this run's own namespace (collision +
     hash checked); on any refusal, clean up the same way and raise. Never touch Home.

- `tools/tv-collect/README.md` — the contract, the manual recipe, and the honest
  "what cannot ship yet". `tools/tv-collect/test_collect.py` — tests below.

## Parent adapter plug-in (no secret export)

The parent constructs its `SdbClient.capture`-based `LiveOps` in its OWN reviewed code and
either registers it in `liveops.LIVEOPS` or passes a temporary instance for live
validation. SERIAL, signing identity, the one-shot TCP callback token, and the root method
live entirely in that adapter. The public contract passes only package id, local artifact
paths, owned remote tmp paths, and sha256 — so nothing here exports or logs a secret, and
no public file names a SERIAL or key.

## Freshness / replay / generation

- **Freshness:** proven by `assert_absent` immediately before this run's install+launch
  (the "securely cleared prior artifact + relaunch" path) — the widget schema is NOT
  changed to add a nonce. The read is refused unless absence passed and the launch is this
  run's.
- **Pairing:** report and frame are read inside ONE `session()` and must echo one
  `session_token`; a cross-session pair refuses.
- **Generation/target:** `factsjoin.join` already enforces exact equality of every
  observed value against the owner's trusted policy and an exact `tvkit-profile/2` match;
  a raw DUID can never pass as the hashed identity (scheme domains differ). No new matcher.

## Tests (every guard proven both ways via `dev/guard_off_check.py`)

- Positive: fake `LiveOps` returns the synthetic report + frame matching the synthetic
  policy/profile fixture → two artifacts + receipt written; `join` succeeds;
  `preflight_display` projects seven facts; feeding the two written files to
  `session.run_preflight` (via `--preflight-config`) reproduces them offline.
- Negative: no adapter → fail closed, nothing written; `assert_absent` false → refuse;
  read-before-launch / out-of-order → refuse; widget or dll sha256 mismatch → refuse,
  nothing pushed; mismatched `session_token` → refuse; drifted report value vs policy →
  refuse (factsjoin); report > 262145 bytes → refuse; remote path with `..`/metachar →
  refuse; `--out-dir` inside any repo/worktree → refuse.
- Historical controls (the exact traps the memory names): a pre-seeded STALE data file
  (assert_absent false) must refuse; an `incomplete` report must refuse
  (`parse_product_info`); a raw-DUID-as-identity report must refuse. Positive control: the
  SAME fakes without the fault produce a passing receipt, so a refusal is not a broken
  test.
- Guard-off (each flips in an isolated copy): remove the assert-absent-before-read
  ordering → a stale artifact is accepted; remove the sha256 pin → an unverified widget is
  pushed; remove the `session_token` pairing check → a cross-session pair is accepted.

## One warm live validation recipe (proves new code ran)

On the parent's reference TV, with the parent's temporary trusted `LiveOps` injected (the
parent handles TV, signing, and token):

1. Run `collect.py` once with the owner's real signed widget, the pinned
   `QuietTVInspect.dll`, and the owner's real policy+profile.
2. It MUST: assert the temp package + data file absent; install the widget; launch Facts;
   read a report; push+run the inspector; decode the frame; join; write three files; then
   uninstall + remove its upload.
3. Proof it is not a placeholder: the receipt's `productInfoSha256` and `inspectionSha256`
   are freshly computed from bytes read THIS session (differ from every saved fixture); the
   seven derived facts equal the reference TV's known values; a second run writes a NEW
   `device-facts.json` (freshness), and feeding the two fresh files to
   `helper/server.py --preflight-config` reproduces the same seven facts offline.
4. Pre-registered "not proven": `assert_absent` cannot be satisfied, the read returns a
   pre-seeded stale file's bytes (identical sha256), or the two reads carry different
   `session_token`s.

## What cannot ship yet (no success placeholder)

- The privileged `LiveOps` adapter (the reviewed upstream `SdbClient.capture` /
  `vd_appinstall` foothold with its one-shot TCP callback) is NOT in this public repo and
  cannot be without the owner's reviewed upstream code and credentials. With no registered
  adapter the tool fails closed; the parent's temporary callbacks are for LIVE VALIDATION
  only, not a consumer release.
- This slice makes NO claim that boot, root, or the launcher are unchanged — only the
  parent's own channel can verify that. The receipt records only what was proven here:
  two fresh artifacts, their hashes, the session proof, and the join result.
- A standalone stock-TV consumer setup still needs a PUBLISHED, reviewed, typed privileged
  adapter (future D5b-scale work) and the authenticated-root read path; both are named
  here as the remaining provenance, not assumed.
