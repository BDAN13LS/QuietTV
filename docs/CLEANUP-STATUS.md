# Cleanup status on the test TV

Status record for one TV: Samsung UN43U8000FBXZA, Tizen 9, ARMv7, firmware
1300.8, launcher 2.0.18. It describes what was done there, not what a public
tool does today. No consumer installer or live public restore writer exists
yet, and nothing here is a promise for other models or firmware.

## What "removed" means here

- The app's **files on the TV's storage were physically deleted**, including
  quarantine copies.
- This is **not** a native package uninstall. The disabled registry entries
  (tombstones) stay behind.
- Owner backups are kept **off the TV**. Restore rehearsals were run on
  scratch copies and checked bytes, owner IDs, permissions and extended
  attributes. That is not a public live-restore guarantee.
- Sizes below are **storage**, never RAM.

## Removed: 27 payload trees

The removed components cover Samsung analytics, targeted advertising,
AI Companion and suggestions, Bixby and voice-assistant features, and
automatic content recognition (ACR, identifying what is being watched).
Guide and the standalone TV Plus app were also removed. This does not prove
that every telemetry path in the remaining operating system is gone.

| Batch | Count | Notes |
|---|---|---|
| Analytics | 1 | `com.samsung.tizen.samsung-analytics` |
| First batch | 23 | listed below |
| Later batch | 3 | Guide, Security Center information screen, TV Plus |

First batch (23):
`com.samsung.tv.AICompanionAgent`, `com.samsung.tv.AICompanionUI`,
`com.samsung.tv.AICompanionUILauncher`, `com.samsung.tv.AICompanionUINotice`,
`com.samsung.tv.acr-service-app`, `com.samsung.tv.ai-discovery-ui`,
`com.samsung.tv.bixby-context-client`, `com.samsung.tv.bixby-prov`,
`com.samsung.tv.bixbycapsuleviewer`, `com.samsung.tv.canalysis-service`,
`com.samsung.tv.contents-recognition-dbmgr`,
`com.samsung.tv.context-aware-agent`, `com.samsung.tv.context-aware-service`,
`com.samsung.tv.mybixby`, `com.samsung.tv.ondevice-voice`,
`com.samsung.tv.pisa-service`, `com.samsung.tv.targeted-ad-service`,
`com.samsung.tv.utterance-recommender-service`, `com.samsung.tv.vif`,
`com.samsung.tv.voice-instant-app`, `com.samsung.tv.voiceassistant-music`,
`com.samsung.tv.wakeup-engine-bixby`, `org.tizen.voice-app`.

Later batch (3):

- `25emanual1`, the Guide.
- `com.samsung.tv.SecurityCenter`, **information screen only**; the core
  security components were kept.
- `HEPsqFNie0`, the standalone TV Plus app.

## Storage freed (excluding analytics)

| Batch | Storage |
|---|---|
| First batch | 218,464 KiB |
| Later batch | 37,096 KiB |
| Total | 255,560 KiB, about 249.6 MiB |

This is disk storage. It is **not RAM**. There is no valid before/after RAM
baseline, so no memory saving is claimed, and no further safe RAM-removal
candidate has been established.

## Memory (RAM)

A read-only snapshot of the owner's TV on 27 October 2026 showed
MemAvailable 504,268 KiB (about 492 MiB) of MemTotal 1,612,368 KiB (about
1,575 MiB). That is one momentary available-memory reading. It is **not** a
before/after saving. Several streaming-app processes remain resident, so app
lifecycle should be evaluated before any more deletion. No further safe
removal candidate is confirmed.

## Blocked, not removed

- **11 services** were blocked and were inactive after the previous cold boot.
- Components in read-only ROM, including the read-only contents-recognition
  daemon, are **blocked, not removed**.

## Held (whole package kept)

Kept pending proof about updates and dependencies:

- `com.samsung.tv.ai-discovery-service`, whose payload tree also contains the
  enabled `com.samsung.tv.download-content` component.
- `org.tizen.voice-client`, whose payload tree also contains the enabled
  `org.tizen.stt-client` component.

## Deliberately kept

- Browser, by owner preference.
- Settings, sources, core security, network, DRM, and the Store and update
  dependencies.

## What this does not show

- Not all telemetry is proven gone. Streaming apps are separate and untouched.
- Store app-update installation, DRM playback and physical HDMI video are
  unverified; IPTV picture is black; the cold-boot black gap is unsolved.

See the [README](../README.md) for overall status.
