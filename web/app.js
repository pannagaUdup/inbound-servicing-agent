"use strict";
/* CreditNirvana servicing console — vanilla JS, same-origin, all real endpoints. */
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
async function api(url, opts) {
  const r = await fetch(url, opts);
  const b = await r.json().catch(() => ({}));
  if (!r.ok) throw b;
  return b;
}
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600); }
const setHTML = (id, html) => { $("#v-" + id).innerHTML = html; };
const rs = v => "₹" + Number(v || 0).toLocaleString("en-IN");
const ago = iso => {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now"; if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago"; return Math.round(s / 86400) + "d ago";
};

/* theme */
(function theme() {
  const root = document.documentElement;
  try { const s = localStorage.getItem("cn-theme"); if (s) root.setAttribute("data-theme", s); } catch (e) {}
  const sync = () => { const cur = root.getAttribute("data-theme"); document.querySelectorAll(".theme-sw button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.theme === cur))); };
  document.querySelectorAll(".theme-sw button").forEach(b => b.onclick = () => { root.setAttribute("data-theme", b.dataset.theme); try { localStorage.setItem("cn-theme", b.dataset.theme); } catch (e) {} sync(); });
  sync();
})();

/* nav */
const RENDER = {};
let CURRENT = "convs";
document.querySelectorAll("#nav button[role=tab]").forEach(t => t.onclick = () => {
  document.querySelectorAll("#nav button[role=tab]").forEach(x => x.setAttribute("aria-selected", String(x === t)));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === "v-" + t.dataset.v));
  CURRENT = t.dataset.v;
  (RENDER[t.dataset.v] || (() => {}))();
});
function go(v) { const b = document.querySelector(`#nav button[data-v="${v}"]`); if (b) b.click(); }

async function health() { try { const h = await api("/healthz"); $("#health").textContent = `${h.accounts} accounts · ${h.planner}`; } catch (e) { $("#health").textContent = "offline"; } }
async function badges() {
  try { const o = await api("/admin/api/overview");
    const q = $("#c-queues"), c = $("#c-cases");
    if (o.open_tickets) { q.textContent = o.open_tickets; q.hidden = false; } else q.hidden = true;
    if (o.open_cases) { c.textContent = o.open_cases; c.hidden = false; } else c.hidden = true;
  } catch (e) {}
}

