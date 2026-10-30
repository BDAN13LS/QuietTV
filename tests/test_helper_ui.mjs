// helper/ui/app.js in a fake DOM: token from the fragment only, fragment removed, header-only auth,
// no TV call before the owner clicks, TV values never reach innerHTML, actions disabled with reasons.
// Run: node --test tests/test_helper_ui.mjs   (Node built-ins only; no npm packages)
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(new URL("../helper/ui/app.js", import.meta.url), "utf8");
const HTML = fs.readFileSync(new URL("../helper/ui/index.html", import.meta.url), "utf8");
const TOKEN = "SyntheticTokenAbcdefghijklmnopqrstuvwxyz_012345";
// Built at run time so no literal LAN address sits in public source.
const TARGET = [192, 168, 1, 20].join(".");
const HOSTILE = "<img src=x onerror=alert(1)>";

function state(overrides = {}) {
  const gates = [{ id: "preview", text: "This developer preview never changes your TV." }];
  const action = (title) => ({ title, enabled: false, steps: ["Step one."], gates });
  return {
    preview: true, target: null, job: null, lastJob: null, check: null, engine: "none",
    capabilities: {},
    actions: { backup: action("Back up"), launcher: action("Install launcher"),
               cleanup: action("Cleanup"), restore: action("Restore") },
    prerequisites: ["The TV is switched on."],
    ...overrides,
  };
}

function makeEl(tag, id) {
  const el = {
    tagName: tag, id, hidden: false, disabled: false, value: "", className: "", type: "",
    children: [], attrs: {}, listeners: {}, _text: "",
    classes: new Set(),
    classList: {
      toggle(c, on) { if (on === undefined ? !el.classes.has(c) : on) el.classes.add(c); else el.classes.delete(c); },
      add(c) { el.classes.add(c); }, remove(c) { el.classes.delete(c); },
    },
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
    set textContent(v) { this._text = String(v); this.children = []; },
    set innerHTML(_) { throw new Error("innerHTML used"); },
    get firstChild() { return this.children[0] || null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(t, f) { this.listeners[t] = f; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; },
    focus() {},
  };
  return el;
}

function harness(hash, responder) {
  const byId = {};
  const getEl = (id) => (byId[id] ||= makeEl("div", id));
  const nav = ["check", "backup", "launcher", "cleanup", "restore", "report"].map((s) => {
    const b = makeEl("button");
    b.setAttribute("data-screen", s);
    return b;
  });
  const calls = { fetch: [], replace: [] };
  const timers = [];
  let onReady = null;
  const location = { hash, pathname: "/" };
  const ctx = {
    window: null,
    document: {
      getElementById: getEl,
      createElement: (tag) => makeEl(tag),
      querySelectorAll: () => nav,
      addEventListener: (t, f) => { if (t === "DOMContentLoaded") onReady = f; },
    },
    navigator: {},
    setTimeout: (f) => timers.push(f), clearTimeout: () => {},
    fetch: (url, init) => {
      calls.fetch.push({ url, init });
      const { status, data } = responder(url, init);
      return Promise.resolve({ ok: status < 300, status, json: () => Promise.resolve(data) });
    },
    JSON, Date, Object, Array, String, Promise, RegExp,
  };
  ctx.window = { location, history: { replaceState: (s, t, url) => { calls.replace.push(url); location.hash = ""; } } };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { ready: () => onReady(), getEl, calls, location, timers };
}

const flush = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 5; i++) await flush(); };

// Serves each /api/state answer in turn (the last one repeats); any other URL fails the test.
function stateSequence(states) {
  let i = 0;
  return (url) => {
    assert.equal(url, "/api/state");
    return { status: 200, data: states[Math.min(i++, states.length - 1)] };
  };
}

// Loads the page, then runs each queued poll once; returns the form line after every step.
async function walk(h, polls) {
  const seen = [];
  const msg = () => ({ text: h.getEl("form-msg").textContent, error: h.getEl("form-msg").classes.has("error") });
  h.ready();
  await settle();
  seen.push(msg());
  for (let p = 0; p < polls; p++) {
    assert.ok(h.timers.length > p, "a poll was scheduled while the check ran");
    h.timers[p]();
    await settle();
    seen.push(msg());
  }
  return seen;
}

const DONE_CHECK = { reachable: true, error: null, model: "SYNTHETIC-MODEL", os: "Tizen", developerMode: true,
                     deviceIdSha256: "b".repeat(64), deviceIdSource: "duid", modelSupported: false, profileIds: [],
                     exactMatchProven: false, missingFacts: [], installEligible: false, checkedAt: 1800000000 };
