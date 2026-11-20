"""Policy gateway: the only path from the agent to systems of record.

* Account-data calls require a verified ``AuthScope``; otherwise they are denied
  (and the denial is logged). The model never holds unverified account data.
* ``account_id`` is injected from the scope. Nothing the LLM writes can change it.
* Results are cut down to the per-intent field allow-list (config/intent_fields.yaml).
* Every value that passes through is recorded so the output guard can check that
  each number in an outgoing message came from a tool result this turn.
"""

from __future__ import annotations

from typing import Any

from servicing_agent.config import intent_fields
from servicing_agent.contracts.cases import CaseRequest
from servicing_agent.contracts.events import EventType
from servicing_agent.contracts.state import AuthScope, Intent
from servicing_agent.agent.tools.authenticity import Authenticity, score_claim
from servicing_agent.agent.tools.reconciliation import ClaimResult, Verdict, reconcile


class NotVerified(Exception):
    pass


class PolicyGateway:
    def __init__(self, tools, cases, scope: AuthScope | None, conversation_id: str, settlement_window_hours: int = 24,
                 claims=None, message_id: str | None = None):
        self.tools = tools
        self.cases = cases
        self.claims = claims                # ClaimLedger (audit + replay guard); None in bare unit tests
        self.message_id = message_id
        self.scope = scope
        self.conversation_id = conversation_id
        self.window = settlement_window_hours
        self.allowed_numbers: set[float] = set()
        self.allowed_strings: set[str] = set()
        self.events: list[dict] = []
        self.policy_refs: list[str] = []      # policy clause IDs applied this turn (servicing_policy.md)
        self.decisions: list[dict] = []       # policy decisions taken this turn (clause, action, facts)

    def cite(self, *clause_ids: str):
        for c in clause_ids:
            if c and c not in self.policy_refs:
                self.policy_refs.append(c)

    # ---- bookkeeping
    def _event(self, type_: EventType, **data):
        self.events.append({"type": type_.value, "data": data})

    def allow(self, value: Any):
        """Record values (recursively) that may appear in outgoing text."""
        if isinstance(value, dict):
            for v in value.values():
                self.allow(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                self.allow(v)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float)):
            self.allowed_numbers.add(round(float(value), 2))
        elif isinstance(value, str):
            self.allowed_strings.add(value)

    def _require_scope(self, tool: str) -> AuthScope:
        if self.scope is None:
            self._event(EventType.TOOL_DENIED, tool=tool, reason="not_verified")
            raise NotVerified(tool)
        return self.scope

    def _filter(self, intent: Intent, result: dict) -> dict:
        allowed = intent_fields().get(intent.value)
        if allowed is None:
            raise PermissionError(f"no field allow-list for intent {intent.value}")
        out = {k: result[k] for k in allowed if k in result}
        self.allow(out)
        return out

    def _call(self, intent: Intent, tool: str, fn, *args, **kwargs) -> dict:
        scope = self._require_scope(tool)
        result = fn(scope.account_id, *args, **kwargs)
        self._event(EventType.TOOL_CALL, tool=tool, intent=intent.value, account_id=scope.account_id)
        return self._filter(intent, result)

    # ---- account-scoped tools
    def dues(self, intent: Intent = Intent.EMI_DUE_QUERY) -> dict:
        return self._call(intent, "lms.dues", self.tools.lms.dues)

    def payment_history(self, months: int = 12, limit: int = 5) -> dict:
        """Most recent payments, newest first. UTRs are masked to the last 4 digits."""
        def shape(account_id: str) -> dict:
            hist = self.tools.payments.history(account_id, months=months)[-limit:][::-1]
            return {"months": months, "payments": [
                {"date": p["txn_date"], "amount": p["amount"], "channel": p["channel"], "status": p["status"],
                 "utr": f"••••{p['utr'][-4:]}"} for p in hist]}
        return self._call(Intent.PAYMENT_HISTORY, "payments.history", shape)

    def loan_summary(self) -> dict:
        return self._call(Intent.LOAN_SUMMARY, "lms.loan_position", self.tools.lms.loan_position)

    def account_facts(self) -> dict:
        """The loan facts a free-form question may be answered from (allow-listed)."""
        return self._call(Intent.ACCOUNT_QUESTION, "lms.loan_position", self.tools.lms.loan_position)

    def charges(self) -> dict:
        def shape(account_id: str) -> dict:
            pos = self.tools.lms.loan_position(account_id)
            total = round((pos["bounce_charges"] or 0) + (pos["penal_charges"] or 0), 2)
            return {**pos, "total_charges": total}
        return self._call(Intent.CHARGES_QUERY, "lms.loan_position", shape)

    def foreclosure_quote(self) -> dict:
        return self._call(Intent.FORECLOSURE_QUOTE, "lms.foreclosure_quote", self.tools.lms.foreclosure_quote)

    def payment_link(self, amount: float, meta: dict | None = None) -> dict:
        scope = self._require_scope("paylinks.create")
        try:                                                 # meta: what the payment is for (e.g. a waiver)
            res = self.tools.paylinks.create(scope.account_id, amount, self.conversation_id, meta=meta)
        except TypeError:
            res = self.tools.paylinks.create(scope.account_id, amount, self.conversation_id)
        self._event(EventType.TOOL_CALL, tool="paylinks.create", account_id=scope.account_id)
        return self._filter(Intent.PAY_NOW, res)

    def receipt(self, utr: str) -> dict:
        return self._call(Intent.RECEIPT_REQUEST, "docs.receipt", self.tools.docs.receipt, utr)

    def statement(self, period_from: str, period_to: str) -> dict:
        return self._call(Intent.STATEMENT_REQUEST, "docs.statement", self.tools.docs.statement, period_from, period_to)

    def last_successful_payment(self) -> dict | None:
        scope = self._require_scope("payments.history")
        hist = [p for p in self.tools.payments.history(scope.account_id) if p["status"] == "success"]
        self._event(EventType.TOOL_CALL, tool="payments.history", account_id=scope.account_id)
        return hist[-1] if hist else None   # internal use only; never returned to the LLM

    def reconcile_claim(self, utr: str | None, amount: float | None, txn_date: str | None,
                        payee_vpa: str | None) -> ClaimResult:
        scope = self._require_scope("payments.reconcile")
        res = reconcile(self.tools.payments, scope.account_id, utr, amount, txn_date, payee_vpa,
                        window_hours=self.window)
        self._event(EventType.PAYMENT_VERDICT, verdict=res.verdict.value, reasons=res.reasons, utr=utr,
                    account_id=scope.account_id)
        if res.verdict == Verdict.MATCHED:
            self.tools.payments.mark_claimed(res.txn["utr"], scope.account_id)
        return res

    def assess_claim(self, slots: dict, res: ClaimResult) -> Authenticity:
        """Authenticity score for a claim that didn't match. Ledger and loan values stay internal:
        only the score, label and feature contributions leave (to the audit log and the ticket)."""
        scope = self._require_scope("claims.assess")
        utr = slots.get("utr")
        ledger_txn = self.tools.payments.find_by_utr(utr) if utr else None
        loan = self.tools.lms.loan_position(scope.account_id)
        a = score_claim(slots, res, ledger_txn, scope.account_id, loan,
                        confidence=float(slots.get("confidence") or 1.0),
                        low_confidence_fields=int(slots.get("low_confidence_fields") or 0))
        self._event(EventType.TOOL_CALL, tool="claims.assess", account_id=scope.account_id, **a.as_dict())
        # What the borrower told us may be repeated back to them.
        if slots.get("amount") is not None:
            self.allow(float(slots["amount"]))
        if utr and len(utr) >= 4:
            self.allow(utr[-4:])
        return a

    @staticmethod
    def mask_account(account_id: str) -> str:
        return f"{account_id[:4]}••••{account_id[-4:]}"

    def borrowers_other_loan(self, utr: str | None) -> str | None:
        """If this UTR is on another loan of the SAME borrower, that loan's masked id (they picked
        the wrong loan; not a fake). Someone else's loan -> None."""
        scope = self._require_scope("payments.owner")
        txn = self.tools.payments.find_by_utr(utr) if utr else None
        if not txn or txn["account_id"] == scope.account_id:
            return None
        same = getattr(self.tools.lms, "same_borrower", None)
        if not same or not same(scope.contact_id, txn["account_id"]):
            return None
        masked = self.mask_account(txn["account_id"])
        self.allow([masked, self.mask_account(scope.account_id)])
        self._event(EventType.TOOL_CALL, tool="payments.owner", account_id=scope.account_id, other_loan=masked)
        return masked

    def latest_claim(self, minutes: int = 30) -> dict | None:
        rows = self.recent_claims(minutes=minutes, limit=1)
        return rows[0] if rows else None

    def recent_claims(self, minutes: int = 24 * 60, limit: int = 5) -> list[dict]:
        """Payment claims on the verified loan (any session), newest first. The borrower's own figures are
        allow-listed so they can be repeated back."""
        scope = self._require_scope("claims.recent")
        if not self.claims or not hasattr(self.claims, "recent"):
            return []
        from datetime import datetime, timedelta
        rows = self.claims.recent(scope.account_id, datetime.utcnow() - timedelta(minutes=minutes), limit=limit)
        for row in rows:
            self.allow([x for x in (row.get("amount"), (row.get("utr") or "")[-4:] or None, row.get("txn_date"))
                        if x is not None])
            if row.get("ticket_id"):
                self.allow({"ticket_id": row["ticket_id"]})
        return rows

    def open_ticket(self, request_type: str) -> dict | None:
        """The newest still-open ticket of this type on the verified loan (to avoid duplicates)."""
        scope = self._require_scope("tickets.open")
        finder = getattr(self.tools.tickets, "open_for_account", None)
        t = finder(scope.account_id, request_type) if finder else None
        if t:
            self.allow({"ticket_id": t["ticket_id"]})
        return t

    def recent_contact_change(self) -> bool:
        """SIM-swap signal (SEC-01): the verified mobile was added to the loan within the last N days."""
        from datetime import date, timedelta
        from servicing_agent.agent.policy import policy
        if self.scope is None or self.scope.contact_id == "system-recheck":
            return False
        getter = getattr(self.tools.lms, "contact_added_on", None)
        added = getter(self.scope.contact_id) if getter else None
        if not added:
            return False
        try:
            days = int(policy().get("SEC-01").action.get("recent_days", 7))
        except StopIteration:
            days = 7
        try:
            return date.fromisoformat(added[:10]) >= date.today() - timedelta(days=days)
        except ValueError:
            return False

    def retire_case_tickets(self, case_id: str, new_version: int) -> list[dict]:
        """The request was replaced: close the old version's open tickets so reviewers stop working on it."""
        scope = self._require_scope("tickets.supersede")
        fn = getattr(self.tools.tickets, "supersede_for_case", None)
        old = fn(scope.account_id, case_id, f"Replaced by version {new_version} at the borrower's request") if fn else []
        if old:
            self._event(EventType.TOOL_CALL, tool="tickets.supersede", case_id=case_id,
                        tickets=[t["ticket_id"] for t in old], new_version=new_version)
        return old

    def ticket_status(self, ticket_id: str | None) -> str | None:
        getter = getattr(self.tools.tickets, "status_of", None)
        return getter(ticket_id) if (getter and ticket_id) else None

    def previous_claim(self, utr: str | None) -> dict | None:
        """This account's earlier claim for the same UTR, if any (used to avoid duplicate tickets)."""
        scope = self._require_scope("claims.previous")
        prev = self.claims.get(scope.account_id, utr) if self.claims else None
        if prev and prev.get("ticket_id"):
            self.allow({"ticket_id": prev["ticket_id"]})
        return prev

    def record_claim(self, res: ClaimResult, slots: dict, *, ticket: dict | None = None) -> dict | None:
        """Write the claim to the payment_claims ledger (audit). Resends update the same row."""
        scope = self._require_scope("claims.record")
        if not self.claims or res.verdict == Verdict.NEED_UTR:
            return None
        verdict = "MATCHED" if res.verdict == Verdict.ALREADY_CONFIRMED else res.verdict.value
        row = self.claims.record(
            conversation_id=self.conversation_id, account_id=scope.account_id,
            utr=(res.txn or {}).get("utr") or slots.get("utr"), result=verdict,
            source=slots.get("source") or "text", message_id=self.message_id,
            amount=slots.get("amount"), txn_date=slots.get("txn_date"), payee_vpa=slots.get("payee_vpa"),
            media_id=slots.get("media_id"), matched_txn_id=(res.txn or {}).get("txn_id"),
            reasons=res.reasons, ticket_id=(ticket or {}).get("ticket_id"), queue=(ticket or {}).get("queue"),
            count_attempt=not (slots.get("recheck") or slots.get("from_context")),
        )
        self._event(EventType.TOOL_CALL, tool="claims.record", account_id=scope.account_id,
                    verdict=verdict, attempts=row["attempts"], source=row["source"])
        return row

    def record_receipt(self, txn_id: str, document_id: str | None) -> bool:
        scope = self._require_scope("receipts.record")
        return self.claims.record_receipt(txn_id, scope.account_id, document_id) if self.claims else True

    # ---- cases (waiver / settlement)
    def open_case(self):
        scope = self._require_scope("cases.open")
        case = self.cases.open_case_for_account(scope.account_id)
        if case:
            self.allow({"case_id": case.case_id, "version": case.version, "amount": case.request.amount})
        return case

    def withdraw_case(self, case_id: str, version: int):
        scope = self._require_scope("cases.withdraw")
        if self.cases.get(case_id)["account_id"] != scope.account_id:
            self._event(EventType.TOOL_DENIED, tool="cases.withdraw", reason="other_account")
            raise PermissionError("case belongs to another account")
        case = self.cases.withdraw(case_id, version)
        self._event(EventType.TOOL_CALL, tool="cases.withdraw", case_id=case_id, version=version)
        return case

    def hardship_facts(self) -> dict:
        """Overdue, next due date, DPD band and confirmed payments in the last 30 days, for the hardship ladder."""
        from datetime import date, timedelta
        scope = self._require_scope("hardship.facts")
        pos = self.tools.lms.loan_position(scope.account_id)
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        paid = round(sum(float(p["amount"]) for p in self.tools.payments.history(scope.account_id, months=2)
                         if p["status"] == "success" and p["txn_date"] >= cutoff), 2)
        overdue = float(pos.get("overdue_amount") or 0)
        sanction = float(pos.get("sanction_amount") or 0)
        principal = float(pos.get("principal_outstanding") or 0)
        # Nothing overdue means not past due, whatever a stale dpd column says; the band always follows the dpd.
        from servicing_agent.data.store import dpd_band
        dpd = int(pos.get("dpd") or 0) if overdue > 0 else 0
        out = {"overdue": overdue, "next_due_date": pos.get("next_due_date"), "dpd_band": dpd_band(dpd),
               "paid_30d": paid, "emi": float(pos.get("emi_amount") or 0), "principal_outstanding": principal,
               "dpd": dpd,
               "charges": round(float(pos.get("bounce_charges") or 0) + float(pos.get("penal_charges") or 0), 2),
               "recovery": round((sanction - principal) / sanction, 3) if sanction else 0.0,
               "preapproved_flag": str(pos.get("preapproved_settlement") or "").upper() == "Y"}
        self.allow([overdue, paid, out["emi"], round(max(overdue - paid, 0), 2), out["charges"],
                    round(max(overdue - out["charges"], 0), 2)])
        if out["next_due_date"]:
            self.allow(out["next_due_date"])
        self._event(EventType.TOOL_CALL, tool="hardship.facts", account_id=scope.account_id, dpd_band=out["dpd_band"],
                    paid_30d=paid, overdue=overdue)
        return out

    def case_restarts(self, case_id: str) -> int:
        """How many times this open request has been changed (re-initiated) by the borrower."""
        scope = self._require_scope("cases.restarts")
        try:
            c = self.cases.get(case_id)
        except Exception:  # noqa: BLE001
            return 0
        return int(c.get("restarts") or 0) if c.get("account_id") == scope.account_id else 0

    def create_case(self, req: CaseRequest):
        scope = self._require_scope("cases.create")
        case = self.cases.create(scope.account_id, self.conversation_id, req)
        self._event(EventType.CASE_CREATED, case_id=case.case_id, version=case.version, account_id=scope.account_id)
        self.allow({"case_id": case.case_id})
        return case

    def supersede_case(self, case_id: str, from_version: int, req: CaseRequest, message_id: str | None):
        scope = self._require_scope("cases.supersede")
        current = self.cases.get(case_id)
        if current["account_id"] != scope.account_id:
            self._event(EventType.TOOL_DENIED, tool="cases.supersede", reason="other_account")
            raise PermissionError("case belongs to another account")
        case = self.cases.supersede(case_id, from_version, req, reason="borrower changed request",
                                    borrower_message_id=message_id)
        self._event(EventType.CASE_SUPERSEDED, case_id=case_id, from_version=from_version, to_version=case.version)
        self.allow({"case_id": case.case_id})
        return case

    # ---- not account-scoped
    def ticket(self, request_type: str, summary: dict) -> dict:
        t = self.tools.tickets.create(request_type, summary, self.conversation_id,
                                      account_id=self.scope.account_id if self.scope else None)
        self._event(EventType.TICKET_CREATED, ticket_id=t["ticket_id"], queue=t["queue"], request_type=request_type)
        self.allow({"ticket_id": t["ticket_id"]})
        return t
