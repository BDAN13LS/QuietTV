# QuietTV device-facts probe (isolated diagnostic widget)

A small, **unsigned, isolated** Samsung Tizen web widget that reads the TV's own device facts
through the **official Samsung Product API** and records them to the app's private store for the
owner to retrieve with the Tizen SDK. It is the first bounded slice toward an observed
model/firmware/region/identity collector.

It is **not** the read-only inventory tool (`tools/tv-inspect`) and does **not** change it, its
`frame.py` decoder, or the `quiettv-inspection/1` contract. It is also **not** a pure no-write tool:
it writes **one** file, its own `wgt-private/device-facts.json`. Nothing else is written, and no TV,
network, certificate or private file is touched by anything in this folder.

The parent alone signs, installs, runs and removes the widget and retrieves the result. This slice
ships only the sources, an unsigned config template, and a deterministic packager — no signing and
no installation.

## Official source

Samsung Product API (`webapis.productinfo`):
https://developer.samsung.com/smarttv/develop/api-references/samsung-product-api-references/productinfo-api.html

Privilege required: `http://developer.samsung.com/privilege/productinfo` (public).
Plus `http://tizen.org/privilege/filesystem.write` for the one private-file write.

## What it reads (exact, finite allowlist)

`collector.js` calls only these, each wrapped so any failure becomes a finite coded status. There is
no `set*` call, no listener, no network URL, and no startup/service/Home registration.

| field | API call | notes |
|---|---|---|
| `firmware` | `webapis.productinfo.getFirmware()` | actual firmware; **not** split, **not** equated with the OS BUILD_ID |
| `realModel` | `webapis.productinfo.getRealModel()` | full model |
| `model` | `webapis.productinfo.getModel()` | distinct meaning from realModel/modelCode |
| `modelCode` | `webapis.productinfo.getModelCode()` | distinct meaning |
| `serviceCountry` | `webapis.productinfo.getSystemConfig(ProductInfoConfigKey.CONFIG_KEY_SERVICE_COUNTRY)` | service country; **never** inferred from a model suffix |
| `localSet` | `webapis.productinfo.getLocalSet()` | opaque; captured verbatim, **not** required for "complete", never interpreted |
| `platformVersion` | `tizen.systeminfo.getCapability('http://tizen.org/feature/platform.version')` | Tizen platform version |
| identity | `webapis.productinfo.getDuid()` | hashed on-device (see below); the **raw DUID is never emitted** |

Values are stored **exactly** as the API returns them — no trim, normalisation or splitting (for
example `uname`'s `armv7l` is not rewritten to `armv7`). No field is ever filled from an expected
model, firmware or profile value. A field that is missing, throws, returns a non-string or returns
empty is recorded with that coded status and the overall result is `incomplete`.

## Device identity (hashed, never raw)

The DUID is hashed on the device with WebCrypto:

- scheme: `quiettv-device-facts-duid/sha256/1`
- digest: `SHA-256( "quiettv-device-facts-duid/1\n" + DUID )`, lowercase hex

Only `{scheme, digest, status}` is stored. If WebCrypto is unavailable the identity status is
`crypto-unavailable` and the result is `incomplete` — there is **no** raw fallback. The raw DUID is
never written to the result, the on-screen summary, a log line or an error.

This scheme is **deliberately distinct** from the existing profile/capsule identity scheme
(`tvkit-device-id/1` in `public-toolkit/tvkit/profile.py`). This slice introduces no silent
identity-compatibility change: joining a facts digest to a profile identity is left to a separately
reviewed adapter.

Per the official FAQ
(https://developer.samsung.com/smarttv/develop/faq/device-information-and-firmware.html, Q10),
a **factory reset changes the DUID** (a SmartHub reset does not). No claim is made here that the
identity is stable across factory reset or across untested firmware updates.

## Output artifact `quiettv-device-facts/1`

Written only to `wgt-private/device-facts.json`:

```json
{
  "schema": "quiettv-device-facts/1",
  "status": "complete | incomplete",
  "source": "samsung-productinfo-webapi",
  "apiReference": "https://developer.samsung.com/.../productinfo-api.html",
  "fields": { "firmware": {"api":"...","value":"...","status":"observed"}, "...": {} },
  "identity": { "scheme": "quiettv-device-facts-duid/sha256/1", "digest": "<hex>", "status": "observed" }
}
```

On screen the widget shows statuses only (no values, no digest), then auto-exits after a short
bounded delay, leaving the private file for SDK retrieval. There is no console logging of raw values
or the DUID.

## Files

| file | what it is |
|---|---|
| `collector.js` | the probe logic; injectable deps, Node- and widget-compatible; the only API-touching code |
| `index.html` | minimal UI that wires the real Tizen/Samsung globals and the private-file write |
| `config.xml.template` | unsigned Tizen config; the only privileges are `filesystem.write` and `productinfo` |
| `pack.py` | deterministic stdlib packager (`.wgt`); no signing, no install; refuses an output inside any repo |
| `collector.test.js` | pure-Node tests (fake webapis): allowlist, hash, errors, incomplete, no raw DUID, fake filesystem write |
| `test_pack.py` | packager tests: determinism, config contents, bad ids, overwrite, outside-repo refusals |

## Build and test

```
node --test tools/tv-device-facts/collector.test.js
python -m unittest test_pack            # from this folder
# package (owner ids; OUTPUT MUST BE OUTSIDE EVERY REPO; parent signs/installs separately):
python pack.py --app-id <10 alnum>.DeviceFacts --package <10 alnum> --out <path outside repo>.wgt
```

## Allowlist paths the parent must add

For a live run the parent (who holds credentials and the signing identity) must allow:

- the widget's own private store write `wgt-private/device-facts.json` (covered by the
  `filesystem.write` privilege in the config template);
- the `productinfo` privilege (already in the template);
- the packager output path **outside every git checkout/worktree** (the packager enforces this).

No new allowlist entry is needed for `tools/tv-inspect` or the capture engine; they are untouched.

## Unresolved (await live result before the general adapter)

- `/etc/info.ini` is generic (`Model=Tizen9/TV`, `Build=latest`) and unusable for SKU/firmware;
  `/etc/tizen-release` gives the OS version and a BUILD_ID that is **not** the Samsung firmware
  revision. The network API returned `firmwareVersion` literally `Unknown`. So the Product API is
  the authoritative route, and its real values are not yet observed from this TV.
- Whether `getFirmware()` needs splitting into firmware + firmwareBuild is **deferred** until the
  parent sees the real returned format.
- No production `tvkit-profile/2` exists yet; no field here enables profile matching or install.
