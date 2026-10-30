// Pure-Node tests for the QuietTV device-facts probe collector. No TV, no network, no device.
// Run: node --test tools/tv-device-facts/collector.test.js
//
// The fake webapis object is a Proxy that records every property it is asked for, so the tests can
// assert the collector touches ONLY the finite allowlist of Product API methods, and nothing named
// set* or *Listener*. A real Node WebCrypto hashes the DUID, so the digest is checked against an
// independent computation; the raw DUID is proven absent from every output.

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const C = require("./collector.js");

const ALLOWED_METHODS = new Set([
  "getFirmware",
  "getRealModel",
  "getModel",
  "getModelCode",
  "getSystemConfig",
  "getLocalSet",
  "getDuid",
  "ProductInfoConfigKey", // the enum object is read off productinfo in the widget bootstrap, not here
]);

const SAMPLE = {
  firmware: "T-X22UDFAKUC-0090",
  realModel: "UN43U8000FBXZA",
  model: "U8000",
  modelCode: "U8000F",
  serviceCountry: "US",
  localSet: "US/EASTERN/opaque",
  platformVersion: "9.0.0",
  duid: "DUID-SECRET-0123456789ABCDEF",
};

function recordingProductInfo(overrides) {
  const accessed = [];
  const impl = Object.assign(
    {
      ProductInfoConfigKey: { CONFIG_KEY_SERVICE_COUNTRY: "CONFIG_KEY_SERVICE_COUNTRY" },
      getFirmware: () => SAMPLE.firmware,
      getRealModel: () => SAMPLE.realModel,
      getModel: () => SAMPLE.model,
      getModelCode: () => SAMPLE.modelCode,
      getSystemConfig: (key) => {
        assert.equal(key, "CONFIG_KEY_SERVICE_COUNTRY", "service-country key not passed through");
        return SAMPLE.serviceCountry;
      },
      getLocalSet: () => SAMPLE.localSet,
      getDuid: () => SAMPLE.duid,
    },
    overrides || {}
  );
  const proxy = new Proxy(impl, {
    get(target, prop) {
      if (typeof prop === "string") {
        accessed.push(prop);
      }
      return target[prop];
    },
  });
  return { proxy, accessed, impl };
}

function fakeSystemInfo(value) {
  return {
    getCapability(name) {
      assert.equal(name, C.PLATFORM_CAPABILITY, "platform capability name changed");
      return value;
    },
  };
}

function baseDeps(productinfo, extra) {
  const writes = [];
  const shown = [];
  const deps = Object.assign(
    {
      productinfo,
      productInfoConfigKey: productinfo ? productinfo.ProductInfoConfigKey : null,
      systeminfo: fakeSystemInfo(SAMPLE.platformVersion),
      subtle: crypto.webcrypto.subtle,
      encodeUtf8: (s) => new TextEncoder().encode(s),
      writeFile: (name, text) => {
        writes.push({ name, text });
        return Promise.resolve();
      },
      showSummary: (s) => shown.push(s),
    },
    extra || {}
  );
  return { deps, writes, shown };
}

function expectedDigest(duid) {
  return crypto.createHash("sha256").update(C.IDENTITY_DOMAIN + duid, "utf8").digest("hex");
}

test("complete capture: exact raw values, correct identity hash, writes, shows", async () => {
  const pi = recordingProductInfo();
  const { deps, writes, shown } = baseDeps(pi.proxy);
  const { result, json } = await C.collect(deps);

  assert.equal(result.schema, "quiettv-device-facts/1");
  assert.equal(result.status, "complete");
  assert.equal(result.source, "samsung-productinfo-webapi");
  // Values retained EXACTLY as the API returned them.
  assert.equal(result.fields.firmware.value, SAMPLE.firmware);
  assert.equal(result.fields.realModel.value, SAMPLE.realModel);
  assert.equal(result.fields.model.value, SAMPLE.model);
  assert.equal(result.fields.modelCode.value, SAMPLE.modelCode);
  assert.equal(result.fields.serviceCountry.value, SAMPLE.serviceCountry);
  assert.equal(result.fields.localSet.value, SAMPLE.localSet);
  assert.equal(result.fields.localSet.opaque, true);
  assert.equal(result.fields.platformVersion.value, SAMPLE.platformVersion);
  for (const k of Object.keys(result.fields)) {
    assert.equal(result.fields[k].status, "observed", k);
  }
  // Identity is a hash under the new, documented scheme; digest matches an independent computation.
  assert.equal(result.identity.scheme, "quiettv-device-facts-duid/sha256/1");
  assert.equal(result.identity.status, "observed");
  assert.equal(result.identity.digest, expectedDigest(SAMPLE.duid));
  assert.equal(result.identity.digest.length, 64); // 32 bytes

  assert.equal(writes.length, 1);
  assert.equal(writes[0].name, "wgt-private/device-facts.json");
  assert.equal(JSON.parse(writes[0].text).schema, "quiettv-device-facts/1");
  assert.equal(writes[0].text, json);
  assert.equal(shown.length, 1);
});

