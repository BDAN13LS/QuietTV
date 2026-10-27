# D1 engine contracts (offline)

Plan phase D1 of `docs/superpowers/plans/2026-10-27-consumer-install.md`.
Everything here is data and pure checks. Nothing touches a TV, nothing opens
a network connection, and `transport.TRANSPORTS` is still empty (a test
checks it). capsule/1 and every existing API are unchanged.

## Profile v2 (`tvkit/profile.py`)

- `tvkit-profile/2` adds two exact match fields, `firmwareBuild` and
  `region`. `tvkit-profile/1` profiles still load and match on the five
  version 1 fields. The built-in profile stays version 1: no build or region
  is inferred for it. Tests use the synthetic
  `tests/fixtures/consumer/profile-v2.json`.
- `MATCH_FIELDS` is unchanged, because the capsule/1 device block is built
  from it. New: `PROFILE_SCHEMA_V2`, `MATCH_FIELDS_V2`, `Profile.schema`,
  `Profile.match_fields`.
- `parse_device_facts` accepts `firmwareBuild` and `region` as optional keys
  and refuses empty, padded or `<CONFIGURE` values. A v2 profile refuses
  facts that lack them, inside `match_device`.
- `match_device` takes its field list from the schema, not from the match
  mapping. `Profile.match` is read-only (`MappingProxyType`). Errors name the
  field and never the value or the device id.
- **Recovery compatibility:** capsule/1 cannot bind build or region, so
  validation, assembly and CLI recovery refuse profile/2 outright before
  writing anything. Capsule/2 is still needed. Profile/1 remains supported;
  validation also calls `match_device` when facts are supplied.

## Catalogue (`tvkit/catalogue.py`)

```
parse_catalogue(data, profile) -> Catalogue                 # tvkit-components/1
parse_inventory(data, profile, facts) -> Inventory          # tvkit-inventory/1
resolve(catalogue, selection, profile, facts, inventory) -> ChangeSet
```

- **Why an inventory is passed in.** Device facts cannot say which packages
  are installed, at which version, with which payload hash. The inventory is
  a separate strict document read from the TV by the owner's transport
  (none ships here). It is bound to `profileId` and to the device id hash.
  Each entry is `{packageId, version, sha256, installRoot}`, matched by exact
  package id, never by display name or icon. `installRoot` must be one of
  the profile's install roots.
- **Component fields:** the plan's list plus one addition, `status`
  (`reviewed` | `draft`). Unknown and missing fields are refused.
  `expectedVersions` keys must equal `packageIds` exactly. `allowedActions`
  may only list `remove` (D1 never disables). A package may belong to only
  one component.
- **Public-safety scan:** a catalogue containing a PEM marker,
  `private key`, `deviceId`, `duid` or `certificate:` anywhere is refused.
  Fixture ids are all synthetic `org.example.*`. There are no entries for
  Bixby, voice or Store packages; the voice fixture is a synthetic draft.
- **Graph rules (parse time):** `dependsOn` naming an unknown component or
  itself, duplicates, and cycles of any length refuse the whole catalogue.
- **Selection:** a list/tuple/set of component ids to remove. A string or
  mapping, a duplicate, or an unknown id refuses the whole request, so a typo
  can never quietly shrink the approved list.
- **Held, per component, with a reason:**
  `protected-id` (profile substrings; no Security Center override yet),
  `not-reviewed` (draft, or reviewed with empty evidence),
  `remove-not-allowed`, `restore-not-supported`, `not-installed`,
  `partially-installed`, `installed-version-differs:<pkg>`,
  `installed-hash-differs:<pkg>`, `required-by:<component>`.
