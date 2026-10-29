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
    if (typeof raw !== "string") {
      record.status = "not-a-string";
      return record;
    }
    if (raw.length === 0) {
      record.status = "empty";
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
    if (typeof raw !== "string") {
      record.status = "not-a-string";
      return record;
    }
    if (raw.length === 0) {
      record.status = "empty";
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
    if (typeof duid !== "string" || duid.length === 0) {
      record.status = "duid-empty";
      return Promise.resolve(record);
    }
    if (!subtle || typeof subtle.digest !== "function") {
      record.status = "crypto-unavailable";
      return Promise.resolve(record);
    }
    var bytes;
    try {
      bytes = encodeUtf8(IDENTITY_DOMAIN + duid);
    } catch (e) {
      record.status = "hash-threw";
      return Promise.resolve(record);
    }
    return Promise.resolve(subtle.digest("SHA-256", bytes)).then(
      function (buf) {
        record.digest = toHex(new Uint8Array(buf));
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
        if (typeof deps.scheduleExit === "function") {
          deps.scheduleExit();
        }
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
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof globalThis !== "undefined") {
    globalThis.QuietTVDeviceFacts = api;
  }
})();
