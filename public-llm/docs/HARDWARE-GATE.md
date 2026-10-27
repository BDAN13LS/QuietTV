# Hardware gate

CI proves the offline behaviour: allowlists, identity refusal, redaction, capture validation and the MCP
protocol, all against a loopback fake. It cannot prove anything about a real TV, and it is never given one.
Device-affecting changes therefore need a **maintainer attestation bound to the exact commit**.

## Which changes need it

Anything that changes what is sent to the TV, the identity check, profiles, key or app allowlists, capture
handling, Wake-on-LAN, or maintenance. Wake-on-LAN (`tv_wake`) is covered offline by a recording sender only; no
hardware attestation for it exists yet. CODEOWNERS routes those files to the maintainer.

## How it works

1. A contributor opens the pull request with offline results (template).
2. The maintainer checks out the **head SHA** on a machine that owns the supported TV, runs the offline suite,
   then the relevant real-device checks (for example `tv_status`, a key press confirmed by `tv_capture`, an app
   launch confirmed by `tv_capture`, and any restore path with its evidence).
3. The maintainer writes an attestation (below), checks it with
   `python tools/check_attestation.py attestation.json --head-sha <head SHA>`, and posts its content as a PR
   review comment. Keep owner values out of it.
4. Any new push changes the head SHA and invalidates the attestation; step 2 is repeated.

Enforcement options for the repository owner (configured through the GitHub API or settings, not by this
folder): require CODEOWNERS review for the device-affecting paths, and require the two CI jobs
**Offline checks** and **Public source guard**. A protected GitHub *environment* with a required reviewer can
also hold a manual "hardware approved" step. None of this is configured by these templates.

## Attestation format

```json
{
  "schema": "quiettv-hardware-attestation/1",
  "commit_sha": "<40-character head SHA>",
  "profile": "UN43U8000FBXZA-1300.8",
  "tested_by": "<maintainer handle>",
  "tested_at": "<date>",
  "offline_checks": "pass",
  "device_checks": [
    {"name": "tv_status then tv_capture", "result": "pass", "evidence": "capture id ..., identity mac_verified true"}
  ],
  "restore_evidence": "<what was backed up and how restore was verified, or 'no TV state changed'>",
  "video_and_sound": "checked-directly"
}
```

`video_and_sound` must be `checked-directly` or `not-affected`: a capture shows black where hardware video plays
and records no sound, so it can never stand in for that check.

This gate is a record of human verification, not an automated hardware test. It does not claim more.