/* ---- inline SVG helpers ---- */
function chart(days, bars, line, lineMax) {
  // bars scale to their own max; line scales to lineMax (default 100, for a 0-100 rate). Pass a count
  // max to overlay a second count series (e.g. tickets resolved) on the same scale as the bars.
  const W = 620, H = 170, pl = 8, pr = 8, pt = 10, pb = 20, iw = W - pl - pr, ih = H - pt - pb;
  const maxB = Math.max(1, ...bars), n = Math.max(1, bars.length), bw = iw / n;
  const lmax = lineMax || 100, unit = lineMax ? "" : "%";
  let g = "";
  for (let i = 0; i <= 3; i++) { const y = pt + ih * i / 3; g += `<line class="gl" x1="${pl}" y1="${y.toFixed(1)}" x2="${W - pr}" y2="${y.toFixed(1)}"/>`; }
  bars.forEach((v, i) => { const bh = ih * v / maxB, x = pl + i * bw + bw * 0.15, y = pt + ih - bh; g += `<rect class="bm" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw * 0.7).toFixed(1)}" height="${Math.max(0, bh).toFixed(1)}" rx="2"><title>${esc(days[i])}: ${v}</title></rect>`; });
  if (line && line.some(v => v)) {
    const pts = line.map((v, i) => `${(pl + i * bw + bw / 2).toFixed(1)},${(pt + ih - ih * Math.min(lmax, v) / lmax).toFixed(1)}`);
    g += `<polyline class="ln" points="${pts.join(" ")}"/>`;
    line.forEach((v, i) => { g += `<circle class="dot" cx="${(pl + i * bw + bw / 2).toFixed(1)}" cy="${(pt + ih - ih * Math.min(lmax, v) / lmax).toFixed(1)}" r="2.3"><title>${esc(days[i])}: ${v}${unit}</title></circle>`; });
  }
  g += `<text class="ax" x="${pl}" y="${H - 5}">${esc(days[0] || "")}</text><text class="ax" x="${W - pr}" y="${H - 5}" text-anchor="end">${esc(days[days.length - 1] || "")}</text>`;
  return `<svg class="svgc" viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}
function ring(pct, cls = "") {
  const r = 16, c = 2 * Math.PI * r, off = c * (1 - Math.max(0, Math.min(1, pct / 100)));
  return `<svg class="ring ${cls}" viewBox="0 0 40 40"><circle class="rbg" cx="20" cy="20" r="${r}"/><circle class="rfg" cx="20" cy="20" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/><text x="20" y="24" text-anchor="middle">${Math.round(pct)}</text></svg>`;
}
const pg = (h1, p) => `<div class="pg"><h1>${esc(h1)}</h1><p>${esc(p)}</p></div>`;

/* ============ Pulse (ops) ============ */
RENDER.ops = async function () {
  const [p, o] = await Promise.all([api("/admin/api/metrics/pulse?days=29"), api("/admin/api/overview")]);
  const h = p.headline, s = p.series;
  const auto = Math.round(h.zero_touch_rate), man = 100 - auto;
  setHTML("ops", pg("Pulse", "Live operational health — computed from the real audit trail.") + `
    <div class="hero">
      <div class="h-left">
        <div class="h-num">${auto}<span>%</span></div>
        <p class="h-lede">of conversations handled <b>end to end by the agent</b>, no human touch (last 29 days).</p>
        <div class="h-flow"><span class="seg s-bot" style="flex:${auto}"></span><span class="seg s-man" style="flex:${man || 1}"></span></div>
        <ul class="h-legend"><li><i class="s-bot"></i>Zero-touch<b>${auto}%</b></li><li><i class="s-man"></i>Handed off<b>${man}%</b></li></ul>
      </div>
      <div class="h-right">
        <div class="h-stat"><b>${h.conversations}</b><p>conversations<br><em>${h.verified} verified</em></p></div>
        <div class="h-stat"><b>${h.first_response_p95_s}<span>s</span></b><p>first reply p95<br><em>p50 ${h.first_response_p50_s}s</em></p></div>
      </div>
    </div>
    <div class="chips5">
      ${chip(o.accounts, "accounts")}${chip(o.campaigns, "campaigns")}${chip(h.fake_proofs_blocked, "fake proofs blocked", "ok")}
      ${chip(h.injections_blocked, "injections blocked", "ok")}${chip(h.shared_before_verification, "shared before verify", "ok")}
    </div>
    <div class="duo">
      <div class="card"><header><h2>Messages</h2><span class="key">in/out · <i class="k-bar"></i>bars</span></header>${chart(s.days.map(d => d.slice(5)), s.inbound.map((v, i) => v + s.outbound[i]), null)}</div>
      <div class="card"><header><h2>Zero-touch rate</h2><span class="key"><i class="k-line"></i>% auto</span></header>${chart(s.days.map(d => d.slice(5)), s.conversations, s.zero_touch_rate)}</div>
    </div>
    <div class="duo" style="margin-top:16px">
      <div class="card"><header><h2>Tickets created vs resolved</h2><span class="key"><i class="k-bar"></i>created <i class="k-line"></i>resolved</span></header>${chart(s.days.map(d => d.slice(5)), s.tickets_created, s.tickets_resolved, Math.max(1, ...s.tickets_created, ...s.tickets_resolved))}</div>
      <div class="card"><header><h2>Verifications</h2></header>${chart(s.days.map(d => d.slice(5)), s.verified, null)}</div>
    </div>`);
};
const chip = (v, l, cls = "") => `<div><span>${esc(l)}</span><b class="num">${v}</b><em class="${cls}">${cls === "ok" ? "✓ enforced" : ""}</em></div>`;

/* ============ Handoff desk (queues) ============ */
let deskGroup = "", deskSel = null;
RENDER.queues = async function () {
  const all = await api("/admin/api/tickets?status=open");
  const groups = {};
  all.forEach(t => (groups[t.group] = (groups[t.group] || 0) + 1));
  const gkeys = Object.keys(groups).sort();
  const list = deskGroup ? all.filter(t => t.group === deskGroup) : all;
  if (!deskSel || !list.find(t => t.ticket_id === deskSel)) deskSel = list[0] ? list[0].ticket_id : null;
  const groupsHtml = `<button data-g="" aria-pressed="${deskGroup === ""}">All<span>${all.length}</span></button>` +
    gkeys.map(g => `<button data-g="${esc(g)}" aria-pressed="${deskGroup === g}">${esc(g)}<span>${groups[g]}</span></button>`).join("");
  const listHtml = list.length ? list.map(t => deskCard(t, t.ticket_id === deskSel)).join("")
    : `<div class="zero"><b>All clear</b>No open tickets in this group.</div>`;
  const sel = list.find(t => t.ticket_id === deskSel);
  setHTML("queues", pg("Handoff desk", "Routed requests waiting for a human — all assigned to one ops user.") + `
    <div class="desk">
      <div class="d-groups">${groupsHtml}</div>
      <div class="d-list">${listHtml}</div>
      <div>${sel ? deskDetail(sel) : `<div class="b"><div class="zero"><b>Nothing selected</b>Pick a ticket.</div></div>`}</div>
    </div>`);
  $("#v-queues").querySelectorAll(".d-groups button").forEach(b => b.onclick = () => { deskGroup = b.dataset.g; RENDER.queues(); });
  $("#v-queues").querySelectorAll(".d-case").forEach(b => b.onclick = () => { deskSel = b.dataset.id; RENDER.queues(); });
  const rb = $("#v-queues .cp-acts .go"); if (rb) rb.onclick = () => resolveTicket(deskSel);
};
function slaInfo(t) {
  if (!t.sla_due) return { pct: 100, cls: "", label: "no SLA" };
  const due = new Date(t.sla_due).getTime(), made = new Date(t.created_at).getTime();
  const total = Math.max(1, due - made), left = due - Date.now(), pct = Math.max(0, Math.min(100, 100 * left / total));
  if (left < 0) return { pct: 0, cls: "over", label: "overdue " + Math.abs(Math.round(left / 3.6e6)) + "h" };
  const cls = pct < 25 ? "hot" : pct < 55 ? "warm" : "";
  return { pct, cls, label: "in " + Math.round(left / 3.6e6) + "h" };
}
function deskCard(t, on) {
  const s = slaInfo(t);
  return `<button class="d-case ${on ? "on" : ""}" data-id="${esc(t.ticket_id)}">
    ${ring(s.pct, s.cls)}
    <div class="dc"><div class="dc-t"><span class="pri p-${t.priority}">${t.priority}</span>${esc(t.borrower_name || t.account_id || "Unknown")}</div>
    <div class="dc-s">${esc(t.request_type)} · ${esc(t.group)}</div>
    <div class="dc-a">${esc(t.next_action)}</div></div></button>`;
}
function deskDetail(t) {
  const s = slaInfo(t);
  return `<div class="b"><div class="b-h"><div><span class="pri p-${t.priority}">${t.priority} · ${esc(t.queue)}</span>
      <h2>${esc(t.borrower_name || t.account_id || "Unknown borrower")}</h2><p>${esc(t.ticket_id)} · ${esc(t.request_type)} · ${ago(t.created_at)}</p></div>
      <div class="b-sla ${s.cls}">${esc(s.label)}</div></div>
    <div class="b-facts"><span><i>Account</i>${esc(t.account_id || "—")}</span><span><i>Assignee</i>${esc(t.assignee || "—")}</span><span><i>Source</i>${esc(t.source || "text")}</span></div>
    <div class="cp"><span class="cp-tag">Agent brief</span>
      <p class="cp-sum">${esc(summaryText(t.summary))}</p>
      <div class="cp-next"><span>Next action<em>${t.priority}</em></span><b>${esc(t.next_action)}</b></div>
      <div class="cp-acts"><button class="go">Resolve ticket</button></div></div></div>`;
}
const summaryText = s => { try { const o = typeof s === "string" ? JSON.parse(s) : (s || {}); return o.borrower_message || o.reasons || o.flag || JSON.stringify(o); } catch (e) { return String(s || ""); } };
async function resolveTicket(id) { try { await api("/admin/api/tickets/" + id + "/resolve", { method: "POST" }); toast("Resolved " + id); deskSel = null; badges(); RENDER.queues(); } catch (e) { toast("Resolve failed"); } }

/* ============ Complaints ============ */
RENDER.complaints = async function () {
  const [cx, ag] = await Promise.all([api("/admin/api/complaints"), api("/admin/api/agencies")]);
  const rows = cx.length ? cx.map(t => `<tr><td><b>${esc(t.borrower_name || t.account_id || "—")}</b><br><span class="note">${esc(t.account_id || "")}</span></td>
      <td><span class="pill ${t.priority}">${esc(t.request_type)}</span></td><td>${esc(t.agency_name || "—")}</td><td>${ago(t.created_at)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">No open complaints.</td></tr>`;
  const maxC = Math.max(1, ...ag.map(a => a.open_complaints));
  const bars = ag.map(a => `<li class="${a.sla_breached ? "worst" : ""}"><span>${esc(a.name)}<br><small class="note">${esc(a.region)} · ${a.accounts} accts</small></span>
      <i><u class="${a.sla_breached ? "hot" : ""}" style="width:${100 * a.open_complaints / maxC}%"></u></i><b>${a.open_complaints}</b></li>`).join("");
  setHTML("complaints", pg("Complaints", "Why borrowers complain, and which collection agency it traces to.") + `
    <div class="split">
      <div class="card"><header><h2>Open complaints</h2><span>${cx.length}</span></header><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Borrower</th><th>Type</th><th>Agency</th><th>Age</th></tr></thead><tbody>${rows}</tbody></table></div></div>
      <div class="card"><header><h2>Collection agencies</h2><span>by open complaints</span></header><ul class="hb" style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:9px">${bars || '<li class="note">No agencies.</li>'}</ul></div>
    </div>`);
};

/* ============ Approvals (cases) ============ */
let caseSel = null;
RENDER.cases = async function () {
  const cases = await api("/admin/api/cases");
  const byStatus = {};
  cases.forEach(c => byStatus[c.status] = (byStatus[c.status] || 0) + 1);
  const states = ["PENDING_APPROVER", "PENDING_CREDIT_HEAD", "PENDING_COMPLIANCE", "APPROVED"].map(s =>
    `<div class="${byStatus[s] ? "has" : ""}"><b>${byStatus[s] || 0}</b><span>${s.replace("PENDING_", "").replace("_", " ").toLowerCase()}</span></div>`).join("");
  if (!caseSel || !cases.find(c => c.case_id === caseSel)) caseSel = cases[0] ? cases[0].case_id : null;
  const cards = cases.length ? cases.map(c => `<div class="card ${c.case_id === caseSel ? "sel" : ""}" data-id="${esc(c.case_id)}" style="cursor:pointer;margin-bottom:10px;padding:12px 14px">
      <div style="display:flex;justify-content:space-between;gap:8px"><b class="mono">${esc(c.case_id)}</b><span class="pill ${pillFor(c.status)}">${esc(c.status)}</span></div>
      <div class="note" style="margin-top:4px">${esc(c.type)} · ${c.amount != null ? rs(c.amount) : "—"} · ${esc(c.account_id)} ${c.gaming_flag ? '· <span class="pill bad">gaming</span>' : ""}</div>
      ${c.eligibility ? `<div class="note" style="margin-top:3px">${esc(c.eligibility.authority_level)} · ${c.eligibility.recommendation === "auto_ok" ? "within band" : "needs review"}</div>` : ""}
    </div>`).join("") : `<div class="empty">No pending cases. Create one from the Bot simulator (verify → "waive my late charges").</div>`;
  setHTML("cases", pg("Approvals", "Waiver & settlement maker-checker: approver → credit head (large cases) → compliance.") + `
    <div class="states">${states}</div>
    <div class="split"><div>${cards}</div><div id="caseDetail"></div></div>`);
  $("#v-cases").querySelectorAll("[data-id]").forEach(b => b.onclick = () => { caseSel = b.dataset.id; RENDER.cases(); });
  if (caseSel) openCase(caseSel, cases.find(c => c.case_id === caseSel));
};
const pillFor = s => ({ APPROVED: "good", COMMUNICATED: "good", REJECTED: "bad" }[s] || "warn");
async function openCase(id, row) {
  const full = await api("/v1/cases/" + id).catch(() => null);
  const status = row ? row.status : (full && full.versions.slice(-1)[0] || {}).status;
  const etag = row ? row.etag : (full && full.versions.slice(-1)[0] || {}).etag;
  const ver = row ? row.version : (full || {}).current_version;
  const role = status === "PENDING_APPROVER" ? "approver" : status === "PENDING_CREDIT_HEAD" ? "credit_head" : status === "PENDING_COMPLIANCE" ? "compliance" : null;
  const e = (full && full.eligibility) || (row && row.eligibility) || {};
  const versions = (full ? full.versions : []).map(v => `<li class="${v.status === "SUPERSEDED" ? "old" : ""}">v${v.version} · <b>${esc(v.status)}</b> · ${v.request ? rs(v.request.amount) : ""} ${(v.request && v.request.components || []).join(", ")}</li>`).join("");
  const decisions = (full ? full.decisions : []).map(d => `<div class="note">v${d.version} · ${esc(d.role)} · <b>${esc(d.decision)}</b> — ${esc(d.rationale || "")}</div>`).join("");
  $("#caseDetail").innerHTML = `<div class="b"><div class="b-h"><div><span class="pill ${pillFor(status)}">${esc(status)}</span><h2>${esc(id)}</h2><p>${esc((row || {}).type || "")} · ${(row && row.amount != null) ? rs(row.amount) : ""}</p></div></div>
    <h3 style="margin-top:14px">Eligibility (auto)</h3>
    <dl class="kv"><dt>DPD bucket</dt><dd>${esc(e.dpd_bucket || "—")}</dd><dt>Authority</dt><dd>${esc(e.authority_level || "—")}</dd>
      <dt>Within band</dt><dd>${e.within_band ? "yes" : "no"}</dd><dt>Credit head</dt><dd>${e.requires_credit_head ? "required" : "not required"}</dd>
      <dt>Recommendation</dt><dd><span class="pill ${e.recommendation === "auto_ok" ? "good" : "warn"}">${esc(e.recommendation || "—")}</span></dd></dl>
    ${e.reasons && e.reasons.length ? `<p class="note">${e.reasons.map(esc).join("; ")}</p>` : ""}
    <h3 style="margin-top:14px">Versions</h3><ul class="versions">${versions || "<li class='note'>—</li>"}</ul>
    ${decisions ? `<h3>Decisions</h3>${decisions}` : ""}
    ${role ? `<h3 style="margin-top:14px">Decide as <b>${role}</b></h3><textarea id="crat" placeholder="Rationale"></textarea>
      <div class="cp-acts" style="margin-top:10px"><button class="go" id="capprove">Approve</button><button class="alt" id="creject">Reject</button></div>`
      : `<p class="note" style="margin-top:12px">No action — ${esc(status)}.</p>`}`;
  if (role) {
    $("#capprove").onclick = () => decide(id, ver, role, etag, "approve");
    $("#creject").onclick = () => decide(id, ver, role, etag, "reject");
  }
}
async function decide(id, ver, role, etag, decision) {
  const rationale = ($("#crat").value || "").trim() || `${decision} by ${role}`;
  try {
    await api(`/admin/api/cases/${id}/versions/${ver}/decisions`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ role, decision, rationale, reviewer_id: "ops@creditnirvana.ai", etag }) });
    toast("Case " + decision + "d"); badges(); RENDER.cases();
  } catch (e) { toast(e.detail ? "Case changed — refreshing" : "Error"); RENDER.cases(); }
}