const FAILED_CHECK = { reachable: false, error: { code: "unreachable", message: "QuietTV could not reach a TV there." },
                       checkedAt: 1800000000 };

test("static: no inline script/style, no token storage, header auth", () => {
  assert.doesNotMatch(HTML, /<script(?![^>]*\bsrc=)[^>]*>/i);
  assert.doesNotMatch(HTML, /\son[a-z]+=/i);
  assert.doesNotMatch(HTML, /style=/i);
  for (const banned of ["localStorage", "sessionStorage", "document.cookie", "innerHTML", "outerHTML",
                        "insertAdjacentHTML", "eval(", "new Function", "?t=", "?token"]) {
    assert.ok(!SRC.includes(banned), banned);
  }
  assert.match(SRC, /history\.replaceState/);
  assert.match(SRC, /X-QuietTV-Session/);
  // /api/check is only ever posted from the submit handler
  const checkCalls = SRC.split('"/api/check"').length - 1;
  assert.equal(checkCalls, 1);
  assert.match(SRC, /function onCheck[\s\S]*"\/api\/check"[\s\S]*function onCancel/);
});

test("token taken from fragment, fragment removed, only local state read on load", async () => {
  const h = harness("#t=" + TOKEN, () => ({ status: 200, data: state() }));
  h.ready();
  await flush();
  assert.deepEqual(h.calls.replace, ["/"]);
  assert.equal(h.location.hash, "");
  assert.equal(h.calls.fetch.length, 1);
  const { url, init } = h.calls.fetch[0];
  assert.equal(url, "/api/state");
  assert.equal(init.headers["X-QuietTV-Session"], TOKEN);
  assert.equal(init.credentials, "omit");
  assert.ok(!url.includes(TOKEN));
});

test("no or malformed token: fragment still removed, nothing fetched, banner shown", async () => {
  for (const hash of ["", "#t=short", "#t=" + TOKEN + "&x=1", "#token=" + TOKEN]) {
    const h = harness(hash, () => { throw new Error("must not fetch"); });
    h.ready();
    await flush();
    assert.equal(h.calls.fetch.length, 0, hash);
    assert.equal(h.getEl("session-banner").hidden, false);
    assert.deepEqual(h.calls.replace, hash ? ["/"] : []);
  }
});

test("Check TV posts target then check with the header; TV values rendered as text", async () => {
  const checked = state({
    target: TARGET,
    check: { reachable: true, error: null, model: HOSTILE, os: HOSTILE, developerMode: true,
             deviceIdSha256: "a".repeat(64), deviceIdSource: "duid", modelSupported: false, profileIds: [],
             exactMatchProven: false, missingFacts: [{ field: "firmware", label: HOSTILE }],
             installEligible: false, checkedAt: 1800000000 },
  });
  const h = harness("#t=" + TOKEN, (url) => ({ status: url === "/api/check" ? 202 : 200,
                                              data: url === "/api/state" ? state() : checked }));
  h.ready();
  await flush();
  h.getEl("tv-ip").value = " " + TARGET + " ";
  h.getEl("check-form").listeners.submit({ preventDefault() {} });
  for (let i = 0; i < 5; i++) await flush();
  const posts = h.calls.fetch.slice(1);
  assert.deepEqual(posts.map((c) => c.url), ["/api/target", "/api/check"]);
  assert.deepEqual(JSON.parse(posts[0].init.body), { ip: TARGET });
  assert.deepEqual(JSON.parse(posts[1].init.body), { target: TARGET });
  for (const c of posts) {
    assert.equal(c.init.method, "POST");
    assert.equal(c.init.headers["X-QuietTV-Session"], TOKEN);
    assert.equal(c.init.headers["Content-Type"], "application/json");
  }
  assert.ok(h.getEl("facts").textContent.includes(HOSTILE)); // as literal text, innerHTML would have thrown
  assert.equal(h.getEl("result-card").hidden, false);
});

