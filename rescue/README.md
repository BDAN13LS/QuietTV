# QuietTV Rescue (restore-only, offline) — engineering slice D3

**This build cannot recover a real TV by itself.** No transport adapter ships
(`tvkit.transport.TRANSPORTS` is empty) and no cached bootstrap/root method is
bundled; that is deferred to D5b publishing. **There is no hard-brick
guarantee.** If the TV will not start, use Samsung's own recovery (Settings >
Support > Self Diagnosis / Reset, or Samsung support).

Nothing here has run against hardware. Every restore test uses one synthetic
package and a fake in-memory adapter.

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

## Not done yet

- A real adapter and the cached bootstrap/root method (D5b).
- capsule v2 / profile v2 (firmware build and region binding) (D5). Until
  then a firmware build or region change is NOT detected on the built-in
  v1 profile; v2 profiles are refused outright.
- Removing our launcher or root helper (`ourPackages`), boot/startup keys.
- Run journal and resume after a stopped run.
- Registry and startup-key rehearsal (the setup plan's rehearsal assumes the
  pre-setup state).
- Bundling Rescue with the Helper and copying it to owner storage (D6).
