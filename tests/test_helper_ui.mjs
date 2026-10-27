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
