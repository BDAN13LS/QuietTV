# Package capture reader

This is a **package archive reader**, not an installer or a complete backup.
It writes archive records to stdout and fixed refusal codes to stderr; it creates
no archive or manifest files on the TV. It does not change the launcher, package
registry, signing, startup, authentication or firmware. Build/staging of its own
DLL is a separate owner-controlled operation.

Build with .NET SDK 6.0.428; run the framework-dependent DLL with the TV's .NET6:

```
dotnet QuietTVCapture.dll --self-test
dotnet QuietTVCapture.dll --package PACKAGE_ID opt MAX_BYTES
```

The only root selectors are `opt` (`/opt/usr/apps`) and `rom` (`/usr/apps`). The
same protected-ID tokens as the supported profile apply. It accepts no path,
command, SQL or URL. GNU tar is required. Linux libc must provide `statx`; missing
support refuses rather than guessing an ARM-specific `stat` layout.

This initial slice accepts directories, regular files and symlink records. It
never follows link targets or recurses through them. **Special files, nested
mounts and more than 15,000 entries refuse.** Package roots and ancestors must
be real directories. The capsule validator must still refuse unsafe links and
require explicit policy for external links before restoration. GNU tar
preserves numeric owners and modes. Regular files with multiple hard links
refuse: this TV's older tar has no `--hard-dereference`, and this reader does
not emit potentially unsupported hard-link archive entries. Access permissions
must allow every requested file to be read.
Full trusted/security xattrs are **not included by this slice**.

The producer checks metadata/inodes before and after archiving and refuses
detected changes. It is **not an atomic filesystem snapshot**. Keep app updates
and concurrent filesystem changes stopped during capture. A hostile concurrent
writer, path exchange between checks, or change restored between checks is
outside this guarantee. Do not use this output alone to authorize cleanup.

The child is bounded to 90 seconds and 4 GiB, with fixed argv and controlled
environment; only its own process is terminated on failure. The caller must also
bound transport reads and terminate this reader if the receiver stops draining
stdout (a blocked OS pipe write cannot be given a reliable managed deadline).

## Transfer

ASCII newline records:

```
QUIETTV-CAPTURE/1 BEGIN package PACKAGE_ID
QUIETTV-CAPTURE/1 DATA SEQUENCE BASE64 CHUNK_SHA256
QUIETTV-CAPTURE/1 END CHUNK_COUNT BYTE_COUNT ARCHIVE_SHA256
```

Sequence starts at zero; each decoded chunk is at most 32,768 bytes. END is
written only after GNU tar exits successfully and the final snapshot agrees.
Hashes are lowercase SHA256, calculated by the TV on the exact archive bytes.
The runtime can emit GC diagnostics on stdout. Only the two previously recorded
GC line formats are accepted; a diagnostic inserted inside a record refuses.

`tvkit.package_stream.decode_package_stream` validates these records, spools
outside Git on owner PC storage, then returns `capture.CapturedStream`. It calls
the supplied zero-argument exit-status provider after EOF. The transport must
use bounded line reads (`readline(MAX_LINE + 1)`), an overall timeout, and pass
only stdout (not SDK exit trailers). It must not return a success status before
the reader actually exits. This module provides **no live/root transport**.

Close `CapturedStream.chunks` when abandoning a transfer. Normal exhaustion or
close deletes its own spool; inode checks preserve an already replaced path.
Owner storage must be private and must not be concurrently replaced by other
writers; these checks are not an atomic deletion primitive against a hostile
same-account process. Read-only transport does not authenticate the device;
the capture engine still requires verified device facts and fingerprint.

Full xattr capture, observed firmware facts, a reviewed privileged transport and
independent Rescue restore remain required before consumer cleanup is enabled.
