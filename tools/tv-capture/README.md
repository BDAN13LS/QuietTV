# Capture reader

This is a bounded **package archive reader** and **xattr manifest reader**, not an
installer or a complete backup. It writes capture records to stdout and fixed
refusal codes to stderr; it creates no archive, manifest or temporary file on the
TV. It does not change the launcher, package registry, signing, startup,
authentication or firmware. Build/staging of its own DLL is a separate
owner-controlled operation.

Build with .NET SDK 6.0.428; run the framework-dependent DLL with the TV's .NET6:

```
dotnet QuietTVCapture.dll --self-test
dotnet QuietTVCapture.dll --package PACKAGE_ID opt MAX_BYTES
dotnet QuietTVCapture.dll --xattrs PACKAGE_ID opt MAX_BYTES
```

The only root selectors are `opt` (`/opt/usr/apps`) and `rom` (`/usr/apps`). The
examples use literal `opt`; use literal `rom` in the same position to select
`/usr/apps`. Do not write `opt|rom` in a command: a shell would treat the `|` as
a pipe. The same protected-ID tokens and strict identifier rules apply to both
kinds. It accepts no path, command, SQL, URL or executable. `--package` keeps its
4 GiB declared cap. `--xattrs` has a 32 MiB declared cap.

## Package capture

GNU tar is required. Linux libc must provide `statx`; missing support refuses
rather than guessing an ARM-specific `stat` layout. This reader accepts
directories, regular files and symlink records. It never follows link targets or
recurses through them. **Special files, nested mounts and more than 15,000
entries refuse.** Package roots and ancestors must be real directories. GNU tar
preserves numeric owners and modes. Regular files with multiple hard links
refuse: this TV's older tar has no `--hard-dereference`, and this reader does not
emit potentially unsupported hard-link archive entries. Access permissions must
allow every requested file to be read. The package path does not include
extended attributes.

The archive is rooted at the package directory: tar receives
`--directory=<parent>/<PACKAGE_ID>` and the final member argument is `.`. The
first archive member is therefore `.`, followed by package-relative paths. The
package basename remains in the protocol `BEGIN package PACKAGE_ID` record and in
the xattr manifest `rootBasename`; it is not prefixed onto archive member paths.

The producer checks metadata/inodes before and after archiving and refuses
detected changes. It is **not an atomic filesystem snapshot**. Keep app updates
and concurrent filesystem changes stopped during capture. A hostile concurrent
writer, path exchange between checks, or change restored between checks is
outside this guarantee. Do not use this output alone to authorize cleanup.

The child is bounded to 90 seconds and 4 GiB, with fixed argv and controlled
environment; only its own process is terminated on failure. The caller must also
bound transport reads and terminate this reader if the receiver stops draining
stdout (a blocked OS pipe write cannot be given a reliable managed deadline).

## Xattr capture

The xattr path reuses the existing version 1 manifest implementation in the
referenced helper; it does not carry a second scanner. `QuietTVCapture.csproj`
references `../xattr-backup/XattrBackup.csproj` (`XattrBackup.dll`) and reaches
only the internal `Dumper`, `LinuxPrivilegeStatus` and `LinuxXattrStore` APIs
through `InternalsVisibleTo`.

That legacy helper still contains and ships its existing restore/write API
(`lsetxattr`, `lremovexattr`) and atomic manifest-file writer. This reader neither
calls nor exposes those paths, but the referenced DLL is **not a read-only
assembly**; review both components together. The reference adds no CLI to the
legacy helper and changes no restore path.

On Linux the xattr path first requires effective UID 0 plus `CAP_SYS_ADMIN`
(capability bit 21), read from `/proc/self/status`. Without that gate trusted
attributes can be hidden or changed, so it refuses before inspecting the root.
It then performs the same `statx` no-follow ancestor/root checks and before/after
snapshots as the package path. The capture path refuses special files, nested
mounts and multiply linked regular files. The legacy helper's own scanner does
not detect all of these, so do not use the helper DLL directly as a substitute
for this guarded capture path.

