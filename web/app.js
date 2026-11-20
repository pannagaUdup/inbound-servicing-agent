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
  try { history.replaceState(null, "", "#" + t.dataset.v); } catch (e) {}   // deep-linkable tabs
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
function chart(days, bars, line, lineMax, opts) {
  // Bars scale to their own max (left axis). The line scales to lineMax (right axis); default 100 for a
  // 0-100 rate, shown with a "%" unit. Pass a count max to overlay a second count series (e.g. tickets
  // resolved) on the same scale as the bars — then the right axis is suppressed (shared scale).
  opts = opts || {};
  const hasLine = line && line.some(v => v != null);
  const sharedScale = !!lineMax;                       // line uses the bar (count) scale, not a % axis
  const W = 640, H = 184, pt = 12, pb = 26;
  const pl = 30;                                       // left gutter for y-axis count labels
  const pr = (hasLine && !sharedScale) ? 34 : 14;      // right gutter only when a % axis is drawn
  const iw = W - pl - pr, ih = H - pt - pb;
  const maxB = Math.max(1, ...bars), n = Math.max(1, bars.length), bw = iw / n;
  const lmax = lineMax || 100, unit = sharedScale ? "" : "%";
  const niceTop = v => { const p = Math.pow(10, Math.floor(Math.log10(Math.max(1, v)))); return Math.ceil(v / p) * p; };
  const topB = niceTop(maxB);
  const yB = v => pt + ih - ih * v / topB;             // count value -> pixel
  const yL = v => pt + ih - ih * Math.min(lmax, v) / lmax; // line value -> pixel
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDay = d => { const p = (d || "").split("-"); return p.length === 2 ? (+p[1]) + " " + (MON[(+p[0]) - 1] || "") : (d || ""); };
  let g = "";
  // horizontal gridlines + left (count) axis labels at 0 / mid / top
  for (let i = 0; i <= 2; i++) {
    const val = topB * i / 2, y = yB(val);
    g += `<line class="gl" x1="${pl}" y1="${y.toFixed(1)}" x2="${W - pr}" y2="${y.toFixed(1)}"/>`;
    g += `<text class="ax" x="${pl - 5}" y="${(y + 3).toFixed(1)}" text-anchor="end">${Math.round(val)}</text>`;
  }
  // right axis labels for the % line (0 / 50 / 100 of lmax)
  if (hasLine && !sharedScale) for (let i = 0; i <= 2; i++) {
    const val = lmax * i / 2, y = yL(val);
    g += `<text class="ax ax-r" x="${(W - pr + 5).toFixed(1)}" y="${(y + 3).toFixed(1)}">${Math.round(val)}${unit}</text>`;
  }
  const barLbl = opts.yLabel || "count", lineLbl = opts.rLabel || (sharedScale ? "" : "rate");
  const xAt = i => pl + i * bw + bw / 2;
  bars.forEach((v, i) => {
    const y = yB(v), bh = pt + ih - y, x = pl + i * bw + bw * 0.15;
    g += `<rect class="bm" data-d="${esc(fmtDay(days[i]))}" data-v="${v} ${esc(barLbl)}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw * 0.7).toFixed(1)}" height="${Math.max(0, bh).toFixed(1)}" rx="2"><title>${esc(fmtDay(days[i]))}: ${v} ${esc(barLbl)}</title></rect>`;
  });
  if (hasLine) {
    const Y = sharedScale ? (v => yB(v)) : (v => yL(v));
    const pts = line.map((v, i) => `${xAt(i).toFixed(1)},${Y(v || 0).toFixed(1)}`);
    g += `<polyline class="ln" points="${pts.join(" ")}"/>`;
    line.forEach((v, i) => { g += `<circle class="dot" data-d="${esc(fmtDay(days[i]))}" data-v="${v}${unit}${lineLbl ? " " + esc(lineLbl) : ""}" cx="${xAt(i).toFixed(1)}" cy="${Y(v || 0).toFixed(1)}" r="3"><title>${esc(fmtDay(days[i]))}: ${v}${unit}</title></circle>`; });
  }
  // x-axis: ~5 evenly spaced date ticks placed under their bars, with tick marks + the last day
  const baseY = pt + ih, step = Math.max(1, Math.round((n - 1) / 4) || 1);
  const ticks = new Set();
  for (let i = 0; i < n; i += step) ticks.add(i);
  ticks.add(n - 1);
  [...ticks].sort((a, b) => a - b).forEach(i => {
    const x = xAt(i), anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
    g += `<line class="gl" x1="${x.toFixed(1)}" y1="${baseY}" x2="${x.toFixed(1)}" y2="${(baseY + 4).toFixed(1)}"/>`;
    g += `<text class="ax" x="${x.toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${esc(fmtDay(days[i]))}</text>`;
  });
  return `<svg class="svgc" viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}
// Click any bar / line point to pin a readout of its date + value (works for touch too).
(function chartTip() {
  let tip;
  const hide = () => { if (tip) tip.classList.remove("on"); };
  document.addEventListener("click", e => {
    const mark = e.target.closest && e.target.closest(".svgc [data-d]");
    if (!mark) { hide(); return; }
    if (!tip) { tip = document.createElement("div"); tip.id = "chart-tip"; document.body.appendChild(tip); }
    tip.innerHTML = `<b>${esc(mark.getAttribute("data-d"))}</b><span>${esc(mark.getAttribute("data-v"))}</span>`;
    tip.classList.add("on");
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 12, y = e.clientY - r.height - 8;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 12;
    if (y < 8) y = e.clientY + 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  });
  addEventListener("scroll", hide, true);
})();
function ring(pct, cls = "") {
  const r = 16, c = 2 * Math.PI * r, off = c * (1 - Math.max(0, Math.min(1, pct / 100)));
  return `<svg class="ring ${cls}" viewBox="0 0 40 40"><circle class="rbg" cx="20" cy="20" r="${r}"/><circle class="rfg" cx="20" cy="20" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/><text x="20" y="24" text-anchor="middle">${Math.round(pct)}</text></svg>`;
}
const pg = (h1, p) => `<div class="pg"><h1>${esc(h1)}</h1><p>${esc(p)}</p></div>`;

/* ============ Pulse (ops) ============ */
RENDER.ops = async function () {
  const [p, o] = await Promise.all([api("/admin/api/metrics/pulse?days=29"), api("/admin/api/overview")]);
  const h = p.headline, s = p.series, days = (p.days || []).map(d => d.slice(5));
  const auto = Math.round(h.zero_touch_rate), man = 100 - auto;
  // Running totals so "created vs resolved" reads as a backlog: resolved always <= created,
  // the gap is the open backlog. (Per-day counts don't line up because a ticket is resolved
  // on a later day than it was created — resolution lags creation.)
  const cum = a => { let t = 0; return (a || []).map(v => (t += v)); };
  const tCreated = cum(s.tickets_created), tResolved = cum(s.tickets_resolved);
  const tMax = Math.max(1, ...tCreated, ...tResolved);
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
      <div class="card"><header><h2>Messages per day</h2><span class="key">in+out · <i class="k-bar"></i>bars</span></header>${chart(days, s.inbound.map((v, i) => v + s.outbound[i]), null, null, { yLabel: "msgs" })}</div>
      <div class="card"><header><h2>Conversations &amp; zero-touch</h2><span class="key"><i class="k-bar"></i>convs <i class="k-line"></i>% auto</span></header>${chart(days, s.conversations, s.zero_touch_rate, null, { yLabel: "convs", rLabel: "% auto" })}</div>
    </div>
    <div class="duo" style="margin-top:16px">
      <div class="card"><header><h2>Tickets created vs resolved</h2><span class="key"><i class="k-bar"></i>created <i class="k-line"></i>resolved · running total</span></header>${chart(days, tCreated, tResolved, tMax, { yLabel: "tickets (total)" })}<p class="note" style="margin:6px 2px 0">Cumulative over the window — the gap between the bars and the line is the current open backlog.</p></div>
      <div class="card"><header><h2>Verifications per day</h2><span class="key"><i class="k-bar"></i>verified</span></header>${chart(days, s.verified, null, null, { yLabel: "verified" })}</div>
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
      ${agentBrief(t.summary)}
      <div class="cp-next"><span>Next action<em>${t.priority}</em></span><b>${esc(t.next_action)}</b></div>
      <div class="cp-acts"><button class="go">Resolve ticket</button></div></div></div>`;
}
const summaryText = s => { try { const o = typeof s === "string" ? JSON.parse(s) : (s || {}); return o.borrower_message || o.reasons || o.flag || JSON.stringify(o); } catch (e) { return String(s || ""); } };
function agentBrief(summary) {
  let s; try { s = typeof summary === "string" ? JSON.parse(summary) : (summary || {}); } catch (e) { s = {}; }
  const bag = s.context_bag || {};
  const out = [];
  // headline: what the borrower said / why the ticket exists
  const head = s.borrower_message || (s.flag ? "Flagged: " + s.flag : "")
    || (s.reasons ? (Array.isArray(s.reasons) ? s.reasons.join("; ") : s.reasons) : "")
    || (s.verdict ? "Payment claim: " + s.verdict : "")
    || (s.request ? `${s.request.type || "request"}${s.request.amount != null ? " of " + rs(s.request.amount) : ""}` : "");
  if (head) out.push(`<p class="cp-sum">${esc(head)}</p>`);
  // triage
  const tri = bag.triage || s.triage;
  if (tri && (tri.sentiment || tri.priority || tri.hardship != null)) {
    const bits = [tri.sentiment, tri.priority && ("priority " + tri.priority), tri.hardship ? "hardship" : null].filter(Boolean);
    out.push(`<div class="bf"><span class="bf-k">Triage</span> ${esc(bits.join(" · "))}</div>`);
  }
  // policy clauses applied
  const cl = bag.policy_clauses || [];
  if (cl.length) out.push(`<div class="bf"><span class="bf-k">Policy</span> ${cl.map(c => `<span class="pill">${esc(c.id || c)}</span>`).join(" ")}</div>`);
  // decision reasoning
  if (bag.reviewer_note) out.push(`<div class="bf"><span class="bf-k">Why</span> ${esc(bag.reviewer_note)}</div>`);
  // recent conversation as a mini-chat
  const rc = bag.recent_conversation || [];
  if (rc.length) out.push(`<div class="bf"><span class="bf-k">Recent conversation</span></div><div class="bf-chat">${rc.slice(-6).map(m => `<div class="bb ${m.role === "borrower" ? "in" : "out"}" style="max-width:92%;font-size:12.5px">${esc(m.text || "")}</div>`).join("")}</div>`);
  if (!out.length) out.push(`<p class="cp-sum muted">${esc(summaryText(summary))}</p>`);
  return out.join("");
}
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
  const [cases, stats] = await Promise.all([api("/admin/api/cases"), api("/admin/api/cases/stats").catch(() => ({}))]);
  const states = ["PENDING_APPROVER", "PENDING_CREDIT_HEAD", "PENDING_COMPLIANCE", "APPROVED", "COMMUNICATED", "REJECTED"].map(s =>
    `<div class="${stats[s] ? "has" : ""} ${s === "COMMUNICATED" ? "term" : ""}"><b>${stats[s] || 0}</b><span>${s === "COMMUNICATED" ? "notified" : s.replace("PENDING_", "").replace("_", " ").toLowerCase()}</span></div>`).join("");
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
      : status === "COMMUNICATED" ? `<p class="cp-done" style="margin-top:12px">✓ Approved — borrower notified of the outcome.</p>`
      : status === "REJECTED" ? `<p class="note" style="margin-top:12px">Rejected — borrower notified; your team will follow up.</p>`
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
    // On the final compliance approve / any reject, the backend notifies the borrower.
    const notified = decision === "reject" || role === "compliance";
    toast(notified ? (decision === "approve" ? "Approved — borrower notified ✓" : "Rejected — borrower notified")
                   : "Case " + decision + "d");
    badges(); RENDER.cases();
  } catch (e) { toast(e.detail ? "Case changed — refreshing" : "Error"); RENDER.cases(); }
}