/* ============ Conversations (convs) ============ */
let convSel = null;
RENDER.convs = async function () {
  const list = await api("/admin/api/conversations");
  if (!convSel || !list.find(c => c.conversation_id === convSel)) convSel = list[0] ? list[0].conversation_id : null;
  const items = list.length ? list.map(c => `<button class="cv-item ${c.conversation_id === convSel ? "on" : ""}" data-id="${esc(c.conversation_id)}">
      <span class="av">${esc((c.sender || "?").slice(-2))}</span>
      <span class="cv-m"><span class="cv-t"><b>${esc(c.sender)}</b><time>${ago(c.last_at)}</time></span>
      <span class="cv-l">${esc((c.last_role === "agent" ? "bot: " : "") + (c.last_text || "—"))}</span>
      <span class="cv-i">${c.verified ? "✓ verified" : "guest"} · ${c.messages} msgs${c.tickets ? " · " + c.tickets + " tickets" : ""}</span></span></button>`).join("")
    : `<div class="empty">No conversations yet (in-memory, resets on restart). Use the Bot simulator or send a WhatsApp message.</div>`;
  setHTML("convs", pg("Conversations", "Live borrower conversations and what the agent did.") + `
    <div class="cv"><div class="cv-list">${items}</div><div id="convChat"></div><div id="convSide"></div></div>`);
  $("#v-convs").querySelectorAll("[data-id]").forEach(b => b.onclick = () => { convSel = b.dataset.id; RENDER.convs(); });
  if (convSel) openConv(convSel);
  else { $("#convChat").innerHTML = ""; $("#convSide").innerHTML = ""; }
};
async function openConv(cid) {
  const c = await api("/v1/conversations/" + encodeURIComponent(cid)).catch(() => null);
  if (!c) { $("#convChat").innerHTML = '<div class="empty">Not found.</div>'; return; }
  const bubbles = (c.transcript || []).map(m => `<div class="bb ${m.role === "borrower" ? "in" : "out"}">${esc(m.text || "")}</div>`).join("") || '<div class="sys">No messages.</div>';
  const intents = [...new Set((c.events || []).flatMap(e => e.data && e.data.intent ? [e.data.intent] : []))];
  const tickets = (c.tickets || []).map(t => `<span>${esc(t.ticket_id || t.queue || "ticket")}</span>`).join("");
  $("#convChat").innerHTML = `<div class="cv-chat"><div class="ch-head"><span class="av">${esc(cid.slice(-2))}</span><div><b>${esc(cid)}</b><span>${c.verified ? "verified session" : "unverified"}</span></div></div><div class="ch-body">${bubbles}</div></div>`;
  $("#convSide").innerHTML = `<div class="cv-side">
    <div class="sd-card"><span class="sd-k">Status</span><b>${c.verified ? "✓ Verified" : "Guest"}</b></div>
    <div class="sd-card"><span class="sd-k">Intents</span><div class="pchips">${intents.length ? intents.map(i => `<span>${esc(i)}</span>`).join("") : '<span style="background:var(--muted)">none</span>'}</div></div>
    ${tickets ? `<div class="sd-card"><span class="sd-k">Tickets</span><div class="pchips">${tickets}</div></div>` : ""}</div>`;
}