test("the collector touches only the allowlisted Product API methods, no set*/listener", async () => {
  const pi = recordingProductInfo();
  const { deps } = baseDeps(pi.proxy);
  await C.collect(deps);
  for (const prop of pi.accessed) {
    assert.ok(ALLOWED_METHODS.has(prop), "collector accessed a non-allowlisted member: " + prop);
    assert.ok(!/^set/.test(prop), "collector accessed a set* member: " + prop);
    assert.ok(!/listener/i.test(prop), "collector accessed a listener member: " + prop);
  }
  for (const m of ["getFirmware", "getRealModel", "getModel", "getModelCode", "getSystemConfig", "getLocalSet", "getDuid"]) {
    assert.ok(pi.accessed.includes(m), "expected method not called: " + m);
  }
});

test("the raw DUID never appears in the result, json or summary", async () => {
  const pi = recordingProductInfo();
  const { deps, writes, shown } = baseDeps(pi.proxy);
  const { result, json, summary } = await C.collect(deps);
  const haystacks = [json, JSON.stringify(result), JSON.stringify(summary), writes[0].text, JSON.stringify(shown)];
  for (const h of haystacks) {
    assert.ok(!h.includes(SAMPLE.duid), "raw DUID leaked into an output");
  }
  assert.ok(json.includes(result.identity.digest));
  assert.equal(summary.identity, "observed");
  assert.equal(typeof summary.fields.firmware, "string");
  assert.equal(summary.fields.firmware, "observed");
});

test("no WebCrypto => explicit incomplete, no digest, raw DUID still absent", async () => {
  const pi = recordingProductInfo();
  const { deps, writes } = baseDeps(pi.proxy, { subtle: null });
  const { result } = await C.collect(deps);
  assert.equal(result.identity.status, "crypto-unavailable");
  assert.ok(!("digest" in result.identity), "a digest was produced without crypto");
  assert.equal(result.status, "incomplete");
  assert.ok(!writes[0].text.includes(SAMPLE.duid), "raw DUID leaked when crypto was unavailable");
});

test("a field that throws is coded and forces incomplete; others still observed", async () => {
  const pi = recordingProductInfo({
    getFirmware: () => {
      throw new Error("not available on this model");
    },
  });
  const { deps } = baseDeps(pi.proxy);
  const { result } = await C.collect(deps);
  assert.equal(result.fields.firmware.status, "threw");
  assert.ok(!("value" in result.fields.firmware), "a throwing field kept a value");
  assert.equal(result.fields.realModel.status, "observed");
  assert.equal(result.status, "incomplete");
});

test("missing Product API object => every field api-unavailable, incomplete", async () => {
  const { deps } = baseDeps(null);
  const { result } = await C.collect(deps);
  for (const k of C.REQUIRED_KEYS) {
    if (k === "platformVersion") {
      continue;
    }
    assert.equal(result.fields[k].status, "api-unavailable", k);
  }
  assert.equal(result.identity.status, "duid-unavailable");
  assert.equal(result.status, "incomplete");
});

test("an empty or non-string field is coded, never filled with a default", async () => {
  const pi = recordingProductInfo({ getModel: () => "", getModelCode: () => 1234 });
  const { deps } = baseDeps(pi.proxy);
  const { result } = await C.collect(deps);
  assert.equal(result.fields.model.status, "empty");
  assert.equal(result.fields.modelCode.status, "not-a-string");
  assert.ok(!("value" in result.fields.model));
  assert.ok(!("value" in result.fields.modelCode));
  assert.equal(result.status, "incomplete");
});

// ---- regression: review round 1 -----------------------------------------------------------