/* ============ Monitor: pick a session, watch the assistant work, open any stage or the whole journey ============ */
let FL_TECH = false; try { FL_TECH = localStorage.getItem("cn-tech") === "1"; } catch (e) {}
const FL = { sel: null, data: null, turn: -1, steps: [], pos: 0, run: 0, speed: 1, live: true, playing: false, all: false, q: "",
             timer: null, botAt: "borrower", sessions: [], tab: "step", node: null, sig: "", mon: null, monAt: 0 };
const FL_NODES = {
  borrower: [70, 230, "Borrower", "ext"], ingress: [190, 230, "Message received", "ext"],
  screen: [310, 230, "Safety check", "graph"], normalize: [430, 230, "Language check", "graph"],
  handoff: [580, 40, "Handoff to a person", "graph"], confirm_change: [580, 100, "Confirm change", "graph"],
  confirm_action: [580, 160, "Confirm request", "graph"], verify: [580, 230, "Identity check", "graph"], plan: [580, 300, "Understand request", "graph"],
  start_verification: [700, 340, "Start identity check", "graph"], clarify: [700, 400, "Ask a question", "graph"],
  execute: [700, 230, "Resolve request", "graph"], output_guard: [830, 230, "Reply safety check", "graph"], outbound: [950, 230, "Send reply", "ext"],
  gateway: [700, 110, "Access control", "gw"], tools: [830, 110, "Customer systems", "tool"], case: [950, 110, "Approval case", "tool"],
  approver: [1090, 110, "Approver", "human"], credit_head: [1090, 230, "Credit head", "human"], compliance: [1090, 350, "Compliance", "human"],
};
const FL_EDGES = [["borrower", "ingress"], ["ingress", "screen"], ["screen", "normalize"], ["normalize", "handoff"], ["normalize", "confirm_change"],
  ["normalize", "confirm_action"], ["normalize", "verify"], ["normalize", "plan"], ["verify", "execute"], ["plan", "execute"], ["plan", "clarify"],
  ["plan", "start_verification"], ["handoff", "output_guard"], ["confirm_change", "output_guard"], ["confirm_action", "output_guard"],
  ["clarify", "output_guard"], ["start_verification", "output_guard"], ["execute", "output_guard"], ["execute", "gateway"], ["gateway", "tools"],
  ["gateway", "case"], ["case", "approver"], ["approver", "credit_head"], ["credit_head", "compliance"], ["output_guard", "outbound"], ["outbound", "borrower"]];
