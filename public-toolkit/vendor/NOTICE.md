# Vendored upstream notice

The files below are copied from the official repository
`chris-ritsen/samsung-tv-root`, pinned at commit
`4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b`.

- `samsung_tv_root/__init__.py`
- `samsung_tv_root/archive_root.py`
- `samsung_tv_root/sdb.py`
- `samsung_tv_root/root_agent.py`
- `samsung_tv_root/resources.py`
- `samsung_tv_root/doctor.py`
- `root-payload-source/SamsungTvArchiveRoot.cs`
- `root-payload-source/SamsungTvArchiveRoot.csproj`
- `root-payload-source/SamsungTvRootAgent.cs`
- `root-payload-source/SamsungTvRootAgent.csproj`

They remain byte-identical to the pinned upstream files apart from line endings. The one
upstream `sdb.py` correction in this repository adds `newline="\n"` when writing a staged
script; that correction is already present in the pinned source copied here.

The upstream licence is the Unlicense, a public-domain dedication. It is **not** MIT. The
exact upstream licence text is kept separately in `UPSTREAM-LICENSE` in this directory.
The local `root_compat.py` runtime fallback is not vendored: its behaviour is reimplemented
idempotently inside the provider's isolated child worker, without editing these files.
For the same reason the worker sets the hardware-measured restricted-shell timing
(`SHELL_INJECTION_DELAY_SECONDS = 6.0`, `SHELL_INJECTION_MINIMUM_DELTA = 4.0`) in its own
imported module before any capture or root preflight. The upstream values are 2.0/1.5 and
are flaky on the reference TV; the delta check is retained, not bypassed. This is a
measured local adaptation in the child process only and does not edit the vendored files.

QuietTV does not ship prebuilt binaries from this source. The owner builds the two root
payload projects and the inspector locally from the checked sources with .NET 6 and pins
the resulting SHA-256 values in an owner-private configuration file.