`Dumper.DumpToStream(ROOT, framingStream, LinuxXattrStore.Instance)` writes the
validated version 1 JSON manifest into a write-only adapter. The adapter splits
writes into protocol chunks of at most 32,768 bytes, counts actual bytes and
sequence numbers, and hashes exactly the emitted manifest bytes. It rejects a
declared-cap overflow before writing the overflowing chunk. It never buffers the
full manifest again and never closes stdout. `END` is written only after the
helper succeeds, its reported manifest byte count equals the bytes actually
framed, and the source snapshot still matches. Any failure writes a fixed
diagnostic and exits nonzero with no `END`. The capture writes no manifest,
temporary or archive file on the TV.

The helper itself buffers metadata, DTOs, base64 values and the serialized JSON.
The 32 MiB bound applies to raw attribute-name/value accumulation and to the
accepted serialized manifest output; it is **not a total or peak RAM ceiling**,
and JSON emission is not incremental. The Linux store lists all attribute names
without filtering and does not intentionally omit `trusted.*` or `security.*`. Kernel/LSM
policy can still hide names, so an absent name is not proof that it does not
exist. Attribute names and values may be sensitive; keep received manifests
outside Git.

## Transfer

ASCII newline records:

```
QUIETTV-CAPTURE/1 BEGIN package PACKAGE_ID
QUIETTV-CAPTURE/1 DATA SEQUENCE BASE64 CHUNK_SHA256
QUIETTV-CAPTURE/1 END CHUNK_COUNT BYTE_COUNT CONTENT_SHA256
```

```
QUIETTV-CAPTURE/1 BEGIN xattrs PACKAGE_ID
QUIETTV-CAPTURE/1 DATA SEQUENCE BASE64 CHUNK_SHA256
QUIETTV-CAPTURE/1 END CHUNK_COUNT BYTE_COUNT MANIFEST_SHA256
```

Sequence starts at zero; each decoded chunk is at most 32,768 bytes. For
`--package`, the payload is the GNU tar archive stream and the hashes cover the
exact archive bytes. For `--xattrs`, the payload is the exact version 1 JSON
manifest bytes and the hashes cover those bytes. Hashes are lowercase SHA256,
calculated by the TV. The runtime can emit GC diagnostics on stdout. Only the
two previously recorded GC line formats are accepted; a diagnostic inserted
inside a record refuses.

`tvkit.package_stream.decode_package_stream` validates the package records,
spools outside Git on owner PC storage, then returns `capture.CapturedStream`. It
calls the supplied zero-argument exit-status provider after EOF. The transport
must use bounded line reads (`readline(MAX_LINE + 1)`), an overall timeout, and
pass only stdout (not SDK exit trailers). It must not return a success status
before the reader actually exits. This module provides **no live/root transport**.
The parent integration supplies xattr decoder handling independently; this
reader only frames the manifest bytes.

Close `CapturedStream.chunks` when abandoning a transfer. Normal exhaustion or
close deletes its own spool; inode checks preserve an already replaced path.
Owner storage must be private and must not be concurrently replaced by other
writers; these checks are not an atomic deletion primitive against a hostile
same-account process. Read-only transport does not authenticate the device; the
capture engine still requires verified device facts and fingerprint.

`--self-test` runs pure, in-memory checks on Windows: strict parsing and both
caps, xattr path/selector/protected-ID rejection, privilege-status parsing
without native calls, and chunk/hash/count framing with overflow and
underlying-stream-open checks. It does not test native xattr syscalls or Linux
`statx`. Actual privileged capture must be verified on the TV by the parent
integration.

Full xattr restore, observed firmware facts, a reviewed privileged transport and
independent Rescue restore remain required before consumer cleanup is enabled.
This tool does not provide transport, device facts, installer, cleanup or
restore.