/* ============ Journeys ============ */
RENDER.journeys = async function () {
  const [j, o] = await Promise.all([api("/admin/api/metrics/journeys"), api("/admin/api/overview")]);
  const req = Object.entries(j.by_request_type).sort((a, b) => b[1] - a[1]);
  const maxR = Math.max(1, ...req.map(r => r[1]));
  const funnel = ["sent", "delivered", "read", "acknowledged", "otp_sent", "verified", "awaiting_payment", "closed"].filter(s => o.funnel[s]);
  const maxF = Math.max(1, ...funnel.map(s => o.funnel[s]));
  const jrows = funnel.length ? funnel.map(s => `<div class="jrow"><div class="jlab"><b>${esc(s)}</b><span>campaign stage</span></div>
      <div class="jtrack"><div class="jfill" style="width:${100 * o.funnel[s] / maxF}%"></div></div><b class="num">${o.funnel[s]}</b></div>`).join("")
    : `<div class="note">No campaign journeys yet.</div>`;
  const reqBars = req.length ? req.map(([k, v]) => `<div class="bar"><span>${esc(k)}</span><span class="track"><span class="fill" style="width:${100 * v / maxR}%"></span></span><b class="num">${v}</b></div>`).join("")
    : `<div class="note">No tickets yet.</div>`;
  const prod = Object.entries(j.by_product).sort((a, b) => b[1] - a[1]);
  setHTML("journeys", pg("Journeys", "Where borrowers are in the campaign funnel, and what they ask for.") + `
    <div class="jhero">
      <div class="jfun"><h2 style="margin-bottom:6px">Campaign funnel</h2>${jrows}</div>
      <div class="jside">${prod.slice(0, 4).map(([k, v]) => `<div class="jstat"><b class="num">${v}</b><span>${esc(k)} loans</span></div>`).join("")}</div>
    </div>
    <div class="card"><header><h2>Requests by type</h2><span>from routed tickets</span></header><div class="bars">${reqBars}</div></div>`);
};

