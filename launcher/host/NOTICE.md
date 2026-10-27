# Notices for the public native host

`RemoteGate.cs` adapts the EVIOCGRAB/uinput forwarding path from
`payloads/common/SamsungTvRemoteInputAgent.cs` in
[chris-ritsen/samsung-tv-root](https://github.com/chris-ritsen/samsung-tv-root),
pinned at commit `4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b`, released under the
[Unlicense](https://github.com/chris-ritsen/samsung-tv-root/blob/4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b/LICENSE).
The same attribution is recorded in the repository's `THIRD-PARTY-NOTICES.md`.

The adaptation consumes only the configured Home key and forwards every other key
event. It is not a kernel patch, firmware change or persistent input rule, and in
this host it runs only when the owner's config sets `remoteGrabEnabled: true`.

This notice does not choose a licence for QuietTV itself.
