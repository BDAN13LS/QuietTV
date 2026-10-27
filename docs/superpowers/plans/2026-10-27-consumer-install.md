# QuietTV Consumer Install and Restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** an owner installs QuietTV's root bootstrap, launcher and removal
menu on a supported Samsung TV without AI or coding, and can restore every
QuietTV change from a standalone Rescue.

**Spec:** `docs/CONSUMER-INSTALL-DESIGN-2026-10-27.md`.

**Architecture:** one engine (`public-toolkit/tvkit`, extended) with
front ends for the Windows Helper, the TV browser as its screen, the on-TV
menu and Rescue. Plans are data; registered transport adapters run them
behind the existing `run_plan` gate.

**Tech Stack:** Python (python.org Windows embeddable runtime), a plain
HTML/JS local UI, the .NET 6 XattrBackup helper on the TV, and minisign
release signatures. No Electron.

**Global Constraints:**
- Recovery before cleanup: nothing is deleted before backup, off-TV
  readback, rehearsal and Rescue are proven.
- Exact profile only. Anything unmatched is refused.
- These planning docs give no exploit steps. That is a documentation
  choice, not a ban on shipping reviewed upstream root code (D5b).
- No OEM payload is hosted until permission is verified.
- No shared keys, certificates or owner state.
- TV sessions need the test owner present and never run while he is watching.
- Every guard has a test proven both ways (`public-toolkit/dev/guard_off_check.py`).
- Each phase is bounded. Anything left open is named, together with the
  probe that settles it.

**Review Focus:**
- A zero-write guarantee on refusal.
- Compare-and-set restore that preserves unrelated changes.
- Rescue independent of the launcher and any on-TV agent.
- No silent "removed" claims.
- The schema limits are honoured.

Proposed files below are not built yet.

## Effort assumptions

One engineering day is about 6 focused hours of evening/weekend work. These
are broad ranges, not deadlines.

- **Engineering recovery proof** (D0a, D1–D4: capsule/1-limited, one
  optional package, no GUI, signing or full launcher build): about 15–25
  days plus about 4 TV sessions. **Not a consumer release.**
- **First consumer alpha** needs D5 (schema v2), D5b (launcher source,
  build and signing), D3 Rescue and D6 (Helper GUI), with the optional
  cleanup gates in place. Broad conditional estimate: roughly the remainder
  of the full range below. Not a deadline.
- **Full production** (all phases): about 45–65 days plus about 11 TV
  sessions.

All of these depend on the D0 outcomes and on the privileged adapter staying
stable.

## D0a. TV-URL capability review (non-mutating) — 1–2 days + 1 TV session

- Files: `docs/D0-TV-URL-FEASIBILITY.md`; `helper/d0/probe_page.py` (a
  throwaway LAN page: pairing code, `fetch` round-trip, download attempt).
- Compare three cases separately: the **raw stock browser** (a static page,
  no helper), a **LAN Helper page**, and an **already-installed homebrew
  catalogue** (from published prior art; no install on our TV in D0a).
- Record for each: LAN `http` reachable, private-network or mixed-content
  blocks, downloads, and which published root/install routes need what
  foothold.
- Test `tests/test_d0_probe_page.py`: binds only to an explicitly given
  address, rejects a wrong Origin/Host, and makes no outbound calls.
- **Verdict rules (pre-registered):** a page load or user agent shows
  reachability **only**. A route is "candidate" only with a named
  mechanism. Nothing in D0a counts as install or root.
- **If route A/C has no candidate:** it is not advertised or shipped and
  does not delay Windows V1. Further research needs the maintainer's cost decision.
- **If the browser cannot save off the TV:** browser-only deletion stays
  blocked until an independently validated off-TV channel exists.

## D0b. Own-namespace bootstrap proof — only after explicit owner authorization

- Bounded hardware proof that bootstrap, signed install, uninstall and root
  work from the browser on stock firmware, in QuietTV's own namespace.
- **No cleanup and no boot mutation.**
- Counts only if it actually worked from stock without a PC or helper.
- Exit: evidence committed (tree hashes before and after, uninstall
  returns them) or a "not proven" verdict.

## D1. Engine contracts — 3–5 days, offline

- **Reuse** `profile.parse_device_facts` / `match_device`. Add
  `tvkit-profile/2` match fields (`firmwareBuild`, `region`) and keep
  version 1 readable. No separate facts API.
- `tvkit/catalogue.py`, schema `tvkit-components/1`, stored at
  `profiles/components/<profileId>.json`. Each component has: `id`,
  `packageIds[]`, `expectedVersions{id: {version, sha256}}`, `title`,
  `explain`, `category`, `recommended`, `allowedActions`, `dependsOn[]`,
  `consequences[]`, `restoreSupported`, `evidence[]`. Public files never hold
  certificates or owner state; a test checks this.
  - `resolve(selection, facts) -> ChangeSet` refuses protected or unknown
    ids, broken dependencies, and version/hash mismatches.
