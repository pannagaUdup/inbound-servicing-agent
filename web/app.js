"use strict";
const $ = (s, r = document) => r.querySelector(s);
const el = (t, props = {}, kids = []) => { const e = document.createElement(t); Object.assign(e, props); for (const k of [].concat(kids)) e.append(k); return e; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
async function api(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw body;
  return body;
}
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2400); }

const STATE_ORDER = ["queued", "sent", "delivered", "read", "acknowledged", "otp_sent", "verified", "awaiting_payment", "closed", "skipped", "failed"];
function statePill(s) {
  const good = ["verified", "awaiting_payment"], bad = ["failed", "skipped"], warn = ["otp_sent", "acknowledged"];
  const cls = good.includes(s) ? "ok" : bad.includes(s) ? "bad" : warn.includes(s) ? "warn" : "brand";
  return `<span class="pill ${cls}">${s}</span>`;
}

/* ---------------- theme + tabs + health ---------------- */
(function theme() {
  const root = document.documentElement;
  try { const s = localStorage.getItem("cn-theme"); if (s) root.setAttribute("data-theme", s); } catch (e) {}
  $("#themeBtn").onclick = () => {
    const cur = root.getAttribute("data-theme");
    const sysDark = matchMedia("(prefers-color-scheme: dark)").matches;
    const next = (cur ? cur === "dark" : sysDark) ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("cn-theme", next); } catch (e) {}
  };
})();
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === t));
  ["campaigns", "bot"].forEach(id => $("#" + id).classList.toggle("hidden", id !== t.dataset.t));
  render(t.dataset.t);
});
function render(t) { ({ campaigns: renderCampaigns, bot: renderBot }[t])(); }
async function health() { try { const h = await api("/healthz"); $("#health").textContent = `${h.accounts} accounts · planner: ${h.planner}`; } catch (e) { $("#health").textContent = "offline"; } }

