# Contributing

Thanks for helping. This project controls a real TV in someone's home, so the bar is "proven", not "plausible".

## Ground rules

- **No secrets or owner values in the repository.** No keys, tokens, IP or MAC addresses, serial numbers, home-folder paths or screenshots. Examples use `10.20.30.40` and `02:00:00:00:00:01`. CI runs `tools/public_source_guard.py` on every pull request.
- **Allowlists, not free text.** Tool inputs that reach the TV are enums or exact patterns. No tool may run arbitrary commands, read arbitrary files or fetch arbitrary URLs.
- **Fail closed.** Every control call checks the TV's model and MAC against the owner's config first. Unsupported models and firmware are rejected, not guessed.
- **Honest results.** Nothing reports "applied", "fresh" or "works" without evidence. Captures show the UI layer only; video and sound must be checked by a person.
- **Scope.** One verified profile at a time. This is not a universal TV flasher and not a cloud service.

## Development

```
cd public-llm
python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"      # Windows: .venv\Scripts\python.exe
.venv/bin/python -m pytest -q
python tools/public_source_guard.py ..
```

The tests use a loopback fake TV and talk real MCP over stdio. They need no TV and no network.

**Public source guard.** CI scans the whole tree. Example values are `10.20.x.x` and `02:00:00:00:00:xx`. A
test file that must carry other fake LAN values or planted secrets as negative controls is approved by exact
hash, with a reason, in `.github/public-fixture-approvals.txt` (`<path> <sha256> <reason>`, `tests/` or
`fixtures/` paths only). Editing the file voids its approval until a maintainer re-reviews it. Reviewed images
go in `.github/public-binaries.txt` the same way. Nothing is exempted by folder name.

**Models.** Contributions must not try to make a model ignore its own refusals or policies.

## Pull requests

Fill in the pull-request template. Offline test output is always required.

**Device-affecting changes** (anything that changes what is sent to the TV, the identity check, profiles, allowlists, capture handling or maintenance) also need the **hardware gate**: a maintainer runs the change on the supported TV at the **exact head commit SHA** and records an attestation (see `docs/HARDWARE-GATE.md`). Pushing again invalidates it. CI cannot do this for you; it never has access to a TV.

## Adding a device profile

Open a feature request first. A profile needs the exact model code, software version, OS and CPU architecture, the TV-side key and app allowlists, and a maintainer who owns that device to attest real-device results. Generic "all Tizen TVs" profiles are not accepted.

## Security issues

Do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting on this repository.