- `tvkit/journal.py`: an append-only JSONL run journal on the PC.
  `resume(journal, plan) -> int` re-verifies the last verified step.
- `tvkit/audit.py`: `WriteAudit` takes inventories before and after (tree
  hashes, listed registry fields, keys). `unexpected(changeset)` lists every
  undeclared difference.
- `tvkit/states.py`: `removed|disabled|kept|held|failed`. "removed"
  requires the path to be absent after the run, verified.
- Tests: `test_public_toolkit_{catalogue,journal,audit,states,profile_v2}.py`.

## D2. Backup and off-TV readback gate — 4–6 days + 1 TV session

- `plan.backup_plan(facts, profile, changeset)` with new capabilities:
  `capture-package`, `xattr-dump`, `registry-read`, `system-key-read`,
  `hash-on-device`.
- `tvkit/backup.py`: `readback_gate(path, device_hashes, facts) ->
  GateResult`. It re-reads the stored copy, runs `capsule.validate` with
  device facts, and compares the hashes taken on the TV.
- Baseline label in `capsule.json`: `stock|current-state`.
- `tvkit/vault.py` uses the `age` format (no new crypto).
- Transport: at first, the existing private host-side adapter (pinned per
  firmware), registered from owner code. It is replaced by the published,
  typed adapter in D5b.
- Tests: partial or truncated capsule, wrong device hash, wrong vault key,
  readback of a different file.
- Exit: one candidate package passes the gate, with zero writes outside
  scratch.

## D3. Standalone Rescue — 4–6 days + 1 TV session

- `rescue/main.py`: restore-only, on the engine. Imports nothing from
  `app/` or `boot/` or the launcher, and never calls an on-TV agent. It ships
  with the Helper and is copied to owner storage.
- Network: **internet/WAN blocked, LAN to the TV available.** It carries
  its own cached bootstrap/root method; if that is unavailable, it reports
  "no hard-brick guarantee" and points to Samsung's recoveries.
- Order: our changes undone, control verified, launcher removed, root helper
  removed last (`remove-root-helper` only after `verify-control-final`).
- Refusals: model, firmware build or region changed; app newer than the
  capsule (report it, don't overwrite).
- Tests: `test_rescue_independence.py` (import graph, WAN blocked by a test
  socket guard) and `test_rescue_refusals.py`.
- Exit: Rescue restores into the TV rehearsal area with WAN blocked.

## D4. Engineering recovery proof slice — 2–3 days + 2 TV sessions

Preflight → off-TV backup → rehearsal → remove **one** optional package →
restore it from Rescue. Explicitly limited to capsule/1 scope: one app
directory, listed registry fields, no boot changes.

- Candidate: present, catalogue `allowedActions: [remove]`, not protected,
  no dependants. the test owner approves the exact id.
- Pre-registered exit:
  - zero changes when the backup is missing or the device unsupported
    (both run on purpose);
  - zero unexpected writes;
  - exact bytes, UID/GID, mode, xattrs, links and fields restored;
  - a second restore run makes zero writes;
  - the test owner verifies Home, remote, Settings, sources and Store.

## D5. Recovery schema v2 and component profiles — 6–9 days + 2 TV sessions

- `tvkit-recovery/2` (with capsule/1 still read):
  - `bootChanges[]`: per record/field `{target, field, before, ours,
    beforeSha256, oursSha256}`. Undo is a three-way compare-and-set: write
    only if the current value equals `ours`; if it equals `before`, skip;
    otherwise stop and report, preserving unrelated changes. Never overwrite
    a whole cache or registry.
  - `externalRefs[]`: external targets are recorded as read-only references
    and checked on restore. Recreate only the symlink itself inside the
    approved package path; never extract or write through to its external
    target. Unknown targets hold that component. Hard links and devices
    remain refused.
- `profiles/components/reviewed/<packageId>.json`: an exact-id override
  for protected-substring ids, holding the evidence and the reviewer.
  - First override: the informational Security Center UI.
  - Tests: the generic path still refuses it, a near-miss id is refused,
    and other `security` ids stay protected.
- Held packages are shown as `held`, with the reason. The firmware-update
  hold is a separate opt-in.
- Exit: "Restore Samsung" over our boot changes is proven on hardware. Only
  then does the README claim it.

## D5b. Launcher, host and root-recovery source; per-owner build and signing — 8–12 days + 2 TV sessions

The public repo has no `app/` or `boot/widget/` yet. The files below are
inferred from known missing pieces. Private files are not read until this
phase starts.

- **Sanitized source release** (export allowlist plus deny-values scan,
  secrets and provenance audit):
  - the `app/` native host;
  - selected `boot/widget/` service, bridge, inputs and config
    *templates* (`config.xml.template`; no owner name, keys or addresses);
  - the root-recovery library.
  The root library is published under its reviewed upstream licence. There
  is no permanent private-only root prerequisite.
- **Reproducible builds:**
  - `tools/build_tv.py` builds the owner-independent ARMv7 helpers and the
    frontend with a pinned toolchain (`release/toolchain.lock.json`).
  - It outputs `release/artifacts.manifest.json`: file, SHA-256 and
    toolchain hash for each artifact.
- **Per-owner, created locally, never shipped:**
  - device config and pairing keys (`helper/owner_init.py`);
  - the Samsung certificate for the owner's DUID;
  - signing (`helper/sign_wgt.py`), done on the owner's PC.
- **Install on the TV:**
  - an actual signed WGT install;
  - default Home/startup setup;
  - autonomous root recovery at cold boot with no PC (a pinned TV-side
    hook);
  - an installed-app catalogue filter.
  - IPTV is hidden or marked unavailable until its black picture is fixed.
- **Typed privileged adapter** `tvkit/transports/root_adapter.py`:
  - declares exactly the plan capabilities it implements;
  - Rescue stays offline and resumable, and stays active until a restore
    completes.
- **Gate:** the root and boot-cache implementation needs the per-record
  patch coverage from D5 **before** any consumer default-Home install.
- **Tests:**
  - `test_source_export_tv.py`: no owner values, and templates only;
  - `test_build_reproducible.py`: two clean builds give the same manifest;
  - `test_owner_init.py`: keys and certificates are written to owner
    storage only and never into the repo or a release;
  - `test_root_adapter_contract.py`: capability set and failure on refusal;
  - `test_catalogue_filter.py`.
- **Exit:**
  - signed install, cold boot to the QuietTV Home without the PC, and root
    recovered;
  - uninstall through Rescue returns the D5 before-hashes.

## D6. Windows Helper and on-TV menu — 7–10 days + 1 TV session

- `helper/server.py` (`127.0.0.1`, CSRF); `helper/ui/`.
- Screens: Check TV → Back up → Install root + launcher → Choose cleanup
  → Review exact list → Approve → Verify with remote → Report. Also
  Restore Individual, Restore Samsung and Open Rescue.
- Guided manual steps: Developer Mode with the PC IP, then a restart; the
  Samsung account sign-in for the owner's own DUID certificate (borrow
  tizen-homebrew's flow after a licence check; partner eligibility not
  assumed).