- **Dependency semantics.** `A.dependsOn = [B]` means A needs B. Removing B
  is held while any component that depends on it is *retained*. Retained
  means at least one of its packages is in the inventory and it is not being
  removed in this ChangeSet. Holds cascade: a held dependent keeps its
  provider. This repeats until nothing changes. Holds only ever grow, so the
  result is the unique fixpoint, and the selection order cannot change it (a
  test runs every permutation and compares digests). Removals are ordered
  dependents first, ties broken by id. Packages that are not in the
  catalogue cannot be seen as dependents. That is a stated limit.
- **ChangeSet:** frozen dataclasses and tuples, with `to_json()` and a
  `digest` (SHA-256 of canonical JSON). It binds the profile, the device id
  hash, the catalogue digest and the inventory digest. The only action is
  `remove`.
- **`check_changeset(changeset, catalogue, profile, facts, inventory)`:** a
  ChangeSet is plain data and can be built by hand. Anything that acts on one
  (the D2 plan builder, `audit.declared_from_changeset`, `states.report`)
  calls this first. It re-resolves the same ids and requires an identical
  digest.

## Journal (`tvkit/journal.py`)

```
Journal.create(path, plan, profile, facts) / Journal.open(...)
journal.started(step) / .verified(step, evidence_sha256) / .failed(step, reason_code)
resume(path, plan, profile, facts, reverify) -> ResumePoint(journal, next_step, reverified_step)
run_with_journal(point, executor) -> last verified step
```

- JSONL on the PC. Each append is written, flushed and fsynced before it
  returns. `create` uses exclusive create and never overwrites. Line 0 binds
  the plan digest, the profile id, the device id hash and the step count.
- Each line carries `seq`, `prevSha256` and its own `sha256`. Steps go in
  plan order: `started`, then `verified` (an evidence SHA-256 is required)
  or `failed` (an enumerated reason code). Each record also carries the
  step's own digest and action name.
- The journal never holds free text, stdout or credentials. An unknown
  reason code is recorded as `adapter-error`.
- **Fail closed** on any of these: a partial last line, bad JSON, an
  unknown kind or field, a sequence gap, a broken chain, a different
  plan/profile/device/facts, an out-of-order step, or a file changed on disk
  since it was read.
- **Limit:** the chain has no key and no external anchor. It catches
  corruption and edits, but not a cleanly truncated trailing record or a
  deliberate re-hash. A real anchor (for example, the last hash shown to the
  owner or signed) is a later phase. No crypto library was added.
- **Resume:**
  - The last event is `verified` N: the caller's `reverify(step)` re-reads
    the TV, and its evidence hash must equal the journal's. Then the run
    continues at N+1.
  - The last event is `started`: `JournalInterrupted`. The effect is
    unknown, so the step is neither assumed done nor re-run.
  - The last event is `failed`: the run is closed.
  - Tests crash a fake executor at every boundary (before `started`, after
    `started`, after the mutation, after `verified`, and mid-append) and
    check that no mutation is ever repeated.
- `run_with_journal` is a driver for an executor object. It is not a
  transport. Nothing in the repo registers or ships an executor; tests use a
  fake. Wiring it into `transport.run_plan` is a later phase.
- **Plan binding.** `Plan` and `Step` are mutable dataclasses, so the
  journal never trusts the caller's object.
  - **Private copy.** The journal keeps a private deep copy of the approved
    plan and runs only from it. `journal.plan` and `approved_step(n)`
    return fresh copies, and each executor gets its own copy of the step.
  - **Re-checks.** It re-hashes BOTH the private copy and the caller's plan
    against the bound `planSha256` at three points:
    - before `started`: a change means nothing is written and nothing runs;
    - immediately before the executor;
    - after the executor returns.
  - **Outcome of a change.** If the plan changed at either of the last two
    points, the started step is durably closed as `failed` with reason
    `plan-changed`, no later step runs, and `PlanChanged` is raised. A
    `failed` record can always be written, so the outcome is never skipped.
  - **What is covered.** Changes to an action, a target or nested `detail`,
    and appended or removed steps, are all caught. A changed plan is never
    approved, even if it is changed back afterwards.
  - **Regression evidence.** Before this fix, a plan changed after
    `create`/`open` ran under the original binding without any error. The
    per-record step hash was computed from the same mutated object. The
    guard-off test reproduces exactly that.

