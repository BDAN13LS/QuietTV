// QuietTV device-facts probe collector (quiettv-device-facts/1).
//
// Runs inside an ISOLATED, parent-signed Samsung Tizen diagnostic widget. It reads device
// facts ONLY through the official Samsung Product API
// (https://developer.samsung.com/smarttv/develop/api-references/samsung-product-api-references/productinfo-api.html)
// and the Tizen platform-version capability, hashes the owner DUID on the device with WebCrypto,
// and returns a typed result. It never invents a native source, never calls any set* method, never
// registers a listener, and never opens a network connection.
//
// Every dependency (the webapis.productinfo object, the ProductInfoConfigKey enum, tizen.systeminfo,
// the WebCrypto subtle object, the UTF-8 encoder, the private-file writer, the on-screen summary
// sink and the auto-exit scheduler) is INJECTED, so this module is exercised in Node against fake
// webapis with no device present. The widget bootstrap in index.html wires the real globals.
//
// Raw values are returned EXACTLY as the API gives them: no trim, no normalisation, no splitting of
// the firmware string, and no equating of the OS BUILD_ID with the Samsung firmware revision. The
// raw DUID is NEVER placed in the result, the summary, a log line or an error; only its hash is.

(function () {
  "use strict";

  var SCHEMA = "quiettv-device-facts/1";
  var SOURCE = "samsung-productinfo-webapi";
  var API_REFERENCE =
    "https://developer.samsung.com/smarttv/develop/api-references/samsung-product-api-references/productinfo-api.html";

  // Device-identity scheme for this capture envelope. It is deliberately DISTINCT from the
  // existing profile/capsule scheme (profile.py DEVICE_ID_DOMAIN "tvkit-device-id/1"); joining a
  // facts digest to a profile identity is left to a separately reviewed adapter, so no silent
  // identity-compatibility change is introduced here.
  var IDENTITY_SCHEME = "quiettv-device-facts-duid/sha256/1";
  var IDENTITY_DOMAIN = "quiettv-device-facts-duid/1\n"; // domain prefix, hashed before the DUID

  var PLATFORM_CAPABILITY = "http://tizen.org/feature/platform.version";
  var SERVICE_COUNTRY_KEY = "CONFIG_KEY_SERVICE_COUNTRY";
  var PRIVATE_OUTPUT = "wgt-private/device-facts.json";

  // Bounds and placeholder rejection for every observed string, so malformed or non-answer values
  // (the network API returns the literal "Unknown") can never count as observed/complete, and a
  // single giant value cannot blow up the output. Valid values are kept byte-exact; these checks
  // only REJECT, they never trim or rewrite an accepted value.
  var MAX_FIELD_CHARS = 4096;
  var SHA256_BYTES = 32;
  // Case-insensitive, trimmed placeholders that are an absence of an answer, not a real fact.
  var PLACEHOLDERS = ["unknown"];

  // The EXACT, finite allowlist of Product API methods this probe may touch. index.html and the
  // tests assert the collector accesses nothing else, and nothing named set* or *Listener*.
  var STRING_FIELDS = [
    { key: "firmware", method: "getFirmware", api: "webapis.productinfo.getFirmware()" },
    { key: "realModel", method: "getRealModel", api: "webapis.productinfo.getRealModel()" },
    { key: "model", method: "getModel", api: "webapis.productinfo.getModel()" },
    { key: "modelCode", method: "getModelCode", api: "webapis.productinfo.getModelCode()" },
    {
      key: "serviceCountry",
      method: "getSystemConfig",
      api: "webapis.productinfo.getSystemConfig(ProductInfoConfigKey." + SERVICE_COUNTRY_KEY + ")",
      configKey: SERVICE_COUNTRY_KEY,
    },
    // getLocalSet() is opaque per the API docs: it is captured verbatim but is NOT required for a
    // "complete" verdict and is never interpreted here.
    { key: "localSet", method: "getLocalSet", api: "webapis.productinfo.getLocalSet()", opaque: true, optional: true },
  ];

  // Fields that must all be observed (plus the identity hash) for an overall "complete" verdict.
  var REQUIRED_KEYS = ["firmware", "realModel", "model", "modelCode", "serviceCountry", "platformVersion"];

  function toHex(bytes) {
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      var h = bytes[i].toString(16);
      out += h.length === 1 ? "0" + h : h;
    }
    return out;
  }

  function defaultEncodeUtf8(text) {
    return new TextEncoder().encode(text);
  }

  function hasControlChar(text) {
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      if (code < 0x20 || code === 0x7f) {
        return true;
      }
    }
    return false;
  }

  function isPlaceholder(text) {
    var folded = text.trim().toLowerCase();
    for (var i = 0; i < PLACEHOLDERS.length; i++) {
      if (folded === PLACEHOLDERS[i]) {
        return true;
      }
    }
    return false;
  }

  // Classifies a raw string into "observed" or a finite rejection code. Length is checked before
  // any whitespace/placeholder work, so an oversize value is rejected without further processing.
  function classifyString(raw) {
    if (typeof raw !== "string") {
      return "not-a-string";
    }
    if (raw.length > MAX_FIELD_CHARS) {
      return "too-long";
    }
    if (raw.trim().length === 0) {
      return "empty"; // covers "" and whitespace-only
    }
    if (hasControlChar(raw)) {
      return "unsafe";
    }
    if (isPlaceholder(raw)) {
      return "placeholder";
    }
    return "observed";
  }

  // Reads one string-valued Product API field. Returns a coded status; the raw value is kept
  // EXACTLY as returned when present. Finite statuses only: observed, api-unavailable, threw,
  // not-a-string, empty.
  function readStringField(def, productinfo, configKey) {
    var record = { api: def.api };
    if (def.opaque) {
      record.opaque = true;
    }
    if (!productinfo || typeof productinfo[def.method] !== "function") {
      record.status = "api-unavailable";
      return record;
    }
    if (def.configKey && (!configKey || typeof configKey[def.configKey] === "undefined")) {
      record.status = "api-unavailable";
      return record;
    }
    var raw;
    try {
      raw = def.configKey ? productinfo[def.method](configKey[def.configKey]) : productinfo[def.method]();
    } catch (e) {
      record.status = "threw";
      return record;
    }
    var status = classifyString(raw);
    if (status !== "observed") {
      record.status = status;
      return record;
    }
    record.value = raw; // exact, unmodified
    record.status = "observed";
    return record;
  }

  function readPlatformVersion(systeminfo) {
    var record = { api: "tizen.systeminfo.getCapability('" + PLATFORM_CAPABILITY + "')" };
    if (!systeminfo || typeof systeminfo.getCapability !== "function") {
      record.status = "api-unavailable";
      return record;
    }
    var raw;
    try {
      raw = systeminfo.getCapability(PLATFORM_CAPABILITY);
    } catch (e) {
      record.status = "threw";
      return record;
    }
    var status = classifyString(raw);
    if (status !== "observed") {
      record.status = status;
      return record;
    }
    record.value = raw;
    record.status = "observed";
    return record;
  }

  // Reads the DUID and returns ONLY its domain-separated SHA-256 hash. The raw DUID stays in a
  // local variable that goes out of scope here; it is never returned, logged or thrown. Absence of
  // WebCrypto is an explicit incomplete (crypto-unavailable), never a raw fallback.
  function readIdentity(productinfo, subtle, encodeUtf8) {
    var record = { scheme: IDENTITY_SCHEME, api: "webapis.productinfo.getDuid()" };
    if (!productinfo || typeof productinfo.getDuid !== "function") {
      record.status = "duid-unavailable";
      return Promise.resolve(record);
    }
    var duid;
    try {
      duid = productinfo.getDuid();
    } catch (e) {
      record.status = "duid-threw";
      return Promise.resolve(record);
    }
    // The DUID is bounds- and placeholder-checked like any field, but with duid-prefixed codes.
    // Its raw value is NEVER placed in the record, even on a rejection.
    var duidStatus = classifyString(duid);
    if (duidStatus !== "observed") {
      record.status = duidStatus === "empty" ? "duid-empty" : "duid-" + duidStatus;
      return Promise.resolve(record);
    }
    if (!subtle || typeof subtle.digest !== "function") {
      record.status = "crypto-unavailable";
      return Promise.resolve(record);
    }
    var digestPromise;
    try {
      // encodeUtf8 and subtle.digest may throw SYNCHRONOUSLY; both are caught here so the failure
      // becomes a coded status and never escapes collect().
      var bytes = encodeUtf8(IDENTITY_DOMAIN + duid);
      digestPromise = subtle.digest("SHA-256", bytes);
    } catch (e) {
      record.status = "hash-threw";
      return Promise.resolve(record);
    }
    return Promise.resolve(digestPromise).then(
      function (buf) {
        var arr;
        try {
          arr = new Uint8Array(buf);
        } catch (e) {
          record.status = "hash-invalid";
          return record;
        }
        if (arr.length !== SHA256_BYTES) {
          record.status = "hash-invalid"; // a SHA-256 digest is exactly 32 bytes
          return record;
        }
        record.digest = toHex(arr);
        record.status = "observed";
        return record;
      },
      function () {
        record.status = "hash-threw";
        return record;
      }
    );
  }

  function computeStatus(fields, identity) {
    for (var i = 0; i < REQUIRED_KEYS.length; i++) {
      var f = fields[REQUIRED_KEYS[i]];
      if (!f || f.status !== "observed") {
        return "incomplete";
      }
    }
    return identity.status === "observed" ? "complete" : "incomplete";
  }

  // The on-screen summary carries NON-SENSITIVE statuses only: no raw values and no DUID digest.
  function buildSummary(result) {
    var fieldStatus = {};
    Object.keys(result.fields).forEach(function (key) {
      fieldStatus[key] = result.fields[key].status;
    });
    return {
      schema: result.schema,
      status: result.status,
      fields: fieldStatus,
      identity: result.identity.status,
    };
  }

  function collect(deps) {
    deps = deps || {};
    var productinfo = deps.productinfo || null;
    var configKey = deps.productInfoConfigKey || null;
    var systeminfo = deps.systeminfo || null;
    var subtle = deps.subtle || null;
    var encodeUtf8 = deps.encodeUtf8 || defaultEncodeUtf8;

    var fields = {};
    STRING_FIELDS.forEach(function (def) {
      fields[def.key] = readStringField(def, productinfo, configKey);
    });
    fields.platformVersion = readPlatformVersion(systeminfo);

    return readIdentity(productinfo, subtle, encodeUtf8).then(function (identity) {
      var result = {
        schema: SCHEMA,
        status: computeStatus(fields, identity),
        source: SOURCE,
        apiReference: API_REFERENCE,
        fields: fields,
        identity: identity,
      };
      var json = JSON.stringify(result, null, 2);
      var summary = buildSummary(result);
      var done = Promise.resolve();
      if (typeof deps.writeFile === "function") {
        done = Promise.resolve(deps.writeFile(PRIVATE_OUTPUT, json));
      }
      return done.then(function () {
        if (typeof deps.showSummary === "function") {
          deps.showSummary(summary);
        }
        // The auto-exit is scheduled ONCE at the widget bootstrap, BEFORE collection, so a
        // never-resolving write or hash callback can never leave the probe in the foreground; it
        // is deliberately NOT tied to collect() completing.
        return { result: result, json: json, summary: summary };
      });
    });
  }

  var api = {
    collect: collect,
    SCHEMA: SCHEMA,
    SOURCE: SOURCE,
    API_REFERENCE: API_REFERENCE,
    IDENTITY_SCHEME: IDENTITY_SCHEME,
    IDENTITY_DOMAIN: IDENTITY_DOMAIN,
    PLATFORM_CAPABILITY: PLATFORM_CAPABILITY,
    SERVICE_COUNTRY_KEY: SERVICE_COUNTRY_KEY,
    PRIVATE_OUTPUT: PRIVATE_OUTPUT,
    REQUIRED_KEYS: REQUIRED_KEYS,
    STRING_FIELDS: STRING_FIELDS,
    MAX_FIELD_CHARS: MAX_FIELD_CHARS,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof globalThis !== "undefined") {
    globalThis.QuietTVDeviceFacts = api;
  }
})();
