/* QuietTV Helper page (developer preview).
 *
 * The session token arrives once in the URL fragment (#t=...), is kept in memory only and
 * the fragment is removed at once. It is sent only in the X-QuietTV-Session header.
 * Nothing talks to the TV until the owner presses Check TV. Every value from the TV is
 * written with textContent.
 */
(function () {
  "use strict";

  var TOKEN_HEADER = "X-QuietTV-Session";
  var TOKEN_RE = /^#t=([A-Za-z0-9_-]{43,128})$/;
  var POLL_MS = 500;
  var ACTION_INFO = {
    backup: "Save a copy of everything QuietTV could ever change on your TV, on this PC, before anything else happens.",
    launcher: "Put the QuietTV home screen on your TV.",
    cleanup: "Remove Samsung apps you do not want, such as ads and tracking. Each one can be put back.",
    restore: "Put back apps QuietTV removed, or undo everything QuietTV changed."
  };
  // Plain-language names for the fixed factsjoin field sources; an unknown source is shown verbatim.
  var SOURCE_LABELS = {
    "observed:productInfo.realModel": "read from the saved ProductInfo report",
    "observed:productInfo.firmware": "read from the saved ProductInfo report",
    "observed:native.machine": "read from the saved native inventory",
    "interpreted": "interpreted by your trusted policy"
  };
  var token = null;
  var state = null;
  var pollTimer = null;
  var jobMsgKey = null;
  var jobMsgShown = true; // whatever the form line says before the first state is stale

  function takeToken() {
    var hash = window.location.hash;
    var match = TOKEN_RE.exec(hash);
    if (match) token = match[1];
    if (hash) window.history.replaceState(null, "", window.location.pathname);
  }

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function api(method, path, body) {
    var headers = {};
    headers[TOKEN_HEADER] = token || "";
    var init = { method: method, headers: headers, credentials: "omit", cache: "no-store", redirect: "error" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return fetch(path, init).then(function (res) {
      return res.json().then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
    });
  }

  function clock(seconds) {
    return new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true });
  }

  function say(id, text, isError) {
    var node = $(id);
    node.textContent = text || "";
    node.classList.toggle("error", !!isError);
  }

  // ---- rendering ----

  function fact(list, label, value, tone) {
    list.appendChild(el("dt", null, label));
    var dd = el("dd", tone ? "tone-" + tone : null, value);
    list.appendChild(dd);
  }

  // The job line under the form changes only when the job state changes, so a message about
  // what the owner typed stays until the next check starts or ends.
  function jobMessage(busy) {
    var job = busy ? state.job : state.lastJob;
    if (!job) return { key: "idle", text: "" };
    var key = (busy ? "job:" : "last:") + job.id + ":" + job.status;
    if (job.status === "cancelling") return { key: key, text: "Stopping the check..." };
    if (job.status === "running") return { key: key, text: "Checking your TV..." };
    if (job.status === "cancelled") return { key: key, text: "The check was cancelled." };
    if (job.status === "done") return { key: key, text: "TV check complete." };
    var check = state.check;
    return { key: key, error: true,
             text: check && check.error ? check.error.message : "The TV check did not finish." };
  }

  function renderCheck() {
    var job = state.job;
    var busy = !!job && (job.status === "running" || job.status === "cancelling");
    $("check-btn").disabled = busy;
    $("tv-ip").disabled = busy;
    $("cancel-btn").hidden = !busy || job.status !== "running";
    var msg = jobMessage(busy);
    if (msg.key !== jobMsgKey) {
      jobMsgKey = msg.key;
      if (msg.text || jobMsgShown) say("form-msg", msg.text, msg.error);
      jobMsgShown = !!msg.text;
    }
    if (state.target && !$("tv-ip").value) $("tv-ip").value = state.target;

    var check = state.check;
    var card = $("result-card");
    card.hidden = !check;
    if (check) {
      var list = $("facts");
      clear(list);
      if (check.error) {
        fact(list, "Reachable", check.reachable ? "Something answered" : "No answer", check.reachable ? "warn" : "bad");
        fact(list, "What happened", check.error.message, "bad");
      } else {
        fact(list, "Reachable", "Yes, a Samsung TV answered", "good");
        fact(list, "Model", check.model);
        fact(list, "Model supported", check.modelSupported ? "Yes" : "Not yet", check.modelSupported ? "good" : "bad");
        if (check.os) fact(list, "Operating system", check.os);
        fact(list, "Developer Mode", check.developerMode === true ? "On" : check.developerMode === false ? "Off" : "Not reported",
             check.developerMode === true ? "good" : "warn");
        fact(list, "Exact match", "Not proven", "warn");
        fact(list, "Ready to install", "No", "bad");
        if (check.deviceIdSha256) fact(list, "TV fingerprint", check.deviceIdSha256.slice(0, 16) + "...");
        fact(list, "Checked at", clock(check.checkedAt));
      }
      var missing = $("missing");
      clear(missing);
      (check.missingFacts || []).forEach(function (m) {
        missing.appendChild(el("li", null, "This TV's " + m.label));
      });
      $("missing-wrap").hidden = !(check.missingFacts && check.missingFacts.length);
    }

    var prereqs = $("prereqs");
    clear(prereqs);
    state.prerequisites.forEach(function (p) { prereqs.appendChild(el("li", null, p)); });
  }

  function renderAction(name) {
    var spec = state.actions[name];
    var section = $("screen-" + name);
    clear(section);
    var h1 = el("h1", null, spec.title);
    h1.id = "h-" + name;
    section.appendChild(h1);
    section.appendChild(el("p", "lead", ACTION_INFO[name]));

    var steps = el("div", "card");
    steps.appendChild(el("h2", null, "What will happen"));
    var ol = el("ol", "numbered");
    spec.steps.forEach(function (s) { ol.appendChild(el("li", null, s)); });
    steps.appendChild(ol);
    section.appendChild(steps);

    var gates = el("div", "card gate-card");
    gates.appendChild(el("h2", null, "Why this is not available yet"));
    var ul = el("ul", "gates");
    ul.id = "gates-" + name;
    spec.gates.forEach(function (g) { ul.appendChild(el("li", null, g.text)); });
    gates.appendChild(ul);
    var row = el("div", "row");
    var btn = el("button", "primary", spec.title);
    btn.type = "button";
    btn.disabled = !spec.enabled;
    btn.setAttribute("aria-describedby", ul.id);
    row.appendChild(btn);
    row.appendChild(el("span", "muted", spec.enabled ? "" : "Not available in this preview"));
    gates.appendChild(row);
    section.appendChild(gates);
  }

  function reportText() {
    var lines = ["QuietTV Helper report (developer preview)", ""];
    var check = state.check;
    if (!check) {
      lines.push("TV check: not run yet.");
    } else if (check.error) {
      lines.push("TV check: " + check.error.message);
    } else {
      lines.push("TV check, at " + clock(check.checkedAt) + ":");
      lines.push("  Model: " + check.model + (check.modelSupported ? " (supported)" : " (not supported yet)"));
      lines.push("  Developer Mode: " + (check.developerMode === true ? "on" : check.developerMode === false ? "off" : "not reported"));
      if (check.deviceIdSha256) lines.push("  TV fingerprint: " + check.deviceIdSha256);
      lines.push("  Exact match: not proven. Missing: " +
                 (check.missingFacts || []).map(function (m) { return m.label; }).join(", ") + ".");
      lines.push("  Ready to install: no.");
    }
    lines.push("", "Changes made to the TV: none. This preview cannot change your TV.", "");
    Object.keys(state.actions).forEach(function (name) {
      var spec = state.actions[name];
      lines.push(spec.title + ": not available");
      spec.gates.forEach(function (g) { lines.push("  - " + g.text); });
    });
    return lines.join("\n");
  }

  function renderPreflight() {
    var pf = state.preflight;
    var card = $("preflight-card");
    if (!pf) { card.hidden = true; return; }
    card.hidden = false;
    var hint = $("preflight-hint");
    $("preflight-actions").hidden = !pf.configured;
    hint.hidden = pf.configured;
    if (!pf.configured) {
      hint.textContent = "Not configured. Start QuietTV Helper with --preflight-config <file> to check saved reports.";
      say("preflight-msg", "");
    } else if (pf.status === "success") {
      say("preflight-msg", "Saved report check complete.");
    } else if (pf.status === "error" && pf.error) {
      say("preflight-msg", pf.error.message, true);
    } else {
      say("preflight-msg", "");
    }

    var result = pf.configured && pf.status === "success" ? pf.result : null;
    $("preflight-result").hidden = !result;
    if (result) {
      var facts = $("preflight-facts");
      clear(facts);
      result.fields.forEach(function (f) {
        facts.appendChild(el("dt", null, f.label));
        var dd = el("dd");
        dd.appendChild(el("span", null, f.value));          // value as literal text
        dd.appendChild(el("span", "src", " — " + (SOURCE_LABELS[f.source] || f.source)));
        facts.appendChild(dd);
      });
      $("preflight-note").textContent = result.note;
      $("preflight-provenance").textContent =
        "Source: saved report files you configured, not a live connection. Profile " + result.profileId +
        "; identity scheme " + result.identityScheme + ". You asserted that you trust this policy and " +
        "profile and that both saved reports came from the same TV. This does not verify your current TV " +
        "and enables nothing.";
    }
  }

  function render() {
    if (!state) return;
    renderCheck();
    renderPreflight();
    Object.keys(state.actions).forEach(renderAction);
    $("report-text").textContent = reportText();
  }

  // ---- data ----

  function handle(result) {
    if (result.status === 403) { $("session-banner").hidden = false; return null; }
    if (result.data && result.data.actions) { state = result.data; render(); }
    return result;
  }

  function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    var job = state && state.job;
    if (job && (job.status === "running" || job.status === "cancelling")) {
      pollTimer = setTimeout(refresh, POLL_MS);
    }
  }

  function refresh() {
    return api("GET", "/api/state").then(handle).then(schedulePoll).catch(function () {
      say("form-msg", "QuietTV Helper stopped. Start it again to continue.", true);
    });
  }

  function onCheck(event) {
    event.preventDefault();
    var ip = $("tv-ip").value.trim();
    say("form-msg", "");
    api("POST", "/api/target", { ip: ip }).then(function (res) {
      if (!handle(res)) return null;
      if (!res.ok) { say("form-msg", res.data.error, true); return null; }
      return api("POST", "/api/check", { target: ip });
    }).then(function (res) {
      if (!res || !handle(res)) return;
      if (!res.ok) say("form-msg", res.data.error, true);
      schedulePoll();
    }).catch(function () {
      say("form-msg", "QuietTV Helper stopped. Start it again to continue.", true);
    });
  }

  function onCancel() {
    if (!state || !state.job) return;
    api("POST", "/api/cancel", { jobId: state.job.id }).then(handle).then(schedulePoll);
  }

  // The only caller of /api/preflight. The body is always the empty object: the sources are
  // fixed at startup, never chosen here.
  function onPreflight() {
    say("preflight-msg", "Checking saved reports...");
    api("POST", "/api/preflight", {}).then(function (res) {
      if (!handle(res)) return;
      if (!res.ok && res.data && res.data.error) say("preflight-msg", res.data.error, true);
    }).catch(function () {
      say("preflight-msg", "QuietTV Helper stopped. Start it again to continue.", true);
    });
  }

  function onCopy() {
    var text = $("report-text").textContent;
    if (!navigator.clipboard) { say("copy-msg", "Select the text and copy it.", true); return; }
    navigator.clipboard.writeText(text).then(function () { say("copy-msg", "Copied."); },
      function () { say("copy-msg", "Select the text and copy it.", true); });
  }

  function show(screen) {
    var items = document.querySelectorAll(".nav-item");
    Array.prototype.forEach.call(items, function (item) {
      var on = item.getAttribute("data-screen") === screen;
      if (on) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
      $("screen-" + item.getAttribute("data-screen")).hidden = !on;
    });
    var heading = $("h-" + screen);
    if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus(); }
  }

  function start() {
    takeToken();
    var items = document.querySelectorAll(".nav-item");
    Array.prototype.forEach.call(items, function (item) {
      item.addEventListener("click", function () { show(item.getAttribute("data-screen")); });
    });
    $("check-form").addEventListener("submit", onCheck);
    $("cancel-btn").addEventListener("click", onCancel);
    $("preflight-btn").addEventListener("click", onPreflight);
    $("copy-btn").addEventListener("click", onCopy);
    if (!token) { $("session-banner").hidden = false; return; }
    refresh();  // local state only; this never contacts the TV
  }

  document.addEventListener("DOMContentLoaded", start);
})();