const FL_STATE = { done: "Done", failed: "Problem", waiting: "Waiting", projected: "Upcoming" };
const FL_NEAR = { borrower: "Borrower" };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const flDwell = s => Math.min(1500, Math.max(600, 450 + (s.ms || 0) * 1.2));
const flPt = id => { const n = FL_NODES[id]; return [n[0], n[1]]; };
const flName = id => (FL_NODES[id] || [0, 0, id])[2];
const flTime = iso => { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
const flSecs = ms => ms == null ? "" : ms < 1000 ? "under a second" : (ms / 1000).toFixed(1) + " s";
function flEdge(a, b) {
  const [x1, y1] = flPt(a), [x2, y2] = flPt(b);
  if (a === "outbound" && b === "borrower") return `M${x1} ${y1 - 20} C${x1} -110 ${x2} -110 ${x2} ${y2 - 20}`;
  return `M${x1} ${y1} L${x2} ${y2}`;
}
const flTurn = () => (FL.data && FL.data.turns[FL.turn]) || null;

function flSetTech(on) {
  FL_TECH = on; try { localStorage.setItem("cn-tech", on ? "1" : "0"); } catch (e) {}
  document.querySelectorAll('#nav button[data-v="monitor"], #nav button[data-v="traces"]').forEach(b => b.hidden = !on);
  const t = $("#flTech"); if (t) t.checked = on;
  if ($("#flStage")) flPanel();
}

function flBuildSkeleton() {
  const edges = FL_EDGES.map(([a, b]) => `<path d="${flEdge(a, b)}" class="fedge"/>`).join("");
  const nodes = Object.entries(FL_NODES).map(([id, [x, y, label, g]]) =>
    `<g class="fn ${g}" id="fn-${id}" transform="translate(${x} ${y})" tabindex="0" role="button" aria-label="${esc(label)}"><rect x="-54" y="-20" width="108" height="40" rx="10"/><text y="4" text-anchor="middle">${esc(label)}</text><title>${esc(label)}: click to see its lifecycle</title></g>`).join("");
  const bot = `<g id="flBot" class="fbot" style="transform:translate(70px,192px)"><g class="fbubble" id="flBubble" opacity="0"><rect id="flBubbleR" x="-80" y="-48" width="160" height="26" rx="8"/><text id="flBubbleT" y="-31" text-anchor="middle"></text></g>
    <line x1="0" y1="-12" x2="0" y2="-20" class="fant"/><circle cx="0" cy="-22" r="3" class="fant-dot"/><circle class="fring" r="22" cy="2"/>
    <rect x="-15" y="-12" width="30" height="24" rx="7" class="fhead"/><circle cx="-6" cy="-1" r="3.2" class="feye"/><circle cx="6" cy="-1" r="3.2" class="feye"/><rect x="-6" y="6" width="12" height="2" rx="1" class="fmouth"/></g>`;
  setHTML("flow", pg("Monitor", "Pick a conversation to watch how the assistant handled it. Click any stage for its history, or open the whole journey.") + `
  <div class="flwrap">
    <aside class="flside"><div class="flhead"><b>Conversations</b><div class="seg"><button data-f="active" class="on">Active</button><button data-f="all">All</button></div></div>
      <input id="flq" class="flsearch" placeholder="Search"><div id="flSessions" class="fllist"></div></aside>
    <div class="flmain">
      <div class="flctl"><button class="go" id="flPlay">Play</button><button class="alt" id="flReplay">Replay</button><button class="alt" id="flAll">Play whole conversation</button>
        <select id="flSpeed" aria-label="Speed"><option value="0.5">Slow</option><option value="1" selected>Normal</option><option value="2">Fast</option><option value="4">Very fast</option></select>
        <label class="mlive"><input type="checkbox" id="flLive" checked> Follow live</label>
        <label class="mlive techsw"><input type="checkbox" id="flTech" ${FL_TECH ? "checked" : ""}> Technical details</label><span class="note" id="flStat"></span></div>
      <div id="flNeeds"></div>
      <div class="flturns" id="flTurns"></div>
      <div class="flstage"><svg id="flStage" viewBox="0 -80 1200 560" role="img" aria-label="Workflow">${edges}<g id="flTrails"></g>${nodes}<g id="flChips"></g>${bot}</svg></div>
      <input type="range" id="flScrub" min="0" max="0" value="0" class="flscrub" aria-label="Move through the steps">
      <div class="fl2"><div class="fllog" id="flLog"></div>
        <div><div class="fltabs" role="tablist"><button data-t="step" class="on">This step</button><button data-t="journey">Whole journey</button><button data-t="node">Stage history</button></div>
          <div class="fldetail" id="flDetail"></div></div></div>
    </div></div>`);
  $("#flPlay").onclick = () => FL.playing ? flPause() : flPlay(FL.pos >= FL.steps.length - 1 ? 0 : FL.pos);
  $("#flReplay").onclick = () => flPlay(0);
  $("#flAll").onclick = flPlaySession;
  $("#flSpeed").onchange = e => { FL.speed = Number(e.target.value); };
  $("#flLive").onchange = e => { FL.live = e.target.checked; };
  $("#flTech").onchange = e => flSetTech(e.target.checked);
  $("#flScrub").oninput = e => { flPause(); const k = Number(e.target.value); flReset(k); FL.tab = "step"; flPanel(Math.max(0, k - 1)); };
  $("#flq").oninput = e => { FL.q = e.target.value.toLowerCase(); flRenderSessions(); };
  document.querySelectorAll(".flhead .seg button").forEach(b => b.onclick = () => { FL.all = b.dataset.f === "all";
    document.querySelectorAll(".flhead .seg button").forEach(x => x.classList.toggle("on", x === b)); flRefresh(); });
  document.querySelectorAll(".fltabs button").forEach(b => b.onclick = () => { FL.tab = b.dataset.t; flPanel(); });
  document.querySelectorAll("#flStage .fn").forEach(g => { const id = g.id.slice(3); g.onclick = () => flNodeOpen(id); g.onkeydown = e => { if (e.key === "Enter" || e.key === " ") flNodeOpen(id); }; });
  flSetTech(FL_TECH);
}

RENDER.flow = async function () {
  if (!$("#flStage")) flBuildSkeleton();
  clearInterval(FL.timer);
  await flRefresh();
  FL.timer = setInterval(() => { if (CURRENT !== "flow") return clearInterval(FL.timer); flRefresh(); }, 4000);
};

const flSig = s => [s.last_trace_id, s.status, s.open_cases.map(c => c.status).join(",")].join("|");
async function flRefresh() {
  let list;
  try { list = await api("/admin/api/traces/sessions?include_idle=" + FL.all); }
  catch (e) { $("#flSessions").innerHTML = `<div class="empty">Monitoring is switched off. Ask engineering to enable it.</div>`; return; }
  FL.sessions = list; flRenderSessions();
  if (!FL.sel && list[0]) return flSelect(list[0].conversation_id);
  const cur = list.find(x => x.conversation_id === FL.sel);
  if (!cur || flSig(cur) === FL.sig || FL.playing) return;
  const newTurn = FL.data && cur.last_trace_id !== FL.data.turns[FL.data.turns.length - 1].trace_id;
  if (newTurn && !FL.live) return;
  await flLoad(FL.sel, newTurn ? "play" : "keep");
}

const FL_STATUS = { live: "live", waiting_approval: "waiting for approval", failed: "problem", active: "active", idle: "idle" };
function flRenderSessions() {
  const q = FL.q, rows = FL.sessions.filter(s => !q || (s.label + " " + (s.last_text || "")).toLowerCase().includes(q));
  $("#flSessions").innerHTML = rows.length ? rows.map(s => `<div class="fcard ${s.conversation_id === FL.sel ? "sel" : ""}" data-id="${esc(s.conversation_id)}" tabindex="0">
      <div class="fc1"><b>${esc(s.label)}</b><span class="fst ${s.status}">${s.status === "live" ? '<i class="dot"></i>' : ""}${esc(FL_STATUS[s.status])}</span></div>
      <div class="fc2">${esc(s.last_text || "(no text)")}</div>
      <div class="note">${s.turns} message${s.turns > 1 ? "s" : ""} · ${s.age_seconds < 60 ? s.age_seconds + " s" : Math.round(s.age_seconds / 60) + " min"} ago${s.verified ? " · identity confirmed" : ""}</div>
      ${s.open_cases.map(c => `<span class="pill warn">Awaiting ${esc(c.status.replace("PENDING_", "").replace("_", " ").toLowerCase())}</span>`).join(" ")}
    </div>`).join("") : `<div class="empty">No active conversations. Send a message from the Bot simulator, or switch to All.</div>`;
  $("#flSessions").querySelectorAll(".fcard").forEach(c => { c.onclick = () => flSelect(c.dataset.id); c.onkeydown = e => { if (e.key === "Enter") flSelect(c.dataset.id); }; });
}

async function flSelect(cid) { flPause(); FL.sel = cid; FL.node = null; FL.tab = "step"; flRenderSessions(); await flLoad(cid, "play"); }

/* Load the whole journey of a conversation; mode: play (animate the latest turn) | keep (refresh in place). */
async function flLoad(cid, mode) {
  const d = await api("/admin/api/traces/lifecycle?conversation_id=" + encodeURIComponent(cid)).catch(() => null);
  if (!d) return;
  const keepTurn = mode === "keep" && FL.data && FL.data.conversation_id === cid ? FL.turn : -1;
  FL.data = d; const cur = FL.sessions.find(x => x.conversation_id === cid); FL.sig = cur ? flSig(cur) : "";
  flUseTurn(keepTurn >= 0 && keepTurn < d.turns.length ? keepTurn : d.turns.length - 1, mode === "play");
}

function flUseTurn(i, play) {
  FL.turn = i; const t = flTurn(); if (!t) return;
  FL.steps = t.events.filter(e => e.node).map(e => ({ node: e.node, plain: e.plain, state: e.state, ms: e.duration_ms, at: e.at, n: e.n,
    category: e.category, catLabel: e.category_label, action: e.action, tech: e.tech, label: e.label, stage: e.stage_label }));
  const keep = FL.pos; FL.pos = 0;
  $("#flScrub").max = FL.steps.length; $("#flScrub").value = 0;
  flRenderTurns(); flRenderNeeds();
  $("#flLog").innerHTML = FL.steps.map((s, k) => `<div class="flrow ${s.state}" data-i="${k}" tabindex="0"><span class="n"><i class="cdot cat-${s.category}"></i></span><b>${esc(s.stage)}</b><small>${esc(s.plain)}</small></div>`).join("");
  $("#flLog").querySelectorAll(".flrow").forEach(r => r.onclick = () => { flPause(); const k = Number(r.dataset.i); flReset(k + 1); FL.tab = "step"; flPanel(k); $("#flScrub").value = k + 1; });
  $("#flStat").textContent = `${FL.steps.filter(s => s.state !== "projected").length} steps · ${flSecs(t.duration_ms)}`;
  if (play) flPlay(0); else { flReset(FL.steps.length); flPanel(Math.min(Math.max(keep, 0), FL.steps.length - 1)); }
}

function flRenderTurns() {
  const d = FL.data;
  $("#flTurns").innerHTML = d.turns.map((t, i) => `<button class="tchip ${i === FL.turn ? "on" : ""} ${t.status}" data-i="${i}"><span class="note">${i + 1}.</span> ${esc((t.text || "message").slice(0, 26))}<small>${esc(t.resolution_plain || "")} · ${flSecs(t.duration_ms)}</small></button>`).join("");
  $("#flTurns").querySelectorAll(".tchip").forEach(b => b.onclick = () => { flPause(); FL.tab = "step"; flUseTurn(Number(b.dataset.i), true); });
}

const FL_SEV = { attention: "Needs attention", blocked: "On hold", waiting: "Waiting on someone", upcoming: "Upcoming", ok: "OK" };
function flRenderNeeds() {
  const t = flTurn(), seen = new Set();
  const cards = (t ? t.needs_action : []).filter(e => { const k = e.action.kind + e.action.text; if (seen.has(k)) return false; seen.add(k); return true; })
    .map(e => `<div class="need ${e.action.severity}"><div><span class="pill cat-${e.category}">${esc(e.category_label)}</span> <b>${esc(e.stage_label || e.label)}</b>
      <span class="sev ${e.action.severity}">${esc(FL_SEV[e.action.severity])}</span><p>${esc(e.plain)} ${esc(e.action.text)}</p></div>${actBtn(e.action)}</div>`).join("");
  $("#flNeeds").innerHTML = cards ? `<h3 class="needh">What needs doing</h3>${cards}` : `<div class="allclear">Nothing needs doing on this message.</div>`;
  $("#flNeeds").querySelectorAll(".actbtn").forEach(b => b.onclick = () => monAct(b.dataset.k, b.dataset.t));
}

/* ---- right-hand panel: this step | whole journey | stage history ---- */
function flPanel(i) {
  document.querySelectorAll(".fltabs button").forEach(b => b.classList.toggle("on", b.dataset.t === FL.tab));
  const box = $("#flDetail"); if (!box || !FL.data) return;
  box.innerHTML = FL.tab === "journey" ? flJourneyHtml() : FL.tab === "node" ? flNodeHtml() : flStepHtml(i == null ? FL.pos : i);
  box.querySelectorAll(".actbtn").forEach(b => b.onclick = () => monAct(b.dataset.k, b.dataset.t));
  box.querySelectorAll("[data-turn]").forEach(b => b.onclick = () => { flPause(); const ti = Number(b.dataset.turn), si = b.dataset.step; FL.tab = "step";
    flUseTurn(ti, false); if (si != null && si !== "") { const k = Number(si); flReset(k + 1); flPanel(k); $("#flScrub").value = k + 1; flHighlight(k); } });
}

function flStepHtml(i) {
  const s = FL.steps[i]; if (!s) return `<p class="note">Choose a step, or press Play.</p>`;
  const a = s.action, tech = FL_TECH && (s.tech || s.label) ? `<details class="tech" open><summary>Technical details</summary><p class="note">Step name: <span class="mono">${esc(s.label)}</span> · trace <span class="mono">${esc(flTurn().trace_id)}</span></p>${s.tech ? jsonBlock(s.tech) : ""}</details>` : "";
  return `<h3><span class="pill cat-${s.category}">${esc(s.catLabel)}</span> ${esc(s.stage)}</h3>
    <p class="plain">${esc(s.plain)}</p>
    <p class="note">${esc(FL_STATE[s.state])}${s.ms != null ? " · took " + flSecs(s.ms) : ""}${s.at ? " · " + esc(flTime(s.at)) : ""}</p>
    <div class="todo ${a.severity}"><b>${esc(FL_SEV[a.severity])}</b><p>${esc(a.text)}</p>${actBtn(a)}</div>
    <p><button class="linkbtn" id="flToNode">See this stage's history →</button></p>${tech}`;
}

function flJourneyHtml() {
  const d = FL.data, turns = d.turns, last = turns[turns.length - 1];
  const total = turns.reduce((a, t) => a + (t.duration_ms || 0), 0);
  const open = d.cases.filter(c => c.versions.some(v => v.current && /PENDING/.test(v.status)));
  const tl = turns.map((t, ti) => `<div class="jturn ${ti === FL.turn ? "cur" : ""}"><div class="jh"><b>${ti + 1}. ${esc(t.text || "message")}</b>
      <span class="note">${esc(flTime(t.started_at))} · ${esc(t.resolution_plain || "")} · ${flSecs(t.duration_ms)}</span></div>
      <div class="jdots">${t.events.filter(e => e.node).map((e, si) => `<button class="jdot ${e.state} cat-${e.category}" data-turn="${ti}" data-step="${si}" title="${esc(e.stage_label + ": " + e.plain)}"></button>`).join("")}</div></div>`).join("");
  const cases = d.cases.map(c => `<h4>Approval · ${esc(c.case_id)}</h4>` + c.versions.map(v => `<div class="tver ${v.current ? "cur" : "old"}"><div class="note">Version ${v.version} · ${esc(v.status.replace("_", " ").toLowerCase())}${v.supersede_reason ? " · replaced: " + esc(v.supersede_reason) : ""}</div>
      <div class="stepper">${v.stages.map(s => `<div class="step ${s.state}"><b>${esc(ROLE_LABEL[s.role] || s.role)}</b><span>${esc(s.state.replace("_", " "))}</span>
        ${s.reviewer ? `<small>${esc(s.reviewer)} · after ${flSecs((s.waited_seconds || 0) * 1000)}</small>` : ""}${s.state === "waiting" ? `<small>waiting ${flSecs((s.waiting_seconds || 0) * 1000)}</small>` : ""}${s.rationale ? `<small>“${esc(s.rationale)}”</small>` : ""}</div>`).join('<em class="tarrow">→</em>')}</div></div>`).join("")).join("");
  return `<h3>Whole journey · ${esc(d.label)}</h3>
    <div class="jsum"><div><b>${turns.length}</b><span>messages</span></div><div><b>${flSecs(total) || "—"}</b><span>assistant time</span></div><div><b>${esc(last.resolution_plain || "—")}</b><span>latest outcome</span></div><div><b>${open.length}</b><span>awaiting approval</span></div></div>
    <p class="note">Each dot is one step; colour shows the type of step, hollow means still to come. Click a dot to jump to it.</p>
    <div class="jlist">${tl}</div>${cases}`;
}

function flNodeOpen(id) {
  FL.node = id; FL.tab = "node";
  document.querySelectorAll("#flStage .fn").forEach(n => n.classList.toggle("pick", n.id === "fn-" + id));
  flPanel();
  const now = Date.now(); if (!FL.mon || now - FL.monAt > 10000) { FL.monAt = now; api("/admin/api/traces/monitor").then(m => { FL.mon = m; if (FL.tab === "node" && FL.node === id) flPanel(); }).catch(() => {}); }
}

function flNodeHtml() {
  const id = FL.node; if (!id) return `<p class="note">Click any box in the picture to see that stage's history: what it does, how it has been behaving, and every time it ran in this conversation.</p>`;
  const d = FL.data, runs = [];
  d.turns.forEach((t, ti) => t.events.filter(e => e.node).forEach((e, si) => { if (e.node === id) runs.push({ ti, si, e, t }); }));
  const real = runs.filter(r => r.e.state !== "projected"), bad = real.filter(r => r.e.state === "failed").length, wait = real.filter(r => r.e.state === "waiting").length;
  const ms = real.filter(r => r.e.duration_ms != null), avg = ms.length ? Math.round(ms.reduce((a, r) => a + r.e.duration_ms, 0) / ms.length) : null;
  const before = FL_EDGES.filter(([a, b]) => b === id).map(([a]) => flName(a)), after = FL_EDGES.filter(([a, b]) => a === id).map(([, b]) => flName(b));
  const all = FL.mon && FL.mon.nodes && FL.mon.nodes[id];
  const rows = runs.map(r => `<tr class="${r.e.state}" data-turn="${r.ti}" data-step="${r.si}" style="cursor:pointer"><td>${r.ti + 1}</td><td>${esc(flTime(r.e.at))}</td><td><span class="sev ${r.e.state === "failed" ? "attention" : r.e.state === "waiting" ? "waiting" : r.e.state === "projected" ? "" : "ok"}">${esc(FL_STATE[r.e.state])}</span></td><td>${esc(r.e.plain)}</td><td>${r.e.duration_ms != null ? flSecs(r.e.duration_ms) : ""}</td></tr>`).join("");
  return `<h3>${esc(flName(id))}</h3><p class="plain">${esc(d.about[id] || "")}</p>
    <div class="jsum"><div><b>${real.length}</b><span>times in this chat</span></div><div><b>${real.length ? Math.round((real.length - bad) * 100 / real.length) + "%" : "—"}</b><span>went smoothly</span></div><div><b>${avg != null ? flSecs(avg) : "—"}</b><span>typical time</span></div><div><b>${wait}</b><span>waiting now</span></div></div>
    ${all ? `<p class="note">Across all recent conversations: ${all.runs} runs, ${all.failed} problem${all.failed === 1 ? "" : "s"}${all.runs ? ", typically " + flSecs(all.avg_ms) : ""}.</p>` : ""}
    <p class="note">Comes after: ${before.map(esc).join(", ") || "—"} · Leads to: ${after.map(esc).join(", ") || "—"}</p>
    <h4>Lifecycle in this conversation</h4>
    ${runs.length ? `<div class="steptbl"><table><thead><tr><th>Msg</th><th>Time</th><th>Result</th><th>What happened</th><th>Took</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="note">This stage hasn't been used in this conversation yet.</p>`}`;
}

/* ---- animation engine (unchanged behaviour) ---- */
function flBtn() { const b = $("#flPlay"); if (b) b.textContent = FL.playing ? "Pause" : (FL.pos >= FL.steps.length - 1 && FL.steps.length ? "Play again" : "Play"); }
function flPause() { FL.run++; FL.playing = false; flBtn(); }
function flClear() {
  document.querySelectorAll("#flStage .fn").forEach(n => n.setAttribute("class", "fn " + FL_NODES[n.id.slice(3)][3] + (n.id === "fn-" + FL.node ? " pick" : "")));
  $("#flTrails").innerHTML = ""; $("#flChips").innerHTML = ""; flBubble(null);
}
function flMark(id, cls) { const n = $("#fn-" + id); if (!n) return; n.classList.remove("active", "visited", "failed", "waiting", "ghost"); n.classList.add(cls); }
function flBotTo(id) { const [x, y] = flPt(id); const b = $("#flBot"); b.style.transitionDuration = (0.48 / FL.speed) + "s"; b.style.transform = `translate(${x}px,${y - 38}px)`; FL.botAt = id; }
function flTrail(a, b, state, animated) {
  if (a === b) return;
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", flEdge(a, b)); p.setAttribute("class", "ftrail " + state); $("#flTrails").appendChild(p);
  if (animated) { const L = p.getTotalLength(); p.style.strokeDasharray = L; p.style.strokeDashoffset = L; p.getBoundingClientRect();
    p.style.transition = `stroke-dashoffset ${0.48 / FL.speed}s ease-in-out`; p.style.strokeDashoffset = 0; }
  else p.style.strokeDasharray = "none";
}
function flBubble(s) {
  const g = $("#flBubble"); if (!g) return;
  if (!s) { g.setAttribute("opacity", "0"); return; }
  const text = s.plain.length > 46 ? s.plain.slice(0, 45) + "…" : s.plain;
  const w = Math.max(90, text.length * 6.2 + 22);
  $("#flBubbleR").setAttribute("x", -w / 2); $("#flBubbleR").setAttribute("width", w); $("#flBubbleT").textContent = text;
  g.setAttribute("opacity", "1"); g.setAttribute("class", "fbubble " + s.state);
}
function flChip(node, text, state) {
  const [x, y] = flPt(node), t = document.createElementNS("http://www.w3.org/2000/svg", "text");
  t.setAttribute("x", x); t.setAttribute("y", y - 28); t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "fchip " + state); t.textContent = String(text).slice(0, 34);
  $("#flChips").appendChild(t); setTimeout(() => t.remove(), 1600);
}
function flHighlight(i) {
  document.querySelectorAll("#flLog .flrow").forEach(r => r.classList.toggle("cur", Number(r.dataset.i) === i));
  const cur = $("#flLog .flrow.cur"); if (cur) cur.scrollIntoView({ block: "nearest" });
  $("#flScrub").value = i + 1;
}
function flReset(k) {
  flClear(); let prev = "borrower";
  FL.steps.forEach((s, i) => {
    if (i >= k || s.state === "projected") { if (s.state === "projected") flMark(s.node, "ghost"); return; }
    flTrail(prev, s.node, s.state, false); prev = s.node;
    flMark(s.node, s.state === "failed" ? "failed" : s.state === "waiting" ? "waiting" : "visited");
  });
  FL.pos = Math.max(0, k - 1); flBotTo(prev); flBtn();
  if (k > 0) { const s = FL.steps.slice(0, k).reverse().find(x => x.state !== "projected"); flBubble(s || null); }
}
async function flStep(i, my) {
  const s = FL.steps[i], sp = FL.speed; FL.pos = i; flHighlight(i);
  if (s.state === "projected") { flMark(s.node, "ghost"); await sleep(280 / sp); return; }
  if (FL.botAt !== s.node) { flTrail(FL.botAt, s.node, s.state, true); flBotTo(s.node); await sleep(480 / sp); }
  if (my !== FL.run) return;
  flMark(s.node, "active"); flBubble(s); if (FL.tab === "step") flPanel(i);
  if (s.node === "tools" || s.node === "gateway" || s.node === "case") flChip(s.node, s.plain.replace(/^Looked up /, ""), s.state);
  await sleep(flDwell(s) / sp);
  if (my !== FL.run) return;
  flMark(s.node, s.state === "failed" ? "failed" : s.state === "waiting" ? "waiting" : "visited");
}
async function flPlay(from) {
  const my = ++FL.run; FL.playing = true; flReset(from); FL.playing = true; flBtn();
  for (let i = from; i < FL.steps.length; i++) { await flStep(i, my); if (my !== FL.run) return false; }
  FL.playing = false; flBtn();
  const last = FL.steps.filter(s => s.state !== "projected").pop();
  if (last && last.state === "waiting") flBubble({ ...last, state: "waiting", plain: "Waiting for a reviewer…" });
  return true;
}
async function flPlaySession() {
  flPause();
  for (let i = 0; i < FL.data.turns.length; i++) {
    flUseTurn(i, false);
    if (!(await flPlay(0))) return;
    await sleep(500);
  }
}
document.addEventListener("click", e => { if (e.target && e.target.id === "flToNode") { const s = FL.steps[FL.pos]; if (s) flNodeOpen(s.node); } });

