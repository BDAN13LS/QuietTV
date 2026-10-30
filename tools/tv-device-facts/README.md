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
Plus `http://tizen.org/privilege/filesystem.read` and `http://tizen.org/privilege/filesystem.write`
for the one private-file write: the widget resolves its **own** `wgt-private` store in read-write
mode and resolves the leaf file before (re)writing it, which needs the read privilege as well as
write. These privileges reach only the app's own private storage; no other path is accessed.

The Samsung Web API loader `<script src="$WEBAPIS/webapis/webapis.js">` is included in `index.html`
before `collector.js`, per the official Samsung reference
(https://developer.samsung.com/smarttv/develop/api-references/samsung-product-api-references.html).
`$WEBAPIS` is resolved by the Tizen runtime to the on-device SDK path; it is not an external URL and
opens no network.

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
model, firmware or profile value.

A value counts as `observed` only when it is a string of 1–4096 characters that is not
whitespace-only, carries no control characters (`< 0x20` or `0x7f`), and is not the literal
placeholder `Unknown` (case-insensitive, the value the network API returns). Anything else is
recorded with a finite code — `api-unavailable`, `threw`, `not-a-string`, `empty`, `too-long`,
`unsafe` or `placeholder` — and the overall result is `incomplete`. Accepted values keep their exact
bytes; these checks only reject, they never trim or rewrite.

On screen the widget shows statuses only, then auto-exits after a short bounded delay that is
scheduled **once at bootstrap, before collection**, so a filesystem or WebCrypto callback that never
resolves still cannot leave the probe in the foreground. There is no console logging of raw values
or the DUID, and no service or background registration.

## Device identity (hashed, never raw)

The DUID is hashed on the device with WebCrypto:

- scheme: `quiettv-device-facts-duid/sha256/1`
- digest: `SHA-256( "quiettv-device-facts-duid/1\n" + DUID )`, lowercase hex

Only `{scheme, digest, status}` is stored. The DUID is bounds- and placeholder-checked like any
field (`duid-empty`, `duid-too-long`, `duid-unsafe`, `duid-placeholder`, `duid-unavailable`,
`duid-threw`) before hashing, and its raw value is never placed in the record even on a rejection.
If WebCrypto is unavailable the status is `crypto-unavailable` and the result is `incomplete` — there
is **no** raw fallback. A `subtle.digest` that throws (synchronously or asynchronously) is coded
`hash-threw`, and a result that is not exactly a 32-byte SHA-256 digest is coded `hash-invalid`; in
both cases no digest is emitted. The raw DUID is never written to the result, the on-screen summary,
a log line or an error.

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

The on-screen summary carries statuses only (no values, no digest); the full result stays in the
private file for SDK retrieval.

## Files

| file | what it is |
|---|---|
| `collector.js` | the probe logic; injectable deps, Node- and widget-compatible; the only API-touching code |
| `index.html` | minimal UI that loads the Samsung `webapis.js`, wires the real globals, writes the private file, and schedules the bounded exit |
| `config.xml.template` | unsigned Tizen config; the only privileges are `filesystem.read`, `filesystem.write` and `productinfo` |
| `pack.py` | deterministic stdlib packager (`.wgt`); no signing, no install; refuses an output inside any repo |
| `collector.test.js` | pure-Node tests (fake webapis): allowlist, hash, errors, placeholder/bounds, incomplete, no raw DUID, fake filesystem write |
| `index.test.js` | pure-Node tests for the bootstrap: loader order, productinfo wiring, bounded exit scheduled before collection |
| `test_pack.py` | packager tests: determinism, config contents, bad/mismatched ids, overwrite, outside-repo refusals |

## Build and test

```
node --test tools/tv-device-facts/collector.test.js tools/tv-device-facts/index.test.js
python -m unittest test_pack            # from this folder
# package (owner ids; OUTPUT MUST BE OUTSIDE EVERY REPO; parent signs/installs separately):
python pack.py --app-id <10 alnum>.DeviceFacts --package <10 alnum> --out <path outside repo>.wgt
```

## Allowlist paths the parent must add

For a live run the parent (who holds credentials and the signing identity) must allow:

- the widget's own private store at `wgt-private/device-facts.json` (covered by the
  `filesystem.read` and `filesystem.write` privileges in the config template; read is needed for the
  read-write resolve of the app's own store);
- the `productinfo` privilege (already in the template);
- the on-device `$WEBAPIS/webapis/webapis.js` loader path (standard Samsung SDK path; no network);
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
