"""Traces console API (read-only): per-turn node traces and the approval / compliance tracker.

Everything here reads ``trace_spans`` (written when SA_TRACING=true), ``audit_events``, and the
case tables. No endpoint writes, so it cannot affect the conversation or the approval flow.
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select

from servicing_agent.agent.tools.reconciliation import india_now
from servicing_agent.platform.db import AuditEvent, Case, CaseVersion, Decision, TraceSpan

# Order in which a case moves through the maker-checker tiers.
STAGES = ("approver", "credit_head", "compliance")
PENDING = {"approver": "PENDING_APPROVER", "credit_head": "PENDING_CREDIT_HEAD", "compliance": "PENDING_COMPLIANCE"}


def _span(r: TraceSpan) -> dict:
    return {"trace_id": r.trace_id, "conversation_id": r.conversation_id, "seq": r.seq, "name": r.name,
            "status": r.status, "started_at": r.started_at.isoformat(), "duration_ms": r.duration_ms,
            "input": r.input, "output": r.output, "tool_calls": r.tool_calls or [], "error": r.error}


def _case_ids(spans: list[TraceSpan]) -> list[str]:
    seen: list[str] = []
    for r in spans:
        for c in r.tool_calls or []:
            cid = c.get("case_id")
            if cid and cid not in seen:
                seen.append(cid)
    return seen


def build_case_timeline(case: Case, versions: list[CaseVersion], decisions: list[Decision],
                        requires_credit_head: bool, now: datetime) -> dict:
    out_versions = []
    for v in versions:
        ds = [d for d in decisions if d.version == v.version]
        by_role = {d.role: d for d in ds}
        started = v.created_at
        stages, waiting_since = [], started
        rejected = any(d.decision == "reject" for d in ds)
        for role in STAGES:
            d = by_role.get(role)
            if role == "credit_head" and not d and v.status != PENDING[role] and not requires_credit_head:
                stages.append({"role": role, "state": "skipped"})
                continue
            if d:
                stages.append({"role": role, "state": "rejected" if d.decision == "reject" else "approved",
                               "reviewer": d.reviewer_id, "rationale": d.rationale, "conditions": d.conditions,
                               "decided_at": d.created_at.isoformat(),
                               "waited_seconds": int((d.created_at - waiting_since).total_seconds())})
                waiting_since = d.created_at
            elif v.status == PENDING[role]:
                stages.append({"role": role, "state": "waiting", "since": waiting_since.isoformat(),
                               "waiting_seconds": int((now - waiting_since).total_seconds())})
            else:
                stages.append({"role": role, "state": "superseded" if v.status == "SUPERSEDED" and not rejected
                               else "not_reached"})
        out_versions.append({
            "version": v.version, "status": v.status, "type": v.type, "amount": v.amount,
            "created_at": started.isoformat(), "diff": v.diff, "superseded_by": v.superseded_by,
            "supersede_reason": v.supersede_reason, "current": v.version == case.current_version,
            "stages": stages,
        })
    return {"case_id": case.case_id, "conversation_id": case.conversation_id, "current_version": case.current_version,
            "restarts": case.restarts, "gaming_flag": case.gaming_flag, "versions": out_versions}


# Lanes of the sequence diagram, left to right (mirrors the architecture layers that exist in this repo).
LANES = ["Borrower", "Ingress", "Agent graph", "Policy gateway", "Tools & systems", "Case service",
         "Approver", "Compliance", "Outbound"]
ROLE_LANE = {"approver": "Approver", "credit_head": "Approver", "compliance": "Compliance"}
# The route the graph can take. The "projected plan" ghosts the nodes a turn did not take.
GRAPH_NODES = ["screen", "normalize", "handoff", "confirm_change", "confirm_action", "verify", "plan",
               "clarify", "start_verification", "execute", "output_guard"]


def _tool_lane(call: dict) -> tuple[str, str]:
    """(target lane, label) for one gateway event."""
    t = call.get("type")
    if t == "case_created":
        return "Case service", f"create case {call.get('case_id', '')} v{call.get('version', 1)}"
    if t == "case_superseded":
        return "Case service", f"supersede v{call.get('from_version')} -> v{call.get('to_version')}"
    if t == "ticket_created":
        return "Tools & systems", f"ticket {call.get('queue', '')}"
    if t == "tool_denied":
        return "Policy gateway", f"DENIED {call.get('tool', '')}: {call.get('reason', '')}"
    tool = call.get("tool") or t
    return ("Case service" if str(tool).startswith("cases.") else "Tools & systems"), str(tool)


def build_sequence(turn: dict | None, spans: list[dict], case_timelines: list[dict]) -> list[dict]:
    """Ordered sequence of events for one turn, then the approval loop it started, then what is still projected.

    ``state``: done | failed | waiting | projected (not yet happened, derived from the approval chain).
    """
    ev: list[dict] = []

    def add(src, dst, label, state="done", sub=None, at=None, ms=None, **ref):
        ev.append({"n": len(ev) + 1, "from": src, "to": dst, "label": label, "sub": sub, "state": state,
                   "at": at, "duration_ms": ms, "ref": ref})

    inp = (turn or {}).get("input") or {}
    add("Borrower", "Ingress", "inbound message", sub=inp.get("text") or f"({inp.get('type', 'message')})",
        at=(turn or {}).get("started_at"), kind="inbound")
    add("Ingress", "Agent graph", "run turn", at=(turn or {}).get("started_at"))
    for sp in spans:
        out = sp.get("output") or {}
        sub = None
        if out.get("tasks"):
            sub = ", ".join(t["intent"] for t in out["tasks"] if t.get("intent"))
        elif out.get("resolutions"):
            sub = ", ".join(out["resolutions"])
        add("Agent graph", "Agent graph", sp["name"], "failed" if sp["status"] == "failed" else "done",
            sub=sp.get("error") or sub, at=sp["started_at"], ms=sp["duration_ms"], node=sp["name"],
            error=sp.get("error"), resolutions=out.get("resolutions") or [], events=out.get("events") or [],
            tech={"input": sp.get("input"), "output": sp.get("output"), "tool_calls": sp.get("tool_calls") or [],
                  "error": sp.get("error")})
        for call in sp.get("tool_calls") or []:
            lane, label = _tool_lane(call)
            denied = call.get("type") == "tool_denied"
            add("Agent graph", "Policy gateway", label,
                "failed" if denied else "done", at=sp["started_at"], call=call)
            if not denied and lane != "Policy gateway":
                add("Policy gateway", lane, label, at=sp["started_at"], call=call)
    for r in ((spans[-1].get("output") or {}).get("final_replies") if spans else None) or []:
        add("Agent graph", "Outbound", "reply: " + (r.get("template_id") or ""), sub=r.get("text"),
            at=spans[-1]["started_at"], kind="reply")
        add("Outbound", "Borrower", "send reply", at=spans[-1]["started_at"], kind="send")

    for tl in case_timelines:
        for v in tl["versions"]:
            tag = f"{tl['case_id']} v{v['version']}"
            add("Case service", "Case service", f"{tag} created", at=v["created_at"], sub=v["status"],
                kind="case", case_id=tl["case_id"])
            if v["status"] == "SUPERSEDED":
                add("Case service", "Case service", f"{tag} superseded", sub=v.get("supersede_reason"), at=None,
                    kind="superseded", case_id=tl["case_id"])
                continue
            prev = "Case service"
            for st in v["stages"]:
                lane = ROLE_LANE[st["role"]]
                name = st["role"].replace("_", " ")
                if st["state"] == "skipped":
                    continue
                if st["state"] in ("approved", "rejected"):
                    add(prev, lane, f"{tag}: review requested ({name})", at=None, kind="review", role=st["role"],
                        case_id=tl["case_id"])
                    add(lane, "Case service", f"{name} {st['state']}", "failed" if st["state"] == "rejected" else "done",
                        sub=f"{st.get('reviewer', '')} · {st.get('rationale') or ''}".strip(" ·"),
                        at=st.get("decided_at"), ms=(st.get("waited_seconds") or 0) * 1000, kind="decision",
                        role=st["role"], case_id=tl["case_id"], decision=st["state"], reviewer=st.get("reviewer"))
                    prev = "Case service"
                    if st["state"] == "rejected":
                        break
                elif st["state"] == "waiting":
                    add(prev, lane, f"{tag}: review requested ({name})", "waiting",
                        sub=f"waiting {st.get('waiting_seconds', 0)} s", at=st.get("since"), kind="review",
                        role=st["role"], case_id=tl["case_id"], waiting_seconds=st.get("waiting_seconds", 0))
                else:  # not reached yet -> projected
                    add("Case service", lane, f"{tag}: review requested ({name})", "projected", kind="review",
                        role=st["role"], case_id=tl["case_id"])
            if v["status"] not in ("APPROVED", "REJECTED", "COMMUNICATED"):
                add("Case service", "Outbound", "notify borrower of outcome", "projected", kind="notify",
                    case_id=tl["case_id"])
                add("Outbound", "Borrower", "outcome message", "projected", kind="send")
    conv = (turn or {}).get("conversation_id")
    return [annotate(e, conv, (turn or {}).get("trace_id")) for e in ev]


# ---- categorisation + actionables ---------------------------------------------------------------
# Every step belongs to one category, and carries what an operator can do about it.
CATEGORIES = {
    "channel": "Channel", "understand": "Understand", "verify": "Verify", "guard": "Guard",
    "resolve": "Resolve", "approval": "Approval", "escalate": "Escalate", "deliver": "Deliver",
}
NODE_CATEGORY = {"screen": "understand", "normalize": "understand", "plan": "understand", "clarify": "understand",
                 "verify": "verify", "start_verification": "verify", "handoff": "escalate",
                 "confirm_change": "resolve", "confirm_action": "resolve", "execute": "resolve", "output_guard": "guard"}
APPROVAL_SLA_SECONDS = 900          # a reviewer waiting longer than this is flagged as overdue

STAGE_LABEL = {
    "borrower": "Borrower", "ingress": "Message received", "screen": "Safety check", "normalize": "Language check",
    "handoff": "Handoff to a person", "confirm_change": "Confirm change", "confirm_action": "Confirm request",
    "verify": "Identity check", "plan": "Understand request", "start_verification": "Start identity check",
    "clarify": "Ask a question", "execute": "Resolve request", "output_guard": "Reply safety check",
    "outbound": "Send reply", "gateway": "Access control", "tools": "Customer systems", "case": "Approval case",
    "approver": "Approver", "credit_head": "Credit head", "compliance": "Compliance",
}
STAGE_ABOUT = {
    "borrower": "The customer on WhatsApp: where every conversation starts and ends.",
    "ingress": "Receives the WhatsApp message, rejects duplicates and hands it to the assistant.",
    "screen": "Checks the message for abuse, distress, legal threats or attempts to trick the assistant.",
    "normalize": "Translates regional or mixed-language messages so they can be understood accurately.",
    "plan": "Works out what the borrower is asking for, possibly several things at once.",
    "verify": "Confirms the borrower is who they say they are (account number plus a one-time code).",
    "start_verification": "Asks the borrower to identify themselves before any account detail is shared.",
    "clarify": "Asks one question when the request is unclear. A second unclear reply goes to a person.",
    "confirm_change": "Handles the borrower's yes or no to a change they were asked to confirm.",
    "confirm_action": "Handles the borrower's yes or no before a waiver or settlement request is submitted.",
    "execute": "Carries out the request: looks up details, makes documents, opens cases or tickets.",
    "handoff": "Passes the conversation to a person (distress, legal, harassment, complaints).",
    "gateway": "Only lets the assistant see the verified borrower's own account, and only the fields allowed.",
    "tools": "The systems that hold the facts: loan records, payments, documents and tickets.",
    "case": "Holds waiver and settlement requests while they go through review.",
    "approver": "First reviewer of a waiver or settlement request.",
    "credit_head": "Extra reviewer for large requests.",
    "compliance": "Final reviewer: checks the request is within policy.",
    "output_guard": "Checks every reply for wrong numbers, promises the assistant cannot make and personal data.",
    "outbound": "Sends the reply to the borrower on WhatsApp.",
}
RESOLUTION_PLAIN = {"auto": "resolved automatically", "routed": "sent to a person", "pending_verification": "waiting for identity check",
                    "clarify": "needs clarification", "open": "still open"}
TOOL_PLAIN = {"lms.dues": "what the borrower owes", "lms.foreclosure": "the foreclosure amount", "lms.statement": "the account statement",
              "payments.history": "payment history", "payments.owner": "who made a payment", "claims.assess": "a payment claim",
              "claims.record": "a payment claim", "paylinks.create": "a payment link", "policy.search": "the servicing policy",
              "cases.revision_limit": "how often this request changed", "cases.restarts": "how often this request changed"}


def humanize(text) -> str:
    return str(text or "").replace("_", " ").replace(".", " ").strip()


def _tool_plain(call: dict) -> str:
    tool = call.get("tool") or ""
    return TOOL_PLAIN.get(tool) or humanize(tool) or humanize(call.get("type"))


def stage_for(e: dict, ref: dict) -> str | None:
    """Which box of the workflow picture this step belongs to (None = not drawn)."""
    f, t, node = e["from"], e["to"], ref.get("node")
    if f == "Borrower" and t == "Ingress":
        return "ingress"
    if f == "Ingress":
        return None
    if node:
        return node if node in STAGE_LABEL else "execute"
    pairs = {("Agent graph", "Policy gateway"): "gateway", ("Policy gateway", "Tools & systems"): "tools",
             ("Policy gateway", "Case service"): "case", ("Agent graph", "Outbound"): "outbound",
             ("Outbound", "Borrower"): "borrower", ("Case service", "Case service"): "case",
             ("Case service", "Outbound"): "outbound"}
    if (f, t) in pairs:
        return pairs[(f, t)]
    if "Approver" in (f, t):
        return "credit_head" if "credit" in e["label"] else "approver"
    if "Compliance" in (f, t):
        return "compliance"
    return None


def plain_text(e: dict, node: str | None, ref: dict) -> str:
    """One sentence an operations person can read, with no internal names."""
    st, call, kind = e["state"], ref.get("call") or {}, ref.get("kind")
    role = (ref.get("role") or "").replace("_", " ")
    sub = e.get("sub")
    if kind == "inbound":
        return f"Borrower message received: “{sub}”" if sub else "Borrower message received."
    if e["from"] == "Ingress":
        return "Message handed to the assistant."
    if node:
        if st == "failed":
            return f"Something went wrong during “{STAGE_LABEL.get(node, node).lower()}”."
        if node == "plan":
            return "Worked out the request" + (f": {humanize(sub).replace(',', ', ')}." if sub else ".")
        if node == "verify":
            return ("Waiting for the borrower to confirm their identity." if "pending_verification" in (ref.get("resolutions") or [])
                    else "Checked the borrower's identity.")
        if node == "execute":
            res = RESOLUTION_PLAIN.get((ref.get("resolutions") or [None])[-1], None)
            return "Carried out the request" + (f" and it was {res}." if res else ".")
        if node == "output_guard":
            return ("Held back an unsafe reply and sent a safe message instead." if "output_blocked" in (ref.get("events") or [])
                    else "Checked the reply is safe to send.")
        return {"screen": "Checked the message for unsafe or off-topic content.",
                "normalize": "Cleaned up or translated the message so it can be understood.",
                "start_verification": "Asked the borrower to identify themselves.",
                "clarify": "Asked the borrower a clarifying question.",
                "handoff": "Passed the conversation to a person.",
                "confirm_change": "Processed the borrower's yes or no to the change.",
                "confirm_action": "Processed the borrower's yes or no to the request."}.get(node, humanize(e["label"]).capitalize() + ".")
    ctype = call.get("type")
    if ctype == "tool_denied":
        return "Request held until the borrower's identity is confirmed."
    if ctype == "case_created":
        return "Opened an approval case."
    if ctype == "case_superseded":
        return "Replaced the case with the borrower's updated request."
    if ctype == "ticket_created":
        return f"Raised a ticket for the {humanize(call.get('queue')) or 'support'} team." if e["from"] == "Agent graph" else "Ticket recorded."
    if e["to"] == "Policy gateway":
        return f"Access control approved a look-up of {_tool_plain(call)}."
    if e["to"] == "Tools & systems":
        return f"Looked up {_tool_plain(call)}."
    if kind == "reply":
        return "Prepared the reply to the borrower."
    if kind == "send":
        return "The reply will be sent to the borrower." if st == "projected" else "Sent the reply to the borrower."
    if kind == "case":
        return "Approval case opened."
    if kind == "superseded":
        return "An earlier version of the request was closed."
    if kind == "review":
        who = role.title() or "A reviewer"
        if st == "waiting":
            w = ref.get("waiting_seconds") or 0
            return f"Waiting for the {role or 'reviewer'} to decide ({w // 60} min {w % 60} s)." if w >= 60 else f"Waiting for the {role or 'reviewer'} to decide ({w} s)."
        if st == "projected":
            return f"Next: the {role or 'reviewer'} will review this."
        return f"{who} was asked to review."
    if kind == "decision":
        who = role.title() or "Reviewer"
        return f"{who} {ref.get('decision') or 'decided'}" + (f" ({ref.get('reviewer')})." if ref.get("reviewer") else ".")
    if kind == "notify":
        return "The borrower will be told the outcome once review finishes."
    return humanize(e["label"]).capitalize() + "."



def _action(severity: str, text: str, kind: str = "none", target: str | None = None) -> dict:
    return {"severity": severity, "text": text, "kind": kind, "target": target}


def annotate(e: dict, conversation_id: str | None, trace_id: str | None) -> dict:
    """Add ``category`` and ``action`` to one sequence event.

    action.severity: ok | upcoming | waiting | attention | blocked.
    action.kind (what the console button does): inspect | open_case | open_conversation | open_queue | none.
    """
    ref = e.get("ref") or {}
    node, kind, state = ref.get("node"), ref.get("kind"), e["state"]
    call = ref.get("call") or {}
    ctype = call.get("type")
    cat, act = "resolve", _action("ok", "No action needed.")

    if kind == "inbound" or e["from"] == "Ingress":
        cat = "channel"
    elif node:
        cat = NODE_CATEGORY.get(node, "resolve")
        if state == "failed":
            act = _action("attention", "This step failed and the borrower may not have had an answer. Ask them to resend, or hand it to "
                          "engineering with the technical details.", "inspect", trace_id)
        elif node == "handoff":
            act = _action("attention", "Agent handed this conversation to a person. Pick it up from the Handoff desk.",
                          "open_queue", "queues")
        elif node == "clarify":
            act = _action("ok", "Agent asked a clarifying question. A second unclear reply routes to a human.")
        elif node in ("verify", "start_verification") and "pending_verification" in (ref.get("resolutions") or []):
            act = _action("ok", "Waiting for the borrower to finish OTP verification. No data is shown until then.",
                          "open_conversation", conversation_id)
        elif node == "output_guard" and "output_blocked" in (ref.get("events") or []):
            act = _action("attention", "A reply was held back and a safe message was sent instead. Check the Guardrails tab to see why.",
                          "inspect", trace_id)
    elif ctype == "tool_denied":
        cat = "verify"
        act = _action("blocked", "Request held until the borrower's identity is confirmed. Normal before OTP; "
                      "otherwise check which account they verified.", "open_conversation", conversation_id)
    elif ctype in ("case_created", "case_superseded") or kind in ("case", "superseded"):
        cat = "approval"
        act = _action("ok", "Case opened for approval." if ctype != "case_superseded" and kind != "superseded"
                      else "The borrower changed the request: the old version is closed and reviewers now see the new one.",
                      "open_case", ref.get("case_id") or call.get("case_id"))
    elif ctype == "ticket_created":
        cat = "escalate"
        if e["from"] == "Policy gateway":          # second arrow of the same call: do not raise it twice
            act = _action("ok", "Ticket recorded in the ticketing system.")
        else:
            act = _action("attention", f"Ticket routed to the {humanize(call.get('queue')) or 'support'} team. Open the Handoff desk to work it.",
                          "open_queue", "queues")
    elif e["to"] == "Policy gateway":
        cat = "guard"
    elif e["from"] == "Policy gateway" or e["to"] == "Tools & systems":
        cat = "resolve"
    elif kind in ("reply", "send"):
        cat = "deliver"
    elif e["to"] in ("Approver", "Compliance") or e["from"] in ("Approver", "Compliance") or kind in ("review", "decision", "notify"):
        cat = "approval"
        role = (ref.get("role") or "reviewer").replace("_", " ")
        cid = ref.get("case_id")
        if kind == "decision" and ref.get("decision") == "rejected":
            act = _action("attention", f"Rejected by {ref.get('reviewer') or role}. Make sure the borrower is told and "
                          "check for a follow-up request.", "open_case", cid)
        elif kind == "decision":
            act = _action("ok", f"{role.title()} approved. Next tier is asked automatically.", "open_case", cid)
        elif kind == "review" and state == "waiting":
            waited = ref.get("waiting_seconds") or 0
            if waited > APPROVAL_SLA_SECONDS:
                act = _action("attention", f"Overdue: waiting {waited // 60} min on the {role}. Open the case and decide, "
                              "or chase the reviewer.", "open_case", cid)
            else:
                act = _action("waiting", f"Waiting on the {role} ({waited} s). Open the case and decide.", "open_case", cid)
        elif kind == "review":
            act = _action("upcoming", f"Next: the {role} will be asked once the previous tier approves.")
        elif kind == "notify":
            act = _action("upcoming", "After the last tier decides, the borrower is messaged the outcome automatically.")
    if state == "projected" and act["severity"] == "ok":
        act = _action("upcoming", act["text"] if act["text"] != "No action needed." else "Not reached yet.")
    stage = stage_for(e, ref)
    e["category"], e["category_label"], e["action"] = cat, CATEGORIES[cat], act
    e["node"], e["stage_label"] = stage, STAGE_LABEL.get(stage) if stage else None
    e["plain"] = plain_text(e, ref.get("node"), ref)
    e["tech"] = ref.get("tech") or ({"call": call} if call else None)     # shown only behind "Technical details"
    e.pop("ref", None)                    # internal hints, not part of the API
    return e


def build_monitor(spans: list[TraceSpan], case_rows: list[tuple[str, int]]) -> dict:
    """Aggregate health for the architecture map from recent spans (one pass, no extra writes)."""
    nodes = {n: {"runs": 0, "failed": 0, "total_ms": 0, "max_ms": 0} for n in GRAPH_NODES}
    tools: dict[str, int] = {}
    denied = 0
    turns = {"total": 0, "failed": 0}
    resolutions: dict[str, int] = {}
    last_at = None
    for sp in spans:
        if sp.seq == 0:
            turns["total"] += 1
            turns["failed"] += sp.status == "failed"
            res = (sp.output or {}).get("resolution")
            if res:
                resolutions[res] = resolutions.get(res, 0) + 1
            last_at = max(last_at, sp.started_at) if last_at else sp.started_at
            continue
        n = nodes.setdefault(sp.name, {"runs": 0, "failed": 0, "total_ms": 0, "max_ms": 0})
        n["runs"] += 1
        n["failed"] += sp.status == "failed"
        n["total_ms"] += sp.duration_ms
        n["max_ms"] = max(n["max_ms"], sp.duration_ms)
        for c in sp.tool_calls or []:
            if c.get("type") == "tool_denied":
                denied += 1
            tools[c.get("tool") or c.get("type")] = tools.get(c.get("tool") or c.get("type"), 0) + 1
    for n in nodes.values():
        n["avg_ms"] = round(n["total_ms"] / n["runs"]) if n["runs"] else 0
    by_status: dict[str, int] = {}
    for status, count in case_rows:
        by_status[status] = count
    return {"turns": turns, "nodes": nodes, "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
            "denied": denied, "resolutions": resolutions, "cases": by_status,
            "last_turn_at": last_at.isoformat() if last_at else None}


LIVE_SECONDS = 15


def _mask_id(conversation_id: str) -> str:
    """Display label for a conversation: the mobile number keeps only its last 4 digits."""
    return re.sub(r"\d{6,}", lambda m: "••••" + m.group(0)[-4:], conversation_id)


def build_sessions(roots: list[TraceSpan], cases: dict[str, list[tuple[str, str]]], now: datetime,
                   active_seconds: int = 1800, include_idle: bool = False) -> list[dict]:
    """One row per conversation, newest first: what the console lists so you can pick a session to watch.

    status: live (turn in the last 15 s) | failed (last turn errored) | waiting_approval (a case is
    pending a reviewer) | active (turn within ``active_seconds``) | idle.
    """
    by_conv: dict[str, list[TraceSpan]] = {}
    for sp in roots:                                   # roots arrive newest first
        by_conv.setdefault(sp.conversation_id, []).append(sp)
    out = []
    for cid, turns in by_conv.items():
        last = turns[0]
        age = max(0, int((now - last.started_at).total_seconds()))
        open_cases = [{"case_id": c, "status": st} for c, st in cases.get(cid, []) if st.startswith("PENDING")]
        if last.status == "failed":
            status = "failed"
        elif age <= LIVE_SECONDS:
            status = "live"
        elif open_cases:
            status = "waiting_approval"
        elif age <= active_seconds:
            status = "active"
        else:
            status = "idle"
        if status == "idle" and not include_idle:
            continue
        o = last.output or {}
        out.append({"conversation_id": cid, "label": _mask_id(cid), "status": status, "turns": len(turns),
                    "failed": sum(t.status == "failed" for t in turns), "verified": bool(o.get("verified")),
                    "last_trace_id": last.trace_id, "last_text": (last.input or {}).get("text"),
                    "last_resolution": o.get("resolution"), "intents": o.get("intents", []),
                    "last_at": last.started_at.isoformat(), "age_seconds": age, "open_cases": open_cases})
    return out


def register_traces(r, runtime) -> None:
    @r.get("/api/traces")
    def list_traces(limit: int = 50, conversation_id: str | None = None, status: str | None = None):
        """Most recent turns first. One row per turn (the root span)."""
        q = select(TraceSpan).where(TraceSpan.seq == 0).order_by(TraceSpan.id.desc()).limit(max(1, min(limit, 200)))
        if conversation_id:
            q = q.where(TraceSpan.conversation_id == conversation_id)
        if status:
            q = q.where(TraceSpan.status == status)
        with runtime.session_factory() as s:
            rows = s.execute(q).scalars().all()
        return [{"trace_id": t.trace_id, "conversation_id": t.conversation_id, "status": t.status,
                 "started_at": t.started_at.isoformat(), "duration_ms": t.duration_ms,
                 "text": (t.input or {}).get("text"), "message_type": (t.input or {}).get("type"),
                 "resolution": (t.output or {}).get("resolution"), "intents": (t.output or {}).get("intents", []),
                 "path": (t.output or {}).get("path", []), "error": t.error} for t in rows]

    def load_case_timeline(case_id: str) -> dict:
        with runtime.session_factory() as s:
            case = s.get(Case, case_id)
            if not case:
                raise HTTPException(404, detail="case not found")
            versions = s.execute(select(CaseVersion).where(CaseVersion.case_id == case_id)
                                 .order_by(CaseVersion.version)).scalars().all()
            decisions = s.execute(select(Decision).where(Decision.case_id == case_id)
                                  .order_by(Decision.id)).scalars().all()
            convo_spans = s.execute(select(TraceSpan).where(TraceSpan.conversation_id == case.conversation_id)
                                    .order_by(TraceSpan.id)).scalars().all()
        try:
            req_ch = bool(runtime.cases.eligibility_for(case_id).get("requires_credit_head"))
        except Exception:  # noqa: BLE001 - eligibility is advisory for display
            req_ch = False
        tl = build_case_timeline(case, versions, decisions, req_ch, datetime.utcnow())
        tl["traces"] = sorted({sp.trace_id for sp in convo_spans if case_id in _case_ids([sp])})
        return tl

    @r.get("/api/traces/sessions")
    def sessions(active_minutes: int = 30, include_idle: bool = False, limit: int = 40):
        """Sessions to pick from: every conversation seen by the tracer, with live / waiting / failed status."""
        with runtime.session_factory() as s:
            roots = s.execute(select(TraceSpan).where(TraceSpan.seq == 0)
                              .order_by(TraceSpan.id.desc()).limit(1000)).scalars().all()
            cids = {x.conversation_id for x in roots}
            rows = s.execute(select(Case.conversation_id, Case.case_id, CaseVersion.status)
                             .join(CaseVersion, CaseVersion.case_id == Case.case_id)
                             .where(CaseVersion.version == Case.current_version,
                                    Case.conversation_id.in_(cids))).all() if cids else []
        cases: dict[str, list[tuple[str, str]]] = {}
        for conv, case_id, status in rows:
            cases.setdefault(conv, []).append((case_id, status))
        found = build_sessions(roots, cases, india_now(), max(1, active_minutes) * 60, include_idle)
        return found[:max(1, min(limit, 200))]

    @r.get("/api/traces/monitor")
    def monitor(limit: int = 300):
        """Health of every stage over the most recent turns: runs, failures, latency, tools, approvals."""
        with runtime.session_factory() as s:
            ids = s.execute(select(TraceSpan.trace_id).where(TraceSpan.seq == 0)
                            .order_by(TraceSpan.id.desc()).limit(max(1, min(limit, 1000)))).scalars().all()
            spans = s.execute(select(TraceSpan).where(TraceSpan.trace_id.in_(ids))).scalars().all() if ids else []
            rows = s.execute(select(CaseVersion.status, func.count()).join(Case, Case.case_id == CaseVersion.case_id)
                             .where(CaseVersion.version == Case.current_version)
                             .group_by(CaseVersion.status)).all()
        return build_monitor(spans, [(a, b) for a, b in rows])

    @r.get("/api/traces/case/{case_id}")
    def case_timeline(case_id: str):
        """Approval / compliance tracker: every version, each tier's state, who decided and how long it waited."""
        return load_case_timeline(case_id)

    @r.get("/api/traces/lifecycle")
    def lifecycle(conversation_id: str, limit: int = 30):
        """Whole journey of one session, oldest turn first: every turn with its categorised, plain-language steps,
        plus the approval lifecycle of the cases it started. The console derives each node's lifecycle from this."""
        with runtime.session_factory() as s:
            roots = s.execute(select(TraceSpan).where(TraceSpan.seq == 0, TraceSpan.conversation_id == conversation_id)
                              .order_by(TraceSpan.id.desc()).limit(max(1, min(limit, 100)))).scalars().all()
        if not roots:
            raise HTTPException(404, detail="no traces for this conversation")
        timelines: dict[str, dict] = {}
        turns = []
        for root in reversed(roots):
            t = get_trace(root.trace_id)
            for cid in t["cases"]:
                if cid not in timelines:
                    timelines[cid] = load_case_timeline(cid)
            events = build_sequence(t["turn"], t["spans"], [timelines[c] for c in t["cases"]])
            turns.append({"trace_id": root.trace_id, "started_at": root.started_at.isoformat(), "status": root.status,
                          "duration_ms": root.duration_ms, "text": (root.input or {}).get("text"),
                          "resolution": (root.output or {}).get("resolution"),
                          "resolution_plain": RESOLUTION_PLAIN.get((root.output or {}).get("resolution"), None),
                          "taken": [x["name"] for x in t["spans"]], "events": events,
                          "needs_action": [e for e in events if e["action"]["severity"] in ("attention", "blocked", "waiting")]})
        return {"conversation_id": conversation_id, "label": _mask_id(conversation_id), "categories": CATEGORIES,
                "stages": STAGE_LABEL, "about": STAGE_ABOUT, "turns": turns, "cases": list(timelines.values())}

    @r.get("/api/traces/{trace_id}/sequence")
    def sequence(trace_id: str):
        """Sequence of events for one turn: lanes, ordered arrows, and the projected rest of the approval chain."""
        t = get_trace(trace_id)
        timelines = [load_case_timeline(c) for c in t["cases"]]
        events = build_sequence(t["turn"], t["spans"], timelines)
        by_cat: dict[str, int] = {}
        for e in events:
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
        return {"trace_id": trace_id, "conversation_id": t["conversation_id"], "lanes": LANES,
                "graph_nodes": GRAPH_NODES, "taken": [x["name"] for x in t["spans"]],
                "categories": CATEGORIES, "by_category": by_cat, "events": events,
                "needs_action": [e for e in events if e["action"]["severity"] in ("attention", "blocked", "waiting")]}

    @r.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str):
        """Every node of one turn, in run order, with masked input, output, timing and gateway tool calls."""
        with runtime.session_factory() as s:
            spans = s.execute(select(TraceSpan).where(TraceSpan.trace_id == trace_id)
                              .order_by(TraceSpan.seq)).scalars().all()
            if not spans:
                raise HTTPException(404, detail="trace not found")
            events = s.execute(select(AuditEvent).where(AuditEvent.trace_id == trace_id)
                               .order_by(AuditEvent.id)).scalars().all()
        root = next((x for x in spans if x.seq == 0), None)
        nodes = [x for x in spans if x.seq != 0]
        return {"trace_id": trace_id, "conversation_id": spans[0].conversation_id,
                "turn": _span(root) if root else None, "spans": [_span(x) for x in nodes],
                "cases": _case_ids(nodes),
                "audit_events": [{"type": e.type, "data": e.data, "at": e.at.isoformat()} for e in events]}
