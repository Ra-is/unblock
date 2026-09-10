const $ = (id) => document.getElementById(id);
const escape = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const names = {
  invoice: "Invoice",
  purchase_order: "Purchase order",
  delivery_receipt: "Signed delivery confirmation",
  missing: "Missing",
  needs_correction: "Needs correction",
  satisfied: "Matched",
  accepted: "Matched",
  pending: "Not checked",
  blocked: "Evidence needed",
  ready_for_review: "Ready for review",
  reviewed: "Packet accepted",
  idle: "Ready",
  running: "Reviewing evidence",
  queued: "Review queued",
  failed: "Delivery failed",
  sent: "Sent · awaiting reply",
  draft: "Not sent",
  resolved: "Evidence received",
  held: "Held for review",
  dismissed: "Dismissed",
  unexpected_sender: "Sender is not the case contact",
  failed_security_checks: "Failed spam or virus checks",
  email_authentication_failed: "Sender authenticity could not be verified",
  ambiguous_sender: "Message has more than one sender",
  no_usable_attachment: "Reply had no usable attachment",
  case_already_reviewed: "Case was already reviewed",
};
const agentStatusText = {
  idle: "Ready",
  running: "Reviewing evidence",
  queued: "Review queued",
  failed: "Review interrupted",
};
const badge = (value) =>
  `<span class="badge ${escape(value)}">${escape(names[value] || value)}</span>`;
let config,
  user,
  cases = [],
  selected,
  activeTab = "evidence",
  audit = [],
  token = sessionStorage.getItem("unblock_token");
let busy = false,
  refreshTimer,
  createKey = crypto.randomUUID();

