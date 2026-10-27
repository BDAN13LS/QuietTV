# XattrBackup

Standalone .NET 6 helper that dumps and restores Linux extended attributes (xattrs) for one directory tree. It exists because the owner's TV `tar` preserves program bytes, UID/GID and mode, but was compiled without xattr support. The TV package must not be physically deleted until this helper has dumped the attributes and a restore has been verified on the parent Linux integration.

The helper does not access the network, devices, credentials, `private/`, or backups. It reads file bytes below `ROOT` to hash them, reads and writes extended attributes below `ROOT`, and writes only the manifest path the caller provides outside `ROOT`.

## Platform

- `--dump` and `--restore` use the Linux syscalls `llistxattr`, `lgetxattr`, `lsetxattr`, and `lremovexattr` with no-follow semantics (`l*`). They run only on Linux.
- The Linux CLI requires effective UID 0 and `CAP_SYS_ADMIN` (capability bit 21) by reading `/proc/self/status`. Without them the kernel hides `trusted.*` attributes from `llistxattr`, which would produce an incomplete manifest. The guard runs before path validation, scanning, or output creation; failure exits with code 2.
- On Windows (and any other non-Linux platform) `--dump` and `--restore` exit with code 2 and a clear error before validating paths, creating files, or reading attributes.
- The build output is a portable managed `net6.0` DLL (AnyCPU, no apphost, no RID-specific native code). It runs on an existing Linux ARMv7 .NET 6 runtime with `dotnet XattrBackup.dll`.
- No NuGet packages are used.

## Build

From the repository root:

```sh
dotnet build tools/xattr-backup/XattrBackup.csproj -c Release -o build/xattr-backup
```

Use an installed .NET 6 SDK (6.0.x). Build output belongs in `build/xattr-backup/` and is not committed.

## Usage

```sh
dotnet XattrBackup.dll --dump ROOT MANIFEST
dotnet XattrBackup.dll --restore ROOT MANIFEST
dotnet XattrBackup.dll --self-test
```

- `ROOT` must exist, must be a directory, must not be a symbolic link, and must not be a filesystem root.
- `MANIFEST` for `--dump` must be outside `ROOT`. Its parent directory must already exist.
- Manifests are written with an atomic temporary file plus rename. On Linux the temporary file is restricted to mode 0600 immediately after creation and before any attribute bytes are written.
- Standard output contains only counts (`entries`, `xattrs`, `set`, `removed`, `unchanged`, `manifest_bytes`). Attribute names and values are never printed.

Exit codes: `0` success, `1` validation or operation failure, `2` usage, unsupported platform, or missing required privileges.

## Manifest format (version 1)

```json
{
  "version": 1,
  "rootBasename": "com.example.app",
  "entries": [
    {
      "path": ".",
      "kind": "directory",
      "xattrs": { "user.example": "<base64>" }
    },
    {
      "path": "bin/program",
      "kind": "file",
      "xattrs": {},
      "sha256": "<64 lowercase hex characters>"
    },
    {
      "path": "current",
      "kind": "symlink",
      "xattrs": {},
      "linkTarget": "releases/1.0"
    }
  ]
}
```

- Paths are relative to `ROOT`, use `/`, and include the root entry `"."`.
- `kind` is exactly `file`, `directory`, or `symlink`. Directory symlinks are recorded but never descended.
- `xattrs` maps each attribute name to the exact value encoded as base64.
- `sha256` is the SHA-256 of the file bytes. It is present only for files.
- `linkTarget` is the exact link target. It is present only for symlinks.
- `rootBasename` is the basename of `ROOT` and must match the restore target; this guards against restoring a manifest into the wrong package.

Manifest input is rejected if it contains unknown or duplicate JSON properties, duplicate entry paths, duplicate attribute names, invalid base64, absolute paths, drive/UNC paths, backslashes, empty or `.` components (except the root entry), `..`, NUL, control characters, or a missing/non-directory parent entry.

## Limits

