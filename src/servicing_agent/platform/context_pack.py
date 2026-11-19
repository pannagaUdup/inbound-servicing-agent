"""Context pack for approvers and compliance, per case version (owner: T2).

Every number comes from a source record. The summary is template-built for now;
if T1 adds an LLM-written summary it must pass the same numeric-grounding check.
"""

from __future__ import annotations

from servicing_agent.config import policy_bands
from servicing_agent.platform.cases import CaseService


def dpd_bucket(dpd: int) -> str:
    if dpd <= 0:
        return "0"
    if dpd <= 30:
        return "1-30"
    if dpd <= 60:
        return "31-60"
    if dpd <= 90:
        return "61-90"
    return "90+"


def authority_level(amount: float | None) -> str:
    for row in policy_bands()["authority_levels"]:
        if row["max_amount"] is None or (amount or 0) <= row["max_amount"]:
            return row["level"]
    return "L4_credit_committee"


REQUIRED_SECTIONS = ["request", "version_history", "loan_position", "payment_behaviour", "prior_concessions",
                     "policy_check", "policy_clauses", "risk_flags", "compliance_checklist", "conversation"]


def _case_clauses(kind: str, bag: dict | None) -> list[dict]:
    from servicing_agent.agent.policy import clauses_for_case, policy
    partial = bool(bag and any(c["id"] == "PPW-01" for c in bag.get("policy_clauses", [])))
    ids = [c.id for c in clauses_for_case(kind, partial=partial)]
    ids += [c["id"] for c in (bag or {}).get("policy_clauses", []) if c["id"] not in ids]
    p = policy()
    return sorted((p.get(i).ref() for i in ids), key=lambda c: p.get(c["id"]).order)


def build_context_pack(case_id: str, version: int, *, cases: CaseService, tools, store, conversations) -> dict:
    case = cases.get(case_id)
    ver = next(v for v in case["versions"] if v["version"] == version)
    account_id = case["account_id"]
    position = tools.lms.loan_position(account_id)
    history = tools.payments.history(account_id, months=12)
    convo = conversations.get(case["conversation_id"]) if conversations else None
    events = (convo or {}).get("events", [])

    band = policy_bands()["bands"]["default"][dpd_bucket(position["dpd"])]
    case_tickets = [t for t in getattr(tools.tickets, "tickets", []) if (t.get("summary") or {}).get("case_id") == case_id]
    bag = next((t["summary"].get("context_bag") for t in reversed(case_tickets) if t["summary"].get("context_bag")), None)
    verified_events = [e for e in events if e["type"] == "verified"]
    pack = {
        "case_id": case_id,
        "version": version,
        "status": ver["status"],
        "request": ver["request"],
        "version_history": [{"version": v["version"], "status": v["status"], "diff": v["diff"],
                             "superseded_by": v["superseded_by"]} for v in case["versions"]],
        "loan_position": position,
        "payment_behaviour": {
            "payments_12m": len([p for p in history if p["status"] == "success"]),
            "failed_or_reversed_12m": len([p for p in history if p["status"] in ("failed", "reversed")]),
            "recent": [{k: p[k] for k in ("txn_date", "amount", "channel", "status")} for p in history[-6:]],
        },
        "prior_concessions": store.waivers_by_account.get(account_id, []),
        "policy_check": {"dpd_bucket": dpd_bucket(position["dpd"]), **band,
                         "required_authority": authority_level(ver["request"].get("amount")),
                         "note": "policy fact, not a recommendation"},
        "risk_flags": {
            "gaming_flag": case["gaming_flag"],
            "restarts": case["restarts"],
            "failed_verifications": len([e for e in events if e["type"] == "verification_failed"]),
            "injection_attempts": len([e for e in events if e["type"] == "injection_detected"]),
            "offer_outstanding_on_previous": any(v["offer_outstanding"] for v in case["versions"]),
        },
        "compliance_checklist": {
            "identity_verification_method": verified_events[-1]["data"].get("method") if verified_events else None,
            "borrower_confirmed_change": ver["version"] == 1 or bool(ver["diff"]),
            "agent_made_no_commitment": not any(e["type"] == "output_blocked" and e["data"].get("reason") == "commitment"
                                                for e in events),
        },
        "conversation": {"transcript": (convo or {}).get("transcript", [])},
        # The same policy clauses the agent applied, plus the context bag from the approval ticket, so the
        # supervisor and compliance review against the same rules and see why the case exists.
        "policy_clauses": _case_clauses(ver["request"].get("type", "waiver"), bag),
        "context_bag": bag,
        "approver_decision": [d for d in case["decisions"] if d["version"] == version and d["role"] == "approver"],
    }
    return pack