test("action screens: button disabled and described by visible reasons", async () => {
  const h = harness("#t=" + TOKEN, () => ({ status: 200, data: state() }));
  h.ready();
  await flush();
  for (const name of ["backup", "launcher", "cleanup", "restore"]) {
    const section = h.getEl("screen-" + name);
    const flat = [];
    const walk = (n) => { flat.push(n); n.children.forEach(walk); };
    walk(section);
    const button = flat.find((n) => n.tagName === "button");
    const list = flat.find((n) => n.id === "gates-" + name);
    assert.ok(button && list, name);
    assert.equal(button.disabled, true);
    assert.equal(button.getAttribute("aria-describedby"), "gates-" + name);
    assert.ok(list.textContent.includes("never changes your TV"));
  }
  assert.ok(h.getEl("report-text").textContent.includes("Changes made to the TV: none"));
});

test("form line: running -> done says the check is complete, with no extra TV call", async () => {
  const running = state({ target: TARGET, job: { id: "job1", status: "running" } });
  const done = state({ target: TARGET, lastJob: { id: "job1", status: "done" }, check: DONE_CHECK });
  const h = harness("#t=" + TOKEN, stateSequence([running, done]));
  const seen = await walk(h, 1);
  assert.deepEqual(seen, [{ text: "Checking your TV...", error: false }, { text: "TV check complete.", error: false }]);
  assert.ok(h.calls.fetch.every((c) => c.url === "/api/state"));
  assert.equal(h.timers.length, 1, "no poll after the check finished");
});

test("form line: running -> failed shows the check's own error, marked as an error", async () => {
  const running = state({ target: TARGET, job: { id: "job2", status: "running" } });
  const failed = state({ target: TARGET, lastJob: { id: "job2", status: "failed" }, check: FAILED_CHECK });
  const h = harness("#t=" + TOKEN, stateSequence([running, failed]));
  const seen = await walk(h, 1);
  assert.deepEqual(seen, [{ text: "Checking your TV...", error: false },
                          { text: FAILED_CHECK.error.message, error: true }]);
  assert.ok(h.calls.fetch.every((c) => c.url === "/api/state"));
});

test("form line: cancelling -> cancelled keeps the cancel text", async () => {
  const cancelling = state({ target: TARGET, job: { id: "job3", status: "cancelling" } });
  const cancelled = state({ target: TARGET, lastJob: { id: "job3", status: "cancelled" } });
  const h = harness("#t=" + TOKEN, stateSequence([cancelling, cancelled, cancelled]));
  const seen = await walk(h, 1);
  assert.deepEqual(seen, [{ text: "Stopping the check...", error: false },
                          { text: "The check was cancelled.", error: false }]);
});

test("form line: no job on load clears stale checking text", async () => {
  const h = harness("#t=" + TOKEN, stateSequence([state()]));
  h.getEl("form-msg").textContent = "Checking your TV...";
  const seen = await walk(h, 0);
  assert.deepEqual(seen, [{ text: "", error: false }]);
});

// ---- saved-report preflight ----

function preflightState(overrides = {}) {
  const fields = [
    { field: "model", label: "model number", value: "SYNTHETIC-QTV0001", source: "observed:productInfo.realModel" },
    { field: "firmware", label: "software (firmware) version", value: "0000.1", source: "interpreted" },
    { field: "arch", label: "processor type", value: "armv7l", source: "observed:native.machine" },
    { field: "osName", label: "operating system", value: "Tizen", source: "interpreted" },
    { field: "osVersion", label: "Tizen version", value: "9", source: "interpreted" },
    { field: "firmwareBuild", label: "full firmware build", value: "SYNTHETIC-BUILD-0000.1", source: "observed:productInfo.firmware" },
    { field: "region", label: "sales region", value: "SYNTHETIC-REGION", source: "interpreted" },
  ];
  const success = { configured: true, status: "success", error: null, result: {
    profileId: "SYNTHETIC-QTV0001-0000.1-armv7l-tizen9", identityScheme: "quiettv-device-facts-duid/sha256/1",
    fields, note: "Preflight display only.", operatorAssertions: { trustPolicyAndProfile: true, pairedFromSameTv: true } } };
  return state({ preflight: { ...success, ...overrides } });
}

test("preflight static: posted only from its handler with an empty body, sources never named in the page", () => {
  assert.equal(SRC.split('"/api/preflight"').length - 1, 1);
  assert.match(SRC, /function onPreflight[\s\S]*"\/api\/preflight", \{\}\)[\s\S]*function/);
  // The page never names or chooses a source (those are startup config only); it only shows the flag name in a hint.
  for (const banned of ["productInfoReport", "nativeInspectionFrame", "joinPolicy"]) {
    assert.ok(!SRC.includes(banned), banned);
  }
});