- At most 15,000 entries.
- At most 1 MiB of attribute names plus values per entry.
- At most 32 MiB per manifest.
- Unreadable files, directories, links, or attributes are errors; nothing is silently skipped.

## Restore semantics

`--restore` performs a full preflight before changing any attribute:

1. Parse and validate the entire manifest, including bounds and path rules.
2. Validate `ROOT` (directory, not a symlink, not a filesystem root) and require the manifest `rootBasename` to match it.
3. Scan the existing tree and require exactly the same entry set and kinds, the same symlink targets, and the same file SHA-256 values.
4. Read the current attributes for every entry and compute the exact change set.

Only after all of that succeeds does it apply changes. For each entry it sets missing or different manifest attributes in ordinal order, re-reads the entry, and only then removes attributes that are not in the manifest. This ordering matters because the kernel can create or update attributes as a side effect of a set (on Tizen, setting `security.SMACK64` creates or updates `security.SMACK64PROCNAME`). After the first apply it re-checks every entry and repeats that reconcile step for at most four passes, because a side effect can also change an attribute that previously matched. It fails without further writes if the tree is still not exact, and it never ignores an extra attribute. It never creates or removes files or directories, never changes mode or ownership, never follows links while setting attributes, and never executes restored content.

Restore is idempotent and can be re-run after a partial failure. Preflight assumes the caller controls the tree and that it is not being concurrently mutated; concurrent hostile mutation is outside the current integration.

## Intended environment and caller preflight

This helper is intended for use only on the TV, or on a host with the same Linux Security Module (LSM) policy as the TV. Root plus `CAP_SYS_ADMIN` is necessary for `trusted.*` visibility but not sufficient for every namespace: the caller must also grant the appropriate permissions for the target attributes, for example MAC admin permission for `security.*`/Smack labels and `CAP_SETFCAP` for `security.capability`. Restoring POSIX ACL attributes can also change group mode bits; extraction order and the parent's mode checks must account for that.

The scanner currently recognizes only `file`, `directory`, and `symlink` semantics and will attempt to hash anything else that `System.IO` reports as a file, including FIFO or device nodes. It does not automatically reject special or hardlink entries. The caller MUST run a fail-closed `find` preflight over the protected 0700 scratch tree before dump or restore and abort on any entry that is not a regular file, directory, or symbolic link. Do not rely on this helper to detect special files.

The parent's independent inventory of the intended trees at the time of this work listed 2,047 directories, 4,815 regular files, 44 symlinks, and no special or hardlink entries; the analytics target contained 9 regular-file and directory entries. That inventory supports the intended run but does not replace the caller's fail-closed preflight.

Required privilege, the fail-closed tree preflight, and the parent's independent hash/label cross-check are all required before a dump may be used as evidence for deletion.

## Required integration order

1. Extract the tar archive first so file bytes, ownership, and modes are in place.
2. Verify the extracted file bytes against `sha256` in the manifest.
3. Run `--restore ROOT MANIFEST` to apply xattrs. Do not apply attributes before ownership and mode extraction.
4. Independently check the original hashes and labels on the TV before treating the package as restorable.
5. Keep the original package files until the parent Linux integration check passes. Do not physically delete the original app on the basis of this helper alone.

## Privacy

A manifest can contain private attribute names and values. Keep it outside the repository, transfer it over the existing trusted channel, and delete copies when they are no longer needed. Never commit a manifest.

## Self-test

`--self-test` runs on Windows and uses only managed file APIs plus an in-memory attribute store, so it makes no native Linux calls. It covers hostile and duplicate paths, base64 and entry/manifest bounds, privilege-status parsing, binary JSON roundtrip, exact restore synchronization and write counts, count-preserving rename rejection with zero attribute writes, whole-preflight rejection on content, kind, link, membership, and root mismatches, root guards, atomic write cleanup, and the non-Linux CLI refusal.

The self-test does not test native xattr syscalls. Actual `--dump` and `--restore` behavior must be validated on Linux against the TV tree by the parent integration step.