function notify(text) {
  $("notice").textContent = text;
  $("notice").hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => ($("notice").hidden = true), 7000);
}
async function api(path, options = {}) {
  const headers = {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  };
  if (options.body && !(options.body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: "The request could not be completed." }));
    if (response.status === 401) {
      sessionStorage.removeItem("unblock_token");
      token = null;
      showLogin();
    }
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : "Check the form fields and try again.",
    );
  }
  return response;
}
async function json(path, options) {
  return (await api(path, options)).json();
}
function showLogin() {
  $("login").hidden = false;
  $("workspace").hidden = true;
  $("signout").hidden = true;
  clearTimeout(refreshTimer);
}
function base64url(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}
async function signin() {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(48)));
  const state = crypto.randomUUID();
  sessionStorage.setItem("pkce", verifier);
  sessionStorage.setItem("oauth_state", state);
  const challenge = base64url(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)),
  );
  const p = new URLSearchParams({
    client_id: config.clientId,
    response_type: "code",
    scope: "openid profile",
    redirect_uri: location.origin + "/",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.assign(config.authDomain + "/oauth2/authorize?" + p);
}
async function callback() {
  const p = new URLSearchParams(location.search);
  if (p.has("error"))
    throw new Error("Sign-in was not completed. Please try again.");
  if (!p.has("code")) return;
  if (p.get("state") !== sessionStorage.getItem("oauth_state"))
    throw new Error("Sign-in state did not match. Please sign in again.");
  const response = await fetch(config.authDomain + "/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.clientId,
      redirect_uri: location.origin + "/",
      code: p.get("code"),
      code_verifier: sessionStorage.getItem("pkce") || "",
    }),
  });
  if (!response.ok) throw new Error("Sign-in expired. Please try again.");
  const data = await response.json();
  token = data.id_token;
  sessionStorage.setItem("unblock_token", token);
  sessionStorage.removeItem("pkce");
  sessionStorage.removeItem("oauth_state");
  history.replaceState(null, "", "/");
}
function money(c) {
  return new Intl.NumberFormat("en", {
    style: "currency",
    currency: c.currency,
  }).format(c.amount_minor / 100);
}
function date(v) {
  return new Date(v).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
async function refresh(force = false) {
  const previousVersion = selected?.version;
  const previousId = selected?.id;
  cases = await json("/api/cases");
  if (selected) selected = cases.find((c) => c.id === selected.id);
  if (!selected && cases.length) selected = cases[0];
  renderList();
  const expiredLease =
    selected?.agent_lease_until &&
    new Date(selected.agent_lease_until) <= new Date() &&
    $("run-agent")?.disabled &&
    !selected.review;
  if (
    selected &&
    (force ||
      expiredLease ||
      selected.id !== previousId ||
      selected.version !== previousVersion)
  ) {
    audit = await json(`/api/cases/${selected.id}/audit`);
    renderDetail();
  }
  schedule();
}
function schedule() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    if (!document.querySelector("dialog[open]") && !busy)
      refresh().catch((error) => {
        notifyError(error);
        if (token || config.local) schedule();
      });
    else schedule();
  }, 5000);
}
function renderList() {
  const term = $("search").value.toLowerCase();
  $("nav-count").textContent = cases.length;
  $("metric-open").textContent = cases.filter(
    (c) => c.status !== "reviewed",
  ).length;
  $("metric-blocked").textContent = cases.filter(
    (c) => c.status === "blocked",
  ).length;
  $("metric-ready").textContent = cases.filter(
    (c) => c.status === "ready_for_review",
  ).length;
  $("metric-reviewed").textContent = cases.filter(
    (c) => c.status === "reviewed",
  ).length;
  $("case-list").innerHTML =
    cases
      .filter((c) =>
        (c.supplier + " " + c.invoice_ref).toLowerCase().includes(term),
      )
      .map(
        (c) =>
          `<button class="case-card ${selected?.id === c.id ? "selected" : ""}" data-case="${c.id}"><span class="ref">${escape(c.invoice_ref)} · ${escape(c.order_ref)}</span><strong>${escape(c.supplier)}</strong><div class="card-bottom">${badge(c.status)}<span class="money">${escape(money(c))}</span></div></button>`,
      )
      .join("") ||
    '<p class="no-cases">No cases yet. Open an invoice case to start collecting evidence.</p>';
  document.querySelectorAll("[data-case]").forEach(
    (b) =>
      (b.onclick = async () => {
        selected = cases.find((c) => c.id === b.dataset.case);
        activeTab = "evidence";
        audit = await json(`/api/cases/${selected.id}/audit`);
        renderList();
        renderDetail();
      }),
  );
}
function renderDetail() {
  if (!selected) return;
  const c = selected;
  const processing =
    ["running", "queued"].includes(c.agent_status) &&
    (!c.agent_lease_until || new Date(c.agent_lease_until) > new Date());
  const locked = processing || c.review || user.demo;
  const held = c.quarantine || [];
  const openHeld = held.filter((q) => q.status === "held").length;
  const waiting = c.awaiting_reply && !processing && !c.review;
  const requirements = c.requirements
    .map(
      (r) =>
        `<div class="requirement"><span class="requirement-name"><span class="check ${r.status === "satisfied" ? "ok" : ""}">${r.status === "satisfied" ? "✓" : "·"}</span>${names[r.kind]}</span>${badge(r.status)}</div>`,
    )
    .join("");
  let body = "";
  if (activeTab === "evidence") {
    body =
      requirements +
      `<div class="upload"><div><strong>Add supporting evidence</strong><p>PDF, TXT, PNG or JPEG · up to 4 MB</p></div><button class="secondary" id="choose-file" ${locked ? "disabled" : ""}>Choose file</button><input id="upload-file" type="file" accept=".pdf,.txt,.png,.jpg,.jpeg" hidden ${locked ? "disabled" : ""}></div>` +
      c.documents
        .map(
          (d) =>
            `<article class="document"><div class="document-title"><button class="text-button" data-download="${d.id}">↧ ${escape(d.filename)}</button>${badge(d.status)}</div>${d.issues.map((i) => `<p>↳ ${escape(i)}</p>`).join("")}${d.extraction ? `<dl><dt>Order</dt><dd>${escape(d.extraction.order_ref || "Not identified")}</dd><dt>Supplier</dt><dd>${escape(d.extraction.supplier || "Not identified")}</dd></dl><blockquote>${escape(d.extraction.source_quote || "No readable source excerpt.")}</blockquote>` : ""}</article>`,
        )
        .join("");
  }
  if (activeTab === "requests") {
    body =
      `<p class="muted">${c.sample ? "Simulated correspondence illustrating the workflow. No email was sent for this sample." : "The agent emails the contact on this case. The supplier replies to that email with the corrected document; the reply resumes the review automatically. Reply routing addresses are kept private."}</p>` +
      c.requests
        .map(
          (r) =>
            `<article class="request"><div class="document-title"><strong>${escape(names[r.requirement])}</strong>${badge(r.status)}</div><p class="muted">${c.sample ? "Sample recipient" : "To"}: ${escape(r.recipient)}${r.sent_at ? (c.sample ? " · simulated send " : " · sent ") + date(r.sent_at) : ""}</p>${r.blocked_reason ? `<p class="warn">Not delivered: ${escape(r.blocked_reason)}</p>` : ""}<strong>${escape(r.subject)}</strong><p>${escape(r.body)}</p><button class="secondary" data-copy="${r.id}" ${c.sample || r.status === "resolved" ? "disabled" : ""}>Copy request</button></article>`,
        )
        .join("");
    if (!c.requests.length)
      body +=
        '<p class="muted">Run the evidence review. The agent sends a request for each outstanding document.</p>';
  }
  if (activeTab === "held") {
    body =
      '<p class="muted">Inbound messages the agent refused to treat as evidence. Each one needs a person to decide.</p>' +
      held
        .map(
          (q) =>
            `<article class="request"><div class="document-title"><strong>${escape(names[q.reason] || q.reason)}</strong>${badge(q.status)}</div><p class="muted">From: ${escape(q.sender || "unknown")} · ${date(q.at)}</p><strong>${escape(q.subject)}</strong><p class="warn">This message was not added as evidence. ${q.reason === "email_authentication_failed" ? "Ask the supplier to resend from an authenticated mailbox." : "Review the sender and request a verified replacement if needed."}</p>${q.detail ? `<details><summary>Email check details</summary><p class="muted">${escape(q.detail)}</p></details>` : ""}${q.status === "held" ? `<button class="secondary" data-dismiss="${q.id}" ${user.reviewer ? "" : "disabled"}>Dismiss message</button>` : `<p class="muted">Dismissed by ${escape(q.decided_by || "a reviewer")}</p>`}</article>`,
        )
        .join("");
    if (!held.length)
      body += '<p class="muted">No inbound messages have been held.</p>';
  }
  if (activeTab === "activity") {
    body = `<div class="timeline">${[...audit]
      .reverse()
      .map(
        (e) =>
          `<div class="event"><p>${escape(e.message)}</p><small>${date(e.at)} · ${{ agent: "Unblock agent", worker: "Background worker", supplier: "Supplier reply", inbound: "Inbound mail" }[e.actor] || "Team member"}</small></div>`,
      )
      .join("")}</div>`;
  }
  $("case-detail").innerHTML =
    `<div class="detail-header"><div><span class="eyebrow">${escape(c.invoice_ref)}</span><h2>${escape(c.supplier)}</h2><span class="muted">Opened ${date(c.created_at)}</span></div>${badge(c.status)}</div><div class="detail-meta"><span>Invoice <strong>${escape(money(c))}</strong></span><span>Order <strong>${escape(c.order_ref)}</strong></span><span>Evidence <strong>${c.requirements.filter((r) => r.status === "satisfied").length}/3</strong></span></div><div class="agent-bar ${waiting ? "waiting" : ""}"><div><strong><span class="pulse"></span>${escape(waiting ? "Waiting for supplier reply" : agentStatusText[c.agent_status] || c.agent_status)}</strong><p>${processing ? "You can leave this page. The review continues in the background." : waiting ? "The agent has asked the supplier and stopped. A reply reopens this case on its own." : "Check documents and send the next follow-up."}</p></div><button id="run-agent" class="primary" ${locked ? "disabled" : ""}>${processing ? "Review in progress…" : "Review evidence →"}</button></div>${c.agent_summary ? `<div class="summary">${escape(c.agent_summary)}</div>` : ""}<nav class="tabs" aria-label="Case views">${[
      ["evidence", "Evidence"],
      ["requests", "Requests"],
      ["held", "Held"],
      ["activity", "Activity"],
    ]
      .map(
        ([id, label]) =>
          `<button class="tab ${activeTab === id ? "selected" : ""}" data-tab="${id}">${label}${id === "requests" ? " · " + c.requests.length : ""}${id === "held" && openHeld ? " · " + openHeld : ""}</button>`,
      )
      .join(
        "",
      )}</nav>${body}<div class="detail-footer"><button id="export" class="secondary">↓ Export evidence packet</button>${c.review ? '<span class="badge reviewed">Accepted by reviewer</span>' : `<button id="review" class="primary" ${c.status !== "ready_for_review" || processing || !user.reviewer ? "disabled" : ""}>Accept packet</button>`}</div>`;
  document.querySelectorAll("[data-tab]").forEach(
    (b) =>
      (b.onclick = () => {
        activeTab = b.dataset.tab;
        renderDetail();
      }),
  );
  $("run-agent").onclick = () =>
    action(async () => {
      await json(`/api/cases/${c.id}/run`, { method: "POST" });
      await refresh();
    });
  if ($("choose-file"))
    $("choose-file").onclick = () => $("upload-file").click();
  if ($("upload-file"))
    $("upload-file").onchange = () =>
      action(async () => {
        const file = $("upload-file").files[0];
        if (!file) return;
        if (file.size > 4 * 1024 * 1024)
          throw new Error("Please choose a document smaller than 4 MB.");
        const form = new FormData();
        form.append("file", file);
        await json(`/api/cases/${c.id}/documents`, {
          method: "POST",
          body: form,
        });
        await refresh();
        notify(
          "Document received. Run Review evidence when your files are ready.",
        );
      });
  document
    .querySelectorAll("[data-download]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          action(() =>
            download(
              `/api/cases/${c.id}/documents/${b.dataset.download}`,
              c.documents.find((d) => d.id === b.dataset.download).filename,
            ),
          )),
    );
  document.querySelectorAll("[data-copy]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          const r = c.requests.find((r) => r.id === b.dataset.copy);
          await navigator.clipboard.writeText(
            `To: ${r.recipient}\nSubject: ${r.subject}\n\n${r.body}`,
          );
          notify("Request copied.");
        })),
  );
  document.querySelectorAll("[data-dismiss]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          await json(
            `/api/cases/${c.id}/quarantine/${b.dataset.dismiss}/dismiss`,
            {
              method: "POST",
              body: JSON.stringify({ version: c.version }),
            },
          );
          await refresh(true);
          notify("Held message dismissed.");
        })),
  );
  $("export").onclick = () =>
    action(() =>
      download(`/api/cases/${c.id}/packet`, `${c.invoice_ref}-evidence.json`),
    );
  if ($("review"))
    $("review").onclick = () => {
      $("review-note").value = "";
      $("review-dialog").showModal();
    };
}
async function download(path, filename) {
  const response = await api(path);
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function notifyError(e) {
  notify(e.message || "Something went wrong. Please retry.");
}
async function action(fn) {
  if (busy) return;
  busy = true;
  try {
    await fn();
  } catch (e) {
    notifyError(e);
  } finally {
    busy = false;
  }
}
function openCreate() {
  createKey = crypto.randomUUID();
  $("create-dialog").showModal();
}
$("new-case").onclick = openCreate;
$("empty-create").onclick = openCreate;
$("close-create").onclick = () => $("create-dialog").close();
$("close-review").onclick = () => $("review-dialog").close();
$("signin").onclick = () => action(signin);
$("try-demo").onclick = () =>
  action(async () => {
    const data = await json("/api/demo/session", { method: "POST" });
    token = data.token;
    sessionStorage.setItem("unblock_token", token);
    await start();
  });
$("search").oninput = renderList;
$("refresh").onclick = () => action(refresh);
$("signout").onclick = () => {
  sessionStorage.removeItem("unblock_token");
  token = null;
  if (config.authDomain)
    location.assign(
      config.authDomain +
        "/logout?" +
        new URLSearchParams({
          client_id: config.clientId,
          logout_uri: location.origin + "/",
        }),
    );
  else showLogin();
};
$("create-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const values = Object.fromEntries(new FormData(e.target));
    const parts = values.amount.split(".");
    values.amount_minor =
      Number(parts[0]) * 100 + Number((parts[1] || "").padEnd(2, "0"));
    delete values.amount;
    selected = await json("/api/cases", {
      method: "POST",
      headers: { "Idempotency-Key": createKey },
      body: JSON.stringify(values),
    });
    $("create-dialog").close();
    e.target.reset();
    activeTab = "evidence";
    await refresh(true);
    notify("Case opened. Add the invoice and supporting documents.");
  });
};
$("review-form").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    await json(`/api/cases/${selected.id}/review`, {
      method: "POST",
      body: JSON.stringify({
        version: selected.version,
        note: $("review-note").value,
      }),
    });
    $("review-dialog").close();
    await refresh();
    notify("Evidence packet accepted.");
  });
};
async function start() {
  config = await (await fetch("/config")).json();
  $("environment").textContent = config.local
    ? "Local development"
    : "Private workspace";
  await callback();
  if (!token && !config.local) {
    $("try-demo").hidden = !config.demoAvailable;
    $("demo-hint").hidden = !config.demoAvailable;
    showLogin();
    return;
  }
  user = await json("/api/me");
  $("workspace").hidden = false;
  $("login").hidden = true;
  $("signout").hidden = config.local && !user.demo;
  $("demo-banner").hidden = !user.demo;
  $("new-case").hidden = user.demo;
  await refresh();
}
start().catch((e) => {
  showLogin();
  notifyError(e);
});