test("preflight success: seven fields render as literal text with their source labels", async () => {
  const h = harness("#t=" + TOKEN, () => ({ status: 200, data: preflightState() }));
  h.ready();
  await flush();
  assert.equal(h.getEl("preflight-card").hidden, false);
  assert.equal(h.getEl("preflight-result").hidden, false);
  const text = h.getEl("preflight-facts").textContent;
  for (const v of ["SYNTHETIC-QTV0001", "0000.1", "armv7l", "SYNTHETIC-REGION"]) assert.ok(text.includes(v), v);
  assert.ok(text.includes("read from the saved ProductInfo report"));
  assert.ok(text.includes("interpreted by your trusted policy"));
  assert.ok(h.getEl("preflight-msg").textContent.includes("complete"));
});

test("preflight success: a hostile field value stays literal text (innerHTML would throw)", async () => {
  const hostile = preflightState();
  hostile.preflight.result.fields[0].value = HOSTILE;
  const h = harness("#t=" + TOKEN, () => ({ status: 200, data: hostile }));
  h.ready();
  await flush();
  assert.ok(h.getEl("preflight-facts").textContent.includes(HOSTILE));
});

test("preflight success does not change the live check facts", async () => {
  const combined = preflightState();
  combined.check = DONE_CHECK;  // modelSupported false -> live card shows the honest gaps
  combined.target = TARGET;
  const h = harness("#t=" + TOKEN, () => ({ status: 200, data: combined }));
  h.ready();
  await flush();
  const live = h.getEl("facts").textContent;
  assert.ok(live.includes("Not proven"), "live exact-match line unchanged");
  assert.ok(live.includes("Ready to install"), "live ready-to-install line unchanged");
});

test("preflight unconfigured: a startup hint, the button hidden, no fake success", async () => {
  const h = harness("#t=" + TOKEN, () => ({ status: 200,
    data: state({ preflight: { configured: false, status: "idle", error: null, result: null } }) }));
  h.ready();
  await flush();
  assert.equal(h.getEl("preflight-card").hidden, false);
  assert.equal(h.getEl("preflight-actions").hidden, true);
  assert.equal(h.getEl("preflight-hint").hidden, false);
  assert.ok(h.getEl("preflight-hint").textContent.includes("--preflight-config"));
  assert.equal(h.getEl("preflight-result").hidden, true);
  assert.equal(h.getEl("preflight-msg").textContent, "");
});

test("preflight running: the button is disabled and a checking message shows", async () => {
  const h = harness("#t=" + TOKEN, () => ({ status: 200,
    data: state({ preflight: { configured: true, status: "running", error: null, result: null } }) }));
  h.ready();
  await flush();
  assert.equal(h.getEl("preflight-btn").disabled, true);           // no second run while one is in flight
  assert.equal(h.getEl("preflight-result").hidden, true);          // nothing claimed mid-run
  assert.ok(h.getEl("preflight-msg").textContent.includes("Checking"));
});

test("preflight button posts an empty body to /api/preflight", async () => {
  const seq = [preflightState({ status: "idle", result: null }), preflightState()];
  let i = 0;
  const h = harness("#t=" + TOKEN, (url) => {
    if (url === "/api/preflight") return { status: 200, data: seq[Math.min(++i, seq.length - 1)] };
    return { status: 200, data: seq[0] };
  });
  h.ready();
  await flush();
  h.getEl("preflight-btn").listeners.click();
  await settle();
  const post = h.calls.fetch.find((c) => c.url === "/api/preflight");
  assert.ok(post, "preflight was posted");
  assert.equal(post.init.method, "POST");
  assert.equal(post.init.body, "{}");
  assert.equal(post.init.headers["X-QuietTV-Session"], TOKEN);
});

test("form line: a refused address keeps its message after a finished check", async () => {
  const done = state({ target: TARGET, lastJob: { id: "job4", status: "done" }, check: DONE_CHECK });
  const refusal = "Type the TV's address as four numbers separated by dots.";
  const h = harness("#t=" + TOKEN, (url) => url === "/api/state" ? { status: 200, data: done }
                                                                  : { status: 400, data: { error: refusal } });
  await walk(h, 0);
  h.getEl("tv-ip").value = "tv.local";
  h.getEl("check-form").listeners.submit({ preventDefault() {} });
  await settle();
  assert.deepEqual(h.calls.fetch.map((c) => c.url), ["/api/state", "/api/target"]);
  assert.equal(h.getEl("form-msg").textContent, refusal);
  assert.ok(h.getEl("form-msg").classes.has("error"));
});