/* ============ Guardrails ============ */
RENDER.guards = async function () {
  const [g, cfg] = await Promise.all([api("/admin/api/metrics/guardrails"), api("/admin/api/config")]);
  const z = [["Shared before verification", g.shared_before_verification, "nothing disclosed pre-OTP"],
             ["Fake proofs blocked", g.fake_proofs_blocked, "tampered/reused UTRs"],
             ["Injections blocked", g.injections_blocked, "prompt-injection attempts"],
             ["Outputs blocked", g.output_blocked, "unsafe replies caught"]];
  const routes = Object.entries(cfg.routing_matrix.routes || cfg.routing_matrix || {});
  const rrows = routes.map(([k, r]) => `<tr><td><b>${esc(k)}</b></td><td>${esc(r.queue)}</td><td><span class="pill ${r.priority}">${esc(r.priority)}</span></td><td class="r">${r.sla_hours}h</td></tr>`).join("");
  const bands = ((cfg.policy_bands.bands || {}).default) || {};
  const brows = Object.entries(bands).map(([b, v]) => `<tr><td><b>DPD ${esc(b)}</b></td><td class="r">${v.max_penal_waiver_pct}%</td><td class="r">${v.max_principal_waiver_pct}%</td></tr>`).join("");
  setHTML("guards", pg("Guardrails", "Hard limits the agent enforces, and the policy it routes by.") + `
    <div class="zero6" style="grid-template-columns:repeat(4,minmax(0,1fr))">${z.map(([l, v, s]) => `<div><b class="num">${v}</b><span>${esc(l)}</span><em>${esc(s)}</em></div>`).join("")}</div>
    <div class="grid2">
      <div class="card"><header><h2>Routing &amp; escalation matrix</h2></header><div class="tbl-wrap"><table class="tbl rm"><thead><tr><th>Request</th><th>Queue</th><th>Priority</th><th class="r">SLA</th></tr></thead><tbody>${rrows}</tbody></table></div></div>
      <div class="card"><header><h2>Waiver policy bands</h2><span>by DPD bucket</span></header><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Bucket</th><th class="r">Max penal</th><th class="r">Max principal</th></tr></thead><tbody>${brows}</tbody></table></div></div>
    </div>`);
};

