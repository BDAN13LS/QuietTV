// Pure-Node tests for the widget bootstrap in index.html. No browser, no TV, no network.
// Run: node --test tools/tv-device-facts/index.test.js
//
// Two kinds of check:
//  1) Static: the official Samsung webapis.js loader is present, typed, and ordered BEFORE
//     collector.js and before the inline bootstrap.
//  2) VM: the inline bootstrap is executed in a sandbox with fakes, proving it wires the real
//     productinfo into collect() AND schedules the bounded auto-exit BEFORE collection, so a
//     never-resolving collect() still leaves the foreground.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const HTML = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

function scriptTags() {
  return [...HTML.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
}

function inlineBootstrap() {
  const inline = scriptTags().find((m) => !/\bsrc=/.test(m[1]) && m[2].trim().length);
  assert.ok(inline, "no inline bootstrap <script> found");
  return inline[2];
}

test("static: webapis.js loader is present, typed, and ordered before collector and bootstrap", () => {
  const webapisTag = scriptTags().find((m) => /\$WEBAPIS\/webapis\/webapis\.js/.test(m[1]));
  assert.ok(webapisTag, "the Samsung webapis.js loader tag is missing");
  assert.match(webapisTag[1], /type\s*=\s*"text\/javascript"/, "webapis.js loader is not typed text/javascript");

  const webapisIdx = HTML.indexOf("$WEBAPIS/webapis/webapis.js");
  const collectorIdx = HTML.indexOf('src="collector.js"');
  const bootstrapIdx = HTML.indexOf(inlineBootstrap().trim().slice(0, 24));
  assert.ok(webapisIdx >= 0 && collectorIdx >= 0 && bootstrapIdx >= 0, "a required script is missing");
  assert.ok(webapisIdx < collectorIdx, "webapis.js must load before collector.js");
  assert.ok(collectorIdx < bootstrapIdx, "collector.js must load before the bootstrap");
  // No external network origin: the loader uses the local $WEBAPIS SDK path only.
  assert.ok(!/src="https?:\/\//i.test(HTML), "a script loads over the network");
});

test("VM: the bootstrap schedules the bounded exit BEFORE collect, and wires real productinfo", () => {
  const events = [];
  let scheduledExit = null;
  let exits = 0;
  let collectDeps = null;

  const fakePI = { ProductInfoConfigKey: { CONFIG_KEY_SERVICE_COUNTRY: "CONFIG_KEY_SERVICE_COUNTRY" } };
  const fakeSubtle = { digest() {} };
  const fakeTizen = {
    filesystem: { resolve() {} },
    systeminfo: { getCapability() {} },
    application: {
      getCurrentApplication() {
        return {
          exit() {
            exits += 1;
          },
        };
      },
    },
  };

  const win = { webapis: { productinfo: fakePI }, tizen: fakeTizen, crypto: { subtle: fakeSubtle } };
  const sandbox = {
    window: win,
    document: { getElementById: () => ({ textContent: "" }) },
    tizen: fakeTizen,
    TextEncoder: TextEncoder,
    Promise: Promise,
    setTimeout: (fn, ms) => {
      events.push("schedule-exit");
      scheduledExit = { fn, ms };
      return 1;
    },
    QuietTVDeviceFacts: {
      collect: (deps) => {
        events.push("collect");
        collectDeps = deps;
        return new Promise(() => {}); // never resolves: simulates a hung write/hash callback
      },
    },
  };

  vm.runInNewContext(inlineBootstrap(), sandbox);

  // Ordering: the exit timer is registered before collection starts.
  assert.deepEqual(events, ["schedule-exit", "collect"], "exit was not scheduled before collect");
  assert.ok(scheduledExit && typeof scheduledExit.fn === "function", "no exit timer was scheduled");
  assert.ok(scheduledExit.ms > 0 && scheduledExit.ms <= 60000, "exit delay is not a small bound");

  // The real productinfo (and its config-key enum, systeminfo and subtle) were wired through.
  assert.equal(collectDeps.productinfo, fakePI);
  assert.equal(collectDeps.productInfoConfigKey, fakePI.ProductInfoConfigKey);
  assert.equal(collectDeps.systeminfo, fakeTizen.systeminfo);
  assert.equal(collectDeps.subtle, fakeSubtle);

  // Even though collect() never resolves, firing the scheduled timer exits the app.
  scheduledExit.fn();
  assert.equal(exits, 1, "the bounded exit did not exit the application");
});