/* ============ Monitor (live architecture map + projected plan + sequence of events) ============ */
let monTrace = null, monTimer = null, monLive = true;
const nodeHealth = (runs, failed) => !runs ? "idle" : failed ? "bad" : "ok";
const mbox = (title, sub, h, extra) => `<div class="mbox ${h}"><b>${esc(title)}</b><span>${sub}</span>${extra || ""}</div>`;
const mlayer = (name, boxes, cls) => `<div class="mlayer ${cls || ""}"><div class="mname">${esc(name)}</div><div class="mrow">${boxes}</div></div>`;

RENDER.monitor = async function () {
  clearInterval(monTimer);
  const [m, list] = await Promise.all([api("/admin/api/traces/monitor").catch(() => null), api("/admin/api/traces?limit=25").catch(() => [])]);
  if (!m) { setHTML("monitor", pg("Monitor", "Live map of every stage.") + `<div class="empty">Traces are off. Set <span class="mono">SA_TRACING=true</span> and restart.</div>`); return; }
  if (!monTrace || !list.find(t => t.trace_id === monTrace)) monTrace = list[0] ? list[0].trace_id : null;
  const N = m.nodes, T = m.turns;
  const gnodes = Object.keys(N).map(k => mbox(NODE_LABEL[k] || k, N[k].runs ? `${N[k].runs} runs · ${N[k].avg_ms} ms avg` : "not used", nodeHealth(N[k].runs, N[k].failed), N[k].failed ? `<i>${N[k].failed} failed</i>` : "")).join("");
  const toolBoxes = Object.entries(m.tools).filter(([k]) => !k.startsWith("case_") && k !== "tool_denied").slice(0, 8).map(([k, v]) => mbox(k, v + " calls", "ok")).join("") || mbox("no calls yet", "", "idle");
  const C = m.cases, tier = (k, label) => mbox(label, (C[k] || 0) + " waiting", (C[k] || 0) ? "warn" : "idle");
  const map = [
    mlayer("Channels", mbox("Borrower · WhatsApp", `${T.total} turns`, nodeHealth(T.total, 0)) + mbox("Web simulator", "eval · demo", "idle")),
    mlayer("Ingress · FastAPI", mbox("POST /webhooks/whatsapp", m.last_turn_at ? "last " + ago(m.last_turn_at) : "no traffic", nodeHealth(T.total, T.failed)) + mbox("POST /v1/simulate", "sync turn", "idle")),
    mlayer("Agent graph · LangGraph", gnodes, "wide"),
    mlayer("Policy gateway", mbox("Scope · allowlist · lockouts", `${Object.values(m.tools).reduce((a, b) => a + b, 0)} gateway events`, nodeHealth(T.total, 0)) + mbox("Denied calls", m.denied + " blocked", m.denied ? "warn" : "idle")),
    mlayer("Tools & systems", toolBoxes, "wide"),
    mlayer("Human approval", tier("PENDING_APPROVER", "Approver") + tier("PENDING_CREDIT_HEAD", "Credit head") + tier("PENDING_COMPLIANCE", "Compliance") + mbox("Approved", (C.APPROVED || 0) + (C.COMMUNICATED || 0), (C.APPROVED || C.COMMUNICATED) ? "ok" : "idle") + mbox("Rejected", C.REJECTED || 0, C.REJECTED ? "bad" : "idle"))
  ].join('<div class="marrow">▼</div>');
  const chips = list.map(t => `<button class="tchip ${t.trace_id === monTrace ? "on" : ""} ${t.status}" data-id="${esc(t.trace_id)}">${esc((t.text || t.message_type || "turn").slice(0, 22))}<small>${esc(t.resolution || "")} · ${ago(t.started_at)}</small></button>`).join("");
  setHTML("monitor", pg("Monitor", "Live map of every stage, the planned route of a turn, and its sequence of events.") + `
    <div class="mtools"><label class="mlive"><input type="checkbox" id="monLive" ${monLive ? "checked" : ""}> Live (5 s)</label>
      <span class="note">${T.total} turns · ${T.failed} failed · ${Object.values(m.resolutions).length ? Object.entries(m.resolutions).map(([k, v]) => k + " " + v).join(" · ") : "no outcomes yet"}</span></div>
    <div class="mmap">${map}</div>
    <h3 style="margin:22px 0 8px">Turn</h3><div class="tchips">${chips || '<span class="note">No turns yet — send a message from the Bot simulator.</span>'}</div>
    <div id="monSeq"></div>`);
  $("#v-monitor").querySelectorAll(".tchip").forEach(b => b.onclick = () => { monTrace = b.dataset.id; RENDER.monitor(); });
  $("#monLive").onchange = e => { monLive = e.target.checked; RENDER.monitor(); };
  if (monTrace) drawSequence(monTrace);
  if (monLive) monTimer = setInterval(() => { if (CURRENT !== "monitor") return clearInterval(monTimer); const y = window.scrollY; RENDER.monitor().then(() => window.scrollTo(0, y)); }, 5000);
};