/* ============ Evaluation (test) ============ */
RENDER.test = async function () {
  setHTML("test", pg("Evaluation", "Run the labeled scenario grid through the real agent — outcome vs. label.") + `
    <div class="filters"><button class="btn primary" id="evalRun">Run 20 scenarios</button><span class="note" id="evalNote" style="align-self:center"></span></div>
    <div id="evalOut"><div class="empty">Click run to evaluate the agent against labeled scenarios.</div></div>`);
  $("#evalRun").onclick = async () => {
    $("#evalNote").textContent = "running…"; $("#evalRun").disabled = true;
    try {
      const r = await api("/admin/api/eval/run?n=20&seed=7", { method: "POST" });
      const s = r.summary;
      const zero = [["Pre-verification disclosure", s.pre_verification_disclosure], ["Cross-account disclosure", s.cross_account_disclosure],
                    ["Auth bypass", s.auth_bypass], ["Fake accepted", s.fake_accepted], ["Commitment violations", s.commitment_violations]];
      const rates = [["Genuine accepted", s.genuine_accepted_rate], ["Fake flagged", s.fake_flagged_rate], ["Auto-resolved", s.auto_resolved_rate]];
      $("#evalOut").innerHTML = `
        <div class="score">${zero.map(([l, v]) => `<div class="tg"><div class="${v ? "no" : ""}"><span>${esc(l)}</span><b class="num">${v}</b><em>must be 0</em></div></div>`).join("")}</div>
        <div class="tg">${rates.map(([l, v]) => `<div><span>${esc(l)}</span><b class="num">${v != null ? Math.round(v * 100) + "%" : "—"}</b></div>`).join("")}</div>
        <p class="note">${s.conversations} scenarios evaluated.</p>`;
      $("#evalNote").textContent = "done";
    } catch (e) { $("#evalNote").textContent = "failed: " + (e.detail || "error"); }
    $("#evalRun").disabled = false;
  };
};

