"use strict";
// Ridgeline CS Copilot Console: plain JavaScript, no dependencies. Text from the server is always set with
// textContent (never innerHTML), so nothing a model or a record says can become markup.

const $ = (id) => document.getElementById(id);
const HEADERS = { "Content-Type": "application/json", "X-Ridgeline-Console": "1" };
const STATUS_TEXT = {
  running: "Running", answered: "Answered", escalated: "Escalated to a person",
  rejected: "Rejected", error: "Error",
};
let config = null;
let pollTimer = null;
let currentRun = null;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

async function api(path, options) {
  const resp = await fetch(path, options);
  let body = {};
  try { body = await resp.json(); } catch (e) { /* empty or non-JSON body */ }
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

async function loadConfig() {
  config = await api("/api/config");
  const role = $("role");
  config.roles.forEach((r) => { const o = el("option", r.label); o.value = r.id; role.appendChild(o); });
  const model = $("model");
  config.models.forEach((m) => {
    const o = el("option", m.available ? m.label : `${m.label} (not running)`);
    o.value = m.id;
    o.disabled = !m.available;
    model.appendChild(o);
  });
  const examples = $("examples");
  config.examples.forEach((q) => { const o = el("option", q); o.value = q; examples.appendChild(o); });
  updateHint();
}

function updateHint() {
  const model = $("model").value;
  const hints = {
    scripted: "Scripted: answers the example questions only (instant).",
    local: "Local model: any question; about 1-2 minutes on a laptop CPU.",
    hosted: "Hosted model: any question; paid, uses your own API key.",
  };
  $("model-hint").textContent = hints[model] || "";
}

async function ask(event) {
  event.preventDefault();
  const error = $("ask-error");
  error.hidden = true;
  const question = $("question").value.trim();
  if (!question) { error.textContent = "Type a question or pick an example."; error.hidden = false; return; }
  $("ask-button").disabled = true;
  try {
    const run = await api("/api/runs", {
      method: "POST", headers: HEADERS,
      body: JSON.stringify({ role: $("role").value, model: $("model").value, question }),
    });
    currentRun = run.id;
    resetRunView();
    render(run);
    poll();
  } catch (e) {
    error.textContent = e.message;
    error.hidden = false;
    $("ask-button").disabled = false;
  }
}

function resetRunView() {
  $("run").hidden = false;
  ["steps", "tools", "recall", "audit-rows"].forEach((id) => { $(id).textContent = ""; });
  $("audit").hidden = true;
  $("audit-note").hidden = true;
  $("citation").hidden = true;
  $("answer-block").hidden = true;
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!currentRun) return;
    try {
      const run = await api(`/api/runs/${encodeURIComponent(currentRun)}`);
      render(run);
      if (run.status === "running") poll(); else $("ask-button").disabled = false;
    } catch (e) {
      $("run-status").textContent = `Lost the run: ${e.message}`;
      $("ask-button").disabled = false;
    }
  }, 1000);
}

function render(run) {
  $("run-id").textContent = run.id;
  const status = $("run-status");
  status.textContent = STATUS_TEXT[run.status] || run.status;
  status.className = `status ${run.status}`;
  $("run-meta").textContent = `${run.role} · ${run.model} · ${run.elapsed_s}s` +
    (run.iterations ? ` · ${run.iterations} iterations` : "") + (run.cost_usd ? ` · $${run.cost_usd.toFixed(4)}` : "");
  $("trace-id").textContent = run.trace_id || "(pending)";
  $("audit-button").disabled = !run.trace_id || run.status === "running";

  renderSteps(run.steps);
  renderTools(run.tools);
  renderRecall(run.recalled, run.status);
  renderApproval(run.approval);
  if (run.status !== "running") renderAnswer(run);
}

