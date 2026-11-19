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
from servicing_agent.data.reconciliation import ClaimResult, Verdict, reconcile


class NotVerified(Exception):
    pass


class PolicyGateway:
    def __init__(self, tools, cases, scope: AuthScope | None, conversation_id: str, settlement_window_hours: int = 24):
        self.tools = tools
        self.cases = cases
        self.scope = scope
        self.conversation_id = conversation_id
        self.window = settlement_window_hours
        self.allowed_numbers: set[float] = set()
        self.allowed_strings: set[str] = set()
        self.events: list[dict] = []

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

    def foreclosure_quote(self) -> dict:
        return self._call(Intent.FORECLOSURE_QUOTE, "lms.foreclosure_quote", self.tools.lms.foreclosure_quote)

    def payment_link(self, amount: float) -> dict:
        scope = self._require_scope("paylinks.create")
        res = self.tools.paylinks.create(scope.account_id, amount)
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

    # ---- cases (waiver / settlement)
    def open_case(self):
        scope = self._require_scope("cases.open")
        case = self.cases.open_case_for_account(scope.account_id)
        if case:
            self.allow({"case_id": case.case_id, "version": case.version, "amount": case.request.amount})
        return case

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
