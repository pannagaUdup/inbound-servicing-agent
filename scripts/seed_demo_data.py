"""Seed synthetic, diverse demo data so the admin dashboard looks lived-in — and *coherent*.

What it fills:
  * 29 days of audit events (inbound / outbound / verified / handoff / ticket_created /
    tool_call-with-intent, plus a few injection & output blocks) with a weekday rhythm + trend;
  * tickets across the routing matrix (mix of open / resolved), each carrying a real agent
    brief in ``summary.context_bag`` (borrower message, triage, policy clauses, reviewer note,
    recent conversation) — the same shape the live agent writes, so the Handoff-desk brief
    renders properly instead of raw JSON;
  * payment claims with every verdict (PAID / EDITED / REUSED / NOT_FOUND);
  * waiver / settlement cases across the lifecycle with decision trails;
  * ~18 rich borrower conversations in several languages — each ticketed conversation gets a
    REAL linked ticket (matching id + the conversation transcript as the brief).

Every quoted number (EMI, overdue, charges, foreclosure) is read from the referenced account,
so the data is internally consistent.

Safe + idempotent: every row it writes is tagged — conversation_id starting ``demo-``,
case_id starting ``DEMO-``, or ``data.demo == True`` — and a re-run deletes the previous demo
rows first. It never touches real rows (live WhatsApp / simulator traffic).

Run:  .venv/bin/python scripts/seed_demo_data.py
      .venv/bin/python scripts/seed_demo_data.py --clear   # just remove demo rows
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from servicing_agent.config import Settings, routing_matrix  # noqa: E402
from servicing_agent.contracts.cases import CaseStatus  # noqa: E402
from servicing_agent.data.store import load_store  # noqa: E402
from servicing_agent.platform.db import (  # noqa: E402
    AuditEvent, Case, CaseVersion, ConversationState, Decision, PaymentClaim, Ticket,
    make_session_factory,
)

DAYS = 29
R = random.Random(20261120)
UTC_NOW = datetime.utcnow()
DEMO_CID_PREFIX = "demo-"
DEMO_CASE_PREFIX = "DEMO-"
SETTINGS: Settings = None  # set in main()


def _etag() -> str:
    return "dm" + "".join(R.choice("0123456789abcdef") for _ in range(10))


def ts(days_ago: float, hour: int | None = None) -> datetime:
    base = UTC_NOW - timedelta(days=days_ago)
    if hour is not None:
        base = base.replace(hour=hour, minute=R.randint(0, 59), second=R.randint(0, 59))
    return base


def rupees(v) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return "₹0.00"


# ---------------------------------------------------------------- intents & routing
INTENT_WEIGHTS = [
    ("emi_due_query", 16), ("payment_claim", 13), ("foreclosure_quote", 8),
    ("statement_request", 8), ("receipt_request", 6), ("charges_query", 7),
    ("loan_summary", 7), ("payment_history", 5), ("pay_now", 6),
    ("waiver_request", 5), ("settlement_request", 4), ("dispute", 4),
    ("complaint", 4), ("charge_reversal", 3), ("payment_difficulty", 4),
    ("policy_question", 3), ("case_status", 3), ("faq", 3), ("greeting", 6),
    ("distress", 1), ("legal_threat", 1), ("harassment_complaint", 1),
]
_INTENTS = [i for i, w in INTENT_WEIGHTS for _ in range(w)]

INTENT_TO_REQUEST = {
    "dispute": "dispute", "complaint": "complaint", "charge_reversal": "charge_reversal",
    "waiver_request": "waiver_request", "settlement_request": "settlement_request",
    "payment_difficulty": "partial_payment_waiver", "distress": "distress",
    "legal_threat": "legal_threat", "harassment_complaint": "harassment_complaint",
}
PAYMENT_VERDICTS = ["PAID", "PAID", "PAID", "EDITED", "REUSED", "NOT_FOUND"]
LANGS = ["en", "en", "en", "hi", "hi", "kn", "ta", "te", "mr"]

# ---- the agent-brief building blocks (mirrors agent/graph.build_context_bag) ------
TRIAGE = {
    "distress": ("distressed", "P1", True), "harassment_complaint": ("angry", "P1", False),
    "legal_threat": ("hostile", "P1", False), "complaint": ("frustrated", "P2", False),
    "dispute": ("concerned", "P2", False), "charge_reversal": ("annoyed", "P3", False),
    "waiver_request": ("hopeful", "P2", True), "settlement_request": ("anxious", "P2", True),
    "partial_payment_waiver": ("worried", "P2", True), "borrower_escalation": ("frustrated", "P2", False),
    "payment_mismatch": ("neutral", "P1", False), "payment_unmatched": ("neutral", "P3", False),
}
CLAUSES = {
    "waiver_request": ["WAIVER-BANDS §2.1", "HARDSHIP-POLICY §4.2"],
    "settlement_request": ["OTS-POLICY §3.1", "HARDSHIP-POLICY §4.2"],
    "partial_payment_waiver": ["WAIVER-BANDS §2.3"],
    "charge_reversal": ["CHARGES-SOP §1.4"],
    "dispute": ["DISPUTE-SOP §2.0"],
    "complaint": ["GRIEVANCE-SOP §1.1"],
    "harassment_complaint": ["GRIEVANCE-SOP §1.1", "RECOVERY-CONDUCT §6"],
    "legal_threat": ["LEGAL-ESCALATION §7"],
    "distress": ["HARDSHIP-POLICY §4.0", "CARE-PROTOCOL §1"],
    "payment_mismatch": ["PROOF-AUTH §3", "FRAUD-SOP §2"],
    "payment_unmatched": ["RECON-SOP §1"],
    "borrower_escalation": ["ESCALATION-SOP §5"],
}
NOTES = {
    "dispute": "Pull the loan agreement and KYC; confirm the borrower actually holds this account before any collection step.",
    "complaint": "Acknowledge within SLA, identify the failed touchpoint, and assign an owner to close the loop.",
    "charge_reversal": "Check the debit event and the mandate; if the charge was levied in error, raise a reversal.",
    "waiver_request": "Verify hardship evidence against the waiver bands; route to credit head if it touches principal.",
    "settlement_request": "Compute the settlement band from DPD and outstanding; OTS needs credit-head + compliance sign-off.",
    "partial_payment_waiver": "Confirm part-payment capacity and apply the late-fee waiver only within band.",
    "distress": "Priority-care callback; pause recovery pressure and offer hardship options compassionately.",
    "legal_threat": "Do not argue the merits. Log verbatim and hand to legal for a formal response.",
    "harassment_complaint": "Pause all recovery contact, pull the agent's call logs, and escalate to the grievance officer.",
    "payment_mismatch": "Proof failed authenticity — verify against the payment feed before any confirmation to the borrower.",
    "payment_unmatched": "UTR not yet in the feed; reconcile against the bank statement within 24h.",
}
BORROWER_MSG = {
    "dispute": "This EMI isn't mine — I never took this loan. Please check.",
    "complaint": "I've called three times this week and nobody helps. Very poor service.",
    "charge_reversal": "You charged me a bounce fee but there was money in my account. Please reverse it.",
    "waiver_request": "Please waive my penal charges — I lost my job last month and can't manage.",
    "settlement_request": "I can't pay the full amount. Can we do a one-time settlement?",
    "partial_payment_waiver": "I can pay half now if you waive the late fee — is that possible?",
    "distress": "I'm under a lot of stress and don't know how I'll manage this payment.",
    "legal_threat": "If you keep calling me I'll take you to consumer court.",
    "harassment_complaint": "Your recovery agent is calling my family and threatening them.",
}
ACK = {
    "dispute": "I've logged your dispute and shared it with our team; we'll respond within SLA.",
    "complaint": "I'm sorry about that — I've logged your complaint and escalated it.",
    "charge_reversal": "I've raised this with the charges team to review and reverse if it was in error.",
    "waiver_request": "I've sent your waiver request for approval. Our team will get back to you.",
    "settlement_request": "I've sent your settlement request for approval. We'll let you know soon.",
    "partial_payment_waiver": "I've noted your part-payment request and passed it to the team.",
    "distress": "You're not alone — I've alerted our care team and they'll reach out shortly.",
    "legal_threat": "I've noted this and passed it to our legal team; they'll contact you.",
    "harassment_complaint": "I'm sorry this happened. I've escalated it to the grievance officer and paused recovery contact.",
}


def brief_summary(request_type: str, intent: str, lang: str, acc_id: str,
                  borrower_text: str | None = None, recent: list[dict] | None = None) -> dict:
    """A ticket ``summary`` with the same context_bag the live agent attaches."""
    sentiment, priority, hardship = TRIAGE.get(request_type, ("neutral", "P3", False))
    tri = {"sentiment": sentiment, "priority": priority, "hardship": hardship}
    msg = borrower_text or BORROWER_MSG.get(request_type, "Borrower needs assistance.")
    if recent is None:
        recent = [{"role": "borrower", "text": msg},
                  {"role": "agent", "text": ACK.get(request_type, "I've passed this to our team.")}]
    bag = {
        "policy_version": "demo-v1",
        "policy_clauses": CLAUSES.get(request_type, []),
        "triage": tri,
        "reviewer_note": NOTES.get(request_type, "Review and action within SLA."),
        "verified_account": acc_id,
        "recent_conversation": recent[-6:],
    }
    return {"demo": True, "intent": intent, "language": lang, "source": "agent",
            "borrower_message": msg, "triage": tri, "context_bag": bag}


# ---------------------------------------------------------------- clearing
def clear_demo(SF) -> None:
    with SF.begin() as s:
        s.execute(delete(AuditEvent).where(AuditEvent.conversation_id.like(DEMO_CID_PREFIX + "%")))
        s.execute(delete(Ticket).where(Ticket.conversation_id.like(DEMO_CID_PREFIX + "%")))
        s.execute(delete(PaymentClaim).where(PaymentClaim.conversation_id.like(DEMO_CID_PREFIX + "%")))
        s.execute(delete(ConversationState).where(ConversationState.conversation_id.like(DEMO_CID_PREFIX + "%")))
        s.execute(delete(Decision).where(Decision.case_id.like(DEMO_CASE_PREFIX + "%")))
        s.execute(delete(CaseVersion).where(CaseVersion.case_id.like(DEMO_CASE_PREFIX + "%")))
        s.execute(delete(Case).where(Case.case_id.like(DEMO_CASE_PREFIX + "%")))


def daily_volume(d: int) -> int:
    trend = 6 + (DAYS - d) * 0.28
    weekday = (UTC_NOW - timedelta(days=d)).weekday()
    season = 0.55 if weekday >= 5 else 1.0
    return max(2, int(trend * season + R.randint(-2, 3)))


# ---------------------------------------------------------------- 29-day traffic
def seed_traffic(SF, accounts):
    events, tickets, claims = [], [], []
    ticket_seq = 900000
    rmatrix = routing_matrix()

    for d in range(DAYS - 1, -1, -1):
        for k in range(daily_volume(d)):
            acc_id, acc = R.choice(accounts)
            cid = f"{DEMO_CID_PREFIX}{(UTC_NOW - timedelta(days=d)).date()}-{k:02d}"
            at0 = ts(d, R.randint(8, 21))
            intent = R.choice(_INTENTS)
            verified = R.random() < 0.72
            lang = R.choice(LANGS)

            n_in = R.randint(1, 3)
            for j in range(n_in):
                events.append(AuditEvent(conversation_id=cid, type="inbound",
                                         at=at0 + timedelta(minutes=j), data={"demo": True}))
            if verified:
                events.append(AuditEvent(conversation_id=cid, type="verified",
                                         at=at0 + timedelta(minutes=1), data={"demo": True, "method": "otp"}))
            events.append(AuditEvent(conversation_id=cid, type="tool_call",
                                     at=at0 + timedelta(minutes=1, seconds=20),
                                     data={"demo": True, "intent": intent, "language": lang}))
            for j in range(n_in + R.randint(0, 1)):
                events.append(AuditEvent(conversation_id=cid, type="outbound",
                                         at=at0 + timedelta(minutes=2 + j), data={"demo": True}))

            if intent == "payment_claim":
                verdict = R.choice(PAYMENT_VERDICTS)
                queue = "fraud_review" if verdict in ("EDITED", "REUSED") else (
                    "payment_reconciliation" if verdict == "NOT_FOUND" else None)
                claims.append(PaymentClaim(
                    conversation_id=cid, account_id=acc_id,
                    source=R.choice(["screenshot", "voice", "text"]),
                    utr="".join(R.choice("0123456789") for _ in range(12)),
                    amount=round(float(acc.get("emi_amount") or 2500), 2),
                    result=verdict, queue=queue, created_at=at0 + timedelta(minutes=2),
                    reasons=(["amount_mismatch"] if verdict == "EDITED" else
                             ["utr_reused"] if verdict == "REUSED" else
                             ["not_in_feed"] if verdict == "NOT_FOUND" else None)))
                events.append(AuditEvent(conversation_id=cid, type="payment_verdict",
                                         at=at0 + timedelta(minutes=2), data={"demo": True, "verdict": verdict}))

            req = INTENT_TO_REQUEST.get(intent)
            if req and R.random() < 0.85:
                route = rmatrix.get(req) or rmatrix["unknown"]
                ticket_seq += 1
                created = at0 + timedelta(minutes=3)
                resolved, status = None, "open"
                if R.random() < 0.62:
                    status = "resolved"
                    resolved = min(UTC_NOW - timedelta(hours=R.uniform(0.2, 6)),
                                   created + timedelta(hours=R.uniform(1.5, route["sla_hours"] * 1.3)))
                tickets.append(Ticket(
                    ticket_id=f"TKT{ticket_seq:06d}", conversation_id=cid, account_id=acc_id,
                    request_type=req, routing_queue=route["queue"], priority=route["priority"],
                    sla_hours=route["sla_hours"], summary=brief_summary(req, intent, lang, acc_id),
                    source="agent", status=status, assignee=SETTINGS.ops_assignee,
                    created_at=created, resolved_at=resolved))
                events.append(AuditEvent(conversation_id=cid, type="ticket_created",
                                         at=created, data={"demo": True, "intent": intent,
                                                           "request_type": req, "queue": route["queue"]}))
                events.append(AuditEvent(conversation_id=cid, type="handoff",
                                         at=created, data={"demo": True, "intent": intent}))

            if R.random() < 0.02:
                events.append(AuditEvent(conversation_id=cid, type="injection_detected",
                                         at=at0 + timedelta(seconds=30),
                                         data={"demo": True, "reason": "ignore previous instructions"}))
            if R.random() < 0.015:
                events.append(AuditEvent(conversation_id=cid, type="output_blocked",
                                         at=at0 + timedelta(minutes=2),
                                         data={"demo": True, "reason": "internal field in draft reply"}))

    with SF.begin() as s:
        s.add_all(events)
        s.add_all(tickets)
        s.add_all(claims)
    return len(events), len(tickets), len(claims)


# ---------------------------------------------------------------- cases
def seed_cases(SF, accounts):
    plans = [
        ("waiver", ["penal", "bounce"], CaseStatus.PENDING_APPROVER, 3400),
        ("waiver", ["penal"], CaseStatus.PENDING_CREDIT_HEAD, 8200),
        ("settlement", ["principal", "penal"], CaseStatus.PENDING_COMPLIANCE, 41000),
        ("waiver", ["bounce", "interest"], CaseStatus.APPROVED, 5600),
        ("settlement", ["principal"], CaseStatus.COMMUNICATED, 62000),
        ("waiver", ["penal"], CaseStatus.REJECTED, 15500),
        ("settlement", ["principal", "interest"], CaseStatus.PENDING_CREDIT_HEAD, 88000),
        ("waiver", ["bounce"], CaseStatus.APPROVED, 1800),
    ]
    reasons = ["job_loss", "medical", "business_loss", "job_loss", "medical"]
    cases, versions, decisions = [], [], []
    for i, (ctype, comps, status, amount) in enumerate(plans, 1):
        acc_id, _ = R.choice(accounts)
        case_id = f"{DEMO_CASE_PREFIX}{i:04d}"
        created = ts(R.uniform(0.5, 18))
        cases.append(Case(case_id=case_id, account_id=acc_id, conversation_id=f"{DEMO_CID_PREFIX}case-{i:02d}",
                          current_version=1, assignee=SETTINGS.ops_assignee, created_at=created))
        versions.append(CaseVersion(
            case_id=case_id, version=1, status=status.value, type=ctype, amount=float(amount),
            request={"type": ctype, "amount": amount, "components": comps,
                     "reason_category": R.choice(reasons), "demo": True},
            etag=_etag(), created_at=created))
        trail = {
            CaseStatus.PENDING_CREDIT_HEAD: [("approver", "approve")],
            CaseStatus.PENDING_COMPLIANCE: [("approver", "approve"), ("credit_head", "approve")],
            CaseStatus.APPROVED: [("approver", "approve"), ("credit_head", "approve"), ("compliance", "approve")],
            CaseStatus.COMMUNICATED: [("approver", "approve"), ("credit_head", "approve"), ("compliance", "approve")],
            CaseStatus.REJECTED: [("approver", "reject")],
        }.get(status, [])
        for role, dec in trail:
            decisions.append(Decision(
                case_id=case_id, version=1, role=role, decision=dec,
                rationale={"approve": "Within band; hardship documented.",
                           "reject": "Outside eligibility band; no hardship evidence."}[dec],
                reviewer_id=SETTINGS.ops_assignee, created_at=created + timedelta(hours=R.uniform(1, 10))))
    with SF.begin() as s:
        s.add_all(cases)
        s.add_all(versions)
        s.add_all(decisions)
    return len(cases)


# ---------------------------------------------------------------- conversations (+ linked tickets)
def seed_conversations(SF, accounts):
    rows: list[ConversationState] = []
    linked: list[Ticket] = []
    rmatrix = routing_matrix()

    def conv(n, sender, acc_id, acc, lang, intent, bubbles, events, verified=True,
             ticket=None, brief_text=None):
        cid = f"{DEMO_CID_PREFIX}conv-{n}:{sender}"
        base = ts(R.uniform(0.02, 6))
        tr = []
        for j, (role, text, extra) in enumerate(bubbles):
            e = {"role": role, "text": text, "at": (base + timedelta(minutes=j)).isoformat()}
            e.update(extra or {})
            tr.append(e)
        evs = [{"type": t, "at": (base + timedelta(seconds=10 + 5 * j)).isoformat(), "data": dd}
               for j, (t, dd) in enumerate(events)]
        chips = []
        if ticket:
            tid, request_type = ticket
            route = rmatrix.get(request_type) or rmatrix["unknown"]
            recent = [{"role": m["role"], "text": m["text"], "at": m["at"]} for m in tr]
            first_b = next((m["text"] for m in tr if m["role"] == "borrower"), None)
            created = base + timedelta(minutes=len(bubbles))
            linked.append(Ticket(
                ticket_id=tid, conversation_id=cid, account_id=acc_id, request_type=request_type,
                routing_queue=route["queue"], priority=route["priority"], sla_hours=route["sla_hours"],
                summary=brief_summary(request_type, intent, lang, acc_id,
                                      borrower_text=brief_text or first_b, recent=recent),
                source="agent", status="open", assignee=SETTINGS.ops_assignee, created_at=created))
            chips = [{"ticket_id": tid, "queue": route["queue"]}]
        rows.append(ConversationState(conversation_id=cid, updated_at=base + timedelta(minutes=len(bubbles)),
            data={"conversation_id": cid,
                  "session": {"verification": {}, "scope": acc_id if verified else None,
                              "pending_tasks": [], "pending_change": None, "clarify_count": 0, "lang": lang},
                  "transcript": tr, "events": evs, "tickets": chips}))

    def pick():
        return R.choice(accounts)

    # 1) Hindi EMI-due, verified — zero touch
    a, acc = pick()
    conv("01", "9811100001", a, acc, "hi", "emi_due_query", [
        ("borrower", "मेरी EMI कितनी है?", None),
        ("agent", f"आपकी अगली EMI {rupees(acc.get('emi_amount'))} है, due date {acc.get('next_due_date')}.", None),
        ("borrower", "ठीक है धन्यवाद", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "emi_due_query", "language": "hi"}), ("outbound", {})])

    # 2) Kannada payment claim — EDITED (fraud) -> linked ticket
    a, acc = pick()
    conv("02", "9880200002", a, acc, "kn", "payment_claim", [
        ("borrower", "ನಾನು ಈಗಾಗಲೇ ಪಾವತಿಸಿದ್ದೇನೆ, screenshot ಕಳುಹಿಸುತ್ತೇನೆ", None),
        ("borrower", "🧾 payment screenshot", {"type": "image", "media_kind": "upi_receipt"}),
        ("agent", "ಈ receipt ನ ಮೊತ್ತ ತಿದ್ದಲಾಗಿದೆ ಎಂದು ಕಂಡುಬಂದಿದೆ. ಪರಿಶೀಲನೆಗಾಗಿ ತಂಡಕ್ಕೆ ಕಳುಹಿಸಿದ್ದೇನೆ.", None),
    ], [("inbound", {}), ("tool_call", {"intent": "payment_claim", "language": "kn"}),
        ("payment_verdict", {"verdict": "EDITED"}), ("handoff", {"intent": "payment_claim"})],
        ticket=("TKT900101", "payment_mismatch"),
        brief_text="Shared a UPI screenshot; the amount on the proof was edited — flagged as a fake proof.")

    # 3) English foreclosure — zero touch (real figure from the account)
    a, acc = pick()
    fq = round(float(acc.get("principal_outstanding") or 120000)
               * (1 + float(acc.get("foreclosure_charge_pct") or 2) / 100)
               + float(acc.get("overdue_amount") or 0), 0)
    conv("03", "9900300003", a, acc, "en", "foreclosure_quote", [
        ("borrower", "I want to close my loan fully. What's the foreclosure amount?", None),
        ("agent", f"Your foreclosure amount today is {rupees(fq)}, valid for 3 days. I've attached the quote PDF.", None),
        ("borrower", "Great, thanks!", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "foreclosure_quote", "language": "en"}), ("outbound", {})])

    # 4) Tamil harassment complaint -> grievance ticket
    a, acc = pick()
    conv("04", "9444400004", a, acc, "ta", "harassment_complaint", [
        ("borrower", "உங்கள் recovery agent என் குடும்பத்தை தொந்தரவு செய்கிறார்", None),
        ("agent", "மன்னிக்கவும். இதை grievance officer க்கு உடனடியாக அனுப்பியுள்ளேன், recovery தொடர்பு இடைநிறுத்தப்படும்.", None),
    ], [("inbound", {}), ("tool_call", {"intent": "harassment_complaint", "language": "ta"}),
        ("handoff", {"intent": "harassment_complaint", "queue": "grievance_officer"})],
        ticket=("TKT900102", "harassment_complaint"),
        brief_text="Recovery agent is calling the borrower's family and threatening them.")

    # 5) English voice note, charges query — zero touch
    a, acc = pick()
    conv("05", "9000500005", a, acc, "en", "charges_query", [
        ("borrower", "🎤 voice note (12s)", {"type": "voice", "media_kind": "voice_note",
                                              "media_text": "why have you charged me extra bounce fees this month"}),
        ("agent", f"This month's bounce charge is {rupees(acc.get('bounce_charges'))} from a failed auto-debit on the due date. Penal charge is {rupees(acc.get('penal_charges'))}.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "charges_query", "language": "en"}), ("outbound", {})])

    # 6) Marathi waiver -> approvals ticket
    a, acc = pick()
    conv("06", "9700600006", a, acc, "mr", "waiver_request", [
        ("borrower", "कृपया माझे penal charges माफ करा, मला नोकरी गेली आहे", None),
        ("agent", "तुमची विनंती मंजुरीसाठी पाठवली आहे. आमचा संघ लवकरच कळवेल.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "waiver_request", "language": "mr"}),
        ("handoff", {"intent": "waiver_request", "queue": "waiver_approvals"})],
        ticket=("TKT900103", "waiver_request"),
        brief_text="Requests a penal-charge waiver citing job loss last month.")

    # 7) Telugu settlement -> approvals ticket
    a, acc = pick()
    conv("07", "9500700007", a, acc, "te", "settlement_request", [
        ("borrower", "నేను one-time settlement చేయాలనుకుంటున్నాను", None),
        ("agent", "మీ settlement అభ్యర్థనను ఆమోదం కోసం పంపాను. త్వరలో తెలియజేస్తాము.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "settlement_request", "language": "te"}),
        ("handoff", {"intent": "settlement_request", "queue": "settlement_approvals"})],
        ticket=("TKT900104", "settlement_request"),
        brief_text="Wants a one-time settlement; cannot pay the full outstanding.")

    # 8) Guest (unverified) — account guessing blocked
    a, acc = pick()
    conv("08", "9300800008", a, acc, "en", "greeting", [
        ("borrower", "hi", None),
        ("agent", "Hello! To help with your loan I first need to verify you. What's your loan account number?", None),
        ("borrower", "CN0000000001", None),
        ("agent", "That didn't match. Let's try again — please share the account number from your statement.", None),
    ], [("inbound", {}), ("tool_call", {"intent": "greeting", "language": "en"}),
        ("verification_failed", {"reason": "no_match"})], verified=False)

    # 9) English payment claim — PAID (clean receipt) — zero touch
    a, acc = pick()
    conv("09", "9211900009", a, acc, "en", "payment_claim", [
        ("borrower", "I paid my EMI yesterday, UTR 512398765432", None),
        ("agent", f"Confirmed ✓ — I matched UTR 512398765432 for {rupees(acc.get('emi_amount'))}. Your receipt PDF is attached.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "payment_claim", "language": "en"}),
        ("payment_verdict", {"verdict": "PAID"}), ("outbound", {})])

    # 10) Hindi legal threat -> legal ticket
    a, acc = pick()
    conv("10", "9112100010", a, acc, "hi", "legal_threat", [
        ("borrower", "मैं आप पर consumer court में केस करूंगा", None),
        ("agent", "आपकी बात नोट कर ली है और इसे हमारी legal टीम को भेज दिया है। वे आपसे संपर्क करेंगे।", None),
    ], [("inbound", {}), ("tool_call", {"intent": "legal_threat", "language": "hi"}),
        ("handoff", {"intent": "legal_threat", "queue": "legal"})],
        ticket=("TKT900105", "legal_threat"),
        brief_text="Threatens to file a case in consumer court if calls continue.")

    # 11) English statement — zero touch
    a, acc = pick()
    conv("11", "9332100011", a, acc, "en", "statement_request", [
        ("borrower", "Can you send my account statement for the last 6 months?", None),
        ("agent", "Sure — I've attached your statement (Apr–Sep). It's password protected (DOB as DDMMYYYY).", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "statement_request", "language": "en"}), ("outbound", {})])

    # 12) Kannada dispute -> disputes ticket
    a, acc = pick()
    conv("12", "9844100012", a, acc, "kn", "dispute", [
        ("borrower", "ಇದು ನನ್ನ ಸಾಲ ಅಲ್ಲ, ನಾನು ಇದನ್ನು ತೆಗೆದುಕೊಂಡಿಲ್ಲ", None),
        ("agent", "ನಿಮ್ಮ dispute ಅನ್ನು ದಾಖಲಿಸಿದ್ದೇನೆ ಮತ್ತು ತಂಡಕ್ಕೆ ಕಳುಹಿಸಿದ್ದೇನೆ. SLA ಒಳಗೆ ಪ್ರತಿಕ್ರಿಯಿಸುತ್ತೇವೆ.", None),
    ], [("inbound", {}), ("tool_call", {"intent": "dispute", "language": "kn"}),
        ("handoff", {"intent": "dispute", "queue": "servicing_disputes"})],
        ticket=("TKT900106", "dispute"),
        brief_text="Says the loan is not theirs and they never took it.")

    # 13) English payment claim — REUSED (fraud) -> fraud ticket
    a, acc = pick()
    conv("13", "9911300013", a, acc, "en", "payment_claim", [
        ("borrower", "Already paid, here's the UTR 371413463057", None),
        ("agent", "That UTR was already used to confirm a payment on a different account, so I can't apply it here. I've flagged it for review.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "payment_claim", "language": "en"}),
        ("payment_verdict", {"verdict": "REUSED"}), ("handoff", {"intent": "payment_claim"})],
        ticket=("TKT900107", "payment_mismatch"),
        brief_text="Submitted a UTR already claimed on a different account (reused proof).")

    # 14) Tamil policy question — zero touch
    a, acc = pick()
    conv("14", "9445100014", a, acc, "ta", "policy_question", [
        ("borrower", "நான் தாமதமாக செலுத்தினால் என் CIBIL score பாதிக்குமா?", None),
        ("agent", "ஆம், 30+ நாட்கள் தாமதம் உங்கள் credit score-ஐ பாதிக்கலாம். due date-க்குள் செலுத்த பரிந்துரைக்கிறேன்.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "policy_question", "language": "ta"}), ("outbound", {})])

    # 15) English pay-now link — zero touch (real amount)
    a, acc = pick()
    amt = acc.get("overdue_amount") or acc.get("emi_amount")
    conv("15", "9333100015", a, acc, "en", "pay_now", [
        ("borrower", "I can pay now, send me the link", None),
        ("agent", f"Here's your secure payment link for {rupees(amt)}: https://pay.example.test/l/9f2ab7c1e004", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "pay_now", "language": "en"}), ("outbound", {})])

    # 16) Hindi distress -> priority care ticket
    a, acc = pick()
    conv("16", "9116100016", a, acc, "hi", "distress", [
        ("borrower", "मैं बहुत परेशान हूँ, कुछ समझ नहीं आ रहा", None),
        ("agent", "आप अकेले नहीं हैं। मैंने हमारी care टीम को सूचित किया है, वे जल्द ही आपसे बात करेंगे।", None),
    ], [("inbound", {}), ("tool_call", {"intent": "distress", "language": "hi"}),
        ("handoff", {"intent": "distress", "queue": "priority_care"})],
        ticket=("TKT900108", "distress"),
        brief_text="Borrower sounds highly distressed and unable to cope with the payment.")

    # 17) English payment claim — NOT_FOUND -> reconciliation ticket
    a, acc = pick()
    conv("17", "9912100017", a, acc, "en", "payment_claim", [
        ("borrower", "Paid ₹5,000 via UPI but it's not reflecting", None),
        ("agent", "I couldn't find that payment in our feed yet. I've opened a reconciliation ticket — it may take up to 24h to settle.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "payment_claim", "language": "en"}),
        ("payment_verdict", {"verdict": "NOT_FOUND"}), ("handoff", {"intent": "payment_claim"})],
        ticket=("TKT900109", "payment_unmatched"),
        brief_text="Claims a ₹5,000 UPI payment not yet visible in the payment feed.")

    # 18) English service complaint -> complaints ticket
    a, acc = pick()
    conv("18", "9334100018", a, acc, "en", "complaint", [
        ("borrower", "Nobody responds to my calls, this is the worst service", None),
        ("agent", "I'm sorry about that. I've logged your complaint and escalated it so someone reaches out.", None),
    ], [("inbound", {}), ("verified", {"method": "otp"}),
        ("tool_call", {"intent": "complaint", "language": "en"}),
        ("handoff", {"intent": "complaint", "queue": "complaints"})],
        ticket=("TKT900110", "complaint"),
        brief_text="Repeated unanswered calls; frustrated with service quality.")

    with SF.begin() as s:
        s.add_all(rows)
        s.add_all(linked)
    return len(rows), len(linked)


def main():
    global SETTINGS
    SETTINGS = Settings()
    SF = make_session_factory(SETTINGS.database_url)
    store = load_store(str(SETTINGS.data_dir))
    accounts = [(aid, a) for aid, a in store.accounts.items() if a.get("emi_amount")]
    accounts = R.sample(accounts, min(120, len(accounts)))

    print(f"DB: {SETTINGS.database_url}")
    clear_demo(SF)
    print("· cleared previous demo rows")
    if "--clear" in sys.argv:
        print("done (clear only).")
        return

    ne, nt, nc = seed_traffic(SF, accounts)
    ncases = seed_cases(SF, accounts)
    nconv, nlinked = seed_conversations(SF, accounts)
    print(f"· audit events:    {ne}")
    print(f"· tickets:         {nt} traffic + {nlinked} conversation-linked")
    print(f"· payment claims:  {nc}")
    print(f"· cases:           {ncases}")
    print(f"· conversations:   {nconv}")
    print("done. Refresh the admin dashboard (no restart needed for the data).")


if __name__ == "__main__":
    main()