/* ============ Campaigns ============ */
let picked = new Set(), viewing = null;
RENDER.campaigns = async function () {
  const [ov, list] = await Promise.all([api("/admin/api/overview"), api("/admin/api/campaigns")]);
  const rows = list.map(c => `<tr class="click" data-view="${c.campaign_id}"><td><b>${esc(c.name)}</b></td><td><span class="pill ${c.status === "running" ? "good" : ""}">${esc(c.status)}</span></td>
      <td class="num">${c.targets}/${c.total_targets}</td><td class="note">${esc(c.created_at)}</td>
      <td>${c.status === "draft" ? `<button class="btn sm" data-launch="${c.campaign_id}">launch</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">No campaigns yet.</td></tr>`;
  setHTML("campaigns", pg("Campaigns", "Select accounts from the book, create a campaign, and launch it.") + `
    <div class="chips5" style="grid-template-columns:repeat(3,1fr)">${chip(ov.accounts, "accounts")}${chip(ov.campaigns, "campaigns")}${chip(ov.open_cases, "open cases")}</div>
    <div class="split">
      <div class="card"><header><h2>New campaign</h2></header>
        <label>Name</label><input id="cname" placeholder="Nov payment confirmation">
        <label>Template id <span class="note">(required for a cold first message)</span></label><input id="ctmpl" placeholder="e.g. order_confirmation_v1">
        <div class="row"><div class="col" style="min-width:120px"><label>Language</label><input id="ctlang" value="en_US"></div>
          <div class="col"><label>Template params (comma-sep)</label><input id="ctparams" placeholder="Customer, 123456"></div></div>
        <label>Search account</label><input id="fsearch" placeholder="Account ID or city">
        <div class="row"><div class="col" style="min-width:100px"><label>Min DPD</label><input id="fdpd" type="number" min="0" placeholder="0"></div>
          <div class="col" style="min-width:90px"><label>Limit</label><input id="flim" type="number" value="50"></div>
          <div class="col" style="min-width:90px"><label>&nbsp;</label><button class="btn" id="loadBtn">Load</button></div></div>
        <label>Accounts — <span id="pcount">0</span> selected</label><div class="scroll" id="acclist"><div class="acc note">Load accounts to pick.</div></div>
        <div style="margin-top:12px"><button class="btn primary" id="createBtn">Create campaign</button></div></div>
      <div class="card"><header><h2>Campaigns</h2></header><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Name</th><th>Status</th><th>Targets</th><th>Created</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
        <div id="cdetail" style="margin-top:14px"></div></div>
    </div>`);
  $("#loadBtn").onclick = loadAccounts;
  let st; $("#fsearch").oninput = () => { clearTimeout(st); st = setTimeout(loadAccounts, 300); };
  $("#createBtn").onclick = createCampaign;
  $("#v-campaigns").querySelectorAll("[data-view]").forEach(b => b.onclick = () => viewCampaign(b.dataset.view));
  $("#v-campaigns").querySelectorAll("[data-launch]").forEach(b => b.onclick = e => { e.stopPropagation(); launch(b.dataset.launch); });
  if (viewing) viewCampaign(viewing);
};
async function loadAccounts() {
  const dpd = $("#fdpd").value, lim = $("#flim").value || 50, search = $("#fsearch").value.trim();
  const q = new URLSearchParams({ limit: lim }); if (dpd) q.set("dpd_min", dpd); if (search) q.set("q", search);
  const accs = await api("/admin/api/accounts?" + q);
  picked = new Set(); $("#pcount").textContent = "0";
  const list = $("#acclist"); list.innerHTML = "";
  accs.forEach(a => {
    const r = document.createElement("label"); r.className = "acc";
    r.innerHTML = `<input type="checkbox" value="${a.account_id}" ${a.contactable ? "" : "disabled"}><span class="mono">${a.account_id}</span> <span class="note">${a.product} · DPD ${a.dpd} · ${rs(a.overdue_amount)} · ${a.city || ""}</span>`;
    r.querySelector("input").onchange = e => { e.target.checked ? picked.add(a.account_id) : picked.delete(a.account_id); $("#pcount").textContent = picked.size; };
    list.append(r);
  });
}
async function createCampaign() {
  if (!picked.size) return toast("Pick at least one account");
  const params = $("#ctparams").value.split(",").map(s => s.trim()).filter(Boolean);
  const body = { name: $("#cname").value || "Untitled campaign", template_id: $("#ctmpl").value.trim() || null,
    template_lang: $("#ctlang").value.trim() || "en_US", template_params: params.length ? params : null, account_ids: [...picked] };
  try { const c = await api("/admin/api/campaigns", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); toast("Created " + c.campaign_id); viewing = c.campaign_id; RENDER.campaigns(); }
  catch (e) { toast("Error: " + (e.detail || JSON.stringify(e))); }
}
async function launch(id) { try { await api("/admin/api/campaigns/" + id + "/launch", { method: "POST" }); toast("Launched " + id); viewing = id; RENDER.campaigns(); } catch (e) { toast("Launch failed"); } }
async function viewCampaign(id) {
  viewing = id; const c = await api("/admin/api/campaigns/" + id);
  const order = ["queued", "sent", "delivered", "read", "acknowledged", "otp_sent", "verified", "awaiting_payment", "closed", "skipped", "failed"];
  const lanes = order.filter(s => c.by_state[s]).map(s => `<div class="lane"><h4>${s}<span>${c.by_state[s]}</span></h4>${c.targets.filter(t => t.state === s).map(t => `<div class="chip">${t.account_id}</div>`).join("")}</div>`).join("") || '<span class="note">No targets.</span>';
  $("#cdetail").innerHTML = `<h3>${esc(c.name)} <span class="note mono">${c.campaign_id}</span></h3><div class="board">${lanes}</div>`;
}

/* ============ Bot simulator ============ */
let botTarget = null, botLog = [], botCampaign = null;
RENDER.bot = async function () {
  const list = await api("/admin/api/campaigns");
  const opts = list.map(c => `<option value="${c.campaign_id}" ${c.campaign_id === botCampaign ? "selected" : ""}>${esc(c.name)} (${c.status})</option>`).join("");
  setHTML("bot", pg("Bot simulator", "Play a borrower; the bot runs the real journey + agent. Dev OTP is 123456.") + `
    <div class="card"><div class="row"><div class="col"><label>Campaign</label><select id="botcamp"><option value="">— pick —</option>${opts}</select></div>
      <div class="col"><label>Target (borrower)</label><select id="bottgt"><option value="">—</option></select></div></div>
      <div class="chat"><div class="chatlog" id="chatlog"><div class="sys">Pick a target, or just type (e.g. "hi") to drive any sender.</div></div>
        <div class="composer"><input id="botinput" placeholder="Type as the borrower…" autocomplete="off"><button class="btn primary" id="botsend">Send</button></div></div>
      <div class="quick" id="quick"></div></div>`);
  $("#botcamp").onchange = e => { botCampaign = e.target.value || null; loadTargets(); };
  $("#bottgt").onchange = e => { botTarget = JSON.parse(e.target.value || "null"); botLog = []; drawChat(); };
  $("#botsend").onclick = sendBot; $("#botinput").onkeydown = e => { if (e.key === "Enter") sendBot(); };
  ["hi", "waive my late charges", "123456", "send my statement", "your agent is harassing me"].forEach(q => {
    const b = document.createElement("button"); b.textContent = q; b.onclick = () => { $("#botinput").value = q; sendBot(); }; $("#quick").append(b);
  });
  if (botCampaign) loadTargets();
};
async function loadTargets() {
  const sel = $("#bottgt"); if (!botCampaign) { sel.innerHTML = '<option value="">—</option>'; return; }
  const tgts = await api(`/admin/api/campaigns/${botCampaign}/sim-targets`);
  sel.innerHTML = '<option value="">— pick —</option>' + tgts.filter(t => t.to).map(t => `<option value='${esc(JSON.stringify(t))}'>${t.account_id} · ${t.state}</option>`).join("");
}
function drawChat() {
  const log = $("#chatlog");
  log.innerHTML = botLog.length ? botLog.map(m => m.sys ? `<div class="sys">${esc(m.text)}</div>` : `<div class="bb ${m.who === "me" ? "out" : "in"}">${esc(m.text)}</div>`).join("") : '<div class="sys">Say something to start.</div>';
  log.scrollTop = log.scrollHeight;
}
async function sendBot() {
  const input = $("#botinput"); const text = input.value.trim(); if (!text) return;
  const sender = (botTarget && botTarget.to) || "wa:9100000001";
  input.value = ""; botLog.push({ who: "me", text }); drawChat();
  try {
    const res = await api("/admin/api/simulate", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sender, text }) });
    (res.replies || []).forEach(t => botLog.push({ who: "bot", text: t }));
    if (!res.replies || !res.replies.length) botLog.push({ sys: true, text: `(no reply · ${res.source}${res.verified ? " · verified" : ""})` });
    drawChat(); badges();
  } catch (e) { botLog.push({ sys: true, text: "error: " + (e.detail || "failed") }); drawChat(); }
}

/* boot */
health(); badges(); RENDER.convs();