/* ---------------- Campaigns ---------------- */
let picked = new Set(), viewing = null;
async function renderCampaigns() {
  const [ov, list] = await Promise.all([api("/admin/api/overview"), api("/admin/api/campaigns")]);
  const sec = $("#campaigns");
  sec.innerHTML = "";
  const stats = el("div", { className: "cards", style: "margin-bottom:18px" });
  stats.innerHTML = `<div class="stat"><b>${ov.accounts}</b><span>accounts</span></div>
    <div class="stat"><b>${ov.campaigns}</b><span>campaigns</span></div>
    <div class="stat"><b>${ov.open_cases}</b><span>open waiver cases</span></div>`;
  sec.append(stats);

  const row = el("div", { className: "row" });
  // --- create panel
  const create = el("div", { className: "panel col" });
  create.innerHTML = `<h2>New campaign</h2><div class="sub">Select accounts from our DB, then create and launch.</div>
    <label>Name</label><input id="cname" placeholder="Nov payment confirmation">
    <label>Message <span class="muted">(placeholder until a template is approved — leave blank for default)</span></label>
    <textarea id="cbody" rows="2" placeholder="Default greeting will be used"></textarea>
    <label>Search account</label><input id="fsearch" placeholder="Account ID or city — e.g. CN9248665532">
    <div class="filters" style="margin-top:12px">
      <div><label>Min DPD</label><input id="fdpd" type="number" min="0" placeholder="0"></div>
      <div><label>Limit</label><input id="flim" type="number" value="50"></div>
      <div style="flex:0"><label>&nbsp;</label><button class="ghost" id="loadBtn">Load accounts</button></div>
    </div>
    <label>Accounts — <span id="pcount">0</span> selected</label>
    <div class="scroll" id="acclist"><div class="acc muted">Load accounts to pick.</div></div>
    <div style="margin-top:14px;display:flex;gap:10px"><button id="createBtn">Create campaign</button></div>`;
  row.append(create);

  // --- list panel
  const listPanel = el("div", { className: "panel col" });
  const rows = list.map(c => `<tr><td>${esc(c.name)}</td><td>${statePillCampaign(c.status)}</td>
      <td class="mono">${c.targets}/${c.total_targets}</td><td class="muted">${c.created_at}</td>
      <td style="white-space:nowrap"><button class="ghost sm" data-view="${c.campaign_id}">view</button>
      ${c.status === "draft" ? `<button class="sm" data-launch="${c.campaign_id}">launch</button>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">No campaigns yet.</td></tr>`;
  listPanel.innerHTML = `<h2>Campaigns</h2><div class="sub">Launch sends the message to each target's registered number.</div>
    <table><thead><tr><th>Name</th><th>Status</th><th>Targets</th><th>Created</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    <div id="cdetail" style="margin-top:16px"></div>`;
  row.append(listPanel);
  sec.append(row);

  // wire create panel
  $("#loadBtn").onclick = loadAccounts;
  let _st; $("#fsearch").oninput = () => { clearTimeout(_st); _st = setTimeout(loadAccounts, 300); };
  $("#fsearch").onkeydown = e => { if (e.key === "Enter") { clearTimeout(_st); loadAccounts(); } };
  $("#createBtn").onclick = createCampaign;
  // wire list
  listPanel.querySelectorAll("[data-view]").forEach(b => b.onclick = () => viewCampaign(b.dataset.view));
  listPanel.querySelectorAll("[data-launch]").forEach(b => b.onclick = () => launch(b.dataset.launch));
  if (viewing) viewCampaign(viewing);
}
function statePillCampaign(s) { return `<span class="pill ${s === "running" ? "ok" : s === "completed" ? "info" : ""}">${s}</span>`; }

async function loadAccounts() {
  const dpd = $("#fdpd").value, lim = $("#flim").value || 50, search = $("#fsearch").value.trim();
  const q = new URLSearchParams({ limit: lim }); if (dpd) q.set("dpd_min", dpd); if (search) q.set("q", search);
  const accs = await api("/admin/api/accounts?" + q);
  picked = new Set(); $("#pcount").textContent = "0";
  const list = $("#acclist"); list.innerHTML = "";
  const head = el("div", { className: "acc", style: "position:sticky;top:0;background:var(--surface)" });
  head.innerHTML = `<input type="checkbox" id="selall"> <b>Select all contactable</b>`;
  list.append(head);
  accs.forEach(a => {
    const r = el("label", { className: "acc" });
    r.innerHTML = `<input type="checkbox" value="${a.account_id}" ${a.contactable ? "" : "disabled"}>
      <span class="ai">${a.account_id}</span> <span class="muted">${a.product} · DPD ${a.dpd} · ₹${a.overdue_amount} · ${a.city || ""}</span>
      ${a.contactable ? "" : '<span class="pill bad" style="margin-left:auto">no contact</span>'}`;
    const cb = r.querySelector("input");
    cb.onchange = () => { cb.checked ? picked.add(a.account_id) : picked.delete(a.account_id); $("#pcount").textContent = picked.size; };
    list.append(r);
  });
  $("#selall").onchange = e => {
    list.querySelectorAll('input[type=checkbox]:not(#selall):not(:disabled)').forEach(cb => { cb.checked = e.target.checked; cb.checked ? picked.add(cb.value) : picked.delete(cb.value); });
    $("#pcount").textContent = picked.size;
  };
}
async function createCampaign() {
  if (!picked.size) return toast("Pick at least one account");
  const body = { name: $("#cname").value || "Untitled campaign", message_body: $("#cbody").value || null, account_ids: [...picked] };
  try { const c = await api("/admin/api/campaigns", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); toast("Created " + c.campaign_id); viewing = c.campaign_id; renderCampaigns(); }
  catch (e) { toast("Error: " + (e.detail || JSON.stringify(e))); }
}
async function launch(id) { try { await api("/admin/api/campaigns/" + id + "/launch", { method: "POST" }); toast("Launched " + id); viewing = id; renderCampaigns(); } catch (e) { toast("Launch failed"); } }
async function viewCampaign(id) {
  viewing = id;
  const c = await api("/admin/api/campaigns/" + id);
  const lanes = STATE_ORDER.filter(s => c.by_state[s]).map(s => {
    const chips = c.targets.filter(t => t.state === s).map(t => `<div class="chip">${t.account_id}</div>`).join("");
    return `<div class="lane"><h4>${s}<span class="cnt">${c.by_state[s]}</span></h4>${chips}</div>`;
  }).join("") || '<span class="muted">No targets.</span>';
  $("#cdetail").innerHTML = `<h2>${esc(c.name)} <span class="muted mono">${c.campaign_id}</span></h2>
    <div class="sub">${esc(c.message_body || "")}</div>
    <div style="display:flex;gap:8px;margin:8px 0"><button class="ghost sm" onclick="viewCampaign('${id}')">refresh</button>
    <button class="ghost sm" onclick="gotoBot('${id}')">test in bot simulator →</button></div>
    <div class="board">${lanes}</div>`;
}
window.viewCampaign = viewCampaign;
window.gotoBot = (id) => { botCampaign = id; document.querySelector('.tab[data-t="bot"]').click(); };

/* ---------------- Bot simulator ---------------- */
let botCampaign = null, botTarget = null, botLog = [];
async function renderBot() {
  const list = await api("/admin/api/campaigns");
  const sec = $("#bot");
  const opts = list.map(c => `<option value="${c.campaign_id}" ${c.campaign_id === botCampaign ? "selected" : ""}>${esc(c.name)} (${c.status})</option>`).join("");
  sec.innerHTML = `<div class="panel"><h2>Bot simulator</h2>
    <div class="sub">Play a borrower; the bot runs the real campaign journey (ack → OTP → verify) and the agent. OTP code in dev is <b class="mono">123456</b>.</div>
    <div class="row">
      <div style="flex:1;min-width:220px"><label>Campaign</label><select id="botcamp"><option value="">— pick a campaign —</option>${opts}</select></div>
      <div style="flex:1;min-width:220px"><label>Target (borrower)</label><select id="bottgt"><option value="">—</option></select></div>
    </div>
    <div class="statebar" id="statebar"><span class="muted">Pick a launched campaign and a target to begin.</span></div>
    <div class="chat">
      <div class="chatlog" id="chatlog"><div class="sysline">No messages yet.</div></div>
      <div class="composer"><input id="botinput" placeholder="Type a reply as the borrower…" autocomplete="off" disabled>
        <button id="botsend" disabled>Send</button></div>
    </div>
    <div class="quick" id="quick"></div>
  </div>`;
  $("#botcamp").onchange = e => { botCampaign = e.target.value || null; botTarget = null; loadTargets(); };
  $("#bottgt").onchange = e => { botTarget = JSON.parse(e.target.value || "null"); resetChat(); };
  $("#botsend").onclick = sendBot;
  $("#botinput").onkeydown = e => { if (e.key === "Enter") sendBot(); };
  ["yes, I've paid", "no", "123456", "send my statement", "I want to foreclose"].forEach(q => {
    const b = el("button", { textContent: q, onclick: () => { $("#botinput").value = q; sendBot(); } });
    $("#quick").append(b);
  });
  if (botCampaign) loadTargets();
}
async function loadTargets() {
  const sel = $("#bottgt");
  if (!botCampaign) { sel.innerHTML = '<option value="">—</option>'; return; }
  const tgts = await api(`/admin/api/campaigns/${botCampaign}/sim-targets`);
  sel.innerHTML = '<option value="">— pick a target —</option>' + tgts.filter(t => t.to).map(t =>
    `<option value='${esc(JSON.stringify(t))}'>${t.account_id} · ${t.state}</option>`).join("");
}
function resetChat() { botLog = []; drawChat(); updateStatebar(); const on = !!(botTarget && botTarget.to); $("#botinput").disabled = !on; $("#botsend").disabled = !on; }
function updateStatebar() {
  if (!botTarget) { $("#statebar").innerHTML = '<span class="muted">Pick a target to begin.</span>'; return; }
  $("#statebar").innerHTML = `<b class="mono">${botTarget.account_id}</b> ${statePill(botTarget.state)}
    <span class="muted">journey state</span>`;
}
function drawChat() {
  const log = $("#chatlog");
  if (!botLog.length) { log.innerHTML = '<div class="sysline">Say something to start the conversation.</div>'; return; }
  log.innerHTML = botLog.map(m => m.sys ? `<div class="sysline">${esc(m.text)}</div>`
    : `<div class="bub ${m.who}"><span class="tag">${m.who === "me" ? "borrower" : "bot · " + (m.src || "")}</span>${esc(m.text)}</div>`).join("");
  log.scrollTop = log.scrollHeight;
}
async function sendBot() {
  if (!botTarget || !botTarget.to) return;
  const input = $("#botinput"); const text = input.value.trim(); if (!text) return;
  input.value = ""; botLog.push({ who: "me", text }); drawChat();
  try {
    const res = await api("/admin/api/simulate", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sender: botTarget.to, text }) });
    (res.replies || []).forEach(t => botLog.push({ who: "bot", text: t, src: res.source }));
    if (!res.replies || !res.replies.length) botLog.push({ sys: true, text: `(no reply · source: ${res.source}${res.verified ? " · verified" : ""})` });
    if (res.state) { botTarget.state = res.state; updateStatebar(); }
    drawChat();
  } catch (e) { botLog.push({ sys: true, text: "error: " + (e.detail || "request failed") }); drawChat(); }
}

health();
renderCampaigns();