## States (`tvkit/states.py`)

- `removed | disabled | kept | held | failed`.
- `report(changeset, observations, disabled_flags)`:
  - `removed` only when every package path has a `PathObservation` of that
    exact path with `present=False` and an evidence SHA-256;
  - a registry flag is never an input to `removed`;
  - a removal target that is still present is `failed`
    (`still-present-disabled:<pkg>` when the flag says disabled), never
    `disabled` or `removed`.
- `untouched_state(...)` for components the run did not act on: `disabled`
  or `kept` only with proven presence and a known flag, otherwise `failed`.

## Write audit (`tvkit/audit.py`)

- `Snapshot(tree, registry, keys)` is immutable and validated:
  - `tree`: absolute path -> SHA-256;
  - `registry`: (app id, field) -> value;
  - `keys`: key -> SHA-256.
- Nothing captures snapshots from a device yet.
- `WriteAudit(before, after).unexpected(declared)` lists every undeclared
  difference by name, never by value. `undone(declared)` lists declared
  removals that did not happen.
- `declared_from_changeset(changeset, profile)`: a declared removal must be
  one package directory **directly below an install root**. It permits only
  that path and the entries below it to disappear. It never permits
  additions, content changes, prefix siblings (`org.example.guide2`) or
  changes to the parent.
- Directory entries must hash the directory's own metadata, not its
  listing.

## Tests and negative controls

Run from `tests/`:
`python -m unittest test_public_toolkit_profile_v2 test_public_toolkit_catalogue test_public_toolkit_journal test_public_toolkit_states test_public_toolkit_audit`.

Each critical guard has an in-test guard-off control (`mock.patch`). The
test asserts the refusal, then weakens that one guard and asserts the
protected outcome changes:

- v2 fields come from the schema;
- the device match in `resolve`;
- the inventory device binding;
- the unknown-selection refusal;
- the public-safety scan;
- the dependency block;
- the hash match;
- the protected hold;
- cycle detection;
- the journal chain;
- the journal header binding;
- the partial-tail refusal;
- the re-verify comparison;
- the plan snapshot and digest re-check (the guard-off run executes the
  mutated step silently);
- evidence required;
- the absence proof for `removed`;
- exact-prefix audit permits.

The interrupted-step refusal is defence in depth: with it off, the evidence
re-check still refuses, and only the error type (and its "inspect the TV"
instruction) changes. A counting wrapper checks one `fsync` per journal
write.

`public-toolkit/dev/guard_off_check.py` was not changed (outside this
lane's files). These guards should be added there as source mutations.

**Fixtures.** `tests/fixtures/consumer/` holds `profile-v2.json`,
`components.json` and `inventory.json`, all synthetic. The device facts are
built in Python (`test_public_toolkit_profile_v2.consumer_facts`), because
the export refuses any JSON file with a `deviceId` key.
`public-toolkit/export-allowlist.json` exports the three consumer JSON
fixtures explicitly, together with `tests/test_public_toolkit_*.py`.
It does not export the entire fixture directory.

**Compatibility note.** `Profile.match` is now a `MappingProxyType`, so
`copy.deepcopy`, `pickle` and `dataclasses.asdict` of a `Profile` raise.
Nothing in the repo does any of these (checked by grep). `dict(profile.match)`
and the JSON CLI output still work.

## Not in this slice

- Plan builders from a ChangeSet (`remove-package` steps with
  pre-conditions), and wiring the journal into `run_plan`. Planned for
  D2/D4.
- Snapshot capture, a Security Center reviewed override (D5), any disable
  action, and an external journal anchor.
- Catalogue files under `public-toolkit/profiles/components/`. No real
  catalogue exists until a component is reviewed with hardware evidence.
