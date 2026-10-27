## What this changes

<!-- One or two sentences. -->

## Is this device-affecting?

- [ ] No: docs, tests, CI or offline-only code. Skip the hardware section.
- [ ] Yes: it changes what is sent to a TV, the identity check, profiles, key/app allowlists, capture handling or maintenance.

## Offline test results (required)

```
<paste the full `python -m pytest public-llm/tests -q` summary line and any failures>
<paste `python public-llm/tools/public_source_guard.py .` output>
```

## Hardware evidence (required for device-affecting changes)

A maintainer must re-run this on the **exact head commit** before merge; any new push invalidates it.

- Exact tested commit SHA (40 characters):
- TV model:
- TV software version (firmware):
- Profile id:
- Device checks run, each with evidence (capture ids, status fields):
- Restore evidence (what was backed up, how restore was verified, or "no state changed"):
- Video and sound (captures cannot show them): checked directly on the TV / not affected

Do not paste IP addresses, MAC addresses, keys, tokens, screenshots of private content, or paths under your home folder.

## Checklist

- [ ] No secrets, owner values, screenshots or build output are committed.
- [ ] New tool inputs are allowlisted (enums or exact patterns), not free text sent to the TV.
- [ ] Read-only tools are annotated read-only; anything that changes the TV is not.
- [ ] Nothing reports "applied" or "works" without evidence.