function renderSteps(steps) {
  const list = $("steps");
  for (let i = list.children.length; i < steps.length; i += 1) {
    const s = steps[i];
    const li = el("li", null, `step ${s.layer} ${s.status === "ok" ? "" : "bad"}`);
    li.appendChild(el("span", s.layer, "layer"));
    li.appendChild(el("span", s.name.split(".").slice(1).join("."), "name"));
    const detail = Object.entries(s.attrs || {}).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(", ") : v}`).join("  ");
    if (detail) li.appendChild(el("span", detail, "detail"));
    li.appendChild(el("span", `${Math.round(s.ms)} ms`, "ms"));
    list.appendChild(li);
  }
  $("steps-count").textContent = steps.length ? `(${steps.length})` : "";
}

function renderTools(tools) {
  const body = $("tools");
  body.textContent = "";
  tools.forEach((t) => {
    const tr = el("tr");
    tr.appendChild(el("td", t.tool, "mono"));
    tr.appendChild(el("td", t.ok ? "allowed, data returned" : (t.error || "refused"), t.ok ? "ok" : "bad"));
    tr.appendChild(el("td", Math.round(t.ms), "num"));
    body.appendChild(tr);
  });
  $("tools-empty").hidden = tools.length > 0;
}

function renderRecall(recalled, status) {
  const list = $("recall");
  list.textContent = "";
  recalled.forEach((m) => {
    const li = el("li");
    li.appendChild(el("span", `${m.kind} · similarity ${Number(m.score).toFixed(2)}`, "tag"));
    li.appendChild(el("span", m.text));
    list.appendChild(li);
  });
  const empty = $("recall-empty");
  empty.hidden = recalled.length > 0;
  empty.textContent = status === "running" ? "Nothing recalled yet." : "Nothing similar asked before by callers with this access.";
}

function renderApproval(approval) {
  const box = $("approval");
  box.textContent = "";
  if (!approval) { box.hidden = true; return; }
  box.hidden = false;
  box.className = `approval ${approval.state}`;
  if (approval.state === "waiting") {
    box.appendChild(el("strong", "Waiting for a human decision"));
    box.appendChild(el("span", ` · the request expires in about ${Math.round(approval.expires_in_s || 0)} s. `));
    if (approval.url) {
      const a = el("a", "Open the approval form", "button-link");
      a.href = approval.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      box.appendChild(a);
    }
  } else if (approval.state === "approved") {
    box.appendChild(el("strong", `Case ${approval.case_id || ""} created`));
    box.appendChild(el("span", approval.approved_by ? ` · approved by ${approval.approved_by}` : ""));
  } else if (approval.state === "not_created") {
    box.appendChild(el("strong", "No case was created"));
    box.appendChild(el("span", ` · ${approval.reason}`));
  } else {
    box.appendChild(el("span", approval.note || "Decision recorded."));
  }
}

function renderAnswer(run) {
  $("answer-block").hidden = false;
  const answer = $("answer");
  answer.textContent = "";
  const text = run.error ? `The run failed: ${run.error}` : run.answer;
  const parts = text.split(/(\[[^\[\]\s]+\.md#\d+\])/);
  parts.forEach((part) => {
    const m = part.match(/^\[([^\[\]\s]+\.md#\d+)\]$/);
    if (m) {
      const b = el("button", m[1], "cite");
      b.type = "button";
      b.addEventListener("click", () => showCitation(m[1]));
      answer.appendChild(b);
    } else {
      answer.appendChild(document.createTextNode(part));
    }
  });
}

async function showCitation(cite) {
  const box = $("citation");
  box.textContent = "";
  try {
    const chunk = await api(`/api/citations/${encodeURIComponent(cite)}`);
    box.appendChild(el("div", `${cite} (${chunk.source})`, "cite-title"));
    box.appendChild(el("pre", chunk.text));
  } catch (e) {
    box.appendChild(el("div", `Could not load ${cite}: ${e.message}`));
  }
  box.hidden = false;
}

async function showAudit() {
  if (!currentRun) return;
  const note = $("audit-note");
  try {
    const data = await api(`/api/runs/${encodeURIComponent(currentRun)}/audit`);
    const body = $("audit-rows");
    body.textContent = "";
    data.rows.forEach((r) => {
      const tr = el("tr");
      tr.appendChild(el("td", String(r.at || "").slice(0, 19).replace("T", " "), "mono"));
      tr.appendChild(el("td", r.client_id || "-"));
      tr.appendChild(el("td", r.tool, "mono"));
      tr.appendChild(el("td", r.outcome, r.outcome === "ok" ? "ok" : "bad"));
      tr.appendChild(el("td", r.reason || "-"));
      tr.appendChild(el("td", JSON.stringify(r.args || {}), "mono small"));
      body.appendChild(tr);
    });
    $("audit").hidden = data.rows.length === 0;
    note.hidden = data.rows.length > 0;
    note.textContent = "No audit rows for this trace in the latest 200 gateway calls.";
  } catch (e) {
    note.textContent = e.message;
    note.hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("ask-form").addEventListener("submit", ask);
  $("model").addEventListener("change", updateHint);
  $("examples").addEventListener("change", (e) => { if (e.target.value) $("question").value = e.target.value; });
  $("audit-button").addEventListener("click", showAudit);
  loadConfig().catch((e) => {
    $("ask-error").textContent = `Could not load the console: ${e.message}`;
    $("ask-error").hidden = false;
  });
});