- On-TV menu: Remove is enabled only when a reachable backup channel or a
  verified already-exported backup of that exact version exists; otherwise
  it is greyed out with the reason.
- `release/manifest.json` (SHA-256, version, rollback pins) plus a minisign
  signature and a reproducible build.
- Tests: `test_helper_server.py` (loopback bind, CSRF, approved change-set
  ids only); `test_release_manifest.py` (tampered file, bad signature,
  rollback below the pin); `test_menu_greyout.py`.
- Exit: a fresh Windows account with no Python, npm or Tizen Studio
  completes the owner flow.

## D7. Companion, measurement, privacy audit — 4–6 days + 1 TV session

- `helper/companion.py` (route B): opt-in, pairing code, 15-minute session,
  the same approval list.
- `tools/measure/`: storage plus PSS under a matched workload, 3 runs each
  before and after, reported as median and range. Include the new launcher,
  root helper and recovery/control services in the total, not only the
  processes removed.
- `tools/privacy-audit/`: known services and domains, with the stated
  limits.
- Exit: README promoted from `docs/drafts/README-consumer.md` once its
  claims match the evidence.

## D8. Release gate — 2–3 days

Export review, the maintainer's licence decision, the guard-off suite, the
known-hardware list, provenance of every borrowed piece, and the maintainer's
go/no-go.

## Failure matrix

| Failure | Expected | Test |
|---|---|---|
| Wrong model/firmware build/region | Refuse before any write | profile_v2, HW D4 |
| Package version/hash differs | Refuse that component | catalogue |
| Capsule hash mismatch | Refuse, naming the file | capsule (exists) |
| Network drop at any step | Journal stops; resume re-verifies | journal, every boundary |
| Power loss at any step | Same; staged files moved only after `verify-tree` | journal + HW D4 |
| Insufficient space | Preflight refuses, with sizes | backup |
| Partial backup | Gate fails; no setup | backup |
| Failed metadata restore | Stop before `move-into-place`; scratch removed | rescue |
| Root lost / cached route unavailable | Report it, no partial writes, no hard-brick guarantee | rescue |
| Apps updated since backup | Keep the newer one, report it | rescue |
| Boot field changed by someone else | Three-way check stops; nothing overwritten | recovery v2 |
| GitHub offline | Runs from the offline copy | helper, rescue |
| Browser cannot back up off the TV | Browser-only delete blocked until a validated channel exists | D0a, menu |
| Unexpected write | Run failed, paths listed | audit |

## Owner 5-step flow (acceptance, no AI)

1. Download QuietTV Helper for Windows and open it (it verifies its own
   signature).
2. On the TV with the remote: Developer Mode on, enter the PC address the
   Helper shows, restart the TV.
3. In the Helper: Check TV, then Back up. Wait for "Backup verified".
4. Choose root + launcher and Recommended or individual cleanup, review the
   exact list, approve.
5. Verify Home, remote, Settings, sources and Store with the remote. Restore
   is always Helper → Restore, or Rescue.