const CAT_ORDER = ["channel", "understand", "verify", "guard", "resolve", "approval", "escalate", "deliver"];
const SEV_LABEL = { attention: "Needs attention", blocked: "Blocked", waiting: "Waiting on someone", upcoming: "Upcoming", ok: "OK" };
let monHide = new Set(), monQ = null;

/* What the action buttons do. Navigation only: nothing here changes data. */
function monAct(kind, target) {
  if (kind === "open_case") { caseSel = target; go("cases"); }
  else if (kind === "open_conversation") { convSel = target; go("convs"); }
  else if (kind === "open_queue") { go("queues"); }
  else if (kind === "inspect") { traceSel = target; nodeSel = null; go("traces"); }
}
const ACT_LABEL = { open_case: "Open case", open_conversation: "Open conversation", open_queue: "Open Handoff desk", inspect: "Inspect trace" };
const actBtn = a => ACT_LABEL[a.kind] ? `<button class="actbtn ${a.severity}" data-k="${esc(a.kind)}" data-t="${esc(a.target || "")}">${ACT_LABEL[a.kind]} →</button>` : "";

async function drawSequence(id) {
  const box = $("#monSeq"); if (!box) return;
  const q = await api("/admin/api/traces/" + encodeURIComponent(id) + "/sequence").catch(() => null);
  if (!q) { box.innerHTML = ""; return; }
  monQ = q;
  const hidden = e => monHide.has(e.category);
  // 1. category chips = legend + filter
  const chips = CAT_ORDER.filter(c => q.by_category[c]).map(c => `<button class="catchip cat-${c} ${monHide.has(c) ? "off" : ""}" data-c="${c}"><i></i>${esc(q.categories[c])}<b>${q.by_category[c]}</b></button>`).join("");
  // 2. needs-action panel
  const need = q.needs_action.map(e => `<div class="need ${e.action.severity}"><div><span class="pill cat-${e.category}">${esc(e.category_label)}</span> <b>${esc(e.label)}</b>
      <span class="sev ${e.action.severity}">${esc(SEV_LABEL[e.action.severity])}</span><p>${esc(e.action.text)}</p></div>${actBtn(e.action)}</div>`).join("");
  // 3. planned route
  const taken = new Set(q.taken);
  const plan = q.graph_nodes.map(n => `<span class="pnode ${taken.has(n) ? "taken" : "ghost"}">${esc(NODE_LABEL[n] || n)}</span>`).join('<em class="tarrow">·</em>');
  // 4. sequence diagram
  const lanes = q.lanes, LW = 128, RH = 40, PAD = 18, top = 46;
  const W = lanes.length * LW, H = top + q.events.length * RH + PAD;
  const X = l => lanes.indexOf(l) * LW + LW / 2;
  const colr = s => s === "failed" ? "var(--brick)" : s === "waiting" ? "var(--amber)" : s === "projected" ? "var(--muted)" : "var(--accent)";
  const trunc = (t, n) => { t = String(t ?? ""); return t.length > n ? t.slice(0, n - 1) + "…" : t; };
  let svg = `<svg class="seq" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Sequence of events">
    <defs>${["done", "failed", "waiting", "projected"].map(s => `<marker id="ar-${s}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="${colr(s)}"/></marker>`).join("")}</defs>`;
  lanes.forEach((l, i) => { const x = X(l); svg += `<rect x="${i * LW + 6}" y="6" width="${LW - 12}" height="26" rx="8" class="lane-h"/><text x="${x}" y="23" text-anchor="middle" class="lane-t">${esc(l)}</text><line x1="${x}" y1="36" x2="${x}" y2="${H - 6}" class="lifeline"/>`; });
  q.events.forEach((e, i) => {
    const y = top + i * RH + 20, a = X(e.from), b = X(e.to), c = colr(e.state), dash = e.state === "projected" ? 'stroke-dasharray="5 4"' : "";
    const tag = `${e.n}. ${e.label}`, op = hidden(e) ? 0.12 : 1;
    svg += `<g opacity="${op}"><rect x="0" y="${y - 18}" width="5" height="${RH - 4}" class="catband cat-${e.category}"/>`;
    if (a === b) {
      svg += `<rect x="${a - 58}" y="${y - 13}" width="116" height="26" rx="6" class="selfbox ${e.state}"/><text x="${a}" y="${y - 1}" text-anchor="middle" class="ev-t">${esc(trunc(tag, 20))}</text><text x="${a}" y="${y + 9}" text-anchor="middle" class="ev-s">${esc(trunc(e.duration_ms != null ? e.duration_ms + " ms" + (e.sub ? " · " + e.sub : "") : e.sub, 22))}</text>`;
    } else {
      const mid = (a + b) / 2;
      svg += `<line x1="${a}" y1="${y}" x2="${b}" y2="${y}" stroke="${c}" stroke-width="2" ${dash} marker-end="url(#ar-${e.state})"/><text x="${mid}" y="${y - 6}" text-anchor="middle" class="ev-t" fill="${c}">${esc(trunc(tag, 34))}</text>${e.sub ? `<text x="${mid}" y="${y + 12}" text-anchor="middle" class="ev-s">${esc(trunc(e.sub, 40))}</text>` : ""}`;
    }
    svg += `<title>${esc(e.category_label)} · ${esc(e.label)}${e.sub ? " — " + esc(e.sub) : ""}\n${esc(e.action.text)}</title></g>`;
  });
  svg += "</svg>";
  // 5. categorised step table with the actionable for each step
  const rows = q.events.filter(e => !hidden(e)).map(e => `<tr class="${e.action.severity}"><td class="mono">${e.n}</td><td><span class="pill cat-${e.category}">${esc(e.category_label)}</span></td>
      <td><b>${esc(e.label)}</b><div class="note">${esc(e.from)} → ${esc(e.to)}${e.duration_ms != null ? " · " + dur(e.duration_ms) : ""}${e.sub ? " · " + esc(String(e.sub).slice(0, 70)) : ""}</div></td>
      <td><span class="sev ${e.action.severity}">${esc(SEV_LABEL[e.action.severity])}</span></td><td>${esc(e.action.text)} ${actBtn(e.action)}</td></tr>`).join("");
  box.innerHTML = `
    <h3 style="margin:22px 0 8px">Needs action ${q.needs_action.length ? `<span class="sev attention">${q.needs_action.length}</span>` : ""}</h3>
    ${need || '<div class="allclear">Nothing needs doing on this turn.</div>'}
    <h3 style="margin:22px 0 8px">Planned route</h3><div class="pplan">${plan}</div>
    <h3 style="margin:18px 0 8px">Sequence of events <span class="note">· click a category to filter · <i class="lg done"></i>done <i class="lg waiting"></i>waiting <i class="lg failed"></i>failed/denied <i class="lg projected"></i>projected</span></h3>
    <div class="catchips">${chips}</div><div class="seqwrap">${svg}</div>
    <h3 style="margin:18px 0 8px">Steps and what to do about each</h3>
    <div class="steptbl"><table><thead><tr><th>#</th><th>Category</th><th>Step</th><th>Status</th><th>Actionable</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  box.querySelectorAll(".catchip").forEach(b => b.onclick = () => { const c = b.dataset.c; monHide.has(c) ? monHide.delete(c) : monHide.add(c); drawSequence(id); });
  box.querySelectorAll(".actbtn").forEach(b => b.onclick = () => monAct(b.dataset.k, b.dataset.t));
}

/* ============ Traces (per-node input/output + approval tracker) ============ */
let traceSel = null, nodeSel = null;
const NODE_LABEL = { screen: "Screen", normalize: "Normalize", plan: "Plan", verify: "Verify", start_verification: "Start verification",
  execute: "Execute", clarify: "Clarify", handoff: "Handoff", confirm_change: "Confirm change", confirm_action: "Confirm action", output_guard: "Output guard" };
const ROLE_LABEL = { approver: "Approver", credit_head: "Credit head", compliance: "Compliance" };
const jsonBlock = v => `<pre class="tjson">${esc(v == null || (typeof v === "object" && !Object.keys(v).length) ? "—" : JSON.stringify(v, null, 2))}</pre>`;
const dur = ms => ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : ms + " ms";
const stPill = s => `<span class="pill ${s === "failed" || s === "rejected" ? "bad" : s === "waiting" ? "warn" : s === "succeeded" || s === "approved" ? "good" : ""}">${esc(s.replace("_", " "))}</span>`;

RENDER.traces = async function () {
  let list;
  try { list = await api("/admin/api/traces?limit=60"); } catch (e) { list = []; }
  if (!traceSel || !list.find(t => t.trace_id === traceSel)) { traceSel = list[0] ? list[0].trace_id : null; nodeSel = null; }
  const rows = list.length ? list.map(t => `<div class="card ${t.trace_id === traceSel ? "sel" : ""}" data-id="${esc(t.trace_id)}" style="cursor:pointer;margin-bottom:10px;padding:12px 14px">
      <div style="display:flex;justify-content:space-between;gap:8px"><b class="mono">${esc(t.trace_id)}</b>${stPill(t.status)}</div>
      <div style="margin-top:4px">${esc(t.text || "(" + (t.message_type || "no text") + ")")}</div>
      <div class="note" style="margin-top:3px">${esc(t.resolution || "—")} · ${(t.intents || []).map(esc).join(", ") || "no intent"} · ${dur(t.duration_ms)} · ${ago(t.started_at)}</div>
    </div>`).join("") : `<div class="empty">No traces yet. Set <span class="mono">SA_TRACING=true</span>, restart, then send a message from the Bot simulator.</div>`;
  setHTML("traces", pg("Traces", "Every agent turn, node by node: what went in, what came out, and which tools ran. Approval tiers are tracked below each turn.") +
    `<div class="split"><div>${rows}</div><div id="traceDetail"></div></div>`);
  $("#v-traces").querySelectorAll("[data-id]").forEach(b => b.onclick = () => { traceSel = b.dataset.id; nodeSel = null; RENDER.traces(); });
  if (traceSel) openTrace(traceSel);
};

async function openTrace(id) {
  const t = await api("/admin/api/traces/" + encodeURIComponent(id)).catch(() => null);
  const el = $("#traceDetail"); if (!el) return;
  if (!t) { el.innerHTML = `<div class="b"><p class="note">Trace not found.</p></div>`; return; }
  const turn = t.turn || {};
  const nodes = t.spans.map((n, i) => `<button class="tnode ${n.status} ${nodeSel === n.seq ? "on" : ""}" data-seq="${n.seq}">
      <b>${esc(NODE_LABEL[n.name] || n.name)}</b><span>${dur(n.duration_ms)}</span>${n.tool_calls.length ? `<i>${n.tool_calls.length} tool</i>` : ""}</button>${i < t.spans.length - 1 ? '<em class="tarrow">→</em>' : ""}`).join("");
  const sel = t.spans.find(n => n.seq === nodeSel) || t.spans[t.spans.length - 1];
  const detail = sel ? `<h3>${esc(NODE_LABEL[sel.name] || sel.name)} ${stPill(sel.status)}</h3>
    ${sel.error ? `<p class="note" style="color:var(--brick)">${esc(sel.error)}</p>` : ""}
    <div class="io"><div><h4>Input</h4>${jsonBlock(sel.input)}</div><div><h4>Output</h4>${jsonBlock(sel.output)}</div></div>
    ${sel.tool_calls.length ? `<h4>Gateway calls</h4>${jsonBlock(sel.tool_calls)}` : ""}` : `<p class="note">No node spans.</p>`;
  el.innerHTML = `<div class="b"><div class="b-h"><div>${stPill(turn.status || "succeeded")}<h2 class="mono">${esc(id)}</h2>
      <p>${esc((turn.input || {}).text || "")} → ${esc((turn.output || {}).resolution || "")} · ${dur(turn.duration_ms || 0)}</p></div></div>
    <h3 style="margin-top:14px">Pipeline</h3><div class="flow">${nodes}</div>
    <div style="margin-top:12px">${detail}</div>
    <div id="approvalTracker"></div></div>`;
  el.querySelectorAll(".tnode").forEach(b => b.onclick = () => { nodeSel = Number(b.dataset.seq); openTrace(id); });
  if (t.cases && t.cases.length) renderApproval(t.cases);
}

async function renderApproval(caseIds) {
  const box = $("#approvalTracker"); if (!box) return;
  const tls = await Promise.all(caseIds.map(c => api("/admin/api/traces/case/" + encodeURIComponent(c)).catch(() => null)));
  box.innerHTML = tls.filter(Boolean).map(tl => `<h3 style="margin-top:16px">Approval · <span class="mono">${esc(tl.case_id)}</span> ${tl.gaming_flag ? '<span class="pill bad">gaming</span>' : ""}</h3>` +
    tl.versions.map(v => `<div class="tver ${v.current ? "cur" : "old"}"><div class="note">v${v.version} · <b>${esc(v.status)}</b> · ${v.amount != null ? rs(v.amount) : "—"}${v.supersede_reason ? " · superseded: " + esc(v.supersede_reason) : ""}</div>
      <div class="stepper">${v.stages.map(s => `<div class="step ${s.state}"><b>${esc(ROLE_LABEL[s.role] || s.role)}</b>
        <span>${esc(s.state.replace("_", " "))}</span>
        ${s.reviewer ? `<small>${esc(s.reviewer)} · waited ${dur((s.waited_seconds || 0) * 1000)}</small>` : ""}
        ${s.state === "waiting" ? `<small>waiting ${dur((s.waiting_seconds || 0) * 1000)}</small>` : ""}
        ${s.rationale ? `<small>“${esc(s.rationale)}”</small>` : ""}</div>`).join('<em class="tarrow">→</em>')}</div></div>`).join("")).join("");
}

/* ============ Conversations (convs) ============ */
let convSel = null, convQ = "", convFilters = new Set(), convTimer = null, convSig = "", convCounts = {};
const LANG = { hi: "Hindi", kn: "Kannada", ta: "Tamil", te: "Telugu", mr: "Marathi", bn: "Bengali", gu: "Gujarati", ml: "Malayalam", pa: "Punjabi", or: "Odia" };
function applyConvFilter() {
  const q = convQ.trim().toLowerCase();
  document.querySelectorAll("#v-convs .cv-item").forEach(el => {
    const d = el.dataset;
    let ok = !q || (d.search || "").includes(q);
    if (ok) for (const f of convFilters) {
      if (f === "verified") ok = d.verified === "1";
      else if (f === "guest") ok = d.verified === "0";
      else if (f === "tickets") ok = d.tickets !== "0";
      else if (f === "escalated") ok = /escalated/.test(d.outcome);
      else if (f === "fraud") ok = /fraud/.test(d.outcome);
      else if (f.startsWith("lang:")) ok = ("," + (d.langs || "") + ",").includes("," + f.slice(5) + ",");
      if (!ok) break;
    }
    el.hidden = !ok;
  });
}
function convSigOf(list) { return list.map(c => c.conversation_id + ":" + (c.last_at || "") + ":" + c.messages).join("|"); }
function convItemHTML(c) {
  const o = c.outcome || {}, search = ((c.sender || "") + " " + (c.name || "") + " " + (c.last_text || "")).toLowerCase();
  return `<button class="cv-item ${c.conversation_id === convSel ? "on" : ""}" data-id="${esc(c.conversation_id)}"
      data-search="${esc(search)}" data-verified="${c.verified ? 1 : 0}" data-tickets="${c.tickets || 0}"
      data-langs="${esc((c.languages || []).join(","))}" data-outcome="${esc((o.label || "").toLowerCase())}">
    <span class="av">${esc((c.sender || "?").slice(-2))}</span>
    <span class="cv-m"><span class="cv-t"><b>${esc(c.name || c.sender)}</b><time>${ago(c.last_at)}</time></span>
    <span class="cv-l">${esc((c.last_role === "agent" ? "bot: " : "") + (c.last_text || "—"))}</span>
    <span class="cv-i"><span class="ob ob-${esc(o.cls || "muted")}">${esc(o.label || "")}</span>${(c.languages || []).length ? " · " + c.languages.map(l => esc(LANG[l] || l)).join(", ") : ""}${c.tickets ? " · " + c.tickets + "t" : ""}</span></span></button>`;
}
function bindConvItems() {
  $("#v-convs").querySelectorAll(".cv-item").forEach(b => b.onclick = () => {
    convSel = b.dataset.id; openConv(convSel);
    $("#v-convs").querySelectorAll(".cv-item").forEach(x => x.classList.toggle("on", x === b));
  });
  applyConvFilter();
}
function stopConvPoll() { if (convTimer) { clearInterval(convTimer); convTimer = null; } }
function startConvPoll() { stopConvPoll(); convTimer = setInterval(refreshConvs, 8000); }
async function refreshConvs() {
  if (CURRENT !== "convs") { stopConvPoll(); return; }
  const list = await api("/admin/api/conversations").catch(() => null);
  if (!list || CURRENT !== "convs") return;
  const sig = convSigOf(list);
  if (sig === convSig) return;                      // nothing changed; keep focus/scroll untouched
  convSig = sig;
  if (!convSel || !list.find(c => c.conversation_id === convSel)) convSel = list[0] ? list[0].conversation_id : null;
  const cl = $("#v-convs .cv-list");
  if (cl) { cl.innerHTML = list.length ? list.map(convItemHTML).join("") : '<div class="empty">No conversations yet.</div>'; bindConvItems(); }
  const sel = list.find(c => c.conversation_id === convSel);
  if (sel && convCounts[convSel] !== sel.messages) openConv(convSel);   // new message in the open thread
}
RENDER.convs = async function () {
  const list = await api("/admin/api/conversations");
  convSig = convSigOf(list);
  if (!convSel || !list.find(c => c.conversation_id === convSel)) convSel = list[0] ? list[0].conversation_id : null;
  const allLangs = [...new Set(list.flatMap(c => c.languages || []))];
  const fchip = (id, label) => `<button class="cvf ${convFilters.has(id) ? "on" : ""}" data-f="${esc(id)}">${esc(label)}</button>`;
  const filterBar = `${fchip("verified", "Verified")}${fchip("guest", "Guest")}${fchip("tickets", "Has tickets")}${fchip("escalated", "Escalated")}${fchip("fraud", "Fraud")}${allLangs.map(l => fchip("lang:" + l, LANG[l] || l)).join("")}`;
  const items = list.length ? list.map(convItemHTML).join("")
    : `<div class="empty">No conversations yet. Use the Bot simulator or send a WhatsApp message.</div>`;
  setHTML("convs", pg("Conversations", "Live borrower conversations and what the agent did. Auto-refreshes every few seconds.") + `
    <div class="cv-top"><input id="convSearch" class="cv-search" placeholder="Search phone, name or message…" value="${esc(convQ)}" autocomplete="off">
      <div class="cv-filters">${filterBar}</div></div>
    <div class="cv"><div class="cv-list">${items}</div><div id="convChat"></div><div id="convSide"></div></div>`);
  bindConvItems();
  $("#v-convs").querySelectorAll(".cvf").forEach(b => b.onclick = () => { const f = b.dataset.f; convFilters.has(f) ? convFilters.delete(f) : convFilters.add(f); b.classList.toggle("on"); applyConvFilter(); });
  const sb = $("#convSearch");
  if (sb) sb.oninput = () => { convQ = sb.value; applyConvFilter(); };
  if (convSel) openConv(convSel);
  else { $("#convChat").innerHTML = ""; $("#convSide").innerHTML = ""; }
  startConvPoll();
};
async function openConv(cid) {
  const c = await api("/admin/api/conversations/" + encodeURIComponent(cid)).catch(() => null);
  if (!c) { $("#convChat").innerHTML = '<div class="empty">Not found.</div>'; return; }
  const bubbles = (c.transcript || []).map(m => {
    const side = m.role === "borrower" ? "in" : "out";
    let inner = "";
    if (m.media_id) {
      const url = "/admin/api/media/" + encodeURIComponent(m.media_id);
      if (m.type === "voice" || m.media_kind === "voice_note") inner += `<audio controls preload="none" src="${url}"></audio>`;
      else if (m.type === "image") inner += `<img src="${url}" alt="screenshot" loading="lazy">`;
      if (m.media_text) inner += `<div class="tr">“${esc(m.media_text)}”</div>`;
      else if (!m.text) inner += `<div class="tr">${m.type === "voice" ? "🎤 voice note" : "📎 attachment"}${m.media_kind && m.media_kind !== "other" ? " · " + esc(m.media_kind) : ""}</div>`;
    } else if (m.type === "voice" || m.type === "image") {
      inner += `<div class="tr">${m.type === "voice" ? "🎤 voice note" : "📎 image"} (not downloaded)</div>`;
    }
    if (m.text) inner += esc(m.text);
    return `<div class="bb ${side}">${inner || "…"}</div>`;
  }).join("") || '<div class="sys">No messages.</div>';
  const ev = c.events || [];
  const uniq = a => [...new Set(a)];
  const intents = uniq(ev.flatMap(e => e.data && e.data.intent ? [e.data.intent] : []));
  const langs = uniq(ev.flatMap(e => e.data && e.data.language && e.data.language !== "en" ? [e.data.language] : []));
  const o = c.outcome || {}, p = c.profile;
  const detail = e => { const d = e.data || {}; return d.intent || d.tool || d.queue || d.verdict || d.result || d.request_type || d.reason || d.stage || d.language || d.method || ""; };
  const trace = ev.filter(e => e.type && e.type !== "turn_normalized").slice(-16).map(e =>
    `<div class="ts ${/(block|flag|fail|denied|injection)/.test(e.type) ? "flag" : /(verified|approved|communicated)/.test(e.type) ? "ok" : ""}">${esc(e.type)}${detail(e) ? ` · <span class="muted">${esc(String(detail(e)).slice(0, 28))}</span>` : ""}</div>`).join("");
  const profileCard = p ? `<div class="sd-card"><span class="sd-k">Borrower</span><b>${esc(p.name || "—")}</b>
      <div class="sd-grid"><span><i>Account</i>${esc(p.account_id)}</span><span><i>Product</i>${esc(p.product || "—")}</span>
      <span><i>DPD</i>${p.dpd != null ? p.dpd : "—"}</span><span><i>EMI</i>${p.emi_amount != null ? rs(p.emi_amount) : "—"}</span>
      <span><i>Overdue</i>${p.overdue_amount != null ? rs(p.overdue_amount) : "—"}</span><span><i>Next due</i>${esc(p.next_due_date || "—")}</span></div></div>` : "";
  const payments = (c.payments || []).map(pm => `<div class="pay-row pay-${esc((pm.result || "").toLowerCase())}">
      <span class="pay-v">${esc(pm.result || "—")}</span>
      <span class="pay-m">${pm.amount != null ? rs(pm.amount) : ""}${pm.utr ? ` · UTR ••••${esc(String(pm.utr).slice(-4))}` : ""}</span>
      ${pm.queue ? `<span class="pay-q">${esc(pm.queue)}</span>` : ""}</div>`).join("");
  const ticketChips = (c.tickets || []).map(t => `<button class="lchip" data-tkt="${esc(t.ticket_id)}">${esc(t.ticket_id)} · ${esc(t.request_type)}<small>${esc(t.status)}</small></button>`).join("");
  const caseChips = (c.cases || []).map(cs => `<button class="lchip" data-case="${esc(cs.case_id)}">${esc(cs.case_id)} · ${esc(cs.type)}<small>${esc(cs.status)}</small></button>`).join("");
  $("#convChat").innerHTML = `<div class="cv-chat"><div class="ch-head"><span class="av">${esc(cid.slice(-2))}</span><div><b>${esc(p && p.name ? p.name : cid)}</b><span>${c.verified ? "verified session · " + esc(cid) : "unverified · " + esc(cid)}</span></div></div><div class="ch-body">${bubbles}</div></div>`;
  $("#convSide").innerHTML = `<div class="cv-side">
    <div class="sd-card"><span class="sd-k">Assessment</span><span class="ob ob-${esc(o.cls || "muted")}">${esc(o.label || "—")}</span></div>
    ${profileCard}
    <div class="sd-card"><span class="sd-k">Intents</span><div class="pchips">${intents.length ? intents.map(i => `<span>${esc(i)}</span>`).join("") : '<span class="muted">none</span>'}</div></div>
    ${langs.length ? `<div class="sd-card"><span class="sd-k">Languages</span><div class="pchips">${langs.map(l => `<span>${esc(LANG[l] || l)}</span>`).join("")}</div></div>` : ""}
    ${payments ? `<div class="sd-card"><span class="sd-k">Payment claims</span><div class="pay-list">${payments}</div></div>` : ""}
    ${ticketChips ? `<div class="sd-card"><span class="sd-k">Tickets</span><div class="lchips">${ticketChips}</div></div>` : ""}
    ${caseChips ? `<div class="sd-card"><span class="sd-k">Cases</span><div class="lchips">${caseChips}</div></div>` : ""}
    <details class="sd-trace" open><summary>Agent trace (${ev.length})</summary><div class="tt">${trace || '<span class="muted">no events</span>'}</div></details></div>`;
  $("#convSide").querySelectorAll("[data-tkt]").forEach(b => b.onclick = () => { deskSel = b.dataset.tkt; deskGroup = ""; go("queues"); });
  $("#convSide").querySelectorAll("[data-case]").forEach(b => b.onclick = () => { caseSel = b.dataset.case; go("cases"); });
  convCounts[cid] = (c.transcript || []).length;        // so the poller knows when a new message arrives
  const body = $("#convChat .ch-body"); if (body) body.scrollTop = body.scrollHeight;   // show the latest
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
  const [g, cfg, feed] = await Promise.all([api("/admin/api/metrics/guardrails"), api("/admin/api/config"), api("/admin/api/guardrails/events").catch(() => [])]);
  const z = [["Shared before verification", g.shared_before_verification, "nothing disclosed pre-OTP"],
             ["Fake proofs blocked", g.fake_proofs_blocked, "tampered/reused UTRs"],
             ["Injections blocked", g.injections_blocked, "prompt-injection attempts"],
             ["Outputs blocked", g.output_blocked, "unsafe replies caught"]];
  const routes = Object.entries(cfg.routing_matrix.routes || cfg.routing_matrix || {});
  const rrows = routes.map(([k, r]) => `<tr><td><b>${esc(k)}</b></td><td>${esc(r.queue)}</td><td><span class="pill ${r.priority}">${esc(r.priority)}</span></td><td class="r">${r.sla_hours}h</td></tr>`).join("");
  const bands = ((cfg.policy_bands.bands || {}).default) || {};
  const brows = Object.entries(bands).map(([b, v]) => `<tr><td><b>DPD ${esc(b)}</b></td><td class="r">${v.max_penal_waiver_pct}%</td><td class="r">${v.max_principal_waiver_pct}%</td></tr>`).join("");
  const sev = t => t === "fake_proof_blocked" ? "crit" : t === "security_alert" ? "crit" : "warn";
  const feedRows = feed.length ? feed.map(f => `<li><span class="sev ${sev(f.type)}"></span><div><b>${esc(f.type.replace(/_/g, " "))}</b>${f.detail ? ` · ${esc(f.detail)}` : ""}<br><span class="note">${ago(f.at)}</span></div></li>`).join("")
    : '<li class="note">No guardrail actions yet — nothing blocked.</li>';
  setHTML("guards", pg("Guardrails", "Hard limits the agent enforces, and the policy it routes by.") + `
    <div class="zero6" style="grid-template-columns:repeat(4,minmax(0,1fr))">${z.map(([l, v, s]) => `<div><b class="num">${v}</b><span>${esc(l)}</span><em>${esc(s)}</em></div>`).join("")}</div>
    <div class="card" style="margin-bottom:16px"><header><h2>Recent blocked attempts</h2><span>live feed</span></header><ul class="feed">${feedRows}</ul></div>
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
health(); badges();
(function start() {
  const v = (location.hash || "").replace("#", "");
  if (v && document.querySelector(`#nav button[data-v="${v}"]`)) { go(v); return; }
  RENDER.convs();
})();
