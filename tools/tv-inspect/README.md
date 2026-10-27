# QuietTVInspect — read-only TV inventory collector

A small .NET 6 console tool (pure BCL, AnyCPU, runs on the TV's ARMv7 runtime) that reports
which apps the package registry lists and which of them are really installed. It is the
inventory step of the approved consumer backup/preflight flow. It **only reads**: it writes,
copies, archives, removes, registers, signs and launches nothing, and it opens no network
connection. The parent flow handles the device, transport and any later step; a root
capture writer, if one is ever built, is a separate tool.

## Commands

| command | does |
|---|---|
| `dotnet QuietTVInspect.dll --self-test` | memory-only checks against a fake system; touches no file, process or network |
| `dotnet QuietTVInspect.dll --inventory-framed` | the same document inside one verifiable frame (Linux only); **use this on the TV** |
| `dotnet QuietTVInspect.dll --inventory` | legacy raw mode: the bare document on stdout |

There are no other arguments: no SQL, path, command or executable can be passed in.
Exit codes: `0` inventory succeeded, `1` inventory failed (document has `status: "failed"`),
`2` usage error or not running on Linux (refusal document, nothing observed).

**Raw mode is not reliable on Samsung.** The TV's .NET runtime appends GC diagnostic lines to
stdout, even with stderr sent to `/dev/null`, so raw stdout is not one JSON document there.
Never parse it with `raw_decode` or by skipping unknown text.

## Framed output and `frame.py`

`--inventory-framed` writes, as one buffer in one write:

```
QUIETTV-FRAME/1 <length> <sha256>\n<payload>\nQUIETTV-FRAME-END/1\n
```

- `<length>` is the exact payload byte count, in decimal with no leading zeros.
- `<sha256>` is the payload's SHA-256 as lowercase hex.
- The payload is the compact one-line UTF-8 JSON document (the raw output without its final
  newline), capped at 8 MiB. A larger document is replaced by a framed `output-oversize`
  failure, and exit is `1`.

`frame.py` (standard library only) is the strict decoder. `decode_frame(stdout_bytes)` returns
the document or raises `FrameError(code)`. It:

- bounds the input, the number of noise lines (4096) and each line's length;
- accepts exactly one frame, and checks the length, end tag and hash;
- parses strict JSON: one line, UTF-8, an object, no duplicate keys, no NaN/Infinity, no floats;
- validates the exact `quiettv-inspection/1` schema (keys, types, sorted unique ids, known
  reasons, status matching records and fatal problems, model/firmware/region null).

Outside the frame, before or after, it allows only whole lines that match the anchored GC
diagnostic shapes:

```
[GC_INFO] GC_Start #<n>: Gen(<d>), Time(<n> ms), Reason(<n>)
[GC_INFO] GC_End #<n>: Gen(<d>), Time(<n> ms)
```

Any other text, a second frame, a truncated, corrupt or multi-line payload refuses. These shapes
come from synthetic examples, not a captured log: if the TV's real lines differ, decoding
refuses, and the regex should be widened only against real captured lines. The SDK's
`[exit:N]` trailer is transport; the caller strips it exactly before calling `decode_frame`.

One write of a large buffer is not atomic on a pipe, so a GC line can still land inside the
frame. The decoder then refuses (`hash-mismatch`, `truncated` or `end-tag-mismatch`). Treat
that as "rerun", not as an inventory result.

## What `--inventory` reads

- `/opt/dbspace/.pkgmgr_parser.db`, through `/usr/bin/sqlite3` started with
  `ProcessStartInfo.ArgumentList` and `UseShellExecute = false`, an empty environment
  (only `PATH=/usr/bin:/bin`, `LANG=C`, `LC_ALL=C`) and the arguments
  `-init /dev/null -readonly -batch -bail -noheader -list -separator |`. Exactly two fixed
  `SELECT` statements run:
  - `package_app_info a JOIN package_info p ON a.package = p.package`, reading `a.app_id,
    app_component, app_exec, app_nodisplay, app_disable, app_root_path, package` and
    `p.root_path, package_disable, package_version`;
  - `package_app_localized_info`, reading `app_id, app_locale, app_label`.

  Every column is returned as `N` (NULL) or `X` plus uppercase hex, so NULL and empty
  never collide and no value can inject a separator or newline.
- `/etc/tizen-release` (at most 4 KiB, printable ASCII lines only).
- `uname -m` from `/usr/bin/uname`, else `/bin/uname`.
- One lstat-style probe of each strict install root. Nothing is enumerated, recursed,
  hashed or followed through a link.

Each child has a timeout (20 s per query, 5 s for uname) and a stdout cap (8 MiB per query,
64 bytes for uname). On timeout or overflow the tool kills **the process it started** and its
descendants, nothing else. Child stderr is never printed: it is mapped to a fixed code
(`table-unsupported`, `column-unsupported`, `database-open-failed`,
`sqlite3-option-unsupported`, `query-failed`), because it can carry device paths or rows.

## Output: `quiettv-inspection/1`

```json
{"schema":"quiettv-inspection/1","toolVersion":"1.0.0","status":"ok",
 "registryDatabase":"/opt/dbspace/.pkgmgr_parser.db",
 "registryRecords":[{"Id":"...","Name":"...","Installed":true,"Disabled":false,"Launchable":true,
   "PackageId":"...","Version":"1.2.3","InstallRoot":"/opt/usr/apps/...","Reason":"installed"}],
 "observedRuntime":{"tizenRelease":{"path":"/etc/tizen-release","lines":["..."]},
   "tizenVersion":"6.5","machine":"armv7l","model":null,"firmware":null,"region":null},
 "problems":[{"code":"model-not-observed","subject":"observedRuntime","fatal":false}]}
```

Real output is one compact line. Records are sorted by `Id` (ordinal), problems by fatal
first, then code and subject, and there are no timestamps. The same registry always gives
the same bytes.

- **Installed** is true only when `p.root_path` is exactly `/opt/usr/apps/<PackageId>` or
  `/usr/apps/<PackageId>` (no trailing slash), `app_root_path` is NULL or the same path, and
  that path is a real directory, not a link. A registry row alone never counts as installed.
  Otherwise `Reason` says why: `root-absent-tombstone`, `symlink-root-refused`,
  `external-root-refused` (for example `/usr/bin/wrt` or an aria-engine directory; these
  shared read-only references are never probed or treated as the package), `root-path-unsafe`,
  `app-root-mismatch`, `root-not-directory`, `root-unreadable` or `registry-root-missing`.
- **Disabled** is `app_disable == "true"` or `package_disable == "true"`.
- **Launchable** is the registry's declaration: `app_component == "uiapp"`, `app_nodisplay ==
  "false"` and a non-empty `app_exec`. It does not include install state, so
  **a usable app is `Installed && !Disabled && Launchable`.** The exec is never run or printed;
  one that is not a safe absolute path adds a non-fatal `exec-unsafe` problem.
- **Name** is the label of the best locale: `en-us` (or `en_US`), then `No Locale` (or
  `NoLocale`), then the ordinally first locale. If that label is missing or unsafe (control,
  format/bidi, private-use or replacement characters, edge whitespace, over 256 chars), Name
  is `null` with a `label-missing` or `label-unsafe` problem. There is no fall back to another
  locale's text.
- **Version** is `null` with a problem when the registry value is missing or unsafe.
- **model, firmware, region** are always `null` with a problem. This tool never derives them
  (from a SKU string or anything else), and it makes no claim about the TV's identity.

## Fatal: no inventory success

These give exit `1`, `status: "failed"` and `registryRecords: null`. Never `[]`, which would
read as "nothing installed".

- A query cannot run, times out, exceeds its cap or exits non-zero. This includes an
  unsupported table or column.
- Output is not the exact encoded shape.
- More than 4096 apps or 65536 label rows.
- No apps at all.
- An app or package id fails the `paths.py` identifier rule.
- A component is malformed.
- A flag is anything but exactly `true` or `false` (including NULL, `TRUE` and `1`).
- A duplicate app id, an app-id or package-id case clash, a duplicate or case-clashing
  (app, locale) label, or a malformed locale.

## Hardware check and remaining limits

The maintainer independently rebuilt the source and ran its 43 memory-backed self-tests
on the supported ARMv7/Tizen 9 TV. A warm `--inventory-framed` run then produced a real
successful inventory that the strict decoder accepted, including the runtime's GC lines.
The physical-root check identified removed payloads as tombstones. The two fixed SQLite
queries and release-file reads worked. Temporary staging was removed afterward; the boot
and existing launcher were unchanged. No install, removal or startup change was performed.

This does not prove the real process timeout/kill failure paths; those are tested offline.
It does not identify model, firmware or region, and it is not a live backup/restore adapter.

### Original assumptions to recheck on another firmware

- **Locale literals.** The literals `en-us` and `No Locale` come from Tizen's usual schema
  and are not yet confirmed against this database. Both spellings are accepted, and every
  preferred literal is in one block (`LocalePreference` in `Inspector.cs`).
- **`-readonly` flag.** The TV's `sqlite3` must support `-readonly`. If it does not, the
  query fails closed with `sqlite3-option-unsupported`, and nothing is retried without it.
- **Database access.** The parent must run the tool as a user that can read the database.

## Build and test

```
$env:QUIETTV_DOTNET = "<path to SDK 6.0.428 dotnet.exe>"
python -m unittest tests.test_tv_inspector -v
```

The tests:

- build offline with the pinned SDK from `global.json`, with caches under the ignored `obj/`;
- run `--self-test`;
- check that two builds (one from a CRLF copy in another folder) give identical bytes, with no
  PDB and no host paths;
- check that `--inventory` and `--inventory-framed` refuse cleanly off Linux and create
  nothing, and that the real framed bytes equal `frame.encode_frame` of the raw document and
  decode strictly, with GC lines around them;
- test `frame.py`: round trips, GC noise, and each refusal (multiple frames, truncation, length,
  hash, end tag, headers, unknown noise, `[exit:N]`, caps, JSON and schema violations).

The guard-off tests remove one protection at a time in a copy under the ignored `bin/`, and
require the named self-test check to fail. The protections covered are:

- duplicates;
- case clashes;
- exact flags;
- row keys;
- row limit;
- empty registry;
- symlink, tombstone, external and traversal roots;
- unsafe labels;
- regex anchoring;
- stderr echo;
- the frame's one-line payload, size cap and oversize fail-closed.

The decoder has its own guard-off tests. Each one removes a single guard in an in-memory copy of
`frame.py` and shows that an input the real decoder refuses is then accepted. The guards covered
are the hash, end tag, noise check, both GC regex anchors, noise count, header anchor, one-line
payload, duplicate keys, the schema call, status consistency, install root, record order,
unguessed runtime fields, NaN and floats.

The harness's own cleanup is guarded too. Every recursive delete goes through one check, which
accepts only the generated folders `obj`, `bin/<name>`, `bin/<name>/obj` and
`bin/<name>/bin/<name>`, and refuses:

- traversal (`..`);
- any junction, symlink or reparse point on the leaf or any ancestor up to the repository,
  checked with `lstat` before anything is resolved;
- a resolved path that differs from the lexical one;
- any link inside the target.

The tool folder, its `bin/` and the sources are never targets. Tests with sentinels outside the
repository cover each refusal. Guard-off copies of the harness show that each guard is what
stops the delete; in those copies deletion is recorded, not performed. `.gitignore` here keeps
`bin/` and `obj/` out of git even when the folder is exported on its own.

The tests never publish, deploy, register, apply, remove, reboot or sign anything.