test("REGRESSION: all getters 'Unknown' => placeholder, never observed/complete", async () => {
  // Parent reproducer: every string (and the platform capability) is the literal placeholder
  // 'Unknown'; the DUID is a valid synthetic value and real WebCrypto hashes it. The result must
  // be incomplete and no field may be 'observed'.
  const pi = recordingProductInfo({
    getFirmware: () => "Unknown",
    getRealModel: () => "Unknown",
    getModel: () => "Unknown",
    getModelCode: () => "Unknown",
    getSystemConfig: () => "Unknown",
    getLocalSet: () => "Unknown",
  });
  const { deps } = baseDeps(pi.proxy, { systeminfo: fakeSystemInfo("Unknown") });
  const { result } = await C.collect(deps);
  for (const k of Object.keys(result.fields)) {
    assert.equal(result.fields[k].status, "placeholder", k + " not refused as placeholder");
    assert.ok(!("value" in result.fields[k]), k + " kept a placeholder value");
  }
  assert.equal(result.identity.status, "observed"); // valid DUID + real crypto
  assert.equal(result.status, "incomplete");
});

test("REGRESSION: whitespace-only, control chars and overlong strings are refused", async () => {
  const over = "x".repeat(C.MAX_FIELD_CHARS + 1);
  const pi = recordingProductInfo({
    getFirmware: () => "   ",
    getRealModel: () => "ok\u0001bad",
    getModel: () => over,
  });
  const { deps } = baseDeps(pi.proxy);
  const { result } = await C.collect(deps);
  assert.equal(result.fields.firmware.status, "empty");
  assert.equal(result.fields.realModel.status, "unsafe");
  assert.equal(result.fields.model.status, "too-long");
  for (const k of ["firmware", "realModel", "model"]) {
    assert.ok(!("value" in result.fields[k]), k + " kept an invalid value");
  }
  assert.equal(result.status, "incomplete");
});

test("REGRESSION: a value at the length bound is observed and kept exactly (no trim)", async () => {
  const atBound = "A".repeat(C.MAX_FIELD_CHARS);
  const exact = "T-X22UDFAKUC-0090-REL-202607141244"; // internal punctuation preserved
  const pi = recordingProductInfo({ getModel: () => atBound, getFirmware: () => exact });
  const { deps } = baseDeps(pi.proxy);
  const { result } = await C.collect(deps);
  assert.equal(result.fields.model.status, "observed");
  assert.equal(result.fields.model.value, atBound);
  assert.equal(result.fields.firmware.value, exact);
});

test("REGRESSION: a synchronous subtle.digest throw is caught as hash-threw", async () => {
  // Parent reproducer: digest throws synchronously, which previously escaped the whole collect().
  const pi = recordingProductInfo();
  const { deps } = baseDeps(pi.proxy, {
    subtle: {
      digest() {
        throw new Error("synthetic crypto failure");
      },
    },
  });
  const { result } = await C.collect(deps); // must NOT throw
  assert.equal(result.identity.status, "hash-threw");
  assert.ok(!("digest" in result.identity));
  assert.equal(result.status, "incomplete");
  assert.ok(!JSON.stringify(result).includes(SAMPLE.duid), "raw DUID leaked on a hash failure");
});

test("REGRESSION: a malformed (wrong-length) digest result is refused", async () => {
  const pi = recordingProductInfo();
  const { deps } = baseDeps(pi.proxy, {
    subtle: {
      digest() {
        return Promise.resolve(new ArrayBuffer(16)); // not 32 bytes
      },
    },
  });
  const { result } = await C.collect(deps);
  assert.equal(result.identity.status, "hash-invalid");
  assert.ok(!("digest" in result.identity));
  assert.equal(result.status, "incomplete");
});

test("REGRESSION: an invalid DUID (placeholder/overlong/control) is coded, not hashed", async () => {
  for (const [duid, code] of [
    ["Unknown", "duid-placeholder"],
    ["   ", "duid-empty"],
    ["x".repeat(C.MAX_FIELD_CHARS + 1), "duid-too-long"],
    ["bad\u0002duid", "duid-unsafe"],
  ]) {
    const pi = recordingProductInfo({ getDuid: () => duid });
    const { deps } = baseDeps(pi.proxy);
    const { result, json } = await C.collect(deps);
    assert.equal(result.identity.status, code, "DUID '" + code + "' not coded");
    assert.ok(!("digest" in result.identity));
    assert.equal(result.status, "incomplete");
    // A whitespace-only value trivially matches indented JSON, so the leak check is only
    // meaningful for a distinctive (non-whitespace) value.
    if (duid.trim().length > 0) {
      assert.ok(!json.includes(duid), "raw invalid DUID leaked");
    }
  }
});
